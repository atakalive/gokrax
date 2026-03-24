"""Tests for engine/fsm.py — get_min_reviews helper and integration."""

from datetime import datetime

from config import LOCAL_TZ
from engine.fsm import get_min_reviews, check_transition


# ---------------------------------------------------------------------------
# Unit tests: get_min_reviews
# ---------------------------------------------------------------------------

def test_get_min_reviews_explicit():
    """min_reviews が明示されている場合、その値を返す。"""
    assert get_min_reviews({"members": ["a", "b"], "min_reviews": 1}) == 1


def test_get_min_reviews_missing():
    """min_reviews が省略されている場合、len(members) を返す。"""
    assert get_min_reviews({"members": ["a", "b"]}) == 2


def test_get_min_reviews_none():
    """min_reviews が None の場合、len(members) にフォールバックする。"""
    assert get_min_reviews({"members": ["a", "b"], "min_reviews": None}) == 2


def test_get_min_reviews_no_members():
    """members も未定義の場合、0 を返す（クラッシュしない）。"""
    assert get_min_reviews({}) == 0


# ---------------------------------------------------------------------------
# Integration tests: no_minrev mode transition
# ---------------------------------------------------------------------------

def test_no_minrev_mode_all_reviews_transitions():
    """min_reviews 省略モードで全メンバーのレビューが揃ったときに遷移する。"""
    batch = [
        {
            "issue": 1,
            "design_reviews": {
                "reviewer1": {"verdict": "APPROVE", "at": "2025-01-01T00:00:00+09:00"},
                "reviewer3": {"verdict": "APPROVE", "at": "2025-01-01T00:00:00+09:00"},
            },
        },
    ]
    data = {
        "project": "test-pj",
        "review_mode": "no_minrev",
        "history": [{"to": "DESIGN_REVIEW", "at": "2025-01-01T00:00:00+09:00"}],
    }
    action = check_transition("DESIGN_REVIEW", batch, data)
    assert action.new_state == "DESIGN_APPROVED"


def test_no_minrev_mode_partial_reviews_no_transition():
    """min_reviews 省略モードで全メンバー未達のときに遷移しない。"""
    now = datetime.now(LOCAL_TZ).isoformat()
    batch = [
        {
            "issue": 1,
            "design_reviews": {
                "reviewer1": {"verdict": "APPROVE", "at": now},
            },
        },
    ]
    data = {
        "project": "test-pj",
        "review_mode": "no_minrev",
        "history": [{"to": "DESIGN_REVIEW", "at": now}],
    }
    action = check_transition("DESIGN_REVIEW", batch, data)
    assert action.new_state is None
