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
import sys
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    PIPELINES_DIR, JST, LOG_FILE, REVIEW_MODES, CC_MODEL_PLAN, CC_MODEL_IMPL,
    DEVBAR_CLI, INACTIVE_THRESHOLD_SEC, SESSIONS_BASE,
    # WATCHDOG_LOOP_PIDFILE, WATCHDOG_LOOP_CRON_MARKER は devbar.py の enable/disable 専用
)
from datetime import datetime as _datetime
import json as _json
from pipeline_io import (
    load_pipeline, update_pipeline, get_path,
    add_history, now_iso, find_issue,
)
from notify import notify_implementer, notify_reviewers, notify_discord, send_to_agent, send_to_agent_queued, ping_agent


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


def _format_merge_summary(project: str, batch: list, automerge: bool = False) -> str:
    """#dev-bar 投稿用マージサマリーを生成する。

    2000文字超は post_discord が自動分割するので、ここでは切り詰めない。

    Args:
        project: プロジェクト名
        batch: バッチアイテム
        automerge: automerge有効時は True (Issue #45)
    """
    from config import MERGE_SUMMARY_FOOTER
    lines = [f"**[{project}] マージサマリー**\n"]
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

    session_id = str(_uuid.uuid4())

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
_notify "[{project}] 📋 CC Plan 開始 (model: {plan_model})"
claude -p --model "{plan_model}" --session-id "{session_id}" \
  --permission-mode plan --output-format json < "{plan_path}"
_notify "[{project}] ✅ CC Plan 完了"

# Phase 2: Impl
_notify "[{project}] 🔨 CC Impl 開始 (model: {impl_model})"
claude -p --model "{impl_model}" --resume "{session_id}" \
  --permission-mode bypassPermissions --output-format json < "{impl_path}"
_notify "[{project}] ✅ CC Impl 完了"

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
    from config import PIPELINES_DIR, DEVBAR_CLI

    queue_path = PIPELINES_DIR / "devbar-queue.txt"
    if not queue_path.exists():
        return

    # devbar qrun を subprocess 経由で呼び出し
    try:
        result = _sp.run(
            ["python3", str(DEVBAR_CLI), "qrun", "--queue", str(queue_path)],
            capture_output=True, text=True, timeout=60,
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


def process(path: Path):
    # === 第1チェック (ロックなし) ===
    data = load_pipeline(path)
    if not data.get("enabled", False):
        return

    state = data.get("state", "IDLE")
    batch = data.get("batch", [])
    pj = data.get("project", path.stem)

    if state != "DONE" and not batch:
        log(f"[{pj}] WARNING: state={state} but batch is empty")
        return

    pre_action = check_transition(state, batch, data)
    if pre_action.new_state is None and not pre_action.nudge and not pre_action.nudge_reviewers and not pre_action.save_grace_met_at:
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
            })
            return

        if action.new_state is None:
            # ロック待ち中に他プロセスが状態を変えた → スキップ
            return

        pj = data.get("project", path.stem)

        # DONE状態: バッチを退避してからクリア + watchdog無効化 + タイムアウト延長リセット + REVISE counters reset
        if state == "DONE":
            _done_batch = list(data.get("batch", []))  # close用に退避
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
        })

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
                log(f"[{pj}] レビュアーを催促: {', '.join(woken)} ({ts})")
                notify_discord(f"[{pj}] レビュアーを催促: {', '.join(woken)} ({ts})")
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
            notify_discord(f"[{pj}] {action.nudge}: 担当者 {notification['implementer']} を催促 ({ts})")
            return

        ts = _datetime.now(JST).strftime("%m/%d %H:%M")
        notify_discord(f"[{pj}] {notification['old_state']} → {action.new_state} ({ts})")

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
                notify_discord(f"[{pj}] REVISE対象:\n" + "\n".join(lines))

        # バッチ開始時のみIssue一覧を別メッセージで通知
        if action.new_state == "DESIGN_PLAN":
            batch = notification["batch"]
            if batch:
                issue_lines = [f"#{i['issue']}: {i.get('title', '')}" for i in batch]
                notify_discord(f"[{pj}] 対象Issue:\n" + "\n".join(issue_lines))

        # MERGE_SUMMARY_SENT遷移時: #dev-bar にサマリーを投稿（リトライ付き）
        if action.send_merge_summary:
            from config import DISCORD_CHANNEL
            from notify import post_discord
            batch = notification["batch"]
            # automerge フラグを最新のパイプラインから読み取る (Issue #45)
            path = get_path(pj)
            pipeline_data = load_pipeline(path)
            automerge = pipeline_data.get("automerge", False)
            content = _format_merge_summary(pj, batch, automerge=automerge)
            message_id = post_discord(DISCORD_CHANNEL, content)
            if message_id:
                # summary_message_id をパイプラインに保存
                path = get_path(pj)
                def _save_summary_id(data):
                    data["summary_message_id"] = message_id
                update_pipeline(path, _save_summary_id)
                log(f"[{pj}] merge summary posted (message_id={message_id})")
            else:
                # 全リトライ失敗: 遷移をロールバックして次サイクルで再試行
                log(f"[{pj}] WARNING: merge summary post failed after 3 attempts, rolling back state")
                path = get_path(pj)
                old_state = notification["old_state"]
                def _rollback(data, restore=old_state):
                    data["state"] = restore
                update_pipeline(path, _rollback)

        # DONE遷移時: git push + issue close を自動実行
        if action.new_state == "DONE":
            _auto_push_and_close(
                notification.get("repo_path", ""),
                notification["gitlab"],
                notification["batch"],
                pj,
            )
            # Queue check: 次のバッチを自動起動 (Issue #45)
            # この通知ブロックは watchdog actor 専用 (CLI force 遷移では到達しない)
            _check_queue()

        if action.reset_reviewers:
            impl = ""
            if action.new_state == "DESIGN_PLAN":
                # DESIGN_PLAN開始時は毎回実装担当もリセット（compaction破損対策）
                impl = notification["implementer"]
            review_mode = notification.get("review_mode", "standard")
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
        if action.run_cc:
            try:
                _start_cc(pj, notification["batch"], notification["gitlab"],
                          notification.get("repo_path", ""), path)
            except Exception as e:
                log(f"[{pj}] _start_cc failed: {e}")
                ts = _datetime.now(JST).strftime("%m/%d %H:%M")
                notify_discord(f"[{pj}] ⚠️ CC起動失敗: {e} ({ts})")


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


def check_discord_commands():
    """Check #dev-bar for 'status' commands from M and respond.

    Process flow:
    1. Load last_command_message_id from devbar-state.json
    2. Fetch latest 10 messages from #dev-bar
    3. Filter: author is M, not bot, content starts with "status"
    4. Filter: message_id > last_command_message_id
    5. Process in chronological order (oldest → newest)
    6. For each: post status, update last_command_message_id
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

        # Filter: from M, not from bot, starts with "status"
        if (author_id == M_DISCORD_USER_ID and
            author_id != BOT_USER_ID and
            content.strip().lower().startswith("status") and
            msg_id and int(msg_id) > int(last_id)):
            candidates.append(msg)

    # 4. Process in chronological order (reversed, API returns newest first)
    for msg in reversed(candidates):
        msg_id = msg["id"]

        # 5. Get status and post response
        status = get_status_text(enabled_only=True)
        response = f"```\n{status}\n```"

        if config.DRY_RUN:
            log(f"[dry-run] Discord status command response skipped (msg_id={msg_id})")
        else:
            post_discord(DISCORD_CHANNEL, response)

        # 6. Update state (even in dry-run to test deduplication)
        state["last_command_message_id"] = msg_id
        _save_devbar_state(state)
        log(f"Processed Discord status command (msg_id={msg_id})")


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
