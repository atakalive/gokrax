"""tests/test_notify_glab.py — post_gitlab_note / fetch_issue_body の run_glab 統一テスト。"""

import json
from unittest.mock import patch

import pytest

from engine.glab import GlabResult


def _ok_json(data: dict) -> GlabResult:
    return GlabResult(ok=True, stdout=json.dumps(data), stderr="", returncode=0, error=None)


def _ok() -> GlabResult:
    return GlabResult(ok=True, stdout="", stderr="", returncode=0, error=None)


def _fail(stderr: str = "error", error=None) -> GlabResult:
    return GlabResult(ok=False, stdout="", stderr=stderr, returncode=1, error=error)


class TestPostGitlabNote:
    def test_uses_retries_one(self):
        import notify
        with patch("notify.run_glab", return_value=_ok()) as mock:
            notify.post_gitlab_note("testns/proj", 42, "body")
        _, kwargs = mock.call_args
        assert kwargs["retries"] == 1

    def test_no_duplicate_on_failure(self):
        import notify
        with patch("notify.run_glab", return_value=_fail()) as mock:
            result = notify.post_gitlab_note("testns/proj", 42, "body")
        assert result is False
        assert mock.call_count == 1


class TestFetchIssueBody:
    def test_uses_run_glab(self):
        import notify
        data = {"description": "body text", "author": {"username": "testns"}}
        with patch("notify.run_glab", return_value=_ok_json(data)):
            result = notify.fetch_issue_body(42, "testns/proj")
        assert result == "body text"

    def test_raises_on_file_not_found(self):
        import notify
        result = GlabResult(ok=False, stdout="", stderr="", returncode=None,
                            error=FileNotFoundError("glab"))
        with patch("notify.run_glab", return_value=result):
            with pytest.raises(FileNotFoundError):
                notify.fetch_issue_body(42, "testns/proj")
