"""Tests for `gokrax issue-update` (commands/issue_ops.py)."""

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from commands.issue_ops import cmd_issue_update
from engine.glab import GlabResult
from tests.conftest import TEST_GITLAB_NS, write_pipeline


def _make_args(project: str, issue: int, body_file: Path, title: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        project=project, issue=issue, body_file=body_file, title=title
    )


def _setup_pipeline(tmp_pipelines, sample_pipeline, project: str = "test-pj",
                    gitlab: str | None = ...) -> None:
    data = dict(sample_pipeline)
    data["project"] = project
    if gitlab is not ...:
        data["gitlab"] = gitlab
    write_pipeline(tmp_pipelines / f"{project}.json", data)


def _ok() -> GlabResult:
    return GlabResult(ok=True, stdout="", stderr="", returncode=0, error=None)


def _fail(stderr: str = "boom") -> GlabResult:
    return GlabResult(ok=False, stdout="", stderr=stderr, returncode=1, error=None)


class TestSuccess:
    def test_argv_no_title(self, tmp_pipelines, sample_pipeline, tmp_path):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hello body", encoding="utf-8")
        args = _make_args("test-pj", 42, bf)

        with patch("commands.issue_ops.run_glab", return_value=_ok()) as run:
            cmd_issue_update(args)

        assert run.call_count == 1
        argv = run.call_args[0][0]
        assert argv[0:6] == ["issue", "update", "42", "-R", f"{TEST_GITLAB_NS}/test-pj", "-d"]
        assert argv[6] == "hello body"
        assert "-t" not in argv
        assert bf.exists()

    def test_argv_with_title(self, tmp_pipelines, sample_pipeline, tmp_path):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("body", encoding="utf-8")
        args = _make_args("test-pj", 7, bf, title="new title")

        with patch("commands.issue_ops.run_glab", return_value=_ok()) as run:
            cmd_issue_update(args)

        argv = run.call_args[0][0]
        assert argv[-2:] == ["-t", "new title"]

    def test_trailing_newline_preserved(self, tmp_pipelines, sample_pipeline, tmp_path):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hello\n", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        with patch("commands.issue_ops.run_glab", return_value=_ok()) as run:
            cmd_issue_update(args)

        argv = run.call_args[0][0]
        assert argv[6] == "hello\n"


class TestValidation:
    def test_missing_file(self, tmp_pipelines, sample_pipeline, tmp_path, capsys):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        args = _make_args("test-pj", 1, tmp_path / "no-such.md")

        with patch("commands.issue_ops.run_glab") as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert not run.called
        assert "body-file not found" in capsys.readouterr().err

    def test_empty_file(self, tmp_pipelines, sample_pipeline, tmp_path, capsys):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "empty.md"
        bf.write_text("", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        with patch("commands.issue_ops.run_glab") as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert not run.called
        assert "body-file is empty" in capsys.readouterr().err

    def test_whitespace_only_file(self, tmp_pipelines, sample_pipeline, tmp_path, capsys):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "ws.md"
        bf.write_text("   \n\t\n", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        with patch("commands.issue_ops.run_glab") as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert not run.called
        assert "body-file is empty" in capsys.readouterr().err

    def test_size_limit(self, tmp_pipelines, sample_pipeline, tmp_path, capsys, monkeypatch):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        monkeypatch.setattr("commands.issue_ops.MAX_CLI_ARG_BYTES", 10)
        bf = tmp_path / "big.md"
        bf.write_text("x" * 11, encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        with patch("commands.issue_ops.run_glab") as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert not run.called
        assert "exceeds MAX_CLI_ARG_BYTES" in capsys.readouterr().err


class TestFailures:
    def test_failure_exits(self, tmp_pipelines, sample_pipeline, tmp_path, capsys):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hi", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        with patch("commands.issue_ops.run_glab", return_value=_fail("nope")) as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert run.call_count == 1
        assert bf.exists()
        assert "nope" in capsys.readouterr().err

    def test_filenotfound_exits(self, tmp_pipelines, sample_pipeline, tmp_path, capsys):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hi", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        result = GlabResult(ok=False, stdout="", stderr="", returncode=None,
                            error=FileNotFoundError("no glab"))
        with patch("commands.issue_ops.run_glab", return_value=result) as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert run.call_count == 1
        assert "glab binary not found" in capsys.readouterr().err

    def test_permissionerror_exits(self, tmp_pipelines, sample_pipeline, tmp_path, capsys):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hi", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        with patch("commands.issue_ops.run_glab", side_effect=PermissionError("denied")) as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert run.call_count == 1
        assert "failed to invoke glab" in capsys.readouterr().err


class TestGitlabResolve:
    def test_empty_gitlab_falls_back(self, tmp_pipelines, sample_pipeline, tmp_path, monkeypatch):
        monkeypatch.setattr("commands.issue_ops.GITLAB_NAMESPACE", TEST_GITLAB_NS)
        _setup_pipeline(tmp_pipelines, sample_pipeline, project="myproj", gitlab="")
        bf = tmp_path / "body.md"
        bf.write_text("hi", encoding="utf-8")
        args = _make_args("myproj", 5, bf)

        with patch("commands.issue_ops.run_glab", return_value=_ok()) as run:
            cmd_issue_update(args)
        argv = run.call_args[0][0]
        assert argv[argv.index("-R") + 1] == f"{TEST_GITLAB_NS}/myproj"
