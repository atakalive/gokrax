"""Tests for gokrax get-comments command (Issue #279)."""

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from commands.dev import cmd_get_comments


def _make_args(project: str = "test-pj", issue: int = 10) -> argparse.Namespace:
    return argparse.Namespace(project=project, issue=issue)


def _mock_run_pages(pages: list[list[dict]]):
    """Return a side_effect function that yields pages then empty list."""
    calls = [*pages, []]  # append terminator
    idx = {"i": 0}

    def side_effect(*args, **kwargs):
        m = MagicMock()
        m.returncode = 0
        m.stdout = json.dumps(calls[idx["i"]])
        m.stderr = ""
        idx["i"] += 1
        return m

    return side_effect


# ---------------------------------------------------------------------------
# Test 1: Only allowed user comments are output
# ---------------------------------------------------------------------------


class TestFilteredOutput:
    def test_allowed_only(self, monkeypatch, capsys):
        monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner_user")
        monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
        monkeypatch.setattr("commands.dev.load_pipeline", lambda p: {"gitlab": "ns/proj"})
        monkeypatch.setattr("commands.dev.get_path", lambda p: Path("/tmp/fake_pipeline.json"))

        notes = [
            {"id": 1, "system": False, "body": "allowed comment",
             "created_at": "2026-01-01T00:00:00Z",
             "author": {"username": "owner_user"}},
            {"id": 2, "system": False, "body": "evil comment",
             "created_at": "2026-01-01T01:00:00Z",
             "author": {"username": "attacker"}},
        ]

        with patch("commands.dev.subprocess.run", side_effect=_mock_run_pages([notes])):
            cmd_get_comments(_make_args())

        out = capsys.readouterr().out
        assert "allowed comment" in out
        assert "evil comment" not in out


# ---------------------------------------------------------------------------
# Test 2: System notes are skipped
# ---------------------------------------------------------------------------


class TestSystemNoteSkipped:
    def test_system_notes_skipped(self, monkeypatch, capsys):
        monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner_user")
        monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
        monkeypatch.setattr("commands.dev.load_pipeline", lambda p: {"gitlab": "ns/proj"})
        monkeypatch.setattr("commands.dev.get_path", lambda p: Path("/tmp/fake_pipeline.json"))

        notes = [
            {"id": 1, "system": True, "body": "added label",
             "created_at": "2026-01-01T00:00:00Z",
             "author": {"username": "owner_user"}},
            {"id": 2, "system": False, "body": "real comment",
             "created_at": "2026-01-01T01:00:00Z",
             "author": {"username": "owner_user"}},
        ]

        with patch("commands.dev.subprocess.run", side_effect=_mock_run_pages([notes])):
            cmd_get_comments(_make_args())

        out = capsys.readouterr().out
        assert "added label" not in out
        assert "real comment" in out


# ---------------------------------------------------------------------------
# Test 3: glab api non-zero exit raises SystemExit
# ---------------------------------------------------------------------------


class TestGlabApiError:
    def test_nonzero_exit(self, monkeypatch, capsys):
        monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner_user")
        monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
        monkeypatch.setattr("commands.dev.load_pipeline", lambda p: {"gitlab": "ns/proj"})
        monkeypatch.setattr("commands.dev.get_path", lambda p: Path("/tmp/fake_pipeline.json"))

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "api error"
        mock_result.stdout = ""

        with patch("commands.dev.subprocess.run", return_value=mock_result):
            with pytest.raises(SystemExit) as exc_info:
                cmd_get_comments(_make_args())

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Error" in err


# ---------------------------------------------------------------------------
# Test 4: Pagination (2 pages of notes combined)
# ---------------------------------------------------------------------------


class TestPagination:
    def test_two_pages(self, monkeypatch, capsys):
        monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner_user")
        monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
        monkeypatch.setattr("commands.dev.load_pipeline", lambda p: {"gitlab": "ns/proj"})
        monkeypatch.setattr("commands.dev.get_path", lambda p: Path("/tmp/fake_pipeline.json"))

        page1 = [
            {"id": i, "system": False, "body": f"page1 comment {i}",
             "created_at": "2026-01-01T00:00:00Z",
             "author": {"username": "owner_user"}}
            for i in range(100)
        ]
        page2 = [
            {"id": 100, "system": False, "body": "page2 comment",
             "created_at": "2026-01-02T00:00:00Z",
             "author": {"username": "owner_user"}},
        ]

        with patch("commands.dev.subprocess.run", side_effect=_mock_run_pages([page1, page2])):
            cmd_get_comments(_make_args())

        out = capsys.readouterr().out
        assert "page1 comment 0" in out
        assert "page1 comment 99" in out
        assert "page2 comment" in out
