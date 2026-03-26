"""Tests for grace_skipped_reviewers in TransitionAction (Issue #233)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from engine.fsm import build_review_config, check_transition


def _mode_config(
    members: list[str], min_reviews: int, grace_period_sec: int
) -> dict:
    return {
        "members": members,
        "min_reviews": min_reviews,
        "grace_period_sec": grace_period_sec,
        "n_pass": {},
    }


def _make_batch(
    review_key: str, reviewed_by: list[str], issue_count: int = 1
) -> list[dict]:
    """Create a batch with given reviewers having APPROVE verdicts."""
    batch = []
    for i in range(issue_count):
        reviews = {
            r: {"verdict": "APPROVE", "at": f"2026-03-26T00:0{idx}:00+09:00"}
            for idx, r in enumerate(reviewed_by)
        }
        batch.append({"issue": i + 1, review_key: reviews})
    return batch


class TestGraceSkippedReviewers:
    """Test A: grace period expired sets grace_skipped_reviewers."""

    def test_grace_expired_sets_skipped_reviewers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        members = ["r1", "r2", "r3"]
        mode = _mode_config(members, min_reviews=2, grace_period_sec=60)
        monkeypatch.setattr("engine.fsm.REVIEW_MODES", {"standard": mode})
        monkeypatch.setattr("engine.fsm.BLOCK_TIMERS", {})

        batch = _make_batch("design_reviews", reviewed_by=["r1", "r2"])
        data = {
            "review_mode": "standard",
            "review_config": build_review_config(mode),
            # met_at far in the past → grace expired
            "design_min_reviews_met_at": "2020-01-01T00:00:00+09:00",
        }

        with patch("engine.fsm.log"):
            action = check_transition("DESIGN_REVIEW", batch, data)

        assert action.new_state is not None
        assert action.grace_skipped_reviewers == ["r3"]

    """Test B: all reviewers done → grace_skipped_reviewers is None."""

    def test_all_done_no_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        members = ["r1", "r2", "r3"]
        mode = _mode_config(members, min_reviews=2, grace_period_sec=60)
        monkeypatch.setattr("engine.fsm.REVIEW_MODES", {"standard": mode})
        monkeypatch.setattr("engine.fsm.BLOCK_TIMERS", {})

        batch = _make_batch("design_reviews", reviewed_by=["r1", "r2", "r3"])
        data = {
            "review_mode": "standard",
            "review_config": build_review_config(mode),
        }

        with patch("engine.fsm.log"):
            action = check_transition("DESIGN_REVIEW", batch, data)

        assert action.new_state is not None
        assert action.grace_skipped_reviewers is None

    """Test C: CODE_REVIEW also works."""

    def test_code_review_grace_expired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        members = ["r1", "r2", "r3"]
        mode = _mode_config(members, min_reviews=2, grace_period_sec=60)
        monkeypatch.setattr("engine.fsm.REVIEW_MODES", {"standard": mode})
        monkeypatch.setattr("engine.fsm.BLOCK_TIMERS", {})

        batch = _make_batch("code_reviews", reviewed_by=["r1", "r2"])
        data = {
            "review_mode": "standard",
            "review_config": build_review_config(mode),
            "code_min_reviews_met_at": "2020-01-01T00:00:00+09:00",
        }

        with patch("engine.fsm.log"):
            action = check_transition("CODE_REVIEW", batch, data)

        assert action.new_state is not None
        assert action.grace_skipped_reviewers == ["r3"]

    """Test D: excluded reviewers not in grace_skipped_reviewers."""

    def test_excluded_not_in_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        members = ["r1", "r2", "r3"]
        mode = _mode_config(members, min_reviews=1, grace_period_sec=60)
        monkeypatch.setattr("engine.fsm.REVIEW_MODES", {"standard": mode})
        monkeypatch.setattr("engine.fsm.BLOCK_TIMERS", {})

        batch = _make_batch("design_reviews", reviewed_by=["r1"])
        data = {
            "review_mode": "standard",
            "review_config": build_review_config(mode),
            "design_min_reviews_met_at": "2020-01-01T00:00:00+09:00",
            "excluded_reviewers": ["r2"],
        }

        with patch("engine.fsm.log"):
            action = check_transition("DESIGN_REVIEW", batch, data)

        assert action.new_state is not None
        # r2 is excluded, only r3 should be skipped
        assert action.grace_skipped_reviewers == ["r3"]

    """Test E: grace period not yet expired → no transition."""

    def test_grace_waiting_no_transition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        members = ["r1", "r2", "r3"]
        mode = _mode_config(members, min_reviews=2, grace_period_sec=60)
        monkeypatch.setattr("engine.fsm.REVIEW_MODES", {"standard": mode})
        monkeypatch.setattr("engine.fsm.BLOCK_TIMERS", {})

        batch = _make_batch("design_reviews", reviewed_by=["r1", "r2"])

        from datetime import datetime, timedelta

        from config import LOCAL_TZ

        # met_at 10 seconds ago (< 60 sec grace)
        met_at = (datetime.now(LOCAL_TZ) - timedelta(seconds=10)).isoformat()
        data = {
            "review_mode": "standard",
            "review_config": build_review_config(mode),
            "design_min_reviews_met_at": met_at,
        }

        with patch("engine.fsm.log"):
            action = check_transition("DESIGN_REVIEW", batch, data)

        assert action.new_state is None
        assert action.save_grace_met_at == "design_min_reviews_met_at"

    """Test F: grace_sec=0 → transition but no grace_skipped_reviewers."""

    def test_no_grace_period_no_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        members = ["r1", "r2", "r3"]
        mode = _mode_config(members, min_reviews=2, grace_period_sec=0)
        monkeypatch.setattr("engine.fsm.REVIEW_MODES", {"standard": mode})
        monkeypatch.setattr("engine.fsm.BLOCK_TIMERS", {})

        batch = _make_batch("design_reviews", reviewed_by=["r1", "r2"])
        data = {
            "review_mode": "standard",
            "review_config": build_review_config(mode),
        }

        with patch("engine.fsm.log"):
            action = check_transition("DESIGN_REVIEW", batch, data)

        assert action.new_state is not None
        assert action.grace_skipped_reviewers is None
