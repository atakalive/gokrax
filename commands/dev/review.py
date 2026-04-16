import subprocess
import sys
import time
from datetime import datetime, timedelta

from config import (
    GLAB_BIN,
    GLAB_TIMEOUT, REVIEWERS,
    STATE_PHASE_MAP,
    GOKRAX_CLI, OWNER_NAME, GITLAB_NAMESPACE, IMPLEMENTERS,
)
from pipeline_io import (
    load_pipeline, update_pipeline,
    now_iso, get_path, find_issue,
)
from notify import (
    send_to_agent_queued,
    post_gitlab_note as _post_gitlab_note,
    mask_agent_name, resolve_reviewer_arg, format_review_note_header,
)

from commands.dev.helpers import (
    VERDICT_SEVERITY, RISK_DISPLAY,
    parse_issue_args, _log, _masked_reviewer,
)


def _update_issue_title_with_assessment(gitlab: str, issue_num: int, complex_level: int, domain_risk: str = "n/a") -> bool:
    """Issue タイトルの末尾に [Lvl N / {Risk}] を付与。既に付いていれば置換。

    domain_risk の値に応じて No Risk / Low Risk / High Risk を表示。
    glab issue view で現在のタイトルを取得し、glab issue update で更新。
    リトライ3回（_post_gitlab_note と同方針）。
    """
    import re as _re

    # [Lvl N / No Risk] 等にマッチ（リスク部分はオプショナル）
    _TAG_RE = r'\[Lvl \d+(?:\s*/\s*(?:No|Low|High)\s+Risk)?\]'

    # 現在のタイトルを取得
    try:
        result = subprocess.run(
            [GLAB_BIN, "issue", "view", str(issue_num), "--output", "json", "-R", gitlab],
            capture_output=True, text=True, timeout=GLAB_TIMEOUT,
        )
        if result.returncode != 0:
            _log(f"glab issue view failed for #{issue_num}: {result.stderr.strip()}")
            return False
        import json as _json
        issue_data = _json.loads(result.stdout)
        current_title = issue_data.get("title", "")
    except Exception as e:
        _log(f"glab issue view error for #{issue_num}: {e}")
        return False

    # 既存タグは先頭・末尾どちらにあっても除去する（異常状態の防御的クリーンアップ）
    new_title = _re.sub(r'^\s*' + _TAG_RE + r'\s*', '', current_title)
    new_title = _re.sub(r'\s*' + _TAG_RE + r'\s*$', '', new_title)
    # 新規付与は常に末尾
    risk_label = RISK_DISPLAY.get(domain_risk, "")
    if risk_label:
        new_title = f"{new_title} [Lvl {complex_level} / {risk_label}]"
    else:
        new_title = f"{new_title} [Lvl {complex_level}]"

    # タイトル更新（リトライ3回）
    for attempt in range(3):
        try:
            result = subprocess.run(
                [GLAB_BIN, "issue", "update", str(issue_num), "--title", new_title, "-R", gitlab],
                capture_output=True, text=True, timeout=GLAB_TIMEOUT,
            )
            if result.returncode == 0:
                return True
            _log(f"glab issue update failed (attempt {attempt+1}/3) for #{issue_num}: {result.stderr.strip()}")
        except Exception as e:
            _log(f"glab issue update error (attempt {attempt+1}/3) for #{issue_num}: {e}")
        if attempt < 2:
            time.sleep(3)
    return False


def cmd_review(args):
    """レビュー結果を記録（pipeline JSON + GitLab Issue note）"""
    import signal

    path = get_path(args.project)
    _pipeline: dict = load_pipeline(path)
    args.reviewer = resolve_reviewer_arg(
        args.reviewer, _pipeline.get("reviewer_number_map")
    )
    if args.reviewer not in REVIEWERS:
        raise SystemExit(f"Unknown reviewer: {args.reviewer}")
    _skipped = False
    _dispute_accepted = False

    def do_review(data):
        nonlocal _skipped, _dispute_accepted
        state = data.get("state", "IDLE")
        if state in ("DESIGN_REVIEW", "DESIGN_REVIEW_NPASS"):
            key = "design_reviews"
        elif state in ("CODE_REVIEW", "CODE_REVIEW_NPASS"):
            key = "code_reviews"
        elif state in ("DESIGN_REVISE", "CODE_REVISE"):
            key = "design_reviews" if "DESIGN" in state else "code_reviews"
            phase = "design" if "DESIGN" in state else "code"
            # REVISE 中は dispute pending のレビュアーからのみ受け付ける
            issue = find_issue(data.get("batch", []), args.issue)
            if not issue:
                raise SystemExit(f"Issue #{args.issue} not in batch")
            # --force 必須（既存レビューの上書きが必要）
            if not args.force:
                raise SystemExit(
                    "--force is required for dispute reviews during REVISE"
                )
            pending_dispute = None
            for d in issue.get("disputes", []):
                if (d.get("reviewer") == args.reviewer
                        and d.get("status") == "pending"
                        and d.get("phase") == phase):
                    pending_dispute = d
                    break
            if pending_dispute is None:
                raise SystemExit(
                    f"Review updates during REVISE are only allowed when a dispute is pending "
                    f"(#{args.issue}, {args.reviewer})"
                )
            # dispute 解決: severity 比較で accepted/rejected を判定
            new_sev = VERDICT_SEVERITY.get(args.verdict.upper(), 0)
            filed_sev = VERDICT_SEVERITY.get(pending_dispute.get("filed_verdict", "P0"), 3)
            resolved = "accepted" if new_sev < filed_sev else "rejected"
            pending_dispute["status"] = resolved
            pending_dispute["resolved_at"] = now_iso()
            pending_dispute["resolved_verdict"] = args.verdict.upper()
            if args.summary:
                pending_dispute["resolved_summary"] = args.summary
            if resolved == "accepted":
                _dispute_accepted = True
        else:
            # 非レビュー状態（IMPLEMENTATION 等）で届いたレビューは静かに破棄する。
            # エラーにするとレビュアーが transition --force で状態を巻き戻す事故が起きる (#135, #136)。
            _skipped = True
            print(f"#{args.issue}: review by {_masked_reviewer(args.reviewer, _pipeline.get('reviewer_number_map'))} silently discarded (state={state})")
            return
        # phase 検証: REVIEW 状態では --phase 必須。
        # 旧コマンド（--phase なし）の遅延応答、または前フェーズの遅延応答を拒否する。
        # REVISE 状態は dispute 処理（--force 付き）で --phase を持たないためスキップ。
        if state in ("DESIGN_REVIEW", "DESIGN_REVIEW_NPASS",
                      "CODE_REVIEW", "CODE_REVIEW_NPASS"):
            _phase_arg = getattr(args, "phase", None)
            current_phase = "design" if state.startswith("DESIGN") else "code"
            if _phase_arg is None:
                # --phase 省略: 旧コマンドの遅延応答と見なし破棄
                _skipped = True
                print(f"#{args.issue}: review by {_masked_reviewer(args.reviewer, _pipeline.get('reviewer_number_map'))} silently discarded "
                      f"(--phase not specified, current phase is {current_phase})")
                return
            if _phase_arg != current_phase:
                _skipped = True
                print(f"#{args.issue}: review by {_masked_reviewer(args.reviewer, _pipeline.get('reviewer_number_map'))} silently discarded "
                      f"(phase mismatch: --phase {_phase_arg}, current {current_phase})")
                return
        # ラウンド番号検証: stale なレビュー（前サイクルの Remind 応答等）を拒否する。
        # DESIGN_REVISE/CODE_REVISE 状態では dispute レビュー（--force 必須）のみ
        # ここに到達する。dispute 経由の場合、notify_dispute が --round を付与しない
        # ため _round_arg=None となり、検証はスキップされる。
        from pipeline_io import get_current_round
        _round_arg = getattr(args, "round", None)
        if _round_arg is not None:
            current_round = get_current_round(data)
            if current_round > 0 and _round_arg != current_round:
                raise SystemExit(
                    f"Round mismatch: current round is {current_round}, "
                    f"but --round {_round_arg} was specified. "
                    f"This review may be stale (from a previous cycle)."
                )
        # REVIEW 状態での dispute 自動解決 + 冪等性バイパス判定
        has_pending_dispute = False
        if state in ("DESIGN_REVIEW", "CODE_REVIEW"):
            phase = "design" if "DESIGN" in state else "code"
            _issue_for_dispute = find_issue(data.get("batch", []), args.issue)
            if _issue_for_dispute:
                has_pending_dispute = any(
                    d.get("reviewer") == args.reviewer
                    and d.get("status") == "pending"
                    and d.get("phase") == phase
                    for d in _issue_for_dispute.get("disputes", [])
                )
                for d in _issue_for_dispute.get("disputes", []):
                    if (d.get("reviewer") == args.reviewer
                            and d.get("status") == "pending"
                            and d.get("phase") == phase):
                        _new_sev = VERDICT_SEVERITY.get(args.verdict.upper(), 0)
                        _filed_sev = VERDICT_SEVERITY.get(d.get("filed_verdict", "P0"), 3)
                        d["status"] = "accepted" if _new_sev < _filed_sev else "rejected"
                        d["resolved_at"] = now_iso()
                        d["resolved_verdict"] = args.verdict.upper()
                        if args.summary:
                            d["resolved_summary"] = args.summary
                        break
        issue = find_issue(data.get("batch", []), args.issue)
        if not issue:
            raise SystemExit(f"Issue #{args.issue} not in batch")
        # 冪等性: 同じレビュアーが既にレビュー済みならスキップ（--force で上書き可）
        if args.reviewer in issue.get(key, {}) and not has_pending_dispute:
            existing = issue[key][args.reviewer]
            # NPASS: pass < target_pass なら上書きを許可（次のパスのレビュー）
            npass_overwrite = existing.get("pass", 1) < existing.get("target_pass", 1)
            if not args.force and not npass_overwrite:
                print(f"#{args.issue}: already reviewed by {_masked_reviewer(args.reviewer, _pipeline.get('reviewer_number_map'))}, skipping")
                _skipped = True
                return
            if npass_overwrite:
                print(f"#{args.issue}: NPASS overwrite (pass {existing.get('pass', 1)}/{existing.get('target_pass', 1)})")
            elif args.force:
                print(f"#{args.issue}: overwriting existing review by {_masked_reviewer(args.reviewer, _pipeline.get('reviewer_number_map'))} (--force)")
        # dispute accepted 時はレビューを削除して早期 return（次の REVIEW サイクルで再レビューを強制）
        if _dispute_accepted:
            issue[key].pop(args.reviewer, None)
            return
        review_entry = {"verdict": args.verdict, "at": now_iso()}
        if args.summary:
            review_entry["summary"] = args.summary

        # pass / target_pass の計算
        from engine.fsm import get_phase_config as _get_phase_config
        phase = "design" if "DESIGN" in state else "code"
        _phase_config = _get_phase_config(data, phase)
        n_pass_config = _phase_config.get("n_pass", {})
        target_pass = n_pass_config.get(args.reviewer, 1)
        if not isinstance(target_pass, int) or target_pass < 1:
            print(f"WARNING: n_pass[{_masked_reviewer(args.reviewer, _pipeline.get('reviewer_number_map'))}] = {target_pass!r} is invalid, defaulting to 1")
            target_pass = 1

        existing_entry = issue.get(key, {}).get(args.reviewer, {})
        current_pass = existing_entry.get("pass", 0) + 1  # no existing->0+1=1, existing->pass+1

        review_entry["pass"] = current_pass
        review_entry["target_pass"] = target_pass

        issue[key][args.reviewer] = review_entry

    # SIGTERM を遅延させ、JSON 書き込み（update_pipeline）の完了を保証する
    _deferred = False
    _orig = signal.getsignal(signal.SIGTERM)

    def _defer_sigterm(signum, frame):
        nonlocal _deferred
        _deferred = True

    signal.signal(signal.SIGTERM, _defer_sigterm)
    try:
        data = update_pipeline(path, do_review)
    finally:
        signal.signal(signal.SIGTERM, _orig)
        if _deferred:
            signal.raise_signal(signal.SIGTERM)

    if _skipped:
        return

    state = data.get("state", "IDLE")
    print(f"{args.project}: #{args.issue} review by {_masked_reviewer(args.reviewer, data.get('reviewer_number_map'))} = {args.verdict}")

    # メトリクス記録（Issue #81）
    from pipeline_io import append_metric
    from datetime import timezone
    phase = "code" if "CODE" in state else "design"
    cycle_key = "design_revise_count" if "DESIGN" in state else "code_revise_count"
    revise_cycle = data.get(cycle_key, 0)
    latency_sec = None
    for entry in reversed(data.get("history", [])):
        if entry.get("to") == state:
            try:
                _JST = timezone(timedelta(hours=9))
                entered_at = datetime.fromisoformat(entry["at"])
                if entered_at.tzinfo is None:
                    entered_at = entered_at.replace(tzinfo=_JST)
                now = datetime.now(_JST)
                latency_sec = round((now - entered_at).total_seconds())
                if latency_sec < 0:
                    latency_sec = None
            except (KeyError, ValueError, TypeError):
                pass
            break
    append_metric("review_response", pj=args.project, issue=args.issue,
                  phase=phase, reviewer=args.reviewer, verdict=args.verdict,
                  latency_sec=latency_sec, revise_cycle=revise_cycle)

    # GitLab Issue note に自動投稿
    gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
    phase = "design" if "DESIGN" in state else "code"

    # NPASS 中間パスでは APPROVE の場合のみ GitLab note をスキップ
    # P0/P1/P2 の指摘は中間パスでも GitLab に投稿（開発者が確認できるようにする）
    issue = find_issue(data.get("batch", []), args.issue)
    skip_note = False
    entry: dict = {}
    if issue:
        review_key = "code_reviews" if "CODE" in state else "design_reviews"
        entry = issue.get(review_key, {}).get(args.reviewer, {})
        if (entry.get("pass", 1) < entry.get("target_pass", 1)
                and args.verdict.upper() == "APPROVE"):
            print(f"  → GitLab note skipped (APPROVE at pass {entry['pass']}/{entry['target_pass']})")
            skip_note = True

    if not skip_note:
        reviewer_map = data.get("reviewer_number_map")
        masked = mask_agent_name(args.reviewer, reviewer_number_map=reviewer_map)
        from pipeline_io import get_current_round
        round_num = get_current_round(data)
        target_pass = entry.get("target_pass", 1)
        header = format_review_note_header(masked, args.verdict, phase, round_num, target_pass)
        note_body = f"{header}\n\n{args.summary or ''}"
        if _post_gitlab_note(gitlab, args.issue, note_body):
            print("  → GitLab issue note posted")


def cmd_dispute(args):
    """REVISE中のP0/P1判定に対して異議を申し立てる（dispute）"""
    import signal

    path = get_path(args.project)
    _pipeline: dict = load_pipeline(path)
    args.reviewer = resolve_reviewer_arg(
        args.reviewer, _pipeline.get("reviewer_number_map")
    )

    def do_dispute(data):
        state = data.get("state", "IDLE")
        if state not in ("DESIGN_REVISE", "CODE_REVISE"):
            raise SystemExit(f"dispute is only allowed in REVISE state (current: {state})")

        issue = find_issue(data.get("batch", []), args.issue)
        if not issue:
            raise SystemExit(f"Issue #{args.issue} not in batch")

        if args.reviewer not in REVIEWERS:
            raise SystemExit(f"Unknown reviewer: {args.reviewer}")

        review_key = "design_reviews" if "DESIGN" in state else "code_reviews"
        reviewer_review = issue.get(review_key, {}).get(args.reviewer, {})
        verdict = reviewer_review.get("verdict", "").upper()
        if verdict not in ("P0", "P1"):
            raise SystemExit(
                f"#{args.issue}: {_masked_reviewer(args.reviewer, _pipeline.get('reviewer_number_map'))}'s verdict is {verdict or '(none)'} — "
                f"only P0/P1 can be disputed"
            )

        disputes = issue.setdefault("disputes", [])
        has_pending = any(
            d.get("reviewer") == args.reviewer and d.get("status") == "pending"
            for d in disputes
        )
        if has_pending:
            raise SystemExit(
                f"#{args.issue}: {_masked_reviewer(args.reviewer, _pipeline.get('reviewer_number_map'))} already has a pending dispute"
            )

        if not args.reason.strip():
            raise SystemExit("--reason cannot be empty")

        phase = "design" if "DESIGN" in state else "code"
        disputes.append({
            "reviewer": args.reviewer,
            "reason": args.reason.strip(),
            "status": "pending",
            "filed_at": now_iso(),
            "filed_verdict": verdict,
            "phase": phase,
        })

    _deferred = False
    _orig = signal.getsignal(signal.SIGTERM)

    def _defer_sigterm(signum, frame):
        nonlocal _deferred
        _deferred = True

    signal.signal(signal.SIGTERM, _defer_sigterm)
    try:
        data = update_pipeline(path, do_dispute)
    finally:
        signal.signal(signal.SIGTERM, _orig)
        if _deferred:
            signal.raise_signal(signal.SIGTERM)

    print(f"{args.project}: #{args.issue} dispute filed against {_masked_reviewer(args.reviewer, data.get('reviewer_number_map'))}")

    # dispute 即時通知（best-effort）
    state = data.get("state", "IDLE")
    phase = "design" if "DESIGN" in state else "code"
    review_key = "design_reviews" if "DESIGN" in state else "code_reviews"
    issue_data = find_issue(data.get("batch", []), args.issue)
    filed_verdict = ""
    if issue_data:
        filed_verdict = issue_data.get(review_key, {}).get(args.reviewer, {}).get("verdict", "")
    dispute_msg = (
        f"[Dispute — objection to your {filed_verdict} verdict]\n"
        f"{args.project} #{args.issue}: The implementer has filed a dispute against your verdict.\n\n"
        f"Reason:\n{args.reason.strip()}\n\n"
        f"Please re-evaluate and report your verdict with --force:\n"
        f"python3 {GOKRAX_CLI} review --pj {args.project} --issue {args.issue} "
        f"--reviewer {args.reviewer} --verdict <APPROVE/P0/P1/P2> --summary \"...\" --force"
    )
    if not send_to_agent_queued(args.reviewer, dispute_msg):
        print(f"WARNING: Failed to send dispute notification ({_masked_reviewer(args.reviewer, data.get('reviewer_number_map'))})")

    gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
    reviewer_map = data.get("reviewer_number_map")
    masked = mask_agent_name(args.reviewer, reviewer_number_map=reviewer_map)
    note_body = (
        f"[dispute] #{args.issue}: {masked}'s verdict disputed\n\n"
        f"**Reason:**\n\n{args.reason.strip()}"
    )
    _post_gitlab_note(gitlab, args.issue, note_body)


def cmd_flag(args):
    """人間（M）による P0/P1 差し込み（任意タイミング）"""
    import signal

    path = get_path(args.project)

    def do_flag(data):
        state = data.get("state", "IDLE")

        # Validate: issue must be in batch
        issue = find_issue(data.get("batch", []), args.issue)
        if not issue:
            raise SystemExit(
                f"Issue #{args.issue} not in batch (state={state}). "
                f"Flags can only be posted when the issue is in an active batch."
            )

        # Determine phase from current state
        phase = STATE_PHASE_MAP.get(state)
        if phase is None:
            raise SystemExit(
                f"Cannot flag in unknown state: {state}. "
                f"Valid states: {', '.join(STATE_PHASE_MAP.keys())}"
            )

        # Record flag
        flag_entry = {
            "verdict": args.verdict,
            "summary": args.summary or "",
            "at": now_iso(),
            "by": OWNER_NAME,
            "phase": phase,
        }
        issue.setdefault("flags", []).append(flag_entry)

    # SIGTERM deferral (same pattern as cmd_review)
    _deferred = False
    _orig = signal.getsignal(signal.SIGTERM)

    def _defer_sigterm(signum, frame):
        nonlocal _deferred
        _deferred = True

    signal.signal(signal.SIGTERM, _defer_sigterm)
    try:
        data = update_pipeline(path, do_flag)
    finally:
        signal.signal(signal.SIGTERM, _orig)
        if _deferred:
            signal.raise_signal(signal.SIGTERM)

    state = data.get("state", "IDLE")
    print(f"{args.project}: #{args.issue} flag by M = {args.verdict}")

    # Post to GitLab issue note
    gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
    note_body = f"[M] FLAG {args.verdict}\n\n{args.summary or ''}"
    if _post_gitlab_note(gitlab, args.issue, note_body):
        print("  → GitLab issue note posted")


def cmd_commit(args):
    """commit hash を記録"""
    args.issue = parse_issue_args(args.issue)
    path = get_path(args.project)

    def do_commit(data):
        batch = data.get("batch", [])
        for num in args.issue:
            issue = find_issue(batch, num)
            if not issue:
                raise SystemExit(f"Issue #{num} not in batch")
            issue["commit"] = args.hash
            if args.session_id:
                issue["cc_session_id"] = args.session_id

    update_pipeline(path, do_commit)
    done = ", ".join(f"#{n}" for n in args.issue)
    print(f"{args.project}: commit={args.hash} ({done})")


def cmd_cc_start(args):
    """CC実行開始時にPIDを記録"""
    path = get_path(args.project)

    def do_cc_start(data):
        data["cc_pid"] = args.pid

    update_pipeline(path, do_cc_start)
    print(f"{args.project}: cc_pid={args.pid} recorded")


def cmd_plan_done(args):
    """設計完了フラグを設定"""
    args.issue = parse_issue_args(args.issue)
    path = get_path(args.project)

    def do_plan_done(data):
        state = data.get("state", "IDLE")
        if state != "DESIGN_PLAN":
            raise SystemExit(f"Not in DESIGN_PLAN state: {state}")
        batch = data.get("batch", [])
        for num in args.issue:
            issue = find_issue(batch, num)
            if not issue:
                raise SystemExit(f"Issue #{num} not in batch")
            issue["design_ready"] = True

    update_pipeline(path, do_plan_done)
    done = ", ".join(f"#{n}" for n in args.issue)
    print(f"{args.project}: design plan done ({done})")


def cmd_assess_done(args):
    """ASSESSMENT: Issue 単位で難易度記録 + Issue タイトル更新"""
    summary = args.summary[:500] if args.summary else ""
    risk_reason = args.risk_reason.strip() if args.risk not in ("none", "n/a") else ""
    if args.risk not in ("none", "n/a") and not risk_reason:
        raise SystemExit("--risk-reason is required when --risk is low or high")

    path = get_path(args.project)

    def do_assess(data):
        state = data.get("state", "IDLE")
        if state != "ASSESSMENT":
            raise SystemExit(f"Not in ASSESSMENT state: {state}")
        batch = data.get("batch", [])
        issue_entry = find_issue(batch, args.issue)
        if not issue_entry:
            raise SystemExit(f"Issue #{args.issue} not in batch")
        issue_entry["assessment"] = {
            "complex_level": args.complex_level,
            "domain_risk": args.risk,
            "risk_reason": risk_reason,
            "summary": summary,
            "assessed_by": data.get("implementer", IMPLEMENTERS[0]),
            "timestamp": now_iso(),
        }

    update_pipeline(path, do_assess)

    # Issue タイトル更新（失敗は warning、遷移はブロックしない）
    data = load_pipeline(path)
    gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
    if not _update_issue_title_with_assessment(gitlab, args.issue, args.complex_level, args.risk):
        print(f"  ⚠ title update failed for #{args.issue} (warning only)", file=sys.stderr)

    risk_label = RISK_DISPLAY.get(args.risk, "")
    if risk_label:
        print(f"{args.project}: assessment done for #{args.issue} (Lvl {args.complex_level} / {risk_label})")
    else:
        print(f"{args.project}: assessment done for #{args.issue} (Lvl {args.complex_level})")


def cmd_design_revise(args):
    """DESIGN_REVISE: design_revised フラグを設定"""
    args.issue = parse_issue_args(args.issue)
    path = get_path(args.project)

    if args.summary:
        data = load_pipeline(path)
        gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
        note_body = f"[gokrax] Revise summary (design)\n\n{args.summary}"
        for num in args.issue:
            if not _post_gitlab_note(gitlab, num, note_body):
                sys.exit(1)

    def do_design_revise(data):
        if data.get("state") != "DESIGN_REVISE":
            raise SystemExit(f"Not in DESIGN_REVISE state: {data.get('state')}")
        batch = data.get("batch", [])
        for num in args.issue:
            issue = find_issue(batch, num)
            if not issue:
                raise SystemExit(f"Issue #{num} not in batch")
            issue["design_revised"] = True

    update_pipeline(path, do_design_revise)
    done = ", ".join(f"#{n}" for n in args.issue)
    print(f"{args.project}: {done} design-revised")


def cmd_code_revise(args):
    """CODE_REVISE: commit 記録 + code_revised フラグを一発で設定"""
    args.issue = parse_issue_args(args.issue)
    path = get_path(args.project)

    if args.summary:
        data = load_pipeline(path)
        gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
        note_body = f"[gokrax] Revise summary (code)\n\n{args.summary}"
        for num in args.issue:
            if not _post_gitlab_note(gitlab, num, note_body):
                sys.exit(1)

    def do_code_revise(data):
        if data.get("state") != "CODE_REVISE":
            raise SystemExit(f"Not in CODE_REVISE state: {data.get('state')}")
        batch = data.get("batch", [])
        for num in args.issue:
            issue = find_issue(batch, num)
            if not issue:
                raise SystemExit(f"Issue #{num} not in batch")
            issue["commit"] = args.hash
            issue["code_revised"] = True

    update_pipeline(path, do_code_revise)
    done = ", ".join(f"#{n}" for n in args.issue)
    print(f"{args.project}: {done} code-revised (commit={args.hash})")
