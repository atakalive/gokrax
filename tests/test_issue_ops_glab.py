"""tests/test_issue_ops_glab.py — cmd_issue_update の run_glab 統一テスト。"""

import argparse
from unittest.mock import patch

import pytest

from engine.glab import GlabResult


def _ok() -> GlabResult:
    return GlabResult(ok=True, stdout="", stderr="", returncode=0, error=None)


def _fail(stderr: str = "nope", error=None) -> GlabResult:
    return GlabResult(ok=False, stdout="", stderr=stderr, returncode=1, error=error)


def _make_args(tmp_path, body="hello", title=None):
    body_file = tmp_path / "body.md"
    body_file.write_text(body)
    return argparse.Namespace(
        project="gokrax",
        issue=42,
        body_file=str(body_file),
        title=title,
    )


class TestCmdIssueUpdate:
    def test_uses_run_glab(self, tmp_path):
        from commands.issue_ops import cmd_issue_update
        args = _make_args(tmp_path)
        with patch("commands.issue_ops.run_glab", return_value=_ok()) as mock, \
             patch("commands.issue_ops.load_pipeline", return_value={"gitlab": "testns/proj"}):
            cmd_issue_update(args)
        assert mock.call_count == 1

    def test_failure_exits(self, tmp_path):
        from commands.issue_ops import cmd_issue_update
        args = _make_args(tmp_path)
        with patch("commands.issue_ops.run_glab", return_value=_fail()), \
             patch("commands.issue_ops.load_pipeline", return_value={"gitlab": "testns/proj"}):
            with pytest.raises(SystemExit):
                cmd_issue_update(args)

    def test_permission_error(self, tmp_path):
        from commands.issue_ops import cmd_issue_update
        args = _make_args(tmp_path)
        with patch("commands.issue_ops.run_glab", side_effect=PermissionError("denied")), \
             patch("commands.issue_ops.load_pipeline", return_value={"gitlab": "testns/proj"}):
            with pytest.raises(SystemExit):
                cmd_issue_update(args)

    def test_file_not_found(self, tmp_path):
        from commands.issue_ops import cmd_issue_update
        args = _make_args(tmp_path)
        result = GlabResult(ok=False, stdout="", stderr="", returncode=None,
                            error=FileNotFoundError("glab"))
        with patch("commands.issue_ops.run_glab", return_value=result), \
             patch("commands.issue_ops.load_pipeline", return_value={"gitlab": "testns/proj"}):
            with pytest.raises(SystemExit):
                cmd_issue_update(args)
