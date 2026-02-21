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
    VALID_VERDICTS, GLAB_TIMEOUT, ALLOWED_REVIEWERS, REVIEWERS,
    DESIGN_REVIEWERS, CODE_REVIEWERS, DESIGN_MIN_REVIEWS, CODE_MIN_REVIEWS,
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
        issues = ", ".join(f"#{i['issue']}" for i in batch) if batch else "none"
        reviewers = DESIGN_REVIEWERS if "DESIGN" in state else CODE_REVIEWERS
        reviewers_str = ", ".join(f'"{r}"' for r in reviewers)
        print(f"[{enabled}] {pj}: {state}  issues=[{issues}]  Reviewers=[{reviewers_str}]")

        # Show per-issue review progress for review states
        if state in ("DESIGN_REVIEW", "CODE_REVIEW") and batch:
            review_key = "design_reviews" if state == "DESIGN_REVIEW" else "code_reviews"
            min_rev = DESIGN_MIN_REVIEWS if state == "DESIGN_REVIEW" else CODE_MIN_REVIEWS
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
    _maybe_reset_reviewers(args.project)
    print(f"{args.project}: watchdog enabled")


def cmd_disable(args):
    path = get_path(args.project)

    def do_disable(data):
        data["enabled"] = False

    update_pipeline(path, do_disable)
    print(f"{args.project}: watchdog disabled")


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
    if args.force or getattr(args, "resume", False):
        _maybe_reset_reviewers(args.project)
    suffix = " [RESUME]" if resume else (" [FORCED]" if args.force else "")
    print(f"{args.project}: {args.to}{suffix}")

    pj = data.get("project", args.project)
    batch = data.get("batch", [])
    gitlab = data.get("gitlab", f"atakalive/{pj}")
    implementer = data.get("implementer", "kaneko")
    repo_path = data.get("repo_path", "")

    notif = get_notification_for_state(args.to, pj, batch, gitlab, implementer)
    prefix = "（再開）" if resume else ""
    if notif.impl_msg:
        notify_implementer(implementer, f"[devbar] {pj}: {prefix}{notif.impl_msg}")
    if notif.send_review:
        notify_reviewers(pj, args.to, batch, gitlab, repo_path=repo_path)

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


def _maybe_reset_reviewers(project: str) -> None:
    """PJ 変更時にレビュアー全員へ /new を送信してセッションリセット。"""
    import config as _cfg
    import json as _json

    state_path = _cfg.DEVBAR_STATE_PATH
    try:
        saved = _json.loads(state_path.read_text())
    except FileNotFoundError:
        saved = {}
    except Exception as e:
        print(f"WARNING: devbar-state.json read error: {e}", file=sys.stderr)
        saved = {}

    last_project = saved.get("last_project")
    if last_project is not None and last_project != project:
        reviewers = list(dict.fromkeys(_cfg.DESIGN_REVIEWERS + _cfg.CODE_REVIEWERS))
        for r in reviewers:
            if not send_to_agent(r, "/new"):
                print(f"WARNING: reviewer session reset failed: {r}", file=sys.stderr)

    saved["last_project"] = project
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(_json.dumps(saved))
    except Exception as e:
        print(f"WARNING: devbar-state.json write error: {e}", file=sys.stderr)


def _post_gitlab_note(gitlab: str, issue_num: int, body: str) -> bool:
    """glab issue note を投稿。失敗時は2回リトライ（間隔2秒）。"""
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
            time.sleep(2)
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


def cmd_merge_summary(args):
    """マージサマリーを #dev-bar に投稿し、MERGE_SUMMARY_SENT に遷移"""
    from config import MERGE_SUMMARY_FOOTER, DISCORD_CHANNEL
    from notify import post_discord

    path = get_path(args.project)
    data = load_pipeline(path)
    state = data.get("state", "IDLE")
    if state != "CODE_APPROVED":
        raise SystemExit(f"Cannot send merge summary in state {state} (expected CODE_APPROVED)")

    batch = data.get("batch", [])
    project = data.get("project", args.project)
    lines = [f"**[{project}] マージサマリー**\n"]
    for item in batch:
        num = item["issue"]
        title = item.get("title", "")
        commit = item.get("commit", "?")
        lines.append(f"- #{num}: {title} (`{commit}`)")
    lines.append(MERGE_SUMMARY_FOOTER)
    content = "\n".join(lines)

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
    parser = argparse.ArgumentParser(prog="devbar", description="開発パイプラインCLI")
    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", help="全PJ状態表示")

    # init
    p = sub.add_parser("init", help="新PJ初期化")
    p.add_argument("--project", required=True)
    p.add_argument("--gitlab", help="GitLab path (default: atakalive/<project>)")
    p.add_argument("--repo-path", dest="repo_path", help="ローカルリポジトリパス")
    p.add_argument("--implementer", default="kaneko")

    # enable / disable
    p = sub.add_parser("enable", help="watchdog有効化")
    p.add_argument("--project", required=True)
    p = sub.add_parser("disable", help="watchdog無効化")
    p.add_argument("--project", required=True)

    # triage
    p = sub.add_parser("triage", help="Issueをバッチに投入")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="Issue番号（複数指定可）")
    p.add_argument("--title", action="append", default=[], help="タイトル（--issue と同数、省略時は空文字）")

    # transition
    p = sub.add_parser("transition", help="状態遷移")
    p.add_argument("--project", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--actor", default="cli")
    p.add_argument("--force", action="store_true", default=False, help="遷移バリデーションをスキップ（BLOCKED遷移等）")
    p.add_argument("--resume", action="store_true", default=False, help="バリデーションスキップ + 「（再開）」プレフィックス付き通知")
    p.add_argument("--dry-run", action="store_true", default=False, dest="dry_run",
                   help="通知をスキップ（テスト用）")

    # review
    p = sub.add_parser("review", help="レビュー結果記録")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--reviewer", required=True, choices=ALLOWED_REVIEWERS)
    p.add_argument("--verdict", required=True, choices=VALID_VERDICTS)
    p.add_argument("--summary", default="")

    # commit
    p = sub.add_parser("commit", help="commit hash記録")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="Issue番号（複数指定可）")
    p.add_argument("--hash", required=True)
    p.add_argument("--session-id", default=None)

    # plan-done
    p = sub.add_parser("plan-done", help="設計完了フラグ設定")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="Issue番号（複数指定可）")

    # revise
    p = sub.add_parser("revise", help="revisedフラグ設定")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--comment", default=None, help="GitLab issue note（省略可）")

    # merge-summary
    p = sub.add_parser("merge-summary", help="マージサマリーを #dev-bar に投稿")
    p.add_argument("--project", required=True)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "status": cmd_status, "init": cmd_init,
        "enable": cmd_enable, "disable": cmd_disable,
        "triage": cmd_triage, "transition": cmd_transition,
        "review": cmd_review, "commit": cmd_commit,
        "plan-done": cmd_plan_done, "revise": cmd_revise,
        "merge-summary": cmd_merge_summary,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
