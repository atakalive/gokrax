#!/usr/bin/env python3
"""gokrax — 開発パイプラインCLI

pipeline JSONの唯一の操作インターフェース。直接JSON編集禁止。
"""

import argparse
import json
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    PIPELINES_DIR, GLAB_BIN, LOG_FILE,
    VALID_STATES, VALID_TRANSITIONS, MAX_BATCH, TRIAGE_ALLOWED_STATES,
    VALID_VERDICTS, GLAB_TIMEOUT, ALLOWED_REVIEWERS, REVIEW_MODES, JST,
    WATCHDOG_LOOP_SCRIPT, WATCHDOG_LOOP_PIDFILE,
    WATCHDOG_LOOP_CRON_MARKER, WATCHDOG_LOOP_CRON_ENTRY,
    VALID_FLAG_VERDICTS, STATE_PHASE_MAP,
    GOKRAX_CLI, OWNER_NAME,
)
from pipeline_io import (
    load_pipeline, save_pipeline, update_pipeline,
    add_history, now_iso, get_path, find_issue,
    clear_pending_notification,
)
from engine.fsm import get_notification_for_state
import os

# Verdict severity for dispute resolution (Issue #86)
VERDICT_SEVERITY = {"REJECT": 3, "P0": 3, "P1": 2, "P2": 1, "APPROVE": 0}


# === Watchdog Loop Management ===

def _is_loop_running() -> bool:
    """watchdog-loop.sh が稼働中か判定。"""
    if not WATCHDOG_LOOP_PIDFILE.exists():
        return False
    try:
        pid = int(WATCHDOG_LOOP_PIDFILE.read_text().strip())
        return Path(f"/proc/{pid}").exists()
    except (ValueError, OSError):
        return False


def _start_loop():
    """watchdog-loop.sh をバックグラウンド起動し、crontab復帰エントリを追加。"""
    if _is_loop_running():
        return
    # loop.sh 起動
    subprocess.Popen(
        ["nohup", "bash", str(WATCHDOG_LOOP_SCRIPT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # crontab に復帰エントリ追加
    _ensure_cron_entry()


def _stop_loop():
    """watchdog-loop.sh を停止。crontabは残す（次回enable時に自動復帰）。"""
    if WATCHDOG_LOOP_PIDFILE.exists():
        try:
            pid = int(WATCHDOG_LOOP_PIDFILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, OSError):
            pass


def _ensure_cron_entry():
    """crontab に watchdog-loop 復帰エントリがなければ追加。"""
    try:
        current = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        ).stdout
    except Exception:
        current = ""
    if WATCHDOG_LOOP_CRON_MARKER in current:
        return
    new = current.rstrip("\n") + "\n" + WATCHDOG_LOOP_CRON_ENTRY + "\n"
    subprocess.run(["crontab", "-"], input=new, text=True, check=True)


def _remove_cron_entry():
    """crontab から watchdog-loop エントリを削除。"""
    try:
        current = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        ).stdout
    except Exception:
        return
    lines = [l for l in current.splitlines() if WATCHDOG_LOOP_CRON_MARKER not in l]
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)


def _any_pj_enabled() -> bool:
    """いずれかのPJが enabled=True か判定。"""
    for path in PIPELINES_DIR.glob("*.json"):
        data = load_pipeline(path)
        if data.get("enabled", False):
            return True
    return False
from notify import notify_implementer, notify_reviewers, notify_discord, send_to_agent, send_to_agent_queued
from commands.spec import (  # noqa: F401 — re-export for backwards compatibility
    cmd_spec,
    cmd_spec_start, cmd_spec_approve, cmd_spec_continue, cmd_spec_done,
    cmd_spec_stop, cmd_spec_retry, cmd_spec_resume, cmd_spec_extend,
    cmd_spec_status, cmd_spec_review_submit, cmd_spec_revise_submit,
    cmd_spec_self_review_submit, cmd_spec_issue_submit,
    cmd_spec_queue_submit, cmd_spec_suggestion_submit,
)


# === Commands ===

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
        "gitlab": args.gitlab or f"atakalive/{args.project}",
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
    path = get_path(args.project)

    def do_enable(data):
        data["enabled"] = True

    update_pipeline(path, do_enable)
    _start_loop()
    print(f"{args.project}: watchdog enabled")


def cmd_disable(args):
    path = get_path(args.project)

    def do_disable(data):
        data["enabled"] = False

    update_pipeline(path, do_disable)
    if not _any_pj_enabled():
        _stop_loop()
        print(f"{args.project}: watchdog disabled (loop stopped — no active projects)")
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
    ts = datetime.now(JST).strftime("%m/%d %H:%M")
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
    gitlab = data.get("gitlab", f"atakalive/{args.project}")
    titles = list(args.title) + [""] * (len(args.issue) - len(args.title))
    # タイトルが空のIssueはGitLab APIで取得
    for idx, (num, title) in enumerate(zip(args.issue, titles)):
        if not title:
            titles[idx] = _fetch_issue_title(num, gitlab)

    def do_triage(data):
        state = data.get("state", "IDLE")
        if state not in TRIAGE_ALLOWED_STATES:
            raise SystemExit(f"Cannot add issues in state {state} (allowed: {TRIAGE_ALLOWED_STATES})")
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
    from config import DEFAULT_QUEUE_OPTIONS

    # 明示的な否定フラグを先に処理
    if getattr(args, "keep_ctx_none", None):
        args.keep_ctx_batch = False
        args.keep_ctx_intra = False
    if getattr(args, "no_skip_cc_plan", None):
        args.skip_cc_plan = False

    # デフォルトオプション適用: CLI 引数で明示指定されていない（None のまま）オプションにデフォルト値を注入
    for key, default_val in DEFAULT_QUEUE_OPTIONS.items():
        if getattr(args, key, None) is None:
            setattr(args, key, default_val)

    # None のまま残っているオプションを False に正規化（後続コードが bool を期待するため）
    for key in ("keep_ctx_batch", "keep_ctx_intra", "p2_fix", "skip_cc_plan"):
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
    update_pipeline(path, _clear_stale_skip)

    # 2. Issue番号取得（--issue指定 or GitLab API）
    if args.issue:
        issue_nums = args.issue
        titles = []
    else:
        # GitLab APIでopen issue全件取得（タイトル付き）
        gitlab = data.get("gitlab", f"atakalive/{args.project}")
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
    if getattr(args, "mode", None) or has_keep_ctx or has_p2_fix or has_comment or has_skip_cc_plan:
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
    pj = data.get("project", "")
    _kill_pytest_baseline(data, pj)
    _cleanup_review_files(pj)

    # --- 状態クリア ---
    data["batch"] = []
    data["enabled"] = False
    # 既存フィールド
    data.pop("design_revise_count", None)
    data.pop("code_revise_count", None)
    data.pop("automerge", None)
    data.pop("p2_fix", None)
    data.pop("p1_fix", None)
    data.pop("cc_plan_model", None)
    data.pop("cc_impl_model", None)
    data.pop("keep_context", None)
    data.pop("keep_ctx_batch", None)
    data.pop("keep_ctx_intra", None)
    data.pop("comment", None)
    data.pop("skip_cc_plan", None)
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
        gitlab = data.get("gitlab", f"atakalive/{pj}")
        implementer = data.get("implementer", "kaneko")
        repo_path = data.get("repo_path", "")
        review_mode = data.get("review_mode", "standard")

        # p1_fix → p2_fix 昇格（後方互換）
        p2_fix = data.get("p2_fix", False) or data.get("p1_fix", False)
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
        notify_implementer(ctx["implementer"], f"[gokrax] {pj}: {prefix}{notif.impl_msg}")
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
    ts = datetime.now(JST).strftime("%m/%d %H:%M")
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


def _post_gitlab_note(gitlab: str, issue_num: int, body: str) -> bool:
    """glab issue note を投稿。失敗時は2回リトライ（間隔3秒）。"""
    for attempt in range(3):
        try:
            result = subprocess.run(
                [GLAB_BIN, "issue", "note", str(issue_num), "-m", body, "-R", gitlab],
                capture_output=True, text=True, timeout=GLAB_TIMEOUT,
            )
            if result.returncode == 0:
                return True
            _log(f"glab note failed (attempt {attempt+1}/3): {result.stderr.strip()}")
        except Exception as e:
            _log(f"glab note error (attempt {attempt+1}/3): {e}")
        if attempt < 2:
            time.sleep(3)
    print("  ⚠ GitLab note failed after 3 attempts", file=sys.stderr)
    return False


def cmd_review(args):
    """レビュー結果を記録（pipeline JSON + GitLab Issue note）"""
    path = get_path(args.project)
    _skipped = False
    _dispute_accepted = False

    def do_review(data):
        nonlocal _skipped, _dispute_accepted
        state = data.get("state", "IDLE")
        if state == "DESIGN_REVIEW":
            key = "design_reviews"
        elif state == "CODE_REVIEW":
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
            raise SystemExit(f"Not in review state: {state}")
        # ラウンド番号検証: stale なレビュー（前サイクルの Remind 応答等）を拒否する。
        # DESIGN_REVISE/CODE_REVISE 状態では dispute レビュー（--force 必須）のみ
        # ここに到達する。dispute 経由の場合、notify_dispute が --round を付与しない
        # ため _round_arg=None となり、検証はスキップされる。
        from config import get_current_round
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
            if not args.force:
                print(f"#{args.issue}: already reviewed by {args.reviewer}, skipping")
                _skipped = True
                return
            print(f"#{args.issue}: overwriting existing review by {args.reviewer} (--force)")
        # dispute accepted 時はレビューを削除して早期 return（次の REVIEW サイクルで再レビューを強制）
        if _dispute_accepted:
            issue[key].pop(args.reviewer, None)
            return
        review_entry = {"verdict": args.verdict, "at": now_iso()}
        if args.summary:
            review_entry["summary"] = args.summary
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
    gitlab = data.get("gitlab", f"atakalive/{args.project}")
    phase = "設計" if "DESIGN" in state else "コード"
    note_body = f"[{args.reviewer}] {args.verdict} ({phase}レビュー)\n\n{args.summary or ''}"
    if _post_gitlab_note(gitlab, args.issue, note_body):
        print("  → GitLab issue note posted")


def cmd_dispute(args):
    """REVISE中のP0/P1判定に対して異議を申し立てる（dispute）"""
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

    gitlab = data.get("gitlab", f"atakalive/{args.project}")
    note_body = (
        f"[dispute] #{args.issue}: {args.reviewer} の判定に異議申し立て\n\n"
        f"理由: {args.reason.strip()}"
    )
    _post_gitlab_note(gitlab, args.issue, note_body)


def cmd_flag(args):
    """人間（M）による P0/P1 差し込み（任意タイミング）"""
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
    gitlab = data.get("gitlab", f"atakalive/{args.project}")
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


def cmd_design_revise(args):
    """DESIGN_REVISE: design_revised フラグを設定"""
    path = get_path(args.project)

    if args.comment:
        data = load_pipeline(path)
        gitlab = data.get("gitlab", f"atakalive/{args.project}")
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
        gitlab = data.get("gitlab", f"atakalive/{args.project}")
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


def main():
    parser = argparse.ArgumentParser(
        prog="gokrax",
        description="gokrax — Issue→設計→実装→レビュー→マージの開発パイプラインCLI",
    )
    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", help="全プロジェクトの状態・バッチ・レビュー進捗を一覧表示")

    # init
    p = sub.add_parser("init", help="新プロジェクトのパイプラインを初期化")
    p.add_argument("--pj", "--project", dest="project", required=True, help="プロジェクト名")
    p.add_argument("--gitlab", help="GitLabパス (default: atakalive/<project>)")
    p.add_argument("--repo-path", dest="repo_path", help="ローカルリポジトリのパス")
    p.add_argument("--implementer", default="kaneko", help="実装担当エージェント (default: kaneko)")

    # enable / disable
    p = sub.add_parser("enable", help="watchdogによる自動遷移・催促を有効化")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p = sub.add_parser("disable", help="watchdogを無効化（手動操作のみ）")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # extend
    p = sub.add_parser("extend", help="DESIGN_PLAN/IMPL等のタイムアウトを延長")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--by", type=int, default=600, help="延長秒数 (default: 600)")

    # start
    p = sub.add_parser("start", help="バッチ開始: triage→DESIGN_PLAN遷移→watchdog有効化を一括実行")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, nargs="+",
                   help="Issue番号（省略時はGitLabのopen issue全件を自動取得）")
    p.add_argument("--mode", choices=["full", "standard", "lite", "min", "skip"],
                   help="レビューモード（省略時は既存設定を維持）")
    p.add_argument("--keep-context", action="store_true", default=False, dest="keep_context",
                   help="(後方互換) = --keep-ctx-all")
    p.add_argument("--keep-ctx-batch", action="store_true", default=None, dest="keep_ctx_batch")
    p.add_argument("--keep-ctx-intra", action="store_true", default=None, dest="keep_ctx_intra")
    p.add_argument("--keep-ctx-all", action="store_true", default=None, dest="keep_ctx_all")
    p.add_argument("--keep-ctx-none", action="store_true", default=None, dest="keep_ctx_none")
    p.add_argument("--p2-fix", action="store_true", default=None, dest="p2_fix")
    p.add_argument("--comment", default=None, help="バッチ全体への注意事項（プロンプトに挿入される）")
    p.add_argument("--skip-cc-plan", action="store_true", default=None, dest="skip_cc_plan",
                   help="CC Plan フェーズをスキップし、直接 Impl に入る")
    p.add_argument("--no-skip-cc-plan", action="store_true", default=None, dest="no_skip_cc_plan")

    # triage
    p = sub.add_parser("triage", help="指定Issueをバッチに投入")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="Issue番号（複数指定可）")
    p.add_argument("--title", action="append", default=[], help="タイトル（--issue と同数、省略時は空文字）")

    # transition
    p = sub.add_parser("transition", help="手動で状態遷移（通常はwatchdogが自動実行）")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--to", required=True, help="遷移先の状態")
    p.add_argument("--actor", default="cli", help="遷移実行者 (default: cli)")
    p.add_argument("--force", action="store_true", default=False,
                   help="遷移バリデーションをスキップ")
    p.add_argument("--resume", action="store_true", default=False,
                   help="バリデーションスキップ＋通知に「（再開）」プレフィックス付与")
    p.add_argument("--dry-run", action="store_true", default=False, dest="dry_run",
                   help="遷移のみ実行し通知をスキップ（テスト用）")

    # reset
    p = sub.add_parser("reset", help="非IDLE状態の全PJをIDLEにリセット")
    p.add_argument("--dry-run", action="store_true", help="変更せず対象を表示のみ")
    p.add_argument("--force", action="store_true", help="確認プロンプトをスキップ")

    # review
    p = sub.add_parser("review", help="レビュー結果を記録（冪等: 同一レビュアーの二重投稿はスキップ）")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--reviewer", required=True, choices=ALLOWED_REVIEWERS)
    p.add_argument("--verdict", required=True, choices=VALID_VERDICTS,
                   help="APPROVE/P0/P1/REJECT")
    p.add_argument("--summary", default="", help="レビューサマリー")
    p.add_argument("--force", action="store_true", default=False,
                   help="既存レビューを上書きする")
    p.add_argument("--round", type=int, default=None, help="レビューラウンド番号（自動埋め込み）")

    # flag
    p = sub.add_parser("flag", help="人間（M）による P0/P1 差し込み（任意タイミング）")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--verdict", required=True, choices=VALID_FLAG_VERDICTS,
                   help="P0 (blocks progress) or P1 (informational)")
    p.add_argument("--summary", default="", help="フラグの説明")

    # dispute
    p = sub.add_parser("dispute", help="REVISE中のP0/P1判定に異議を申し立て")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--reviewer", required=True, choices=ALLOWED_REVIEWERS)
    p.add_argument("--reason", required=True, help="異議の理由")

    # commit
    p = sub.add_parser("commit", help="実装完了: commitハッシュをバッチに記録")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="Issue番号（複数指定可）")
    p.add_argument("--hash", required=True, help="gitコミットハッシュ")
    p.add_argument("--session-id", default=None, help="CC セッションID")

    # cc-start
    p = sub.add_parser("cc-start", help="CC (Claude Code) 実行開始時にPIDを記録")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--pid", type=int, required=True, help="CCプロセスのPID")

    # plan-done
    p = sub.add_parser("plan-done", help="設計確認完了: 対象Issueにdesign_readyフラグを設定")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="Issue番号（複数指定可）")

    # design-revise
    p = sub.add_parser("design-revise", help="設計修正完了: DESIGN_REVISE状態でdesign_revisedフラグを設定")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="Issue番号（複数指定可）")
    p.add_argument("--comment", default=None, help="GitLab issue noteに投稿するコメント（省略可）")

    # code-revise
    p = sub.add_parser("code-revise", help="コード修正完了: CODE_REVISE状態でcommit記録+code_revisedフラグを一発で設定")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="Issue番号（複数指定可）")
    p.add_argument("--hash", required=True, help="gitコミットハッシュ")
    p.add_argument("--comment", default=None, help="GitLab issue noteに投稿するコメント（省略可）")

    # review-mode
    p = sub.add_parser("review-mode", help="レビューモード変更 (full=4人/standard=3人/lite=2人/min=1人/skip=なし)")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--mode", required=True, choices=list(REVIEW_MODES.keys()),
                   help="full/standard/lite/min/skip")

    # merge-summary
    p = sub.add_parser("merge-summary", help="マージサマリーを #gokrax に投稿してMの承認待ちへ")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # qrun
    p = sub.add_parser("qrun", help="キューから次のバッチを実行")
    p.add_argument("--queue", type=Path, help="キューファイルパス (default: gokrax-queue.txt)")
    p.add_argument("--dry-run", action="store_true", help="実行せず内容のみ表示")

    # qstatus
    p = sub.add_parser("qstatus", help="キューの有効エントリを表示")
    p.add_argument("--queue", type=Path, help="キューファイルパス")

    # qadd
    p = sub.add_parser("qadd", help="キューに1行以上追加")
    p.add_argument("entry", nargs="*", help="追加するエントリ (例: BeamShifter 33,34 lite no-automerge comment=注意事項) ※comment=は末尾専用")
    p.add_argument("--file", type=Path, dest="file", help="エントリファイルパス（1行1エントリ）")
    p.add_argument("--stdin", action="store_true", dest="from_stdin", help="stdinから複数行を読み込む")
    p.add_argument("--queue", type=Path, help="キューファイルパス")

    # qdel
    p = sub.add_parser("qdel", help="キューから1行削除")
    p.add_argument("target", help="削除対象 (インデックス番号 or 'last')")
    p.add_argument("--queue", type=Path, help="キューファイルパス")

    # qedit
    p = sub.add_parser("qedit", help="キューのエントリを置換")
    p.add_argument("target", help="置換対象 (インデックス番号 or 'last')")
    p.add_argument("entry", nargs="+", help="新しいエントリ (例: gokrax 105 full automerge)")
    p.add_argument("--queue", type=Path, help="キューファイルパス")

    # spec
    spec_parser = sub.add_parser("spec", help="Spec mode commands")
    spec_sub = spec_parser.add_subparsers(dest="spec_command")

    # spec start
    p = spec_sub.add_parser("start", help="spec modeパイプライン開始")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--spec", required=True, help="specファイルのリポジトリ相対パス")
    p.add_argument("--implementer", required=True, help="改訂エージェントID")
    p.add_argument("--review-only", action="store_true", default=False, dest="review_only")
    p.add_argument("--no-queue", action="store_true", default=False, dest="no_queue")
    p.add_argument("--skip-review", action="store_true", default=False, dest="skip_review")
    p.add_argument("--max-cycles", type=int, default=None, dest="max_cycles")
    p.add_argument("--review-mode", default=None, dest="review_mode",
                   choices=["full", "standard", "lite", "min"])
    p.add_argument("--model", default=None)
    p.add_argument("--auto-continue", action="store_true", default=False, dest="auto_continue")
    p.add_argument("--auto-qrun", action="store_true", default=False, dest="auto_qrun")
    p.add_argument("--rev", type=int, default=None,
                   help="current_revの初期値（デフォルト: 1）")

    # spec stop
    p = spec_sub.add_parser("stop", help="spec modeを強制停止してIDLEに戻す")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # spec approve
    p = spec_sub.add_parser("approve", help="SPEC_APPROVEDに遷移")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--force", action="store_true", default=False)

    # spec continue
    p = spec_sub.add_parser("continue", help="APPROVED → ISSUE_SUGGESTION")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # spec done
    p = spec_sub.add_parser("done", help="SPEC_DONE → IDLE")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # spec retry
    p = spec_sub.add_parser("retry", help="FAILED → REVIEW")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # spec resume
    p = spec_sub.add_parser("resume", help="PAUSED → paused_from")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # spec extend
    p = spec_sub.add_parser("extend", help="STALLED → REVISE (MAX増加)")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--cycles", type=int, default=2, help="追加サイクル数 (default: 2)")

    # spec status
    p = spec_sub.add_parser("status", help="spec mode ステータス表示")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # spec review-submit
    p = spec_sub.add_parser("review-submit", help="レビュー結果をYAMLファイルから投入")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--file", required=True)

    # spec revise-submit
    p = spec_sub.add_parser("revise-submit", help="SPEC_REVISE完了報告をファイルから投入")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--file", required=True)

    # spec self-review-submit
    p = spec_sub.add_parser("self-review-submit", help="セルフレビュー結果をファイルから投入")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--file", required=True)

    # spec issue-submit
    p = spec_sub.add_parser("issue-submit", help="ISSUE_PLAN完了報告をファイルから投入")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--file", required=True)

    # spec queue-submit
    p = spec_sub.add_parser("queue-submit", help="QUEUE_PLAN完了報告をファイルから投入")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--file", required=True)

    # spec suggestion-submit
    p = spec_sub.add_parser("suggestion-submit", help="ISSUE_SUGGESTIONのレビュアー提案をファイルから投入")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--file", required=True)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "status": cmd_status, "init": cmd_init,
        "enable": cmd_enable, "disable": cmd_disable,
        "extend": cmd_extend, "start": cmd_start,
        "triage": cmd_triage, "transition": cmd_transition, "reset": cmd_reset,
        "review": cmd_review, "flag": cmd_flag, "dispute": cmd_dispute, "commit": cmd_commit,
        "cc-start": cmd_cc_start, "plan-done": cmd_plan_done,
        "design-revise": cmd_design_revise, "code-revise": cmd_code_revise,
        "review-mode": cmd_review_mode,
        "merge-summary": cmd_merge_summary,
        "qrun": cmd_qrun,
        "qstatus": cmd_qstatus,
        "qadd": cmd_qadd,
        "qdel": cmd_qdel,
        "qedit": cmd_qedit,
        "spec": cmd_spec,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()


# --- spec stop (injected) は下のparser登録で追加 ---
