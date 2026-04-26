import logging
import signal
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    PIPELINES_DIR, LOCAL_TZ, REVIEW_MODES, OWNER_NAME,
    MAX_SPEC_REVISE_CYCLES,
    SPEC_BLOCK_TIMERS,
    MAX_SPEC_RETRIES, GITLAB_NAMESPACE, IMPLEMENTERS,
)
from engine.fsm import get_min_reviews
from pipeline_io import (
    load_pipeline, update_pipeline, save_pipeline,
    add_history, now_iso, get_path, default_spec_config,
)
from notify import (
    notify_discord,
    spec_notify_approved_forced, spec_notify_review_start,
)

logger = logging.getLogger(__name__)


# === Spec Mode Commands ===

def _reset_review_requests(spec_config: dict) -> None:
    """review_requestsの全エントリをpendingにリセット（§5.4）"""
    for entry in spec_config.get("review_requests", {}).values():
        entry["status"] = "pending"
        entry["sent_at"] = None
        entry["timeout_at"] = None
        entry["last_nudge_at"] = None
        entry["response"] = None


def _archive_current_reviews(spec_config: dict) -> None:
    """current_reviewsをreview_historyにアーカイブし、current_reviewsをクリア（§12.2）"""
    cr = spec_config.get("current_reviews", {})
    if not cr or not cr.get("entries"):
        spec_config["current_reviews"] = {}
        return

    _SEV_MAP = {"p0": "critical", "p1": "major", "p2": "minor"}
    reviews_summary = {}
    merged = {"critical": 0, "major": 0, "minor": 0, "suggestion": 0}
    for reviewer, entry in cr.get("entries", {}).items():
        counts = {}
        for item in entry.get("items", []):
            sev = _SEV_MAP.get(item.get("severity", "minor").lower(),
                               item.get("severity", "minor").lower())
            counts[sev] = counts.get(sev, 0) + 1
            if sev in merged:
                merged[sev] += 1
        reviews_summary[reviewer] = {
            "verdict": entry.get("verdict"),
            "counts": counts,
        }

    history_entry = {
        "rev": cr.get("reviewed_rev", spec_config.get("current_rev", "?")),
        "rev_index": spec_config.get("rev_index", 0),
        "reviews": reviews_summary,
        "merged_counts": merged,
        "commit": spec_config.get("last_commit"),
        "timestamp": datetime.now(LOCAL_TZ).isoformat(),
    }
    spec_config.setdefault("review_history", []).append(history_entry)
    spec_config["current_reviews"] = {}


def cmd_spec_start(args):
    """spec modeパイプライン開始（§4.2, §2.5, §2.6, §3.3）"""
    path = get_path(args.project)
    if not path.exists():
        # パイプライン JSON が未作成 → 自動 init
        PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
        # repo_path を推測: /mnt/s/wsl/work/project/<project>
        default_repo = f"/mnt/s/wsl/work/project/{args.project}"
        repo_path = default_repo if Path(default_repo).is_dir() else ""
        data = {
            "project": args.project,
            "gitlab": f"{GITLAB_NAMESPACE}/{args.project}",
            "repo_path": repo_path,
            "state": "IDLE",
            "enabled": False,
            "implementer": args.implementer or IMPLEMENTERS[0],
            "batch": [],
            "history": [],
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        save_pipeline(path, data)
        print(f"Auto-initialized: {path} (repo_path={repo_path})")
    data = load_pipeline(path)
    # 事前チェック（早期エラー用、本番チェックはdo_start内flock内で再実行）
    if data.get("state", "IDLE") != "IDLE":
        raise SystemExit(f"Cannot start: state is {data['state']} (expected IDLE)")
    if data.get("spec_mode"):
        raise SystemExit("spec_mode already active")

    if args.skip_review and args.review_only:
        raise SystemExit("--skip-review and --review-only are mutually exclusive")

    # specファイル存在チェック（repo_path相対）
    repo_path = data.get("repo_path", "")
    if repo_path and not Path(args.spec).is_absolute():
        spec_resolved = Path(repo_path) / args.spec
    else:
        spec_resolved = Path(args.spec)
    if not spec_resolved.exists():
        raise SystemExit(f"Spec file not found: {spec_resolved}")

    # spec_path から revN を自動推定
    from spec_revise import extract_rev_from_path
    detected_rev = extract_rev_from_path(str(spec_resolved))

    rev_arg: int | None = getattr(args, "rev", None)
    if rev_arg is not None:
        if rev_arg < 1:
            raise SystemExit("--rev must be >= 1")
        if detected_rev is not None and detected_rev != rev_arg:
            raise SystemExit(
                f"--rev={rev_arg} conflicts with spec file name "
                f"(detected rev{detected_rev} from {spec_resolved.name})"
            )
        if detected_rev is None and rev_arg > 1:
            raise SystemExit(
                f"--rev={rev_arg} requires spec file with '-rev{rev_arg}' suffix. "
                f"Got: {spec_resolved.name}"
            )

    effective_rev: int = rev_arg if rev_arg is not None else (detected_rev if detected_rev is not None else 1)

    # §2.6 優先順位ルール適用
    auto_continue = args.auto_continue
    review_only = args.review_only
    no_queue = args.no_queue
    skip_review = args.skip_review

    if skip_review:
        auto_continue = True
    if review_only:
        auto_continue = False
        no_queue = True

    review_mode = args.review_mode or data.get("review_mode")
    if not review_mode:
        raise SystemExit(
            "review_mode is not set. Use --review-mode <mode> or set it with: "
            f"gokrax review-mode --pj {args.project} --mode <mode>"
        )
    if review_mode not in REVIEW_MODES:
        raise SystemExit(f"Unknown review_mode: {review_mode!r} (available: {sorted(REVIEW_MODES.keys())})")
    from engine.fsm import _build_phase_config
    phase_config = _build_phase_config(REVIEW_MODES[review_mode], "design")
    review_requests = {
        r: {
            "status": "pending",
            "sent_at": None,
            "timeout_at": None,
            "last_nudge_at": None,
            "response": None,
        }
        for r in phase_config["members"]
    }

    pipelines_dir = str(Path(PIPELINES_DIR) / args.project / "spec-reviews")

    # ロック外: _reset_reviewers（ネットワーク I/O）
    if not skip_review:
        from engine.reviewer import _reset_reviewers
        excluded = _reset_reviewers(phase_config, implementer=args.implementer or "")
        excluded_reviewers_only = [r for r in excluded if r in phase_config["members"]]
    else:
        excluded = []
        excluded_reviewers_only = []

    def do_start(data):
        # flock内で再チェック（TOCTOU回避）
        if data.get("state", "IDLE") != "IDLE":
            raise SystemExit(f"Cannot start: state is {data['state']} (expected IDLE)")
        if data.get("spec_mode"):
            raise SystemExit("spec_mode already active")
        sc = default_spec_config()
        sc.update({
            "spec_path": str(spec_resolved.resolve()),
            "spec_implementer": args.implementer,
            "review_only": review_only,
            "no_queue": no_queue,
            "skip_review": skip_review,
            "auto_continue": auto_continue,
            "auto_qrun": args.auto_qrun,
            "max_revise_cycles": args.max_cycles if args.max_cycles is not None else MAX_SPEC_REVISE_CYCLES,
            "model": args.model,
            "review_requests": review_requests,
            "pipelines_dir": pipelines_dir,
        })
        sc["current_rev"] = str(effective_rev)
        sc["rev_index"] = effective_rev
        data["spec_mode"] = True
        old_state = data.get("state", "IDLE")
        data["state"] = "SPEC_APPROVED" if skip_review else "SPEC_REVIEW"
        add_history(data, old_state, data["state"], actor="cli")
        data["enabled"] = True
        if args.review_mode:
            data["review_mode"] = review_mode
        data["spec_config"] = sc
        # 前回 run の残留値をクリア
        data.pop("excluded_reviewers", None)
        data.pop("min_reviews_override", None)
        # _save_excluded のロジックを統合
        if excluded_reviewers_only:
            data["excluded_reviewers"] = excluded
            rr = sc.get("review_requests", {})
            for r in excluded_reviewers_only:
                rr.pop(r, None)
            effective_count = len(phase_config["members"]) - len(excluded_reviewers_only)
            min_reviews = get_min_reviews(phase_config)
            if effective_count < min_reviews:
                data["min_reviews_override"] = max(1, effective_count)

    update_pipeline(path, do_start)
    from gokrax import _start_loop
    _start_loop()

    # Discord 通知（除外後の正確な reviewer_count を使用）
    target = "SPEC_APPROVED" if skip_review else "SPEC_REVIEW"
    if not skip_review:
        effective_reviewer_count = len(review_requests) - len(excluded_reviewers_only) if excluded_reviewers_only else len(review_requests)
        try:
            notify_discord(spec_notify_review_start(args.project, str(effective_rev), effective_reviewer_count))
        except Exception:
            logger.warning("Failed to send review_start notification")
    print(f"{args.project}: spec mode started (spec={args.spec}) → {target}")


def cmd_spec_approve(args):
    """SPEC_APPROVEDに遷移（§4.3）"""
    _APPROVE_ALLOWED = ("SPEC_REVIEW", "SPEC_STALLED", "SPEC_REVISE")
    path = get_path(args.project)
    # 全チェックをflock内に移動してTOCTOU回避
    ctx = {}  # pass values from inside lock to outside

    def do_approve(data):
        state = data.get("state")
        if state not in _APPROVE_ALLOWED:
            raise SystemExit(
                f"Cannot approve: state is {state} "
                f"(expected one of {_APPROVE_ALLOWED})"
            )
        if not data.get("spec_mode"):
            raise SystemExit("Cannot approve: spec_mode is not active")

        sc = data.get("spec_config", {})

        if not args.force:
            cr = sc.get("current_reviews", {})
            for reviewer, entry in cr.get("entries", {}).items():
                v = entry.get("verdict", "")
                if v in ("P0", "P1"):
                    raise SystemExit(
                        f"Cannot approve: {reviewer} has {v}. Use --force to override."
                    )

        if args.force:
            # remaining_p1_items 収集（archive前に取得）
            remaining = []
            cr = sc.get("current_reviews", {})
            for reviewer, entry in cr.get("entries", {}).items():
                if entry.get("verdict", "") in ("P0", "P1"):
                    for item in entry.get("items", []):
                        remaining.append(f"{reviewer}:{item.get('id', '?')}")

            ctx["from_state"] = state
            ctx["rev"] = sc.get("current_rev", "?")

            _archive_current_reviews(sc)
            sc.setdefault("force_events", []).append({
                "at": datetime.now(LOCAL_TZ).isoformat(),
                "actor": OWNER_NAME,
                "from_state": state,
                "rev": sc.get("current_rev", "?"),
                "rev_index": sc.get("rev_index", 0),
                "remaining_p1_items": remaining,
            })
        add_history(data, state, "SPEC_APPROVED", actor="cli:spec-approve")
        data["state"] = "SPEC_APPROVED"

    update_pipeline(path, do_approve)

    if args.force:
        try:
            remaining_count = len(ctx.get("remaining_p1_items", []))
            notify_discord(
                spec_notify_approved_forced(args.project, ctx.get("rev", "?"), remaining_count)
            )
        except Exception:
            pass

    print(f"{args.project}: → SPEC_APPROVED" + (" (forced)" if args.force else ""))


def cmd_spec_continue(args):
    """SPEC_APPROVED → ISSUE_SUGGESTION"""
    path = get_path(args.project)
    data = load_pipeline(path)
    if data.get("state") != "SPEC_APPROVED":
        raise SystemExit(f"Cannot continue: state is {data['state']} (expected SPEC_APPROVED)")

    def do_continue(data):
        sc = data.setdefault("spec_config", {})
        # C1: review_requests を全員 pending にリセット
        rr = sc.get("review_requests", {})
        for reviewer in rr:
            rr[reviewer] = {
                "status": "pending",
                "sent_at": None,
                "timeout_at": None,
                "last_nudge_at": None,
                "response": None,
            }
        # A3: history 記録
        add_history(data, "SPEC_APPROVED", "ISSUE_SUGGESTION", actor="cli")
        data["state"] = "ISSUE_SUGGESTION"

    update_pipeline(path, do_continue)
    print(f"{args.project}: SPEC_APPROVED → ISSUE_SUGGESTION")


def cmd_spec_done(args):
    """SPEC_DONE → IDLE (normal completion of spec mode)"""
    path = get_path(args.project)
    data = load_pipeline(path)
    if data.get("state") != "SPEC_DONE":
        raise SystemExit(f"Cannot done: state is {data['state']} (expected SPEC_DONE)")

    def do_done(data):
        add_history(data, "SPEC_DONE", "IDLE", actor="cli:spec-done")
        data["state"] = "IDLE"
        data["spec_mode"] = False
        data["spec_config"] = {}
        data["enabled"] = False
        data.pop("review_mode", None)
        data.pop("excluded_reviewers", None)
        data.pop("min_reviews_override", None)
        data.pop("review_config", None)
        data.pop("reviewer_number_map", None)

    update_pipeline(path, do_done)
    from gokrax import _any_pj_enabled, _stop_loop
    if not _any_pj_enabled():
        _stop_loop()
        print(f"{args.project}: SPEC_DONE → IDLE (spec mode ended, watchdog disabled, loop stopped)")
    else:
        print(f"{args.project}: SPEC_DONE → IDLE (spec mode ended, watchdog disabled)")




def cmd_spec_stop(args):
    """spec modeを強制停止してIDLEに戻す"""
    path = get_path(args.project)
    data = load_pipeline(path)
    if not data.get("spec_mode"):
        raise SystemExit(f"{args.project}: spec mode is not active")

    old_state = data.get("state", "IDLE")

    def do_stop(data):
        old = data.get("state", "IDLE")  # acquired inside lock
        data["state"] = "IDLE"
        data["spec_mode"] = False
        data["spec_config"] = {}
        data["enabled"] = False
        add_history(data, old, "IDLE", actor="cli:spec-stop")
        data.pop("review_mode", None)
        data.pop("excluded_reviewers", None)
        data.pop("min_reviews_override", None)
        data.pop("review_config", None)
        data.pop("reviewer_number_map", None)

    update_pipeline(path, do_stop)
    from gokrax import _any_pj_enabled, _stop_loop
    if not _any_pj_enabled():
        _stop_loop()
        print(f"{args.project}: spec mode stopped ({old_state} → IDLE, watchdog disabled, loop stopped)")
    else:
        print(f"{args.project}: spec mode stopped ({old_state} → IDLE, watchdog disabled)")

def cmd_spec_retry(args):
    """SPEC_REVIEW_FAILED → SPEC_REVIEW（§4.5）"""
    path = get_path(args.project)
    data = load_pipeline(path)
    if data.get("state") != "SPEC_REVIEW_FAILED":
        raise SystemExit(f"Cannot retry: state is {data['state']} (expected SPEC_REVIEW_FAILED)")

    def do_retry(data):
        sc = data["spec_config"]
        _reset_review_requests(sc)
        sc["current_reviews"] = {}
        add_history(data, "SPEC_REVIEW_FAILED", "SPEC_REVIEW", actor="cli:spec-retry")
        data["state"] = "SPEC_REVIEW"

    update_pipeline(path, do_retry)
    print(f"{args.project}: SPEC_REVIEW_FAILED → SPEC_REVIEW (retry)")


def cmd_spec_resume(args):
    """SPEC_PAUSED → paused_from（§4.6）"""
    path = get_path(args.project)
    data = load_pipeline(path)
    if data.get("state") != "SPEC_PAUSED":
        raise SystemExit(f"Cannot resume: state is {data['state']} (expected SPEC_PAUSED)")

    sc = data.get("spec_config", {})
    paused_from = sc.get("paused_from")
    if not paused_from:
        raise SystemExit("Cannot resume: paused_from is null")

    def do_resume(data):
        sc = data["spec_config"]
        now = datetime.now(LOCAL_TZ)
        target = sc["paused_from"]

        if target == "SPEC_REVIEW":
            _reset_review_requests(sc)
            sc["current_reviews"] = {}

        for entry in sc.get("review_requests", {}).values():
            if entry.get("status") == "pending":
                entry["timeout_at"] = (
                    now + timedelta(seconds=SPEC_BLOCK_TIMERS["SPEC_REVIEW"])
                ).isoformat()

        if target == "ISSUE_SUGGESTION":
            for entry in sc.get("issue_suggestions", {}).values():
                if isinstance(entry, dict) and entry.get("status") == "pending":
                    entry["timeout_at"] = (
                        now + timedelta(seconds=SPEC_BLOCK_TIMERS["ISSUE_SUGGESTION"])
                    ).isoformat()

        sc.setdefault("retry_counts", {})[target] = 0
        add_history(data, "SPEC_PAUSED", target, actor="cli:spec-resume")
        data["state"] = target
        sc["paused_from"] = None

    update_pipeline(path, do_resume)
    print(f"{args.project}: SPEC_PAUSED → {paused_from} (resumed)")


def cmd_spec_extend(args):
    """SPEC_STALLED → SPEC_REVISE（MAX_CYCLES増加）（§4.7）"""
    path = get_path(args.project)
    data = load_pipeline(path)
    if data.get("state") != "SPEC_STALLED":
        raise SystemExit(f"Cannot extend: state is {data['state']} (expected SPEC_STALLED)")

    n = args.cycles

    def do_extend(data):
        sc = data["spec_config"]
        sc["max_revise_cycles"] = sc.get("max_revise_cycles", MAX_SPEC_REVISE_CYCLES) + n
        add_history(data, "SPEC_STALLED", "SPEC_REVISE", actor="cli:spec-extend")
        data["state"] = "SPEC_REVISE"

    update_pipeline(path, do_extend)
    print(f"{args.project}: SPEC_STALLED → SPEC_REVISE (max_cycles += {n})")


def cmd_spec_status(args):
    """spec mode ステータス表示（§4.4）"""
    path = get_path(args.project)
    data = load_pipeline(path)
    sc = data.get("spec_config", {})

    if not data.get("spec_mode"):
        print(f"{args.project}: spec mode is not active")
        return

    state = data.get("state", "?")
    rev = sc.get("current_rev", "?")
    cycle = f"rev{sc.get('rev_index', 0)}/{sc.get('max_revise_cycles', '?')}"

    retry_parts = []
    for k, v in sc.get("retry_counts", {}).items():
        retry_parts.append(f"{k}={v}/{MAX_SPEC_RETRIES}")
    retries = ", ".join(retry_parts) if retry_parts else "none"

    print(f"gokrax [{state}] rev{rev} (cycle {cycle}, retries: {retries})")
    print(f"  spec: {sc.get('spec_path', '?')}")
    print(f"  implementer: {sc.get('spec_implementer', '?')}")

    rr = sc.get("review_requests", {})
    cr_entries = sc.get("current_reviews", {}).get("entries", {})
    reviewer_parts = []
    for r, entry in rr.items():
        status = entry.get("status", "?")
        if r in cr_entries:
            ce = cr_entries[r]
            verdict = ce.get("verdict", "?")
            items = ce.get("items", [])
            p0_count = sum(
                1 for i in items if i.get("severity", "").upper() in ("CRITICAL", "P0")
            )
            reviewer_parts.append(f"{r}({'✅' if verdict == 'APPROVE' else verdict} P0×{p0_count})")
        else:
            reviewer_parts.append(f"{r}({'⏳' if status == 'pending' else status})")
    print(f"  reviewers: {', '.join(reviewer_parts)}")

    review_mode = data.get("review_mode", "")
    mode_cfg = REVIEW_MODES.get(review_mode)
    min_valid = get_min_reviews(mode_cfg) if mode_cfg else 0
    print(f"  min_valid: {min_valid} ({review_mode} mode)")
    print(f"  auto_qrun: {sc.get('auto_qrun', False)}")
    print(f"  pipelines_dir: {sc.get('pipelines_dir', '?')}")


def cmd_spec_review_submit(args):
    """spec mode レビュー結果をYAMLファイルから取り込む"""
    path = get_path(args.project)

    # ファイル読み込み
    review_path = Path(args.file)
    if not review_path.is_file():
        raise SystemExit(f"File not found: {args.file}")
    raw_text = review_path.read_text(encoding="utf-8")

    # パース（既存の parse_review_yaml を使用 — spec_review.py §5.5）
    # フェンス付き（```yaml ... ```）→ そのまま解析
    # フェンスなし（素のYAML）→ フェンスで包んで再試行
    from spec_review import parse_review_yaml
    result = parse_review_yaml(raw_text, args.reviewer)
    if not result.parse_success:
        result = parse_review_yaml(f"```yaml\n{raw_text}\n```", args.reviewer)
    if not result.parse_success:
        raise SystemExit(
            f"Failed to parse review YAML from {args.file}. "
            f"Ensure the file contains valid YAML with 'verdict' and 'items' keys."
        )

    # SIGTERM遅延（cmd_review L636-648 と同パターン）
    _deferred = False
    _orig = signal.getsignal(signal.SIGTERM)

    def _defer_sigterm(signum, frame):
        nonlocal _deferred
        _deferred = True

    signal.signal(signal.SIGTERM, _defer_sigterm)

    try:
        # pipeline JSON に書き込み
        def do_submit(data):
            state = data.get("state", "IDLE")
            if state != "SPEC_REVIEW":
                raise SystemExit(f"Not in SPEC_REVIEW state: {state}")

            sc = data.get("spec_config", {})
            rr = sc.get("review_requests", {})

            # reviewer が review_requests に存在するか確認
            if args.reviewer not in rr:
                raise SystemExit(
                    f"Reviewer '{args.reviewer}' not in review_requests. "
                    f"Valid reviewers: {list(rr.keys())}"
                )

            # 冪等性: 既に received なら上書きせずスキップ
            cr = sc.setdefault("current_reviews", {})
            entries = cr.setdefault("entries", {})
            if args.reviewer in entries and entries[args.reviewer].get("status") == "received":
                print(f"{args.reviewer}: already submitted, skipping")
                return

            # items を dict のリストに変換（SpecReviewItem → dict）
            items_dicts = [
                {
                    "id": item.id,
                    "severity": item.severity,
                    "section": item.section,
                    "title": item.title,
                    "description": item.description,
                    "suggestion": item.suggestion,
                    "reviewer": item.reviewer,
                    "normalized_id": item.normalized_id,
                }
                for item in result.items
            ]

            # current_reviews.entries に書き込み（§3.1 received 不変条件を満たす形式）
            entries[args.reviewer] = {
                "status": "received",
                "verdict": result.verdict,
                "items": items_dicts,
                "raw_text": result.raw_text,
                "parse_success": True,
            }

            # review_requests のステータスも更新（§5.2: pending → received）
            rr[args.reviewer]["status"] = "received"

            # I1: reviewed_rev を設定（§3.1 準拠）
            if "reviewed_rev" not in cr:
                cr["reviewed_rev"] = sc.get("current_rev", "?")

            sc["current_reviews"] = cr
            data["spec_config"] = sc

        data = update_pipeline(path, do_submit)
    finally:
        signal.signal(signal.SIGTERM, _orig)
        if _deferred:
            signal.raise_signal(signal.SIGTERM)

    # 結果表示
    print(f"{args.project}: spec review by {args.reviewer} submitted")
    print(f"  verdict: {result.verdict}")
    print(f"  items: {len(result.items)}")
    for item in result.items:
        print(f"    {item.normalized_id} [{item.severity}] {item.title}")

    # §12.1: レビュー原文を pipelines_dir にも保存（アーカイブ用）
    sc = data.get("spec_config", {})
    pipelines_dir = sc.get("pipelines_dir")
    if pipelines_dir:
        spec_name = Path(sc.get("spec_path", "")).stem
        current_rev = sc.get("current_rev", "1")
        ts = datetime.now(LOCAL_TZ).strftime("%Y%m%dT%H%M%S")
        archive_name = f"{ts}_{args.reviewer}_{spec_name}_rev{current_rev}.yaml"
        archive_path = Path(pipelines_dir) / archive_name
        try:
            archive_path.write_text(raw_text, encoding="utf-8")
            archive_path.chmod(0o600)
            print(f"  archived: {archive_path}")
        except OSError as e:
            print(f"  warning: archive failed: {e}")


def cmd_spec_revise_submit(args):
    """SPEC_REVISE: implementer改訂完了報告をファイルから投入"""
    path = get_path(args.project)

    review_path = Path(args.file)
    if not review_path.is_file():
        raise SystemExit(f"File not found: {args.file}")
    raw_text = review_path.read_text(encoding="utf-8")

    # parse_revise_response は current_rev 必須 → ロック内で一括検証

    _deferred = False
    _orig = signal.getsignal(signal.SIGTERM)

    def _defer_sigterm(signum, frame):
        nonlocal _deferred
        _deferred = True

    signal.signal(signal.SIGTERM, _defer_sigterm)

    try:
        def do_submit(data):
            state = data.get("state", "IDLE")
            if state != "SPEC_REVISE":
                raise SystemExit(f"Not in SPEC_REVISE state: {state}")
            sc = data.get("spec_config", {})

            if sc.get("_revise_response"):
                print(f"{args.project}: revise response already submitted, skipping")
                return

            from spec_revise import parse_revise_response

            current_rev = str(sc.get("current_rev", "1"))

            canonical_text = raw_text
            parsed = parse_revise_response(raw_text, current_rev)
            if parsed is None:
                fenced = f"```yaml\n{raw_text}\n```"
                parsed = parse_revise_response(fenced, current_rev)
                if parsed is not None:
                    canonical_text = fenced
            if parsed is None:
                raise SystemExit(
                    f"Failed to parse revise response from {args.file}. "
                    f"Expected YAML with status=done, new_rev, commit (7+ hex), "
                    f"changes (added_lines/removed_lines)."
                )

            sc["_revise_response"] = canonical_text
            data["spec_config"] = sc

        update_pipeline(path, do_submit)
    finally:
        signal.signal(signal.SIGTERM, _orig)
        if _deferred:
            signal.raise_signal(signal.SIGTERM)

    print(f"{args.project}: spec revise response submitted")


def cmd_spec_self_review_submit(args):
    """SPEC_REVISE: セルフレビュー結果をファイルから投入"""
    path = get_path(args.project)

    review_path = Path(args.file)
    if not review_path.is_file():
        raise SystemExit(f"File not found: {args.file}")
    raw_text = review_path.read_text(encoding="utf-8")

    from spec_revise import parse_self_review_response

    # まず pipeline から expected_ids を読む（Euler P0-2: カスタムチェックリスト対応）
    expected_ids_from_pipeline = None
    try:
        import json as _json
        with open(path, encoding="utf-8") as _f:
            _pdata = _json.load(_f)
        sc_pre = _pdata.get("spec_config", {})
        expected_ids_from_pipeline = sc_pre.get("_self_review_expected_ids")
    except Exception:
        pass  # continue processing even if unreadable

    # パース検証（フェンス補完パターン）
    canonical_text = raw_text
    result = parse_self_review_response(raw_text, expected_ids=expected_ids_from_pipeline)
    if result["verdict"] == "parse_failed":
        # フェンスで包んでリトライ
        fenced = f"```yaml\n{raw_text}\n```"
        result2 = parse_self_review_response(fenced, expected_ids=expected_ids_from_pipeline)
        if result2["verdict"] != "parse_failed":
            canonical_text = fenced
    # parse_failed でも格納する（watchdog 側でリトライ処理するため）

    _deferred = False
    _orig = signal.getsignal(signal.SIGTERM)

    def _defer_sigterm(signum, frame):
        nonlocal _deferred
        _deferred = True

    signal.signal(signal.SIGTERM, _defer_sigterm)

    try:
        def do_submit(data):
            state = data.get("state", "IDLE")
            if state != "SPEC_REVISE":
                raise SystemExit(f"Not in SPEC_REVISE state: {state}")
            sc = data.get("spec_config", {})
            if not sc.get("_self_review_sent"):
                raise SystemExit("Self-review not requested (no _self_review_sent)")
            if sc.get("_self_review_response"):
                print(f"{args.project}: self-review response already submitted, skipping")
                return
            sc["_self_review_response"] = canonical_text
            data["spec_config"] = sc

        update_pipeline(path, do_submit)
    finally:
        signal.signal(signal.SIGTERM, _orig)
        if _deferred:
            signal.raise_signal(signal.SIGTERM)

    print(f"{args.project}: spec self-review response submitted")


def cmd_spec_issue_submit(args):
    """ISSUE_PLAN: implementerのIssue起票完了報告をファイルから投入"""
    path = get_path(args.project)

    review_path = Path(args.file)
    if not review_path.is_file():
        raise SystemExit(f"File not found: {args.file}")
    raw_text = review_path.read_text(encoding="utf-8")

    from spec_issue import parse_issue_plan_response

    canonical_text = raw_text
    parsed = parse_issue_plan_response(raw_text)
    if parsed is None:
        fenced = f"```yaml\n{raw_text}\n```"
        parsed = parse_issue_plan_response(fenced)
        if parsed is not None:
            canonical_text = fenced
    if parsed is None:
        raise SystemExit(
            f"Failed to parse issue plan response from {args.file}. "
            f"Expected YAML with status=done, created_issues=[int, ...]."
        )

    _deferred = False
    _orig = signal.getsignal(signal.SIGTERM)

    def _defer_sigterm(signum, frame):
        nonlocal _deferred
        _deferred = True

    signal.signal(signal.SIGTERM, _defer_sigterm)

    try:
        def do_submit(data):
            state = data.get("state", "IDLE")
            if state != "ISSUE_PLAN":
                raise SystemExit(f"Not in ISSUE_PLAN state: {state}")
            sc = data.get("spec_config", {})

            if sc.get("_issue_plan_response"):
                print(f"{args.project}: issue plan response already submitted, skipping")
                return

            sc["_issue_plan_response"] = canonical_text
            data["spec_config"] = sc

        update_pipeline(path, do_submit)
    finally:
        signal.signal(signal.SIGTERM, _orig)
        if _deferred:
            signal.raise_signal(signal.SIGTERM)

    print(f"{args.project}: spec issue plan response submitted")
    print(f"  created_issues: {parsed['created_issues']}")


def cmd_spec_queue_submit(args):
    """QUEUE_PLAN: implementerのキュー生成完了報告をファイルから投入"""
    path = get_path(args.project)

    review_path = Path(args.file)
    if not review_path.is_file():
        raise SystemExit(f"File not found: {args.file}")
    raw_text = review_path.read_text(encoding="utf-8")

    from spec_issue import parse_queue_plan_response

    canonical_text = raw_text
    parsed = parse_queue_plan_response(raw_text)
    if parsed is None:
        fenced = f"```yaml\n{raw_text}\n```"
        parsed = parse_queue_plan_response(fenced)
        if parsed is not None:
            canonical_text = fenced
    if parsed is None:
        raise SystemExit(
            f"Failed to parse queue plan response from {args.file}. "
            f"Expected YAML with status=done, batches=int(>=1), queue_file=str."
        )

    _deferred = False
    _orig = signal.getsignal(signal.SIGTERM)

    def _defer_sigterm(signum, frame):
        nonlocal _deferred
        _deferred = True

    signal.signal(signal.SIGTERM, _defer_sigterm)

    try:
        def do_submit(data):
            state = data.get("state", "IDLE")
            if state != "QUEUE_PLAN":
                raise SystemExit(f"Not in QUEUE_PLAN state: {state}")
            sc = data.get("spec_config", {})

            if sc.get("_queue_plan_response"):
                print(f"{args.project}: queue plan response already submitted, skipping")
                return

            sc["_queue_plan_response"] = canonical_text
            data["spec_config"] = sc

        update_pipeline(path, do_submit)
    finally:
        signal.signal(signal.SIGTERM, _orig)
        if _deferred:
            signal.raise_signal(signal.SIGTERM)

    print(f"{args.project}: spec queue plan response submitted")
    print(f"  batches: {parsed['batches']}")


def cmd_spec_suggestion_submit(args):
    """ISSUE_SUGGESTION: レビュアーのIssue分割提案をファイルから投入"""
    path = get_path(args.project)

    review_path = Path(args.file)
    if not review_path.is_file():
        raise SystemExit(f"File not found: {args.file}")
    raw_text = review_path.read_text(encoding="utf-8")

    from spec_issue import parse_issue_suggestion_response

    canonical_text = raw_text
    parsed = parse_issue_suggestion_response(raw_text)
    if parsed is None:
        fenced = f"```yaml\n{raw_text}\n```"
        parsed = parse_issue_suggestion_response(fenced)
        if parsed is not None:
            canonical_text = fenced
    if parsed is None:
        raise SystemExit(
            f"Failed to parse issue suggestion from {args.file}. "
            f"Expected YAML with phases=[{{name: str, issues: [{{title: str, ...}}]}}]."
        )

    _deferred = False
    _orig = signal.getsignal(signal.SIGTERM)

    def _defer_sigterm(signum, frame):
        nonlocal _deferred
        _deferred = True

    signal.signal(signal.SIGTERM, _defer_sigterm)

    try:
        def do_submit(data):
            state = data.get("state", "IDLE")
            if state != "ISSUE_SUGGESTION":
                raise SystemExit(f"Not in ISSUE_SUGGESTION state: {state}")
            sc = data.get("spec_config", {})
            rr = sc.get("review_requests", {})

            if args.reviewer not in rr:
                raise SystemExit(
                    f"Reviewer '{args.reviewer}' not in review_requests. "
                    f"Valid reviewers: {list(rr.keys())}"
                )

            if rr[args.reviewer].get("sent_at") is None:
                raise SystemExit(
                    f"Reviewer '{args.reviewer}' has not been sent a prompt yet (sent_at is None). "
                    f"Wait for watchdog to send the prompt first."
                )

            cr = sc.setdefault("current_reviews", {})
            entries = cr.setdefault("entries", {})
            if args.reviewer in entries and entries[args.reviewer].get("status") == "received":
                print(f"{args.reviewer}: already submitted, skipping")
                return

            entries[args.reviewer] = {
                "status": "received",
                "raw_text": canonical_text,
            }

            sc["current_reviews"] = cr
            data["spec_config"] = sc

        update_pipeline(path, do_submit)
    finally:
        signal.signal(signal.SIGTERM, _orig)
        if _deferred:
            signal.raise_signal(signal.SIGTERM)

    print(f"{args.project}: spec issue suggestion by {args.reviewer} submitted")
    print(f"  phases: {len(parsed['phases'])}")
    for phase in parsed["phases"]:
        print(f"    {phase['name']}: {len(phase['issues'])} issues")


def cmd_spec(args):
    """spec サブコマンドのディスパッチ"""
    spec_cmds = {
        "start": cmd_spec_start,
        "approve": cmd_spec_approve,
        "continue": cmd_spec_continue,
        "done": cmd_spec_done,
        "retry": cmd_spec_retry,
        "resume": cmd_spec_resume,
        "extend": cmd_spec_extend,
        "status": cmd_spec_status,
        "stop": cmd_spec_stop,
        "review-submit": cmd_spec_review_submit,
        "revise-submit": cmd_spec_revise_submit,
        "self-review-submit": cmd_spec_self_review_submit,
        "issue-submit": cmd_spec_issue_submit,
        "queue-submit": cmd_spec_queue_submit,
        "suggestion-submit": cmd_spec_suggestion_submit,
    }
    if not args.spec_command:
        raise SystemExit(
            "usage: gokrax spec {start|stop|approve|continue|done|retry|resume|extend|status"
            "|review-submit|revise-submit|self-review-submit|issue-submit|queue-submit|suggestion-submit}"
        )
    spec_cmds[args.spec_command](args)
