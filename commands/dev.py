import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    PIPELINES_DIR, GLAB_BIN, LOG_FILE,
    VALID_STATES, VALID_TRANSITIONS, MAX_BATCH,
    VALID_VERDICTS, GLAB_TIMEOUT, ALLOWED_REVIEWERS, REVIEW_MODES, LOCAL_TZ,
    WATCHDOG_LOOP_PIDFILE, WATCHDOG_LOOP_LOCKFILE,
    VALID_FLAG_VERDICTS, STATE_PHASE_MAP,
    GOKRAX_CLI, OWNER_NAME, GITLAB_NAMESPACE,
)
from pipeline_io import (
    load_pipeline, save_pipeline, update_pipeline,
    add_history, now_iso, get_path, find_issue,
    clear_pending_notification,
)
from engine.fsm import get_notification_for_state
from notify import notify_implementer, notify_reviewers, notify_discord, send_to_agent, send_to_agent_queued, post_gitlab_note as _post_gitlab_note, mask_agent_name
import os

# Verdict severity for dispute resolution (Issue #86)
VERDICT_SEVERITY = {"REJECT": 3, "P0": 3, "P1": 2, "P2": 1, "APPROVE": 0}


def get_status_text(enabled_only: bool = False) -> str:
    """全PJの状態を文字列として取得。

    Args:
        enabled_only: True の場合、enabled=True のプロジェクトのみ含める

    Returns:
        Status text string. "No active pipelines." if no matching pipelines.
    """
    import io
    output = io.StringIO()

    PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(PIPELINES_DIR.glob("*.json"))

    # Filter by enabled if requested
    matching_files = []
    for path in files:
        data = load_pipeline(path)
        if not enabled_only or data.get("enabled", False):
            matching_files.append((path, data))

    if not matching_files:
        return "No active pipelines."

    for path, data in matching_files:
        pj = data.get("project", path.stem)
        state = data.get("state", "IDLE")
        enabled = "ON" if data.get("enabled") else "OFF"
        batch = data.get("batch", [])
        review_mode = data.get("review_mode", "standard")
        issues = ", ".join(f"#{i['issue']}" for i in batch) if batch else "none"
        mode_config = REVIEW_MODES.get(review_mode, REVIEW_MODES["standard"])
        reviewers_str = ", ".join(f'"{r}"' for r in mode_config["members"])
        output.write(f"[{enabled}] {pj}: {state}  issues=[{issues}]  ReviewerSize={review_mode}  Reviewers=[{reviewers_str}]\n")

        # Show per-issue review progress
        if state in ("DESIGN_REVIEW", "CODE_REVIEW") and batch:
            review_key = "design_reviews" if state == "DESIGN_REVIEW" else "code_reviews"
            min_rev = mode_config["min_reviews"]
            for item in batch:
                reviews = item.get(review_key, {})
                done = len(reviews)
                verdicts = {}
                for rev in reviews.values():
                    v = rev.get("verdict", "?")
                    verdicts[v] = verdicts.get(v, 0) + 1
                verdict_parts = ", ".join(f"{c} {v}" for v, c in sorted(verdicts.items()))
                verdict_str = f" ({verdict_parts})" if verdict_parts else ""
                output.write(f"  #{item['issue']}: {done}/{min_rev} reviews{verdict_str}\n")

    return output.getvalue().rstrip()


def cmd_status(args):
    """全PJの状態を表示"""
    print(get_status_text(enabled_only=False))


def cmd_init(args):
    """新PJのpipeline JSONを初期化"""
    PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
    path = get_path(args.project)
    if path.exists():
        print(f"Already exists: {path}", file=sys.stderr)
        sys.exit(1)

    data = {
        "project": args.project,
        "gitlab": args.gitlab or f"{GITLAB_NAMESPACE}/{args.project}",
        "repo_path": args.repo_path or "",
        "state": "IDLE",
        "enabled": False,
        "implementer": args.implementer or "kaneko",
        "batch": [],
        "history": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    save_pipeline(path, data)
    print(f"Created: {path}")


def cmd_enable(args):
    from gokrax import _start_loop

    path = get_path(args.project)

    def do_enable(data):
        data["enabled"] = True

    update_pipeline(path, do_enable)
    _start_loop()
    print(f"{args.project}: watchdog enabled")


def cmd_disable(args):
    from gokrax import _any_pj_enabled, _stop_loop

    path = get_path(args.project)

    def do_disable(data):
        data["enabled"] = False

    update_pipeline(path, do_disable)
    if not _any_pj_enabled():
        _stop_loop()
        for f in [WATCHDOG_LOOP_PIDFILE, WATCHDOG_LOOP_LOCKFILE]:
            f.unlink(missing_ok=True)
        print("All projects disabled — watchdog loop stopped (crontab kept for auto-restart).")
    else:
        print(f"{args.project}: watchdog disabled")


def cmd_extend(args):
    """タイムアウト延長申請。

    対象状態: DESIGN_PLAN, DESIGN_REVISE, IMPLEMENTATION, CODE_REVISE
    """
    from config import EXTENDABLE_STATES

    path = get_path(args.project)

    MAX_EXTENDS = 2

    result = {}
    def do_extend(data):
        state = data.get("state", "IDLE")
        if state not in EXTENDABLE_STATES:
            raise SystemExit(
                f"延長不可: 現在の状態 {state} は対象外です "
                f"(対象: {', '.join(sorted(EXTENDABLE_STATES))})"
            )
        extend_count = data.get("extend_count", 0)
        if extend_count >= MAX_EXTENDS:
            raise SystemExit(
                f"延長不可: 延長回数上限({MAX_EXTENDS}回)に達しています"
            )
        data["timeout_extension"] = data.get("timeout_extension", 0) + args.by
        data["extend_count"] = extend_count + 1
        result["state"] = state
        result["implementer"] = data.get("implementer", "kaneko")
        result["total"] = data["timeout_extension"]
        result["count"] = data["extend_count"]

    update_pipeline(path, do_extend)

    from datetime import datetime
    ts = datetime.now(LOCAL_TZ).strftime("%m/%d %H:%M")
    notify_discord(
        f"[{args.project}] {result['implementer']} がタイムアウトを{args.by}秒延長 "
        f"({result['state']}, {result['count']}/{MAX_EXTENDS}回, 累計+{result['total']}秒, {ts})"
    )

    print(f"{args.project}: タイムアウト延長 +{args.by}秒 (累計+{result['total']}秒)")


def _fetch_open_issues(gitlab: str) -> list[tuple[int, str]]:
    """glab issue list でopen issueの (番号, タイトル) リストを取得。"""
    try:
        result = subprocess.run(
            [GLAB_BIN, "issue", "list", "-R", gitlab,
             "-O", "json", "-P", "100"],
            capture_output=True, text=True, timeout=GLAB_TIMEOUT,
        )
        if result.returncode != 0:
            print(f"glab issue list failed: {result.stderr.strip()}", file=sys.stderr)
            return []

        import json
        issues = json.loads(result.stdout)
        return [
            (issue["iid"], issue.get("title", ""))
            for issue in issues if issue.get("state") == "opened"
        ]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
        print(f"Failed to fetch open issues: {e}", file=sys.stderr)
        return []


def _fetch_issue_title(issue_num: int, gitlab: str) -> str:
    """GitLab APIでIssueタイトルを取得。失敗時は空文字列。"""
    try:
        result = subprocess.run(
            [GLAB_BIN, "issue", "show", str(issue_num), "--output", "json", "-R", gitlab],
            capture_output=True, text=True, timeout=GLAB_TIMEOUT, check=False,
        )
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            return data.get("title", "")
    except Exception:
        pass
    return ""


def cmd_triage(args):
    """Issueをバッチに投入（複数指定可）"""
    path = get_path(args.project)
    data = load_pipeline(get_path(args.project))
    gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
    titles = list(args.title) + [""] * (len(args.issue) - len(args.title))
    # タイトルが空のIssueはGitLab APIで取得
    for idx, (num, title) in enumerate(zip(args.issue, titles)):
        if not title:
            titles[idx] = _fetch_issue_title(num, gitlab)

    def do_triage(data):
        state = data.get("state", "IDLE")
        if state != "IDLE":
            raise SystemExit(f"Cannot add issues in state {state} (allowed: IDLE)")
        batch = data.get("batch", [])
        if len(batch) + len(args.issue) > MAX_BATCH:
            raise SystemExit(
                f"Batch overflow: {len(batch)} existing + {len(args.issue)} new > {MAX_BATCH}"
            )

        # Clear reviewer metadata if starting a new batch
        if len(batch) == 0:
            data.pop("excluded_reviewers", None)
            data.pop("min_reviews_override", None)
            data.pop("design_min_reviews_met_at", None)
            data.pop("code_min_reviews_met_at", None)

        for num, title in zip(args.issue, titles):
            if find_issue(batch, num):
                raise SystemExit(f"Issue #{num} already in batch")
            batch.append({
                "issue": num,
                "title": title,
                "commit": None,
                "cc_session_id": None,
                "design_reviews": {},
                "code_reviews": {},
                "added_at": now_iso(),
            })
        data["batch"] = batch

    update_pipeline(path, do_triage)
    nums = ", ".join(f"#{n}" for n in args.issue)
    print(f"{args.project}: {nums} added to batch")


def cmd_start(args):
    """gokrax start --project X [--issue N [N...]]

    triage + DESIGN_PLAN遷移 + watchdog有効化を一括実行。
    --issue省略時はGitLab APIでopen issue全件取得。
    """
    from gokrax import _start_loop
    from config import DEFAULT_QUEUE_OPTIONS, NONE_TO_FALSE_KEYS

    # 明示的な否定フラグを先に処理
    if getattr(args, "keep_ctx_none", None):
        args.keep_ctx_batch = False
        args.keep_ctx_intra = False
    if getattr(args, "no_skip_cc_plan", None):
        args.skip_cc_plan = False
    if getattr(args, "no_skip_test", None):
        args.skip_test = False
    if getattr(args, "no_skip_assess", None):
        args.skip_assess = False
    if getattr(args, "no_exclude_high_risk", None):
        args.exclude_high_risk = False
    if getattr(args, "no_exclude_any_risk", None):
        args.exclude_any_risk = False

    # デフォルトオプション適用: CLI 引数で明示指定されていない（None のまま）オプションにデフォルト値を注入
    from task_queue import _QUEUE_OPT_ALIASES
    for key, default_val in DEFAULT_QUEUE_OPTIONS.items():
        if "=" in key:
            # パターン A: "impl=opus": True → lhs="impl", rhs="opus"
            if not default_val:
                continue
            lhs, rhs = key.split("=", 1)
            if not rhs:
                continue
            internal_key = _QUEUE_OPT_ALIASES.get(lhs)
            if internal_key and getattr(args, internal_key, None) is None:
                setattr(args, internal_key, rhs)
        else:
            internal_key = _QUEUE_OPT_ALIASES.get(key, key)
            if getattr(args, internal_key, None) is None:
                setattr(args, internal_key, default_val)

    # None のまま残っているオプションを False に正規化（後続コードが bool を期待するため）
    for key in NONE_TO_FALSE_KEYS:
        if getattr(args, key, None) is None:
            setattr(args, key, False)

    path = get_path(args.project)

    # 1. 前提条件チェック: IDLE状態でなければエラー
    data = load_pipeline(path)
    if data.get("state", "IDLE") != "IDLE":
        raise SystemExit(
            f"Cannot start: current state is {data['state']} (expected IDLE)"
        )

    # 前回失敗時の残留フラグをクリア（do_mode で再設定する前に）
    def _clear_stale_skip(d):
        d.pop("skip_cc_plan", None)
        d.pop("skip_test", None)
        d.pop("skip_assess", None)
        d.pop("exclude_high_risk", None)
        d.pop("exclude_any_risk", None)
    update_pipeline(path, _clear_stale_skip)

    # 2. Issue番号取得（--issue指定 or GitLab API）
    if args.issue:
        issue_nums = args.issue
        titles = []
    else:
        # GitLab APIでopen issue全件取得（タイトル付き）
        gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
        results = _fetch_open_issues(gitlab)
        if not results:
            raise SystemExit(f"No open issues found in {gitlab}")
        issue_nums = [r[0] for r in results]
        titles = [r[1] for r in results]

    # 3. triage実行（既存のcmd_triageロジック流用）
    import argparse
    triage_args = argparse.Namespace(
        project=args.project,
        issue=issue_nums,
        title=titles,
    )
    cmd_triage(triage_args)

    # 4. keep-ctx フラグ正規化 (keep-context / keep-ctx-all → 両方True)
    if getattr(args, "keep_context", False) or getattr(args, "keep_ctx_all", False):
        args.keep_ctx_batch = True
        args.keep_ctx_intra = True

    # 5. review_mode / keep_ctx / p2_fix / comment 設定（遷移前に設定して/newの宛先に反映させる）
    has_keep_ctx = getattr(args, "keep_ctx_batch", False) or getattr(args, "keep_ctx_intra", False)
    has_p2_fix = getattr(args, "p2_fix", False)
    has_comment = bool(getattr(args, "comment", None))
    has_skip_cc_plan = getattr(args, "skip_cc_plan", False)
    has_skip_test = getattr(args, "skip_test", False)
    has_skip_assess = getattr(args, "skip_assess", False)
    has_exclude_high_risk = getattr(args, "exclude_high_risk", False)
    has_exclude_any_risk = getattr(args, "exclude_any_risk", False)
    has_cc_plan_model = bool(getattr(args, "cc_plan_model", None))
    has_cc_impl_model = bool(getattr(args, "cc_impl_model", None))
    if getattr(args, "mode", None) or has_keep_ctx or has_p2_fix or has_comment or has_skip_cc_plan or has_skip_test or has_skip_assess or has_exclude_high_risk or has_exclude_any_risk or has_cc_plan_model or has_cc_impl_model:
        from config import REVIEW_MODES
        if getattr(args, "mode", None) and args.mode not in REVIEW_MODES:
            raise SystemExit(f"Invalid mode: {args.mode} (valid: {list(REVIEW_MODES)})")
        def do_mode(data):
            if getattr(args, "mode", None):
                data["review_mode"] = args.mode
            if getattr(args, "keep_ctx_batch", False):
                data["keep_ctx_batch"] = True
            if getattr(args, "keep_ctx_intra", False):
                data["keep_ctx_intra"] = True
            if getattr(args, "p2_fix", False):
                data["p2_fix"] = True
            if getattr(args, "comment", None):
                from task_queue import sanitize_comment
                sanitized = sanitize_comment(args.comment)
                if sanitized:
                    data["comment"] = sanitized
            if getattr(args, "skip_cc_plan", False):
                data["skip_cc_plan"] = True
            if getattr(args, "skip_test", False):
                data["skip_test"] = True
            if getattr(args, "skip_assess", False):
                data["skip_assess"] = True
            if getattr(args, "exclude_high_risk", False):
                data["exclude_high_risk"] = True
            if getattr(args, "exclude_any_risk", False):
                data["exclude_any_risk"] = True
            if getattr(args, "cc_plan_model", None):
                data["cc_plan_model"] = args.cc_plan_model
            if getattr(args, "cc_impl_model", None):
                data["cc_impl_model"] = args.cc_impl_model
        update_pipeline(path, do_mode)


    # 7. INITIALIZEに遷移
    transition_args = argparse.Namespace(
        project=args.project,
        to="INITIALIZE",
        actor="cli",
        force=False,
        resume=False,
    )
    cmd_transition(transition_args)

    # 8. watchdog有効化 + loop起動
    def do_enable(data):
        data["enabled"] = True
    update_pipeline(path, do_enable)
    _start_loop()

    # 9. 完了メッセージ
    issues_str = ", ".join(f"#{n}" for n in issue_nums)
    print(f"{args.project}: started with issues [{issues_str}] → INITIALIZE (watchdog enabled)")


def _reset_to_idle(data: dict) -> None:
    """data を IDLE 状態にリセットする（batch クリア + フラグ除去 + リソース解放）。

    state と history の更新は呼び出し側で行う。
    spec_mode のクリーンアップは行わない（それは cmd_spec_stop の責務）。
    """
    # --- リソース解放（pop より先に実行）---
    from engine.cc import _kill_pytest_baseline
    from engine.reviewer import _cleanup_review_files
    from notify import cleanup_npass_files
    pj = data.get("project", "")
    _kill_pytest_baseline(data, pj)
    _cleanup_review_files(pj)
    cleanup_npass_files(pj)

    # --- 状態クリア ---
    data["batch"] = []
    data["enabled"] = False
    # 既存フィールド
    data.pop("design_revise_count", None)
    data.pop("code_revise_count", None)
    data.pop("automerge", None)
    data.pop("p2_fix", None)
    data.pop("cc_plan_model", None)
    data.pop("cc_impl_model", None)
    data.pop("keep_context", None)
    data.pop("keep_ctx_batch", None)
    data.pop("keep_ctx_intra", None)
    data.pop("comment", None)
    data.pop("skip_cc_plan", None)
    data.pop("skip_test", None)
    data.pop("skip_assess", None)
    data.pop("exclude_high_risk", None)
    data.pop("exclude_any_risk", None)
    data.pop("assessment", None)
    # タイムアウト延長
    data.pop("timeout_extension", None)
    data.pop("extend_count", None)
    # キューモード
    data.pop("queue_mode", None)
    # pytest ベースライン（_kill_pytest_baseline で PID 使用済み）
    data.pop("test_baseline", None)
    data.pop("_pytest_baseline", None)
    # CC 実行追跡
    data.pop("cc_pid", None)
    data.pop("cc_session_id", None)
    # バッチ開始時 HEAD
    data.pop("base_commit", None)
    # レビュアー除外
    data.pop("excluded_reviewers", None)
    data.pop("min_reviews_override", None)
    # マージサマリー
    data.pop("summary_message_id", None)
    # 未送通知
    data.pop("_pending_notifications", None)
    # 状態タイマー
    data.pop("_state_entered_at", None)
    # 前回レビュー退避
    data.pop("_prev_design_reviews", None)
    data.pop("_prev_code_reviews", None)
    # NPASS
    data.pop("_npass_target_reviewers", None)
    # 催促系（動的キー含む）
    data.pop("_last_nudge_at", None)
    for k in [k for k in data if k.startswith(("_nudge_failed_", "_last_nudge_"))]:
        del data[k]
    for k in [k for k in data if k.endswith("_notify_count")]:
        del data[k]


def cmd_transition(args):
    """状態遷移（バリデーション付き）"""
    import config as _cfg
    if getattr(args, "dry_run", False):
        _cfg.DRY_RUN = True
    path = get_path(args.project)
    resume = getattr(args, "resume", False)
    ctx = {}  # ロック内→外の値受け渡し (Issue #59)

    def do_transition(data):
        current = data.get("state", "IDLE")
        target = args.to
        if target not in VALID_STATES:
            raise SystemExit(f"Invalid state: {target}")
        if not args.force and not resume:
            allowed = VALID_TRANSITIONS.get(current, [])
            if target not in allowed:
                raise SystemExit(
                    f"Invalid transition: {current} → {target} (allowed: {allowed}). "
                    f"Use --force to override."
                )
        add_history(data, current, target, args.actor or "cli")
        data["state"] = target
        if target == "IDLE":
            _reset_to_idle(data)
        elif args.force and target in ("DESIGN_REVIEW", "CODE_REVIEW"):
            # Issue #41: Reset counters when force-transitioning to REVIEW states from BLOCKED
            counter_key = "design_revise_count" if "DESIGN" in target else "code_revise_count"
            data.pop(counter_key, None)
            print(f"[FORCE] Resetting {counter_key} for {current} → {target} transition")
        elif target == "BLOCKED":
            # Disable watchdog when manually transitioning to BLOCKED (Issue #29)
            data["enabled"] = False

        # === Issue #59: 通知情報をロック内で構築 + pending フラグ ===
        pj = data.get("project", args.project)
        batch = data.get("batch", [])
        gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{pj}")
        implementer = data.get("implementer", "kaneko")
        repo_path = data.get("repo_path", "")
        review_mode = data.get("review_mode", "standard")

        p2_fix = data.get("p2_fix", False)
        comment = data.get("comment", "")
        notif = get_notification_for_state(target, pj, batch, gitlab, implementer, p2_fix=p2_fix, comment=comment)
        prefix = "（再開）" if resume else ""

        pending = {}
        if notif.impl_msg:
            pending["impl"] = {
                "implementer": implementer,
                "msg": f"[gokrax] {pj}: {prefix}{notif.impl_msg}",
            }
        if notif.send_review:
            pending["review"] = {
                "new_state": target,
                "batch": list(batch),
                "gitlab": gitlab,
                "repo_path": repo_path,
                "review_mode": review_mode,
            }
        if pending:
            if "_pending_notifications" in data:
                _log(f"[{pj}] WARNING: overwriting existing _pending_notifications")
            data["_pending_notifications"] = pending

        ctx.update({
            "pj": pj, "notif": notif, "prefix": prefix,
            "batch": list(batch), "gitlab": gitlab,
            "implementer": implementer, "repo_path": repo_path,
            "review_mode": review_mode,
            "excluded_reviewers": list(data.get("excluded_reviewers", [])),
            "keep_ctx_batch": data.get("keep_ctx_batch", False),
            "keep_ctx_intra": data.get("keep_ctx_intra", False),
            "queue_mode": data.get("queue_mode", False),
            "history": list(data.get("history", [])),
        })

    data = update_pipeline(path, do_transition)
    suffix = " [RESUME]" if resume else (" [FORCED]" if args.force else "")
    print(f"{args.project}: {args.to}{suffix}")

    if not ctx:
        return

    pj = ctx["pj"]
    notif = ctx["notif"]
    prefix = ctx["prefix"]

    if notif.reset_reviewers:
        if args.to in ("DESIGN_REVISE", "CODE_REVISE"):
            skip_reset = True  # REVISE遷移は常にスキップ
        elif args.to == "DESIGN_PLAN":
            skip_reset = ctx["keep_ctx_batch"]
        elif args.to == "IMPLEMENTATION":
            skip_reset = ctx["keep_ctx_intra"]
        else:
            skip_reset = False
        if skip_reset:
            print(f"[{pj}] reset_reviewers SKIPPED (keep_ctx for {args.to})")
        else:
            from engine.reviewer import _reset_reviewers
            impl = ""
            if args.to == "DESIGN_PLAN":
                from config import GOKRAX_STATE_PATH
                # グローバル状態から前回PJを取得（PJ単位JSONではなく共有ファイル）
                try:
                    with open(GOKRAX_STATE_PATH) as _sf:
                        _gstate = json.load(_sf)
                    last_pj = _gstate.get("last_impl_project", "")
                except (FileNotFoundError, json.JSONDecodeError):
                    last_pj = ""
                if not last_pj or last_pj != pj:
                    impl = ctx["implementer"]
                # グローバル状態に記録
                try:
                    with open(GOKRAX_STATE_PATH) as _sf:
                        _gstate = json.load(_sf)
                except (FileNotFoundError, json.JSONDecodeError):
                    _gstate = {}
                _gstate["last_impl_project"] = pj
                with open(GOKRAX_STATE_PATH, "w") as _sf:
                    json.dump(_gstate, _sf, indent=2)
            _reset_reviewers(ctx["review_mode"], implementer=impl)
    if notif.impl_msg:
        phase = STATE_PHASE_MAP.get(args.to, "")
        notify_implementer(ctx["implementer"], f"[gokrax] {pj}: {prefix}{notif.impl_msg}", project=pj, phase=phase)
        clear_pending_notification(pj, "impl")
    if notif.send_review:
        excluded = ctx["excluded_reviewers"]
        notify_reviewers(pj, args.to, ctx["batch"], ctx["gitlab"],
                        repo_path=ctx["repo_path"],
                        review_mode=ctx["review_mode"], excluded=excluded)
        clear_pending_notification(pj, "review")

    # Discord 通知（pending 対象外 — 重複許容）
    history = ctx["history"]
    current = history[-1].get("from", "?") if history else "?"
    actor = args.actor or "cli"
    from datetime import datetime
    ts = datetime.now(LOCAL_TZ).strftime("%m/%d %H:%M")
    q_prefix = "[Queue]" if ctx.get("queue_mode") else ""
    notify_discord(f"{q_prefix}[{pj}] {prefix}{current} → {args.to} (by {actor}, {ts})")


def cmd_reset(args: argparse.Namespace) -> None:
    """非IDLE状態の全PJをIDLEにリセット"""
    targets = []
    for path in sorted(PIPELINES_DIR.glob("*.json")):
        data = load_pipeline(path)
        state = data.get("state", "IDLE")
        if state == "IDLE":
            continue
        targets.append((path, data.get("project", path.stem), state))

    if not targets:
        print("All projects are already IDLE.")
        return

    projects_str = ", ".join(f"{pj} ({st})" for _, pj, st in targets)
    print(f"Projects to reset: {projects_str}")

    if getattr(args, "dry_run", False):
        return

    if not getattr(args, "force", False):
        answer = input(f"Reset {len(targets)} project(s) to IDLE? [y/N] ")
        if answer not in ("y", "Y"):
            print("Aborted.")
            return

    for path, pj, old_state in targets:
        def do_reset(data, _old=old_state):
            add_history(data, _old, "IDLE", "cli")
            data["state"] = "IDLE"
            if data.get("spec_mode", False):
                data["spec_mode"] = False
                data["spec_config"] = {}
            _reset_to_idle(data)
        update_pipeline(path, do_reset)
        print(f"  [RESET] {pj}: {old_state} → IDLE")

    print(f"Reset {len(targets)} project(s) to IDLE.")


def _log(msg: str) -> None:
    """LOG_FILE にメッセージを追記。失敗は無視。"""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{now_iso()} {msg}\n")
    except Exception:
        pass


def _update_issue_title_with_assessment(gitlab: str, issue_num: int, complex_level: int, domain_risk: str = "none") -> bool:
    """Issue タイトルの末尾に [Lvl N] または [Lvl N / Low Risk] 等を付与。既に付いていれば置換。

    domain_risk が "none" の場合はリスク部分を省略し [Lvl N] のみ。
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
    if domain_risk in ("low", "high"):
        _RISK_DISPLAY = {"low": "Low Risk", "high": "High Risk"}
        risk_label = _RISK_DISPLAY[domain_risk]
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



# _post_gitlab_note is imported from notify.py (moved in Issue #177)


def cmd_review(args):
    """レビュー結果を記録（pipeline JSON + GitLab Issue note）"""
    import signal

    path = get_path(args.project)
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
                    "REVISE 中の dispute レビューには --force が必須です"
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
                    f"REVISE 中のレビュー更新は dispute pending 時のみ可能 "
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
            print(f"#{args.issue}: review by {args.reviewer} silently discarded (state={state})")
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
                print(f"#{args.issue}: already reviewed by {args.reviewer}, skipping")
                _skipped = True
                return
            if npass_overwrite:
                print(f"#{args.issue}: NPASS overwrite (pass {existing.get('pass', 1)}/{existing.get('target_pass', 1)})")
            elif args.force:
                print(f"#{args.issue}: overwriting existing review by {args.reviewer} (--force)")
        # dispute accepted 時はレビューを削除して早期 return（次の REVIEW サイクルで再レビューを強制）
        if _dispute_accepted:
            issue[key].pop(args.reviewer, None)
            return
        review_entry = {"verdict": args.verdict, "at": now_iso()}
        if args.summary:
            review_entry["summary"] = args.summary

        # pass / target_pass の計算
        from config import REVIEW_MODES as _REVIEW_MODES
        review_mode = data.get("review_mode", "standard")
        mode_config = _REVIEW_MODES.get(review_mode, {})
        n_pass_config = mode_config.get("n_pass", {})
        target_pass = n_pass_config.get(args.reviewer, 1)
        if not isinstance(target_pass, int) or target_pass < 1:
            print(f"WARNING: n_pass[{args.reviewer}] = {target_pass!r} is invalid, defaulting to 1")
            target_pass = 1

        existing_entry = issue.get(key, {}).get(args.reviewer, {})
        current_pass = existing_entry.get("pass", 0) + 1  # 既存なし→0+1=1, 既存あり→pass+1

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
    print(f"{args.project}: #{args.issue} review by {args.reviewer} = {args.verdict}")

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
    phase = "設計" if "DESIGN" in state else "コード"

    # NPASS 中間パスでは APPROVE の場合のみ GitLab note をスキップ
    # P0/P1/P2 の指摘は中間パスでも GitLab に投稿（開発者が確認できるようにする）
    issue = find_issue(data.get("batch", []), args.issue)
    skip_note = False
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
        note_body = f"[{masked}] {args.verdict} ({phase}レビュー)\n\n{args.summary or ''}"
        if _post_gitlab_note(gitlab, args.issue, note_body):
            print("  → GitLab issue note posted")


def cmd_dispute(args):
    """REVISE中のP0/P1判定に対して異議を申し立てる（dispute）"""
    import signal

    path = get_path(args.project)

    def do_dispute(data):
        state = data.get("state", "IDLE")
        if state not in ("DESIGN_REVISE", "CODE_REVISE"):
            raise SystemExit(f"dispute は REVISE 状態でのみ実行可能 (現在: {state})")

        issue = find_issue(data.get("batch", []), args.issue)
        if not issue:
            raise SystemExit(f"Issue #{args.issue} not in batch")

        if args.reviewer not in ALLOWED_REVIEWERS:
            raise SystemExit(f"Unknown reviewer: {args.reviewer}")

        review_key = "design_reviews" if "DESIGN" in state else "code_reviews"
        reviewer_review = issue.get(review_key, {}).get(args.reviewer, {})
        verdict = reviewer_review.get("verdict", "").upper()
        if verdict not in ("P0", "P1"):
            raise SystemExit(
                f"#{args.issue}: {args.reviewer} の verdict は {verdict or '(なし)'} — "
                f"P0/P1 のみ dispute 可能"
            )

        disputes = issue.setdefault("disputes", [])
        has_pending = any(
            d.get("reviewer") == args.reviewer and d.get("status") == "pending"
            for d in disputes
        )
        if has_pending:
            raise SystemExit(
                f"#{args.issue}: {args.reviewer} への dispute は既に pending"
            )

        if not args.reason.strip():
            raise SystemExit("--reason は空にできません")

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

    print(f"{args.project}: #{args.issue} dispute filed against {args.reviewer}")

    # dispute 即時通知（best-effort）
    state = data.get("state", "IDLE")
    phase = "design" if "DESIGN" in state else "code"
    review_key = "design_reviews" if "DESIGN" in state else "code_reviews"
    issue_data = find_issue(data.get("batch", []), args.issue)
    filed_verdict = ""
    if issue_data:
        filed_verdict = issue_data.get(review_key, {}).get(args.reviewer, {}).get("verdict", "")
    dispute_msg = (
        f"【異議申し立て — あなたの {filed_verdict} 判定に対する異議】\n"
        f"{args.project} #{args.issue}: 実装者があなたの判定に異議を申し立てました。\n\n"
        f"理由:\n{args.reason.strip()}\n\n"
        f"再評価した上で --force 付きで判定を報告してください:\n"
        f"python3 {GOKRAX_CLI} review --pj {args.project} --issue {args.issue} "
        f"--reviewer {args.reviewer} --verdict <APPROVE/P0/P1/P2> --summary \"...\" --force"
    )
    if not send_to_agent_queued(args.reviewer, dispute_msg):
        print(f"WARNING: dispute 通知の送信に失敗 ({args.reviewer})")

    gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
    reviewer_map = data.get("reviewer_number_map")
    masked = mask_agent_name(args.reviewer, reviewer_number_map=reviewer_map)
    note_body = (
        f"[dispute] #{args.issue}: {masked} の判定に異議申し立て\n\n"
        f"理由: {args.reason.strip()}"
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
    risk_reason = args.risk_reason.strip() if args.risk != "none" else ""
    if args.risk != "none" and not risk_reason:
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
            "assessed_by": data.get("implementer", "kaneko"),
            "timestamp": now_iso(),
        }

    update_pipeline(path, do_assess)

    # Issue タイトル更新（失敗は warning、遷移はブロックしない）
    data = load_pipeline(path)
    gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
    if not _update_issue_title_with_assessment(gitlab, args.issue, args.complex_level, args.risk):
        print(f"  ⚠ title update failed for #{args.issue} (warning only)", file=sys.stderr)

    if args.risk in ("low", "high"):
        risk_label = {"low": "Low Risk", "high": "High Risk"}[args.risk]
        print(f"{args.project}: assessment done for #{args.issue} (Lvl {args.complex_level} / {risk_label})")
    else:
        print(f"{args.project}: assessment done for #{args.issue} (Lvl {args.complex_level})")


def cmd_design_revise(args):
    """DESIGN_REVISE: design_revised フラグを設定"""
    path = get_path(args.project)

    if args.comment:
        data = load_pipeline(path)
        gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
        for num in args.issue:
            if not _post_gitlab_note(gitlab, num, args.comment):
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
    path = get_path(args.project)

    if args.comment:
        data = load_pipeline(path)
        gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
        for num in args.issue:
            if not _post_gitlab_note(gitlab, num, args.comment):
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


def cmd_review_mode(args):
    """レビューモードを変更"""
    path = get_path(args.project)

    def do_update(data):
        old = data.get("review_mode", "standard")
        data["review_mode"] = args.mode
        return old

    data = update_pipeline(path, do_update)
    old = data.get("_prev_review_mode", data.get("review_mode", "standard"))
    members = REVIEW_MODES[args.mode]["members"]
    print(f"{args.project}: review_mode → {args.mode} (reviewers: {members})")


def cmd_merge_summary(args):
    """マージサマリーを #gokrax に投稿し、MERGE_SUMMARY_SENT に遷移"""
    import logging
    logger = logging.getLogger(__name__)
    from config import DISCORD_CHANNEL
    from notify import post_discord, notify_implementer
    from config import MERGE_SUMMARY_FOOTER
    from messages import render

    path = get_path(args.project)
    data = load_pipeline(path)
    state = data.get("state", "IDLE")
    if state != "CODE_APPROVED":
        raise SystemExit(f"Cannot send merge summary in state {state} (expected CODE_APPROVED)")

    batch = data.get("batch", [])
    project = data.get("project", args.project)
    automerge = data.get("automerge", False)
    queue_mode = data.get("queue_mode", False)
    content = render("dev.merge_summary_sent", "format_merge_summary",
        project=project, batch=batch, automerge=automerge,
        queue_mode=queue_mode,
        MERGE_SUMMARY_FOOTER=MERGE_SUMMARY_FOOTER,
    )

    message_id = post_discord(DISCORD_CHANNEL, content)
    if not message_id:
        raise SystemExit("Discord 投稿に失敗しました")

    def do_update(data):
        data["summary_message_id"] = message_id
        add_history(data, data["state"], "MERGE_SUMMARY_SENT", "cli")
        data["state"] = "MERGE_SUMMARY_SENT"

    update_pipeline(path, do_update)

    # Notify implementer of batch completion (Issue #48)
    implementer = data.get("implementer") or "kaneko"
    notification_msg = (
        f"[gokrax] {project}: バッチ完了\n"
        f"{content}\n\n"
        "上記の作業を振り返り、以下だけを記録してください:\n"
        "- 踏んだ罠、ハマったこと（あれば）\n"
        "- レビュアー指摘で学んだこと（あれば）\n"
        "- 今後の作業に影響する判断（あれば）\n"
        "記録すべきことがなければ NO_REPLY で構いません。"
    )
    try:
        notify_implementer(implementer, notification_msg)
    except Exception as e:
        logger.warning("実装者通知失敗（続行）: %s", e)

    print(f"{args.project}: merge summary sent (message_id={message_id})")


def cmd_qrun(args):
    """キューから次のバッチを実行: pop → cmd_start → オプション保存"""
    from task_queue import pop_next_queue_entry, restore_queue_entry, peek_queue
    from config import QUEUE_FILE

    queue_path = Path(args.queue) if args.queue else QUEUE_FILE

    # Dry-run: キュー内容を表示
    if args.dry_run:
        entries = peek_queue(queue_path)
        if not entries:
            print("Queue empty")
            return
        for e in entries:
            done = " [DONE]" if e.get("done") else ""
            mode = f" mode={e['mode']}" if e.get("mode") else ""
            opts = []
            if not e.get("automerge", True):
                opts.append("no-automerge")
            if e.get("p2_fix"):
                opts.append("p2-fix")
            if e.get("cc_plan_model"):
                opts.append(f"plan={e['cc_plan_model']}")
            if e.get("cc_impl_model"):
                opts.append(f"impl={e['cc_impl_model']}")
            if e.get("keep_ctx_batch") and e.get("keep_ctx_intra"):
                opts.append("keep-ctx-all")
            elif e.get("keep_ctx_batch"):
                opts.append("keep-ctx-batch")
            elif e.get("keep_ctx_intra"):
                opts.append("keep-ctx-intra")
            if e.get("comment"):
                opts.append(f"comment={e['comment']}")
            if e.get("skip_cc_plan"):
                opts.append("skip-cc-plan")
            if e.get("skip_test"):
                opts.append("skip-test")
            if e.get("skip_assess"):
                opts.append("skip-assess")
            if e.get("exclude_high_risk"):
                opts.append("exclude-high-risk")
            if e.get("exclude_any_risk"):
                opts.append("exclude-any-risk")
            opts_str = " " + " ".join(opts) if opts else ""
            print(f"{e['project']} {e['issues']}{mode}{opts_str}{done}")
        return

    # 次のエントリをpop
    entry = pop_next_queue_entry(queue_path)
    if not entry:
        print("Queue empty or no executable entries")
        return

    # cmd_start 引数を構築
    project = entry["project"]
    issues = entry["issues"]
    mode = entry.get("mode")

    start_args = argparse.Namespace(
        project=project,
        issue=None if issues == "all" else [int(x) for x in issues.split(",")],
        mode=mode,
        keep_ctx_batch=entry.get("keep_ctx_batch", False),
        keep_ctx_intra=entry.get("keep_ctx_intra", False),
        p2_fix=entry.get("p2_fix", False),
        comment=entry.get("comment") or None,
        skip_cc_plan=entry.get("skip_cc_plan", False),
        skip_test=entry.get("skip_test", False),
        skip_assess=entry.get("skip_assess", False),
        exclude_high_risk=entry.get("exclude_high_risk", False),
        exclude_any_risk=entry.get("exclude_any_risk", False),
    )

    # queue_mode を先に設定（cmd_start 内の遷移通知で [Queue] prefix を使うため）
    path = get_path(project)
    def _set_queue_mode_early(data):
        data["queue_mode"] = True
    update_pipeline(path, _set_queue_mode_early)

    # cmd_start 実行 (エラー時は復元)
    try:
        cmd_start(start_args)
    except (SystemExit, Exception) as e:
        restore_queue_entry(queue_path, entry["original_line"])
        print(f"[qrun] Failed to start {project}: {e}", file=sys.stderr)
        raise

    # 成功: automerge/cc_model/comment をパイプラインに保存
    def _save_queue_options(data):
        data["automerge"] = entry.get("automerge", True)
        if entry.get("p2_fix"):
            data["p2_fix"] = True
        if entry.get("cc_plan_model"):
            data["cc_plan_model"] = entry["cc_plan_model"]
        if entry.get("cc_impl_model"):
            data["cc_impl_model"] = entry["cc_impl_model"]
        if entry.get("comment"):
            data["comment"] = entry["comment"]
        if entry.get("skip_cc_plan"):
            data["skip_cc_plan"] = True
        if entry.get("skip_test"):
            data["skip_test"] = True
        if entry.get("skip_assess"):
            data["skip_assess"] = True
        if entry.get("exclude_high_risk"):
            data["exclude_high_risk"] = True
        if entry.get("exclude_any_risk"):
            data["exclude_any_risk"] = True

    update_pipeline(path, _save_queue_options)

    automerge_flag = entry.get("automerge", True)
    print(f"[qrun] {project}: started (automerge={automerge_flag})")


def _get_running_info() -> "dict | None":
    """PIPELINES_DIR を走査し、state != "IDLE" のパイプラインの情報を返す。

    複数が active の場合は全件 warning をログに出し、最初の1件（ソート順）を返す。
    見つからなければ None。
    """
    import logging
    from config import PIPELINES_DIR
    from pipeline_io import load_pipeline

    candidates = []
    for p in sorted(PIPELINES_DIR.glob("*.json")):
        try:
            data = load_pipeline(p)
        except Exception:
            continue
        if data.get("state", "IDLE") != "IDLE":
            batch = data.get("batch", [])
            issue_strs = []
            if batch and isinstance(batch, list):
                for item in batch:
                    if isinstance(item, dict) and "issue" in item:
                        issue_strs.append(f"#{item['issue']}")
                    elif isinstance(item, (int, str)):
                        issue_strs.append(f"#{item}")
            candidates.append({
                "project": data.get("project", p.stem),
                "issues": ",".join(issue_strs),
                "state": data["state"],
                "review_mode": data.get("review_mode") or "",
            })

    if len(candidates) > 1:
        logging.warning(f"Multiple active pipelines detected: {[c['project'] for c in candidates]}")

    return candidates[0] if candidates else None


def get_qstatus_text(entries: list[dict], running: "dict | None" = None) -> str:
    """active エントリのフォーマット済み文字列を返す。"""
    lines = []
    if running:
        parts = [running["project"]]
        if running.get("issues"):
            parts.append(running["issues"])
        if running.get("state"):
            parts.append(running["state"])
        if running.get("review_mode"):
            parts.append(running["review_mode"])
        lines.append(f"[*] {' '.join(parts)}")
    for e in entries:
        idx = e.get("index", 0)
        parts = [e["project"], e["issues"]]
        if e.get("mode"):
            parts.append(e["mode"])
        if not e.get("automerge", True):
            parts.append("no-automerge")
        if e.get("p2_fix"):
            parts.append("p2-fix")
        if e.get("cc_plan_model"):
            parts.append(f"plan={e['cc_plan_model']}")
        if e.get("cc_impl_model"):
            parts.append(f"impl={e['cc_impl_model']}")
        if e.get("keep_ctx_batch") and e.get("keep_ctx_intra"):
            parts.append("keep-ctx-all")
        elif e.get("keep_ctx_batch"):
            parts.append("keep-ctx-batch")
        elif e.get("keep_ctx_intra"):
            parts.append("keep-ctx-intra")
        if e.get("skip_cc_plan"):
            parts.append("skip-cc-plan")
        if e.get("skip_test"):
            parts.append("skip-test")
        if e.get("skip_assess"):
            parts.append("skip-assess")
        if e.get("exclude_high_risk"):
            parts.append("exclude-high-risk")
        if e.get("exclude_any_risk"):
            parts.append("exclude-any-risk")
        lines.append(f"[{idx}] {' '.join(parts)}")
    return "\n".join(lines)


def cmd_qstatus(args):
    """キューの有効エントリを表示"""
    from task_queue import get_active_entries
    from config import QUEUE_FILE

    queue_path = Path(args.queue) if args.queue else QUEUE_FILE
    entries = get_active_entries(queue_path)
    running = _get_running_info()
    if not entries and not running:
        print("Queue empty")
        return
    print(get_qstatus_text(entries, running=running))


def cmd_qadd(args):
    """キューに1行以上追加"""
    from task_queue import append_entry, get_active_entries, parse_queue_line
    from config import QUEUE_FILE

    queue_path = Path(args.queue) if args.queue else QUEUE_FILE

    # 入力ソース決定
    if getattr(args, "file", None):
        if args.entry or getattr(args, "from_stdin", False):
            raise SystemExit("--file と位置引数/--stdin は排他です")
        lines = args.file.read_text().splitlines()
    elif getattr(args, "from_stdin", False):
        if args.entry:
            raise SystemExit("--stdin と位置引数は排他です")
        lines = sys.stdin.read().splitlines()
    elif args.entry:
        lines = [" ".join(args.entry)]
    else:
        raise SystemExit("追加するエントリを指定してください（位置引数 or --file or --stdin）")

    # 空行・コメント行を除外
    lines = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]

    if not lines:
        raise SystemExit("追加するエントリがありません")

    # 全行をバリデーション（1行でもエラーなら全体を中止）
    for i, line in enumerate(lines, 1):
        try:
            parse_queue_line(line)
        except ValueError as e:
            raise SystemExit(f"行 {i}: {e}")

    # バリデーション通過後に追加
    added = []
    for line in lines:
        try:
            append_entry(queue_path, line)
            added.append(line)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # 追加後の状態表示
    entries = get_active_entries(queue_path)
    running = _get_running_info()
    for a in added:
        print(f"Added: {a}")
    print(get_qstatus_text(entries, running=running))


def cmd_qdel(args):
    """キューから1行削除"""
    from task_queue import delete_entry, get_active_entries
    from config import QUEUE_FILE

    queue_path = Path(args.queue) if args.queue else QUEUE_FILE
    target = args.target  # "last", "-1", or integer string

    # Parse target
    if target in ("last", "-1"):
        idx = "last"
    else:
        try:
            idx = int(target)
        except ValueError:
            print(f"Error: invalid target '{target}' (use integer or 'last')", file=sys.stderr)
            sys.exit(1)

    result = delete_entry(queue_path, idx)
    if result is None:
        print("Error: target not found or queue empty", file=sys.stderr)
        sys.exit(1)

    # 削除後の状態表示
    entries = get_active_entries(queue_path)
    running = _get_running_info()
    orig = result.get("original_line", "?")
    print(f"Deleted: {orig}")
    if entries or running:
        print(get_qstatus_text(entries, running=running))
    else:
        print("Queue empty")


def cmd_qedit(args):
    """キューのエントリを置換"""
    from task_queue import replace_entry, get_active_entries
    from config import QUEUE_FILE

    queue_path = Path(args.queue) if args.queue else QUEUE_FILE
    display_target = args.target

    # Parse target
    if args.target in ("last", "-1"):
        idx = "last"
    else:
        try:
            idx = int(args.target)
        except ValueError:
            print(f"Error: invalid target '{args.target}' (use integer or 'last')", file=sys.stderr)
            sys.exit(1)

    new_line = " ".join(args.entry)

    try:
        result = replace_entry(queue_path, idx, new_line)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if result is None:
        print("Error: target not found or queue empty", file=sys.stderr)
        sys.exit(1)

    print(f"Replaced [{display_target}]: {new_line}")
    entries = get_active_entries(queue_path)
    running = _get_running_info()
    if entries or running:
        print(get_qstatus_text(entries, running=running))
    else:
        print("Queue empty")
