import argparse
import json
import subprocess
import sys

import os

from config import (
    GLAB_BIN,
    VALID_STATES, VALID_TRANSITIONS, MAX_BATCH,
    GLAB_TIMEOUT, REVIEWERS, REVIEW_MODES, LOCAL_TZ,
    WATCHDOG_LOOP_PIDFILE, WATCHDOG_LOOP_LOCKFILE,  # noqa: F401  (LOCKFILE kept for tests to monkeypatch)
    STATE_PHASE_MAP,
    GITLAB_NAMESPACE, IMPLEMENTERS,
)
from pipeline_io import (
    load_pipeline, save_pipeline, update_pipeline,
    add_history, now_iso, get_path, find_issue,
    clear_pending_notification, merge_pending_notifications,
)
from engine.filter import require_issue_author, UnauthorizedAuthorError
from engine.fsm import get_notification_for_state
from notify import (
    notify_implementer, notify_reviewers, notify_discord,
    resolve_reviewer_arg,
)

from commands.dev.helpers import parse_issue_args, _masked_reviewer, _reset_to_idle


def _pipelines_dir():
    """Resolve PIPELINES_DIR through the package namespace.

    Tests patch ``commands.dev.PIPELINES_DIR`` via monkeypatch.  By
    resolving through the package ``__dict__`` at call-time, those patches
    are honoured even though this code lives in a submodule.
    """
    import commands.dev as _pkg  # noqa: F811 — deferred to avoid circular import
    return _pkg.PIPELINES_DIR


def get_status_text(enabled_only: bool = False) -> str:
    """全PJの状態を文字列として取得。

    Args:
        enabled_only: True の場合、enabled=True のプロジェクトのみ含める

    Returns:
        Status text string. "No active pipelines." if no matching pipelines.
    """
    import io
    output = io.StringIO()

    _pipelines_dir().mkdir(parents=True, exist_ok=True)
    files = sorted(_pipelines_dir().glob("*.json"))

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
        review_mode = data.get("review_mode", "")
        issues = ", ".join(f"#{i['issue']}" for i in batch) if batch else "none"
        has_review_info = "review_config" in data or "review_mode" in data
        if has_review_info:
            from engine.fsm import get_phase_config
            phase = "code" if state.startswith("CODE_") else "design"
            phase_config = get_phase_config(data, phase)
            reviewers_str = ", ".join(f'"{r}"' for r in phase_config["members"])
        else:
            reviewers_str = ""
        output.write(f"[{enabled}] {pj}: {state}  issues=[{issues}]  ReviewerSize={review_mode}  Reviewers=[{reviewers_str}]\n")

        # Show per-issue review progress
        if state in ("DESIGN_REVIEW", "CODE_REVIEW") and batch and has_review_info:
            review_key = "design_reviews" if state == "DESIGN_REVIEW" else "code_reviews"
            min_rev = phase_config["min_reviews"]
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
    _pipelines_dir().mkdir(parents=True, exist_ok=True)
    path = get_path(args.project)
    if path.exists():
        print(f"Already exists: {path}", file=sys.stderr)
        sys.exit(1)

    abs_repo = os.path.abspath(args.repo_path) if args.repo_path else ""
    if abs_repo and not os.path.isdir(abs_repo):
        print(f"Error: --repo-path does not exist or is not a directory: {abs_repo}", file=sys.stderr)
        sys.exit(1)

    data = {
        "project": args.project,
        "gitlab": args.gitlab or f"{GITLAB_NAMESPACE}/{args.project}",
        "repo_path": abs_repo,
        "state": "IDLE",
        "enabled": False,
        "implementer": args.implementer or IMPLEMENTERS[0],
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
        WATCHDOG_LOOP_PIDFILE.unlink(missing_ok=True)
        # Do not unlink LOCKFILE: the running watchdog-loop process tree still
        # holds the inode via inherited fd 200. Unlinking would let the next
        # cron firing create the path with a new inode, bypassing the
        # inode-based flock singleton protection and spawning a duplicate loop.
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
                f"Cannot extend: current state {state} is not eligible "
                f"(eligible: {', '.join(sorted(EXTENDABLE_STATES))})"
            )
        extend_count = data.get("extend_count", 0)
        if extend_count >= MAX_EXTENDS:
            raise SystemExit(
                f"Cannot extend: maximum extension count ({MAX_EXTENDS}) reached"
            )
        data["timeout_extension"] = data.get("timeout_extension", 0) + args.by
        data["extend_count"] = extend_count + 1
        result["state"] = state
        result["implementer"] = data.get("implementer", IMPLEMENTERS[0])
        result["total"] = data["timeout_extension"]
        result["count"] = data["extend_count"]

    update_pipeline(path, do_extend)

    from datetime import datetime
    ts = datetime.now(LOCAL_TZ).strftime("%m/%d %H:%M")
    notify_discord(
        f"[{args.project}] {result['implementer']} extended timeout by {args.by}s "
        f"({result['state']}, {result['count']}/{MAX_EXTENDS}, total +{result['total']}s, {ts})"
    )

    print(f"{args.project}: timeout extended +{args.by}s (total +{result['total']}s)")


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


def _fetch_issue_info(issue_num: int, gitlab: str) -> tuple[str, str | None]:
    """GitLab APIでIssueのタイトルとstateを取得。

    Returns:
        (title, state) のタプル。
        title: Issue タイトル。API失敗時は空文字列。
        state: "opened" / "closed" / None。
            - "opened": open issue
            - "closed": closed issue
            - None: API失敗（タイムアウト、ネットワークエラー、glab未検出）
                    または未知の state 値（"opened" でも "closed" でもない）
    """
    try:
        result = subprocess.run(
            [GLAB_BIN, "issue", "show", str(issue_num), "--output", "json", "-R", gitlab],
            capture_output=True, text=True, timeout=GLAB_TIMEOUT, check=False,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            require_issue_author(data)
            title = data.get("title", "")
            raw_state = data.get("state")
            if raw_state in ("opened", "closed"):
                return (title, raw_state)
            # 未知の state: None 扱い + 警告
            print(f"Warning: issue #{issue_num} has unknown state '{raw_state}'",
                  file=sys.stderr)
            return (title, None)
        # glab が非ゼロ終了
        print(f"Warning: glab issue show failed for #{issue_num} (rc={result.returncode})",
              file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"Warning: glab issue show timed out for #{issue_num}", file=sys.stderr)
    except FileNotFoundError:
        print(f"Warning: glab binary not found: {GLAB_BIN}", file=sys.stderr)
    except UnauthorizedAuthorError:
        raise
    except Exception as e:
        print(f"Warning: failed to fetch issue #{issue_num}: {e}", file=sys.stderr)
    return ("", None)


def cmd_triage(args):
    """Issueをバッチに投入（複数指定可）"""
    path = get_path(args.project)
    data = load_pipeline(get_path(args.project))
    gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{args.project}")
    titles = list(args.title) + [""] * (len(args.issue) - len(args.title))

    # --- Phase 1: タイトル＋state 一括取得 ---
    # タイトルが既知の場合でも state 確認のために API を呼ぶ。
    # タイトルが既知の場合は API 取得の title は使わず、引数の title を維持する。
    states: list[str | None] = [None] * len(args.issue)
    for idx, (num, title) in enumerate(zip(args.issue, titles)):
        fetched_title, state = _fetch_issue_info(num, gitlab)
        states[idx] = state
        if not title:
            titles[idx] = fetched_title

    # --- Phase 2: closed フィルタリング（do_triage の外で実施） ---
    skipped_closed: list[int] = []
    unverified: list[int] = []
    if not getattr(args, "allow_closed", False):
        survivors = []
        survivor_titles = []
        for num, title, state in zip(args.issue, titles, states):
            if state == "closed":
                skipped_closed.append(num)
            elif state is None:
                # API失敗 or 未知state: スキップしない（可用性優先）
                unverified.append(num)
                survivors.append(num)
                survivor_titles.append(title)
            else:  # "opened"
                survivors.append(num)
                survivor_titles.append(title)
    else:
        survivors = list(args.issue)
        survivor_titles = list(titles)

    # --- Phase 3: Discord 通知（SystemExit より前に必ず送信） ---
    # 通知対象: allow_closed=False の場合のみ。allow_closed=True では通知しない。
    if skipped_closed or unverified:
        from config import DISCORD_CHANNEL
        from notify import post_discord
        if skipped_closed:
            nums_str = ", ".join(f"#{n}" for n in skipped_closed)
            post_discord(DISCORD_CHANNEL, f"⚠️ Skipped closed issues: {nums_str}")
            print(f"Skipped closed issues: {nums_str}")
        if unverified:
            nums_str = ", ".join(f"#{n}" for n in unverified)
            post_discord(DISCORD_CHANNEL,
                         f"⚠️ Could not verify issue state: {nums_str}")
            print(f"Warning: could not verify state for issues: {nums_str}")

    # --- Phase 4: all-closed チェック ---
    if not survivors:
        from task_queue import QueueSkipError
        raise QueueSkipError("All issues are closed. Nothing to add to batch.")

    # --- Phase 5: do_triage に filtered リストを渡してバッチ追加 ---
    filtered_args = argparse.Namespace(
        project=args.project,
        issue=survivors,
        title=survivor_titles,
        allow_closed=getattr(args, "allow_closed", False),
    )

    def do_triage(data):
        state = data.get("state", "IDLE")
        if state != "IDLE":
            raise SystemExit(f"Cannot add issues in state {state} (allowed: IDLE)")
        batch = data.get("batch", [])
        if len(batch) + len(filtered_args.issue) > MAX_BATCH:
            raise SystemExit(
                f"Batch overflow: {len(batch)} existing + {len(filtered_args.issue)} new > {MAX_BATCH}"
            )

        # Clear reviewer metadata if starting a new batch
        if len(batch) == 0:
            data.pop("excluded_reviewers", None)
            data.pop("min_reviews_override", None)
            data.pop("design_min_reviews_met_at", None)
            data.pop("code_min_reviews_met_at", None)
            data.pop("review_config", None)

        for num, title in zip(filtered_args.issue, filtered_args.title):
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
    nums = ", ".join(f"#{n}" for n in filtered_args.issue)
    print(f"{args.project}: {nums} added to batch")


def cmd_start(args):
    """gokrax start --project X [--issue N [N...]]

    triage + DESIGN_PLAN遷移 + watchdog有効化を一括実行。
    --issue省略時はGitLab APIでopen issue全件取得。
    """
    args.issue = parse_issue_args(args.issue) if args.issue else args.issue
    from gokrax import _start_loop
    from config import NONE_TO_FALSE_KEYS, resolve_queue_options

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
    if getattr(args, "no_skip_design", None):
        args.skip_design = False
    if getattr(args, "no_no_cc", None):
        args.no_cc = False
    if getattr(args, "no_exclude_high_risk", None):
        args.exclude_high_risk = False
    if getattr(args, "no_exclude_any_risk", None):
        args.exclude_any_risk = False
    if getattr(args, "no_automerge", None):
        args.automerge = False

    # デフォルトオプション適用: CLI 引数で明示指定されていない（None のまま）オプションにデフォルト値を注入
    from task_queue import _QUEUE_OPT_ALIASES
    resolved = resolve_queue_options(args.project)
    for key, default_val in resolved.items():
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

    # 前回失敗時の残留フラグをクリア（do_setup で再設定する前に）
    def _clear_stale_skip(d):
        d.pop("skip_cc_plan", None)
        d.pop("skip_test", None)
        d.pop("skip_assess", None)
        d.pop("skip_design", None)
        d.pop("no_cc", None)
        d.pop("exclude_high_risk", None)
        d.pop("exclude_any_risk", None)

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
        allow_closed=getattr(args, "allow_closed", False),
    )
    cmd_triage(triage_args)

    # 4. keep-ctx フラグ正規化 (keep-context / keep-ctx-all → 両方True)
    if getattr(args, "keep_context", False) or getattr(args, "keep_ctx_all", False):
        args.keep_ctx_batch = True
        args.keep_ctx_intra = True

    # 5. review_mode / keep_ctx / p2_fix / comment 設定（遷移前に設定して/newの宛先に反映させる）
    from config import REVIEW_MODES
    if getattr(args, "mode", None) and args.mode not in REVIEW_MODES:
        raise SystemExit(f"Invalid mode: {args.mode} (valid: {list(REVIEW_MODES)})")

    def do_setup(data):
        # 残留フラグクリア（常に実行）
        _clear_stale_skip(data)
        # モード設定（各項目は条件付き）
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
        if getattr(args, "skip_design", False):
            data["skip_design"] = True
        if getattr(args, "no_cc", False):
            data["no_cc"] = True
        if getattr(args, "exclude_high_risk", False):
            data["exclude_high_risk"] = True
        if getattr(args, "exclude_any_risk", False):
            data["exclude_any_risk"] = True
        am = getattr(args, "automerge", None)
        if am is not None:
            data["automerge"] = am
        if getattr(args, "cc_plan_model", None):
            data["cc_plan_model"] = args.cc_plan_model
        if getattr(args, "cc_impl_model", None):
            data["cc_impl_model"] = args.cc_impl_model
        if not data.get("review_mode"):
            raise SystemExit(
                f"review_mode is not set for {args.project}. "
                f"Use --mode <mode> or set it with: gokrax review-mode --pj {args.project} --mode <mode>"
            )
    update_pipeline(path, do_setup)


    # 7. INITIALIZEに遷移 + watchdog有効化（set_enabled で同一ロック内で設定）
    transition_args = argparse.Namespace(
        project=args.project,
        to="INITIALIZE",
        actor="cli",
        force=False,
        resume=False,
        set_enabled=True,
    )
    try:
        cmd_transition(transition_args)

        # 8. loop起動
        _start_loop()
    except BaseException:
        # step 6 が成功して enabled=True + INITIALIZE が残っている可能性がある
        # 安全側に倒す: 状態を IDLE + enabled=False に戻す
        def _rollback(data: dict) -> None:
            if data.get("state") != "IDLE":
                add_history(data, data.get("state", "IDLE"), "IDLE", "cli:rollback")
            _reset_to_idle(data)
            data["state"] = "IDLE"
        try:
            update_pipeline(path, _rollback)
        except Exception:
            pass  # ロールバック自体の失敗は握りつぶす（元の例外を優先）
        # watchdog-loop を停止（他 PJ が有効でない場合のみ）
        from gokrax import _any_pj_enabled, _stop_loop
        if not _any_pj_enabled():
            _stop_loop()
        raise

    # 9. 完了メッセージ
    issues_str = ", ".join(f"#{n}" for n in issue_nums)
    print(f"{args.project}: started with issues [{issues_str}] → INITIALIZE (watchdog enabled)")


def cmd_transition(args):
    """状態遷移（バリデーション付き）"""
    import config as _cfg
    if getattr(args, "dry_run", False):
        _cfg.DRY_RUN = True
    path = get_path(args.project)
    resume = getattr(args, "resume", False)
    ctx = {}  # pass values from inside lock to outside (Issue #59)

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
        _set_enabled = getattr(args, "set_enabled", None)
        if isinstance(_set_enabled, bool) and target != "IDLE":
            data["enabled"] = _set_enabled
        if target == "IDLE":
            _reset_to_idle(data)
        elif args.force and target in ("DESIGN_REVIEW", "CODE_REVIEW"):
            # Issue #41: Reset counters when force-transitioning to REVIEW states from BLOCKED
            counter_key = "design_revise_count" if "DESIGN" in target else "code_revise_count"
            data.pop(counter_key, None)
            print(f"[FORCE] Resetting {counter_key} for {current} → {target} transition")
        elif resume and current == "BLOCKED" and target in ("DESIGN_REVISE", "CODE_REVISE"):
            from config import MAX_REVISE_CYCLES
            max_key = "max_design_revise_cycles" if "DESIGN" in target else "max_code_revise_cycles"
            val = data.get(max_key)
            current_max = val if val is not None else MAX_REVISE_CYCLES
            data[max_key] = current_max + MAX_REVISE_CYCLES
            data["enabled"] = True  # BLOCKED時にFalseになっているのでTrueに戻す
            print(f"[RESUME] {max_key}: {current_max} → {data[max_key]} for {current} → {target} transition")
        elif target == "BLOCKED":
            # Disable watchdog when manually transitioning to BLOCKED (Issue #29)
            data["enabled"] = False

        # === Issue #59: 通知情報をロック内で構築 + pending フラグ ===
        pj = data.get("project", args.project)
        batch = data.get("batch", [])
        gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{pj}")
        implementer = data.get("implementer", IMPLEMENTERS[0])
        repo_path = data.get("repo_path", "")
        review_mode = data.get("review_mode")
        if not review_mode:
            raise SystemExit(f"review_mode is not set for {args.project}. Set it with: gokrax review-mode --pj {args.project} --mode <mode>")

        p2_fix = data.get("p2_fix", False)
        comment = data.get("comment", "")
        notif = get_notification_for_state(target, pj, batch, gitlab, implementer, p2_fix=p2_fix, comment=comment)
        prefix = "(resumed) " if resume else ""

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
            merge_pending_notifications(data, pending, pj)

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
            skip_reset = True  # always skip for REVISE transition
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
                from pipeline_io import load_gokrax_state, update_gokrax_state
                # グローバル状態から前回PJを取得（PJ単位JSONではなく共有ファイル）
                gstate = load_gokrax_state()
                last_pj = gstate.get("last_impl_project", "")
                if not last_pj or last_pj != pj:
                    impl = ctx["implementer"]
                # グローバル状態に記録
                def _set_last_impl(s):
                    s["last_impl_project"] = pj
                update_gokrax_state(_set_last_impl)
            from engine.fsm import get_phase_config
            reset_phase = STATE_PHASE_MAP.get(args.to, "design")
            pipeline_data = load_pipeline(get_path(pj))
            phase_config = get_phase_config(pipeline_data, reset_phase)
            _reset_reviewers(phase_config, implementer=impl)
    if notif.impl_msg:
        phase = STATE_PHASE_MAP.get(args.to, "")
        ok = notify_implementer(ctx["implementer"], f"[gokrax] {pj}: {prefix}{notif.impl_msg}", project=pj, phase=phase)
        if ok:
            clear_pending_notification(pj, "impl")
    if notif.send_review:
        excluded = ctx["excluded_reviewers"]
        from engine.fsm import get_phase_config as _gpc
        _reset_phase = STATE_PHASE_MAP.get(args.to, "design")
        _pipeline_data = load_pipeline(get_path(pj))
        phase_config = _gpc(_pipeline_data, _reset_phase)
        notify_reviewers(pj, args.to, ctx["batch"], ctx["gitlab"],
                        repo_path=ctx["repo_path"],
                        review_mode=ctx["review_mode"], excluded=excluded,
                        phase_config=phase_config)
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
    for path in sorted(_pipelines_dir().glob("*.json")):
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


def cmd_review_mode(args):
    """レビューモードを変更"""
    path = get_path(args.project)

    def do_update(data):
        old = data.get("review_mode", "(unset)")
        data["review_mode"] = args.mode
        return old

    data = update_pipeline(path, do_update)
    old = data.get("_prev_review_mode", data.get("review_mode", "(unset)"))
    members = REVIEW_MODES[args.mode]["members"]
    print(f"{args.project}: review_mode → {args.mode} (reviewers: {members})")


def cmd_exclude(args):
    """レビュアーの動的除外を管理"""
    path = get_path(args.project)

    if args.list:
        data = load_pipeline(path)
        excluded = data.get("excluded_reviewers", [])
        _rnm_list = data.get("reviewer_number_map")
        masked_excluded = [_masked_reviewer(n, _rnm_list) for n in excluded]
        print(f"{args.project}: excluded_reviewers = {masked_excluded}")
        return

    _pipeline: dict = load_pipeline(path)
    _rnm: dict[str, int] | None = _pipeline.get("reviewer_number_map")
    if args.add:
        args.add = [resolve_reviewer_arg(n, _rnm) for n in args.add]
    elif args.remove:
        args.remove = [resolve_reviewer_arg(n, _rnm) for n in args.remove]

    # --add / --remove 共通: レビュアー名バリデーション
    names = args.add or args.remove
    unknown = [n for n in names if n not in REVIEWERS]
    if unknown:
        sys.exit(f"Unknown reviewer(s): {unknown}")

    if args.add:
        added_names: list[str] = []
        final_excluded: list[str] = []
        clamp_msg: str = ""

        def do_add(data: dict) -> None:
            nonlocal added_names, final_excluded, clamp_msg
            excluded = data.get("excluded_reviewers", [])
            added = []
            for name in args.add:
                if name not in excluded:
                    excluded.append(name)
                    added.append(name)
            data["excluded_reviewers"] = excluded
            # deadlock clamp
            from engine.fsm import get_phase_config as _get_phase_config_ex
            state = data.get("state", "IDLE")
            phase = "code" if state.startswith("CODE_") else "design"
            _phase_cfg = _get_phase_config_ex(data, phase)
            effective_count = len([m for m in _phase_cfg["members"] if m not in excluded])
            min_reviews = _phase_cfg["min_reviews"]
            if effective_count < min_reviews:
                clamped = max(effective_count, 0)
                data["min_reviews_override"] = clamped
                clamp_msg = f"  deadlock clamp: effective={effective_count} < min_reviews={min_reviews}, override={clamped}"
            else:
                data.pop("min_reviews_override", None)
            added_names = added
            final_excluded = list(excluded)

        update_pipeline(path, do_add)
        _masked_added = [_masked_reviewer(n, _rnm) for n in added_names]
        _masked_final = [_masked_reviewer(n, _rnm) for n in final_excluded]
        if added_names:
            print(f"{args.project}: excluded {_masked_added} (excluded_reviewers={_masked_final})")
        else:
            print(f"{args.project}: already excluded (excluded_reviewers={_masked_final})")
        if clamp_msg:
            print(clamp_msg)
        return

    if args.remove:
        removed_names: list[str] = []
        final_excluded_r: list[str] = []
        clamp_msg_r: str = ""

        def do_remove(data: dict) -> None:
            nonlocal removed_names, final_excluded_r, clamp_msg_r
            excluded = data.get("excluded_reviewers", [])
            removed = []
            for name in args.remove:
                if name in excluded:
                    excluded.remove(name)
                    removed.append(name)
            data["excluded_reviewers"] = excluded
            # deadlock clamp
            from engine.fsm import get_phase_config as _get_phase_config_ex
            state = data.get("state", "IDLE")
            phase = "code" if state.startswith("CODE_") else "design"
            _phase_cfg = _get_phase_config_ex(data, phase)
            effective_count = len([m for m in _phase_cfg["members"] if m not in excluded])
            min_reviews = _phase_cfg["min_reviews"]
            if effective_count < min_reviews:
                clamped = max(effective_count, 0)
                data["min_reviews_override"] = clamped
                clamp_msg_r = f"  deadlock clamp: effective={effective_count} < min_reviews={min_reviews}, override={clamped}"
            else:
                data.pop("min_reviews_override", None)
            removed_names = removed
            final_excluded_r = list(excluded)

        update_pipeline(path, do_remove)
        _masked_removed = [_masked_reviewer(n, _rnm) for n in removed_names]
        _masked_final_r = [_masked_reviewer(n, _rnm) for n in final_excluded_r]
        if removed_names:
            print(f"{args.project}: unexcluded {_masked_removed} (excluded_reviewers={_masked_final_r})")
        else:
            print(f"{args.project}: not excluded (excluded_reviewers={_masked_final_r})")
        if clamp_msg_r:
            print(clamp_msg_r)
        return


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

    if DISCORD_CHANNEL:
        result = post_discord(DISCORD_CHANNEL, content)
        if not result:
            raise SystemExit("Failed to post to Discord")
        if result.is_partial:
            logger.warning("Partial delivery: some notification chunks failed")
        message_id = result.message_id
    else:
        message_id = ""
        logger.warning("Discord not configured; skipping merge-summary post")

    def do_update(data):
        data["summary_message_id"] = message_id
        add_history(data, data["state"], "MERGE_SUMMARY_SENT", "cli")
        data["state"] = "MERGE_SUMMARY_SENT"

    update_pipeline(path, do_update)

    # Notify implementer of batch completion (Issue #48)
    implementer = data.get("implementer") or IMPLEMENTERS[0]
    notification_msg = (
        f"[gokrax] {project}: batch completed\n"
        f"{content}\n\n"
        "Review the above work and record only the following:\n"
        "- Pitfalls or issues encountered (if any)\n"
        "- Lessons learned from reviewer feedback (if any)\n"
        "- Decisions that affect future work (if any)\n"
        "If nothing to record, NO_REPLY is fine."
    )
    try:
        notify_implementer(implementer, notification_msg)
    except Exception as e:
        logger.warning("implementer notification failed (continuing): %s", e)

    print(f"{args.project}: merge summary sent (message_id={message_id})")


def cmd_ok(args):
    """MERGE_SUMMARY_SENT: record merge approval"""
    path = get_path(args.project)

    def do_ok(data: dict) -> None:
        state = data.get("state", "IDLE")
        if state != "MERGE_SUMMARY_SENT":
            raise SystemExit(f"Cannot approve in state {state} (expected MERGE_SUMMARY_SENT)")
        data["merge_approved"] = True

    update_pipeline(path, do_ok)
    print(f"{args.project}: merge approved")


def cmd_get_comments(args: argparse.Namespace) -> None:
    """Retrieve filtered comments for a GitLab issue."""
    from engine.filter import validate_comment_author

    project = args.project
    issue_num = args.issue

    path = get_path(project)
    data = load_pipeline(path)
    gitlab = data.get("gitlab", "")

    page = 1
    all_notes: list[dict] = []
    while True:
        try:
            result = subprocess.run(
                [GLAB_BIN, "api",
                 f"projects/:id/issues/{issue_num}/notes?per_page=100&sort=asc&page={page}",
                 "-R", gitlab],
                capture_output=True, text=True, timeout=GLAB_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"Error: glab api timed out for issue #{issue_num}", file=sys.stderr)
            sys.exit(1)
        if result.returncode != 0:
            print(f"Error: glab api failed (rc={result.returncode}): {result.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
        try:
            notes = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"Error: invalid JSON from glab api for issue #{issue_num}", file=sys.stderr)
            sys.exit(1)
        if not notes:
            break
        all_notes.extend(notes)
        page += 1

    filtered: list[dict] = []
    for note in all_notes:
        if note.get("system"):
            continue
        if not validate_comment_author(note):
            continue
        filtered.append(note)

    for i, note in enumerate(filtered):
        author = note.get("author")
        username = author.get("username", "<unknown>") if isinstance(author, dict) else "<unknown>"
        created_at = note.get("created_at", "<unknown>")
        body = note.get("body") or ""
        if i > 0:
            print()
        print(f"--- comment by {username} at {created_at} ---")
        print(body)


def cmd_blocked_report(args) -> None:
    """BLOCKED: send implementer situation report to Discord (fire-and-forget)."""
    path = get_path(args.project)
    data = load_pipeline(path)
    state = data.get("state", "IDLE")
    if state != "BLOCKED":
        raise SystemExit(f"Not in BLOCKED state: {state}")
    summary = args.summary.strip()
    if not summary:
        raise SystemExit("--summary must not be empty or whitespace-only")
    summary = summary[:500]
    q_prefix = "[Queue]" if data.get("queue_mode") else ""
    notify_discord(f"{q_prefix}[{args.project}] BLOCKED report: {summary}")
    print(f"{args.project}: blocked report sent")
