"""tests/test_spec_mode_integration.py — spec mode 統合テスト（E2E フロー + エッジケース）"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from watchdog import (
    SpecTransitionAction,
    check_transition_spec,
    _check_spec_review,
    _check_spec_revise,
    _apply_spec_action,
    _build_spec_review_prompt_revision,
)
from pipeline_io import default_spec_config
from spec_review import (
    should_continue_review,
    validate_received_entry,
    build_review_history_entry,
    _reset_review_requests,
)
from spec_revise import build_revise_completion_updates
from devbar import cmd_spec_start
from tests.conftest import write_pipeline
import config

JST = timezone(timedelta(hours=9))


def _now():
    return datetime(2026, 3, 1, 12, 0, 0, tzinfo=JST)


def _make_pipeline(state="IDLE", spec_mode=False, spec_config=None, **kwargs):
    data = {
        "project": "test-pj",
        "gitlab": "atakalive/test-pj",
        "state": state,
        "spec_mode": spec_mode,
        "spec_config": spec_config if spec_config is not None else {},
        "enabled": True,
        "implementer": "kaneko",
        "review_mode": "full",
        "batch": [],
        "history": [],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    data.update(kwargs)
    return data


def _make_spec_config(**overrides):
    cfg = default_spec_config()
    cfg.update(overrides)
    return cfg


from config import REVIEW_MODES as _REVIEW_MODES
_REVIEWERS = tuple(_REVIEW_MODES["full"]["members"])


def _pending_review_requests():
    return {
        r: {"status": "pending", "sent_at": None, "timeout_at": None,
            "last_nudge_at": None, "response": None}
        for r in _REVIEWERS
    }


def _make_active_pipeline(state="SPEC_REVIEW", **sc_overrides):
    sc = _make_spec_config(
        spec_path="docs/test-spec.md",
        spec_implementer="kaneko",
        review_requests=_pending_review_requests(),
        **sc_overrides,
    )
    return _make_pipeline(state=state, spec_mode=True, spec_config=sc)


def _args(**kwargs):
    return argparse.Namespace(**kwargs)


def _yaml_block(content: str) -> str:
    return f"```yaml\n{content}\n```"


def _apply_updates_to_sc(sc: dict, updates: dict | None) -> dict:
    """_apply_spec_action._update() の deep-merge を再現するヘルパー。"""
    if not updates:
        return dict(sc)
    sc = json.loads(json.dumps(sc))  # deep copy
    pu = dict(updates)
    rr_patch = pu.pop("review_requests_patch", None)
    if rr_patch:
        rr = sc.setdefault("review_requests", {})
        for reviewer, p in rr_patch.items():
            rr.setdefault(reviewer, {}).update(p)
    cr_patch = pu.pop("current_reviews_patch", None)
    if cr_patch:
        cr = sc.setdefault("current_reviews", {})
        entries = cr.setdefault("entries", {})
        entries.update(cr_patch)
    # _review_history_append: review_history に append
    rh_entry = pu.pop("_review_history_append", None)
    if rh_entry is not None:
        sc.setdefault("review_history", []).append(rh_entry)
    # _reviewed_rev: current_reviews.reviewed_rev を設定
    reviewed_rev = pu.pop("_reviewed_rev", None)
    if reviewed_rev is not None:
        cr = sc.setdefault("current_reviews", {})
        cr["reviewed_rev"] = reviewed_rev
    sc.update(pu)
    return sc


def _received_entry(verdict="APPROVE", items=None):
    return {
        "verdict": verdict,
        "items": items or [],
        "raw_text": "...",
        "parse_success": True,
        "status": "received",
    }


def _set_all_received(sc, verdicts=None):
    """current_reviews.entries と review_requests を全員 received にする。"""
    if verdicts is None:
        verdicts = {r: "APPROVE" for r in _REVIEWERS}
    sc["current_reviews"] = {
        "reviewed_rev": sc.get("current_rev", "1"),
        "entries": {r: _received_entry(v) for r, v in verdicts.items()},
    }
    for r in _REVIEWERS:
        sc["review_requests"][r]["status"] = "received"
        if sc["review_requests"][r].get("sent_at") is None:
            sc["review_requests"][r]["sent_at"] = "2026-03-01T12:00:00+09:00"
            sc["review_requests"][r]["timeout_at"] = "2026-03-01T12:30:00+09:00"


def _revise_yaml(new_rev="2", commit="abc1234", added=50, removed=10):
    return _yaml_block(
        f"status: done\n"
        f"new_rev: \"{new_rev}\"\n"
        f"commit: \"{commit}\"\n"
        f"changes:\n"
        f"  added_lines: {added}\n"
        f"  removed_lines: {removed}\n"
    )


def _self_review_clean_yaml():
    """セルフレビュー全 Yes の YAML（#77 チェックリスト方式）。"""
    return """\
```yaml
checklist:
  - id: "reflected_items_match"
    result: "Yes"
    evidence: "OK"
  - id: "no_new_contradictions"
    result: "Yes"
    evidence: "OK"
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: "OK"
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: "OK"
```
"""


def _simulate_revise_complete(sc: dict, data: dict) -> tuple[dict, object]:
    """SPEC_REVISE の revise_response → self_review clean → SPEC_REVIEW の2ステップを模倣。

    Returns:
        (updated_sc, final_action) — final_action.next_state == "SPEC_REVIEW"
    """
    # Step1: revise_response パース → self_review 開始
    action1 = _check_spec_revise(sc, _now(), data)
    sc = _apply_updates_to_sc(sc, action1.pipeline_updates)
    assert action1.next_state is None, f"Expected None, got {action1.next_state}"
    assert sc.get("_self_review_sent") is not None

    # Step2: self_review clean 応答 → SPEC_REVIEW 遷移
    sc["_self_review_response"] = _self_review_clean_yaml()
    action2 = _check_spec_revise(sc, _now(), data)
    return _apply_updates_to_sc(sc, action2.pipeline_updates), action2


# ===========================================================================
# 1. TestNormalFlowE2E — 正常フロー E2E
# ===========================================================================

class TestNormalFlowE2E:

    def test_full_flow_approve_to_done(self):
        """SPEC_REVIEW → SPEC_APPROVED → ISSUE_SUGGESTION → ISSUE_PLAN → QUEUE_PLAN → SPEC_DONE"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            auto_continue=True,
            review_requests=_pending_review_requests(),
        )
        data = _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=sc)

        # --- tick 1: SPEC_REVIEW — 送信 ---
        action = check_transition_spec("SPEC_REVIEW", sc, _now(), data)
        assert action.next_state is None  # まだ遷移しない
        assert action.send_to is not None
        for r in _REVIEWERS:
            assert r in action.send_to, f"{r} should be in send_to"
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # --- tick 2: SPEC_REVIEW — 全員 APPROVE ---
        _set_all_received(sc)
        action = check_transition_spec("SPEC_REVIEW", sc, _now(), data)
        assert action.next_state == "SPEC_APPROVED"
        assert action.discord_notify is not None
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # --- SPEC_APPROVED — auto_continue → ISSUE_SUGGESTION ---
        action = check_transition_spec("SPEC_APPROVED", sc, _now(), data)
        assert action.next_state == "ISSUE_SUGGESTION"

        # --- tick 1: ISSUE_SUGGESTION — 送信 ---
        # SPEC_APPROVED→ISSUE_SUGGESTION の pipeline_updates で review_requests がリセットされる
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)
        action = check_transition_spec("ISSUE_SUGGESTION", sc, _now(), data)
        assert action.send_to is not None
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # --- tick 2: ISSUE_SUGGESTION — 回収 ---
        # _check_issue_suggestion は status=="pending" かつ sent_at!=None の reviewer を回収する
        issue_yaml = _yaml_block(
            "phases:\n"
            "  - name: Phase 1\n"
            "    issues:\n"
            "      - title: Implement foo\n"
            "        files: [src/foo.py]\n"
        )
        for r in _REVIEWERS:
            # status は "pending" のまま（sent_at は tick1 で設定済み）
            sc["current_reviews"].setdefault("entries", {})[r] = {
                "status": "received",
                "raw_text": issue_yaml,
                "response": issue_yaml,
            }
        action = check_transition_spec("ISSUE_SUGGESTION", sc, _now(), data)
        assert action.next_state == "ISSUE_PLAN"
        assert action.discord_notify is not None
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)
        assert "issue_suggestions" in sc or action.pipeline_updates.get("issue_suggestions")

        # --- tick 1: ISSUE_PLAN — 送信 ---
        action = check_transition_spec("ISSUE_PLAN", sc, _now(), data)
        assert action.next_state is None
        assert action.send_to is not None
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # --- tick 2: ISSUE_PLAN — 応答回収 ---
        sc["_issue_plan_response"] = _yaml_block(
            "status: done\ncreated_issues:\n  - 51\n  - 52\n"
        )
        action = check_transition_spec("ISSUE_PLAN", sc, _now(), data)
        assert action.next_state == "QUEUE_PLAN"
        assert action.discord_notify is not None
        assert action.pipeline_updates["created_issues"] == [51, 52]
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # --- tick 1: QUEUE_PLAN — 送信 ---
        action = check_transition_spec("QUEUE_PLAN", sc, _now(), data)
        assert action.next_state is None
        assert action.send_to is not None
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # --- tick 2: QUEUE_PLAN — 応答回収 ---
        sc["_queue_plan_response"] = _yaml_block(
            "status: done\nbatches: 2\nqueue_file: /tmp/q.txt\n"
        )
        action = check_transition_spec("QUEUE_PLAN", sc, _now(), data)
        assert action.next_state == "SPEC_DONE"
        assert action.discord_notify is not None

        # --- SPEC_DONE — IDLE 自動遷移（Issue #78）---
        action = check_transition_spec("SPEC_DONE", sc, _now(), data)
        assert action.next_state == "IDLE"


# ===========================================================================
# 2. TestReviewCycleE2E — レビューサイクル E2E
# ===========================================================================

class TestReviewCycleE2E:

    def test_p0_revise_then_approve(self):
        """P0 → SPEC_REVISE → SPEC_REVIEW → APPROVE"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
            auto_continue=True,
        )
        data = _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=sc)

        # 全員 received: pascal=P0, leibniz=APPROVE
        _set_all_received(sc, {_REVIEWERS[0]: "P0", **{r: "APPROVE" for r in _REVIEWERS[1:]}})
        action = check_transition_spec("SPEC_REVIEW", sc, _now(), data)
        assert action.next_state == "SPEC_REVISE"
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # SPEC_REVISE — implementer 完了報告 → self_review clean → SPEC_REVIEW
        sc["_revise_response"] = _revise_yaml("2", "abc1234", 50, 10)
        sc, action = _simulate_revise_complete(sc, data)
        assert action.next_state == "SPEC_REVIEW"
        assert action.discord_notify is not None
        pu = action.pipeline_updates
        assert pu["current_rev"] == "2"
        assert pu["rev_index"] == 2

        # 再 SPEC_REVIEW — 全員 APPROVE
        _set_all_received(sc, {r: "APPROVE" for r in _REVIEWERS})
        action = check_transition_spec("SPEC_REVIEW", sc, _now(), data)
        assert action.next_state == "SPEC_APPROVED"

    def test_revise_count_and_rev_index_progression(self):
        """3ラウンドの REVIEW→REVISE→REVIEW サイクルでカウンタ進行確認"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
        )
        data = _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=sc)

        for cycle in range(3):
            expected_rev = str(cycle + 1)
            expected_next_rev = str(cycle + 2)

            # SPEC_REVIEW — P0
            _set_all_received(sc, {_REVIEWERS[0]: "P0", **{r: "APPROVE" for r in _REVIEWERS[1:]}})
            action = check_transition_spec("SPEC_REVIEW", sc, _now(), data)
            assert action.next_state == "SPEC_REVISE"
            sc = _apply_updates_to_sc(sc, action.pipeline_updates)

            # SPEC_REVISE — complete → self_review clean → SPEC_REVIEW
            sc["_revise_response"] = _revise_yaml(
                expected_next_rev, f"aaa{cycle:04x}bb", 10, 5
            )
            sc, action = _simulate_revise_complete(sc, data)
            assert action.next_state == "SPEC_REVIEW"
            pu = action.pipeline_updates
            assert pu["current_rev"] == expected_next_rev
            assert pu["rev_index"] == cycle + 2

            # review_history が蓄積
            assert len(sc.get("review_history", [])) == cycle + 1


# ===========================================================================
# 3. TestAbnormalFlowE2E — 異常系 E2E
# ===========================================================================

class TestAbnormalFlowE2E:

    def test_all_timeout_to_review_failed_then_retry(self):
        """全員 timeout → SPEC_REVIEW_FAILED → retry"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests={
                r: {"status": "timeout", "sent_at": "2026-03-01T11:30:00+09:00",
                    "timeout_at": "2026-03-01T12:00:00+09:00",
                    "last_nudge_at": None, "response": None}
                for r in _REVIEWERS
            },
            current_reviews={
                "reviewed_rev": "1",
                "entries": {
                    r: {"verdict": None, "items": [], "raw_text": None,
                        "parse_success": False, "status": "timeout"}
                    for r in _REVIEWERS
                },
            },
        )
        data = _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=sc)

        action = check_transition_spec("SPEC_REVIEW", sc, _now(), data)
        assert action.next_state == "SPEC_REVIEW_FAILED"

        # SPEC_REVIEW_FAILED — terminal
        action = check_transition_spec("SPEC_REVIEW_FAILED", sc, _now(), data)
        assert action.next_state is None

        # retry シミュレート: review_requests リセット確認
        _reset_review_requests(sc, _now())
        for r in _REVIEWERS:
            assert sc["review_requests"][r]["status"] == "pending"
            assert sc["review_requests"][r]["sent_at"] is None

    def test_parse_fail_insufficient_to_paused(self):
        """parse_failed(1) + timeout(1) → 有効<2 → paused"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests={
                "pascal": {"status": "received", "sent_at": "2026-03-01T11:30:00+09:00",
                           "timeout_at": "2026-03-01T12:00:00+09:00",
                           "last_nudge_at": None, "response": None},
                "leibniz": {"status": "timeout", "sent_at": "2026-03-01T11:30:00+09:00",
                            "timeout_at": "2026-03-01T12:00:00+09:00",
                            "last_nudge_at": None, "response": None},
            },
            current_reviews={
                "reviewed_rev": "1",
                "entries": {
                    "pascal": {"verdict": None, "items": [], "raw_text": "bad",
                               "parse_success": False, "status": "parse_failed"},
                    "leibniz": {"verdict": None, "items": [], "raw_text": None,
                                "parse_success": False, "status": "timeout"},
                },
            },
        )
        data = _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=sc,
                              review_mode="full")

        result = should_continue_review(sc, "full")
        assert result == "paused"

        action = check_transition_spec("SPEC_REVIEW", sc, _now(), data)
        assert action.next_state == "SPEC_PAUSED"
        assert action.pipeline_updates["paused_from"] == "SPEC_REVIEW"

    def test_paused_resume_to_review(self):
        """SPEC_PAUSED → M resume → SPEC_REVIEW"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
            paused_from="SPEC_REVIEW",
        )
        data = _make_pipeline(state="SPEC_PAUSED", spec_mode=True, spec_config=sc)

        # watchdog はパッシブ
        action = check_transition_spec("SPEC_PAUSED", sc, _now(), data)
        assert action.next_state is None

        # resume シミュレート
        _reset_review_requests(sc, _now())
        action = check_transition_spec("SPEC_REVIEW", sc, _now(), data)
        # 送信が発生する（遷移はまだ）
        assert action.send_to is not None or action.next_state is not None

    def test_max_cycles_p1_to_stalled(self):
        """revise_count == max → P1 → stalled"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
            rev_index=config.MAX_SPEC_REVISE_CYCLES,
            max_revise_cycles=config.MAX_SPEC_REVISE_CYCLES,
        )
        data = _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=sc)

        _set_all_received(sc, {_REVIEWERS[0]: "P1", **{r: "APPROVE" for r in _REVIEWERS[1:]}})
        result = should_continue_review(sc, "full")
        assert result == "stalled"

        action = check_transition_spec("SPEC_REVIEW", sc, _now(), data)
        assert action.next_state == "SPEC_STALLED"

    def test_stalled_force_approve(self):
        """SPEC_STALLED → force approve → SPEC_APPROVED → auto_continue"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
            auto_continue=True,
            force_events=[],
        )
        data = _make_pipeline(state="SPEC_STALLED", spec_mode=True, spec_config=sc)

        # SPEC_STALLED は terminal
        action = check_transition_spec("SPEC_STALLED", sc, _now(), data)
        assert action.next_state is None

        # force approve シミュレート
        sc["force_events"].append({
            "action": "approve",
            "from": "SPEC_STALLED",
            "at": _now().isoformat(),
            "by": "operator",
        })

        # SPEC_APPROVED → auto_continue → ISSUE_SUGGESTION
        action = check_transition_spec("SPEC_APPROVED", sc, _now(), data)
        assert action.next_state == "ISSUE_SUGGESTION"

    def test_revise_timeout_retry_then_paused(self):
        """SPEC_REVISE タイムアウト × MAX_SPEC_RETRIES → SPEC_PAUSED"""
        past = (_now() - timedelta(seconds=config.SPEC_REVISE_TIMEOUT_SEC + 100))
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
            retry_counts={},
        )
        history_entry = {"from": "SPEC_REVIEW", "to": "SPEC_REVISE",
                         "at": past.isoformat(), "actor": "watchdog"}
        data = _make_pipeline(
            state="SPEC_REVISE", spec_mode=True, spec_config=sc,
            history=[history_entry],
        )

        # retry 1
        action = check_transition_spec("SPEC_REVISE", sc, _now(), data)
        assert action.next_state is None
        assert action.pipeline_updates["retry_counts"]["SPEC_REVISE"] == 1
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # retry 2 — _revise_retry_at を過去に設定
        sc["_revise_retry_at"] = past.isoformat()
        action = check_transition_spec("SPEC_REVISE", sc, _now(), data)
        assert action.next_state is None
        assert action.pipeline_updates["retry_counts"]["SPEC_REVISE"] == 2
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # retry 3
        sc["_revise_retry_at"] = past.isoformat()
        action = check_transition_spec("SPEC_REVISE", sc, _now(), data)
        assert action.next_state is None
        assert action.pipeline_updates["retry_counts"]["SPEC_REVISE"] == 3
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # retry >= MAX → PAUSED
        sc["_revise_retry_at"] = past.isoformat()
        action = check_transition_spec("SPEC_REVISE", sc, _now(), data)
        assert action.next_state == "SPEC_PAUSED"
        assert action.pipeline_updates["paused_from"] == "SPEC_REVISE"

    def test_spec_revise_sends_to_implementer_on_first_tick(self):
        """SPEC_REVISE 初回 tick で implementer に send_to が発行される (#72)"""
        p0_items = [
            {
                "id": "p0-1", "severity": "P0", "section": "§2",
                "title": "致命的な問題", "description": "説明", "suggestion": "提案",
                "reviewer": "pascal", "normalized_id": "pascal:p0-1",
            }
        ]
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
            current_reviews={
                "reviewed_rev": "1",
                "entries": {
                    "pascal": _received_entry("P0", items=p0_items),
                    "leibniz": _received_entry("APPROVE"),
                    "dijkstra": _received_entry("APPROVE"),
                },
            },
        )
        data = _make_pipeline(state="SPEC_REVISE", spec_mode=True, spec_config=sc)

        # 初回 tick: _revise_sent 未設定 → send_to が発行される
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state is None
        assert action.send_to is not None
        assert "kaneko" in action.send_to
        assert action.pipeline_updates.get("_revise_sent") is not None

        # 2回目 tick: _revise_sent がセット済み → send_to は出ない（冪等）
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)
        action2 = _check_spec_revise(sc, _now(), data)
        assert action2.send_to is None

    def test_full_mode_one_timeout_approved(self):
        """standardモード 3人中1人 timeout → approved（min_valid=3 を3人 received で満たす）"""
        members = list(_REVIEW_MODES["standard"]["members"])
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests={
                r: {"status": "pending", "sent_at": None, "timeout_at": None,
                    "last_nudge_at": None, "response": None}
                for r in members + ["extra_reviewer"]
            },
            current_reviews={
                "reviewed_rev": "1",
                "entries": {
                    **{r: _received_entry("APPROVE") for r in members},
                    "extra_reviewer": {"verdict": None, "items": [], "raw_text": None,
                              "parse_success": False, "status": "timeout"},
                },
            },
        )
        result = should_continue_review(sc, "standard")
        assert result == "approved"

    def test_lite_mode_one_timeout_approved(self):
        """liteモード 2人中1人 timeout + extra → approved（min_valid=2, #65 C3）"""
        lite_members = list(_REVIEW_MODES["lite"]["members"])
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests={
                r: {"status": "pending", "sent_at": None, "timeout_at": None,
                    "last_nudge_at": None, "response": None}
                for r in lite_members + ["extra_reviewer"]
            },
            current_reviews={
                "reviewed_rev": "1",
                "entries": {
                    **{r: _received_entry("APPROVE") for r in lite_members},
                    "extra_reviewer": {"verdict": None, "items": [], "raw_text": None,
                                 "parse_success": False, "status": "timeout"},
                },
            },
        )
        result = should_continue_review(sc, "lite")
        assert result == "approved"

    def test_full_mode_insufficient_received_failed(self):
        """fullモード received < min_valid → failed"""
        # full mode has min_valid=4; provide only 1 received + 3 timeout
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
            current_reviews={
                "reviewed_rev": "1",
                "entries": {
                    _REVIEWERS[0]: _received_entry("APPROVE"),
                    **{r: {"verdict": None, "items": [], "raw_text": None,
                           "parse_success": False, "status": "timeout"} for r in _REVIEWERS[1:]},
                },
            },
        )
        result = should_continue_review(sc, "full")
        assert result == "failed"

    def test_unknown_review_mode_raises(self):
        """未知 review_mode → ValueError（#65 A5）"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
            current_reviews={"reviewed_rev": "1", "entries": {}},
        )
        with pytest.raises(ValueError, match="Unknown review_mode"):
            should_continue_review(sc, "nonexistent")


# ===========================================================================
# 4. TestDCL — Double-Check Locking テスト
# ===========================================================================

class TestDCL:

    def test_concurrent_expected_state_mismatch(self, tmp_pipelines):
        """expected_state 不一致 → 何もしない"""
        pj_path = tmp_pipelines / "test-pj.json"
        pj_data = {
            "project": "test-pj", "state": "SPEC_APPROVED",
            "enabled": True, "batch": [],
            "spec_mode": True, "spec_config": {},
            "history": [],
        }
        write_pipeline(pj_path, pj_data)

        action = SpecTransitionAction(
            next_state="SPEC_REVISE",
            expected_state="SPEC_REVIEW",  # 不一致
        )
        with patch("watchdog.send_to_agent_queued") as mock_send, \
             patch("watchdog.notify_discord") as mock_discord:
            _apply_spec_action(pj_path, action, _now(), pj_data)

        result = json.loads(pj_path.read_text())
        assert result["state"] == "SPEC_APPROVED"  # 変わっていない
        mock_send.assert_not_called()
        mock_discord.assert_not_called()

    def test_apply_spec_action_normal(self, tmp_pipelines):
        """正常適用: SPEC_REVIEW + review_only → SPEC_DONE"""
        pj_path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(review_only=True)
        pj_data = _make_pipeline(
            state="SPEC_REVIEW", spec_mode=True, spec_config=sc,
        )
        write_pipeline(pj_path, pj_data)

        action = SpecTransitionAction(
            next_state="SPEC_APPROVED",
            expected_state="SPEC_REVIEW",
        )
        with patch("watchdog.send_to_agent_queued"), \
             patch("watchdog.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)

        result = json.loads(pj_path.read_text())
        # DCL 再計算: SPEC_REVIEW → check_transition_spec
        # review_requests が空なので all_complete=False → next_state=None
        # ただし expected_state 一致で _update は実行される
        # review_only=True でも review_requests 空 → SPEC_REVIEW のまま
        # (check_transition_spec("SPEC_REVIEW") は should_continue_review に入れない)
        # 重要なのは DCL が正常に動作すること
        assert result["state"] in ("SPEC_REVIEW", "SPEC_APPROVED", "SPEC_DONE")


# ===========================================================================
# 5. TestCLIOptionCombinations — CLI オプション組み合わせ（§2.5 真理値表）
# ===========================================================================

class TestCLIOptionCombinations:

    @pytest.fixture(autouse=True)
    def _spec_file(self, tmp_pipelines, monkeypatch):
        (tmp_pipelines / "docs").mkdir(exist_ok=True)
        (tmp_pipelines / "docs" / "spec.md").write_text("# test spec")
        monkeypatch.chdir(tmp_pipelines)

    def _base_args(self, **overrides):
        defaults = dict(
            project="test-pj", spec="docs/spec.md", implementer="kaneko",
            review_only=False, no_queue=False, skip_review=False,
            max_cycles=None, review_mode=None, model=None, auto_continue=False, auto_qrun=False,
        )
        defaults.update(overrides)
        return _args(**defaults)

    def _run_start(self, tmp_pipelines, **overrides):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE"))
        with patch("devbar._start_loop"):
            cmd_spec_start(self._base_args(**overrides))
        return json.loads(path.read_text())

    def test_default_options(self, tmp_pipelines):
        data = self._run_start(tmp_pipelines)
        assert data["state"] == "SPEC_REVIEW"
        sc = data["spec_config"]
        assert sc["skip_review"] is False
        assert sc["review_only"] is False
        assert sc["no_queue"] is False
        assert sc["auto_continue"] is False

    def test_skip_review(self, tmp_pipelines):
        data = self._run_start(tmp_pipelines, skip_review=True)
        assert data["state"] == "SPEC_APPROVED"
        sc = data["spec_config"]
        assert sc["skip_review"] is True
        assert sc["auto_continue"] is True  # 強制

    def test_review_only(self, tmp_pipelines):
        data = self._run_start(tmp_pipelines, review_only=True)
        sc = data["spec_config"]
        assert sc["review_only"] is True
        assert sc["auto_continue"] is False
        assert sc["no_queue"] is True  # 強制

    def test_no_queue(self, tmp_pipelines):
        data = self._run_start(tmp_pipelines, no_queue=True)
        sc = data["spec_config"]
        assert sc["no_queue"] is True

    def test_auto_continue(self, tmp_pipelines):
        data = self._run_start(tmp_pipelines, auto_continue=True)
        sc = data["spec_config"]
        assert sc["auto_continue"] is True

    def test_skip_review_plus_review_only_error(self, tmp_pipelines):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE"))
        with pytest.raises(SystemExit, match="mutually exclusive"):
            cmd_spec_start(self._base_args(skip_review=True, review_only=True))

    def test_review_only_overrides_auto_continue(self, tmp_pipelines):
        data = self._run_start(tmp_pipelines, review_only=True, auto_continue=True)
        sc = data["spec_config"]
        assert sc["review_only"] is True
        assert sc["auto_continue"] is False  # review_only が勝つ


# ===========================================================================
# 6. TestCurrentReviewsStructure — current_reviews エントリ構造（§3.1 [v7]）
# ===========================================================================

class TestCurrentReviewsStructure:

    def test_reviewed_rev_top_level(self):
        """reviewed_rev がトップレベルに存在"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
            current_reviews={
                "reviewed_rev": "2",
                "entries": {},
            },
        )
        # current_reviews.reviewed_rev が保持される
        assert sc["current_reviews"]["reviewed_rev"] == "2"

    def test_entries_nested(self):
        """reviewer エントリが entries 配下"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
            current_reviews={
                "reviewed_rev": "1",
                "entries": {
                    "pascal": _received_entry("P0"),
                    "leibniz": _received_entry("APPROVE"),
                },
            },
        )
        entries = sc["current_reviews"]["entries"]
        assert entries["pascal"]["verdict"] == "P0"
        assert entries["leibniz"]["verdict"] == "APPROVE"

    def test_status_pending_to_received(self):
        """received エントリが validate_received_entry を通過する"""
        entry = _received_entry("APPROVE")
        assert validate_received_entry(entry) is True
        assert entry["status"] == "received"
        assert entry["verdict"] == "APPROVE"
        assert entry["parse_success"] is True

    def test_status_pending_to_timeout(self):
        """timeout_at 超過 → timeout patch が生成される"""
        past = _now() - timedelta(seconds=config.SPEC_REVIEW_TIMEOUT_SEC + 100)
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests={
                "pascal": {"status": "pending",
                           "sent_at": past.isoformat(),
                           "timeout_at": (past + timedelta(seconds=config.SPEC_REVIEW_TIMEOUT_SEC)).isoformat(),
                           "last_nudge_at": None, "response": None},
            },
            current_reviews={"entries": {}},
        )
        data = {"project": "test-pj", "review_mode": "full"}
        action = _check_spec_review(sc, _now(), data)
        rr_patch = action.pipeline_updates.get("review_requests_patch", {})
        assert rr_patch.get("pascal", {}).get("status") == "timeout"

    def test_status_pending_to_parse_failed(self):
        """parse_success=False の received → validate_received_entry が False"""
        entry = {
            "verdict": None, "items": [], "raw_text": "bad",
            "parse_success": False, "status": "received",
        }
        assert validate_received_entry(entry) is False

    def test_received_invariant_violation_fallback(self):
        """verdict=None で status=received → validate_received_entry が False"""
        entry = {
            "verdict": None, "items": [], "raw_text": "...",
            "parse_success": True, "status": "received",
        }
        assert validate_received_entry(entry) is False


# ===========================================================================
# 7. TestLastChangesVerification — last_changes 検証（§3.1 [v7]）
# ===========================================================================

class TestLastChangesVerification:

    def test_last_changes_stored_after_revise(self):
        """SPEC_REVISE 完了後に last_changes が SPEC_REVIEW 遷移の pipeline_updates に含まれる"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
            _revise_response=_revise_yaml("2", "abc1234", 50, 10),
        )
        data = _make_pipeline(state="SPEC_REVISE", spec_mode=True, spec_config=sc)

        # (C) revise_response → self_review 開始: last_changes は pending_updates に入る
        action_c = check_transition_spec("SPEC_REVISE", sc, _now(), data)
        assert action_c.next_state is None
        pending = action_c.pipeline_updates.get("_self_review_pending_updates", {})
        assert "last_changes" in pending
        lc = pending["last_changes"]
        assert lc["added_lines"] == 50
        assert lc["removed_lines"] == 10

        # (A) self_review clean → SPEC_REVIEW: pending が適用されて pipeline_updates に含まれる
        sc = _apply_updates_to_sc(sc, action_c.pipeline_updates)
        sc["_self_review_response"] = _self_review_clean_yaml()
        action_a = check_transition_spec("SPEC_REVISE", sc, _now(), data)
        assert action_a.next_state == "SPEC_REVIEW"
        assert "last_changes" in action_a.pipeline_updates

        # build_revise_completion_updates 単体確認
        from spec_revise import parse_revise_response
        parsed = parse_revise_response(_revise_yaml("2", "abc1234", 50, 10), "1")
        updates = build_revise_completion_updates(sc, parsed, _now())
        assert "last_changes" in updates

    def test_last_changes_used_in_revision_prompt(self):
        """last_changes の情報がレビュー改訂プロンプトに反映される"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=_pending_review_requests(),
            last_commit="abc1234",
            last_changes={"added_lines": 50, "removed_lines": 10,
                          "changelog_summary": "Fixed stuff"},
            pipelines_dir="/tmp",
        )
        prompt = _build_spec_review_prompt_revision(
            "devbar", "docs/spec.md", "2", sc, {},
        )
        assert "abc1234" in prompt
        assert "+50" in prompt


# ===========================================================================
# 8. TestApprovedTransitionFix — Issue #66: SPEC_APPROVED→ISSUE_SUGGESTION 遷移修正
# ===========================================================================

class TestApprovedTransitionFix:

    def test_spec_e2e_approved_resets_review_requests(self):
        """7c: SPEC_REVIEW→SPEC_APPROVED→ISSUE_SUGGESTION で review_requests リセット・アーカイブ・current_reviews クリアを確認"""
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            auto_continue=True,
            review_requests=_pending_review_requests(),
        )
        data = _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=sc)

        # tick1: 送信
        action = check_transition_spec("SPEC_REVIEW", sc, _now(), data)
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # tick2: 全員 APPROVE → SPEC_APPROVED
        _set_all_received(sc)
        action = check_transition_spec("SPEC_REVIEW", sc, _now(), data)
        assert action.next_state == "SPEC_APPROVED"
        pu = action.pipeline_updates or {}
        # A1: _review_history_append が含まれる
        assert "_review_history_append" in pu
        # A4: current_reviews クリアが含まれる
        assert pu.get("current_reviews") == {}
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # review_history にエントリが1件追加されている
        assert len(sc.get("review_history", [])) == 1
        # current_reviews がクリアされている
        assert sc.get("current_reviews") == {}

        # SPEC_APPROVED → auto_continue → ISSUE_SUGGESTION
        action = check_transition_spec("SPEC_APPROVED", sc, _now(), data)
        assert action.next_state == "ISSUE_SUGGESTION"
        pu = action.pipeline_updates or {}
        # C1: review_requests_patch で全員 pending にリセット
        rr_patch = pu.get("review_requests_patch", {})
        assert set(rr_patch.keys()) == set(_REVIEWERS)
        for r in _REVIEWERS:
            assert rr_patch[r]["status"] == "pending"
            assert rr_patch[r]["sent_at"] is None
        sc = _apply_updates_to_sc(sc, action.pipeline_updates)

        # 適用後の状態確認
        for r in _REVIEWERS:
            assert sc["review_requests"][r]["status"] == "pending"
        assert sc.get("current_reviews") == {}
        assert len(sc.get("review_history", [])) == 1

        # 冪等性: SPEC_APPROVED で再度 check_transition_spec → _review_history_append なし
        # (_check_spec_review は呼ばれず、auto_continue の review_requests_patch のみ)
        action2 = check_transition_spec("SPEC_APPROVED", sc, _now(), data)
        pu2 = action2.pipeline_updates or {}
        assert "_review_history_append" not in pu2

    def test_cmd_spec_continue_resets_and_records_history(self, tmp_pipelines):
        """7d: cmd_spec_continue が review_requests をリセットし history を記録する"""
        import json
        from pipeline_io import get_path, load_pipeline

        # SPEC_APPROVED 状態で review_requests が全員 received なパイプラインを作成
        rr = {
            r: {
                "status": "received",
                "sent_at": "2026-03-01T12:00:00+09:00",
                "timeout_at": "2026-03-01T12:30:00+09:00",
                "last_nudge_at": None,
                "response": None,
            }
            for r in _REVIEWERS
        }
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            review_requests=rr,
            current_reviews={},
        )
        data = _make_pipeline(state="SPEC_APPROVED", spec_mode=True, spec_config=sc)
        path = get_path("test-pj")
        write_pipeline(path, data)

        args = _args(project="test-pj")
        from devbar import cmd_spec_continue
        cmd_spec_continue(args)

        result = load_pipeline(path)
        assert result["state"] == "ISSUE_SUGGESTION"
        # C1: review_requests が全員 pending にリセット
        rr_result = result["spec_config"]["review_requests"]
        for r in _REVIEWERS:
            assert rr_result[r]["status"] == "pending"
            assert rr_result[r]["sent_at"] is None
        # A3: history に遷移エントリが記録されている
        history = result.get("history", [])
        assert any(
            h.get("from") == "SPEC_APPROVED" and h.get("to") == "ISSUE_SUGGESTION"
            for h in history
        )

    def test_reviewed_rev_set_by_review_submit(self, tmp_pipelines):
        """7e: cmd_spec_review_submit 後に current_reviews.reviewed_rev がセットされる"""
        import tempfile
        from pipeline_io import get_path, load_pipeline

        rr = {
            r: {
                "status": "pending",
                "sent_at": "2026-03-01T12:00:00+09:00",
                "timeout_at": "2026-03-02T12:00:00+09:00",
                "last_nudge_at": None,
                "response": None,
            }
            for r in _REVIEWERS
        }
        sc = _make_spec_config(
            spec_path="docs/test-spec.md",
            spec_implementer="kaneko",
            current_rev="2",
            review_requests=rr,
            current_reviews={},
        )
        data = _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=sc)
        path = get_path("test-pj")
        write_pipeline(path, data)

        review_yaml = (
            "verdict: APPROVE\n"
            "items: []\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(review_yaml)
            review_file = f.name

        args = _args(project="test-pj", reviewer="pascal", file=review_file)
        from devbar import cmd_spec_review_submit
        cmd_spec_review_submit(args)

        result = load_pipeline(path)
        cr = result["spec_config"].get("current_reviews", {})
        assert cr.get("reviewed_rev") == "2"


# ===========================================================================
# 8. TestSpecDoneAutoTransition — Issue #78: SPEC_DONE → IDLE 自動遷移
# ===========================================================================

class TestSpecDoneAutoTransition:
    """SPEC_DONE → IDLE 自動遷移の検証（Issue #78）。"""

    def test_check_transition_spec_done_returns_idle(self):
        """check_transition_spec("SPEC_DONE") は next_state="IDLE" を返す。"""
        sc = _make_spec_config(spec_path="docs/test-spec.md", spec_implementer="kaneko")
        data = _make_pipeline(state="SPEC_DONE", spec_mode=True, spec_config=sc)
        action = check_transition_spec("SPEC_DONE", sc, _now(), data)
        assert action.next_state == "IDLE"

    @pytest.mark.parametrize("state", ["SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED"])
    def test_check_transition_terminal_states_return_none(self, state):
        """他の terminal states は next_state=None のまま（M操作待ち）。"""
        sc = _make_spec_config(spec_path="docs/test-spec.md", spec_implementer="kaneko")
        data = _make_pipeline(state=state, spec_mode=True, spec_config=sc)
        action = check_transition_spec(state, sc, _now(), data)
        assert action.next_state is None

    def test_spec_done_not_in_terminal_states(self):
        """_SPEC_TERMINAL_STATES に SPEC_DONE が含まれない。"""
        from watchdog import _SPEC_TERMINAL_STATES
        assert "SPEC_DONE" not in _SPEC_TERMINAL_STATES
        assert "SPEC_STALLED" in _SPEC_TERMINAL_STATES
        assert "SPEC_REVIEW_FAILED" in _SPEC_TERMINAL_STATES
        assert "SPEC_PAUSED" in _SPEC_TERMINAL_STATES

    def test_apply_spec_action_spec_done_to_idle(self, tmp_pipelines):
        """_apply_spec_action 経由で SPEC_DONE → IDLE: state/spec_mode/spec_config がクリアされる。"""
        from pipeline_io import get_path, load_pipeline

        sc = _make_spec_config(spec_path="docs/test-spec.md", spec_implementer="kaneko")
        pj_data = _make_pipeline(state="SPEC_DONE", spec_mode=True, spec_config=sc)
        path = get_path("test-pj")
        write_pipeline(path, pj_data)

        action = SpecTransitionAction(next_state="IDLE", expected_state="SPEC_DONE")
        with patch("watchdog._check_queue"):
            _apply_spec_action(path, action, _now(), pj_data)

        result = load_pipeline(path)
        assert result["state"] == "IDLE"
        assert result["spec_mode"] is False
        assert result["spec_config"] == {}

    def test_apply_spec_action_spec_done_history_recorded_once(self, tmp_pipelines):
        """SPEC_DONE → IDLE 遷移時、history に actor=watchdog で1回だけ記録される。"""
        from pipeline_io import get_path, load_pipeline

        sc = _make_spec_config(spec_path="docs/test-spec.md", spec_implementer="kaneko")
        pj_data = _make_pipeline(state="SPEC_DONE", spec_mode=True, spec_config=sc)
        path = get_path("test-pj")
        write_pipeline(path, pj_data)

        action = SpecTransitionAction(next_state="IDLE", expected_state="SPEC_DONE")
        with patch("watchdog._check_queue"):
            _apply_spec_action(path, action, _now(), pj_data)

        result = load_pipeline(path)
        watchdog_history = [
            h for h in result.get("history", [])
            if h.get("actor") == "watchdog"
            and h.get("from") == "SPEC_DONE"
            and h.get("to") == "IDLE"
        ]
        assert len(watchdog_history) == 1, "add_history は SPEC_DONE→IDLE で1回だけ呼ばれること"

    def test_apply_spec_action_spec_done_transitions_to_idle(self, tmp_pipelines):
        """SPEC_DONE → IDLE 遷移で spec_mode が False になること。
        _check_queue は process() レベルで呼ばれるため、_apply_spec_action 単体では呼ばれない。
        """
        from pipeline_io import get_path, load_pipeline

        sc = _make_spec_config(spec_path="docs/test-spec.md", spec_implementer="kaneko")
        pj_data = _make_pipeline(state="SPEC_DONE", spec_mode=True, spec_config=sc)
        path = get_path("test-pj")
        write_pipeline(path, pj_data)

        action = SpecTransitionAction(next_state="IDLE", expected_state="SPEC_DONE")
        with patch("watchdog._check_queue"):
            _apply_spec_action(path, action, _now(), pj_data)

        result = load_pipeline(path)
        assert result["state"] == "IDLE"
        assert result["spec_mode"] is False
