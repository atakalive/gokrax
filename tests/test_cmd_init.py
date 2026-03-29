"""Tests for cmd_init repo_path normalization (Issue #267)."""

import json
import types

import pytest

import config
import pipeline_io
from commands.dev import cmd_init


@pytest.fixture()
def pipeline_dir(tmp_path, monkeypatch):
    """Redirect PIPELINES_DIR to tmp_path."""
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
    monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)
    return tmp_path


class TestCmdInitRepoPath:
    def test_relative_path_converted_to_absolute(self, pipeline_dir, tmp_path, monkeypatch):
        (tmp_path / "myrepo").mkdir()
        monkeypatch.chdir(tmp_path)
        args = types.SimpleNamespace(project="testpj", gitlab=None, repo_path="myrepo", implementer=None)
        cmd_init(args)
        data = json.loads((pipeline_dir / "testpj.json").read_text())
        assert data["repo_path"] == str(tmp_path / "myrepo")

    def test_absolute_path_stored_as_is(self, pipeline_dir, tmp_path):
        (tmp_path / "myrepo").mkdir()
        abs_path = str(tmp_path / "myrepo")
        args = types.SimpleNamespace(project="testpj", gitlab=None, repo_path=abs_path, implementer=None)
        cmd_init(args)
        data = json.loads((pipeline_dir / "testpj.json").read_text())
        assert data["repo_path"] == abs_path

    def test_empty_repo_path_remains_empty(self, pipeline_dir):
        args = types.SimpleNamespace(project="testpj", gitlab=None, repo_path="", implementer=None)
        cmd_init(args)
        data = json.loads((pipeline_dir / "testpj.json").read_text())
        assert data["repo_path"] == ""

    def test_nonexistent_path_exits_with_error(self, pipeline_dir):
        args = types.SimpleNamespace(project="testpj", gitlab=None, repo_path="/nonexistent/path/xyz", implementer=None)
        with pytest.raises(SystemExit):
            cmd_init(args)
