"""tests/test_fsm_initialize.py — check_transition("INITIALIZE", ...) のユニットテスト (Issue #125)"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.fsm import check_transition


class TestCheckTransitionInitialize:

    _BATCH = [
        {"issue": 1, "title": "T", "commit": None, "cc_session_id": None,
         "design_reviews": {}, "code_reviews": {},
         "added_at": "2025-01-01T00:00:00+09:00"},
    ]

    def test_new_state_is_design_plan(self):
        """INITIALIZE → DESIGN_PLAN"""
        data = {"project": "pj", "comment": ""}
        action = check_transition("INITIALIZE", list(self._BATCH), data)
        assert action.new_state == "DESIGN_PLAN"

    def test_reset_reviewers_true(self):
        """reset_reviewers が True であること"""
        data = {"project": "pj", "comment": ""}
        action = check_transition("INITIALIZE", list(self._BATCH), data)
        assert action.reset_reviewers is True

    def test_impl_msg_not_none(self):
        """impl_msg が None でないこと（DESIGN_PLAN の設計確認指示メッセージ）"""
        data = {"project": "pj", "comment": ""}
        action = check_transition("INITIALIZE", list(self._BATCH), data)
        assert action.impl_msg is not None

    def test_empty_batch(self):
        """空バッチでも DESIGN_PLAN に遷移する"""
        data = {"project": "pj", "comment": ""}
        action = check_transition("INITIALIZE", [], data)
        assert action.new_state == "DESIGN_PLAN"

    def test_no_data(self):
        """data=None でも DESIGN_PLAN に遷移する"""
        action = check_transition("INITIALIZE", list(self._BATCH), None)
        assert action.new_state == "DESIGN_PLAN"
        assert action.reset_reviewers is True
