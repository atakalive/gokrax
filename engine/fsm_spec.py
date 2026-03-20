from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime as _datetime, timedelta as _timedelta
from pathlib import Path

from config import (
    LOCAL_TZ, GOKRAX_CLI,
    SPEC_STATES, SPEC_REVIEW_TIMEOUT_SEC, SPEC_REVISE_TIMEOUT_SEC,
    SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC, SPEC_ISSUE_PLAN_TIMEOUT_SEC,
    SPEC_QUEUE_PLAN_TIMEOUT_SEC,
    MAX_SPEC_RETRIES, SPEC_REVISE_SELF_REVIEW_PASSES,
    SPEC_REVIEW_RAW_RETENTION_DAYS,
    NUDGE_GRACE_SEC,
)
from pipeline_io import (
    load_pipeline, update_pipeline, add_history,
)
from notify import (
    notify_discord, send_to_agent_queued,
)
from messages import render
from spec_review import (
    should_continue_review, build_review_history_entry,
    merge_reviews, format_merged_report,
    SpecReviewItem, SpecReviewResult,
)
from spec_issue import (
    build_issue_suggestion_prompt,
    parse_issue_suggestion_response,
    build_issue_plan_prompt,
    parse_issue_plan_response,
    build_queue_plan_prompt,
    parse_queue_plan_response,
)


@dataclass
class SpecTransitionAction:
    """check_transition_spec() の返り値。"""
    next_state: str | None = None
    expected_state: str | None = None   # DCL用: 現在のstate（競合検出）
    send_to: dict[str, str] | None = None  # {agent_id: message}
    discord_notify: str | None = None
    pipeline_updates: dict | None = None  # spec_config への更新差分
    error: str | None = None
    nudge_reviewers: list[str] | None = None   # 催促が必要なレビュアーリスト。None=催促不要、[]=催促不要（副作用なし）
    nudge_implementer: bool = False              # implementer 催促フラグ


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
            # rev_index > 1 でも last_changes がない場合は初回プロンプトを使う
            # （gokrax spec start --rev 2 で初回起動した場合など）
            has_prior_review = bool(spec_config.get("last_changes"))
            if rev_index <= 1 or not has_prior_review:
                prompt = render("spec.review", "initial",
                    project=project, spec_path=spec_path,
                    current_rev=current_rev, GOKRAX_CLI=GOKRAX_CLI,
                )
            else:
                last_changes = spec_config.get("last_changes") or {}
                prompt = render("spec.review", "revision",
                    project=project, spec_path=spec_path,
                    current_rev=current_rev, GOKRAX_CLI=GOKRAX_CLI,
                    changelog=last_changes.get("changelog_summary", "変更履歴なし"),
                    added=str(last_changes.get("added_lines", "?")),
                    removed=str(last_changes.get("removed_lines", "?")),
                    last_commit=spec_config.get("last_commit") or "unknown",
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
    if not current_reviews.get("reviewed_rev"):
        updates["_reviewed_rev"] = current_rev

    if all_complete:
        # should_continue_review() 用に patch 適用後の仮 spec_config を構築
        effective_cr = dict(current_reviews)
        effective_entries = dict(entries)
        effective_entries.update(cr_patch)
        effective_cr["entries"] = effective_entries
        effective_sc = dict(spec_config)
        effective_sc["current_reviews"] = effective_cr

        review_mode = data.get("review_mode", "standard")
        min_reviews_override = data.get("min_reviews_override")
        result = should_continue_review(effective_sc, review_mode, min_reviews_override=min_reviews_override)

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

        if result == "approved":
            # A1: current_reviews を review_history にアーカイブ
            history_entry = build_review_history_entry(effective_sc, now)
            updates["_review_history_append"] = history_entry
            # A4: current_reviews をクリア（entries 残留防止）
            updates["current_reviews"] = {}

        result_map = {
            "approved": ("SPEC_APPROVED", render("spec.approved", "notify_approved", project=project, rev=current_rev)),
            "revise":   ("SPEC_REVISE",   render("spec.review", "notify_complete", project=project, rev=current_rev, critical=c_count, major=m_count, minor=mi_count, suggestion=s_count)),
            "stalled":  ("SPEC_STALLED",  render("spec.stalled", "notify_stalled", project=project, rev=current_rev, remaining_p1_plus=p1_plus)),
            "failed":   ("SPEC_REVIEW_FAILED", render("spec.review", "notify_failed", project=project, rev=current_rev)),
            "paused":   ("SPEC_PAUSED",   render("spec.paused", "notify_paused", project=project, reason="パース失敗")),
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

    # --- 催促ロジック（#76）---
    # all_complete=False の場合のみここに到達
    # 猶予期間チェック（entered_at 取得不可時は安全側=催促しない）
    from engine.fsm import _get_state_entered_at
    entered_at = _get_state_entered_at(data, "SPEC_REVIEW")
    if entered_at is None:
        if send_to or updates:
            return SpecTransitionAction(
                next_state=None,
                send_to=send_to if send_to else None,
                pipeline_updates=updates if updates else None,
            )
        return SpecTransitionAction(next_state=None)

    try:
        elapsed = (now - entered_at).total_seconds()
    except TypeError:
        # naive datetime との減算失敗 → 安全側=催促しない
        if send_to or updates:
            return SpecTransitionAction(
                next_state=None,
                send_to=send_to if send_to else None,
                pipeline_updates=updates if updates else None,
            )
        return SpecTransitionAction(next_state=None)
    if elapsed < NUDGE_GRACE_SEC:
        if send_to or updates:
            return SpecTransitionAction(
                next_state=None,
                send_to=send_to if send_to else None,
                pipeline_updates=updates if updates else None,
            )
        return SpecTransitionAction(next_state=None)

    # 猶予期間経過 → 未完了レビュアーを特定
    # 対象: effective_status == "pending" かつ sent_at 設定済み（送信済みだが未応答）
    # 対象外: "received"（完了済み）/ "timeout"（タイムアウト済み）/ sent_at=None（未送信）
    pending_reviewers = [
        reviewer
        for reviewer, req in review_requests.items()
        if _effective_status(reviewer) == "pending" and req.get("sent_at") is not None
    ]

    result_action = SpecTransitionAction(
        next_state=None,
        nudge_reviewers=pending_reviewers if pending_reviewers else None,
        pipeline_updates=updates if updates else None,
    )
    if send_to:
        result_action.send_to = send_to
    return result_action


# ---------------------------------------------------------------------------
# Spec mode: _check_spec_revise（§6.1, §6.3, §10.2）
# ---------------------------------------------------------------------------

def _check_spec_revise(
    spec_config: dict,
    now: _datetime,
    data: dict,
) -> SpecTransitionAction:
    """SPEC_REVISE: タイムアウト検出。純粋関数（spec_config を mutate しない）。"""
    from spec_revise import (
        parse_revise_response, build_revise_completion_updates, build_revise_prompt,
        get_self_review_agent, build_self_review_prompt, parse_self_review_response,
        DEFAULT_SELF_REVIEW_CHECKLIST,
    )

    project = data.get("project", "")

    # -----------------------------------------------------------------------
    # (A) self_review 応答あり → パースして判定
    # -----------------------------------------------------------------------
    self_review_response = spec_config.get("_self_review_response")
    if self_review_response:
        expected_ids = spec_config.get("_self_review_expected_ids")
        result = parse_self_review_response(self_review_response, expected_ids=expected_ids)
        verdict = result["verdict"]

        if verdict == "clean":
            # self_review 通過 → SPEC_REVIEW へ遷移
            # pending 欠落はフェイルセーフ（Euler Major）
            raw_pending = spec_config.get("_self_review_pending_updates")
            if not isinstance(raw_pending, dict):
                return SpecTransitionAction(
                    next_state="SPEC_PAUSED",
                    discord_notify=render("spec.paused", "notify_paused",
                        project=project,
                        reason="セルフレビュー通過したが _self_review_pending_updates が欠落/不正。人間の介入が必要です",
                    ),
                    pipeline_updates={"paused_from": "SPEC_REVISE"},
                )
            pending = dict(raw_pending)
            pending["_self_review_sent"] = None
            pending["_self_review_response"] = None
            pending["_self_review_pass"] = 0
            pending["_self_review_pending_updates"] = None
            pending["_self_review_expected_ids"] = None
            pending["_revise_sent"] = None  # 遷移完了。(E) 抑止不要になるのでクリア
            current_rev = pending.get("current_rev", spec_config.get("current_rev", "?"))
            reviewer_count = len(spec_config.get("review_requests", {}))
            revise_msg = render("spec.revise", "notify_done", project=project, rev=current_rev, commit=pending.get("last_commit", ""))
            review_msg = render("spec.review", "notify_start", project=project, rev=current_rev, reviewer_count=reviewer_count)
            return SpecTransitionAction(
                next_state="SPEC_REVIEW",
                discord_notify=f"{revise_msg}\n{review_msg}",
                pipeline_updates=pending,
            )

        elif verdict == "issues_found":
            # 差し戻し: implementer に No 項目を通知して再 revise
            # _self_review_pass はインクリメントしない（機械的失敗ではない）
            failed_items = result["items"]
            implementer = spec_config.get("spec_implementer", "")
            feedback_lines = ["セルフレビューで以下の問題が検出されました。修正して再度 revise-submit してください:\n"]
            for item in failed_items:
                feedback_lines.append(f"- **{item['id']}**: {item.get('evidence', '(証拠なし)')}")
            feedback_msg = "\n".join(feedback_lines)
            return SpecTransitionAction(
                next_state=None,  # SPEC_REVISE のまま
                send_to={implementer: feedback_msg} if implementer else None,
                discord_notify=render("spec.revise", "notify_self_review_failed", project=project, failed_count=len(failed_items)),
                pipeline_updates={
                    # self_review 関連フィールドを全クリア（self_review フェーズ終了）
                    "_self_review_sent": None,
                    "_self_review_response": None,
                    "_self_review_pass": 0,
                    "_self_review_pending_updates": None,  # 破棄（再 revise で再計算）
                    "_self_review_expected_ids": None,
                    # _revise_sent を now に更新:
                    # (1) (E) 二重送信防止 (2) (D) タイムアウト基準リセット
                    "_revise_sent": now.isoformat(),
                    # _revise_retry_at クリア: 古いタイムアウト基準が残るのを防ぐ（Euler P0-1）
                    "_revise_retry_at": None,
                    # _revise_response をクリアして再 revise-submit を受付可能にする
                    "_revise_response": None,
                },
            )

        else:  # parse_failed
            current_pass = spec_config.get("_self_review_pass", 0)
            if current_pass + 1 >= SPEC_REVISE_SELF_REVIEW_PASSES:
                return SpecTransitionAction(
                    next_state="SPEC_PAUSED",
                    discord_notify=render("spec.paused", "notify_paused",
                        project=project,
                        reason=f"セルフレビューのパース失敗が{current_pass + 1}回に到達。人間の介入が必要です",
                    ),
                    pipeline_updates={
                        "paused_from": "SPEC_REVISE",
                        "_self_review_sent": None,
                        "_self_review_response": None,
                        "_self_review_pass": 0,
                        "_self_review_expected_ids": None,
                        # _self_review_pending_updates は保持（PAUSED解除後に再利用可能）
                    },
                )
            # リトライ: 再送信
            agent = get_self_review_agent(spec_config)
            prompt = build_self_review_prompt(spec_config, data)
            return SpecTransitionAction(
                next_state=None,
                send_to={agent: prompt},
                pipeline_updates={
                    "_self_review_sent": now.isoformat(),
                    "_self_review_response": None,
                    "_self_review_pass": current_pass + 1,
                },
            )

    # -----------------------------------------------------------------------
    # (B) self_review 送信済み & 応答なし → タイムアウトチェック
    # -----------------------------------------------------------------------
    self_review_sent = spec_config.get("_self_review_sent")
    if self_review_sent:
        # SPEC_REVIEW_TIMEOUT_SEC を流用（self_review 専用タイムアウトは不要。運用上同一SLA）
        try:
            baseline = _datetime.fromisoformat(self_review_sent)
            elapsed = (now - baseline).total_seconds()
        except (ValueError, TypeError):
            # 日付パース失敗 → 機械的失敗として扱う（Euler Minor-B: 永久待ち防止）
            current_pass = spec_config.get("_self_review_pass", 0)
            if current_pass + 1 >= SPEC_REVISE_SELF_REVIEW_PASSES:
                return SpecTransitionAction(
                    next_state="SPEC_PAUSED",
                    discord_notify=render("spec.paused", "notify_paused",
                        project=project,
                        reason=f"セルフレビュー _self_review_sent 日付パース失敗 + パス{current_pass + 1}回到達",
                    ),
                    pipeline_updates={
                        "paused_from": "SPEC_REVISE",
                        "_self_review_sent": None,
                        "_self_review_response": None,
                        "_self_review_pass": 0,
                        "_self_review_expected_ids": None,
                    },
                )
            agent = get_self_review_agent(spec_config)
            prompt = build_self_review_prompt(spec_config, data)
            return SpecTransitionAction(
                next_state=None,
                send_to={agent: prompt},
                pipeline_updates={
                    "_self_review_sent": now.isoformat(),
                    "_self_review_response": None,
                    "_self_review_pass": current_pass + 1,
                },
            )

        if elapsed < SPEC_REVIEW_TIMEOUT_SEC:
            return SpecTransitionAction(next_state=None)  # まだ待つ

        # タイムアウト → 機械的失敗としてリトライ or SPEC_PAUSED
        current_pass = spec_config.get("_self_review_pass", 0)
        if current_pass + 1 >= SPEC_REVISE_SELF_REVIEW_PASSES:
            return SpecTransitionAction(
                next_state="SPEC_PAUSED",
                discord_notify=render("spec.paused", "notify_paused",
                    project=project,
                    reason=f"セルフレビュータイムアウトが{current_pass + 1}回に到達。人間の介入が必要です",
                ),
                pipeline_updates={
                    "paused_from": "SPEC_REVISE",
                    "_self_review_sent": None,
                    "_self_review_response": None,
                    "_self_review_pass": 0,
                    "_self_review_expected_ids": None,
                    # _self_review_pending_updates は保持（PAUSED解除後に再利用可能）
                },
            )
        # リトライ
        agent = get_self_review_agent(spec_config)
        prompt = build_self_review_prompt(spec_config, data)
        return SpecTransitionAction(
            next_state=None,
            send_to={agent: prompt},
            discord_notify=render("spec.paused", "notify_failure",
                project=project,
                kind="セルフレビュータイムアウト",
                detail=f"retry {current_pass + 1}/{SPEC_REVISE_SELF_REVIEW_PASSES}",
            ),
            pipeline_updates={
                "_self_review_sent": now.isoformat(),
                "_self_review_response": None,
                "_self_review_pass": current_pass + 1,
            },
        )

    # -----------------------------------------------------------------------
    # (C) implementer 改訂完了報告あり → self_review フェーズ開始
    # -----------------------------------------------------------------------
    revise_response = spec_config.get("_revise_response")
    if revise_response:
        parsed = parse_revise_response(revise_response, spec_config.get("current_rev", "1"))
        if parsed is None:
            # パース失敗 → PAUSED
            return SpecTransitionAction(
                next_state="SPEC_PAUSED",
                discord_notify=render("spec.paused", "notify_paused", project=project, reason="REVISE完了報告のパース失敗"),
                pipeline_updates={"paused_from": "SPEC_REVISE"},
            )
        if not parsed.get("commit"):
            return SpecTransitionAction(
                next_state="SPEC_PAUSED",
                discord_notify=render("spec.revise", "notify_commit_failed", project=project, rev=spec_config.get("current_rev", "1")),
                pipeline_updates={"paused_from": "SPEC_REVISE"},
            )
        # 差分0 → PAUSED（§11: 変更なし）
        changes = parsed.get("changes", {})
        if changes.get("added_lines", 0) + changes.get("removed_lines", 0) == 0:
            return SpecTransitionAction(
                next_state="SPEC_PAUSED",
                discord_notify=render("spec.revise", "notify_no_changes", project=project, rev=spec_config.get("current_rev", "1")),
                pipeline_updates={"paused_from": "SPEC_REVISE", "_revise_response": None},
            )

        # 改訂完了 → self_review フェーズ開始
        try:
            updates = build_revise_completion_updates(spec_config, parsed, now)
        except ValueError as e:
            logging.error("build_revise_completion_updates failed: %s", e)
            return SpecTransitionAction(
                next_state="SPEC_PAUSED",
                discord_notify=f"⚠️ {project}: revise completion failed: {e}",
                pipeline_updates={"paused_from": "SPEC_REVISE", "_revise_response": None},
            )
        updates["_revise_response"] = None  # 消費済みクリア
        # 注意: _revise_sent は維持する（クリアしない）。
        # issues_found で差し戻し後、(E) の初回送信が誤発火するのを防ぐため。
        checklist = spec_config.get("self_review_checklist", DEFAULT_SELF_REVIEW_CHECKLIST)
        expected_ids = [c["id"] for c in checklist]
        agent = get_self_review_agent(spec_config)
        # self_review プロンプトには更新後の config を使用（rev/spec_path を反映）
        merged_cfg = {**spec_config, **updates}
        prompt = build_self_review_prompt(merged_cfg, data, checklist=checklist)
        return SpecTransitionAction(
            next_state=None,  # SPEC_REVISE のまま
            send_to={agent: prompt},
            pipeline_updates={
                "_revise_response": None,
                # _revise_sent は維持（クリアしない）
                "_self_review_sent": now.isoformat(),
                "_self_review_response": None,  # 過去残骸の誤判定防止（明示的クリア）
                "_self_review_pass": 0,
                "_self_review_pending_updates": updates,  # 遷移用データを保存
                "_self_review_expected_ids": expected_ids,  # parse 時の ID 検証用
            },
        )
    # --- ここまで S-5 追加。以下は implementer 送信 + タイムアウトチェック ---

    # implementer への初回リバイス依頼送信（§6.1）
    implementer = spec_config.get("spec_implementer", "")
    revise_sent = spec_config.get("_revise_sent")
    if not revise_sent and implementer:
        # current_reviews.entries からレビュー指摘を整形
        current_reviews = spec_config.get("current_reviews", {})
        entries = current_reviews.get("entries", {})
        reviews_for_merge = []
        for reviewer, entry in entries.items():
            if not isinstance(entry, dict) or entry.get("status") != "received":
                continue
            items_raw = entry.get("items", []) or []
            review_items = []
            for it in items_raw:
                if isinstance(it, dict):
                    review_items.append(SpecReviewItem(
                        id=it.get("id", ""),
                        severity=it.get("severity", "minor"),
                        section=it.get("section", ""),
                        title=it.get("title", ""),
                        description=it.get("description", ""),
                        suggestion=it.get("suggestion", ""),
                        reviewer=reviewer,
                        normalized_id=it.get("normalized_id", f"{reviewer}:{it.get('id', '')}"),
                    ))
            reviews_for_merge.append(SpecReviewResult(
                reviewer=reviewer,
                verdict=entry.get("verdict", ""),
                items=review_items,
                raw_text="",
                parse_success=True,
            ))
        if reviews_for_merge:
            merged = merge_reviews(reviews_for_merge)
            current_rev = spec_config.get("current_rev", "1")
            merged_md = format_merged_report(merged, current_rev)
            prompt = build_revise_prompt(spec_config, merged_md, data)
            return SpecTransitionAction(
                next_state=None,
                send_to={implementer: prompt},
                pipeline_updates={"_revise_sent": now.isoformat()},
            )

    # タイムアウト起点: _revise_retry_at（リトライ後）or _revise_sent or history（初回）
    retry_at_str = spec_config.get("_revise_retry_at")
    if retry_at_str:
        try:
            baseline = _datetime.fromisoformat(retry_at_str)
        except (ValueError, TypeError):
            baseline = None
    else:
        baseline = None

    if baseline is None and revise_sent:
        try:
            baseline = _datetime.fromisoformat(revise_sent)
        except (ValueError, TypeError):
            baseline = None

    if baseline is None:
        from engine.fsm import _get_state_entered_at
        baseline = _get_state_entered_at(data, "SPEC_REVISE")

    if baseline is None:
        return SpecTransitionAction(next_state=None)

    try:
        elapsed = (now - baseline).total_seconds()
    except TypeError:
        # naive datetime との減算失敗 → 安全側=催促しない
        return SpecTransitionAction(next_state=None)

    # --- implementer 催促（#76）---
    # 猶予期間経過後、タイムアウト到達前の区間で催促
    if elapsed >= NUDGE_GRACE_SEC and elapsed < SPEC_REVISE_TIMEOUT_SEC:
        return SpecTransitionAction(next_state=None, nudge_implementer=True)

    if elapsed < SPEC_REVISE_TIMEOUT_SEC:
        return SpecTransitionAction(next_state=None)

    # タイムアウト: retry_counts を更新
    retry_counts = spec_config.get("retry_counts", {})
    revise_retries = retry_counts.get("SPEC_REVISE", 0)

    if revise_retries >= MAX_SPEC_RETRIES:
        return SpecTransitionAction(
            next_state="SPEC_PAUSED",
            discord_notify=render("spec.paused", "notify_paused", project=project, reason=f"REVISE タイムアウト × {MAX_SPEC_RETRIES}"),
            pipeline_updates={
                "paused_from": "SPEC_REVISE",
                "retry_counts": {**retry_counts, "SPEC_REVISE": revise_retries + 1},
                "_revise_retry_at": None,  # クリア
            },
        )

    # リトライ: カウント更新 + 起点リセット
    return SpecTransitionAction(
        next_state=None,
        discord_notify=render("spec.paused", "notify_failure", project=project, kind="REVISE タイムアウト", detail=f"retry {revise_retries + 1}/{MAX_SPEC_RETRIES}"),
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
            prompt = build_issue_suggestion_prompt(spec_config, data, reviewer=reviewer)
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
                discord_notify=render("spec.paused", "notify_paused", project=project, reason="Issue分割提案: 有効応答なし"),
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
                discord_notify=render("spec.paused", "notify_paused", project=project, reason="ISSUE_PLAN応答パース失敗"),
                pipeline_updates={"paused_from": "ISSUE_PLAN"},
            )
        # 成功: no_queue チェック
        n = len(parsed["created_issues"])
        next_state = "SPEC_DONE" if spec_config.get("no_queue") else "QUEUE_PLAN"
        notify_msg = render("spec.issue_plan", "notify_done", project=project, issue_count=n)
        if next_state == "SPEC_DONE":
            notify_msg = f"{notify_msg}\n{render('spec.done', 'notify_done', project=project)}"
        return SpecTransitionAction(
            next_state=next_state,
            discord_notify=notify_msg,
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
            discord_notify=render("spec.paused", "notify_paused", project=project, reason=f"ISSUE_PLAN タイムアウト × {MAX_SPEC_RETRIES}"),
            pipeline_updates={
                "paused_from": "ISSUE_PLAN",
                "retry_counts": {**retry_counts, "ISSUE_PLAN": issue_plan_retries + 1},
            },
        )

    return SpecTransitionAction(
        next_state=None,
        discord_notify=render("spec.paused", "notify_failure", project=project, kind="ISSUE_PLAN タイムアウト", detail=f"retry {issue_plan_retries + 1}/{MAX_SPEC_RETRIES}"),
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
                discord_notify=render("spec.paused", "notify_paused", project=project, reason="QUEUE_PLAN応答パース失敗"),
                pipeline_updates={"paused_from": "QUEUE_PLAN"},
            )
        batches = parsed["batches"]
        done_msg = f"{render('spec.queue_plan', 'notify_done', project=project, batch_count=batches)}\n{render('spec.done', 'notify_done', project=project)}"
        return SpecTransitionAction(
            next_state="SPEC_DONE",
            discord_notify=done_msg,
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
            discord_notify=render("spec.paused", "notify_paused", project=project, reason=f"QUEUE_PLAN タイムアウト × {MAX_SPEC_RETRIES}"),
            pipeline_updates={
                "paused_from": "QUEUE_PLAN",
                "retry_counts": {**retry_counts, "QUEUE_PLAN": queue_plan_retries + 1},
            },
        )

    return SpecTransitionAction(
        next_state=None,
        discord_notify=render("spec.paused", "notify_failure", project=project, kind="QUEUE_PLAN タイムアウト", detail=f"retry {queue_plan_retries + 1}/{MAX_SPEC_RETRIES}"),
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
            discord_notify=render("spec.paused", "notify_paused", project=project, reason=f"未知状態 {state}"),
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
                discord_notify=render("spec.done", "notify_done", project=project),
            )
        if spec_config.get("auto_continue"):
            reset_patch = {
                r: {
                    "status": "pending",
                    "sent_at": None,
                    "timeout_at": None,
                    "last_nudge_at": None,
                    "response": None,
                }
                for r in spec_config.get("review_requests", {})
            }
            return SpecTransitionAction(
                next_state="ISSUE_SUGGESTION",
                discord_notify=render("spec.approved", "notify_approved_auto", project=project, rev=spec_config.get("current_rev", "?")),
                pipeline_updates={
                    "review_requests_patch": reset_patch,
                },
            )
        # デフォルト: M確認待ち（通知は遷移元で発火済み）
        return SpecTransitionAction(next_state=None)
    elif state == "ISSUE_SUGGESTION":
        return _check_issue_suggestion(spec_config, now, data)
    elif state == "ISSUE_PLAN":
        return _check_issue_plan(spec_config, now, data)
    elif state == "QUEUE_PLAN":
        return _check_queue_plan(spec_config, now, data)
    elif state == "SPEC_DONE":
        # spec mode 終了 → IDLE 自動遷移。flags reset は _apply_spec_action 内で実施。
        return SpecTransitionAction(next_state="IDLE")
    elif state in ("SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED"):
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
    from engine.shared import log
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
_SPEC_TERMINAL_STATES = {"SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED"}


def _cleanup_expired_spec_files(pipelines_dir: str) -> None:
    """pipelines_dir内のspec-review生成物で、RETENTION超過ファイルを削除（§12.1）。

    削除対象: *_rev*.md パターンのファイルのみ（命名規則で限定）。
    mtime基準でSPEC_REVIEW_RAW_RETENTION_DAYS超過を判定。
    ディレクトリ不在・is_dir失敗はログのみで安全にreturn。
    """
    from engine.shared import log
    pd = Path(pipelines_dir)
    if not pd.is_dir():
        return
    cutoff = _datetime.now(LOCAL_TZ) - _timedelta(days=SPEC_REVIEW_RAW_RETENTION_DAYS)
    for f in pd.iterdir():
        if not f.is_file():
            continue
        if not _SPEC_REVIEW_FILE_PATTERN.match(f.name):
            continue
        try:
            mtime = _datetime.fromtimestamp(f.stat().st_mtime, tz=LOCAL_TZ)
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
    from engine.shared import log
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
            # SPEC_DONE → IDLE: spec mode 終了のクリーンアップ（cmd_spec_done と同等）
            if old_state == "SPEC_DONE" and action2.next_state == "IDLE":
                data["spec_mode"] = False
                data["spec_config"] = {}

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
            # _review_history_append: review_history に entry を append（上書きではなく追記）
            rh_entry = pu.pop("_review_history_append", None)
            if rh_entry is not None:
                sc.setdefault("review_history", []).append(rh_entry)
            # _reviewed_rev: current_reviews のトップレベルに reviewed_rev を設定
            reviewed_rev = pu.pop("_reviewed_rev", None)
            if reviewed_rev is not None:
                cr = sc.setdefault("current_reviews", {})
                cr["reviewed_rev"] = reviewed_rev
            # 残りのフィールドは直接 update
            sc.update(pu)
            data["spec_config"] = sc

        if (action2.next_state or action2.pipeline_updates or action2.send_to
                or action2.discord_notify or action2.nudge_reviewers or action2.nudge_implementer):
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
                    notify_discord(render("spec.paused", "notify_failure", project=pj, kind="送信失敗", detail=f"agent={agent_id}"))
        # spec mode レビュアー催促（#76）
        if applied_action.nudge_reviewers:
            from engine.shared import _is_agent_inactive
            from config import INACTIVE_THRESHOLD_SEC
            # 最新の pipeline を再読込（並行プロセス/連続tickでの二重催促防止）
            fresh_data = load_pipeline(pipeline_path)
            fresh_sc = fresh_data.get("spec_config", {})
            pj_fresh = fresh_data.get("project", pipeline_path.stem)
            project_fresh = fresh_data.get("project", "")
            current_rev_fresh = fresh_sc.get("current_rev", "1")
            spec_path_fresh = fresh_sc.get("spec_path", "")

            woken = []
            for reviewer in applied_action.nudge_reviewers:
                if not _is_agent_inactive(reviewer):
                    continue
                # INACTIVE_THRESHOLD_SEC 以内の再催促をスキップ
                rr = fresh_sc.get("review_requests", {}).get(reviewer, {})
                last_nudge = rr.get("last_nudge_at")
                if last_nudge:
                    try:
                        since = (_datetime.now(LOCAL_TZ) - _datetime.fromisoformat(last_nudge)).total_seconds()
                        if since < INACTIVE_THRESHOLD_SEC:
                            continue
                    except (ValueError, TypeError):
                        continue  # parse 失敗時は安全側=催促しない

                nudge_msg = render("spec.review", "nudge",
                    project=project_fresh, current_rev=current_rev_fresh,
                    spec_path=spec_path_fresh, reviewer=reviewer,
                    GOKRAX_CLI=GOKRAX_CLI,
                )
                if send_to_agent_queued(reviewer, nudge_msg):
                    woken.append(reviewer)

            if woken:
                def _set_spec_nudge(data, reviewers=woken):
                    sc = data.setdefault("spec_config", {})
                    rr = sc.setdefault("review_requests", {})
                    for r in reviewers:
                        rr.setdefault(r, {})["last_nudge_at"] = _datetime.now(LOCAL_TZ).isoformat()
                    data["spec_config"] = sc
                update_pipeline(pipeline_path, _set_spec_nudge)
                ts = _datetime.now(LOCAL_TZ).strftime("%m/%d %H:%M")
                notify_discord(f"[Spec][{pj_fresh}] レビュアーを催促: {', '.join(woken)} ({ts})")
        # spec mode implementer 催促（#76）
        if applied_action.nudge_implementer:
            from engine.shared import _is_agent_inactive
            from config import INACTIVE_THRESHOLD_SEC
            # 最新の pipeline を再読込（二重催促防止）
            fresh_data = load_pipeline(pipeline_path)
            fresh_sc = fresh_data.get("spec_config", {})
            pj_fresh = fresh_data.get("project", pipeline_path.stem)
            implementer = fresh_sc.get("spec_implementer", "")
            project_fresh = fresh_data.get("project", "")
            current_rev_fresh = fresh_sc.get("current_rev", "1")

            if implementer and _is_agent_inactive(implementer):
                last_nudge = fresh_sc.get("_last_nudge_implementer")
                should_nudge = True
                if last_nudge:
                    try:
                        since = (_datetime.now(LOCAL_TZ) - _datetime.fromisoformat(last_nudge)).total_seconds()
                        if since < INACTIVE_THRESHOLD_SEC:
                            should_nudge = False
                    except (ValueError, TypeError):
                        should_nudge = False  # parse 失敗時は安全側=催促しない

                if should_nudge:
                    nudge_msg = render("spec.revise", "nudge",
                        project=project_fresh, current_rev=current_rev_fresh,
                        GOKRAX_CLI=GOKRAX_CLI,
                    )
                    if send_to_agent_queued(implementer, nudge_msg):
                        def _set_impl_nudge(data, impl=implementer):
                            sc = data.get("spec_config", {})
                            sc["_last_nudge_implementer"] = _datetime.now(LOCAL_TZ).isoformat()
                            data["spec_config"] = sc
                        update_pipeline(pipeline_path, _set_impl_nudge)
                        ts = _datetime.now(LOCAL_TZ).strftime("%m/%d %H:%M")
                        notify_discord(f"[Spec][{pj_fresh}] implementer {implementer} を催促 ({ts})")
        if applied_action.discord_notify:
            notify_discord(applied_action.discord_notify)
        should_cleanup = (
            applied_action.next_state in _SPEC_TERMINAL_STATES
            or (action.expected_state == "SPEC_DONE" and applied_action.next_state == "IDLE")
        )
        if should_cleanup:
            pd = orig_data.get("spec_config", {}).get("pipelines_dir")
            if pd:
                _cleanup_expired_spec_files(pd)
        # SPEC_DONE → IDLE 後: auto_qrun が True の場合のみキュー自動起動
        if (action.expected_state == "SPEC_DONE"
                and applied_action.next_state == "IDLE"
                and orig_data.get("spec_config", {}).get("auto_qrun")):
            from watchdog import _check_queue
            _check_queue()
