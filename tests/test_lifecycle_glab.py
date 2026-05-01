"""tests/test_lifecycle_glab.py — _fetch_issue_info / _fetch_open_issues の run_glab 統一テスト。"""

import json
from unittest.mock import patch

import pytest

from engine.glab import GlabResult


def _ok_json(data: dict) -> GlabResult:
    return GlabResult(ok=True, stdout=json.dumps(data), stderr="", returncode=0, error=None)


def _fail(stderr: str = "502", error=None) -> GlabResult:
    return GlabResult(ok=False, stdout="", stderr=stderr, returncode=1, error=error)


# --- _fetch_issue_info ---

class TestFetchIssueInfo:
    def test_returns_state_on_success(self):
        import commands.dev
        data = {"title": "T", "state": "closed", "author": {"username": "testns"}}
        with patch("commands.dev.lifecycle.run_glab", return_value=_ok_json(data)):
            title, state = commands.dev._fetch_issue_info(42, "testns/proj")
        assert title == "T"
        assert state == "closed"

    def test_returns_none_on_glab_failure(self):
        import commands.dev
        with patch("commands.dev.lifecycle.run_glab", return_value=_fail("502")):
            title, state = commands.dev._fetch_issue_info(42, "testns/proj")
        assert title == ""
        assert state is None

    def test_raises_on_file_not_found(self):
        import commands.dev
        result = GlabResult(ok=False, stdout="", stderr="", returncode=None,
                            error=FileNotFoundError("glab"))
        with patch("commands.dev.lifecycle.run_glab", return_value=result):
            with pytest.raises(FileNotFoundError):
                commands.dev._fetch_issue_info(42, "testns/proj")

    def test_preserves_unauthorized_author_error(self):
        import commands.dev
        from engine.filter import UnauthorizedAuthorError
        data = {"title": "T", "state": "opened", "author": {"username": "hacker"}}
        with patch("commands.dev.lifecycle.run_glab", return_value=_ok_json(data)):
            with pytest.raises(UnauthorizedAuthorError):
                commands.dev._fetch_issue_info(42, "testns/proj")

    def test_uses_retries_2(self):
        import commands.dev
        data = {"title": "T", "state": "opened", "author": {"username": "testns"}}
        with patch("commands.dev.lifecycle.run_glab", return_value=_ok_json(data)) as mock:
            commands.dev._fetch_issue_info(42, "testns/proj")
        _, kwargs = mock.call_args
        assert kwargs["retries"] == 2

    def test_propagates_permission_error(self):
        import commands.dev
        with patch("commands.dev.lifecycle.run_glab", side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError):
                commands.dev._fetch_issue_info(42, "testns/proj")

    def test_propagates_os_error(self):
        import commands.dev
        with patch("commands.dev.lifecycle.run_glab", side_effect=OSError("disk error")):
            with pytest.raises(OSError):
                commands.dev._fetch_issue_info(42, "testns/proj")


# --- _fetch_open_issues ---

class TestFetchOpenIssues:
    def test_uses_run_glab(self):
        import commands.dev
        issues = [
            {"iid": 1, "title": "A", "state": "opened"},
            {"iid": 2, "title": "B", "state": "closed"},
        ]
        with patch("commands.dev.lifecycle.run_glab", return_value=_ok_json(issues)):
            result = commands.dev._fetch_open_issues("testns/proj")
        assert result == [(1, "A")]

    def test_raises_on_file_not_found(self):
        import commands.dev
        result = GlabResult(ok=False, stdout="", stderr="", returncode=None,
                            error=FileNotFoundError("glab"))
        with patch("commands.dev.lifecycle.run_glab", return_value=result):
            with pytest.raises(FileNotFoundError):
                commands.dev._fetch_open_issues("testns/proj")


# --- cmd_get_comments pagination ---

class TestPaginationLoop:
    def test_uses_run_glab(self):
        from commands.dev.lifecycle import cmd_get_comments
        import argparse

        page1 = [{"id": 1, "body": "note1", "author": {"username": "u"}, "created_at": "2025-01-01T00:00:00Z", "system": False}]
        page2 = []

        with patch("commands.dev.lifecycle.run_glab", side_effect=[
            _ok_json(page1), _ok_json(page2),
        ]):
            args = argparse.Namespace(project="gokrax", issue=42)
            with patch("commands.dev.lifecycle.get_path"), \
                 patch("commands.dev.lifecycle.load_pipeline", return_value={"gitlab": "testns/proj"}), \
                 patch("builtins.print"):
                cmd_get_comments(args)
