#!/usr/bin/env python3
"""devbar — 開発パイプラインCLI

pipeline JSONの唯一の操作インターフェース。直接JSON編集禁止。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    PIPELINES_DIR, GLAB_BIN,
    VALID_STATES, VALID_TRANSITIONS, MAX_BATCH, TRIAGE_ALLOWED_STATES,
    VALID_VERDICTS, GLAB_TIMEOUT,
)
from pipeline_io import (
    load_pipeline, save_pipeline, update_pipeline,
    add_history, now_iso, get_path, find_issue,
)


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
        print(f"[{enabled}] {pj}: {state}  issues=[{issues}]")


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


def cmd_triage(args):
    """Issueをバッチに投入"""
    path = get_path(args.project)

    def do_triage(data):
        state = data.get("state", "IDLE")
        if state not in TRIAGE_ALLOWED_STATES:
            raise SystemExit(f"Cannot add issues in state {state} (allowed: {TRIAGE_ALLOWED_STATES})")
        batch = data.get("batch", [])
        if len(batch) >= MAX_BATCH:
            raise SystemExit(f"Batch full ({MAX_BATCH})")
        if find_issue(batch, args.issue):
            raise SystemExit(f"Issue #{args.issue} already in batch")
        batch.append({
            "issue": args.issue,
            "title": args.title or "",
            "commit": None,
            "cc_session_id": None,
            "design_reviews": {},
            "code_reviews": {},
            "added_at": now_iso(),
        })
        data["batch"] = batch

    update_pipeline(path, do_triage)
    print(f"{args.project}: #{args.issue} added to batch")


def cmd_transition(args):
    """状態遷移（バリデーション付き）"""
    path = get_path(args.project)

    def do_transition(data):
        current = data.get("state", "IDLE")
        target = args.to
        if target not in VALID_STATES:
            raise SystemExit(f"Invalid state: {target}")
        allowed = VALID_TRANSITIONS.get(current, [])
        if target not in allowed:
            raise SystemExit(f"Invalid transition: {current} → {target} (allowed: {allowed})")
        add_history(data, current, target, args.actor or "cli")
        data["state"] = target
        if target == "IDLE":
            data["batch"] = []
            data["enabled"] = False

    update_pipeline(path, do_transition)
    print(f"{args.project}: {args.to}")


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

    data = update_pipeline(path, do_review)
    state = data.get("state", "IDLE")
    print(f"{args.project}: #{args.issue} review by {args.reviewer} = {args.verdict}")

    # GitLab Issue note に自動投稿
    gitlab = data.get("gitlab", f"atakalive/{args.project}")
    phase = "設計" if "DESIGN" in state else "コード"
    note_body = f"[{args.reviewer}] {args.verdict} ({phase}レビュー)\n\n{args.summary or ''}"
    try:
        import subprocess
        result = subprocess.run(
            [GLAB_BIN, "issue", "note", str(args.issue), "-m", note_body,
             "-R", gitlab],
            capture_output=True, text=True, timeout=GLAB_TIMEOUT,
        )
        if result.returncode == 0:
            print(f"  → GitLab issue note posted")
        else:
            print(f"  ⚠ GitLab note failed: {result.stderr.strip()}", file=sys.stderr)
    except Exception as e:
        print(f"  ⚠ GitLab note error: {e}", file=sys.stderr)


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


def main():
    parser = argparse.ArgumentParser(prog="devbar", description="開発パイプラインCLI")
    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", help="全PJ状態表示")

    # init
    p = sub.add_parser("init", help="新PJ初期化")
    p.add_argument("--project", required=True)
    p.add_argument("--gitlab", help="GitLab path (default: atakalive/<project>)")
    p.add_argument("--implementer", default="kaneko")

    # enable / disable
    p = sub.add_parser("enable", help="watchdog有効化")
    p.add_argument("--project", required=True)
    p = sub.add_parser("disable", help="watchdog無効化")
    p.add_argument("--project", required=True)

    # triage
    p = sub.add_parser("triage", help="Issueをバッチに投入")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--title", default="")

    # transition
    p = sub.add_parser("transition", help="状態遷移")
    p.add_argument("--project", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--actor", default="cli")

    # review
    p = sub.add_parser("review", help="レビュー結果記録")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--reviewer", required=True)
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
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
