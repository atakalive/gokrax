"""tests/test_cc_glab.py — _mark_push_failed / _auto_push_and_close の run_glab 統一テスト。"""

import json
from unittest.mock import patch, MagicMock

from engine.glab import GlabResult


def _ok_json(data: dict) -> GlabResult:
    return GlabResult(ok=True, stdout=json.dumps(data), stderr="", returncode=0, error=None)


def _ok() -> GlabResult:
    return GlabResult(ok=True, stdout="", stderr="", returncode=0, error=None)


def _fail(stderr: str = "error") -> GlabResult:
    return GlabResult(ok=False, stdout="", stderr=stderr, returncode=1, error=None)


class TestMarkPushFailed:
    def test_uses_run_glab(self):
        from engine.cc import _mark_push_failed
        view = _ok_json({"title": "My Issue"})
        update = _ok()
        with patch("engine.glab.run_glab", side_effect=[view, update]) as mock:
            _mark_push_failed("testns/proj", [{"issue": 42}], "proj")
        assert mock.call_count == 2
        view_argv = mock.call_args_list[0][0][0]
        assert "view" in view_argv
        update_argv = mock.call_args_list[1][0][0]
        assert "update" in update_argv
        assert "[PUSH FAILED] My Issue" in " ".join(update_argv)


class TestAutoPushAndClose:
    def test_close_uses_run_glab(self):
        from engine.cc import _auto_push_and_close
        push_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=push_result), \
             patch("engine.glab.run_glab", return_value=_ok()) as mock:
            _auto_push_and_close("/tmp/repo", "testns/proj", [{"issue": 42}], "proj")
        assert mock.call_count == 1
        argv = mock.call_args[0][0]
        assert argv == ["issue", "close", "42", "-R", "testns/proj"]
