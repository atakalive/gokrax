"""tests/test_recover_notifications.py — Issue #224: fresh state recovery tests"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.fsm import _recover_pending_notifications


class TestRecoverReviewFreshExcluded:
    """Test A: review リカバリが excluded_reviewers を fresh state から取得する。"""

    def test_excluded_from_fresh_data(self) -> None:
        pending = {
            "review": {
                "new_state": "DESIGN_REVIEW",
                "batch": [{"issue": 1, "title": "t"}],
                "gitlab": "ns/proj",
                "repo_path": "/tmp/repo",
                "review_mode": "short_context",
                "base_commit": "abc123",
            },
        }
        fresh_pipeline = {
            "excluded_reviewers": ["reviewer_fresh"],
            "comment": "fresh comment",
        }

        with (
            patch("engine.fsm.load_pipeline", return_value=fresh_pipeline) as mock_load,
            patch("engine.fsm.get_path", return_value=Path("/mock/path.json")) as mock_get_path,
            patch("engine.fsm.notify_reviewers") as mock_notify,
            patch("engine.fsm.clear_pending_notification") as mock_clear,
        ):
            _recover_pending_notifications("proj", pending)

            # get_path called with project name
            mock_get_path.assert_called_once_with("proj")
            # load_pipeline called with the path from get_path
            mock_load.assert_called_once_with(Path("/mock/path.json"))
            # excluded comes from fresh data, not stale
            mock_notify.assert_called_once()
            call_kwargs = mock_notify.call_args
            assert call_kwargs[1]["excluded"] == ["reviewer_fresh"]
            # durable payload comes from pending dict
            assert call_kwargs[0] == (
                "proj", "DESIGN_REVIEW",
                [{"issue": 1, "title": "t"}], "ns/proj",
            )
            assert call_kwargs[1]["repo_path"] == "/tmp/repo"
            assert call_kwargs[1]["review_mode"] == "short_context"
            assert call_kwargs[1]["base_commit"] == "abc123"
            # pending cleared after success
            mock_clear.assert_called_once_with("proj", "review")


class TestRecoverReviewFreshComment:
    """Test B: review リカバリが comment を fresh state から取得する。"""

    def test_comment_from_fresh_data(self) -> None:
        pending = {
            "review": {
                "new_state": "CODE_REVIEW",
                "batch": [{"issue": 2, "title": "t2"}],
                "gitlab": "ns/proj2",
            },
        }
        fresh_pipeline = {
            "excluded_reviewers": [],
            "comment": "updated comment",
        }

        with (
            patch("engine.fsm.load_pipeline", return_value=fresh_pipeline),
            patch("engine.fsm.get_path", return_value=Path("/mock/p.json")),
            patch("engine.fsm.notify_reviewers") as mock_notify,
            patch("engine.fsm.clear_pending_notification"),
        ):
            _recover_pending_notifications("proj2", pending)

            mock_notify.assert_called_once()
            assert mock_notify.call_args[1]["comment"] == "updated comment"


class TestRecoverReviewLoadPipelineFailure:
    """Test C: load_pipeline 失敗時のフォールバック。"""

    def test_load_pipeline_error_suppressed(self) -> None:
        pending = {
            "review": {
                "new_state": "DESIGN_REVIEW",
                "batch": [{"issue": 3, "title": "t3"}],
                "gitlab": "ns/proj3",
            },
        }

        with (
            patch("engine.fsm.load_pipeline", side_effect=FileNotFoundError("gone")),
            patch("engine.fsm.get_path", return_value=Path("/missing.json")),
            patch("engine.fsm.notify_reviewers") as mock_notify,
            patch("engine.fsm.clear_pending_notification") as mock_clear,
        ):
            # Should not raise
            _recover_pending_notifications("proj3", pending)

            # notify_reviewers must NOT be called (load_pipeline failed before it)
            mock_notify.assert_not_called()
            # pending must NOT be cleared (at-least-once guarantee)
            mock_clear.assert_not_called()
