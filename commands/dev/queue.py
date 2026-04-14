import argparse
import sys
from pathlib import Path

from pipeline_io import (
    update_pipeline,
    add_history, get_path,
)

from commands.dev.helpers import _reset_to_idle


def cmd_qrun(args):
    """キューから次のバッチを実行: pop → cmd_start → オプション保存"""
    from task_queue import pop_next_queue_entry, restore_queue_entry, peek_queue, save_queue_options_to_pipeline, rollback_queue_mode, QueueSkipError
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
            if "automerge" in e.get("_explicit_keys", set()) and not e.get("automerge", False):
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
            if e.get("skip_design"):
                opts.append("skip-design")
            if e.get("no_cc"):
                opts.append("no-cc")
            if e.get("exclude_high_risk"):
                opts.append("exclude-high-risk")
            if e.get("exclude_any_risk"):
                opts.append("exclude-any-risk")
            if e.get("allow_closed"):
                opts.append("allow-closed")
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
        skip_design=entry.get("skip_design", False),
        no_cc=entry.get("no_cc", False),
        exclude_high_risk=entry.get("exclude_high_risk", False),
        exclude_any_risk=entry.get("exclude_any_risk", False),
        allow_closed=entry.get("allow_closed", False),
    )

    # queue_mode を先に設定（cmd_start 内の遷移通知で [Queue] prefix を使うため）
    path = get_path(project)
    def _set_queue_mode_early(data):
        data["queue_mode"] = True
    update_pipeline(path, _set_queue_mode_early)

    def _rollback_pipeline() -> None:
        """cmd_start 成功後の例外で pipeline を安全に戻す。"""
        def _do_rollback(data: dict) -> None:
            if data.get("state") != "IDLE":
                add_history(data, data.get("state", "IDLE"), "IDLE", "cli:qrun-rollback")
            _reset_to_idle(data)
            data["state"] = "IDLE"
        try:
            update_pipeline(path, _do_rollback)
        except Exception:
            pass  # ロールバック失敗は握りつぶす
        from gokrax import _any_pj_enabled, _stop_loop
        if not _any_pj_enabled():
            _stop_loop()

    # cmd_start 実行 (エラー時は復元 + queue_mode ロールバック)
    try:
        from commands.dev.lifecycle import cmd_start
        cmd_start(start_args)
    except QueueSkipError as e:
        # 永続的エラー: エントリを復元せずスキップ。
        # pop_next_queue_entry が付与した "# done: " prefix がそのまま残り、
        # 次回の qrun では次のエントリが処理される。
        # cleanup してから re-raise → main() で exit 75 に変換。
        rollback_queue_mode(path)
        _rollback_pipeline()
        print(f"[qrun] Skipped {project}: {e}", file=sys.stderr)
        raise
    except (SystemExit, Exception) as e:
        # 一時的エラー: エントリを復元。
        # NOTE: SystemExit は BaseException のサブクラスであり Exception ではない。
        # ここでは一時的エラー（状態不整合、ネットワーク障害等）を想定して
        # 両方を明示的にキャッチし、エントリを復元して再試行可能にしている。
        restore_queue_entry(queue_path, entry["original_line"])
        rollback_queue_mode(path)
        _rollback_pipeline()
        print(f"[qrun] Failed to start {project}: {e}", file=sys.stderr)
        raise

    # 成功: automerge/cc_model/comment をパイプラインに保存
    try:
        def _save_queue_options(data: dict) -> None:
            save_queue_options_to_pipeline(data, entry)

        update_pipeline(path, _save_queue_options)
    except BaseException:
        _rollback_pipeline()
        raise

    automerge_flag = entry.get("automerge", False)
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
                "automerge": data.get("automerge", False),
                "p2_fix": data.get("p2_fix", False),
                "cc_plan_model": data.get("cc_plan_model"),
                "cc_impl_model": data.get("cc_impl_model"),
                "keep_ctx_batch": data.get("keep_ctx_batch", False),
                "keep_ctx_intra": data.get("keep_ctx_intra", False),
                "skip_cc_plan": data.get("skip_cc_plan", False),
                "skip_test": data.get("skip_test", False),
                "skip_assess": data.get("skip_assess", False),
                "skip_design": data.get("skip_design", False),
                "no_cc": data.get("no_cc", False),
                "exclude_high_risk": data.get("exclude_high_risk", False),
                "exclude_any_risk": data.get("exclude_any_risk", False),
                "allow_closed": data.get("allow_closed", False),
                "comment": data.get("comment"),
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
        if not running.get("automerge", False):
            parts.append("no-automerge")
        if running.get("p2_fix"):
            parts.append("p2-fix")
        if running.get("cc_plan_model"):
            parts.append(f"plan={running['cc_plan_model']}")
        if running.get("cc_impl_model"):
            parts.append(f"impl={running['cc_impl_model']}")
        if running.get("keep_ctx_batch") and running.get("keep_ctx_intra"):
            parts.append("keep-ctx-all")
        elif running.get("keep_ctx_batch"):
            parts.append("keep-ctx-batch")
        elif running.get("keep_ctx_intra"):
            parts.append("keep-ctx-intra")
        if running.get("skip_cc_plan"):
            parts.append("skip-cc-plan")
        if running.get("skip_test"):
            parts.append("skip-test")
        if running.get("skip_assess"):
            parts.append("skip-assess")
        if running.get("skip_design"):
            parts.append("skip-design")
        if running.get("no_cc"):
            parts.append("no-cc")
        if running.get("exclude_high_risk"):
            parts.append("exclude-high-risk")
        if running.get("exclude_any_risk"):
            parts.append("exclude-any-risk")
        if running.get("allow_closed"):
            parts.append("allow-closed")
        lines.append(f"[*] {' '.join(parts)}")
    for e in entries:
        idx = e.get("index", 0)
        parts = [e["project"], e["issues"]]
        if e.get("mode"):
            parts.append(e["mode"])
        if "automerge" in e.get("_explicit_keys", set()) and not e.get("automerge", False):
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
        if e.get("skip_design"):
            parts.append("skip-design")
        if e.get("no_cc"):
            parts.append("no-cc")
        if e.get("exclude_high_risk"):
            parts.append("exclude-high-risk")
        if e.get("exclude_any_risk"):
            parts.append("exclude-any-risk")
        if e.get("allow_closed"):
            parts.append("allow-closed")
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
            raise SystemExit("--file and positional args/--stdin are mutually exclusive")
        lines = args.file.read_text().splitlines()
    elif getattr(args, "from_stdin", False):
        if args.entry:
            raise SystemExit("--stdin and positional args are mutually exclusive")
        lines = sys.stdin.read().splitlines()
    elif args.entry:
        lines = [" ".join(args.entry)]
    else:
        raise SystemExit("Specify entries to add (positional args or --file or --stdin)")

    # 空行・コメント行を除外
    lines = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]

    if not lines:
        raise SystemExit("No entries to add")

    # 全行をバリデーション（1行でもエラーなら全体を中止）
    for i, line in enumerate(lines, 1):
        try:
            parse_queue_line(line)
        except ValueError as e:
            raise SystemExit(f"line {i}: {e}")

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
