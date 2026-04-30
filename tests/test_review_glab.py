"""tests/test_review_glab.py — _update_issue_title_with_assessment の run_glab 統一テスト。"""

import json
from unittest.mock import patch

from engine.glab import GlabResult


def _ok_json(data: dict) -> GlabResult:
    return GlabResult(ok=True, stdout=json.dumps(data), stderr="", returncode=0, error=None)


def _ok() -> GlabResult:
    return GlabResult(ok=True, stdout="", stderr="", returncode=0, error=None)


def _fail(stderr: str = "error") -> GlabResult:
    return GlabResult(ok=False, stdout="", stderr=stderr, returncode=1, error=None)


class TestUpdateTitleWithAssessment:
    def test_view_uses_run_glab(self):
        from commands.dev import _update_issue_title_with_assessment
        view = _ok_json({"title": "My Issue", "iid": 42})
        update = _ok()
        with patch("commands.dev.review.run_glab", side_effect=[view, update]) as mock:
            ok = _update_issue_title_with_assessment("testns/proj", 42, 3, "low")
        assert ok is True
        assert mock.call_count == 2
        assert mock.call_args_list[0][0][0] == ["issue", "view", "42", "--output", "json", "-R", "testns/proj"]

    def test_update_uses_run_glab(self):
        from commands.dev import _update_issue_title_with_assessment
        view = _ok_json({"title": "My Issue", "iid": 42})
        update = _ok()
        with patch("commands.dev.review.run_glab", side_effect=[view, update]) as mock:
            _update_issue_title_with_assessment("testns/proj", 42, 3, "low")
        update_call = mock.call_args_list[1]
        argv = update_call[0][0]
        assert argv[0] == "issue"
        assert argv[1] == "update"

    def test_view_failure_returns_false(self):
        from commands.dev import _update_issue_title_with_assessment
        with patch("commands.dev.review.run_glab", return_value=_fail()):
            ok = _update_issue_title_with_assessment("testns/proj", 42, 3, "low")
        assert ok is False
