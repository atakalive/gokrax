"""Tests for batch Issue notification timing (Issue #241).

Verify that the target Issue list is notified on Discord when transitioning
from INITIALIZE regardless of the destination state (DESIGN_PLAN or
DESIGN_APPROVED with --skip-design).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from engine.fsm import TransitionAction


def _write_pipeline(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _base_data(batch: list[dict] | None = None) -> dict:
    return {
        "project": "test-pj",
        "gitlab": "testns/test-pj",
        "state": "INITIALIZE",
        "enabled": True,
        "review_mode": "standard",
        "batch": batch or [
            {"issue": 1, "title": "First issue"},
            {"issue": 2, "title": "Second issue"},
        ],
        "history": [],
    }


class TestBatchNotifyOnInitialize:
    """Issue #241: batch Issue notification fires on any INITIALIZE exit."""

    def test_initialize_to_design_plan_notifies_issues(
        self, tmp_pipelines: Path, monkeypatch,
    ) -> None:
        """INITIALIZE -> DESIGN_PLAN: target Issue list is notified (regression)."""
        from watchdog import process

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, _base_data())
        monkeypatch.setattr("watchdog.PIPELINES_DIR", tmp_pipelines)

        mock_discord = MagicMock()

        with patch("watchdog.check_transition", return_value=TransitionAction(new_state="DESIGN_PLAN")), \
             patch("watchdog.notify_discord", mock_discord), \
             patch("watchdog.notify_implementer"), \
             patch("watchdog._poll_pytest_baseline"):
            process(path)

        discord_msgs = [call[0][0] for call in mock_discord.call_args_list]
        issue_msgs = [m for m in discord_msgs if "Target Issues" in m or "対象Issue" in m]
        assert len(issue_msgs) == 1, f"Expected 1 issue notification, got {len(issue_msgs)}: {discord_msgs}"
        assert "#1:" in issue_msgs[0]
        assert "#2:" in issue_msgs[0]

    def test_initialize_to_design_approved_notifies_issues(
        self, tmp_pipelines: Path, monkeypatch,
    ) -> None:
        """INITIALIZE -> DESIGN_APPROVED (skip-design): target Issue list is notified."""
        from watchdog import process

        path = tmp_pipelines / "test-pj.json"
        data = _base_data()
        data["skip_design"] = True
        _write_pipeline(path, data)
        monkeypatch.setattr("watchdog.PIPELINES_DIR", tmp_pipelines)

        mock_discord = MagicMock()

        with patch("watchdog.check_transition", return_value=TransitionAction(new_state="DESIGN_APPROVED")), \
             patch("watchdog.notify_discord", mock_discord), \
             patch("watchdog.notify_implementer"), \
             patch("watchdog._poll_pytest_baseline"):
            process(path)

        discord_msgs = [call[0][0] for call in mock_discord.call_args_list]
        issue_msgs = [m for m in discord_msgs if "Target Issues" in m or "対象Issue" in m]
        assert len(issue_msgs) == 1, f"Expected 1 issue notification, got {len(issue_msgs)}: {discord_msgs}"
        assert "#1:" in issue_msgs[0]
        assert "#2:" in issue_msgs[0]
