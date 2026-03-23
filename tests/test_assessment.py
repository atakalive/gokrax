"""tests/test_assessment.py — ASSESSMENT 状態のスケルトンテスト (Issue #168)"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_batch(n=1, **kwargs):
    items = []
    for i in range(1, n + 1):
        item = {
            "issue": i, "title": f"Issue {i}", "commit": None,
            "cc_session_id": None,
            "design_reviews": {}, "code_reviews": {},
            "added_at": "2025-01-01T00:00:00+09:00",
        }
        item.update(kwargs)
        items.append(item)
    return items


class TestAssessmentTransitions:

    def test_design_approved_transitions_to_assessment(self):
        from engine.fsm import check_transition
        action = check_transition("DESIGN_APPROVED", _make_batch())
        assert action.new_state == "ASSESSMENT"
        assert action.run_cc is False
        assert action.reset_reviewers is False

    def test_assessment_transitions_to_implementation(self):
        from engine.fsm import check_transition
        data = {"assessment": {"complex_level": 3}}
        action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.reset_reviewers is True

    def test_skip_assess_skips_to_implementation(self):
        from engine.fsm import check_transition
        data = {"skip_assess": True}
        action = check_transition("DESIGN_APPROVED", _make_batch(), data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.reset_reviewers is True

    def test_skip_assess_false_explicit(self):
        from engine.fsm import check_transition
        data = {"skip_assess": False}
        action = check_transition("DESIGN_APPROVED", _make_batch(), data)
        assert action.new_state == "ASSESSMENT"

    def test_assessment_ignores_skip_assess(self):
        from engine.fsm import check_transition
        data = {"skip_assess": True, "assessment": {"complex_level": 3}}
        action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IMPLEMENTATION"


class TestAssessmentConfig:

    def test_assessment_in_valid_transitions(self):
        from config.states import VALID_TRANSITIONS
        assert "ASSESSMENT" in VALID_TRANSITIONS["DESIGN_APPROVED"]
        assert "IMPLEMENTATION" in VALID_TRANSITIONS["DESIGN_APPROVED"]
        assert VALID_TRANSITIONS["ASSESSMENT"] == ["IMPLEMENTATION"]

    def test_assessment_in_state_phase_map(self):
        from config.states import STATE_PHASE_MAP
        assert STATE_PHASE_MAP["ASSESSMENT"] == "design"

    def test_assessment_in_block_timers(self):
        from config.states import BLOCK_TIMERS
        assert BLOCK_TIMERS["ASSESSMENT"] == 1200

    def test_assessment_state_enum_exists(self):
        from config.states import State
        assert State.ASSESSMENT.value == "ASSESSMENT"


class TestParseQueueLineSkipAssess:

    def test_parse_queue_line_skip_assess(self):
        from task_queue import parse_queue_line
        result = parse_queue_line("gokrax 1 skip-assess")
        assert result["skip_assess"] is True

    def test_parse_queue_line_no_skip_assess(self):
        from task_queue import parse_queue_line
        result = parse_queue_line("gokrax 1 no-skip-assess")
        assert result["skip_assess"] is False
