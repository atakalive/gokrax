#!/usr/bin/env python3
"""devbar — 開発パイプラインCLI

pipeline JSONの唯一の操作インターフェース。直接JSON編集禁止。
"""

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    PIPELINES_DIR, GLAB_BIN, LOG_FILE,
    VALID_STATES, VALID_TRANSITIONS, MAX_BATCH, TRIAGE_ALLOWED_STATES,
    VALID_VERDICTS, GLAB_TIMEOUT, ALLOWED_REVIEWERS, REVIEW_MODES,
)
from pipeline_io import (
    load_pipeline, save_pipeline, update_pipeline,
    add_history, now_iso, get_path, find_issue,
)
from watchdog import get_notification_for_state
from notify import notify_implementer, notify_reviewers, notify_discord, send_to_agent


# === Commands ===

def cmd_status(args):
    """全PJの状態を表示"""
    PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(PIPELINES_DIR.glob("*.json"))
    if not files:
        print("No pipelines found.")
        return

    for path in files:
        data = load_pipeline(path)
        pj = data.get("project", path.stem)
        state = data.get("state", "IDLE")
        enabled = "ON" if data.get("enabled") else "OFF"
        batch = data.get("batch", [])
        review_mode = data.get("review_mode", "standard")
        issues = ", ".join(f"#{i['issue']}" for i in batch) if batch else "none"
        mode_config = REVIEW_MODES.get(review_mode, REVIEW_MODES["standard"])
        reviewers_str = ", ".join(f'"{r}"' for r in mode_config["members"])
        print(f"[{enabled}] {pj}: {state}  issues=[{issues}]  ReviewerSize={review_mode}  Reviewers=[{reviewers_str}]")

        # Show per-issue review progress for review states
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
                print(f"  #{item['issue']}: {done}/{min_rev} reviews{verdict_str}")


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
    print(f"{args.project}: watchdog enabled")


def cmd_disable(args):
    path = get_path(args.project)

    def do_disable(data):
        data["enabled"] = False

    update_pipeline(path, do_disable)
    print(f"{args.project}: watchdog disabled")


def cmd_extend(args):
    """タイムアウト延長申請。

    対象状態: DESIGN_PLAN, DESIGN_REVISE, IMPLEMENTATION, CODE_REVISE
    """
    from watchdog import EXTENDABLE_STATES

    path = get_path(args.project)

    result = {}
    def do_extend(data):
        state = data.get("state", "IDLE")
        if state not in EXTENDABLE_STATES:
            raise SystemExit(
                f"延長不可: 現在の状態 {state} は対象外です "
                f"(対象: {', '.join(sorted(EXTENDABLE_STATES))})"
            )
        data["timeout_extension"] = data.get("timeout_extension", 0) + args.by
        result["state"] = state
        result["implementer"] = data.get("implementer", "kaneko")
        result["total"] = data["timeout_extension"]

    update_pipeline(path, do_extend)

    notify_discord(
        f"[{args.project}] {result['implementer']} がタイムアウトを{args.by}秒延長 "
        f"({result['state']}, 累計+{result['total']}秒)"
    )

    print(f"{args.project}: タイムアウト延長 +{args.by}秒 (累計+{result['total']}秒)")


def _fetch_open_issues(gitlab: str) -> list[int]:
    """glab issue list でopen issue番号のリストを取得。"""
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
        return [issue["iid"] for issue in issues if issue.get("state") == "opened"]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
        print(f"Failed to fetch open issues: {e}", file=sys.stderr)
        return []


def cmd_triage(args):
    """Issueをバッチに投入（複数指定可）"""
    path = get_path(args.project)
    titles = args.title + [""] * (len(args.issue) - len(args.title))

    def do_triage(data):
        state = data.get("state", "IDLE")
        if state not in TRIAGE_ALLOWED_STATES:
            raise SystemExit(f"Cannot add issues in state {state} (allowed: {TRIAGE_ALLOWED_STATES})")
        batch = data.get("batch", [])
        if len(batch) + len(args.issue) > MAX_BATCH:
            raise SystemExit(
                f"Batch overflow: {len(batch)} existing + {len(args.issue)} new > {MAX_BATCH}"
            )
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
    else:
        # GitLab APIでopen issue全件取得
        gitlab = data.get("gitlab", f"atakalive/{args.project}")
        issue_nums = _fetch_open_issues(gitlab)
        if not issue_nums:
            raise SystemExit(f"No open issues found in {gitlab}")

    # 3. triage実行（既存のcmd_triageロジック流用）
    import argparse
    triage_args = argparse.Namespace(
        project=args.project,
        issue=issue_nums,
        title=[]  # タイトルは空（GitLab APIからは取得しない）
    )
    cmd_triage(triage_args)

    # 4. DESIGN_PLANに遷移
    transition_args = argparse.Namespace(
        project=args.project,
        to="DESIGN_PLAN",
        actor="cli",
        force=False,
        resume=False,
    )
    cmd_transition(transition_args)

    # 5. watchdog有効化
    def do_enable(data):
        data["enabled"] = True
    update_pipeline(path, do_enable)

    # 6. 完了メッセージ
    issues_str = ", ".join(f"#{n}" for n in issue_nums)
    print(f"{args.project}: started with issues [{issues_str}] → DESIGN_PLAN (watchdog enabled)")


def cmd_transition(args):
    """状態遷移（バリデーション付き）"""
    import config as _cfg
    if getattr(args, "dry_run", False):
        _cfg.DRY_RUN = True
    path = get_path(args.project)
    resume = getattr(args, "resume", False)

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

    data = update_pipeline(path, do_transition)
    suffix = " [RESUME]" if resume else (" [FORCED]" if args.force else "")
    print(f"{args.project}: {args.to}{suffix}")

    pj = data.get("project", args.project)
    batch = data.get("batch", [])
    gitlab = data.get("gitlab", f"atakalive/{pj}")
    implementer = data.get("implementer", "kaneko")
    repo_path = data.get("repo_path", "")
    review_mode = data.get("review_mode", "standard")

    notif = get_notification_for_state(args.to, pj, batch, gitlab, implementer)
    prefix = "（再開）" if resume else ""
    if notif.impl_msg:
        notify_implementer(implementer, f"[devbar] {pj}: {prefix}{notif.impl_msg}")
    if notif.send_review:
        notify_reviewers(pj, args.to, batch, gitlab, repo_path=repo_path,
                        review_mode=review_mode)

    # Discord 通知
    history = data.get("history", [])
    current = history[-1].get("from", "?") if history else "?"
    actor = args.actor or "cli"
    notify_discord(f"[{pj}] {prefix}{current} → {args.to} (by {actor})")


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

    def do_review(data):
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
        # 冪等性: 同じレビュアーが既にレビュー済みならスキップ
        if args.reviewer in issue.get(key, {}):
            print(f"#{args.issue}: already reviewed by {args.reviewer}, skipping")
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

    state = data.get("state", "IDLE")
    print(f"{args.project}: #{args.issue} review by {args.reviewer} = {args.verdict}")

    # GitLab Issue note に自動投稿
    gitlab = data.get("gitlab", f"atakalive/{args.project}")
    phase = "設計" if "DESIGN" in state else "コード"
    note_body = f"[{args.reviewer}] {args.verdict} ({phase}レビュー)\n\n{args.summary or ''}"
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


def cmd_revise(args):
    """revised フラグを設定"""
    path = get_path(args.project)

    if args.comment:
        data = load_pipeline(path)
        gitlab = data.get("gitlab", f"atakalive/{args.project}")
        if not _post_gitlab_note(gitlab, args.issue, args.comment):
            sys.exit(1)

    def do_revise(data):
        state = data.get("state", "IDLE")
        if state == "DESIGN_REVISE":
            flag = "design_revised"
        elif state == "CODE_REVISE":
            flag = "code_revised"
        else:
            raise SystemExit(f"Not in revise state: {state}")
        issue = find_issue(data.get("batch", []), args.issue)
        if not issue:
            raise SystemExit(f"Issue #{args.issue} not in batch")
        issue[flag] = True

    update_pipeline(path, do_revise)
    print(f"{args.project}: #{args.issue} marked as revised")


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
    from config import DISCORD_CHANNEL
    from notify import post_discord
    from watchdog import _format_merge_summary

    path = get_path(args.project)
    data = load_pipeline(path)
    state = data.get("state", "IDLE")
    if state != "CODE_APPROVED":
        raise SystemExit(f"Cannot send merge summary in state {state} (expected CODE_APPROVED)")

    batch = data.get("batch", [])
    project = data.get("project", args.project)
    content = _format_merge_summary(project, batch)

    message_id = post_discord(DISCORD_CHANNEL, content)
    if not message_id:
        raise SystemExit("Discord 投稿に失敗しました")

    def do_update(data):
        data["summary_message_id"] = message_id
        add_history(data, data["state"], "MERGE_SUMMARY_SENT", "cli")
        data["state"] = "MERGE_SUMMARY_SENT"

    update_pipeline(path, do_update)
    print(f"{args.project}: merge summary sent (message_id={message_id})")


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
    p.add_argument("--project", required=True, help="プロジェクト名")
    p.add_argument("--gitlab", help="GitLabパス (default: atakalive/<project>)")
    p.add_argument("--repo-path", dest="repo_path", help="ローカルリポジトリのパス")
    p.add_argument("--implementer", default="kaneko", help="実装担当エージェント (default: kaneko)")

    # enable / disable
    p = sub.add_parser("enable", help="watchdogによる自動遷移・催促を有効化")
    p.add_argument("--project", required=True)
    p = sub.add_parser("disable", help="watchdogを無効化（手動操作のみ）")
    p.add_argument("--project", required=True)

    # extend
    p = sub.add_parser("extend", help="DESIGN_PLAN/IMPL等のタイムアウトを延長")
    p.add_argument("--project", required=True)
    p.add_argument("--by", type=int, default=600, help="延長秒数 (default: 600)")

    # start
    p = sub.add_parser("start", help="バッチ開始: triage→DESIGN_PLAN遷移→watchdog有効化を一括実行")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, nargs="+",
                   help="Issue番号（省略時はGitLabのopen issue全件を自動取得）")

    # triage
    p = sub.add_parser("triage", help="指定Issueをバッチに投入")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="Issue番号（複数指定可）")
    p.add_argument("--title", action="append", default=[], help="タイトル（--issue と同数、省略時は空文字）")

    # transition
    p = sub.add_parser("transition", help="手動で状態遷移（通常はwatchdogが自動実行）")
    p.add_argument("--project", required=True)
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
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--reviewer", required=True, choices=ALLOWED_REVIEWERS)
    p.add_argument("--verdict", required=True, choices=VALID_VERDICTS,
                   help="APPROVE/P0/P1/REJECT")
    p.add_argument("--summary", default="", help="レビューサマリー")

    # commit
    p = sub.add_parser("commit", help="実装完了: commitハッシュをバッチに記録")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="Issue番号（複数指定可）")
    p.add_argument("--hash", required=True, help="gitコミットハッシュ")
    p.add_argument("--session-id", default=None, help="CC セッションID")

    # cc-start
    p = sub.add_parser("cc-start", help="CC (Claude Code) 実行開始時にPIDを記録")
    p.add_argument("--project", required=True)
    p.add_argument("--pid", type=int, required=True, help="CCプロセスのPID")

    # plan-done
    p = sub.add_parser("plan-done", help="設計確認完了: 対象Issueにdesign_readyフラグを設定")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="Issue番号（複数指定可）")

    # revise
    p = sub.add_parser("revise", help="修正完了: レビュー指摘への修正が終わったことを記録")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--comment", default=None, help="GitLab issue noteに投稿するコメント（省略可）")

    # review-mode
    p = sub.add_parser("review-mode", help="レビューモード変更 (full=4人/standard=3人/lite=2人/skip=なし)")
    p.add_argument("--project", required=True)
    p.add_argument("--mode", required=True, choices=list(REVIEW_MODES.keys()),
                   help="full/standard/lite/skip")

    # merge-summary
    p = sub.add_parser("merge-summary", help="マージサマリーを #dev-bar に投稿してMの承認待ちへ")
    p.add_argument("--project", required=True)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "status": cmd_status, "init": cmd_init,
        "enable": cmd_enable, "disable": cmd_disable,
        "extend": cmd_extend, "start": cmd_start,
        "triage": cmd_triage, "transition": cmd_transition,
        "review": cmd_review, "commit": cmd_commit,
        "cc-start": cmd_cc_start, "plan-done": cmd_plan_done,
        "revise": cmd_revise, "review-mode": cmd_review_mode,
        "merge-summary": cmd_merge_summary,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
