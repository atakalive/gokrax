"""Tests for engine/fsm.py — get_min_reviews helper and integration."""

from datetime import datetime, timedelta
from unittest.mock import patch

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


# ---------------------------------------------------------------------------
# Issue #206: no_cc option tests
# ---------------------------------------------------------------------------

def test_check_transition_implementation_no_cc_waits():
    """IMPLEMENTATION + no_cc=True + commit なし → CC を起動しない。"""
    batch = [{"issue": 1}]
    data = {
        "project": "test-pj",
        "no_cc": True,
        "history": [{"to": "IMPLEMENTATION", "at": datetime.now(LOCAL_TZ).isoformat()}],
    }
    action = check_transition("IMPLEMENTATION", batch, data)
    assert action.run_cc is False
    assert action.new_state is None


def test_check_transition_implementation_no_cc_no_nudge():
    """IMPLEMENTATION + no_cc=True + 十分な経過時間 → BLOCKED にならない。"""
    old_time = (datetime.now(LOCAL_TZ) - timedelta(hours=4)).isoformat()
    batch = [{"issue": 1}]
    data = {
        "project": "test-pj",
        "no_cc": True,
        "history": [{"to": "IMPLEMENTATION", "at": old_time}],
    }
    action = check_transition("IMPLEMENTATION", batch, data)
    assert action.run_cc is False
    assert action.new_state is None


def test_check_transition_implementation_no_cc_complete():
    """IMPLEMENTATION + no_cc=True + 全 commit あり → 通常の完了遷移。"""
    batch = [{"issue": 1, "commit": "abc123"}]
    data = {
        "project": "test-pj",
        "no_cc": True,
        "history": [{"to": "IMPLEMENTATION", "at": datetime.now(LOCAL_TZ).isoformat()}],
    }
    action = check_transition("IMPLEMENTATION", batch, data)
    # no_cc は完了判定に影響しない
    assert action.new_state in ("CODE_TEST", "CODE_REVIEW")


def test_check_transition_implementation_default_runs_cc():
    """IMPLEMENTATION + no_cc 未設定 + CC 未実行 → run_cc is True（従来動作の回帰テスト）。"""
    batch = [{"issue": 1}]
    data = {
        "project": "test-pj",
        "history": [{"to": "IMPLEMENTATION", "at": datetime.now(LOCAL_TZ).isoformat()}],
    }
    with patch("engine.fsm._is_cc_running", return_value=False):
        action = check_transition("IMPLEMENTATION", batch, data)
    assert action.run_cc is True


def test_check_transition_design_approved_no_cc():
    """DESIGN_APPROVED + skip_assess=True + no_cc=True → run_cc is False。"""
    batch = [{"issue": 1}]
    data = {
        "project": "test-pj",
        "skip_assess": True,
        "no_cc": True,
    }
    action = check_transition("DESIGN_APPROVED", batch, data)
    assert action.new_state == "IMPLEMENTATION"
    assert action.run_cc is False


def test_check_transition_assessment_no_cc():
    """ASSESSMENT + 全 assessed + no_cc=True → run_cc is False。"""
    batch = [{"issue": 1, "assessment": {"domain_risk": "none"}}]
    data = {
        "project": "test-pj",
        "no_cc": True,
        "history": [{"to": "ASSESSMENT", "at": datetime.now(LOCAL_TZ).isoformat()}],
    }
    action = check_transition("ASSESSMENT", batch, data)
    assert action.new_state == "IMPLEMENTATION"
    assert action.run_cc is False


def test_check_transition_design_approved_no_cc_skip_design():
    """DESIGN_APPROVED + skip_assess=True + no_cc=True + skip_design=True → 共存しても正常動作。"""
    batch = [{"issue": 1}]
    data = {
        "project": "test-pj",
        "skip_assess": True,
        "no_cc": True,
        "skip_design": True,
    }
    action = check_transition("DESIGN_APPROVED", batch, data)
    assert action.new_state == "IMPLEMENTATION"
    assert action.run_cc is False


# ---------------------------------------------------------------------------
# Issue #381: phase-tagged min_reviews_override + intersection effective_count
# ---------------------------------------------------------------------------


def _make_code_review_data(**overrides):
    """CODE_REVIEW data dict with distinct code/design phases (Issue #381)."""
    now = datetime.now(LOCAL_TZ).isoformat()
    data = {
        "project": "test-pj",
        "review_mode": "standard",
        "review_config": {
            "code": {
                "members": ["reviewer1", "reviewer2", "reviewer3"],
                "min_reviews": 3,
                "n_pass": {},
                "grace_period_sec": 0,
            },
            "design": {
                "members": ["reviewer4", "reviewer5"],
                "min_reviews": 2,
                "n_pass": {},
                "grace_period_sec": 0,
            },
        },
        "history": [{"to": "CODE_REVIEW", "at": now}],
    }
    data.update(overrides)
    return data


def test_381_cross_phase_excluded_does_not_deflate_effective_count():
    """Defect C: a design-only excluded reviewer must not deflate the code effective_count."""
    batch = [
        {
            "issue": 1,
            "code_reviews": {
                "reviewer1": {"verdict": "APPROVE", "at": "2025-01-01T00:00:00+09:00"},
                "reviewer2": {"verdict": "APPROVE", "at": "2025-01-01T00:00:00+09:00"},
            },
        },
    ]
    data = _make_code_review_data(excluded_reviewers=["reviewer4"])
    # grace must be > 0 so partial-but-min-met starts a grace wait (no immediate transition)
    data["review_config"]["code"]["min_reviews"] = 2
    data["review_config"]["code"]["grace_period_sec"] = 300
    action = check_transition("CODE_REVIEW", batch, data)
    # new effective_count=3 → count(2) < effective → grace starts, no transition
    assert action.new_state is None
    assert action.save_grace_met_at == "code_min_reviews_met_at"


def test_381_phase_matched_override_is_honored():
    """Defect B: a code-tagged override lowers min_rev below the capped base."""
    batch = [
        {
            "issue": 1,
            "code_reviews": {
                "reviewer1": {"verdict": "APPROVE", "at": "2025-01-01T00:00:00+09:00"},
                "reviewer2": {"verdict": "APPROVE", "at": "2025-01-01T00:00:00+09:00"},
            },
        },
    ]
    data = _make_code_review_data(
        min_reviews_override=2, min_reviews_override_phase="code"
    )
    action = check_transition("CODE_REVIEW", batch, data)
    # effective_count=3, base min_rev=min(3,3)=3; override lowers to 2 → count(2) >= 2
    assert action.new_state == "CODE_APPROVED"


def test_381_cross_phase_override_is_ignored():
    """Defect B: a design-tagged override is ignored during CODE_REVIEW."""
    now = datetime.now(LOCAL_TZ).isoformat()
    batch = [
        {
            "issue": 1,
            "code_reviews": {
                "reviewer1": {"verdict": "APPROVE", "at": now},
            },
        },
    ]
    data = _make_code_review_data(
        min_reviews_override=1, min_reviews_override_phase="design"
    )
    action = check_transition("CODE_REVIEW", batch, data)
    # override_phase "design" != "code" → ignored → min_rev=3 → count(1) < 3 → None
    assert action.new_state is None


def test_381_effective_count_zero_no_auto_approve():
    """effective_count == 0 → override not consulted → no auto-approve."""
    batch = [{"issue": 1, "code_reviews": {}}]
    data = _make_code_review_data(
        excluded_reviewers=["reviewer1", "reviewer2", "reviewer3"],
        min_reviews_override=0,
        min_reviews_override_phase="code",
    )
    action = check_transition("CODE_REVIEW", batch, data)
    # effective_count == 0 branch → min_rev=3 → count(0) < 3 → None
    assert action.new_state is None
