"""Tests for gokrax ok CLI command (Issue #258)."""

import json
import types

import pytest

import config
import pipeline_io
from commands.dev import cmd_ok


@pytest.fixture()
def pipeline_dir(tmp_path, monkeypatch):
    """Redirect PIPELINES_DIR to tmp_path."""
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
    monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)
    return tmp_path


def _write_pipeline(pipeline_dir, project: str, data: dict) -> None:
    path = pipeline_dir / f"{project}.json"
    path.write_text(json.dumps(data))


class TestCmdOk:
    def test_sets_merge_approved(self, pipeline_dir):
        """Normal: MERGE_SUMMARY_SENT -> merge_approved=True."""
        _write_pipeline(pipeline_dir, "test", {"state": "MERGE_SUMMARY_SENT", "project": "test"})
        args = types.SimpleNamespace(project="test")
        cmd_ok(args)

        data = json.loads((pipeline_dir / "test.json").read_text())
        assert data["merge_approved"] is True

    def test_rejects_wrong_state(self, pipeline_dir):
        """Error: non-MERGE_SUMMARY_SENT state raises SystemExit."""
        _write_pipeline(pipeline_dir, "test", {"state": "CODE_APPROVED", "project": "test"})
        args = types.SimpleNamespace(project="test")
        with pytest.raises(SystemExit, match="Cannot approve in state CODE_APPROVED"):
            cmd_ok(args)


class TestCheckTransitionMergeApproved:
    def test_merge_approved_transitions_to_done(self):
        """check_transition returns DONE when merge_approved=True."""
        from engine.fsm import check_transition

        action = check_transition(
            "MERGE_SUMMARY_SENT",
            batch=[],
            data={"merge_approved": True, "project": "test"},
        )
        assert action.new_state == "DONE"
