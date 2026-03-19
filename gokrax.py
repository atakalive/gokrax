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
    p.add_argument("--keep-context", action="store_true", default=None, dest="keep_context",
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
    p.add_argument("--skip-test", action="store_true", default=None, dest="skip_test",
                   help="CODE_TEST フェーズをスキップし、直接 CODE_REVIEW に入る")
    p.add_argument("--no-skip-test", action="store_true", default=None, dest="no_skip_test")

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
