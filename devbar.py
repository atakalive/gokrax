#!/usr/bin/env python3
"""devbar — 開発パイプラインCLI

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
    MAX_SPEC_REVISE_CYCLES, MIN_VALID_REVIEWS_BY_MODE,
    SPEC_REVIEW_TIMEOUT_SEC, SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC,
    SPEC_REVISE_SELF_REVIEW_PASSES, MAX_SPEC_RETRIES,
)
from pipeline_io import (
    load_pipeline, save_pipeline, update_pipeline,
    add_history, now_iso, get_path, find_issue,
    clear_pending_notification, default_spec_config,
)
from watchdog import get_notification_for_state
import os


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
from notify import notify_implementer, notify_reviewers, notify_discord, send_to_agent, spec_notify_approved_forced, spec_notify_review_start


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
    from watchdog import EXTENDABLE_STATES

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
    """devbar start --project X [--issue N [N...]]

    triage + DESIGN_PLAN遷移 + watchdog有効化を一括実行。
    --issue省略時はGitLab APIでopen issue全件取得。
    """
    path = get_path(args.project)

    # 1. 前提条件チェック: IDLE状態でなければエラー
    data = load_pipeline(path)
    if data.get("state", "IDLE") != "IDLE":
        raise SystemExit(
            f"Cannot start: current state is {data['state']} (expected IDLE)"
        )

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

    # 5. review_mode / keep_ctx 設定（遷移前に設定して/newの宛先に反映させる）
    has_keep_ctx = getattr(args, "keep_ctx_batch", False) or getattr(args, "keep_ctx_intra", False)
    if getattr(args, "mode", None) or has_keep_ctx:
        from watchdog import REVIEW_MODES
        if getattr(args, "mode", None) and args.mode not in REVIEW_MODES:
            raise SystemExit(f"Invalid mode: {args.mode} (valid: {list(REVIEW_MODES)})")
        def do_mode(data):
            if getattr(args, "mode", None):
                data["review_mode"] = args.mode
            if getattr(args, "keep_ctx_batch", False):
                data["keep_ctx_batch"] = True
            if getattr(args, "keep_ctx_intra", False):
                data["keep_ctx_intra"] = True
        update_pipeline(path, do_mode)

    # 6. DESIGN_PLANに遷移
    transition_args = argparse.Namespace(
        project=args.project,
        to="DESIGN_PLAN",
        actor="cli",
        force=False,
        resume=False,
    )
    cmd_transition(transition_args)

    # 7. watchdog有効化 + loop起動
    def do_enable(data):
        data["enabled"] = True
    update_pipeline(path, do_enable)
    _start_loop()

    # 8. 完了メッセージ
    issues_str = ", ".join(f"#{n}" for n in issue_nums)
    print(f"{args.project}: started with issues [{issues_str}] → DESIGN_PLAN (watchdog enabled)")


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
            data["batch"] = []
            data["enabled"] = False
            # Reset REVISE cycle counters when returning to IDLE (Issue #29)
            data.pop("design_revise_count", None)
            data.pop("code_revise_count", None)
            # Clear queue options (Issue #45)
            data.pop("automerge", None)
            data.pop("cc_plan_model", None)
            data.pop("cc_impl_model", None)
            data.pop("keep_context", None)      # 旧フラグ（後方互換クリーンアップ）
            data.pop("keep_ctx_batch", None)
            data.pop("keep_ctx_intra", None)
        elif target == "DESIGN_PLAN":
            # Reset REVISE cycle counters when starting new batch (Issue #29)
            data.pop("design_revise_count", None)
            data.pop("code_revise_count", None)
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

        notif = get_notification_for_state(target, pj, batch, gitlab, implementer)
        prefix = "（再開）" if resume else ""

        pending = {}
        if notif.impl_msg:
            pending["impl"] = {
                "implementer": implementer,
                "msg": f"[devbar] {pj}: {prefix}{notif.impl_msg}",
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
            from watchdog import _reset_reviewers
            impl = ""
            if args.to == "DESIGN_PLAN":
                from config import DEVBAR_STATE_PATH
                # グローバル状態から前回PJを取得（PJ単位JSONではなく共有ファイル）
                try:
                    with open(DEVBAR_STATE_PATH) as _sf:
                        _gstate = json.load(_sf)
                    last_pj = _gstate.get("last_impl_project", "")
                except (FileNotFoundError, json.JSONDecodeError):
                    last_pj = ""
                if not last_pj or last_pj != pj:
                    impl = ctx["implementer"]
                # グローバル状態に記録
                try:
                    with open(DEVBAR_STATE_PATH) as _sf:
                        _gstate = json.load(_sf)
                except (FileNotFoundError, json.JSONDecodeError):
                    _gstate = {}
                _gstate["last_impl_project"] = pj
                with open(DEVBAR_STATE_PATH, "w") as _sf:
                    json.dump(_gstate, _sf, indent=2)
            _reset_reviewers(ctx["review_mode"], implementer=impl)
    if notif.impl_msg:
        notify_implementer(ctx["implementer"], f"[devbar] {pj}: {prefix}{notif.impl_msg}")
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

    def do_review(data):
        nonlocal _skipped
        state = data.get("state", "IDLE")
        if state == "DESIGN_REVIEW":
            key = "design_reviews"
        elif state == "CODE_REVIEW":
            key = "code_reviews"
        else:
            raise SystemExit(f"Not in review state: {state}")
        issue = find_issue(data.get("batch", []), args.issue)
        if not issue:
            raise SystemExit(f"Issue #{args.issue} not in batch")
        # 冪等性: 同じレビュアーが既にレビュー済みならスキップ（--force で上書き可）
        if args.reviewer in issue.get(key, {}):
            if not args.force:
                print(f"#{args.issue}: already reviewed by {args.reviewer}, skipping")
                _skipped = True
                return
            print(f"#{args.issue}: overwriting existing review by {args.reviewer} (--force)")
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

    # GitLab Issue note に自動投稿
    gitlab = data.get("gitlab", f"atakalive/{args.project}")
    phase = "設計" if "DESIGN" in state else "コード"
    note_body = f"[{args.reviewer}] {args.verdict} ({phase}レビュー)\n\n{args.summary or ''}"
    if _post_gitlab_note(gitlab, args.issue, note_body):
        print("  → GitLab issue note posted")


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
            "by": "M",
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
    """マージサマリーを #dev-bar に投稿し、MERGE_SUMMARY_SENT に遷移"""
    import logging
    logger = logging.getLogger(__name__)
    from config import DISCORD_CHANNEL
    from notify import post_discord, notify_implementer
    from watchdog import _format_merge_summary

    path = get_path(args.project)
    data = load_pipeline(path)
    state = data.get("state", "IDLE")
    if state != "CODE_APPROVED":
        raise SystemExit(f"Cannot send merge summary in state {state} (expected CODE_APPROVED)")

    batch = data.get("batch", [])
    project = data.get("project", args.project)
    automerge = data.get("automerge", False)
    content = _format_merge_summary(project, batch, automerge=automerge)

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
        f"[devbar] {project}: バッチ完了\n"
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
            if e.get("automerge"):
                opts.append("automerge")
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

    # 成功: automerge/cc_model をパイプラインに保存
    def _save_queue_options(data):
        if entry.get("automerge"):
            data["automerge"] = True
        if entry.get("cc_plan_model"):
            data["cc_plan_model"] = entry["cc_plan_model"]
        if entry.get("cc_impl_model"):
            data["cc_impl_model"] = entry["cc_impl_model"]

    update_pipeline(path, _save_queue_options)

    automerge_flag = entry.get("automerge", False)
    print(f"[qrun] {project}: started (automerge={automerge_flag})")


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
        "timestamp": datetime.now(JST).isoformat(),
    }
    spec_config.setdefault("review_history", []).append(history_entry)
    spec_config["current_reviews"] = {}


def cmd_spec_start(args):
    """spec modeパイプライン開始（§4.2, §2.5, §2.6, §3.3）"""
    path = get_path(args.project)
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

    review_mode = args.review_mode or data.get("review_mode", "full")
    reviewers = REVIEW_MODES.get(review_mode, REVIEW_MODES["full"])["members"]
    review_requests = {
        r: {
            "status": "pending",
            "sent_at": None,
            "timeout_at": None,
            "last_nudge_at": None,
            "response": None,
        }
        for r in reviewers
    }

    pipelines_dir = str(Path(PIPELINES_DIR) / args.project / "spec-reviews")

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
            "max_revise_cycles": args.max_cycles if args.max_cycles is not None else MAX_SPEC_REVISE_CYCLES,
            "model": args.model,
            "review_requests": review_requests,
            "pipelines_dir": pipelines_dir,
        })
        data["spec_mode"] = True
        data["state"] = "SPEC_APPROVED" if skip_review else "SPEC_REVIEW"
        data["enabled"] = True
        if args.review_mode:
            data["review_mode"] = review_mode
        data["spec_config"] = sc

    update_pipeline(path, do_start)
    _start_loop()

    target = "SPEC_APPROVED" if skip_review else "SPEC_REVIEW"
    if not skip_review:
        reviewer_count = len(review_requests)
        try:
            notify_discord(spec_notify_review_start(args.project, "1", reviewer_count))
        except Exception:
            logger.warning("Failed to send review_start notification")
    print(f"{args.project}: spec mode started (spec={args.spec}) → {target}")


def cmd_spec_approve(args):
    """SPEC_APPROVEDに遷移（§4.3）"""
    _APPROVE_ALLOWED = ("SPEC_REVIEW", "SPEC_STALLED", "SPEC_REVISE")
    path = get_path(args.project)
    # 全チェックをflock内に移動してTOCTOU回避
    ctx = {}  # ロック内→外の値受け渡し

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
                "at": datetime.now(JST).isoformat(),
                "actor": "M",
                "from_state": state,
                "rev": sc.get("current_rev", "?"),
                "rev_index": sc.get("rev_index", 0),
                "remaining_p1_items": remaining,
            })
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
        data["state"] = "ISSUE_SUGGESTION"

    update_pipeline(path, do_continue)
    print(f"{args.project}: SPEC_APPROVED → ISSUE_SUGGESTION")


def cmd_spec_done(args):
    """SPEC_DONE → IDLE"""
    path = get_path(args.project)
    data = load_pipeline(path)
    if data.get("state") != "SPEC_DONE":
        raise SystemExit(f"Cannot done: state is {data['state']} (expected SPEC_DONE)")

    def do_done(data):
        data["state"] = "IDLE"
        data["spec_mode"] = False
        data["spec_config"] = {}

    update_pipeline(path, do_done)
    print(f"{args.project}: SPEC_DONE → IDLE (spec mode ended)")




def cmd_spec_stop(args):
    """spec modeを強制停止してIDLEに戻す"""
    path = get_path(args.project)
    data = load_pipeline(path)
    if not data.get("spec_mode"):
        raise SystemExit(f"{args.project}: spec mode is not active")

    old_state = data.get("state", "IDLE")

    def do_stop(data):
        data["state"] = "IDLE"
        data["spec_mode"] = False
        data["spec_config"] = {}
        data["enabled"] = False
        add_history(data, old_state, "IDLE", actor="cli:spec-stop")

    update_pipeline(path, do_stop)
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
        now = datetime.now(JST)
        target = sc["paused_from"]

        if target == "SPEC_REVIEW":
            _reset_review_requests(sc)
            sc["current_reviews"] = {}

        for entry in sc.get("review_requests", {}).values():
            if entry.get("status") == "pending":
                entry["timeout_at"] = (
                    now + timedelta(seconds=SPEC_REVIEW_TIMEOUT_SEC)
                ).isoformat()

        if target == "ISSUE_SUGGESTION":
            for entry in sc.get("issue_suggestions", {}).values():
                if isinstance(entry, dict) and entry.get("status") == "pending":
                    entry["timeout_at"] = (
                        now + timedelta(seconds=SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC)
                    ).isoformat()

        sc.setdefault("retry_counts", {})[target] = 0
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
    cycle = f"{sc.get('revise_count', 0)}/{sc.get('max_revise_cycles', '?')}"

    retry_parts = []
    for k, v in sc.get("retry_counts", {}).items():
        retry_parts.append(f"{k}={v}/{MAX_SPEC_RETRIES}")
    retries = ", ".join(retry_parts) if retry_parts else "none"

    print(f"DevBar [{state}] rev{rev} (cycle {cycle}, retries: {retries})")
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

    review_mode = data.get("review_mode", "full")
    min_valid = MIN_VALID_REVIEWS_BY_MODE.get(review_mode, 0)
    print(f"  min_valid: {min_valid} ({review_mode} mode)")
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
        ts = datetime.now(JST).strftime("%Y%m%dT%H%M%S")
        archive_name = f"{ts}_{args.reviewer}_{spec_name}_rev{current_rev}.yaml"
        archive_path = Path(pipelines_dir) / archive_name
        try:
            archive_path.write_text(raw_text, encoding="utf-8")
            archive_path.chmod(0o600)
            print(f"  archived: {archive_path}")
        except OSError as e:
            print(f"  warning: archive failed: {e}")


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
    }
    if not args.spec_command:
        raise SystemExit(
            "usage: devbar spec {start|stop|approve|continue|done|retry|resume|extend|status|review-submit}"
        )
    spec_cmds[args.spec_command](args)


def main():
    parser = argparse.ArgumentParser(
        prog="devbar",
        description="DevBar — Issue→設計→実装→レビュー→マージの開発パイプラインCLI",
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
    p.add_argument("--keep-ctx-batch", action="store_true", default=False, dest="keep_ctx_batch")
    p.add_argument("--keep-ctx-intra", action="store_true", default=False, dest="keep_ctx_intra")
    p.add_argument("--keep-ctx-all", action="store_true", default=False, dest="keep_ctx_all")

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

    # flag
    p = sub.add_parser("flag", help="人間（M）による P0/P1 差し込み（任意タイミング）")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--verdict", required=True, choices=VALID_FLAG_VERDICTS,
                   help="P0 (blocks progress) or P1 (informational)")
    p.add_argument("--summary", default="", help="フラグの説明")

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
    p = sub.add_parser("merge-summary", help="マージサマリーを #dev-bar に投稿してMの承認待ちへ")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # qrun
    p = sub.add_parser("qrun", help="キューから次のバッチを実行")
    p.add_argument("--queue", type=Path, help="キューファイルパス (default: devbar-queue.txt)")
    p.add_argument("--dry-run", action="store_true", help="実行せず内容のみ表示")

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

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "status": cmd_status, "init": cmd_init,
        "enable": cmd_enable, "disable": cmd_disable,
        "extend": cmd_extend, "start": cmd_start,
        "triage": cmd_triage, "transition": cmd_transition,
        "review": cmd_review, "flag": cmd_flag, "commit": cmd_commit,
        "cc-start": cmd_cc_start, "plan-done": cmd_plan_done,
        "design-revise": cmd_design_revise, "code-revise": cmd_code_revise,
        "review-mode": cmd_review_mode,
        "merge-summary": cmd_merge_summary,
        "qrun": cmd_qrun,
        "spec": cmd_spec,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()


# --- spec stop (injected) は下のparser登録で追加 ---
