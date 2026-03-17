#!/usr/bin/env python3
"""devbar-watchdog.py — LLM不要のパイプラインオーケストレーター

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
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    PIPELINES_DIR, JST, LOG_FILE, REVIEW_MODES, CC_MODEL_PLAN, CC_MODEL_IMPL,
    DEVBAR_CLI, INACTIVE_THRESHOLD_SEC, SESSIONS_BASE,
    STATE_PHASE_MAP,
    # WATCHDOG_LOOP_PIDFILE, WATCHDOG_LOOP_CRON_MARKER は devbar.py の enable/disable 専用
)
from config import (
    SPEC_STATES, SPEC_REVIEW_TIMEOUT_SEC, SPEC_REVISE_TIMEOUT_SEC,
    SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC, SPEC_ISSUE_PLAN_TIMEOUT_SEC, SPEC_QUEUE_PLAN_TIMEOUT_SEC,
    MAX_SPEC_RETRIES, SPEC_REVISE_SELF_REVIEW_PASSES, SPEC_REVIEW_RAW_RETENTION_DAYS,
)
from datetime import datetime as _datetime
from datetime import timedelta as _timedelta
import json as _json
from pipeline_io import (
    load_pipeline, update_pipeline, get_path,
    add_history, now_iso, find_issue,
    clear_pending_notification,
    ensure_spec_reviews_dir,
)
from notify import (
    notify_implementer, notify_reviewers, notify_discord,
    send_to_agent, send_to_agent_queued, ping_agent,
)
from messages import render
from spec_review import (
    should_continue_review, _reset_review_requests,
    parse_review_yaml, validate_received_entry,
)

# Issue #92: pytest ベースライン定数
PYTEST_BASELINE_TIMEOUT_SEC = 300   # 5分
MAX_BASELINE_OUTPUT_CHARS   = 50_000
MAX_BASELINE_EMBED_CHARS    = 30_000


def _reset_reviewers(review_mode: str = "standard", implementer: str = "") -> list[str]:
    """レビュアー（+実装担当）に /new を先行送信（collectキュー経由）。free tier をping確認。

    Args:
        review_mode: Review mode to determine member list
        implementer: Implementer agent ID (if DESIGN_PLAN state)

    Returns:
        List of excluded reviewer names (those who failed ping check)
    """
    from config import AGENTS, REVIEW_MODES, POST_NEW_COMMAND_WAIT_SEC
    from notify import ping_agent
    import config
    import time

    mode_config = REVIEW_MODES.get(review_mode, REVIEW_MODES["standard"])
    targets = set(mode_config["members"])
    if implementer:
        targets.add(implementer)

    log(f"[/new] reset_reviewers: mode={review_mode}, impl='{implementer}', targets={sorted(targets)}")

    # Send /new to all targets
    sent_impl = False
    for r in targets:
        if r in AGENTS:
            log(f"[/new] sending /new to {r}")
            if not send_to_agent_queued(r, "/new"):
                log(f"[/new] WARNING: failed to send /new to {r}")
            if r == implementer:
                sent_impl = True
        else:
            log(f"[/new] SKIP {r} (not in AGENTS)")

    # Wait for session reset completion
    if sent_impl or targets:
        log(f"[/new] waiting {POST_NEW_COMMAND_WAIT_SEC} sec for session reset")
        from config import DRY_RUN
        if not DRY_RUN:
            time.sleep(POST_NEW_COMMAND_WAIT_SEC)

    # Ping free tier reviewers
    excluded = []

    # Check if any free tier members exist in this mode
    free_members = [m for m in mode_config["members"] if config.get_tier(m) == "free"]
    if not free_members:
        log("[/new] no free tier members in mode, skipping ping")
        return excluded

    for reviewer in free_members:
        if not ping_agent(reviewer):
            log(f"[/new] WARNING: free tier reviewer {reviewer} failed ping, excluding")
            excluded.append(reviewer)

    if excluded:
        log(f"[/new] excluded {len(excluded)} reviewers: {excluded}")

    return excluded


def _reset_short_context_reviewers(review_mode: str) -> None:
    """keep_ctx スキップ時でも short-context tier のレビュアーだけ /new を送信。"""
    from config import AGENTS, REVIEW_MODES, POST_NEW_COMMAND_WAIT_SEC
    import config
    import time

    mode_config = REVIEW_MODES.get(review_mode)
    if mode_config is None:
        log(f"[/new] WARNING: unknown review_mode '{review_mode}', skipping short-context reset")
        return
    short_ctx = [m for m in mode_config["members"]
                 if config.get_tier(m) == "short-context" and m in AGENTS]
    if not short_ctx:
        return
    for r in short_ctx:
        log(f"[/new] sending /new to {r} (short-context, forced)")
        if not send_to_agent_queued(r, "/new"):
            log(f"[/new] WARNING: failed to send /new to {r}")
    from config import DRY_RUN
    if not DRY_RUN:
        log(f"[/new] waiting {POST_NEW_COMMAND_WAIT_SEC} sec for short-context reset")
        time.sleep(POST_NEW_COMMAND_WAIT_SEC)


def log(msg: str):
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    # crontabの >> リダイレクトと直接書き込みの二重出力を防止
    # ファイルのみに書き込み、stdoutには出さない
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _is_ok_reply(content: str) -> bool:
    """マージサマリーへのOK返信を判定。ok, OK, おk, おｋ 等に対応。"""
    s = content.strip().lower()
    return s.startswith("ok") or s.startswith("おk") or s.startswith("おｋ")


def count_reviews(batch: list, key: str) -> tuple:
    """(最小レビュー数, P0有無, P1有無, P2有無)"""
    min_n = min((len(i.get(key, {})) for i in batch), default=0)
    has_p0 = any(
        r.get("verdict", "").upper() in ("REJECT", "P0")
        for i in batch for r in i.get(key, {}).values()
    )
    has_p1 = any(
        r.get("verdict", "").upper() == "P1"
        for i in batch for r in i.get(key, {}).values()
    )
    has_p2 = any(
        r.get("verdict", "").upper() == "P2"
        for i in batch for r in i.get(key, {}).values()
    )
    return min_n, has_p0, has_p1, has_p2


def _awaiting_dispute_re_review(
    batch: list, review_key: str, excluded: list[str] | None = None,
) -> list[str]:
    """dispute 後に再レビューを出していないレビュアーのリストを返す。

    対象:
    - status == "pending": 常に awaiting（未解決）
    - status == "accepted": review が無い or review.at < dispute.resolved_at → awaiting
    - status == "rejected": 対象外（元の判定維持）

    excluded に含まれるレビュアーは対象外（レート制限等で除外済み）。
    """
    excluded_set = set(excluded or [])
    awaiting = set()
    for issue in batch:
        for d in issue.get("disputes", []):
            status = d.get("status", "")
            if status not in ("pending", "accepted"):
                continue
            reviewer = d.get("reviewer", "")
            if reviewer in excluded_set:
                continue
            phase = d.get("phase", "")
            expected_key = "design_reviews" if phase == "design" else "code_reviews"
            if expected_key != review_key:
                continue
            if status == "pending":
                awaiting.add(reviewer)
                continue
            # accepted: review の有無と時刻で判定
            resolved_at = d.get("resolved_at", "")
            review = issue.get(review_key, {}).get(reviewer)
            if review is None:
                awaiting.add(reviewer)
            else:
                review_at = review.get("at", "")
                try:
                    from datetime import datetime
                    r_dt = datetime.fromisoformat(review_at)
                    d_dt = datetime.fromisoformat(resolved_at)
                    if r_dt < d_dt:
                        awaiting.add(reviewer)
                except (ValueError, TypeError):
                    if review_at < resolved_at:
                        awaiting.add(reviewer)
    return sorted(awaiting)


def _revise_target_issues(batch: list, review_key: str, revised_key: str, p2_fix: bool = False) -> str:
    """REVISE対象Issueを文字列化。P0/REJECT/P1が付いた未修正Issueを明示。p2_fix時はP2も含む。

    対象が特定できない場合（flag差し込み等）は全Issueをフォールバックで返す。
    """
    targets = []
    for i in batch:
        if i.get(revised_key):
            continue
        reviews = i.get(review_key, {})
        has_p0 = any(
            r.get("verdict", "").upper() in ("REJECT", "P0")
            for r in reviews.values()
        )
        has_p1 = any(
            r.get("verdict", "").upper() == "P1"
            for r in reviews.values()
        )
        has_p2 = any(
            r.get("verdict", "").upper() == "P2"
            for r in reviews.values()
        )
        if has_p0 or has_p1 or (p2_fix and has_p2):
            targets.append(f"#{i['issue']}")
    return ", ".join(targets) if targets else ", ".join(f"#{i['issue']}" for i in batch)


def clear_reviews(batch: list, key: str, revised_key: str):
    """P0/REJECT/P1を出したレビュアーのレビューのみクリアする。
    APPROVEのレビューは保持。
    revised_key は全Issueから削除（次のREVISEサイクル用）。
    """
    for issue in batch:
        reviews = issue.get(key, {})
        to_clear = [
            reviewer for reviewer, r in reviews.items()
            if r.get("verdict", "").upper() in ("REJECT", "P0", "P1", "P2")
        ]
        for reviewer in to_clear:
            del reviews[reviewer]
        issue.pop(revised_key, None)


# BLOCKEDまでの時間 (秒)
from config import BLOCK_TIMERS, NUDGE_GRACE_SEC, EXTENDABLE_STATES, EXTEND_NOTICE_THRESHOLD


@dataclass
class TransitionAction:
    """check_transition() の返り値。new_state が None なら遷移不要。"""
    new_state: str | None = None
    impl_msg: str | None = None
    send_review: bool = False
    reset_reviewers: bool = False  # レビュアーに /new を先行送信
    send_merge_summary: bool = False  # #dev-bar にマージサマリーを投稿
    run_cc: bool = False  # CC CLI を直接起動
    nudge: str | None = None   # 催促通知が必要な状態名
    nudge_reviewers: list | None = None  # 催促が必要なレビュアーのリスト
    dispute_nudge_reviewers: list | None = None  # dispute pending で催促が必要なレビュアー
    extend_notice: str | None = None  # タイムアウト延長案内メッセージ
    save_grace_met_at: str | None = None  # grace met_atをpipelineに保存する必要がある場合のキー名


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


def _get_state_entered_at(data: dict, state: str) -> _datetime | None:
    """historyから指定stateに遷移した最新時刻を取得。"""
    for entry in reversed(data.get("history", [])):
        if entry.get("to") == state:
            try:
                return _datetime.fromisoformat(entry["at"])
            except (KeyError, ValueError):
                return None
    return None


def _nudge_key(state: str) -> str:
    """状態ごとの催促カウンタキー名。"""
    return f"{state.lower()}_notify_count"


def _check_nudge(state: str, data: dict) -> TransitionAction | None:
    """催促/BLOCKED判定。該当しなければNone。"""
    block_sec = BLOCK_TIMERS.get(state)
    if not block_sec or data is None:
        return None

    # 延長分を加算
    block_sec += data.get("timeout_extension", 0)

    entered_at = _get_state_entered_at(data, state)
    elapsed = 0.0
    if entered_at is not None:
        elapsed = (_datetime.now(JST) - entered_at).total_seconds()

    # BLOCKED判定（時間超過）
    if elapsed >= block_sec:
        return TransitionAction(
            new_state="BLOCKED",
            impl_msg=f"{state} タイムアウト。応答がありませんでした。",
        )

    # 猶予期間内は催促しない
    if elapsed < NUDGE_GRACE_SEC:
        return None

    # 催促メッセージ作成
    nudge = TransitionAction(nudge=state)

    # 延長案内を付加（残り5分未満 + 対象フェーズ）
    remaining = block_sec - elapsed
    if remaining < EXTEND_NOTICE_THRESHOLD and state in EXTENDABLE_STATES:
        project = data.get("project", "")
        extend_count = data.get("extend_count", 0)
        max_extends = 2
        if extend_count < max_extends:
            nudge.extend_notice = (
                f"\n\n⏰ タイムアウトまで残り{int(remaining)}秒（延長残り{max_extends - extend_count}回）。延長が必要なら:\n"
                f"{DEVBAR_CLI} extend --project {project} --by 600"
            )
        else:
            nudge.extend_notice = (
                f"\n\n⏰ タイムアウトまで残り{int(remaining)}秒。延長上限に達しています。"
            )

    return nudge



def get_notification_for_state(
    state: str,
    project: str = "",
    batch: list | None = None,
    gitlab: str = "",
    implementer: str = "",
    p2_fix: bool = False,
    comment: str = "",
) -> TransitionAction:
    """全状態の通知メッセージを一元管理。

    遷移通知（初回）にも催促（nudge）にも使う。ただし現状の催促は "continue" のみ。
    check_transition(), cmd_transition(), 催促処理 から呼ばれる共通ロジック。
    通知不要な状態は TransitionAction() を返す。
    """
    batch = batch or []

    if state in ("DESIGN_REVIEW", "CODE_REVIEW"):
        return TransitionAction(send_review=True)

    from config import OWNER_NAME
    comment_line = f"{OWNER_NAME}からの要望: {comment}\n" if comment else ""

    if state == "DESIGN_PLAN":
        issues_str = ", ".join(
            f"#{i['issue']}" for i in batch if not i.get("design_ready")
        ) or "（全Issue）"
        msg = render("dev.design_plan", "transition",
            project=project, issues_str=issues_str,
            comment_line=comment_line, DEVBAR_CLI=DEVBAR_CLI,
        )
        return TransitionAction(impl_msg=msg, reset_reviewers=True)

    if state == "DESIGN_REVISE":
        issues_str = _revise_target_issues(batch, "design_reviews", "design_revised", p2_fix=p2_fix)
        p2_note = ""
        if p2_fix:
            p2_note = "\n⚠️ --p2-fix モード: P2 指摘も全件修正が必要です。P0/P1 がなくても P2 が残っていれば再度 REVISE に差し戻されます。\n"
        fix_label = "P0/P1/P2指摘" if p2_fix else "P0/P1指摘"
        msg = render("dev.design_revise", "transition",
            project=project, issues_str=issues_str, comment_line=comment_line,
            fix_label=fix_label, p2_note=p2_note, DEVBAR_CLI=DEVBAR_CLI,
        )
        return TransitionAction(impl_msg=msg)

    if state == "CODE_REVISE":
        issues_str = _revise_target_issues(batch, "code_reviews", "code_revised", p2_fix=p2_fix)
        p2_note = ""
        if p2_fix:
            p2_note = "\n⚠️ --p2-fix モード: P2 指摘も全件修正が必要です。P0/P1 がなくても P2 が残っていれば再度 REVISE に差し戻されます。\n"
        fix_label = "P0/P1/P2指摘" if p2_fix else "P0/P1指摘"
        msg = render("dev.code_revise", "transition",
            project=project, issues_str=issues_str, comment_line=comment_line,
            fix_label=fix_label, p2_note=p2_note, DEVBAR_CLI=DEVBAR_CLI,
        )
        return TransitionAction(impl_msg=msg)

    # 現在はシステム側でCCを動かしているため使っていないが、残しておく
    if state == "IMPLEMENTATION":
        return TransitionAction(run_cc=True, reset_reviewers=True)

    return TransitionAction()


def _resolve_review_outcome(
    state: str, data: dict | None, batch: list,
    has_p0: bool, has_p1: bool, has_p2: bool,
    comment: str = "",
) -> TransitionAction:
    """min_reviews 到達後の遷移先を決定する。APPROVED or REVISE or BLOCKED.

    義務レベル:
    - P0: 常に義務。max cycles → BLOCKED。
    - P1: 常に義務。max cycles → APPROVE フォールバック（免除）。
    - P2: p2_fix 有効時のみ義務。max cycles → APPROVE フォールバック（免除）。
    """
    appr = "DESIGN_APPROVED" if "DESIGN" in state else "CODE_APPROVED"
    revise_state = "DESIGN_REVISE" if "DESIGN" in state else "CODE_REVISE"
    pj = data.get("project", "") if data else ""
    # p1_fix → p2_fix 昇格（後方互換）
    p2_fix = (data.get("p2_fix", False) or data.get("p1_fix", False)) if data else False

    # P0 or P1 あり → REVISE or BLOCKED/フォールバック
    if has_p0 or has_p1:
        from config import MAX_REVISE_CYCLES, OWNER_NAME
        counter_key = "design_revise_count" if "DESIGN" in state else "code_revise_count"
        current_count = data.get(counter_key, 0) if data else 0

        if current_count >= MAX_REVISE_CYCLES:
            severity = "P0" if has_p0 else "P1"
            # P0/P1 あり → BLOCKED（いずれも免除しない）
            return TransitionAction(
                new_state="BLOCKED",
                impl_msg=render("dev.blocked", "blocked_max_cycles",
                    state=state, MAX_REVISE_CYCLES=MAX_REVISE_CYCLES,
                    OWNER_NAME=OWNER_NAME, severity=severity,
                ),
            )

        return TransitionAction(
            new_state=revise_state,
            impl_msg=get_notification_for_state(revise_state, project=pj, batch=batch, p2_fix=p2_fix, comment=comment).impl_msg,
        )

    # P0/P1 なし + p2_fix 有効 + P2 あり → REVISE（max_revise_cycles フォールバック付き）
    if p2_fix and has_p2:
        from config import MAX_REVISE_CYCLES
        counter_key = "design_revise_count" if "DESIGN" in state else "code_revise_count"
        current_count = data.get(counter_key, 0) if data else 0

        if current_count >= MAX_REVISE_CYCLES:
            # フォールバック: P0/P1 がないので APPROVE する
            return TransitionAction(
                new_state=appr,
                impl_msg=get_notification_for_state(appr, project=pj, batch=batch, comment=comment).impl_msg,
            )

        return TransitionAction(
            new_state=revise_state,
            impl_msg=get_notification_for_state(revise_state, project=pj, batch=batch, p2_fix=True, comment=comment).impl_msg,
        )

    # P0/P1/P2 なし or (P2 ありだが p2_fix 無効) → APPROVE
    return TransitionAction(
        new_state=appr,
        impl_msg=get_notification_for_state(appr, project=pj, batch=batch, comment=comment).impl_msg,
    )


def check_transition(state: str, batch: list, data: dict | None = None) -> TransitionAction:
    """現在の状態とバッチから次の遷移アクションを決定する純粋関数。副作用なし。"""
    if state in ("IDLE", "TRIAGE", "BLOCKED"):
        return TransitionAction()

    if state == "MERGE_SUMMARY_SENT":
        if data is None:
            return TransitionAction()

        # Automerge: skip approval wait (Issue #45)
        if data.get("automerge", False):
            return TransitionAction(new_state="DONE")

        # Manual merge: wait for M's OK
        from notify import fetch_discord_replies
        from config import M_DISCORD_USER_ID, DISCORD_CHANNEL
        summary_id = data.get("summary_message_id")
        if not summary_id:
            return TransitionAction()
        messages = fetch_discord_replies(DISCORD_CHANNEL, summary_id)
        for msg in messages:
            ref = msg.get("message_reference", {})
            if (ref.get("message_id") == summary_id
                    and msg.get("author", {}).get("id") == M_DISCORD_USER_ID
                    and _is_ok_reply(msg.get("content", ""))):
                return TransitionAction(new_state="DONE")
        return TransitionAction()

    if state == "DONE":
        # DONE→IDLEは自動遷移。通知不要（push+closeは呼び出し元で処理済み）
        return TransitionAction(new_state="IDLE")

    if not batch:
        return TransitionAction()

    if state == "DESIGN_PLAN":
        if all(i.get("design_ready") for i in batch):
            return TransitionAction(new_state="DESIGN_REVIEW", send_review=True)
        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    if state in ("DESIGN_REVIEW", "CODE_REVIEW"):
        key = "design_reviews" if "DESIGN" in state else "code_reviews"
        appr = "DESIGN_APPROVED" if "DESIGN" in state else "CODE_APPROVED"
        revise_state = "DESIGN_REVISE" if "DESIGN" in state else "CODE_REVISE"
        review_mode = data.get("review_mode", "standard") if data else "standard"

        # "skip" mode: 即座に承認状態へ遷移
        if review_mode == "skip":
            return TransitionAction(
                new_state=appr,
                impl_msg=f"[review_mode=skip] 自動承認: {appr}",
            )

        mode_config = REVIEW_MODES.get(review_mode, REVIEW_MODES["standard"])
        min_rev = data.get("min_reviews_override", mode_config["min_reviews"]) if data else mode_config["min_reviews"]
        excluded = data.get("excluded_reviewers", []) if data else []
        effective_count = len(mode_config["members"]) - len(excluded)
        grace_sec = mode_config.get("grace_period_sec", 0)

        count, has_p0, has_p1, has_p2 = count_reviews(batch, key)

        # Check for unresolved P0 flags (flag P0 → immediate REVISE)
        flag_phase = STATE_PHASE_MAP.get(state)
        if flag_phase is not None:
            has_flag_p0 = any(
                f.get("verdict") == "P0"
                and not f.get("resolved")
                and f.get("phase") == flag_phase
                for issue in batch
                for f in issue.get("flags", [])
            )
            if has_flag_p0:
                revise_state = "DESIGN_REVISE" if "DESIGN" in state else "CODE_REVISE"
                log(f"[FLAG] unresolved P0 flag(s) in {flag_phase} phase → {revise_state}")
                return TransitionAction(new_state=revise_state, send_review=False)
        else:
            # Defensive: should never happen (all REVIEW states are in STATE_PHASE_MAP)
            log(f"[FLAG] WARNING: unknown state {state} in REVIEW block, skipping flag check")

        # Check if min_reviews reached
        if count >= min_rev:
            met_key = f"{'design' if 'DESIGN' in state else 'code'}_min_reviews_met_at"
            if data and not data.get(met_key):
                data[met_key] = datetime.now(JST).isoformat()
                log(
                    f"[GRACE] min_reviews={min_rev} met at {data[met_key]}, effective={effective_count}, grace={grace_sec} sec"
                )

            # Determine if we should transition now
            should_transition = False

            # Case 1: All effective reviewers done → immediate
            if count >= effective_count:
                log(f"[GRACE] all {effective_count} effective reviewers done, transitioning")
                should_transition = True

            # Case 2: Grace period check
            elif min_rev < effective_count and grace_sec > 0 and data and data.get(met_key):
                from datetime import timedelta
                met_at = datetime.fromisoformat(data[met_key])
                elapsed = (datetime.now(JST) - met_at).total_seconds()
                if elapsed >= grace_sec:
                    log(f"[GRACE] grace period expired ({elapsed:.1f}s >= {grace_sec}s), transitioning")
                    should_transition = True
                else:
                    log(f"[GRACE] waiting ({grace_sec - elapsed:.1f}s remaining)")
                    return TransitionAction(save_grace_met_at=met_key)

            # Case 3: No grace (min == effective or grace_sec == 0) → immediate
            else:
                log(f"[GRACE] no grace period, transitioning")
                should_transition = True

            if should_transition:
                awaiting = _awaiting_dispute_re_review(batch, key, excluded=excluded)
                if awaiting:
                    log(f"[DISPUTE] waiting for re-review from: {', '.join(awaiting)}")
                    should_transition = False

            if should_transition:
                if data:
                    data.pop(met_key, None)
                comment = data.get("comment", "") if data else ""
                return _resolve_review_outcome(state, data, batch, has_p0, has_p1, has_p2, comment=comment)

        # Not enough reviews yet
        # 1. タイムアウト判定（BLOCKEDのみ早期リターン）
        nudge = _check_nudge(state, data) if data is not None else None
        if nudge and nudge.new_state == "BLOCKED":
            return nudge
        # 2. 未完了レビュアーの催促（猶予期間内はスキップ）
        entered_at = _get_state_entered_at(data, state) if data is not None else None
        if entered_at is not None:
            elapsed = (_datetime.now(JST) - entered_at).total_seconds()
            if elapsed < NUDGE_GRACE_SEC:
                return TransitionAction()
        # 3. レビュアー催促（最低優先）
        excluded = data.get("excluded_reviewers", []) if data else []
        pending = _get_pending_reviewers(batch, key, mode_config["members"], excluded=excluded)
        dispute_awaiting = _awaiting_dispute_re_review(batch, key, excluded=excluded)
        if pending or dispute_awaiting:
            return TransitionAction(
                nudge="REVIEW",
                nudge_reviewers=sorted(pending) or None,
                dispute_nudge_reviewers=sorted(dispute_awaiting) or None,
            )
        return TransitionAction()

    if state == "DESIGN_APPROVED":
        return TransitionAction(
            new_state="IMPLEMENTATION",
            run_cc=True,
            reset_reviewers=True,
        )

    if state == "CODE_APPROVED":
        return TransitionAction(
            new_state="MERGE_SUMMARY_SENT",
            send_merge_summary=True,
        )

    if state in ("DESIGN_REVISE", "CODE_REVISE"):
        review_key = "design_reviews" if "DESIGN" in state else "code_reviews"
        revised_key = "design_revised" if "DESIGN" in state else "code_revised"
        review_state = "DESIGN_REVIEW" if "DESIGN" in state else "CODE_REVIEW"
        # p1_fix → p2_fix 昇格（後方互換）
        p2_fix = (data.get("p2_fix", False) or data.get("p1_fix", False)) if data else False

        flag_phase = STATE_PHASE_MAP.get(state)
        all_done = True
        for issue in batch:
            reviews = issue.get(review_key, {})

            has_p0 = any(
                r.get("verdict", "").upper() in ("REJECT", "P0")
                for r in reviews.values()
            )
            has_p1 = any(
                r.get("verdict", "").upper() == "P1"
                for r in reviews.values()
            )
            has_p2 = any(
                r.get("verdict", "").upper() == "P2"
                for r in reviews.values()
            )

            has_unresolved_flag = False
            if flag_phase is not None:
                has_unresolved_flag = any(
                    f.get("verdict") == "P0"
                    and not f.get("resolved")
                    and f.get("phase") == flag_phase
                    for f in issue.get("flags", [])
                )

            needs_revision = has_p0 or has_p1 or (p2_fix and has_p2) or has_unresolved_flag
            if needs_revision and not issue.get(revised_key):
                all_done = False
                break

        if all_done:
            log(
                f"[REVISE] all issues with P0/P1{'/P2' if p2_fix else ''} revised, transitioning to {review_state}"
            )
            return TransitionAction(new_state=review_state, send_review=True)

        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    if state == "IMPLEMENTATION":
        # 1. 完了判定（最優先）
        if all(i.get("commit") for i in batch):
            return TransitionAction(new_state="CODE_REVIEW", send_review=True)
        # 2. CC未実行 → 起動指示
        if data is not None and not _is_cc_running(data):
            return TransitionAction(run_cc=True)
        # 3. CC実行中だが進捗なし → タイムアウト判定
        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    return TransitionAction()


def _start_cc(project: str, batch: list, gitlab: str, repo_path: str, pipeline_path: Path) -> None:
    """CC を非同期起動し、PID を記録。"""
    import subprocess as _sub
    import uuid as _uuid
    import os
    import tempfile
    from notify import fetch_issue_body

    # CC モデル指定を pipeline JSON から読み取る (Issue #45)
    data = load_pipeline(pipeline_path)
    skip_plan = data.get("skip_cc_plan", False)
    log(f"[{project}] _start_cc: skip_cc_plan={skip_plan}")

    # base_commit フォールバック: DESIGN_PLAN 遷移で記録されていない場合のみ
    if not data.get("base_commit") and repo_path:
        try:
            result = _sub.run(
                ["git", "-C", repo_path, "log", "--format=%H", "-1"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                base = result.stdout.strip()
                def _set_base(d):
                    if not d.get("base_commit"):
                        d["base_commit"] = base
                update_pipeline(pipeline_path, _set_base)
                log(f"[{project}] base_commit fallback recorded: {base[:7]}")
        except Exception as e:
            log(f"[{project}] WARNING: failed to record base_commit: {e}")

    plan_model = data.get("cc_plan_model") or CC_MODEL_PLAN
    impl_model = data.get("cc_impl_model") or CC_MODEL_IMPL
    q_tag = "[Queue]" if data.get("queue_mode") else ""

    # keep_ctx_batch: 前バッチの cc_session_id を再利用 (Issue #58)
    prev_session = data.get("cc_session_id") if data.get("keep_ctx_batch") else None
    session_id = prev_session or str(_uuid.uuid4())

    # Issue本文を収集
    issue_nums: list[int] = []
    issue_texts: list[str] = []
    for item in batch:
        if item.get("commit"):
            continue
        num = item["issue"]
        issue_nums.append(num)
        body = fetch_issue_body(num, gitlab) or f"(Issue #{num} の本文取得失敗)"
        issue_texts.append(f"### Issue #{num}: {item.get('title', '')}\n{body}")

    if not issue_nums:
        return

    issues_block = "\n\n".join(issue_texts)
    closes = " ".join(f"Closes #{n}" for n in issue_nums)
    issue_args = " ".join(str(n) for n in issue_nums)

    comment = data.get("comment", "")
    from config import OWNER_NAME as _owner
    comment_line = f"{_owner}からの要望: {comment}\n\n" if comment else ""
    plan_prompt = render("dev.implementation", "cc_plan",
        issues_block=issues_block, closes=closes, comment_line=comment_line,
    )
    # Issue #92: テストベースライン埋め込み
    test_baseline_section = ""
    baseline = data.get("test_baseline")
    if baseline and repo_path:
        import subprocess as _sub_bl
        try:
            current_head = _sub_bl.run(
                ["git", "-C", repo_path, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip()
            if current_head == baseline["commit"]:
                bl_exit   = baseline.get("exit_code", -1)
                bl_output = baseline.get("output", "")
                if len(bl_output) > MAX_BASELINE_EMBED_CHARS:
                    bl_output = "(truncated)\n..." + bl_output[-(MAX_BASELINE_EMBED_CHARS - 20):]
                if bl_exit == 0:
                    test_baseline_section = render("dev.implementation", "test_baseline_pass",
                        bl_output=bl_output,
                    )
                else:
                    test_baseline_section = render("dev.implementation", "test_baseline_fail",
                        bl_exit=bl_exit, bl_output=bl_output,
                    )
                log(f"[{project}] test baseline embedded (exit_code={bl_exit})")
            else:
                log(f"[{project}] test baseline skipped: HEAD mismatch ({current_head[:8]} != {baseline['commit'][:8]})")
        except Exception as e:
            log(f"[{project}] WARNING: test baseline embed failed: {e}")

    if skip_plan:
        scope_warning = render("dev.implementation", "scope_warning_skip_plan")
        impl_prompt = render("dev.implementation", "cc_impl_skip_plan",
            issues_block=issues_block, closes=closes, comment_line=comment_line,
            scope_warning=scope_warning, test_baseline_section=test_baseline_section,
        )
    else:
        scope_warning = render("dev.implementation", "scope_warning_normal")
        impl_prompt = render("dev.implementation", "cc_impl_resume",
            closes=closes, scope_warning=scope_warning,
            test_baseline_section=test_baseline_section,
        )

    # mkstemp で安全に一時ファイル作成
    plan_path: str | None = None
    impl_path: str | None = None
    script_path: str | None = None

    try:
        if skip_plan:
            fd_impl, impl_path = tempfile.mkstemp(suffix=".txt", prefix="devbar-impl-")
            fd_script, script_path = tempfile.mkstemp(suffix=".sh", prefix="devbar-cc-")
        else:
            fd_plan, plan_path = tempfile.mkstemp(suffix=".txt", prefix="devbar-plan-")
            fd_impl, impl_path = tempfile.mkstemp(suffix=".txt", prefix="devbar-impl-")
            fd_script, script_path = tempfile.mkstemp(suffix=".sh", prefix="devbar-cc-")

        if plan_path is not None:
            os.write(fd_plan, plan_prompt.encode())
            os.close(fd_plan)
        os.write(fd_impl, impl_prompt.encode())
        os.close(fd_impl)

        # コミット検証+リトライブロック（skip_plan/通常 共通）
        commit_verify_block = f'''
# コミット検証: CC が実際に新しいコミットを作ったか確認し、なければリトライ
HASH=$(git rev-parse --short HEAD)
RETRY=0
while [ "$HASH" = "$BEFORE_HASH" ] && [ "$RETRY" -lt 2 ]; do
    RETRY=$((RETRY + 1))
    _notify "{q_tag}[{project}] ⚠️ コミット未検出 — CC にリトライ指示 ($RETRY/2)"
    echo "実装は完了しているが git commit されていない。以下のコマンドを実行せよ:

  git add -A
  git commit -m \\"feat({closes}): <変更内容の要約>\\"

コミットメッセージには {closes} を必ず含めること。
変更すべきファイルがワーキングツリーにない場合は、Issue本文の変更対象を読み直して実装してからコミットせよ。" | \\
    claude -p --model "{impl_model}" --resume "{session_id}" \\
      --permission-mode bypassPermissions --output-format json
    HASH=$(git rev-parse --short HEAD)
done

if [ "$HASH" = "$BEFORE_HASH" ]; then
    _notify "{q_tag}[{project}] ❌ CC がコミットを作成しなかった（2回リトライ後）→ BLOCKED"
    "{DEVBAR_CLI}" transition --project "{project}" --to BLOCKED --force --comment "CC がコミットを作成しなかった（2回リトライ後）"
    exit 1
fi

# devbar commit
"{DEVBAR_CLI}" commit --project "{project}" --issue {issue_args} --hash "$HASH" --session-id "{session_id}"
'''

        if skip_plan:
            script_content = f'''#!/bin/bash
set -e
cleanup() {{ rm -f "{script_path}" "{impl_path}"; }}
trap cleanup EXIT

cd "{repo_path}"

_notify() {{ local ts=$(date +"%m/%d %H:%M"); python3 -c "import sys; sys.path.insert(0,'{Path(DEVBAR_CLI).resolve().parent}'); from notify import notify_discord; notify_discord(sys.argv[1])" "$1 ($ts)" 2>/dev/null || true; }}

BEFORE_HASH=$(git rev-parse --short HEAD)

# Plan フェーズなし — 直接 Impl
_notify "{q_tag}[{project}] 🔨 CC Impl 開始 (plan skip, model: {impl_model})"
claude -p --model "{impl_model}" {"--resume" if prev_session else "--session-id"} "{session_id}" \
  --permission-mode bypassPermissions --output-format json < "{impl_path}"
_notify "{q_tag}[{project}] ✅ CC Impl 完了"
{commit_verify_block}'''
        else:
            script_content = f'''#!/bin/bash
set -e
cleanup() {{ rm -f "{script_path}" "{plan_path}" "{impl_path}"; }}
trap cleanup EXIT

cd "{repo_path}"

_notify() {{ local ts=$(date +"%m/%d %H:%M"); python3 -c "import sys; sys.path.insert(0,'{Path(DEVBAR_CLI).resolve().parent}'); from notify import notify_discord; notify_discord(sys.argv[1])" "$1 ($ts)" 2>/dev/null || true; }}

BEFORE_HASH=$(git rev-parse --short HEAD)

# Phase 1: Plan
_notify "{q_tag}[{project}] 📋 CC Plan 開始 (model: {plan_model})"
claude -p --model "{plan_model}" {"--resume" if prev_session else "--session-id"} "{session_id}" \
  --permission-mode plan --output-format json < "{plan_path}"
_notify "{q_tag}[{project}] ✅ CC Plan 完了"

# Phase 2: Impl
_notify "{q_tag}[{project}] 🔨 CC Impl 開始 (model: {impl_model})"
claude -p --model "{impl_model}" --resume "{session_id}" \
  --permission-mode bypassPermissions --output-format json < "{impl_path}"
_notify "{q_tag}[{project}] ✅ CC Impl 完了"
{commit_verify_block}'''
        os.write(fd_script, script_content.encode())
        os.close(fd_script)
        os.chmod(script_path, 0o700)

        proc = _sub.Popen(
            ["bash", script_path],
            stdout=open(os.devnull, "w"),
            stderr=_sub.STDOUT,
            start_new_session=True,
        )

    except Exception:
        for p in filter(None, [plan_path, impl_path, script_path]):
            try:
                os.unlink(p)
            except OSError:
                pass
        raise

    # cc_pid + cc_session_id を記録
    def _save_cc_info(data):
        data["cc_pid"] = proc.pid
        data["cc_session_id"] = session_id
    update_pipeline(pipeline_path, _save_cc_info)
    log(f"[{project}] CC started (pid={proc.pid}, session={session_id})")


def _is_cc_running(data: dict) -> bool:
    """パイプラインに記録されたCC PIDが生存中か判定。"""
    pid = data.get("cc_pid")
    if not pid:
        return False
    return Path(f"/proc/{pid}").exists()


# ── Issue #92: pytest ベースライン ──────────────────────────────────────────

def _has_pytest(repo_path: str) -> bool:
    """repo に pytest が設定されているかを確認する。"""
    try:
        pyproject = Path(repo_path) / "pyproject.toml"
        if pyproject.exists():
            t = pyproject.read_text(errors="replace")
            if "[tool.pytest" in t or "[pytest]" in t:
                return True
        setup_cfg = Path(repo_path) / "setup.cfg"
        if setup_cfg.exists():
            if "[tool:pytest]" in setup_cfg.read_text(errors="replace"):
                return True
        if (Path(repo_path) / "tests").is_dir():
            return True
    except Exception:
        pass
    return False


def _kill_pytest_baseline(data: dict, pj: str) -> None:
    """既存の pytest baseline プロセスを停止し、残留ファイルを掃除する。

    start_new_session=True で起動しているため pid == PGID。
    os.killpg でプロセスグループごと停止する（子の pytest も確実に殺す）。
    """
    info = data.pop("_pytest_baseline", None)
    if not info:
        return
    pid = info.get("pid")
    if pid and Path(f"/proc/{pid}").exists():
        try:
            os.killpg(pid, signal.SIGTERM)
            import time
            time.sleep(0.5)
            if Path(f"/proc/{pid}").exists():
                os.killpg(pid, signal.SIGKILL)
            log(f"[{pj}] killed stale pytest baseline (pgid={pid})")
        except OSError:
            pass
    for key in ("output_path", "exit_code_path"):
        p = info.get(key, "")
        if p:
            try:
                os.unlink(p)
            except OSError:
                pass


def _poll_pytest_baseline(path: Path, pj: str) -> None:
    """バックグラウンド pytest の完了を検出し、test_baseline に書き込む。

    ロック外で呼ぶ。完了していれば update_pipeline で結果を書き込む。
    """
    data = load_pipeline(path)
    info = data.get("_pytest_baseline")
    if not info:
        return
    pid = info.get("pid")
    if not pid:
        return

    exit_code_path = info.get("exit_code_path", "")
    output_path    = info.get("output_path", "")
    finished   = bool(exit_code_path and os.path.exists(exit_code_path))
    proc_alive = Path(f"/proc/{pid}").exists()

    # タイムアウト判定
    timed_out = False
    started_at = info.get("started_at", "")
    if started_at:
        try:
            elapsed = (_datetime.now(JST) - _datetime.fromisoformat(started_at)).total_seconds()
            if elapsed > PYTEST_BASELINE_TIMEOUT_SEC:
                timed_out = True
        except (ValueError, TypeError):
            pass

    if timed_out and not finished:
        if proc_alive:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass

        def _save_timeout(d):
            d["test_baseline"] = {
                "commit": info["commit"],
                "summary": "(pytest timed out)",
                "exit_code": -1,
                "output": "",
                "timestamp": now_iso(),
            }
            d.pop("_pytest_baseline", None)

        update_pipeline(path, _save_timeout)
        for p in (output_path, exit_code_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        log(f"[{pj}] pytest baseline timed out (pid={pid})")
        return

    if not finished:
        if proc_alive:
            return  # まだ実行中
        # 異常終了: exit_code_path なし + proc 消滅 → exit_code=-1 で保存

    # 結果回収
    output    = ""
    exit_code = -1
    try:
        if output_path and os.path.exists(output_path):
            with open(output_path) as f:
                output = f.read()
            os.unlink(output_path)
        if exit_code_path and os.path.exists(exit_code_path):
            with open(exit_code_path) as f:
                exit_code = int(f.read().strip())
            os.unlink(exit_code_path)
    except Exception as e:
        log(f"[{pj}] WARNING: pytest baseline output read failed: {e}")

    if len(output) > MAX_BASELINE_OUTPUT_CHARS:
        output = "(truncated)\n..." + output[-(MAX_BASELINE_OUTPUT_CHARS - 20):]

    lines   = output.strip().splitlines()
    summary = lines[-1] if lines else "(no output)"

    def _save_baseline(d):
        d["test_baseline"] = {
            "commit": info["commit"],
            "summary": summary,
            "exit_code": exit_code,
            "output": output,
            "timestamp": now_iso(),
        }
        d.pop("_pytest_baseline", None)

    update_pipeline(path, _save_baseline)
    log(f"[{pj}] pytest baseline completed: exit_code={exit_code}, summary={summary}")


# ────────────────────────────────────────────────────────────────────────────

def _is_agent_inactive(agent_id: str, pipeline_data: dict | None = None) -> bool:
    """エージェントが非アクティブ(81秒以上更新なし)かどうか判定。

    CC実行中（cc_pid が /proc に存在）はアクティブと判定する。
    """
    # CC実行中ならアクティブ
    if pipeline_data is not None and _is_cc_running(pipeline_data):
        return False
    try:
        path = SESSIONS_BASE / agent_id / "sessions" / "sessions.json"
        data = _json.loads(path.read_text())
        session = data.get(f"agent:{agent_id}:main")
        if not session or "updatedAt" not in session:
            return True  # 情報なし = 非アクティブ扱い
        last_active = _datetime.fromtimestamp(session["updatedAt"] / 1000, JST)
        elapsed = (_datetime.now(JST) - last_active).total_seconds()
        return elapsed >= INACTIVE_THRESHOLD_SEC
    except (FileNotFoundError, _json.JSONDecodeError, KeyError):
        return True  # 読めない = 非アクティブ扱い


def _get_pending_reviewers(batch: list, review_key: str, reviewers: list, excluded=None) -> list:
    """全Issueのレビューを完了していないレビュアーのリストを返す。

    Args:
        excluded: 除外するレビュアーのリスト（例: レート制限で応答不能になった者）
    """
    excluded_set = set(excluded or [])
    pending = []
    for reviewer in reviewers:
        if reviewer in excluded_set:
            continue
        # 全Issueにこのレビュアーのレビューがあるか
        if not all(reviewer in i.get(review_key, {}) for i in batch):
            pending.append(reviewer)
    return pending


def _auto_push_and_close(repo_path: str, gitlab: str, batch: list, project: str) -> None:
    """DONE遷移時に git push + issue close を自動実行。"""
    import subprocess as _sp
    from config import GLAB_BIN

    # git push
    if repo_path:
        try:
            result = _sp.run(
                ["git", "-C", repo_path, "push"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                log(f"[{project}] git push 成功")
            else:
                log(f"[{project}] git push 失敗: {result.stderr.strip()}")
        except Exception as e:
            log(f"[{project}] git push エラー: {e}")

    # issue close
    for item in batch:
        issue_num = item.get("issue")
        if not issue_num:
            continue
        try:
            result = _sp.run(
                [GLAB_BIN, "issue", "close", str(issue_num), "-R", gitlab],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                log(f"[{project}] Issue #{issue_num} closed")
            else:
                log(f"[{project}] Issue #{issue_num} close失敗: {result.stderr.strip()}")
        except Exception as e:
            log(f"[{project}] Issue #{issue_num} closeエラー: {e}")


def _check_queue():
    """キューから次のタスクを起動 (DONE→IDLE後、または SPEC_DONE→IDLE後に呼ばれる)。

    Issue #45: devbar qrun をサブプロセス経由で呼び出し、循環 import を回避。
    """
    import subprocess as _sp
    from config import DEVBAR_CLI, QUEUE_FILE

    queue_path = QUEUE_FILE
    if not queue_path.exists():
        return

    # devbar qrun を subprocess 経由で呼び出し
    try:
        result = _sp.run(
            [str(DEVBAR_CLI), "qrun", "--queue", str(queue_path)],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0 and result.stdout.strip():
            log(f"[queue] {result.stdout.strip()}")
        elif "Queue empty" not in result.stdout:
            log(f"[queue] qrun failed: {result.stderr.strip()}")
    except _sp.TimeoutExpired:
        log("[queue] qrun timeout (>60s)")
    except Exception as e:
        log(f"[queue] qrun error: {e}")


def _format_nudge_message(state: str, project: str, batch: list) -> str:
    """催促メッセージ生成。get_notification_for_state() に委譲。"""
    notif = get_notification_for_state(state, project=project, batch=batch)
    return notif.impl_msg or f"[devbar] {project}: {state} — 対応してください。"


def _recover_pending_notifications(pj: str, pending: dict, data: dict) -> None:
    """未完了通知のリカバリ(Issue #59)。impl/review は再送、merge_summary/run_cc は Discord警告。

    At-least-once 保証: 通知成功時のみ pending をクリアする。
    失敗時は pending を維持し、次回 process() で再試行する。
    """
    if "impl" in pending:
        info = pending["impl"]
        try:
            notify_implementer(info["implementer"], info["msg"])
            clear_pending_notification(pj, "impl")
        except Exception as e:
            log(f"[{pj}] WARNING: impl recovery failed, will retry next cycle: {e}")

    if "review" in pending:
        info = pending["review"]
        try:
            excluded = data.get("excluded_reviewers", [])
            notify_reviewers(
                pj, info["new_state"], info["batch"], info["gitlab"],
                repo_path=info.get("repo_path", ""),
                review_mode=info.get("review_mode", "standard"),
                excluded=excluded,
                base_commit=info.get("base_commit"),
                comment=data.get("comment", ""),
            )
            clear_pending_notification(pj, "review")
        except Exception as e:
            log(f"[{pj}] WARNING: review recovery failed, will retry next cycle: {e}")

    if "merge_summary" in pending:
        try:
            notify_discord(render("dev.blocked", "notify_recovery_merge_summary", project=pj))
            clear_pending_notification(pj, "merge_summary")
        except Exception as e:
            log(f"[{pj}] WARNING: merge_summary recovery warning failed, will retry: {e}")

    if "run_cc" in pending:
        try:
            notify_discord(render("dev.blocked", "notify_recovery_cc", project=pj))
            clear_pending_notification(pj, "run_cc")
        except Exception as e:
            log(f"[{pj}] WARNING: run_cc recovery warning failed, will retry: {e}")


def _cleanup_review_files(project: str) -> None:
    """REVIEW_FILE_DIR 内の当該プロジェクトのファイルのみ削除する。

    ファイル名のダブルハイフン ``--`` セパレータより前が sanitized project 名。
    sanitized は notify._write_review_file() と同じ正規化ルール:
        sanitized = re.sub(r'[/\\\\s]', '-', project)
    判定: f.name.startswith(f"{sanitized}--")

    ダブルハイフンにより、プロジェクト "foo" のクリーンアップが
    "foo-bar" のファイルを誤削除しない（"foo--" vs "foo-bar--"）。

    呼び出しタイミング:
    - IDLE→DESIGN_PLAN 遷移時（バッチ開始時）
    - DONE遷移時（二重保険）
    ディレクトリが存在しない場合は何もしない。
    """
    import config
    review_dir = config.REVIEW_FILE_DIR
    if not review_dir.exists():
        return
    sanitized = re.sub(r'[/\\\s]', '-', project)
    prefix = f"{sanitized}--"
    for f in review_dir.iterdir():
        if f.name.startswith(prefix):
            try:
                f.unlink(missing_ok=True)
            except OSError as e:
                log(f"Warning: failed to delete review file {f}: {e}")


def process(path: Path):
    # === 第1チェック (ロックなし) ===
    data = load_pipeline(path)
    if not data.get("enabled", False):
        return

    # === Issue #92: pytest ベースライン回収 ===
    pj_poll = data.get("project", path.stem)
    _poll_pytest_baseline(path, pj_poll)

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
        now = _datetime.now(JST)
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
    if pre_action.new_state is None and not pre_action.nudge and not pre_action.nudge_reviewers and not pre_action.dispute_nudge_reviewers and not pre_action.save_grace_met_at and not pre_action.run_cc:
        return

    # === ロック内で第2チェック + 遷移 (Double-Checked Locking) ===
    notification: dict = {}

    state0 = state  # 第1チェック時点のstate（DCL用）

    def do_transition(data):
        # ロック待ち中に他プロセスが状態を変えた場合は何もしない（通知も含めてスキップ）
        if data.get("state", "IDLE") != state0:
            return

        state = data.get("state", "IDLE")
        batch = data.get("batch", [])
        action = check_transition(state, batch, data)

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
            implementer = data.get("implementer", "kaneko")
            if not _is_agent_inactive(implementer, data):
                # アクティブなら催促しない（カウンタも上げない）
                return
            # 前回催促からINACTIVE_THRESHOLD_SEC未満ならスキップ
            last_nudge = data.get("_last_nudge_at")
            if last_nudge:
                try:
                    elapsed_since_nudge = (_datetime.now(JST) - _datetime.fromisoformat(last_nudge)).total_seconds()
                    if elapsed_since_nudge < INACTIVE_THRESHOLD_SEC:
                        return
                except (ValueError, TypeError):
                    pass
            key = _nudge_key(action.nudge)
            data[key] = data.get(key, 0) + 1
            data["_last_nudge_at"] = _datetime.now(JST).isoformat()
            pj = data.get("project", path.stem)
            log(f"[{pj}] {action.nudge}: 催促通知送信 (count={data[key]})")
            notification.update({
                "pj": pj,
                "action": action,
                "implementer": data.get("implementer", "kaneko"),
                "batch": list(batch),
                "gitlab": data.get("gitlab", f"atakalive/{pj}"),
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
                "gitlab": data.get("gitlab", f"atakalive/{pj}"),
            })
            # Issue #59: pending notification for run_cc
            pending = {"run_cc": True}
            if "_pending_notifications" in data:
                log(f"[{pj}] WARNING: overwriting existing _pending_notifications")
            data["_pending_notifications"] = pending
            return

        pj = data.get("project", path.stem)

        # 旧 keep_context → 新フィールドへの正規化 (Issue #58)
        if "keep_context" in data and "keep_ctx_batch" not in data:
            legacy = data.pop("keep_context", False)
            if legacy:
                data["keep_ctx_batch"] = True
                data["keep_ctx_intra"] = True

        # DONE状態: バッチを退避してからクリア + watchdog無効化 + タイムアウト延長リセット + REVISE counters reset
        if state == "DONE":
            _done_batch = list(data.get("batch", []))  # close用に退避
            _done_queue_mode = data.get("queue_mode", False)  # _check_queue判定用に退避
            data["batch"] = []
            _cleanup_review_files(pj)
            data["enabled"] = False
            data.pop("timeout_extension", None)
            data.pop("extend_count", None)
            # Reset REVISE cycle counters (Issue #29)
            data.pop("design_revise_count", None)
            data.pop("code_revise_count", None)
            # Clear queue options (Issue #45, #71)
            data.pop("automerge", None)
            data.pop("p2_fix", None)
            data.pop("p1_fix", None)      # 旧フラグ（後方互換クリーンアップ）
            data.pop("cc_plan_model", None)
            data.pop("cc_impl_model", None)
            data.pop("keep_context", None)      # 旧フラグ（後方互換クリーンアップ）
            data.pop("keep_ctx_batch", None)
            data.pop("keep_ctx_intra", None)
            data.pop("queue_mode", None)
            data.pop("comment", None)
            data.pop("skip_cc_plan", None)
            # Issue #92: pytest baseline クリーンアップ
            _kill_pytest_baseline(data, pj)
            data.pop("test_baseline", None)

        # IDLE→DESIGN_PLAN: Reset REVISE cycle counters (Issue #29)
        if state == "IDLE" and action.new_state == "DESIGN_PLAN":
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
                        log(f"[{pj}] base_commit recorded at DESIGN_PLAN: {data['base_commit'][:7]}")
                except Exception as e:
                    log(f"[{pj}] WARNING: failed to record base_commit: {e}")

            # Issue #92: 前バッチの pytest を停止 + test_baseline クリア
            _kill_pytest_baseline(data, pj)
            data.pop("test_baseline", None)

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

                    fd_out, pytest_out_path = tempfile.mkstemp(suffix=".txt", prefix="devbar-pytest-")
                    os.close(fd_out)
                    exit_code_path = pytest_out_path + ".exit"

                    import shlex
                    fd_sh, script_path = tempfile.mkstemp(suffix=".sh", prefix="devbar-pytest-")
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

        # REVIEW→REVISE: Increment cycle counter (Issue #29)
        if state in ("DESIGN_REVIEW", "CODE_REVIEW") and action.new_state in ("DESIGN_REVISE", "CODE_REVISE"):
            counter_key = "design_revise_count" if "DESIGN" in state else "code_revise_count"
            data[counter_key] = data.get(counter_key, 0) + 1
            log(f"[{pj}] {counter_key} incremented to {data[counter_key]}")

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
            review_mode = data.get("review_mode", "standard")
            mode_config = REVIEW_MODES.get(review_mode, REVIEW_MODES["standard"])
            all_reviewers = set(mode_config["members"])
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
                    data["min_reviews_override"] = max(1, min(mode_config["min_reviews"], effective))
                log(f"[{pj}] 無応答レビュアーを除外: {sorted(no_response)}, excluded={excluded}, effective={effective}")

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
        data["state"] = action.new_state

        # ロック外通知用に情報を保存
        # DONE遷移時はbatchが既にクリア済みなので退避分を使う
        saved_batch = _done_batch if state == "DONE" else list(data.get("batch", []))
        notification.update({
            "pj": pj,
            "old_state": state,
            "action": action,
            "gitlab": data.get("gitlab", f"atakalive/{pj}"),
            "implementer": data.get("implementer", "kaneko"),
            "batch": saved_batch,
            "repo_path": data.get("repo_path", ""),
            "review_mode": data.get("review_mode", "standard"),
            "keep_ctx_batch": data.get("keep_ctx_batch", False),
            "keep_ctx_intra": data.get("keep_ctx_intra", False),
            "queue_mode": _done_queue_mode if state == "DONE" else data.get("queue_mode", False),
        })

        # Issue #59: _pending_notifications — at-least-once guarantee
        pending = {}
        if action.impl_msg:
            pending["impl"] = {
                "implementer": data.get("implementer", "kaneko"),
                "msg": f"[devbar] {pj}: {action.impl_msg}",
            }
        if action.send_review:
            pending["review"] = {
                "new_state": action.new_state,
                "batch": saved_batch,
                "gitlab": data.get("gitlab", f"atakalive/{pj}"),
                "repo_path": data.get("repo_path", ""),
                "review_mode": data.get("review_mode", "standard"),
                "base_commit": data.get("base_commit"),
            }
        if action.send_merge_summary:
            pending["merge_summary"] = True
        if action.run_cc:
            pending["run_cc"] = True
        if pending:
            if "_pending_notifications" in data:
                log(f"[{pj}] WARNING: overwriting existing _pending_notifications")
            data["_pending_notifications"] = pending

    update_pipeline(path, do_transition)

    # === ロック外で通知 ===
    if notification:
        action = notification["action"]
        pj = notification["pj"]

        if action.nudge_reviewers or action.dispute_nudge_reviewers:
            # 非アクティブなレビュアーにのみ「continue」送信（送信失敗時は次回スキップ）
            path = get_path(pj)
            pipeline_data = load_pipeline(path)
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
                if _is_agent_inactive(reviewer):
                    # 前回催促からINACTIVE_THRESHOLD_SEC未満ならスキップ
                    nudge_key = f"_last_nudge_{reviewer}"
                    last_at = pipeline_data.get(nudge_key) or pipeline_data.get(f"_nudge_failed_{reviewer}")
                    if last_at:
                        try:
                            elapsed = (_datetime.now(JST) - _datetime.fromisoformat(last_at)).total_seconds()
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
                    from config import review_command, get_current_round, DEVBAR_CLI
                    round_num = get_current_round(pipeline_data)
                    msg_parts = []

                    review_module = "dev.code_review" if is_code else "dev.design_review"

                    if dispute_items:
                        lines = []
                        for issue_num, reason in dispute_items:
                            lines.append(
                                f"  #{issue_num}: {reason}\n"
                                f"    {DEVBAR_CLI} review --pj {pj} --issue {issue_num} "
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
                        data[f"_last_nudge_{r}"] = _datetime.now(JST).isoformat()
                    for r in ng:
                        data[f"_nudge_failed_{r}"] = _datetime.now(JST).isoformat()
                update_pipeline(path, _set_nudge_ts)

            if woken:
                ts = _datetime.now(JST).strftime("%m/%d %H:%M")
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
            else:
                nudge_msg = "[Remind] 作業を進め、完了してください。"

            if action.extend_notice:
                nudge_msg += action.extend_notice
            send_to_agent_queued(notification["implementer"], nudge_msg)
            ts = _datetime.now(JST).strftime("%m/%d %H:%M")
            q_prefix = "[Queue]" if notification.get("queue_mode") else ""
            notify_discord(f"{q_prefix}[{pj}] {action.nudge}: 担当者 {notification['implementer']} を催促 ({ts})")
            return

        ts = _datetime.now(JST).strftime("%m/%d %H:%M")
        q_prefix = "[Queue]" if notification.get("queue_mode") else ""
        notify_discord(f"{q_prefix}[{pj}] {notification['old_state']} → {action.new_state} ({ts})")

        # REVISE遷移時: P0サマリーを投稿
        if action.new_state in ("DESIGN_REVISE", "CODE_REVISE"):
            review_key = "design_reviews" if "DESIGN" in action.new_state else "code_reviews"
            batch = notification["batch"]
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
                parts = []
                if p0_reviewers:
                    parts.append(f"{len(p0_reviewers)} P0 ({', '.join(p0_reviewers)})")
                if p1_reviewers:
                    parts.append(f"{len(p1_reviewers)} P1 ({', '.join(p1_reviewers)})")
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

        # MERGE_SUMMARY_SENT遷移時: #dev-bar にサマリーを投稿（リトライ付き）
        if action.send_merge_summary:
            from config import DISCORD_CHANNEL
            from notify import post_discord
            batch = notification["batch"]
            # automerge フラグを最新のパイプラインから読み取る (Issue #45)
            path = get_path(pj)
            pipeline_data = load_pipeline(path)
            automerge = pipeline_data.get("automerge", False)
            from config import MERGE_SUMMARY_FOOTER
            content = render("dev.merge_summary_sent", "format_merge_summary",
                project=pj, batch=batch, automerge=automerge,
                queue_mode=notification.get("queue_mode", False),
                MERGE_SUMMARY_FOOTER=MERGE_SUMMARY_FOOTER,
            )
            message_id = post_discord(DISCORD_CHANNEL, content)
            if message_id:
                # summary_message_id をパイプラインに保存
                path = get_path(pj)
                def _save_summary_id(data):
                    data["summary_message_id"] = message_id
                update_pipeline(path, _save_summary_id)
                log(f"[{pj}] merge summary posted (message_id={message_id})")
                clear_pending_notification(pj, "merge_summary")

                # 実装者セッションに通知 (Issue #48)
                pipeline_data_fresh = load_pipeline(path)
                implementer = pipeline_data_fresh.get("implementer") or "kaneko"
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
                path = get_path(pj)
                old_state = notification["old_state"]
                def _rollback(data, restore=old_state):
                    data["state"] = restore
                update_pipeline(path, _rollback)
                clear_pending_notification(pj, "merge_summary")

        # DONE遷移時: git push + issue close を自動実行
        if action.new_state == "DONE":
            _auto_push_and_close(
                notification.get("repo_path", ""),
                notification["gitlab"],
                notification["batch"],
                pj,
            )

        # DONE→IDLE遷移後: キューモードのときだけ次行を自動起動 (Issue #45)
        # devbar start で起動した場合は queue_mode=False なのでスキップ
        if (notification.get("old_state") == "DONE"
                and action.new_state == "IDLE"
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
            path = get_path(pj)

            def _save_excluded(data):
                data["excluded_reviewers"] = excluded

                # Calculate effective reviewer count
                effective_count = len(mode_config["members"]) - len(excluded)
                min_reviews = mode_config["min_reviews"]

                # Clamp min_reviews if deadlock would occur
                if effective_count < min_reviews:
                    clamped = max(effective_count, 0)
                    log(
                        f"[{pj}] [DEADLOCK] effective reviewers ({effective_count}) < min_reviews ({min_reviews}), clamping to {clamped}"
                    )
                    data["min_reviews_override"] = clamped
                else:
                    data.pop("min_reviews_override", None)

            update_pipeline(path, _save_excluded)

        if action.impl_msg:
            notify_implementer(
                notification["implementer"],
                f"[devbar] {pj}: {action.impl_msg}",
            )
            clear_pending_notification(pj, "impl")
        if action.send_review:
            review_mode = notification.get("review_mode", "standard")
            prev_reviews = notification.get("prev_reviews", {})
            # Read excluded from pipeline data (not notification) to pick up _save_excluded writes
            pipeline_data = load_pipeline(get_path(pj))
            excluded = pipeline_data.get("excluded_reviewers", [])

            base_commit = pipeline_data.get("base_commit")
            from config import get_current_round
            round_num = get_current_round(pipeline_data)
            notify_reviewers(
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
            clear_pending_notification(pj, "review")
        if action.run_cc:
            try:
                _start_cc(pj, notification["batch"], notification["gitlab"],
                          notification.get("repo_path", ""), path)
            except Exception as e:
                log(f"[{pj}] _start_cc failed: {e}")
                ts = _datetime.now(JST).strftime("%m/%d %H:%M")
                notify_discord(f"[{pj}] ⚠️ CC起動失敗: {e} ({ts})")
            clear_pending_notification(pj, "run_cc")


# _stop_loop_if_idle は廃止。crontab/loop.sh は常時稼働し、
# enabledチェックは process() 内の早期returnで行う。


def _load_devbar_state() -> dict:
    """Load devbar-state.json or return default state."""
    from config import DEVBAR_STATE_PATH
    if not DEVBAR_STATE_PATH.exists():
        return {"last_command_message_id": "0"}
    try:
        with open(DEVBAR_STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log("WARNING: devbar-state.json corrupt, using default")
        return {"last_command_message_id": "0"}


def _save_devbar_state(state: dict):
    """Atomically save devbar-state.json."""
    import tempfile
    from config import DEVBAR_STATE_PATH
    DEVBAR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DEVBAR_STATE_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(DEVBAR_STATE_PATH))
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
    from devbar import cmd_start
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
    from devbar import get_qstatus_text, _get_running_info
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
    from devbar import get_qstatus_text, _get_running_info
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
    from devbar import get_qstatus_text, _get_running_info
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
    from devbar import get_qstatus_text, _get_running_info
    import config

    if config.DRY_RUN:
        log(f"[dry-run] Discord qedit command skipped (msg_id={msg_id})")
        return

    parts = content.strip().split(None, 2)
    if len(parts) < 3:
        post_discord(DISCORD_CHANNEL, "qedit: 引数が必要です (例: qedit 1 devbar 105 full ...)")
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
    """Check #dev-bar for commands from M and respond.

    Process flow:
    1. Load last_command_message_id from devbar-state.json
    2. Fetch latest 10 messages from #dev-bar
    3. Filter: author is M, not bot, first word in DISCORD_COMMANDS
    4. Filter: message_id > last_command_message_id
    5. Process in chronological order (oldest → newest)
    6. For each: handle command, update last_command_message_id
    """
    from config import DISCORD_CHANNEL, M_DISCORD_USER_ID, BOT_USER_ID
    from notify import fetch_discord_latest, post_discord
    from devbar import get_status_text
    import config

    # 1. Load state
    state = _load_devbar_state()
    last_id = state.get("last_command_message_id", "0")

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
        if (author_id == M_DISCORD_USER_ID and
            author_id != BOT_USER_ID and
            cmd_word in DISCORD_COMMANDS and
            msg_id and int(msg_id) > int(last_id)):
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
        _save_devbar_state(state)
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
