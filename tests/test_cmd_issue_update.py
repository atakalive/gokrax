"""Tests for `gokrax issue-update` (commands/issue_ops.py)."""

import argparse
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from commands.issue_ops import cmd_issue_update
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


def _ok_run(*_a, **_kw):
    m = MagicMock()
    m.returncode = 0
    m.stderr = ""
    return m


def _fail_run(stderr: str = "boom"):
    def _runner(*_a, **_kw):
        m = MagicMock()
        m.returncode = 1
        m.stderr = stderr
        return m
    return _runner


class TestSuccess:
    def test_argv_no_title(self, tmp_pipelines, sample_pipeline, tmp_path):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hello body", encoding="utf-8")
        args = _make_args("test-pj", 42, bf)

        with patch("commands.issue_ops.subprocess.run", side_effect=_ok_run) as run:
            cmd_issue_update(args)

        assert run.call_count == 1
        argv = run.call_args[0][0]
        assert argv[1:7] == ["issue", "update", "42", "-R", f"{TEST_GITLAB_NS}/test-pj", "-d"]
        assert argv[7] == "hello body"
        assert "-t" not in argv
        # body-file is not deleted
        assert bf.exists()

    def test_argv_with_title(self, tmp_pipelines, sample_pipeline, tmp_path):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("body", encoding="utf-8")
        args = _make_args("test-pj", 7, bf, title="new title")

        with patch("commands.issue_ops.subprocess.run", side_effect=_ok_run) as run:
            cmd_issue_update(args)

        argv = run.call_args[0][0]
        assert argv[-2:] == ["-t", "new title"]

    def test_trailing_newline_preserved(self, tmp_pipelines, sample_pipeline, tmp_path):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hello\n", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        with patch("commands.issue_ops.subprocess.run", side_effect=_ok_run) as run:
            cmd_issue_update(args)

        argv = run.call_args[0][0]
        assert argv[7] == "hello\n"


class TestValidation:
    def test_missing_file(self, tmp_pipelines, sample_pipeline, tmp_path, capsys):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        args = _make_args("test-pj", 1, tmp_path / "no-such.md")

        with patch("commands.issue_ops.subprocess.run") as run:
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

        with patch("commands.issue_ops.subprocess.run") as run:
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

        with patch("commands.issue_ops.subprocess.run") as run:
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

        with patch("commands.issue_ops.subprocess.run") as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert not run.called
        assert "exceeds MAX_CLI_ARG_BYTES" in capsys.readouterr().err


class TestRetries:
    def test_all_failures(self, tmp_pipelines, sample_pipeline, tmp_path, capsys):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hi", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        with patch("commands.issue_ops.subprocess.run", side_effect=_fail_run("nope")) as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert run.call_count == 3
        assert bf.exists()
        assert "nope" in capsys.readouterr().err

    def test_retry_then_success(self, tmp_pipelines, sample_pipeline, tmp_path):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hi", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        fail = MagicMock()
        fail.returncode = 1
        fail.stderr = "transient"
        ok = MagicMock()
        ok.returncode = 0
        ok.stderr = ""

        with patch("commands.issue_ops.subprocess.run", side_effect=[fail, ok]) as run, \
             patch("commands.issue_ops.time.sleep") as sleep:
            cmd_issue_update(args)
        assert run.call_count == 2
        assert sleep.call_count == 1

    def test_timeout_retries(self, tmp_pipelines, sample_pipeline, tmp_path):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hi", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        with patch("commands.issue_ops.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="glab", timeout=1)) as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert run.call_count == 3

    def test_filenotfound_no_retry(self, tmp_pipelines, sample_pipeline, tmp_path, capsys):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hi", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        with patch("commands.issue_ops.subprocess.run",
                   side_effect=FileNotFoundError("no glab")) as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert run.call_count == 1
        assert "failed to invoke" in capsys.readouterr().err

    def test_permissionerror_no_retry(self, tmp_pipelines, sample_pipeline, tmp_path, capsys):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hi", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        with patch("commands.issue_ops.subprocess.run",
                   side_effect=PermissionError("denied")) as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert run.call_count == 1
        assert "failed to invoke" in capsys.readouterr().err

    def test_blockingioerror_retries(self, tmp_pipelines, sample_pipeline, tmp_path):
        _setup_pipeline(tmp_pipelines, sample_pipeline)
        bf = tmp_path / "body.md"
        bf.write_text("hi", encoding="utf-8")
        args = _make_args("test-pj", 1, bf)

        with patch("commands.issue_ops.subprocess.run",
                   side_effect=BlockingIOError("eagain")) as run:
            with pytest.raises(SystemExit) as exc:
                cmd_issue_update(args)
        assert exc.value.code == 1
        assert run.call_count == 3


class TestGitlabResolve:
    def test_empty_gitlab_falls_back(self, tmp_pipelines, sample_pipeline, tmp_path, monkeypatch):
        monkeypatch.setattr("commands.issue_ops.GITLAB_NAMESPACE", TEST_GITLAB_NS)
        _setup_pipeline(tmp_pipelines, sample_pipeline, project="myproj", gitlab="")
        bf = tmp_path / "body.md"
        bf.write_text("hi", encoding="utf-8")
        args = _make_args("myproj", 5, bf)

        with patch("commands.issue_ops.subprocess.run", side_effect=_ok_run) as run:
            cmd_issue_update(args)
        argv = run.call_args[0][0]
        assert argv[argv.index("-R") + 1] == f"{TEST_GITLAB_NS}/myproj"
