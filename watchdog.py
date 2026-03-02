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
    spec_notify_review_start, spec_notify_review_complete,
    spec_notify_approved, spec_notify_approved_auto,
    spec_notify_approved_forced,
    spec_notify_stalled, spec_notify_review_failed,
    spec_notify_paused, spec_notify_revise_done,
    spec_notify_revise_commit_failed, spec_notify_revise_no_changes,
    spec_notify_issue_plan_done, spec_notify_queue_plan_done,
    spec_notify_done, spec_notify_failure,

)
from spec_review import (
    should_continue_review, _reset_review_requests,
    parse_review_yaml, validate_received_entry,
    merge_reviews, format_merged_report, build_review_history_entry,
)
from spec_issue import (
    build_issue_suggestion_prompt,
    parse_issue_suggestion_response,
    build_issue_plan_prompt,
    parse_issue_plan_response,
    build_queue_plan_prompt,
    parse_queue_plan_response,
)


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
    """(最小レビュー数, P0有無)"""
    min_n = min((len(i.get(key, {})) for i in batch), default=0)
    has_p0 = any(
        r.get("verdict", "").upper() in ("REJECT", "P0")
        for i in batch for r in i.get(key, {}).values()
    )
    return min_n, has_p0


def _revise_target_issues(batch: list, review_key: str, revised_key: str) -> str:
    """REVISE対象Issueを文字列化。P0/REJECTが付いた未修正Issueを明示。"""
    targets = []
    for i in batch:
        if i.get(revised_key):
            continue
        reviews = i.get(review_key, {})
        has_p0 = any(
            r.get("verdict", "").upper() in ("REJECT", "P0")
            for r in reviews.values()
        )
        if has_p0:
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
            if r.get("verdict", "").upper() in ("REJECT", "P0", "P1")
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
    extend_notice: str | None = None  # タイムアウト延長案内メッセージ
    save_grace_met_at: str | None = None  # grace met_atをpipelineに保存する必要がある場合のキー名


@dataclass
class SpecTransitionAction:
    """check_transition_spec() の返り値。"""
    next_state: str | None = None
    expected_state: str | None = None   # DCL用: 現在のstate（競合検出）
    send_to: dict[str, str] | None = None  # {agent_id: message}
    discord_notify: str | None = None
    pipeline_updates: dict | None = None  # spec_config への更新差分
    error: str | None = None


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
                f"python3 {DEVBAR_CLI} extend --project {project} --by 600"
            )
        else:
            nudge.extend_notice = (
                f"\n\n⏰ タイムアウトまで残り{int(remaining)}秒。延長上限に達しています。"
            )

    return nudge


_VERDICT_EMOJI = {"APPROVE": "🟢", "P0": "🔴", "P1": "🟡"}


def _format_merge_summary(project: str, batch: list, automerge: bool = False, queue_mode: bool = False) -> str:
    """#dev-bar 投稿用マージサマリーを生成する。

    2000文字超は post_discord が自動分割するので、ここでは切り詰めない。

    Args:
        project: プロジェクト名
        batch: バッチアイテム
        automerge: automerge有効時は True (Issue #45)
        queue_mode: qrun実行時は True (Issue #60)
    """
    from config import MERGE_SUMMARY_FOOTER
    q_prefix = "[Queue]" if queue_mode else ""
    lines = [f"**{q_prefix}[{project}] マージサマリー**\n"]
    for item in batch:
        num = item["issue"]
        title = item.get("title", "")
        commit = item.get("commit", "?")
        lines.append(f"**#{num}: {title}** (`{commit}`)")
        # コードレビュー結果を表示（なければ設計レビュー）
        reviews = item.get("code_reviews") or item.get("design_reviews") or {}
        for reviewer, rev in reviews.items():
            verdict = rev.get("verdict", "?")
            emoji = _VERDICT_EMOJI.get(verdict, "⚪")
            summary = rev.get("summary", "")
            # summary の1行目だけ使う（長いレビューは切る）
            first_line = summary.split("\n")[0][:120] if summary else ""
            if first_line:
                lines.append(f"  {emoji} **{reviewer}**: {verdict} — {first_line}")
            else:
                lines.append(f"  {emoji} **{reviewer}**: {verdict}")
        lines.append("")  # 空行で区切り

    # Footer (Issue #45: automerge時は文言変更)
    if automerge:
        lines.append("\n---\n⚡ automerge有効 — 自動マージします")
    else:
        lines.append(MERGE_SUMMARY_FOOTER)

    return "\n".join(lines)


def get_notification_for_state(
    state: str,
    project: str = "",
    batch: list | None = None,
    gitlab: str = "",
    implementer: str = "",
) -> TransitionAction:
    """全状態の通知メッセージを一元管理。

    遷移通知（初回）にも催促（nudge）にも使う。ただし現状の催促は "continue" のみ。
    check_transition(), cmd_transition(), 催促処理 から呼ばれる共通ロジック。
    通知不要な状態は TransitionAction() を返す。
    """
    batch = batch or []

    if state in ("DESIGN_REVIEW", "CODE_REVIEW"):
        return TransitionAction(send_review=True)

    if state == "DESIGN_PLAN":
        issues_str = ", ".join(
            f"#{i['issue']}" for i in batch if not i.get("design_ready")
        ) or "（全Issue）"
        msg = (
            f"[devbar] {project}: 設計確認フェーズ\n"
            f"対象Issue: {issues_str}\n"
            f"Claude Codeが確実に実装できる粒度まで、**対象Issue本文の説明を修正せよ** (glab issue update)。\n"
            f"コメントによる補足は禁止する。\n"
            f"全て修正後、問題がなければ plan-done して完了せよ（一括報告できる）。\n"
            f"python3 {DEVBAR_CLI} plan-done --project {project} --issue N [N...]\n"
            f"[お願い] 仕事は中断せず、完了まで一気にやること。"
        )
        return TransitionAction(impl_msg=msg, reset_reviewers=True)

    if state == "DESIGN_REVISE":
        issues_str = _revise_target_issues(batch, "design_reviews", "design_revised")
        msg = (
            f"[devbar] {project}: 設計修正フェーズ\n"
            f"対象Issue: {issues_str}\n"
            f"【手順】\n"
            f"1. P0指摘を読み、Issue本文を修正する（glab issue update）\n"
            f"2. devbar に完了報告:\n"
            f"   python3 {DEVBAR_CLI} design-revise --pj {project} --issue N [N...]\n\n"
            f"複数レビュアーから同一のP1指摘がある場合、その指摘は正しい可能性が高いため修正せよ。\n"
            f"レビュアー指摘と設計判断が相違する場合は、新規Issueを立てて設計判断を議論する場所を用意せよ。\n"
            f"[お願い] 仕事は中断せず、完了まで一気にやること。"
        )
        return TransitionAction(impl_msg=msg)

    if state == "CODE_REVISE":
        issues_str = _revise_target_issues(batch, "code_reviews", "code_revised")
        msg = (
            f"[devbar] {project}: コード修正フェーズ\n"
            f"対象Issue: {issues_str}\n"
            f"【手順】\n"
            f"1. P0指摘を読み、コードを修正する\n"
            f"2. git commit する\n"
            f"3. devbar に完了報告:\n"
            f"   python3 {DEVBAR_CLI} code-revise --pj {project} --issue N [N...] --hash <commit>\n\n"
            f"複数レビュアーから同一のP1指摘がある場合、その指摘は正しい可能性が高いため修正せよ。\n"
            f"--hash <commit> を忘れずに添付して送信すること。\n"
            f"レビュアー指摘と設計判断が相違する場合は、新規Issueを立てて設計判断を議論する場所を用意せよ。\n"
            f"[お願い] 仕事は中断せず、完了まで一気にやること。"
        )
        return TransitionAction(impl_msg=msg)

    # 現在はシステム側でCCを動かしているため使っていないが、残しておく
    if state == "IMPLEMENTATION":
        issues_str = ", ".join(
            f"#{i['issue']}" for i in batch if not i.get("commit")
        ) or "（全Issue）"
        msg = (
            f"[devbar] {project}: 実装フェーズ\n"
            f"— あなた（実装担当）がClaude Codeを使用して全ての対象Issueを一括で Plan => Impl して、devbarに完了報告してください。\n"
            f"対象Issue: {issues_str}\n"
            f"手順:\n"
            f"1. `claude --model {CC_MODEL_PLAN}` で、全対象Issueをまとめて設計確認（Plan）\n"
            f"2. `claude --model {CC_MODEL_IMPL}` で、全対象Issueをまとめて実装（Impl）\n"
            f"3. 完了後: `python3 {DEVBAR_CLI} commit --project {project} --issue N [N...] --hash <commit>`"
        )
        return TransitionAction(run_cc=True, reset_reviewers=True)

    return TransitionAction()


def _resolve_review_outcome(
    state: str, data: dict | None, batch: list, has_p0: bool,
) -> TransitionAction:
    """min_reviews 到達後の遷移先を決定する。APPROVED or REVISE or BLOCKED."""
    appr = "DESIGN_APPROVED" if "DESIGN" in state else "CODE_APPROVED"
    revise_state = "DESIGN_REVISE" if "DESIGN" in state else "CODE_REVISE"
    pj = data.get("project", "") if data else ""

    if has_p0:
        from config import MAX_REVISE_CYCLES
        counter_key = "design_revise_count" if "DESIGN" in state else "code_revise_count"
        current_count = data.get(counter_key, 0) if data else 0

        if current_count >= MAX_REVISE_CYCLES:
            phase = "設計" if "DESIGN" in state else "コード"
            return TransitionAction(
                new_state="BLOCKED",
                impl_msg=(
                    f"{phase}レビューサイクルが上限（{MAX_REVISE_CYCLES}回）に達しました。\n"
                    f"P0の指摘が解消されていません。手動で対応してください。DiscordでMに報告してください。"
                ),
            )

        return TransitionAction(
            new_state=revise_state,
            impl_msg=get_notification_for_state(revise_state, project=pj, batch=batch).impl_msg,
        )
    else:
        return TransitionAction(
            new_state=appr,
            impl_msg=get_notification_for_state(appr, project=pj, batch=batch).impl_msg,
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

        count, has_p0 = count_reviews(batch, key)

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
                if data:
                    data.pop(met_key, None)
                return _resolve_review_outcome(state, data, batch, has_p0)

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
        if pending:
            return TransitionAction(nudge_reviewers=pending)
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

        # Issue #37 fix: Only P0/REJECT issues need the revised flag
        # Empty/missing review dict → conservatively require revised flag
        all_done = True
        for issue in batch:
            reviews = issue.get(review_key, {})
            if not reviews:
                # No reviews recorded: conservatively require revised
                if not issue.get(revised_key):
                    all_done = False
                    break
                continue

            has_p0 = any(
                r.get("verdict", "").upper() in ("REJECT", "P0")
                for r in reviews.values()
            )

            # Only P0/REJECT issues need the revised flag
            if has_p0 and not issue.get(revised_key):
                all_done = False
                break

        if all_done:
            log(
                f"[REVISE] all P0/REJECT issues revised, transitioning to {review_state}"
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

    plan_prompt = (
        f"以下のIssueを実装する計画を立ててください。\n\n{issues_block}\n\n"
        f"コミットメッセージに {closes} を必ず含めること。"
    )
    impl_prompt = f"計画OK。実装して commit して。コミットメッセージに {closes} を必ず含めること。"

    # mkstemp で安全に一時ファイル作成
    fd_plan, plan_path = tempfile.mkstemp(suffix=".txt", prefix="devbar-plan-")
    fd_impl, impl_path = tempfile.mkstemp(suffix=".txt", prefix="devbar-impl-")
    fd_script, script_path = tempfile.mkstemp(suffix=".sh", prefix="devbar-cc-")

    try:
        os.write(fd_plan, plan_prompt.encode())
        os.close(fd_plan)
        os.write(fd_impl, impl_prompt.encode())
        os.close(fd_impl)

        # Discord通知用のヘルパー（CC進捗4段階通知）
        notify_cmd = f'python3 -c "from notify import notify_discord; notify_discord(\\\"$1\\\")" 2>/dev/null'

        script_content = f'''#!/bin/bash
set -e
cleanup() {{ rm -f "{script_path}" "{plan_path}" "{impl_path}"; }}
trap cleanup EXIT

cd "{repo_path}"

_notify() {{ local ts=$(date +"%m/%d %H:%M"); python3 -c "import sys; sys.path.insert(0,'{Path(DEVBAR_CLI).resolve().parent}'); from notify import notify_discord; notify_discord(sys.argv[1])" "$1 ($ts)" 2>/dev/null || true; }}

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

# コミットハッシュ取得
HASH=$(git log --oneline -1 --format=%h)

# devbar commit
python3 "{DEVBAR_CLI}" commit --project "{project}" --issue {issue_args} --hash "$HASH" --session-id "{session_id}"
'''
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
        for p in (plan_path, impl_path, script_path):
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
    """キューから次のタスクを起動 (DONE→IDLE後にのみ呼ばれる)。

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
            ["python3", str(DEVBAR_CLI), "qrun", "--queue", str(queue_path)],
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
            )
            clear_pending_notification(pj, "review")
        except Exception as e:
            log(f"[{pj}] WARNING: review recovery failed, will retry next cycle: {e}")

    if "merge_summary" in pending:
        try:
            notify_discord(f"[{pj}] ⚠️ merge_summary通知が中断されていました。手動確認してください。")
            clear_pending_notification(pj, "merge_summary")
        except Exception as e:
            log(f"[{pj}] WARNING: merge_summary recovery warning failed, will retry: {e}")

    if "run_cc" in pending:
        try:
            notify_discord(f"[{pj}] ⚠️ CC起動が中断されていました。手動確認してください。")
            clear_pending_notification(pj, "run_cc")
        except Exception as e:
            log(f"[{pj}] WARNING: run_cc recovery warning failed, will retry: {e}")


# ---------------------------------------------------------------------------
# Spec mode: プロンプト生成ヘルパー（§5.1）
# ---------------------------------------------------------------------------

def _build_spec_review_prompt_initial(
    project: str,
    spec_path: str,
    current_rev: str,
    spec_config: dict,
) -> str:
    """初回レビュー依頼プロンプト（§5.1）。"""
    pipelines_dir = spec_config.get("pipelines_dir") or str(PIPELINES_DIR)
    spec_name = Path(spec_path).stem
    return f"""以下の仕様書をレビューしてください。**やりすぎレビュー**を依頼します。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})

## レビュー指示
- 重篤度を必ず付与: 🔴 Critical (P0) / 🟠 Major (P1) / 🟡 Minor / 💡 Suggestion
- セクション番号を明記（例: §6.2）
- 擬似コード間の整合性に特に注意
- 既存devbarコードベースとの整合性も確認
- ステートマシン遷移の抜け穴・デッドロックを探せ
- YAMLブロックは応答内で**1つだけ**

## 出力フォーマット
```yaml
verdict: APPROVE | P0 | P1
items:
  - id: C-1
    severity: critical | major | minor | suggestion
    section: "§6.2"
    title: "タイトル"
    description: "説明"
    suggestion: "修正案"
```

## レビュー結果の保存
`{pipelines_dir}/{spec_name}_rev{current_rev}.md`"""


def _build_spec_review_prompt_revision(
    project: str,
    spec_path: str,
    current_rev: str,
    spec_config: dict,
    data: dict,
) -> str:
    """rev2以降のレビュー依頼プロンプト（§5.1）。"""
    pipelines_dir = spec_config.get("pipelines_dir") or str(PIPELINES_DIR)
    spec_name = Path(spec_path).stem
    last_commit = spec_config.get("last_commit", "unknown")
    last_changes = spec_config.get("last_changes", {})
    added = last_changes.get("added_lines", "?")
    removed = last_changes.get("removed_lines", "?")
    changelog = last_changes.get("changelog_summary", "変更履歴なし")
    return f"""以下の仕様書の改訂版をレビューしてください。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})
前回からの変更: +{added}行, -{removed}行
前回commit: {last_commit}

## 前回レビューからの変更点
{changelog}

## レビュー指示
- 前回の指摘が適切に反映されているか確認
- 新たに追加された部分に問題がないか確認
- 重篤度・セクション番号・YAMLフォーマットは前回と同様
- YAMLブロックは応答内で**1つだけ**

## レビュー結果の保存
`{pipelines_dir}/{spec_name}_rev{current_rev}.md`"""


# ---------------------------------------------------------------------------
# Spec mode: _check_spec_review（§5.1, §5.2）
# ---------------------------------------------------------------------------

def _check_spec_review(
    spec_config: dict,
    now: _datetime,
    data: dict,
) -> SpecTransitionAction:
    """SPEC_REVIEW: 送信・タイムアウト・回収判定。純粋関数（spec_config を mutate しない）。"""
    review_requests = spec_config.get("review_requests", {})
    current_reviews = spec_config.get("current_reviews", {})
    entries = current_reviews.get("entries", {})
    project = data.get("project", "")
    spec_path = spec_config.get("spec_path", "")
    current_rev = spec_config.get("current_rev", "1")
    rev_index = spec_config.get("rev_index", 1)

    send_to: dict[str, str] = {}
    # 更新差分を構築（元の dict は触らない）
    rr_patch: dict[str, dict] = {}   # review_requests への patch
    cr_patch: dict[str, dict] = {}   # current_reviews.entries への patch

    for reviewer, req in review_requests.items():
        status = req.get("status", "pending")

        if status == "pending" and req.get("sent_at") is None:
            # 未送信 → レビュー依頼プロンプト生成
            if rev_index <= 1:
                prompt = _build_spec_review_prompt_initial(
                    project, spec_path, current_rev, spec_config,
                )
            else:
                prompt = _build_spec_review_prompt_revision(
                    project, spec_path, current_rev, spec_config, data,
                )
            send_to[reviewer] = prompt
            # 事後条件を patch に積む（元の req は変更しない）
            rr_patch[reviewer] = {
                "sent_at": now.isoformat(),
                "timeout_at": (now + _timedelta(seconds=SPEC_REVIEW_TIMEOUT_SEC)).isoformat(),
            }

        elif status == "pending" and req.get("timeout_at"):
            # タイムアウトチェック
            try:
                timeout_at = _datetime.fromisoformat(req["timeout_at"])
            except (ValueError, TypeError):
                continue
            if now >= timeout_at:
                rr_patch[reviewer] = {"status": "timeout"}
                cr_patch[reviewer] = {
                    "verdict": None, "items": [], "raw_text": None,
                    "parse_success": False, "status": "timeout",
                }

    # 回収完了判定: patch 適用後の状態でチェック
    def _effective_status(reviewer: str) -> str:
        if reviewer in rr_patch and "status" in rr_patch[reviewer]:
            return rr_patch[reviewer]["status"]
        return review_requests.get(reviewer, {}).get("status", "pending")

    all_complete = (
        bool(review_requests)
        and all(_effective_status(r) != "pending" for r in review_requests)
    )

    # pipeline_updates を構築
    updates: dict = {}
    if rr_patch:
        updates["review_requests_patch"] = rr_patch
    if cr_patch:
        updates["current_reviews_patch"] = cr_patch

    if all_complete:
        # should_continue_review() 用に patch 適用後の仮 spec_config を構築
        effective_cr = dict(current_reviews)
        effective_entries = dict(entries)
        effective_entries.update(cr_patch)
        effective_cr["entries"] = effective_entries
        effective_sc = dict(spec_config)
        effective_sc["current_reviews"] = effective_cr

        review_mode = data.get("review_mode", "standard")
        result = should_continue_review(effective_sc, review_mode)

        # merged severity counts を entries から集計
        sev_counts = {"critical": 0, "major": 0, "minor": 0, "suggestion": 0}
        for e in effective_cr.get("entries", {}).values():
            if e.get("status") != "received":
                continue
            for item in e.get("items", []):
                if isinstance(item, dict) and item.get("severity") in sev_counts:
                    sev_counts[item["severity"]] += 1
        c_count  = sev_counts["critical"]
        m_count  = sev_counts["major"]
        mi_count = sev_counts["minor"]
        s_count  = sev_counts["suggestion"]
        p1_plus  = c_count + m_count

        result_map = {
            "approved": ("SPEC_APPROVED", spec_notify_approved(project, current_rev)),
            "revise":   ("SPEC_REVISE",   spec_notify_review_complete(project, current_rev, c_count, m_count, mi_count, s_count)),
            "stalled":  ("SPEC_STALLED",  spec_notify_stalled(project, current_rev, p1_plus)),
            "failed":   ("SPEC_REVIEW_FAILED", spec_notify_review_failed(project, current_rev)),
            "paused":   ("SPEC_PAUSED",   spec_notify_paused(project, "パース失敗")),
        }
        next_state, notify = result_map.get(result, (None, None))
        if result == "paused":
            updates["paused_from"] = "SPEC_REVIEW"
        return SpecTransitionAction(
            next_state=next_state,
            discord_notify=notify,
            pipeline_updates=updates if updates else None,
            send_to=send_to if send_to else None,
        )

    if send_to or updates:
        return SpecTransitionAction(
            next_state=None,
            send_to=send_to if send_to else None,
            pipeline_updates=updates if updates else None,
        )

    return SpecTransitionAction(next_state=None)


# ---------------------------------------------------------------------------
# Spec mode: _check_spec_revise（§6.1, §6.3, §10.2）
# ---------------------------------------------------------------------------

def _check_spec_revise(
    spec_config: dict,
    now: _datetime,
    data: dict,
) -> SpecTransitionAction:
    """SPEC_REVISE: タイムアウト検出。純粋関数（spec_config を mutate しない）。"""
    # --- implementer 応答チェック（S-5 追加） ---
    from spec_revise import parse_revise_response, build_revise_completion_updates

    project = data.get("project", "")
    revise_response = spec_config.get("_revise_response")
    if revise_response:
        parsed = parse_revise_response(revise_response, spec_config.get("current_rev", "1"))
        if parsed is None:
            # パース失敗 → PAUSED
            return SpecTransitionAction(
                next_state="SPEC_PAUSED",
                discord_notify=spec_notify_paused(project, "REVISE完了報告のパース失敗"),
                pipeline_updates={"paused_from": "SPEC_REVISE"},
            )
        if not parsed.get("commit"):
            return SpecTransitionAction(
                next_state="SPEC_PAUSED",
                discord_notify=spec_notify_revise_commit_failed(project, spec_config.get("current_rev", "1")),
                pipeline_updates={"paused_from": "SPEC_REVISE"},
            )
        # 差分0 → PAUSED（§11: 変更なし）
        changes = parsed.get("changes", {})
        if changes.get("added_lines", 0) + changes.get("removed_lines", 0) == 0:
            return SpecTransitionAction(
                next_state="SPEC_PAUSED",
                discord_notify=spec_notify_revise_no_changes(project, spec_config.get("current_rev", "1")),
                pipeline_updates={"paused_from": "SPEC_REVISE", "_revise_response": None},
            )

        # 改訂完了 → SPEC_REVIEW へ遷移
        updates = build_revise_completion_updates(spec_config, parsed, now)
        updates["_revise_response"] = None  # 消費済みクリア（Leibniz P0-1）
        current_rev = parsed.get("new_rev", "?")
        reviewer_count = len(spec_config.get("review_requests", {}))
        revise_msg = spec_notify_revise_done(project, current_rev, parsed.get("commit", ""))
        review_msg = spec_notify_review_start(project, current_rev, reviewer_count)
        return SpecTransitionAction(
            next_state="SPEC_REVIEW",
            discord_notify=f"{revise_msg}\n{review_msg}",
            pipeline_updates=updates,
        )
    # --- ここまで S-5 追加。以下は既存のタイムアウトチェック ---

    # タイムアウト起点: _revise_retry_at（リトライ後）or history（初回）
    retry_at_str = spec_config.get("_revise_retry_at")
    if retry_at_str:
        try:
            baseline = _datetime.fromisoformat(retry_at_str)
        except (ValueError, TypeError):
            baseline = None
    else:
        baseline = None

    if baseline is None:
        baseline = _get_state_entered_at(data, "SPEC_REVISE")

    if baseline is None:
        return SpecTransitionAction(next_state=None)

    elapsed = (now - baseline).total_seconds()
    if elapsed < SPEC_REVISE_TIMEOUT_SEC:
        return SpecTransitionAction(next_state=None)

    # タイムアウト: retry_counts を更新
    retry_counts = spec_config.get("retry_counts", {})
    revise_retries = retry_counts.get("SPEC_REVISE", 0)

    if revise_retries >= MAX_SPEC_RETRIES:
        return SpecTransitionAction(
            next_state="SPEC_PAUSED",
            discord_notify=spec_notify_paused(project, f"REVISE タイムアウト × {MAX_SPEC_RETRIES}"),
            pipeline_updates={
                "paused_from": "SPEC_REVISE",
                "retry_counts": {**retry_counts, "SPEC_REVISE": revise_retries + 1},
                "_revise_retry_at": None,  # クリア
            },
        )

    # リトライ: カウント更新 + 起点リセット
    return SpecTransitionAction(
        next_state=None,
        discord_notify=spec_notify_failure(project, "REVISE タイムアウト", f"retry {revise_retries + 1}/{MAX_SPEC_RETRIES}"),
        pipeline_updates={
            "retry_counts": {**retry_counts, "SPEC_REVISE": revise_retries + 1},
            "_revise_retry_at": now.isoformat(),  # 起点リセット（Dijkstra P1-2）
        },
    )


# ---------------------------------------------------------------------------
# Spec mode: _check_issue_suggestion（§7, §10.1）
# ---------------------------------------------------------------------------

def _check_issue_suggestion(
    spec_config: dict,
    now: _datetime,
    data: dict,
) -> SpecTransitionAction:
    """ISSUE_SUGGESTION: 送信・タイムアウト・回収判定。純粋関数（spec_config を mutate しない）。"""
    project = data.get("project", "")
    review_requests = spec_config.get("review_requests", {})
    current_reviews = spec_config.get("current_reviews", {})
    entries = current_reviews.get("entries", {})

    send_to: dict[str, str] = {}
    rr_patch: dict[str, dict] = {}
    # 既存の永続化分から初期化（Leibniz P0: tick跨ぎで過去受領分が消えない）
    issue_suggestions: dict = dict(spec_config.get("issue_suggestions", {}))

    for reviewer, req in review_requests.items():
        status = req.get("status", "pending")

        if status == "pending" and req.get("sent_at") is None:
            # 未送信 → Issue分割提案プロンプト生成
            prompt = build_issue_suggestion_prompt(spec_config, data)
            send_to[reviewer] = prompt
            rr_patch[reviewer] = {
                "sent_at": now.isoformat(),
                "timeout_at": (now + _timedelta(seconds=SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC)).isoformat(),
            }

        elif status == "pending" and req.get("timeout_at"):
            # タイムアウトチェック
            try:
                timeout_at = _datetime.fromisoformat(req["timeout_at"])
            except (ValueError, TypeError):
                continue
            if now >= timeout_at:
                rr_patch[reviewer] = {
                    "status": "timeout",
                    "sent_at": req.get("sent_at"),
                    "timeout_at": req.get("timeout_at"),
                    "last_nudge_at": req.get("last_nudge_at"),
                    "response": req.get("response"),
                }

        # 応答回収: 送信済み(sent_at is not None)かつ entries に received があればパース
        # sent_at チェックで送信フェーズとの同一tick競合を防止（Dijkstra P1-1）
        # パース成功時は issue_suggestions を pipeline_updates 経由で逐次永続化（Leibniz P0）
        if status == "pending" and req.get("sent_at") is not None:
            entry = entries.get(reviewer, {})
            if entry.get("status") == "received":
                raw_text = entry.get("raw_text") or entry.get("response") or ""
                parsed = parse_issue_suggestion_response(raw_text)
                if parsed is not None:
                    issue_suggestions[reviewer] = parsed
                    rr_patch[reviewer] = {
                        "status": "received",
                        "sent_at": req.get("sent_at"),
                        "timeout_at": req.get("timeout_at"),
                        "last_nudge_at": req.get("last_nudge_at"),
                        "response": req.get("response"),
                    }
                else:
                    rr_patch[reviewer] = {
                        "status": "parse_failed",
                        "sent_at": req.get("sent_at"),
                        "timeout_at": req.get("timeout_at"),
                        "last_nudge_at": req.get("last_nudge_at"),
                        "response": req.get("response"),
                    }

    # 完了判定: patch 適用後の effective status でチェック
    def _effective_status(reviewer: str) -> str:
        if reviewer in rr_patch and "status" in rr_patch[reviewer]:
            return rr_patch[reviewer]["status"]
        return review_requests.get(reviewer, {}).get("status", "pending")

    all_complete = (
        bool(review_requests)
        and all(_effective_status(r) != "pending" for r in review_requests)
    )

    updates: dict = {}
    if rr_patch:
        updates["review_requests_patch"] = rr_patch

    # issue_suggestions を毎tick逐次永続化（Leibniz P0: tick跨ぎ消失防止）
    # 関数冒頭で既存永続化分から初期化済みなのでマージ不要
    if issue_suggestions:
        updates["issue_suggestions"] = issue_suggestions

    if all_complete:
        if issue_suggestions:
            # 有効応答あり → ISSUE_PLAN へ遷移
            # review_requests を全リセット（全フィールド明示）
            reset_patch: dict[str, dict] = {}
            for reviewer in review_requests:
                reset_patch[reviewer] = {
                    "status": "pending",
                    "sent_at": None,
                    "timeout_at": None,
                    "last_nudge_at": None,
                    "response": None,
                }
            updates["review_requests_patch"] = reset_patch
            # issue_suggestions は上で逐次永続化済み
            return SpecTransitionAction(
                next_state="ISSUE_PLAN",
                discord_notify="[Spec] Issue分割提案回収完了 → ISSUE_PLAN",
                pipeline_updates=updates,
                send_to=send_to if send_to else None,
            )
        else:
            # 有効応答なし → SPEC_PAUSED
            updates["paused_from"] = "ISSUE_SUGGESTION"
            return SpecTransitionAction(
                next_state="SPEC_PAUSED",
                discord_notify=spec_notify_paused(project, "Issue分割提案: 有効応答なし"),
                pipeline_updates=updates,
                send_to=send_to if send_to else None,
            )

    if send_to or updates:
        return SpecTransitionAction(
            next_state=None,
            send_to=send_to if send_to else None,
            pipeline_updates=updates if updates else None,
        )

    return SpecTransitionAction(next_state=None)


# ---------------------------------------------------------------------------
# Spec mode: _check_issue_plan（§8.1, §10.1, §10.2）
# ---------------------------------------------------------------------------

def _check_issue_plan(
    spec_config: dict,
    now: _datetime,
    data: dict,
) -> SpecTransitionAction:
    """ISSUE_PLAN: 送信・応答回収・タイムアウト。純粋関数（spec_config を mutate しない）。"""
    project = data.get("project", "")
    implementer = spec_config.get("spec_implementer", "")
    retry_counts = spec_config.get("retry_counts", {})
    # NOTE: retry_counts は sc.update(pu) で全体置換される（deep merge なし）。
    # {**retry_counts, "KEY": n} で既存カウントを保持。既存パターン踏襲（Dijkstra Minor-1）
    issue_plan_retries = retry_counts.get("ISSUE_PLAN", 0)

    # 応答回収
    issue_plan_response = spec_config.get("_issue_plan_response")
    if issue_plan_response:
        parsed = parse_issue_plan_response(issue_plan_response)
        if parsed is None:
            return SpecTransitionAction(
                next_state="SPEC_PAUSED",
                discord_notify=spec_notify_paused(project, "ISSUE_PLAN応答パース失敗"),
                pipeline_updates={"paused_from": "ISSUE_PLAN"},
            )
        # 成功: no_queue チェック
        n = len(parsed["created_issues"])
        next_state = "SPEC_DONE" if spec_config.get("no_queue") else "QUEUE_PLAN"
        return SpecTransitionAction(
            next_state=next_state,
            discord_notify=spec_notify_issue_plan_done(project, n),
            pipeline_updates={
                "created_issues": parsed["created_issues"],
                "_issue_plan_response": None,
                "_issue_plan_sent": None,
            },
        )

    # 未送信チェック
    issue_plan_sent = spec_config.get("_issue_plan_sent")
    if not issue_plan_sent:
        if not implementer:
            return SpecTransitionAction(next_state=None)
        prompt = build_issue_plan_prompt(spec_config, data)
        return SpecTransitionAction(
            next_state=None,
            send_to={implementer: prompt},
            pipeline_updates={"_issue_plan_sent": now.isoformat()},
        )

    # タイムアウトチェック
    try:
        sent_at = _datetime.fromisoformat(issue_plan_sent)
    except (ValueError, TypeError):
        return SpecTransitionAction(next_state=None)

    elapsed = (now - sent_at).total_seconds()
    if elapsed < SPEC_ISSUE_PLAN_TIMEOUT_SEC:
        return SpecTransitionAction(next_state=None)

    # タイムアウト: リトライ管理
    if issue_plan_retries >= MAX_SPEC_RETRIES:
        return SpecTransitionAction(
            next_state="SPEC_PAUSED",
            discord_notify=spec_notify_paused(project, f"ISSUE_PLAN タイムアウト × {MAX_SPEC_RETRIES}"),
            pipeline_updates={
                "paused_from": "ISSUE_PLAN",
                "retry_counts": {**retry_counts, "ISSUE_PLAN": issue_plan_retries + 1},
            },
        )

    return SpecTransitionAction(
        next_state=None,
        discord_notify=spec_notify_failure(project, "ISSUE_PLAN タイムアウト", f"retry {issue_plan_retries + 1}/{MAX_SPEC_RETRIES}"),
        pipeline_updates={
            "retry_counts": {**retry_counts, "ISSUE_PLAN": issue_plan_retries + 1},
            "_issue_plan_sent": None,
        },
    )


# ---------------------------------------------------------------------------
# Spec mode: _check_queue_plan（§9, §10.1, §10.2）
# ---------------------------------------------------------------------------

def _check_queue_plan(
    spec_config: dict,
    now: _datetime,
    data: dict,
) -> SpecTransitionAction:
    """QUEUE_PLAN: 送信・応答回収・タイムアウト。純粋関数（spec_config を mutate しない）。"""
    project = data.get("project", "")
    implementer = spec_config.get("spec_implementer", "")
    retry_counts = spec_config.get("retry_counts", {})
    queue_plan_retries = retry_counts.get("QUEUE_PLAN", 0)

    # 応答回収
    queue_plan_response = spec_config.get("_queue_plan_response")
    if queue_plan_response:
        parsed = parse_queue_plan_response(queue_plan_response)
        if parsed is None:
            return SpecTransitionAction(
                next_state="SPEC_PAUSED",
                discord_notify=spec_notify_paused(project, "QUEUE_PLAN応答パース失敗"),
                pipeline_updates={"paused_from": "QUEUE_PLAN"},
            )
        batches = parsed["batches"]
        return SpecTransitionAction(
            next_state="SPEC_DONE",
            discord_notify=spec_notify_queue_plan_done(project, batches),
            pipeline_updates={
                "_queue_plan_response": None,
                "_queue_plan_sent": None,
            },
        )

    # 未送信チェック
    queue_plan_sent = spec_config.get("_queue_plan_sent")
    if not queue_plan_sent:
        if not implementer:
            return SpecTransitionAction(next_state=None)
        prompt = build_queue_plan_prompt(spec_config, data)
        return SpecTransitionAction(
            next_state=None,
            send_to={implementer: prompt},
            pipeline_updates={"_queue_plan_sent": now.isoformat()},
        )

    # タイムアウトチェック
    try:
        sent_at = _datetime.fromisoformat(queue_plan_sent)
    except (ValueError, TypeError):
        return SpecTransitionAction(next_state=None)

    elapsed = (now - sent_at).total_seconds()
    if elapsed < SPEC_QUEUE_PLAN_TIMEOUT_SEC:
        return SpecTransitionAction(next_state=None)

    # タイムアウト: リトライ管理
    if queue_plan_retries >= MAX_SPEC_RETRIES:
        return SpecTransitionAction(
            next_state="SPEC_PAUSED",
            discord_notify=spec_notify_paused(project, f"QUEUE_PLAN タイムアウト × {MAX_SPEC_RETRIES}"),
            pipeline_updates={
                "paused_from": "QUEUE_PLAN",
                "retry_counts": {**retry_counts, "QUEUE_PLAN": queue_plan_retries + 1},
            },
        )

    return SpecTransitionAction(
        next_state=None,
        discord_notify=spec_notify_failure(project, "QUEUE_PLAN タイムアウト", f"retry {queue_plan_retries + 1}/{MAX_SPEC_RETRIES}"),
        pipeline_updates={
            "retry_counts": {**retry_counts, "QUEUE_PLAN": queue_plan_retries + 1},
            "_queue_plan_sent": None,
        },
    )


# ---------------------------------------------------------------------------
# Spec mode: check_transition_spec（§10.1）
# ---------------------------------------------------------------------------

def check_transition_spec(
    state: str,
    spec_config: dict,
    now: _datetime,
    data: dict,
) -> SpecTransitionAction:
    """純粋関数。spec_config/data を一切 mutate しない。
    全状態変更は pipeline_updates に積む。"""
    project = data.get("project", "")
    if state not in SPEC_STATES:
        return SpecTransitionAction(
            next_state="SPEC_PAUSED",
            error=f"Unknown spec state: {state}",
            discord_notify=spec_notify_paused(project, f"未知状態 {state}"),
            pipeline_updates={"paused_from": state},
        )

    if state == "SPEC_REVIEW":
        return _check_spec_review(spec_config, now, data)
    elif state == "SPEC_REVISE":
        return _check_spec_revise(spec_config, now, data)
    elif state == "SPEC_APPROVED":
        if spec_config.get("review_only"):
            return SpecTransitionAction(
                next_state="SPEC_DONE",
                discord_notify=spec_notify_done(project),
            )
        if spec_config.get("auto_continue"):
            return SpecTransitionAction(
                next_state="ISSUE_SUGGESTION",
                discord_notify=spec_notify_approved_auto(project, spec_config.get("current_rev", "?")),
            )
        # デフォルト: M確認待ち（通知は遷移元で発火済み）
        return SpecTransitionAction(next_state=None)
    elif state == "ISSUE_SUGGESTION":
        return _check_issue_suggestion(spec_config, now, data)
    elif state == "ISSUE_PLAN":
        return _check_issue_plan(spec_config, now, data)
    elif state == "QUEUE_PLAN":
        return _check_queue_plan(spec_config, now, data)
    elif state in ("SPEC_DONE", "SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED"):
        return SpecTransitionAction(next_state=None)  # M操作待ち
    else:
        return SpecTransitionAction(next_state=None)


# ---------------------------------------------------------------------------
# Spec mode: pipelines_dir 管理（§12.1）
# ---------------------------------------------------------------------------

def _ensure_pipelines_dir(pipelines_dir: str) -> None:
    """pipelines_dirが存在しなければ作成（§12.1）。

    - is_dir() でチェック（同名ファイルが存在したらログ警告して return）
    - mkdir(mode=0o700) でアトミックにパーミッション設定
    - chmod 失敗（/mnt 等の非互換FS）はログのみで続行
    """
    pd = Path(pipelines_dir)
    if pd.exists():
        if not pd.is_dir():
            log(f"[spec] pipelines_dir is not a directory: {pd}")
        return
    try:
        pd.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as e:
        log(f"[spec] mkdir failed: {pd}: {e}")


_SPEC_REVIEW_FILE_PATTERN = re.compile(r".*_rev\d+\.md$")
_SPEC_TERMINAL_STATES = {"SPEC_DONE", "SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED"}


def _cleanup_expired_spec_files(pipelines_dir: str) -> None:
    """pipelines_dir内のspec-review生成物で、RETENTION超過ファイルを削除（§12.1）。

    削除対象: *_rev*.md パターンのファイルのみ（命名規則で限定）。
    mtime基準でSPEC_REVIEW_RAW_RETENTION_DAYS超過を判定。
    ディレクトリ不在・is_dir失敗はログのみで安全にreturn。
    """
    pd = Path(pipelines_dir)
    if not pd.is_dir():
        return
    cutoff = _datetime.now(JST) - _timedelta(days=SPEC_REVIEW_RAW_RETENTION_DAYS)
    for f in pd.iterdir():
        if not f.is_file():
            continue
        if not _SPEC_REVIEW_FILE_PATTERN.match(f.name):
            continue
        try:
            mtime = _datetime.fromtimestamp(f.stat().st_mtime, tz=JST)
            if mtime < cutoff:
                f.unlink()
                log(f"[spec] cleanup expired: {f.name}")
        except OSError as e:
            log(f"[spec] cleanup error: {f.name}: {e}")


# Spec mode: _apply_spec_action — DCLパターン（§10.1）
# ---------------------------------------------------------------------------

def _apply_spec_action(
    pipeline_path: Path,
    action: SpecTransitionAction,
    now: _datetime,
    orig_data: dict,
) -> None:
    """DCLパターン: ディスクから再読み込み + state一致確認 + 再計算。"""
    applied = False
    applied_action: SpecTransitionAction | None = None
    pj = orig_data.get("project", pipeline_path.stem)

    def _update(data: dict) -> None:
        nonlocal applied, applied_action
        # expected_state 不一致 → 競合スキップ
        if data.get("state") != action.expected_state:
            log(f"[{pj}] spec DCL conflict: expected={action.expected_state}, actual={data.get('state')}")
            return

        sc = data.get("spec_config", {})
        action2 = check_transition_spec(data["state"], sc, now, data)

        # 状態遷移
        if action2.next_state:
            old_state = data["state"]
            data["state"] = action2.next_state
            add_history(data, old_state, action2.next_state, actor="watchdog")

        # pipeline_updates は常に適用（next_state=None でも）
        if action2.pipeline_updates:
            pu = action2.pipeline_updates
            # review_requests_patch: per-reviewer の差分を deep merge
            rr_patch = pu.pop("review_requests_patch", None)
            if rr_patch:
                rr = sc.setdefault("review_requests", {})
                for reviewer, patch in rr_patch.items():
                    rr.setdefault(reviewer, {}).update(patch)
            # current_reviews_patch: entries への差分を deep merge
            cr_patch = pu.pop("current_reviews_patch", None)
            if cr_patch:
                cr = sc.setdefault("current_reviews", {})
                entries = cr.setdefault("entries", {})
                entries.update(cr_patch)
            # 残りのフィールドは直接 update
            sc.update(pu)
            data["spec_config"] = sc

        if action2.next_state or action2.pipeline_updates or action2.send_to or action2.discord_notify:
            applied = True
            applied_action = action2

    update_pipeline(pipeline_path, _update)

    # 副作用はロック外で実行（applied_action の結果を使用）
    if applied and applied_action:
        if applied_action.send_to:
            pd = orig_data.get("spec_config", {}).get("pipelines_dir")
            if pd:
                _ensure_pipelines_dir(pd)
            for agent_id, msg in applied_action.send_to.items():
                if not send_to_agent_queued(agent_id, msg):
                    notify_discord(spec_notify_failure(pj, "送信失敗", f"agent={agent_id}"))
        if applied_action.discord_notify:
            notify_discord(applied_action.discord_notify)
        if applied_action.next_state in _SPEC_TERMINAL_STATES:
            pd = orig_data.get("spec_config", {}).get("pipelines_dir")
            if pd:
                _cleanup_expired_spec_files(pd)


def process(path: Path):
    # === 第1チェック (ロックなし) ===
    data = load_pipeline(path)
    if not data.get("enabled", False):
        return

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

    # spec mode: batch空を許容し、専用ロジックに委譲
    if data.get("spec_mode") and state in SPEC_STATES:
        spec_config = data.get("spec_config", {})
        now = _datetime.now(JST)
        action = check_transition_spec(state, spec_config, now, data)
        # 副作用フィールドが1つでもあれば適用
        if action.next_state or action.pipeline_updates or action.send_to or action.discord_notify:
            action.expected_state = state
            _apply_spec_action(path, action, now, data)
        return

    if state != "DONE" and not batch and not data.get("spec_mode"):
        log(f"[{pj}] WARNING: state={state} but batch is empty")
        return

    pre_action = check_transition(state, batch, data)
    if pre_action.new_state is None and not pre_action.nudge and not pre_action.nudge_reviewers and not pre_action.save_grace_met_at and not pre_action.run_cc:
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
        if action.nudge_reviewers:
            pj = data.get("project", path.stem)
            notification.update({
                "pj": pj,
                "action": action,
                "nudge_reviewers": list(action.nudge_reviewers),
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
            data["enabled"] = False
            data.pop("timeout_extension", None)
            data.pop("extend_count", None)
            # Reset REVISE cycle counters (Issue #29)
            data.pop("design_revise_count", None)
            data.pop("code_revise_count", None)
            # Clear queue options (Issue #45)
            data.pop("automerge", None)
            data.pop("cc_plan_model", None)
            data.pop("cc_impl_model", None)
            data.pop("keep_context", None)      # 旧フラグ（後方互換クリーンアップ）
            data.pop("keep_ctx_batch", None)
            data.pop("keep_ctx_intra", None)
            data.pop("queue_mode", None)

        # IDLE→DESIGN_PLAN: Reset REVISE cycle counters (Issue #29)
        if state == "IDLE" and action.new_state == "DESIGN_PLAN":
            data.pop("design_revise_count", None)
            data.pop("code_revise_count", None)

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
                    if v.get("verdict", "").upper() in ("REJECT", "P0", "P1")
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

        if action.nudge_reviewers:
            # 非アクティブなレビュアーにのみ「continue」送信（送信失敗時は次回スキップ）
            path = get_path(pj)
            pipeline_data = load_pipeline(path)
            state = notification.get("old_state", "")
            batch = notification.get("batch", [])
            woken = []
            failed = []

            # Determine review key based on state
            review_key = "design_reviews" if "DESIGN" in state else "code_reviews"

            for reviewer in notification["nudge_reviewers"]:
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

                    # Find pending issues for this reviewer
                    pending_issues = [
                        item["issue"] for item in batch
                        if reviewer not in item.get(review_key, {})
                    ]

                    # Skip if no pending issues (shouldn't happen due to _get_pending_reviewers)
                    if not pending_issues:
                        continue

                    # Generate copy-pasteable command for each issue
                    from config import review_command
                    cmd_lines = "\n".join(
                        review_command(pj, num, reviewer) for num in pending_issues
                    )

                    msg = (
                        f"[Remind] {pj} のレビューが未完了です。対象: {', '.join(f'#{n}' for n in pending_issues)}\n"
                        f"以下のコマンドで各 Issue のレビューを報告してください:\n"
                        f"{cmd_lines}"
                    )

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
                log(f"[{pj}] レビュアーを催促: {', '.join(woken)} ({ts})")
                notify_discord(f"{q_prefix}[{pj}] レビュアーを催促: {', '.join(woken)} ({ts})")
            return

        if action.nudge:
            # 状態ごとの具体的な指示メッセージ
            nudge_state = action.nudge  # e.g. "DESIGN_REVISE", "CODE_REVISE", etc.

            if nudge_state == "DESIGN_REVISE":
                nudge_msg = (
                    "[Remind] 予定のリバイス作業を進め、完了してください。\n"
                    "devbar design-revise --pj <project> --issue <N> で完了報告してください。"
                )
            elif nudge_state == "CODE_REVISE":
                nudge_msg = (
                    "[Remind] 予定のリバイス作業を進め、完了してください。\n"
                    "devbar code-revise --pj <project> --issue <N> --hash <commit> で修正コミットを報告してください。"
                )
            elif nudge_state == "DESIGN_PLAN":
                nudge_msg = (
                    "[Remind] 設計確認を進め、完了してください。\n"
                    "devbar plan-done --project <project> --issue <N> で完了報告してください。"
                )
            elif nudge_state == "IMPLEMENTATION":
                nudge_msg = (
                    "[Remind] 実装を進め、完了してください。\n"
                    "devbar commit --pj <project> --issue <N> --hash <commit> でコミットを報告してください。"
                )
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
                if p0_reviewers:
                    lines.append(f"#{item['issue']}: {len(p0_reviewers)} P0 ({', '.join(p0_reviewers)})")
            if lines:
                notify_discord(f"{q_prefix}[{pj}] REVISE対象:\n" + "\n".join(lines))

        # バッチ開始時のみIssue一覧を別メッセージで通知
        if action.new_state == "DESIGN_PLAN":
            batch = notification["batch"]
            if batch:
                issue_lines = [f"#{i['issue']}: {i.get('title', '')}" for i in batch]
                notify_discord(f"{q_prefix}[{pj}] 対象Issue:\n" + "\n".join(issue_lines))

        # MERGE_SUMMARY_SENT遷移時: #dev-bar にサマリーを投稿（リトライ付き）
        if action.send_merge_summary:
            from config import DISCORD_CHANNEL
            from notify import post_discord
            batch = notification["batch"]
            # automerge フラグを最新のパイプラインから読み取る (Issue #45)
            path = get_path(pj)
            pipeline_data = load_pipeline(path)
            automerge = pipeline_data.get("automerge", False)
            content = _format_merge_summary(pj, batch, automerge=automerge, queue_mode=notification.get("queue_mode", False))
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
                prompt = (
                    f"[devbar] {pj}: バッチ完了\n"
                    f"{content}\n\n"
                    "上記の作業を振り返り、以下だけを記録してください:\n"
                    "- 踏んだ罠、ハマったこと（あれば）\n"
                    "- レビュアー指摘で学んだこと（あれば）\n"
                    "- 今後の作業に影響する判断（あれば）\n"
                    "記録すべきことがなければ NO_REPLY で構いません。"
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
                excluded = []
            else:
                impl = ""
                if action.new_state == "DESIGN_PLAN":
                    # DESIGN_PLAN開始時は毎回実装担当もリセット（compaction破損対策）
                    impl = notification["implementer"]
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

            notify_reviewers(
                pj, action.new_state, notification["batch"], notification["gitlab"],
                repo_path=notification.get("repo_path", ""),
                review_mode=review_mode,
                prev_reviews=prev_reviews,
                excluded=excluded,
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
        if entry.get("automerge"):
            data["automerge"] = True
        if entry.get("cc_plan_model"):
            data["cc_plan_model"] = entry["cc_plan_model"]
        if entry.get("cc_impl_model"):
            data["cc_impl_model"] = entry["cc_impl_model"]

    update_pipeline(path, _save_queue_options)

    # Post success to Discord
    automerge_flag = entry.get("automerge", False)
    success_msg = f"qrun: {project} started (issues={issues}, automerge={automerge_flag})"
    post_discord(DISCORD_CHANNEL, success_msg)
    log(f"[qrun] {success_msg} (msg_id={msg_id})")


def check_discord_commands():
    """Check #dev-bar for 'status' and 'qrun' commands from M and respond.

    Process flow:
    1. Load last_command_message_id from devbar-state.json
    2. Fetch latest 10 messages from #dev-bar
    3. Filter: author is M, not bot, content starts with "status" or "qrun"
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
        # Filter: from M, not from bot, starts with "status" or "qrun"
        if (author_id == M_DISCORD_USER_ID and
            author_id != BOT_USER_ID and
            (content_lower.startswith("status") or content_lower.startswith("qrun")) and
            msg_id and int(msg_id) > int(last_id)):
            candidates.append(msg)

    # 4. Process in chronological order (reversed, API returns newest first)
    for msg in reversed(candidates):
        msg_id = msg["id"]
        content = msg["content"]
        content_lower = content.strip().lower()

        # 5. Route to appropriate handler
        if content_lower.startswith("status"):
            status = get_status_text(enabled_only=True)
            response = f"```\n{status}\n```"

            if config.DRY_RUN:
                log(f"[dry-run] Discord status command response skipped (msg_id={msg_id})")
            else:
                post_discord(DISCORD_CHANNEL, response)

        elif content_lower.startswith("qrun"):
            _handle_qrun(msg_id)

        # 6. Update state (even in dry-run to test deduplication)
        state["last_command_message_id"] = msg_id
        _save_devbar_state(state)
        log(f"Processed Discord {content_lower.split()[0]} command (msg_id={msg_id})")


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
