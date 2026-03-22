#!/usr/bin/env python3
"""gokrax — 開発パイプラインCLI

pipeline JSONの唯一の操作インターフェース。直接JSON編集禁止。
"""

import argparse
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    PIPELINES_DIR, ALLOWED_REVIEWERS, REVIEW_MODES,
    VALID_VERDICTS, VALID_FLAG_VERDICTS,
    WATCHDOG_LOOP_SCRIPT, WATCHDOG_LOOP_PIDFILE,
    WATCHDOG_LOOP_CRON_MARKER, WATCHDOG_LOOP_CRON_ENTRY,
)
from pipeline_io import load_pipeline
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
    if not _is_loop_running():
        subprocess.Popen(
            ["nohup", "bash", str(WATCHDOG_LOOP_SCRIPT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    # crontab は loop の状態に関係なく常に保証する
    _ensure_cron_entry()


def _stop_loop():
    """watchdog-loop.sh プロセスを SIGTERM で停止する。crontab やファイルの後処理は呼び出し側で行う。"""
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
from commands.dev import (  # noqa: F401 — re-export for backwards compatibility
    cmd_status, cmd_init, cmd_enable, cmd_disable, cmd_extend,
    cmd_triage, cmd_start, cmd_transition, cmd_reset,
    cmd_review, cmd_dispute, cmd_flag, cmd_commit,
    cmd_cc_start, cmd_plan_done, cmd_design_revise, cmd_code_revise,
    cmd_review_mode, cmd_merge_summary,
    cmd_qrun, cmd_qstatus, cmd_qadd, cmd_qdel, cmd_qedit,
    get_status_text, get_qstatus_text, _get_running_info,
    _reset_to_idle,
)


def main():
    parser = argparse.ArgumentParser(
        prog="gokrax",
        description="gokrax — development pipeline CLI: issue → design → implement → review → merge",
    )
    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", help="show status of all projects (state, batch, review progress)")

    # init
    p = sub.add_parser("init", help="initialize pipeline for a new project")
    p.add_argument("--pj", "--project", dest="project", required=True, help="project name")
    p.add_argument("--gitlab", help="GitLab path (default: atakalive/<project>)")
    p.add_argument("--repo-path", dest="repo_path", help="local repository path")
    p.add_argument("--implementer", default="kaneko", help="implementer agent (default: kaneko)")

    # enable / disable
    p = sub.add_parser("enable", help="enable watchdog (automatic transitions and nudges)")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p = sub.add_parser("disable", help="disable watchdog (manual-only mode)")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # extend
    p = sub.add_parser("extend", help="extend timeout for DESIGN_PLAN, IMPLEMENTATION, etc.")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--by", type=int, default=600, help="seconds to add (default: 600)")

    # start
    p = sub.add_parser("start", help="start batch: triage + transition to DESIGN_PLAN + enable watchdog")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, nargs="+",
                   help="issue numbers (omit to fetch all open issues from GitLab)")
    p.add_argument("--mode", choices=["full", "standard", "lite", "min", "skip"],
                   help="review mode (omit to keep current setting)")
    p.add_argument("--keep-context", action="store_true", default=None, dest="keep_context",
                   help="(backward compat) alias for --keep-ctx-all")
    p.add_argument("--keep-ctx-batch", action="store_true", default=None, dest="keep_ctx_batch")
    p.add_argument("--keep-ctx-intra", action="store_true", default=None, dest="keep_ctx_intra")
    p.add_argument("--keep-ctx-all", action="store_true", default=None, dest="keep_ctx_all")
    p.add_argument("--keep-ctx-none", action="store_true", default=None, dest="keep_ctx_none")
    p.add_argument("--p2-fix", action="store_true", default=None, dest="p2_fix")
    p.add_argument("--comment", default=None, help="note for the entire batch (injected into prompts)")
    p.add_argument("--skip-cc-plan", action="store_true", default=None, dest="skip_cc_plan",
                   help="skip CC plan phase, go directly to implementation")
    p.add_argument("--no-skip-cc-plan", action="store_true", default=None, dest="no_skip_cc_plan")
    p.add_argument("--skip-test", action="store_true", default=None, dest="skip_test",
                   help="skip CODE_TEST phase, go directly to CODE_REVIEW")
    p.add_argument("--no-skip-test", action="store_true", default=None, dest="no_skip_test")
    p.add_argument("--skip-assess", action="store_true", default=None, dest="skip_assess",
                   help="skip ASSESSMENT phase, go directly to IMPLEMENTATION")
    p.add_argument("--no-skip-assess", action="store_true", default=None, dest="no_skip_assess")

    # transition
    p = sub.add_parser("transition", help="manually trigger a state transition (normally done by watchdog)")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--to", required=True, help="target state")
    p.add_argument("--actor", default="cli", help="transition actor (default: cli)")
    p.add_argument("--force", action="store_true", default=False,
                   help="skip transition validation")
    p.add_argument("--resume", action="store_true", default=False,
                   help="skip validation and prefix notifications with (resumed)")
    p.add_argument("--dry-run", action="store_true", default=False, dest="dry_run",
                   help="apply transition only, skip notifications (for testing)")

    # reset
    p = sub.add_parser("reset", help="reset all non-IDLE projects to IDLE")
    p.add_argument("--dry-run", action="store_true", help="show targets without making changes")
    p.add_argument("--force", action="store_true", help="skip confirmation prompt")

    # review
    p = sub.add_parser("review", help="record review result (idempotent: duplicate from same reviewer is skipped)")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--reviewer", required=True, choices=ALLOWED_REVIEWERS)
    p.add_argument("--verdict", required=True, choices=VALID_VERDICTS,
                   help="verdict: APPROVE / P0 / P1 / P2 / REJECT")
    p.add_argument("--summary", default="", help="review summary")
    p.add_argument("--force", action="store_true", default=False,
                   help="overwrite existing review")
    p.add_argument("--round", type=int, default=None, help="review round number (auto-filled)")

    # flag
    p = sub.add_parser("flag", help="human (M) P0/P1/P2 injection at any time")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--verdict", required=True, choices=VALID_FLAG_VERDICTS,
                   help="verdict: P0 / P1 / P2")
    p.add_argument("--summary", default="", help="flag description")

    # dispute
    p = sub.add_parser("dispute", help="dispute a P0/P1 verdict during REVISE")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--reviewer", required=True, choices=ALLOWED_REVIEWERS)
    p.add_argument("--reason", required=True, help="reason for the dispute")

    # commit
    p = sub.add_parser("commit", help="record commit hash for completed implementation")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="issue numbers (multiple allowed)")
    p.add_argument("--hash", required=True, help="git commit hash")
    p.add_argument("--session-id", default=None, help="CC session ID")

    # cc-start
    p = sub.add_parser("cc-start", help="record CC (Claude Code) process PID on start")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--pid", type=int, required=True, help="CC process PID")

    # plan-done
    p = sub.add_parser("plan-done", help="mark design plan as done: set design_ready flag on issues")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="issue numbers (multiple allowed)")

    # design-revise
    p = sub.add_parser("design-revise", help="mark design revision as done: set design_revised flag")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="issue numbers (multiple allowed)")
    p.add_argument("--comment", default=None, help="comment to post as GitLab issue note (optional)")

    # code-revise
    p = sub.add_parser("code-revise", help="mark code revision as done: record commit hash + set code_revised flag")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--issue", type=int, nargs="+", required=True, help="issue numbers (multiple allowed)")
    p.add_argument("--hash", required=True, help="git commit hash")
    p.add_argument("--comment", default=None, help="comment to post as GitLab issue note (optional)")

    # review-mode
    p = sub.add_parser("review-mode", help="change review mode")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--mode", required=True, choices=list(REVIEW_MODES.keys()),
                   help="review mode (choices shown in --help)")

    # merge-summary
    p = sub.add_parser("merge-summary", help="post merge summary to #gokrax and await M approval")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # qrun
    p = sub.add_parser("qrun", help="run next batch from queue")
    p.add_argument("--queue", type=Path, help="queue file path (default: gokrax-queue.txt)")
    p.add_argument("--dry-run", action="store_true", help="show entry without executing")

    # qstatus
    p = sub.add_parser("qstatus", help="show active queue entries")
    p.add_argument("--queue", type=Path, help="queue file path")

    # qadd
    p = sub.add_parser("qadd", help="add one or more entries to the queue")
    p.add_argument("entry", nargs="*", help="entry to add (e.g. BeamShifter 33,34 lite no-automerge comment=note)")
    p.add_argument("--file", type=Path, dest="file", help="file containing entries (one per line)")
    p.add_argument("--stdin", action="store_true", dest="from_stdin", help="read entries from stdin")
    p.add_argument("--queue", type=Path, help="queue file path")

    # qdel
    p = sub.add_parser("qdel", help="delete a queue entry")
    p.add_argument("target", help="target to delete (index number or 'last')")
    p.add_argument("--queue", type=Path, help="queue file path")

    # qedit
    p = sub.add_parser("qedit", help="replace a queue entry")
    p.add_argument("target", help="target to replace (index number or 'last')")
    p.add_argument("entry", nargs="+", help="new entry (e.g. gokrax 105 full automerge)")
    p.add_argument("--queue", type=Path, help="queue file path")

    # spec
    spec_parser = sub.add_parser("spec", help="Spec mode commands")
    spec_sub = spec_parser.add_subparsers(dest="spec_command")

    # spec start
    p = spec_sub.add_parser("start", help="start spec mode pipeline")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--spec", required=True, help="path to spec file (repo-relative)")
    p.add_argument("--implementer", required=True, help="revision agent ID")
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
                   help="initial current_rev value (default: 1)")

    # spec stop
    p = spec_sub.add_parser("stop", help="force-stop spec mode and return to IDLE")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # spec approve
    p = spec_sub.add_parser("approve", help="manually transition to SPEC_APPROVED")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--force", action="store_true", default=False)

    # spec continue
    p = spec_sub.add_parser("continue", help="proceed from SPEC_APPROVED to ISSUE_SUGGESTION")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # spec done
    p = spec_sub.add_parser("done", help="transition from SPEC_DONE to IDLE")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # spec retry
    p = spec_sub.add_parser("retry", help="retry from FAILED back to SPEC_REVIEW")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # spec resume
    p = spec_sub.add_parser("resume", help="resume from PAUSED to previous state")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # spec extend
    p = spec_sub.add_parser("extend", help="extend from STALLED back to SPEC_REVISE (increase max cycles)")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--cycles", type=int, default=2, help="additional cycles to add (default: 2)")

    # spec status
    p = spec_sub.add_parser("status", help="show spec mode status")
    p.add_argument("--pj", "--project", dest="project", required=True)

    # spec review-submit
    p = spec_sub.add_parser("review-submit", help="submit review result from YAML file")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--file", required=True)

    # spec revise-submit
    p = spec_sub.add_parser("revise-submit", help="submit SPEC_REVISE completion report from file")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--file", required=True)

    # spec self-review-submit
    p = spec_sub.add_parser("self-review-submit", help="submit self-review result from file")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--file", required=True)

    # spec issue-submit
    p = spec_sub.add_parser("issue-submit", help="submit ISSUE_PLAN completion report from file")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--file", required=True)

    # spec queue-submit
    p = spec_sub.add_parser("queue-submit", help="submit QUEUE_PLAN completion report from file")
    p.add_argument("--pj", "--project", dest="project", required=True)
    p.add_argument("--file", required=True)

    # spec suggestion-submit
    p = spec_sub.add_parser("suggestion-submit", help="submit reviewer suggestion for ISSUE_SUGGESTION from file")
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
        "transition": cmd_transition, "reset": cmd_reset,
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
