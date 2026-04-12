"""tests/test_blocked_report.py — blocked-report CLI command (Issue #310)"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _write_pipeline(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _blocked_pipeline(project: str = "test-pj", queue_mode: bool = False) -> dict:
    return {
        "project": project,
        "gitlab": f"testns/{project}",
        "state": "BLOCKED",
        "enabled": False,
        "implementer": "impl1",
        "queue_mode": queue_mode,
        "batch": [{"issue": 1, "title": "t"}],
        "history": [],
    }


class TestCmdBlockedReport:

    def test_blocked_report_sends_discord(self, tmp_pipelines):
        """BLOCKED state: Discord notification is sent."""
        from commands.dev import cmd_blocked_report

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, _blocked_pipeline())

        args = SimpleNamespace(project="test-pj", summary="dependency issue")
        mock_notify = MagicMock()
        with patch("commands.dev.notify_discord", new=mock_notify):
            cmd_blocked_report(args)

        mock_notify.assert_called_once_with(
            "[test-pj] BLOCKED report: dependency issue"
        )

    def test_blocked_report_queue_mode_prefix(self, tmp_pipelines):
        """queue_mode=True: [Queue] prefix is prepended."""
        from commands.dev import cmd_blocked_report

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, _blocked_pipeline(queue_mode=True))

        args = SimpleNamespace(project="test-pj", summary="queue issue")
        mock_notify = MagicMock()
        with patch("commands.dev.notify_discord", new=mock_notify):
            cmd_blocked_report(args)

        mock_notify.assert_called_once_with(
            "[Queue][test-pj] BLOCKED report: queue issue"
        )

    def test_blocked_report_rejects_non_blocked_state(self, tmp_pipelines):
        """Non-BLOCKED state raises SystemExit."""
        from commands.dev import cmd_blocked_report

        data = _blocked_pipeline()
        data["state"] = "IMPLEMENTATION"
        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, data)

        args = SimpleNamespace(project="test-pj", summary="some issue")
        with pytest.raises(SystemExit, match="Not in BLOCKED state"):
            cmd_blocked_report(args)

    def test_blocked_report_truncates_at_500(self, tmp_pipelines):
        """Summary is truncated to 500 characters."""
        from commands.dev import cmd_blocked_report

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, _blocked_pipeline())

        args = SimpleNamespace(project="test-pj", summary="x" * 600)
        mock_notify = MagicMock()
        with patch("commands.dev.notify_discord", new=mock_notify):
            cmd_blocked_report(args)

        call_msg = mock_notify.call_args[0][0]
        # Extract summary part after "BLOCKED report: "
        summary_part = call_msg.split("BLOCKED report: ", 1)[1]
        assert len(summary_part) == 500

    def test_blocked_report_rejects_empty_summary(self, tmp_pipelines):
        """Empty/whitespace-only summary raises SystemExit."""
        from commands.dev import cmd_blocked_report

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, _blocked_pipeline())

        args = SimpleNamespace(project="test-pj", summary="   ")
        with pytest.raises(SystemExit, match="must not be empty"):
            cmd_blocked_report(args)
