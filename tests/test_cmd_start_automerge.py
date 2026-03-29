"""Tests for automerge flag persistence in cmd_start (Issue #269)."""

import json
import types

import pytest

import config
import pipeline_io
from commands.dev import cmd_start


@pytest.fixture()
def pipeline_dir(tmp_path, monkeypatch):
    """Redirect PIPELINES_DIR to tmp_path."""
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
    monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_internals(monkeypatch):
    """Stub out cmd_triage, cmd_transition, _start_loop, resolve_queue_options."""
    import commands.dev
    import gokrax as gokrax_mod

    monkeypatch.setattr(commands.dev, "cmd_triage", lambda args: None)
    monkeypatch.setattr(commands.dev, "cmd_transition", lambda args: None)
    monkeypatch.setattr(gokrax_mod, "_start_loop", lambda: None)
    monkeypatch.setattr(config, "resolve_queue_options", lambda project: {})


def _write_pipeline(pipeline_dir, project: str, data: dict) -> None:
    path = pipeline_dir / f"{project}.json"
    path.write_text(json.dumps(data))


def _base_pipeline() -> dict:
    return {
        "project": "test",
        "state": "IDLE",
        "enabled": False,
        "batch": [],
        "history": [],
        "review_mode": "min",
        "repo_path": "/tmp/test",
        "implementer": "impl1",
        "gitlab": "ns/test",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def _make_args(**overrides) -> types.SimpleNamespace:
    defaults = dict(
        project="test", issue=[1], mode="min",
        automerge=None, no_automerge=None,
        keep_context=None, keep_ctx_batch=None, keep_ctx_intra=None,
        keep_ctx_all=None, keep_ctx_none=None, p2_fix=None,
        skip_cc_plan=None, no_skip_cc_plan=None,
        skip_test=None, no_skip_test=None,
        skip_assess=None, no_skip_assess=None,
        skip_design=None, no_skip_design=None,
        no_cc=None, no_no_cc=None,
        exclude_high_risk=None, no_exclude_high_risk=None,
        exclude_any_risk=None, no_exclude_any_risk=None,
        allow_closed=False, comment=None,
        cc_plan_model=None, cc_impl_model=None,
        implementer=None, gitlab=None,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


class TestCmdStartAutomerge:
    def test_automerge_explicit_true(self, pipeline_dir):
        """--automerge -> pipeline JSON has automerge=True."""
        _write_pipeline(pipeline_dir, "test", _base_pipeline())
        args = _make_args(automerge=True)
        cmd_start(args)
        data = json.loads((pipeline_dir / "test.json").read_text())
        assert data["automerge"] is True

    def test_no_automerge_explicit(self, pipeline_dir):
        """--no-automerge -> pipeline JSON has automerge=False."""
        _write_pipeline(pipeline_dir, "test", _base_pipeline())
        args = _make_args(no_automerge=True)
        cmd_start(args)
        data = json.loads((pipeline_dir / "test.json").read_text())
        assert data["automerge"] is False

    def test_automerge_not_specified(self, pipeline_dir):
        """Neither flag -> pipeline JSON has no automerge key."""
        _write_pipeline(pipeline_dir, "test", _base_pipeline())
        args = _make_args()
        cmd_start(args)
        data = json.loads((pipeline_dir / "test.json").read_text())
        assert "automerge" not in data
