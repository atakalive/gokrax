from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import datetime as _datetime

from config import (
    BLOCK_TIMERS,
    GOKRAX_CLI,
    EXTENDABLE_STATES,
    EXTEND_NOTICE_THRESHOLD,
    LOCAL_TZ,
    NUDGE_GRACE_SEC,
    REVIEW_MODES,
    STATE_PHASE_MAP,
)
from engine.reviewer import (
    _awaiting_dispute_re_review,
    _get_pending_reviewers,
    _revise_target_issues,
    count_reviews,
)
from engine.shared import _is_cc_running, _is_ok_reply, log
from messages import render
from notify import notify_discord, notify_implementer, notify_reviewers
from pipeline_io import clear_pending_notification


@dataclass
class TransitionAction:
    """check_transition() の返り値。new_state が None なら遷移不要。"""
    new_state: str | None = None
    impl_msg: str | None = None
    send_review: bool = False
    reset_reviewers: bool = False  # レビュアーに /new を先行送信
    send_merge_summary: bool = False  # #gokrax にマージサマリーを投稿
    run_cc: bool = False  # CC CLI を直接起動
    run_test: bool = False  # テスト実行トリガー（CODE_TEST進入時）
    nudge: str | None = None   # 催促通知が必要な状態名
    nudge_reviewers: list | None = None  # 催促が必要なレビュアーのリスト
    dispute_nudge_reviewers: list | None = None  # dispute pending で催促が必要なレビュアー
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
        elapsed = (_datetime.now(LOCAL_TZ) - entered_at).total_seconds()

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
                f"{GOKRAX_CLI} extend --project {project} --by 600"
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
            comment_line=comment_line, GOKRAX_CLI=GOKRAX_CLI,
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
            fix_label=fix_label, p2_note=p2_note, GOKRAX_CLI=GOKRAX_CLI,
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
            fix_label=fix_label, p2_note=p2_note, GOKRAX_CLI=GOKRAX_CLI,
        )
        return TransitionAction(impl_msg=msg)

    if state == "ASSESSMENT":
        issues_str = ", ".join(f"#{i['issue']}" for i in batch) or "（全Issue）"
        msg = render("dev.assessment", "transition",
            project=project, issues_str=issues_str,
            comment_line=comment_line, GOKRAX_CLI=GOKRAX_CLI,
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
    p2_fix = data.get("p2_fix", False) if data else False

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
    if state in ("IDLE", "BLOCKED"):
        return TransitionAction()

    # INITIALIZE → DESIGN_PLAN: 自動遷移（初期化処理は watchdog の do_transition 内で実行）
    if state == "INITIALIZE":
        pj = data.get("project", "") if data else ""
        comment = data.get("comment", "") if data else ""
        notif = get_notification_for_state(
            "DESIGN_PLAN", project=pj, batch=batch, comment=comment,
        )
        return TransitionAction(
            new_state="DESIGN_PLAN",
            impl_msg=notif.impl_msg,
            reset_reviewers=True,
        )

    if state == "MERGE_SUMMARY_SENT":
        if data is None:
            return TransitionAction()

        # Automerge: skip approval wait (Issue #45)
        if data.get("automerge", False):
            return TransitionAction(new_state="DONE")

        # Manual merge: wait for M's OK
        from notify import fetch_discord_replies
        from config import MERGE_APPROVER_DISCORD_ID, DISCORD_CHANNEL
        summary_id = data.get("summary_message_id")
        if not summary_id:
            return TransitionAction()
        messages = fetch_discord_replies(DISCORD_CHANNEL, summary_id)
        for msg in messages:
            ref = msg.get("message_reference", {})
            if (ref.get("message_id") == summary_id
                    and msg.get("author", {}).get("id") == MERGE_APPROVER_DISCORD_ID
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
                data[met_key] = datetime.now(LOCAL_TZ).isoformat()
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
                from datetime import timedelta  # noqa: F401
                met_at = datetime.fromisoformat(data[met_key])
                elapsed = (datetime.now(LOCAL_TZ) - met_at).total_seconds()
                if elapsed >= grace_sec:
                    log(f"[GRACE] grace period expired ({elapsed:.1f}s >= {grace_sec}s), transitioning")
                    should_transition = True
                else:
                    log(f"[GRACE] waiting ({grace_sec - elapsed:.1f}s remaining)")
                    return TransitionAction(save_grace_met_at=met_key)

            # Case 3: No grace (min == effective or grace_sec == 0) → immediate
            else:
                log("[GRACE] no grace period, transitioning")
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
            elapsed = (_datetime.now(LOCAL_TZ) - entered_at).total_seconds()
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
        skip_assess = data.get("skip_assess", False) if data else False
        if skip_assess:
            return TransitionAction(
                new_state="IMPLEMENTATION",
                run_cc=True,
                reset_reviewers=True,
            )
        pj = data.get("project", "") if data else ""
        comment = data.get("comment", "") if data else ""
        notif = get_notification_for_state(
            "ASSESSMENT", project=pj, batch=batch, comment=comment,
        )
        return TransitionAction(
            new_state="ASSESSMENT",
            impl_msg=notif.impl_msg,
        )

    if state == "ASSESSMENT":
        # assess-done 完了待ち
        if data and data.get("assessment"):
            return TransitionAction(
                new_state="IMPLEMENTATION",
                run_cc=True,
                reset_reviewers=True,
            )
        # 未完了 → タイムアウト判定のみ
        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    if state == "CODE_APPROVED":
        return TransitionAction(
            new_state="MERGE_SUMMARY_SENT",
            send_merge_summary=True,
        )

    if state in ("DESIGN_REVISE", "CODE_REVISE"):
        review_key = "design_reviews" if "DESIGN" in state else "code_reviews"
        revised_key = "design_revised" if "DESIGN" in state else "code_revised"
        review_state = "DESIGN_REVIEW" if "DESIGN" in state else "CODE_REVIEW"
        p2_fix = data.get("p2_fix", False) if data else False

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
            if "CODE" in state:
                from config import TEST_CONFIG
                project = data.get("project", "") if data else ""
                has_test = bool(TEST_CONFIG.get(project, {}).get("test_command"))
                skip_test = data.get("skip_test", False) if data else False
                if has_test and not skip_test:
                    log(
                        f"[REVISE] all issues with P0/P1{'/P2' if p2_fix else ''} revised, transitioning to CODE_TEST"
                    )
                    return TransitionAction(new_state="CODE_TEST", run_test=True)
            log(
                f"[REVISE] all issues with P0/P1{'/P2' if p2_fix else ''} revised, transitioning to {review_state}"
            )
            return TransitionAction(new_state=review_state, send_review=True)

        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    if state == "IMPLEMENTATION":
        # 1. 完了判定（最優先）
        if all(i.get("commit") for i in batch):
            from config import TEST_CONFIG
            project = data.get("project", "") if data else ""
            has_test = bool(TEST_CONFIG.get(project, {}).get("test_command"))
            skip_test = data.get("skip_test", False) if data else False
            if has_test and not skip_test:
                return TransitionAction(new_state="CODE_TEST", run_test=True)
            else:
                return TransitionAction(new_state="CODE_REVIEW", send_review=True)
        # 2. CC未実行 → 起動指示
        if data is not None and not _is_cc_running(data):
            return TransitionAction(run_cc=True)
        # 3. CC実行中だが進捗なし → タイムアウト判定
        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    if state == "CODE_TEST":
        if data is None:
            return TransitionAction()
        test_result = data.get("test_result")
        if test_result is None:
            block_sec = BLOCK_TIMERS.get("CODE_TEST", 600)
            block_sec += data.get("timeout_extension", 0)
            entered_at = _get_state_entered_at(data, "CODE_TEST")
            if entered_at is not None:
                elapsed = (_datetime.now(LOCAL_TZ) - entered_at).total_seconds()
                if elapsed >= block_sec:
                    return TransitionAction(
                        new_state="BLOCKED",
                        impl_msg="CODE_TEST タイムアウト。テストプロセスが応答しませんでした。",
                    )
            return TransitionAction()
        if test_result == "pass":
            return TransitionAction(new_state="CODE_REVIEW", send_review=True)
        # test_result == "fail"
        from config import MAX_TEST_RETRY
        retry_count = data.get("test_retry_count", 0)
        if retry_count >= MAX_TEST_RETRY:
            return TransitionAction(
                new_state="BLOCKED",
                impl_msg=f"テスト {retry_count} 回連続失敗。自動修復不能。",
            )
        return TransitionAction(new_state="CODE_TEST_FIX")

    if state == "CODE_TEST_FIX":
        if data is None:
            return TransitionAction()
        cc_pid = data.get("cc_pid")
        if cc_pid is not None and not _is_cc_running(data):
            return TransitionAction(new_state="CODE_TEST", run_test=True)
        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    return TransitionAction()


def _format_nudge_message(state: str, project: str, batch: list) -> str:
    """催促メッセージ生成。get_notification_for_state() に委譲。"""
    notif = get_notification_for_state(state, project=project, batch=batch)
    return notif.impl_msg or f"[gokrax] {project}: {state} — 対応してください。"


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
