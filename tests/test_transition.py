"""tests/test_transition.py — cmd_transition --force フラグテスト"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def write_pipeline(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class TestTransitionForce:

    def test_normal_valid_transition(self, tmp_pipelines, sample_pipeline):
        """通常遷移（IDLE→DESIGN_PLAN）→ 成功"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        import argparse
        args = argparse.Namespace(project="test-pj", to="DESIGN_PLAN", actor="cli", force=False)
        cmd_transition(args)
        with open(path) as f:
            assert json.load(f)["state"] == "DESIGN_PLAN"

    def test_invalid_transition_rejected(self, tmp_pipelines, sample_pipeline):
        """不正遷移（IDLE→CODE_REVIEW）→ SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        import argparse
        args = argparse.Namespace(project="test-pj", to="CODE_REVIEW", actor="cli", force=False)
        with pytest.raises(SystemExit, match="Invalid transition"):
            cmd_transition(args)

    def test_force_skips_transition_validation(self, tmp_pipelines, sample_pipeline):
        """--force で不正遷移 → 成功"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        import argparse
        args = argparse.Namespace(project="test-pj", to="CODE_REVIEW", actor="cli", force=True)
        cmd_transition(args)
        with open(path) as f:
            assert json.load(f)["state"] == "CODE_REVIEW"

    def test_force_to_blocked(self, tmp_pipelines, sample_pipeline):
        """--force で IMPLEMENTATION→BLOCKED → 成功 + history記録"""
        sample_pipeline["state"] = "IMPLEMENTATION"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        import argparse
        args = argparse.Namespace(project="test-pj", to="BLOCKED", actor="M", force=True)
        cmd_transition(args)
        with open(path) as f:
            data = json.load(f)
        assert data["state"] == "BLOCKED"
        assert data["history"][-1]["from"] == "IMPLEMENTATION"
        assert data["history"][-1]["to"] == "BLOCKED"
        assert data["history"][-1]["actor"] == "M"

    def test_nonexistent_state_rejected_even_with_force(self, tmp_pipelines, sample_pipeline):
        """存在しない状態名 → --force でも SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        import argparse
        args = argparse.Namespace(project="test-pj", to="NONEXISTENT", actor="cli", force=True)
        with pytest.raises(SystemExit, match="Invalid state"):
            cmd_transition(args)

    def test_blocked_to_idle_without_force(self, tmp_pipelines, sample_pipeline):
        """BLOCKED→IDLE → --force 不要（通常遷移で成功）"""
        sample_pipeline["state"] = "BLOCKED"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        import argparse
        args = argparse.Namespace(project="test-pj", to="IDLE", actor="cli", force=False)
        cmd_transition(args)
        with open(path) as f:
            data = json.load(f)
        assert data["state"] == "IDLE"
        assert data["batch"] == []
        assert data["enabled"] is False
