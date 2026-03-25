#!/usr/bin/env python3
"""gokrax-watchdog.py — LLM不要のパイプラインオーケストレーター

loop.shで20秒間隔で実行。cronで1分間隔でloop.sh確認。pipeline JSONを読んで条件満たしてたら状態遷移+アクター通知。
冪等。何回実行しても同じ結果。

Double-Checked Locking パターン:
  1. ロックなしで事前チェック（不要なら早期リターン）
  2. update_pipeline のロック内で再チェック + 遷移
  3. ロック外で通知
"""

import json
import logging
import os
import re
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    PIPELINES_DIR, LOCAL_TZ, LOG_FILE, REVIEW_MODES, CC_MODEL_PLAN, CC_MODEL_IMPL,
    GOKRAX_CLI, INACTIVE_THRESHOLD_SEC, SESSIONS_BASE,
    STATE_PHASE_MAP, GITLAB_NAMESPACE, IMPLEMENTERS,
    # WATCHDOG_LOOP_PIDFILE, WATCHDOG_LOOP_CRON_MARKER は gokrax.py の enable/disable 専用
)
from config import (
    SPEC_STATES,
    MAX_SPEC_RETRIES, SPEC_REVISE_SELF_REVIEW_PASSES, SPEC_REVIEW_RAW_RETENTION_DAYS,
)
from datetime import datetime as _datetime
from pipeline_io import (
    load_pipeline, update_pipeline, get_path,
    add_history, now_iso, find_issue,
    clear_pending_notification, merge_pending_notifications,
    ensure_spec_reviews_dir,
)
from notify import (
    notify_implementer, notify_reviewers, notify_discord,
    send_to_agent, send_to_agent_queued, ping_agent,
)
from messages import render
from engine.shared import log, _is_ok_reply, _is_cc_running, _is_agent_inactive
from engine.cc import (
    _start_cc, _has_pytest, _kill_pytest_baseline,
    _poll_pytest_baseline, _auto_push_and_close,
    _poll_code_test, _start_code_test, _kill_code_test, _start_cc_test_fix,
)
from engine.reviewer import (
    _reset_reviewers, _reset_short_context_reviewers,
    count_reviews, _awaiting_dispute_re_review,
    _revise_target_issues, clear_reviews,
    _get_pending_reviewers, _cleanup_review_files,
)
from engine.cleanup import _cleanup_batch_state
from spec_review import (
    should_continue_review, _reset_review_requests,
    parse_review_yaml, validate_received_entry,
)





# BLOCKEDまでの時間 (秒)
from config import BLOCK_TIMERS, NUDGE_GRACE_SEC, EXTENDABLE_STATES, EXTEND_NOTICE_THRESHOLD

from engine.fsm import check_transition, get_min_reviews, _nudge_key, _recover_pending_notifications, build_review_config, get_phase_config


from engine.fsm_spec import (
    SpecTransitionAction,
    check_transition_spec,
    _apply_spec_action,
    _ensure_pipelines_dir,
    _cleanup_expired_spec_files,
    _check_spec_review,
    _check_spec_revise,
    _check_issue_suggestion,
    _check_issue_plan,
    _check_queue_plan,
    _SPEC_TERMINAL_STATES,
    _SPEC_REVIEW_FILE_PATTERN,
)


def _check_queue():
    """キューから次のタスクを起動 (DONE→IDLE後、または SPEC_DONE→IDLE後に呼ばれる)。

    Issue #45: gokrax qrun をサブプロセス経由で呼び出し、循環 import を回避。
    """
    import subprocess as _sp
    from config import GOKRAX_CLI, QUEUE_FILE

    queue_path = QUEUE_FILE
    if not queue_path.exists():
        return

    # gokrax qrun を subprocess 経由で呼び出し
    try:
        result = _sp.run(
            [str(GOKRAX_CLI), "qrun", "--queue", str(queue_path)],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0 and result.stdout.strip():
            log(f"[queue] {result.stdout.strip()}")
        elif "Queue empty" not in result.stdout:
            log(f"[queue] qrun failed: {result.stderr.strip()}")
    except _sp.TimeoutExpired:
        log("[queue] qrun timeout (>180s)")
    except Exception as e:
        log(f"[queue] qrun error: {e}")


def process(path: Path):
    # === 第1チェック (ロックなし) ===
    data = load_pipeline(path)
    if not data.get("enabled", False):
        return

    # === Issue #92: pytest ベースライン回収 ===
    pj_poll = data.get("project", path.stem)
    _poll_pytest_baseline(path, pj_poll)

    # === Issue #87: CODE_TEST テスト完了ポーリング ===
    _poll_code_test(path, pj_poll)

    # === Issue #59: 未完了通知のリカバリ ===
    # pending が残っていれば再送してクリアし、今回のループは終了。
    # 20秒間隔なので1サイクルスキップは許容（設計判断）。
    pj_recover = data.get("project", path.stem)
    pending = data.get("_pending_notifications")
    if pending:
        log(f"[{pj_recover}] recovering pending notifications: {list(pending.keys())}")
        _recover_pending_notifications(pj_recover, pending, data)
        return

    state = data.get("state", "IDLE")
    batch = data.get("batch", [])
    pj = data.get("project", path.stem)

    # (Issue #108) 旧方式の dispute エントリを削除（マイグレーション）
    dispute_pn = data.get("pending_notifications", {})
    if dispute_pn:
        old_keys = [k for k, v in dispute_pn.items() if v.get("type") == "dispute"]
        if old_keys:
            def _clear_old(d, keys=old_keys):
                pn = d.get("pending_notifications", {})
                for k in keys:
                    pn.pop(k, None)
                if not pn:
                    d.pop("pending_notifications", None)
            update_pipeline(path, _clear_old)

    # spec mode: batch空を許容し、専用ロジックに委譲
    if data.get("spec_mode") and state in SPEC_STATES:
        spec_config = data.get("spec_config", {})
        now = _datetime.now(LOCAL_TZ)
        action = check_transition_spec(state, spec_config, now, data)
        # 副作用フィールドが1つでもあれば適用
        if (action.next_state or action.pipeline_updates or action.send_to
                or action.discord_notify or action.nudge_reviewers or action.nudge_implementer):
            action.expected_state = state
            _apply_spec_action(path, action, now, data)
        return

    if state != "DONE" and not batch and not data.get("spec_mode"):
        log(f"[{pj}] WARNING: state={state} but batch is empty")
        return

    pre_action = check_transition(state, batch, data)
    if pre_action.new_state is None and not pre_action.nudge and not pre_action.nudge_reviewers and not pre_action.dispute_nudge_reviewers and not pre_action.save_grace_met_at and not pre_action.run_cc and not pre_action.run_test:
        return

    # === ロック内で第2チェック + 遷移 (Double-Checked Locking) ===
    notification: dict = {}

    state0 = state  # 第1チェック時点のstate（DCL用）

    def do_transition(data):
        _done_batch = []
        _done_queue_mode = False
        # ロック待ち中に他プロセスが状態を変えた場合は何もしない（通知も含めてスキップ）
        if data.get("state", "IDLE") != state0:
            return

        state = data.get("state", "IDLE")
        batch = data.get("batch", [])
        action = check_transition(state, batch, data)

        # Grace period met_at の永続化（check_transition は data を変更しない）
        if action.save_grace_met_at and not action.new_state:
            key = action.save_grace_met_at
            if not data.get(key):
                data[key] = _datetime.now(LOCAL_TZ).isoformat()
                pj = data.get("project", path.stem)
                log(f"[{pj}] {key} saved: {data[key]}")
            return

        # レビュアー催促（書き込み不要、情報保存のみ）
        if action.nudge_reviewers or action.dispute_nudge_reviewers:
            pj = data.get("project", path.stem)
            notification.update({
                "pj": pj,
                "action": action,
                "nudge_reviewers": list(action.nudge_reviewers) if action.nudge_reviewers else [],
                "dispute_nudge_reviewers": list(action.dispute_nudge_reviewers) if action.dispute_nudge_reviewers else [],
                "batch": list(batch),
                "old_state": state,
                "queue_mode": data.get("queue_mode", False),
            })
            return

        # 実装担当催促（遷移なし、カウンタ書き込みのみ）
        if action.nudge:
            implementer = data.get("implementer", IMPLEMENTERS[0])
            if not _is_agent_inactive(implementer, data):
                # アクティブなら催促しない（カウンタも上げない）
                return
            # 前回催促からINACTIVE_THRESHOLD_SEC未満ならスキップ
            last_nudge = data.get("_last_nudge_at")
            if last_nudge:
                try:
                    elapsed_since_nudge = (_datetime.now(LOCAL_TZ) - _datetime.fromisoformat(last_nudge)).total_seconds()
                    if elapsed_since_nudge < INACTIVE_THRESHOLD_SEC:
                        return
                except (ValueError, TypeError):
                    pass
            key = _nudge_key(action.nudge)
            data[key] = data.get(key, 0) + 1
            data["_last_nudge_at"] = _datetime.now(LOCAL_TZ).isoformat()
            pj = data.get("project", path.stem)
            log(f"[{pj}] {action.nudge}: 催促通知送信 (count={data[key]})")
            notification.update({
                "pj": pj,
                "action": action,
                "implementer": data.get("implementer", IMPLEMENTERS[0]),
                "batch": list(batch),
                "gitlab": data.get("gitlab", f"{GITLAB_NAMESPACE}/{pj}"),
                "nudge_count": data[key],
                "queue_mode": data.get("queue_mode", False),
            })
            return

        if action.new_state is None and not action.run_cc:
            # ロック待ち中に他プロセスが状態を変えた → スキップ
            return

        # run_cc only（状態遷移なし）: CC起動フラグだけ立ててreturn
        if action.run_cc and action.new_state is None:
            pj = data.get("project", path.stem)
            notification.update({
                "pj": pj,
                "action": action,
                "old_state": data.get("state", "IDLE"),
                "repo_path": data.get("repo_path", ""),
                "batch": list(data.get("batch", [])),
                "gitlab": data.get("gitlab", f"{GITLAB_NAMESPACE}/{pj}"),
            })
            # Issue #59: pending notification for run_cc
            pending = {"run_cc": True}
            merge_pending_notifications(data, pending, pj)
            return

        pj = data.get("project", path.stem)

        # 旧 keep_context → 新フィールドへの正規化 (Issue #58)
        if "keep_context" in data and "keep_ctx_batch" not in data:
            legacy = data.pop("keep_context", False)
            if legacy:
                data["keep_ctx_batch"] = True
                data["keep_ctx_intra"] = True

        # DONE状態: バッチを退避してからクリア + watchdog無効化
        if state == "DONE":
            _done_batch = list(data.get("batch", []))  # close用に退避
            _done_queue_mode = data.get("queue_mode", False)  # _check_queue判定用に退避
            _cleanup_batch_state(data, pj)

        # ASSESSMENT → IDLE (リスクスキップ): クリーンアップ前に通知用データを退避 (Issue #181)
        _skip_assessment = {}
        _skip_batch = []
        _skip_queue_mode = False
        if state == "ASSESSMENT" and action.new_state == "IDLE":
            from engine.fsm import _worst_risk
            worst_risk = _worst_risk(data.get("batch", []))
            _skip_assessment = {"domain_risk": worst_risk}
            _skip_batch = list(data.get("batch", []))
            _skip_queue_mode = data.get("queue_mode", False)

        # ASSESSMENT → IDLE (リスクスキップ): DONE と同等のクリーンアップ (Issue #181)
        if state == "ASSESSMENT" and action.new_state == "IDLE":
            _cleanup_batch_state(data, pj)

        # ASSESSMENT → IMPLEMENTATION (一部除外): batch を remaining に差し替え (Issue #200)
        if state == "ASSESSMENT" and action.new_state == "IMPLEMENTATION" and action.remaining_issues is not None:
            data["batch"] = list(action.remaining_issues)

        # INITIALIZE→DESIGN_PLAN/DESIGN_APPROVED: Reset REVISE cycle counters + 初期化処理 (Issue #29, #125, #201)
        if state == "INITIALIZE" and action.new_state in ("DESIGN_PLAN", "DESIGN_APPROVED"):
            data.pop("design_revise_count", None)
            data.pop("code_revise_count", None)
            _cleanup_review_files(pj)
            # base_commit: バッチ開始時点の HEAD を full SHA で記録
            data.pop("base_commit", None)
            repo = data.get("repo_path", "")
            if repo:
                try:
                    import subprocess as _sub_bc
                    _result = _sub_bc.run(
                        ["git", "-C", repo, "log", "--format=%H", "-1"],
                        capture_output=True, text=True, timeout=10, check=False,
                    )
                    if _result.returncode == 0 and _result.stdout.strip():
                        data["base_commit"] = _result.stdout.strip()
                        log(f"[{pj}] base_commit recorded at {action.new_state}: {data['base_commit'][:7]}")
                except Exception as e:
                    log(f"[{pj}] WARNING: failed to record base_commit: {e}")

            # Issue #92: 前バッチの pytest を停止 + test_baseline クリア
            _kill_pytest_baseline(data, pj)
            data.pop("test_baseline", None)
            # Issue #87: code test クリーンアップ
            _kill_code_test(data, pj)
            data.pop("test_result", None)
            data.pop("test_output", None)
            data.pop("test_retry_count", None)

            # Issue #203: フェーズ別レビュアー構成を review_config に展開
            review_mode = data.get("review_mode", "standard")
            mode_config = REVIEW_MODES.get(review_mode, REVIEW_MODES["standard"])
            data["review_config"] = build_review_config(mode_config)

            # Issue #92: pytest ベースライン取得（バックグラウンド）
            repo = data.get("repo_path", "")
            if repo and _has_pytest(repo):
                import subprocess as _sub
                import tempfile
                try:
                    head = _sub.run(
                        ["git", "-C", repo, "rev-parse", "HEAD"],
                        capture_output=True, text=True, timeout=10, check=True,
                    ).stdout.strip()

                    fd_out, pytest_out_path = tempfile.mkstemp(suffix=".txt", prefix="gokrax-pytest-")
                    os.close(fd_out)
                    exit_code_path = pytest_out_path + ".exit"

                    import shlex
                    fd_sh, script_path = tempfile.mkstemp(suffix=".sh", prefix="gokrax-pytest-")
                    script = (
                        f'#!/bin/bash\n'
                        f'cd {shlex.quote(repo)}\n'
                        f'python3 -m pytest --tb=short -q > {shlex.quote(pytest_out_path)} 2>&1\n'
                        f'echo $? > {shlex.quote(exit_code_path)}\n'
                        f'rm -f {shlex.quote(script_path)}\n'
                    )
                    os.write(fd_sh, script.encode())
                    os.close(fd_sh)
                    os.chmod(script_path, 0o700)

                    proc = _sub.Popen(
                        ["bash", script_path],
                        stdout=_sub.DEVNULL,
                        stderr=_sub.DEVNULL,
                        start_new_session=True,
                    )
                    data["_pytest_baseline"] = {
                        "pid": proc.pid,
                        "commit": head,
                        "started_at": now_iso(),
                        "output_path": pytest_out_path,
                        "exit_code_path": exit_code_path,
                    }
                    log(f"[{pj}] pytest baseline started (pid={proc.pid}, commit={head[:8]})")
                except Exception as e:
                    log(f"[{pj}] WARNING: pytest baseline start failed: {e}")
                    data.pop("_pytest_baseline", None)
            else:
                data.pop("_pytest_baseline", None)
                data.pop("test_baseline", None)

        # REVIEW/NPASS→REVISE: Increment cycle counter (Issue #29) + NPASS cleanup
        if state in ("DESIGN_REVIEW", "CODE_REVIEW", "DESIGN_REVIEW_NPASS", "CODE_REVIEW_NPASS") and action.new_state in ("DESIGN_REVISE", "CODE_REVISE"):
            counter_key = "design_revise_count" if "DESIGN" in state else "code_revise_count"
            data[counter_key] = data.get(counter_key, 0) + 1
            log(f"[{pj}] {counter_key} incremented to {data[counter_key]}")
            data.pop("_npass_target_reviewers", None)

        # REVISE → REVIEW: ロック内でレビュークリア
        if state in ("DESIGN_REVISE", "CODE_REVISE"):
            revised_key = "design_revised" if "DESIGN" in state else "code_revised"
            key = "design_reviews" if "DESIGN" in state else "code_reviews"
            
            # クリア前にP0/P1レビューを退避（再レビュー依頼で前回指摘を引用するため）
            prev_reviews = {}
            for issue in batch:
                reviews = issue.get(key, {})
                cleared = {
                    r: dict(v) for r, v in reviews.items()
                    if v.get("verdict", "").upper() in ("REJECT", "P0", "P1", "P2")
                }
                if cleared:
                    prev_reviews[issue["issue"]] = cleared
            # notification dict 経由で渡す（pipeline JSON には保存しない）
            notification["prev_reviews"] = prev_reviews
            
            clear_reviews(batch, key, revised_key)

            # Mark flags as resolved (REVISE→REVIEW transition confirmed)
            # Only flags from the current phase that were posted before this transition
            flag_phase = STATE_PHASE_MAP.get(state)
            if flag_phase is not None:
                for issue in batch:
                    for f in issue.get("flags", []):
                        if f.get("phase") == flag_phase and not f.get("resolved"):
                            f["resolved"] = True
                log(f"[{pj}] marked {flag_phase} phase flags as resolved")

            # Clear met_at timestamp when REVISE→REVIEW
            if "DESIGN" in state:
                data.pop("design_min_reviews_met_at", None)
                log(f"[{pj}] cleared design_min_reviews_met_at")
            else:
                data.pop("code_min_reviews_met_at", None)
                log(f"[{pj}] cleared code_min_reviews_met_at")

        # DESIGN_REVIEW → DESIGN_APPROVED: 無応答レビュアーを excluded に追加 (Issue #44)
        if state == "DESIGN_REVIEW" and action.new_state == "DESIGN_APPROVED":
            phase_config = get_phase_config(data, "design")
            all_reviewers = set(phase_config["members"])
            responded = set()
            for item in batch:
                responded.update(item.get("design_reviews", {}).keys())
            no_response = all_reviewers - responded
            if no_response:
                excluded = data.get("excluded_reviewers", [])
                for r in no_response:
                    if r not in excluded:
                        excluded.append(r)
                data["excluded_reviewers"] = excluded
                # effective は excluded 全体（既存 + 今回追加分）を差し引いた実員数
                effective = len(all_reviewers - set(excluded))
                if effective == 0:
                    # 全員除外 — 理論上ありえないが防御
                    log(f"[{pj}] WARNING: effective==0 at DESIGN_APPROVED, skipping min_reviews_override")
                else:
                    data["min_reviews_override"] = max(1, min(phase_config["min_reviews"], effective))
                log(f"[{pj}] 無応答レビュアーを除外: {sorted(no_response)}, excluded={excluded}, effective={effective}")

        # CODE_TEST 進入時: テスト起動情報を notification に保存（ロック外でテスト起動）
        if action.new_state == "CODE_TEST" and action.run_test:
            notification["run_test"] = True
            notification["repo_path"] = data.get("repo_path", "")
            data["test_result"] = None

        # CODE_TEST_FIX 進入時: 古い cc_pid を削除し CC 起動フラグを立てる
        if action.new_state == "CODE_TEST_FIX":
            data.pop("cc_pid", None)
            notification["run_cc_test_fix"] = True

        # BLOCKED: Disable watchdog (Issue #29)
        if action.new_state == "BLOCKED":
            data["enabled"] = False
            log(f"[{pj}] Watchdog disabled due to BLOCKED transition")

        # 催促カウンタ・失敗フラグ・催促タイマーリセット（状態から出るとき）
        if state in BLOCK_TIMERS:
            data.pop(_nudge_key(state), None)
        data.pop("_last_nudge_at", None)
        for k in [k for k in data if k.startswith(("_nudge_failed_", "_last_nudge_"))]:
            del data[k]

        log(f"[{pj}] {state} → {action.new_state}")
        add_history(data, state, action.new_state, actor="watchdog")
        if action.clear_grace_met_at:
            data.pop(action.clear_grace_met_at, None)
        data["state"] = action.new_state

        # NPASS: ターゲットレビュアー保存/更新
        if action.npass_target_reviewers is not None:
            data["_npass_target_reviewers"] = action.npass_target_reviewers

        # NPASS→APPROVED: タイムアウト時の GitLab note 情報を構築
        _npass_timeout_notes: list[dict] = []
        if state in ("DESIGN_REVIEW_NPASS", "CODE_REVIEW_NPASS") and action.new_state in ("DESIGN_APPROVED", "CODE_APPROVED"):
            review_key = "design_reviews" if "DESIGN" in state else "code_reviews"
            for reviewer in data.get("_npass_target_reviewers", []):
                for issue in batch:
                    entry = issue.get(review_key, {}).get(reviewer, {})
                    if entry.get("pass", 1) < entry.get("target_pass", 1):
                        _npass_timeout_notes.append({
                            "issue_num": issue["issue"],
                            "reviewer": reviewer,
                            "pass": entry.get("pass", 1),
                            "target_pass": entry.get("target_pass", 1),
                        })
            data.pop("_npass_target_reviewers", None)

        # ロック外通知用に情報を保存
        # DONE遷移時はbatchが既にクリア済みなので退避分を使う
        saved_batch = _done_batch if state == "DONE" else (_skip_batch if state == "ASSESSMENT" and action.new_state == "IDLE" else list(data.get("batch", [])))
        notification.update({
            "pj": pj,
            "old_state": state,
            "action": action,
            "gitlab": data.get("gitlab", f"{GITLAB_NAMESPACE}/{pj}"),
            "implementer": data.get("implementer", IMPLEMENTERS[0]),
            "batch": saved_batch,
            "repo_path": data.get("repo_path", ""),
            "review_mode": data.get("review_mode", "standard"),
            "keep_ctx_batch": data.get("keep_ctx_batch", False),
            "keep_ctx_intra": data.get("keep_ctx_intra", False),
            "queue_mode": _done_queue_mode if state == "DONE" else (_skip_queue_mode if state == "ASSESSMENT" and action.new_state == "IDLE" else data.get("queue_mode", False)),
            "p2_fix": data.get("p2_fix", False),
            "reviewer_number_map": data.get("reviewer_number_map"),
        })
        if _npass_timeout_notes:
            notification["_npass_timeout_notes"] = _npass_timeout_notes
        notification["skip_assessment"] = _skip_assessment
        notification["skip_batch"] = _skip_batch
        # Issue #200: 一部除外時の skipped_issues を通知に格納
        if action.skipped_issues:
            notification["skipped_issues"] = list(action.skipped_issues)

        # Issue #206: no_cc モード — 実装者に手動実装通知
        if action.new_state == "IMPLEMENTATION" and data.get("no_cc", False):
            issues_str = ", ".join(f"#{i['issue']}" for i in data.get("batch", []) if not i.get("commit"))
            notification["no_cc_msg"] = (
                f"手動実装モード（--no-cc）。\n"
                f"対象: {issues_str}\n"
                f"完了後: gokrax commit --pj {pj} --issue N --hash HASH\n"
                f"※ タイムアウトは無効化されています。"
            )

        # Issue #59: _pending_notifications — at-least-once guarantee
        pending = {}
        if action.impl_msg:
            pending["impl"] = {
                "implementer": data.get("implementer", IMPLEMENTERS[0]),
                "msg": f"[gokrax] {pj}: {action.impl_msg}",
            }
        if action.send_review:
            pending["review"] = {
                "new_state": action.new_state,
                "batch": saved_batch,
                "gitlab": data.get("gitlab", f"{GITLAB_NAMESPACE}/{pj}"),
                "repo_path": data.get("repo_path", ""),
                "review_mode": data.get("review_mode", "standard"),
                "base_commit": data.get("base_commit"),
            }
        if action.send_merge_summary:
            pending["merge_summary"] = True
        if action.run_cc:
            pending["run_cc"] = True
        if pending:
            merge_pending_notifications(data, pending, pj)

    update_pipeline(path, do_transition)

    # === ロック外で通知 ===
    if notification:
        action = notification["action"]
        pj = notification["pj"]

        if action.nudge_reviewers or action.dispute_nudge_reviewers:
            # 非アクティブなレビュアーにのみ「continue」送信（送信失敗時は次回スキップ）
            notify_path = get_path(pj)
            pipeline_data = load_pipeline(notify_path)
            state = notification.get("old_state", "")
            batch = notification.get("batch", [])
            woken = []
            failed = []

            # Determine review key based on state
            review_key = "design_reviews" if "DESIGN" in state else "code_reviews"
            is_code = "CODE" in state

            # 全催促対象レビュアーを統合（重複排除）
            all_reviewers = sorted(set(
                notification.get("nudge_reviewers", [])
                + notification.get("dispute_nudge_reviewers", [])
            ))

            for reviewer in all_reviewers:
                # 前回催促からINACTIVE_THRESHOLD_SEC未満ならスキップ（レート制限）
                nudge_key = f"_last_nudge_{reviewer}"
                last_at = pipeline_data.get(nudge_key) or pipeline_data.get(f"_nudge_failed_{reviewer}")
                if last_at:
                    try:
                        elapsed = (_datetime.now(LOCAL_TZ) - _datetime.fromisoformat(last_at)).total_seconds()
                        if elapsed < INACTIVE_THRESHOLD_SEC:
                            continue
                    except (ValueError, TypeError):
                        pass

                # このレビュアーの通常未レビュー Issue を収集
                normal_pending_issues = []
                if reviewer in notification.get("nudge_reviewers", []):
                    normal_pending_issues = [
                        item["issue"] for item in batch
                        if reviewer not in item.get(review_key, {})
                    ]

                # このレビュアーの pending dispute を収集
                dispute_items: list[tuple[int, str]] = []  # (issue番号, reason)
                if reviewer in notification.get("dispute_nudge_reviewers", []):
                    for item in batch:
                        for d in item.get("disputes", []):
                            if (d.get("reviewer") == reviewer
                                    and d.get("status") == "pending"):
                                dispute_items.append((item["issue"], d.get("reason", "(不明)")))

                # どちらもなければスキップ
                if not normal_pending_issues and not dispute_items:
                    continue

                # メッセージ組み立て（1通にまとめる）
                from notify import review_command
                from pipeline_io import get_current_round
                from config import GOKRAX_CLI
                round_num = get_current_round(pipeline_data)
                msg_parts = []

                review_module = "dev.code_review" if is_code else "dev.design_review"

                if dispute_items:
                    lines = []
                    for issue_num, reason in dispute_items:
                        lines.append(
                            f"  #{issue_num}: {reason}\n"
                            f"    {GOKRAX_CLI} review --pj {pj} --issue {issue_num} "
                            f"--reviewer {reviewer} --verdict <APPROVE/P0/P1/P2> --summary \"...\" --force"
                        )
                    msg_parts.append(render(review_module, "nudge_dispute",
                        project=pj, dispute_lines="\n".join(lines),
                    ))

                if normal_pending_issues:
                    cmd_lines = "\n".join(
                        review_command(pj, num, reviewer, round_num=round_num if round_num > 0 else None) for num in normal_pending_issues
                    )
                    msg_parts.append(render(review_module, "nudge_review",
                        project=pj,
                        issues_display=", ".join(f"#{n}" for n in normal_pending_issues),
                        cmd_lines=cmd_lines,
                    ))

                msg = "\n\n".join(msg_parts)

                if send_to_agent_queued(reviewer, msg):
                    woken.append(reviewer)
                else:
                    failed.append(reviewer)
                    log(f"[{pj}] {reviewer}: 催促送信失敗、次回スキップ")
            # 催促タイムスタンプを一括更新
            nudged = woken + failed
            if nudged:
                def _set_nudge_ts(data, reviewers=nudged, ok=woken, ng=failed):
                    for r in reviewers:
                        data[f"_last_nudge_{r}"] = _datetime.now(LOCAL_TZ).isoformat()
                    for r in ng:
                        data[f"_nudge_failed_{r}"] = _datetime.now(LOCAL_TZ).isoformat()
                update_pipeline(notify_path, _set_nudge_ts)

            if woken:
                ts = _datetime.now(LOCAL_TZ).strftime("%m/%d %H:%M")
                q_prefix = "[Queue]" if notification.get("queue_mode") else ""
                reviewers_with_ts = f"{', '.join(woken)} ({ts})"
                review_module = "dev.code_review" if is_code else "dev.design_review"
                nudge_notify = render(review_module, "notify_nudge_reviewers",
                    project=pj, reviewers=reviewers_with_ts, q_prefix=q_prefix,
                )
                log(nudge_notify)
                notify_discord(nudge_notify)
            return

        if action.nudge:
            # 状態ごとの具体的な指示メッセージ
            nudge_state = action.nudge  # e.g. "DESIGN_REVISE", "CODE_REVISE", etc.

            if nudge_state == "DESIGN_REVISE":
                nudge_msg = render("dev.design_revise", "nudge")
            elif nudge_state == "CODE_REVISE":
                nudge_msg = render("dev.code_revise", "nudge")
            elif nudge_state == "DESIGN_PLAN":
                nudge_msg = render("dev.design_plan", "nudge")
            elif nudge_state == "IMPLEMENTATION":
                nudge_msg = render("dev.implementation", "nudge")
            elif nudge_state == "CODE_TEST_FIX":
                nudge_msg = render("dev.code_test_fix", "nudge")
            elif nudge_state == "ASSESSMENT":
                nudge_msg = render("dev.assessment", "nudge", batch=data.get("batch", []))
            else:
                nudge_msg = "[Remind] 作業を進め、完了してください。"

            if action.extend_notice:
                nudge_msg += action.extend_notice
            send_to_agent_queued(notification["implementer"], nudge_msg)
            ts = _datetime.now(LOCAL_TZ).strftime("%m/%d %H:%M")
            q_prefix = "[Queue]" if notification.get("queue_mode") else ""
            notify_discord(f"{q_prefix}[{pj}] {action.nudge}: 担当者 {notification['implementer']} を催促 ({ts})")
            return

        ts = _datetime.now(LOCAL_TZ).strftime("%m/%d %H:%M")
        q_prefix = "[Queue]" if notification.get("queue_mode") else ""
        notify_discord(f"{q_prefix}[{pj}] {notification['old_state']} → {action.new_state} ({ts})")

        # NPASS timeout: GitLab note を投稿（ロック外）
        _npass_timeout_notes = notification.get("_npass_timeout_notes", [])
        if _npass_timeout_notes:
            from notify import post_gitlab_note, mask_agent_name
            gitlab = notification.get("gitlab", "")
            _rnm = notification.get("reviewer_number_map")
            for note in _npass_timeout_notes:
                masked = mask_agent_name(note["reviewer"], reviewer_number_map=_rnm)
                body = (
                    f"[gokrax] NPASS timeout: {masked} のパス "
                    f"{note['pass']}/{note['target_pass']} が未完了のため、"
                    f"承認にフォールバックしました。"
                )
                post_gitlab_note(gitlab, note["issue_num"], body)

        # ASSESSMENT遷移時: assessment 結果を GitLab note に投稿 (Issue #186)
        _RISK_LABELS = {"none": "No Risk", "low": "Low Risk", "high": "High Risk"}
        if notification.get("old_state") == "ASSESSMENT" and action.new_state in ("IMPLEMENTATION", "IDLE"):
            from notify import post_gitlab_note
            gitlab = notification.get("gitlab", "")
            _assess_batch = notification.get("skip_batch") if action.new_state == "IDLE" else notification.get("batch", [])
            for issue in _assess_batch:
                assessment = issue.get("assessment")
                if not assessment:
                    continue
                complex_level = assessment.get("complex_level", "?")
                domain_risk = assessment.get("domain_risk", "none")
                risk_label = _RISK_LABELS.get(domain_risk, "Unknown Risk")
                risk_reason = assessment.get("risk_reason", "") or ""
                summary = assessment.get("summary", "") or ""
                header = f"[gokrax] Assessment: Lvl {complex_level} / {risk_label}"
                if action.new_state == "IDLE":
                    header += " — 実装スキップ（除外条件に合致）"
                lines = [header]
                if domain_risk != "none" and risk_reason:
                    lines.append(f"Risk reason: {risk_reason}")
                if summary:
                    lines.append(summary)
                body = "\n".join(lines)
                ok = post_gitlab_note(gitlab, issue["issue"], body)
                if not ok:
                    log(f"[{pj}] WARNING: assessment note failed for issue #{issue['issue']}")

            # Issue #200: 一部除外時、除外 Issue にも assessment note を投稿（Excluded 付記）
            skipped = notification.get("skipped_issues", [])
            if skipped:
                for issue in skipped:
                    assessment = issue.get("assessment")
                    if not assessment:
                        continue
                    complex_level = assessment.get("complex_level", "?")
                    domain_risk = assessment.get("domain_risk", "none")
                    risk_label = _RISK_LABELS.get(domain_risk, "Unknown Risk")
                    risk_reason = assessment.get("risk_reason", "") or ""
                    summary = assessment.get("summary", "") or ""
                    header = f"[gokrax] Assessment: Lvl {complex_level} / {risk_label} — Excluded by risk filter"
                    lines = [header]
                    if domain_risk != "none" and risk_reason:
                        lines.append(f"Risk reason: {risk_reason}")
                    if summary:
                        lines.append(summary)
                    body = "\n".join(lines)
                    ok = post_gitlab_note(gitlab, issue["issue"], body)
                    if not ok:
                        log(f"[{pj}] WARNING: assessment note (excluded) failed for issue #{issue['issue']}")

                from notify import post_discord
                from config import DISCORD_CHANNEL
                skipped_nums = ", ".join(f"#{i['issue']}" for i in skipped if isinstance(i, dict) and "issue" in i)
                post_discord(DISCORD_CHANNEL, f"[{pj}] Excluded by risk filter: {skipped_nums}")

        # REVISE遷移時: P0サマリーを投稿
        if action.new_state in ("DESIGN_REVISE", "CODE_REVISE"):
            review_key = "design_reviews" if "DESIGN" in action.new_state else "code_reviews"
            batch = notification["batch"]
            p2_fix = notification.get("p2_fix", False)
            lines = []
            for item in batch:
                reviews = item.get(review_key, {})
                p0_reviewers = [
                    r for r, rev in reviews.items()
                    if rev.get("verdict", "").upper() in ("P0", "REJECT")
                ]
                p1_reviewers = [
                    r for r, rev in reviews.items()
                    if rev.get("verdict", "").upper() == "P1"
                ]
                p2_reviewers = [
                    r for r, rev in reviews.items()
                    if rev.get("verdict", "").upper() == "P2"
                ]
                parts = []
                if p0_reviewers:
                    parts.append(f"{len(p0_reviewers)} P0 ({', '.join(p0_reviewers)})")
                if p1_reviewers:
                    parts.append(f"{len(p1_reviewers)} P1 ({', '.join(p1_reviewers)})")
                if p2_fix and p2_reviewers:
                    parts.append(f"{len(p2_reviewers)} P2 ({', '.join(p2_reviewers)})")
                if parts:
                    lines.append(f"#{item['issue']}: {', '.join(parts)}")
            if lines:
                notify_discord(render("dev.design_revise", "notify_revise_summary",
                    project=pj, revise_lines="\n".join(lines), q_prefix=q_prefix,
                ))

        # バッチ開始時のみIssue一覧を別メッセージで通知
        if action.new_state == "DESIGN_PLAN":
            batch = notification["batch"]
            if batch:
                issue_lines = [f"#{i['issue']}: {i.get('title', '')}" for i in batch]
                notify_discord(render("dev.design_plan", "notify_issues",
                    project=pj, issue_lines="\n".join(issue_lines), q_prefix=q_prefix,
                ))

        # MERGE_SUMMARY_SENT遷移時: #gokrax にサマリーを投稿（リトライ付き）
        if action.send_merge_summary:
            from config import DISCORD_CHANNEL
            from notify import post_discord
            batch = notification["batch"]
            # automerge フラグを最新のパイプラインから読み取る (Issue #45)
            notify_path = get_path(pj)
            pipeline_data = load_pipeline(notify_path)
            automerge = pipeline_data.get("automerge", False)
            from config import MERGE_SUMMARY_FOOTER
            content = render("dev.merge_summary_sent", "format_merge_summary",
                project=pj, batch=batch, automerge=automerge,
                queue_mode=notification.get("queue_mode", False),
                MERGE_SUMMARY_FOOTER=MERGE_SUMMARY_FOOTER,
                reviewer_number_map=pipeline_data.get("reviewer_number_map"),
            )
            message_id = post_discord(DISCORD_CHANNEL, content)
            if message_id:
                # summary_message_id をパイプラインに保存
                notify_path = get_path(pj)
                def _save_summary_id(data):
                    data["summary_message_id"] = message_id
                update_pipeline(notify_path, _save_summary_id)
                log(f"[{pj}] merge summary posted (message_id={message_id})")
                clear_pending_notification(pj, "merge_summary")

                # 実装者セッションに通知 (Issue #48)
                pipeline_data_fresh = load_pipeline(notify_path)
                implementer = pipeline_data_fresh.get("implementer") or IMPLEMENTERS[0]
                prompt = render("dev.done", "batch_done",
                    project=pj, content=content,
                )
                try:
                    notify_implementer(implementer, prompt)
                    log(f"[{pj}] implementer notified: {implementer}")
                except Exception as e:
                    log(f"[{pj}] WARNING: implementer notification failed: {e}")
            else:
                # 全リトライ失敗: 遷移をロールバックして次サイクルで再試行
                log(f"[{pj}] WARNING: merge summary post failed after 3 attempts, rolling back state")
                notify_path = get_path(pj)
                old_state = notification["old_state"]
                def _rollback(data, restore=old_state):
                    data["state"] = restore
                update_pipeline(notify_path, _rollback)
                clear_pending_notification(pj, "merge_summary")

        # DONE遷移時: git push + issue close を自動実行
        if action.new_state == "DONE":
            _auto_push_and_close(
                notification.get("repo_path", ""),
                notification["gitlab"],
                notification["batch"],
                pj,
            )

        # ASSESSMENT → IDLE スキップ通知 (Issue #181)
        if notification.get("old_state") == "ASSESSMENT" and action.new_state == "IDLE":
            from notify import post_discord
            from config import DISCORD_CHANNEL
            skip_assessment = notification.get("skip_assessment", {})
            skip_batch = notification.get("skip_batch", [])
            domain_risk = skip_assessment.get("domain_risk", "none")
            issue_nums = ", ".join(f"#{i['issue']}" for i in skip_batch if isinstance(i, dict) and "issue" in i)
            skip_q_prefix = "[Queue]" if notification.get("queue_mode") else ""
            skip_msg = (
                f"{skip_q_prefix}[{pj}] ⏭️ All issues excluded by risk filter\n"
                f"Excluded issues: {issue_nums}"
            )
            post_discord(DISCORD_CHANNEL, skip_msg)

        # IDLE遷移後: キューモードのときだけ次行を自動起動 (Issue #45, #181)
        # DONE→IDLE, ASSESSMENT→IDLE(リスクスキップ) の両方をカバー
        if (action.new_state == "IDLE"
                and notification.get("queue_mode")):
            _check_queue()

        skip_reset = True  # reset_reviewers=False なら reset 未実行 → already_reset=False
        if action.reset_reviewers:
            review_mode = notification.get("review_mode", "standard")
            # keep_ctx 分岐: 遷移先に応じて参照フラグを切り替え
            if action.new_state in ("DESIGN_REVISE", "CODE_REVISE"):
                skip_reset = True  # REVISE遷移は常にスキップ
            elif action.new_state == "DESIGN_PLAN":
                skip_reset = notification.get("keep_ctx_batch", False)
            elif action.new_state == "IMPLEMENTATION":
                skip_reset = notification.get("keep_ctx_intra", False)
            else:
                skip_reset = False
            if skip_reset:
                log(f"[{pj}] reset_reviewers SKIPPED (keep_ctx for {action.new_state})")
                _reset_short_context_reviewers(review_mode)
                excluded = []
            else:
                # 実装担当も常にリセット（レビュアーと同タイミングで/new）
                impl = notification.get("implementer", "")
                log(f"[{pj}] reset_reviewers triggered: new_state={action.new_state}, impl='{impl}', review_mode={review_mode}")
                excluded = _reset_reviewers(review_mode, implementer=impl)

            # Save excluded_reviewers and min_reviews_override inside update_pipeline lock
            mode_config = REVIEW_MODES.get(review_mode, REVIEW_MODES["standard"])
            notify_path = get_path(pj)

            import random

            def _save_excluded(data: dict) -> None:
                data["excluded_reviewers"] = excluded
                # reviewer_number_map は初回のみ生成（バッチ内で安定させるため）
                if "reviewer_number_map" not in data:
                    # reviewer_number_map: バッチ参加レビュアーにランダム番号を割り当て
                    # review_config からフェーズ別メンバーの和集合を取得（フェーズ上書き対応）
                    rc = data.get("review_config", {})
                    all_phase_members: set[str] = set()
                    for phase_cfg in rc.values():
                        if isinstance(phase_cfg, dict) and "members" in phase_cfg:
                            all_phase_members.update(phase_cfg["members"])
                    # review_config 自体が存在しない場合のフォールバック（旧パイプライン互換）
                    # 注: review_config が存在するが全フェーズの members が空リストの場合は
                    # フォールバックしない（空集合は意図的な設定）。
                    if not rc:
                        all_phase_members = set(mode_config.get("members", []))
                    active_reviewers = sorted(r for r in all_phase_members if r not in excluded)
                    n = len(active_reviewers)
                    numbers = list(range(1, n + 1))
                    random.shuffle(numbers)
                    data["reviewer_number_map"] = dict(zip(active_reviewers, numbers))

                # Calculate effective reviewer count
                effective_count = len([m for m in mode_config["members"] if m not in excluded])
                min_reviews = get_min_reviews(mode_config)

                # Clamp min_reviews if deadlock would occur
                if effective_count < min_reviews:
                    clamped = max(effective_count, 0)
                    log(
                        f"[{pj}] [DEADLOCK] effective reviewers ({effective_count}) < min_reviews ({min_reviews}), clamping to {clamped}"
                    )
                    data["min_reviews_override"] = clamped
                else:
                    data.pop("min_reviews_override", None)

            update_pipeline(notify_path, _save_excluded)

        if action.impl_msg:
            phase = STATE_PHASE_MAP.get(action.new_state or "", "")
            notify_implementer(
                notification["implementer"],
                f"[gokrax] {pj}: {action.impl_msg}",
                project=pj,
                phase=phase,
            )
            clear_pending_notification(pj, "impl")
        if action.send_review:
            review_mode = notification.get("review_mode", "standard")
            prev_reviews = notification.get("prev_reviews", {})
            # Read excluded from pipeline data (not notification) to pick up _save_excluded writes
            pipeline_data = load_pipeline(get_path(pj))
            excluded = pipeline_data.get("excluded_reviewers", [])

            base_commit = pipeline_data.get("base_commit")
            from pipeline_io import get_current_round
            round_num = get_current_round(pipeline_data)
            failed = notify_reviewers(
                pj, action.new_state, notification["batch"], notification["gitlab"],
                repo_path=notification.get("repo_path", ""),
                review_mode=review_mode,
                prev_reviews=prev_reviews,
                excluded=excluded,
                base_commit=base_commit,
                comment=pipeline_data.get("comment", ""),
                round_num=round_num if round_num > 0 else None,
                already_reset=not skip_reset,  # _reset_reviewers() 実行済みなら True
            )

            # 送信失敗レビュアーを excluded_reviewers に追加（2回目の update_pipeline）
            if isinstance(failed, list) and failed:
                _failed_mode_config = REVIEW_MODES.get(review_mode, REVIEW_MODES["standard"])
                def _add_failed_to_excluded(data: dict) -> None:
                    ex = data.get("excluded_reviewers", [])
                    for r in failed:
                        if r not in ex:
                            ex.append(r)
                    data["excluded_reviewers"] = ex
                    # DEADLOCK クランプ再計算
                    effective_count = len([m for m in _failed_mode_config["members"] if m not in data["excluded_reviewers"]])
                    _min_reviews = get_min_reviews(_failed_mode_config)
                    if effective_count < _min_reviews:
                        clamped = max(effective_count, 0)
                        log(f"[{pj}] [DEADLOCK] effective reviewers ({effective_count}) < min_reviews ({_min_reviews}), clamping to {clamped}")
                        data["min_reviews_override"] = clamped
                    else:
                        data.pop("min_reviews_override", None)
                update_pipeline(get_path(pj), _add_failed_to_excluded)

            clear_pending_notification(pj, "review")
        if action.run_cc:
            try:
                _start_cc(pj, notification["batch"], notification["gitlab"],
                          notification.get("repo_path", ""), path)
            except Exception as e:
                log(f"[{pj}] _start_cc failed: {e}")
                ts = _datetime.now(LOCAL_TZ).strftime("%m/%d %H:%M")
                notify_discord(f"[{pj}] ⚠️ CC起動失敗: {e} ({ts})")
            clear_pending_notification(pj, "run_cc")

        # Issue #206: no_cc モード通知
        if notification.get("no_cc_msg"):
            notify_implementer(
                notification["implementer"],
                f"[gokrax] {pj}: {notification['no_cc_msg']}",
                project=pj,
                phase="code",
            )

        # Issue #87: CODE_TEST テスト起動
        if notification.get("run_test"):
            try:
                pipeline_data = load_pipeline(path)
                _start_code_test(pj, pipeline_data, path)
            except Exception as e:
                log(f"[{pj}] _start_code_test failed: {e}")
                def _block_test_fail(data: dict) -> None:
                    data["state"] = "BLOCKED"
                    data["enabled"] = False
                    add_history(data, "CODE_TEST", "BLOCKED", actor="watchdog")
                    _kill_code_test(data, pj)
                update_pipeline(path, _block_test_fail)
                notify_discord(f"[{pj}] ⚠️ テスト起動失敗: {e}")

        # Issue #87: CODE_TEST_FIX CC 起動
        if notification.get("run_cc_test_fix"):
            try:
                pipeline_data = load_pipeline(path)
                _start_cc_test_fix(pj, notification["batch"], pipeline_data, path)
            except Exception as e:
                log(f"[{pj}] _start_cc_test_fix failed: {e}")
                def _block_cc_fail(data: dict) -> None:
                    data["state"] = "BLOCKED"
                    data["enabled"] = False
                    add_history(data, "CODE_TEST_FIX", "BLOCKED", actor="watchdog")
                update_pipeline(path, _block_cc_fail)
                notify_discord(f"[{pj}] ⚠️ CC テスト修正起動失敗: {e}")

        # Issue #87: CODE_TEST_FIX 実装者通知（CC起動成功時のみ）
        if action.new_state == "CODE_TEST_FIX":
            pipeline_data = load_pipeline(path)
            if pipeline_data.get("state") != "CODE_TEST_FIX":
                log(f"[{pj}] skipping CODE_TEST_FIX notification: state is {pipeline_data.get('state')}")
            else:
                test_output = pipeline_data.get("test_output", "")
                retry_count = pipeline_data.get("test_retry_count", 0)
                from config import MAX_TEST_RETRY, GOKRAX_CLI as _GOKRAX_CLI
                msg = render("dev.code_test_fix", "transition",
                    project=pj, test_output=test_output,
                    retry_count=retry_count, max_retry=MAX_TEST_RETRY,
                    GOKRAX_CLI=_GOKRAX_CLI,
                )
                notify_implementer(
                    notification["implementer"],
                    f"[gokrax] {pj}: {msg}",
                    project=pj,
                    phase="code",
                )


# _stop_loop_if_idle は廃止。crontab/loop.sh は常時稼働し、
# enabledチェックは process() 内の早期returnで行う。


def _load_gokrax_state() -> dict:
    """Load gokrax-state.json or return default state."""
    from config import GOKRAX_STATE_PATH
    if not GOKRAX_STATE_PATH.exists():
        return {"last_command_message_id": "0"}
    try:
        with open(GOKRAX_STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log("WARNING: gokrax-state.json corrupt, using default")
        return {"last_command_message_id": "0"}


def _save_gokrax_state(state: dict):
    """Atomically save gokrax-state.json."""
    import tempfile
    from config import GOKRAX_STATE_PATH
    GOKRAX_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=GOKRAX_STATE_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(GOKRAX_STATE_PATH))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _handle_qrun(msg_id: str):
    """Handle Discord qrun command: pop queue entry and start project.

    Process flow:
    1. Check DRY_RUN mode (skip all actions if true)
    2. Pop next queue entry
    3. If None: post "Queue empty" to Discord
    4. Parse issues field (handle ValueError)
    5. Build argparse.Namespace for cmd_start
    6. Call cmd_start() with try-catch
    7. On exception: restore_queue_entry + post error to Discord
    8. On success: update_pipeline with queue options + post success to Discord

    Args:
        msg_id: Discord message ID (for logging only)
    """
    from config import DISCORD_CHANNEL, QUEUE_FILE
    from notify import post_discord
    from task_queue import pop_next_queue_entry, restore_queue_entry
    from gokrax import cmd_start
    from pipeline_io import update_pipeline, get_path
    import config
    import argparse

    # DRY_RUN mode: log only, skip all actions
    if config.DRY_RUN:
        log(f"[dry-run] Discord qrun command skipped (msg_id={msg_id})")
        return

    # Pop next queue entry
    entry = pop_next_queue_entry(QUEUE_FILE)

    # Handle empty queue
    if not entry:
        post_discord(DISCORD_CHANNEL, "Queue empty")
        log(f"[qrun] Queue empty (msg_id={msg_id})")
        return

    project = entry["project"]
    issues = entry["issues"]
    mode = entry.get("mode")

    # Parse issues field (defensive, parse_queue_line already validates)
    try:
        if issues == "all":
            issue_list = None
        else:
            issue_list = [int(x) for x in issues.split(",")]
    except ValueError as e:
        restore_queue_entry(QUEUE_FILE, entry["original_line"])
        error_msg = f"qrun: invalid issues format: {issues}"
        post_discord(DISCORD_CHANNEL, error_msg)
        log(f"[qrun] {error_msg} (msg_id={msg_id})")
        return

    # Build argparse.Namespace for cmd_start
    start_args = argparse.Namespace(
        project=project,
        issue=issue_list,
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

    # Call cmd_start with try-catch
    try:
        cmd_start(start_args)
    except SystemExit as e:
        # cmd_start raises SystemExit on validation errors
        restore_queue_entry(QUEUE_FILE, entry["original_line"])
        error_msg = f"qrun: failed to start {project}: {str(e)}"
        post_discord(DISCORD_CHANNEL, error_msg)
        log(f"[qrun] {error_msg} (msg_id={msg_id})")
        return
    except Exception as e:
        # Unexpected exception
        restore_queue_entry(QUEUE_FILE, entry["original_line"])
        error_msg = f"qrun: failed to start {project}: {type(e).__name__}: {str(e)}"
        post_discord(DISCORD_CHANNEL, error_msg)
        log(f"[qrun] {error_msg} (msg_id={msg_id})")
        return

    # Success: update_pipeline with queue options (same as cmd_qrun)
    path = get_path(project)

    def _save_queue_options(data):
        data["queue_mode"] = True
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
        if entry.get("skip_design"):
            data["skip_design"] = True
        if entry.get("no_cc"):
            data["no_cc"] = True
        if entry.get("exclude_high_risk"):
            data["exclude_high_risk"] = True
        if entry.get("exclude_any_risk"):
            data["exclude_any_risk"] = True
        if entry.get("allow_closed"):
            data["allow_closed"] = True

    update_pipeline(path, _save_queue_options)

    # Post success to Discord
    automerge_flag = entry.get("automerge", True)
    success_msg = f"qrun: {project} started (issues={issues}, automerge={automerge_flag})"
    post_discord(DISCORD_CHANNEL, success_msg)
    log(f"[qrun] {success_msg} (msg_id={msg_id})")


def _handle_qstatus(msg_id: str):
    from config import DISCORD_CHANNEL, QUEUE_FILE
    from notify import post_discord
    from task_queue import get_active_entries
    from gokrax import get_qstatus_text, _get_running_info
    import config

    if config.DRY_RUN:
        log(f"[dry-run] Discord qstatus command skipped (msg_id={msg_id})")
        return

    entries = get_active_entries(QUEUE_FILE)
    running = _get_running_info()
    if not entries and not running:
        post_discord(DISCORD_CHANNEL, "Queue empty")
    else:
        text = get_qstatus_text(entries, running=running)
        post_discord(DISCORD_CHANNEL, f"```\n{text}\n```")
    log(f"Processed Discord qstatus command (msg_id={msg_id})")


def _handle_qadd(msg_id: str, content: str):
    from config import DISCORD_CHANNEL, QUEUE_FILE
    from notify import post_discord
    from task_queue import append_entry, get_active_entries, parse_queue_line
    from gokrax import get_qstatus_text, _get_running_info
    import config

    if config.DRY_RUN:
        log(f"[dry-run] Discord qadd command skipped (msg_id={msg_id})")
        return

    # 1行目: "qadd PROJECT ISSUES [OPTIONS...]" → "PROJECT ISSUES [OPTIONS...]"
    # 2行目以降: そのまま（PROJECT から始まる）
    raw_lines = content.strip().split("\n")
    first_line_parts = raw_lines[0].strip().split(None, 1)
    if len(first_line_parts) < 2:
        post_discord(DISCORD_CHANNEL, "qadd: 引数が必要です (例: qadd BeamShifter 33,34 lite no-automerge)")
        return

    lines = [first_line_parts[1]]  # 1行目の "qadd" を除去した残り
    lines.extend(l.strip() for l in raw_lines[1:] if l.strip() and not l.strip().startswith("#"))

    if not lines:
        post_discord(DISCORD_CHANNEL, "qadd: 引数が必要です")
        return

    # 全行バリデーション
    for i, line in enumerate(lines, 1):
        try:
            parse_queue_line(line)
        except ValueError as e:
            post_discord(DISCORD_CHANNEL, f"qadd: 行{i} エラー: {e}")
            log(f"[qadd] Validation error line {i}: {e} (msg_id={msg_id})")
            return

    # バリデーション通過後に追加
    added = []
    for line in lines:
        try:
            append_entry(QUEUE_FILE, line)
            added.append(line)
        except ValueError as e:
            post_discord(DISCORD_CHANNEL, f"qadd: エラー: {e}")
            log(f"[qadd] Error: {e} (msg_id={msg_id})")
            return

    entries = get_active_entries(QUEUE_FILE)
    running = _get_running_info()
    text = get_qstatus_text(entries, running=running)
    added_text = "\n".join(f"  {a}" for a in added)
    post_discord(DISCORD_CHANNEL, f"Added {len(added)} entries:\n{added_text}\n```\n{text}\n```")
    log(f"Processed Discord qadd command ({len(added)} entries, msg_id={msg_id})")


def _handle_qdel(msg_id: str, content: str):
    from config import DISCORD_CHANNEL, QUEUE_FILE
    from notify import post_discord
    from task_queue import delete_entry, get_active_entries
    from gokrax import get_qstatus_text, _get_running_info
    import config

    if config.DRY_RUN:
        log(f"[dry-run] Discord qdel command skipped (msg_id={msg_id})")
        return

    parts = content.strip().split()
    if len(parts) < 2:
        post_discord(DISCORD_CHANNEL, "qdel: 引数が必要です (例: qdel last / qdel 2)")
        return

    target = parts[1]
    if target in ("last", "-1"):
        idx = "last"
    else:
        try:
            idx = int(target)
        except ValueError:
            post_discord(DISCORD_CHANNEL, f"qdel: 無効な引数 '{target}' (数値 or 'last')")
            return

    result = delete_entry(QUEUE_FILE, idx)
    if result is None:
        post_discord(DISCORD_CHANNEL, "qdel: 対象が見つからないか、キューが空です")
        log(f"[qdel] Target not found (msg_id={msg_id})")
        return

    orig = result.get("original_line", "?")
    entries = get_active_entries(QUEUE_FILE)
    running = _get_running_info()
    if entries or running:
        text = get_qstatus_text(entries, running=running)
        post_discord(DISCORD_CHANNEL, f"Deleted: {orig}\n```\n{text}\n```")
    else:
        post_discord(DISCORD_CHANNEL, f"Deleted: {orig}\nQueue empty")
    log(f"Processed Discord qdel command (msg_id={msg_id})")


def _handle_qedit(msg_id: str, content: str):
    from config import DISCORD_CHANNEL, QUEUE_FILE
    from notify import post_discord
    from task_queue import replace_entry, get_active_entries
    from gokrax import get_qstatus_text, _get_running_info
    import config

    if config.DRY_RUN:
        log(f"[dry-run] Discord qedit command skipped (msg_id={msg_id})")
        return

    parts = content.strip().split(None, 2)
    if len(parts) < 3:
        post_discord(DISCORD_CHANNEL, "qedit: 引数が必要です (例: qedit 1 gokrax 105 full ...)")
        return

    target = parts[1]
    new_line = parts[2]

    if target in ("last", "-1"):
        idx = "last"
    else:
        try:
            idx = int(target)
        except ValueError:
            post_discord(DISCORD_CHANNEL, f"qedit: 無効な引数 '{target}' (数値 or 'last')")
            return

    try:
        result = replace_entry(QUEUE_FILE, idx, new_line)
    except ValueError as e:
        post_discord(DISCORD_CHANNEL, f"qedit: エラー: {e}")
        return

    if result is None:
        post_discord(DISCORD_CHANNEL, "qedit: 対象が見つからないか、キューが空です")
        return

    entries = get_active_entries(QUEUE_FILE)
    running = _get_running_info()
    text = get_qstatus_text(entries, running=running)
    post_discord(DISCORD_CHANNEL, f"Replaced [{target}]: {new_line}\n```\n{text}\n```")
    log(f"Processed Discord qedit command (msg_id={msg_id})")


DISCORD_COMMANDS = ("status", "qrun", "qstatus", "qadd", "qdel", "qedit")


def check_discord_commands():
    """Check #gokrax for commands from M and respond.

    Process flow:
    1. Load last_command_message_id from gokrax-state.json
    2. Fetch latest 10 messages from #gokrax
    3. Filter: author in ALLOWED_COMMAND_USER_IDS, not gokrax bot, first word in DISCORD_COMMANDS
    4. Filter: message_id > last_command_message_id
    5. Process in chronological order (oldest → newest)
    6. For each: handle command, update last_command_message_id
    """
    from config import DISCORD_CHANNEL, ALLOWED_COMMAND_USER_IDS, ANNOUNCE_BOT_USER_ID
    from notify import fetch_discord_latest, post_discord
    from gokrax import get_status_text
    import config

    # 1. Load state
    state = _load_gokrax_state()
    last_id_raw = state.get("last_command_message_id", "0")
    try:
        last_id = int(last_id_raw)
    except (ValueError, TypeError):
        log(f"[discord-commands] Invalid last_command_message_id={last_id_raw!r}, self-healing")
        # Self-heal: メッセージを fetch して数値として妥当な最新IDで state を修復
        messages = fetch_discord_latest(DISCORD_CHANNEL, 10)
        if messages:
            # 最新メッセージから順に、数値として妥当なIDを探す
            healed = False
            for heal_msg in messages:
                heal_id = heal_msg.get("id")
                if heal_id is None:
                    continue
                try:
                    int(heal_id)  # 数値として妥当か検証
                except (ValueError, TypeError):
                    continue
                state["last_command_message_id"] = heal_id
                _save_gokrax_state(state)
                log(f"[discord-commands] Self-healed last_command_message_id to {heal_id}")
                healed = True
                break
            if not healed:
                log("[discord-commands] Self-heal failed: no valid message id found in fetched messages")
        return

    # 2. Fetch latest messages
    messages = fetch_discord_latest(DISCORD_CHANNEL, 10)
    if not messages:
        return  # API failure or empty channel, skip this cycle

    # 3. Filter messages
    candidates = []
    for msg in messages:
        author_id = msg.get("author", {}).get("id")
        content = msg.get("content", "")
        msg_id = msg.get("id")

        content_lower = content.strip().lower()
        cmd_word = content_lower.split()[0] if content_lower else ""
        # Filter: from M, not from bot, first word is a known command
        if (author_id in ALLOWED_COMMAND_USER_IDS and
            author_id != ANNOUNCE_BOT_USER_ID and
            cmd_word in DISCORD_COMMANDS and
            msg_id):
            try:
                msg_id_int = int(msg_id)
            except (ValueError, TypeError):
                log(f"[discord-commands] Skipping message with invalid id: msg_id={msg_id!r}")
                continue
            if msg_id_int > last_id:
                candidates.append(msg)

    # 4. Process in chronological order (reversed, API returns newest first)
    for msg in reversed(candidates):
        msg_id = msg["id"]
        content = msg["content"]
        content_lower = content.strip().lower()

        # 5. Route to appropriate handler
        parts = content_lower.split()
        if not parts:
            continue
        cmd_word = parts[0]

        if cmd_word == "status":
            status = get_status_text(enabled_only=True)
            response = f"```\n{status}\n```"

            if config.DRY_RUN:
                log(f"[dry-run] Discord status command response skipped (msg_id={msg_id})")
            else:
                post_discord(DISCORD_CHANNEL, response)

        elif cmd_word == "qrun":
            _handle_qrun(msg_id)

        elif cmd_word == "qstatus":
            _handle_qstatus(msg_id)

        elif cmd_word == "qadd":
            _handle_qadd(msg_id, content)

        elif cmd_word == "qdel":
            _handle_qdel(msg_id, content)

        elif cmd_word == "qedit":
            _handle_qedit(msg_id, content)

        # 6. Update state (even in dry-run to test deduplication)
        state["last_command_message_id"] = msg_id
        _save_gokrax_state(state)
        log(f"Processed Discord {cmd_word} command (msg_id={msg_id})")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
    )

    # Check Discord commands BEFORE pipeline processing
    # Works even if PIPELINES_DIR doesn't exist
    try:
        check_discord_commands()
    except Exception as e:
        log(f"[discord-commands] ERROR: {e}")

    # Early exit if no pipelines
    if not PIPELINES_DIR.exists():
        return

    # Process pipelines
    for path in sorted(PIPELINES_DIR.glob("*.json")):
        try:
            process(path)
        except Exception as e:
            log(f"[{path.stem}] ERROR: {e}")


if __name__ == "__main__":
    main()
