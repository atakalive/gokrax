"""tests/test_reset.py — cmd_reset コマンドのテスト (Issue #104)"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def write_pipeline(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_pipeline(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def make_pipeline(project: str, state: str = "IDLE", **kwargs) -> dict:
    data = {
        "project": project,
        "gitlab": f"testns/{project}",
        "state": state,
        "enabled": False,
        "implementer": "implementer1",
        "batch": [],
        "history": [],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    data.update(kwargs)
    return data


def reset_args(**kwargs) -> argparse.Namespace:
    defaults = {"dry_run": False, "force": True}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestCmdReset:

    def test_all_idle_no_change(self, tmp_pipelines, capsys):
        """全PJがIDLEの場合: 何も変更されず "All projects are already IDLE." が出力される"""
        path = tmp_pipelines / "pj-a.json"
        write_pipeline(path, make_pipeline("pj-a", state="IDLE"))

        from gokrax import cmd_reset
        cmd_reset(reset_args())

        out = capsys.readouterr().out
        assert "All projects are already IDLE." in out
        assert read_pipeline(path)["state"] == "IDLE"

    def test_single_non_idle_reset(self, tmp_pipelines, capsys):
        """非IDLEのPJが1つ: IDLEにリセットされる"""
        pipeline = make_pipeline(
            "pj-a", state="IMPLEMENTATION",
            enabled=True,
            batch=[{"issue": 1}],
            design_revise_count=2,
            code_revise_count=1,
            automerge=True,
            p2_fix=True,
            cc_plan_model="opus",
            cc_impl_model="sonnet",
            keep_context=True,
            keep_ctx_batch=True,
            keep_ctx_intra=True,
            comment="note",
            skip_cc_plan=True,
            skip_test=True,
        )
        path = tmp_pipelines / "pj-a.json"
        write_pipeline(path, pipeline)

        from gokrax import cmd_reset
        cmd_reset(reset_args())

        out = capsys.readouterr().out
        assert "[RESET] pj-a: IMPLEMENTATION → IDLE" in out

        saved = read_pipeline(path)
        assert saved["state"] == "IDLE"
        assert saved["batch"] == []
        assert saved["enabled"] is False
        for key in ("design_revise_count", "code_revise_count", "automerge", "p2_fix",
                    "cc_plan_model", "cc_impl_model", "keep_context",
                    "keep_ctx_batch", "keep_ctx_intra", "comment", "skip_cc_plan", "skip_test"):
            assert key not in saved, f"key {key!r} should be removed"

    def test_multiple_non_idle_all_reset(self, tmp_pipelines, capsys):
        """非IDLEのPJが複数: 全てリセットされる"""
        for pj, state in [("pj-a", "DESIGN_REVIEW"), ("pj-b", "IMPLEMENTATION"), ("pj-c", "BLOCKED")]:
            write_pipeline(tmp_pipelines / f"{pj}.json", make_pipeline(pj, state=state))

        from gokrax import cmd_reset
        cmd_reset(reset_args())

        out = capsys.readouterr().out
        assert "[RESET] pj-a: DESIGN_REVIEW → IDLE" in out
        assert "[RESET] pj-b: IMPLEMENTATION → IDLE" in out
        assert "[RESET] pj-c: BLOCKED → IDLE" in out
        assert "Reset 3 project(s) to IDLE." in out

        for pj in ("pj-a", "pj-b", "pj-c"):
            assert read_pipeline(tmp_pipelines / f"{pj}.json")["state"] == "IDLE"

    def test_dry_run_no_change(self, tmp_pipelines, capsys):
        """--dry-run: 表示されるが変更されない"""
        pipeline = make_pipeline("pj-a", state="DESIGN_REVIEW")
        path = tmp_pipelines / "pj-a.json"
        write_pipeline(path, pipeline)
        original_content = path.read_text()

        from gokrax import cmd_reset
        cmd_reset(reset_args(dry_run=True))

        out = capsys.readouterr().out
        assert "Projects to reset:" in out
        assert "pj-a (DESIGN_REVIEW)" in out
        assert path.read_text() == original_content

    def test_force_no_prompt(self, tmp_pipelines):
        """--force: input() が呼ばれない"""
        write_pipeline(tmp_pipelines / "pj-a.json", make_pipeline("pj-a", state="BLOCKED"))

        from gokrax import cmd_reset
        with patch("builtins.input") as mock_input:
            cmd_reset(reset_args(force=True))
        mock_input.assert_not_called()

    def test_confirm_n_aborts(self, tmp_pipelines, capsys):
        """確認プロンプトで n: 中止される"""
        pipeline = make_pipeline("pj-a", state="IMPLEMENTATION")
        path = tmp_pipelines / "pj-a.json"
        write_pipeline(path, pipeline)
        original_content = path.read_text()

        from gokrax import cmd_reset
        with patch("builtins.input", return_value="n"):
            cmd_reset(reset_args(force=False))

        assert path.read_text() == original_content
        out = capsys.readouterr().out
        assert "Aborted." in out

    def test_reset_to_idle_unit(self):
        """_reset_to_idle 単体: 全クリーンアップキーが除去される"""
        from gokrax import _reset_to_idle
        data = {
            "state": "IMPLEMENTATION",
            "batch": [{"issue": 1}],
            "enabled": True,
            "design_revise_count": 3,
            "code_revise_count": 2,
            "automerge": True,
            "p2_fix": True,
            "cc_plan_model": "opus",
            "cc_impl_model": "sonnet",
            "keep_context": True,
            "keep_ctx_batch": True,
            "keep_ctx_intra": True,
            "comment": "test",
            "skip_cc_plan": True,
            "skip_test": True,
            "_code_test": {"pid": 99999, "output_path": "/tmp/x", "exit_code_path": "/tmp/x.exit", "script_path": "/tmp/x.sh"},
            "test_result": "fail",
            "test_output": "some output",
            "test_retry_count": 2,
        }
        with patch("engine.cc._kill_pytest_baseline",
                    side_effect=lambda data, pj: data.pop("_pytest_baseline", None)), \
             patch("engine.cc._kill_code_test",
                    side_effect=lambda data, pj: data.pop("_code_test", None)) as mock_kill_ct, \
             patch("engine.reviewer._cleanup_review_files"), \
             patch("notify.cleanup_npass_files"):
            _reset_to_idle(data)
            mock_kill_ct.assert_called_once()

        assert data["batch"] == []
        assert data["enabled"] is False
        for key in ("design_revise_count", "code_revise_count", "automerge", "p2_fix",
                    "cc_plan_model", "cc_impl_model", "keep_context",
                    "keep_ctx_batch", "keep_ctx_intra", "comment", "skip_cc_plan", "skip_test"):
            assert key not in data, f"key {key!r} should be removed"
        for key in ("_code_test", "test_result", "test_output", "test_retry_count"):
            assert key not in data, f"key {key!r} should be removed"
        # state は変更しない（呼び出し側の責務）
        assert data["state"] == "IMPLEMENTATION"

    def test_spec_mode_project_included(self, tmp_pipelines, capsys):
        """spec_mode=True のPJもリセットされ、spec_mode/spec_configがクリアされる"""
        spec_pipeline = make_pipeline("pj-spec", state="SPEC_REVIEW", spec_mode=True, spec_config={"spec_path": "test.md"})
        spec_path = tmp_pipelines / "pj-spec.json"
        write_pipeline(spec_path, spec_pipeline)

        from gokrax import cmd_reset
        cmd_reset(reset_args())

        out = capsys.readouterr().out
        assert "[RESET] pj-spec: SPEC_REVIEW → IDLE" in out

        saved = read_pipeline(spec_path)
        assert saved["state"] == "IDLE"
        assert saved["spec_mode"] is False
        assert saved["spec_config"] == {}

    def test_spec_mode_mixed_with_normal(self, tmp_pipelines, capsys):
        """spec_mode PJと通常PJが混在: 両方ともリセットされる"""
        spec_path = tmp_pipelines / "pj-spec.json"
        normal_path = tmp_pipelines / "pj-normal.json"
        write_pipeline(spec_path, make_pipeline("pj-spec", state="SPEC_REVIEW", spec_mode=True, spec_config={}))
        write_pipeline(normal_path, make_pipeline("pj-normal", state="IMPLEMENTATION"))

        from gokrax import cmd_reset
        cmd_reset(reset_args())

        out = capsys.readouterr().out
        assert "[RESET] pj-normal: IMPLEMENTATION → IDLE" in out
        assert "[RESET] pj-spec: SPEC_REVIEW → IDLE" in out

        spec_saved = read_pipeline(spec_path)
        assert spec_saved["state"] == "IDLE"
        assert spec_saved["spec_mode"] is False
        assert spec_saved["spec_config"] == {}

        assert read_pipeline(normal_path)["state"] == "IDLE"

    def test_spec_only_resets_normally(self, tmp_pipelines, capsys):
        """spec_mode PJのみが非IDLEの場合: 通常通りリセットされる"""
        spec_path = tmp_pipelines / "pj-spec.json"
        write_pipeline(
            spec_path,
            make_pipeline("pj-spec", state="SPEC_REVIEW", spec_mode=True, spec_config={}),
        )

        from gokrax import cmd_reset
        cmd_reset(reset_args())

        out = capsys.readouterr().out
        assert "[RESET] pj-spec: SPEC_REVIEW → IDLE" in out

        saved = read_pipeline(spec_path)
        assert saved["state"] == "IDLE"
        assert saved["spec_mode"] is False
        assert saved["spec_config"] == {}

    def test_confirm_y_executes(self, tmp_pipelines, capsys):
        """確認プロンプトで y: 実行される"""
        write_pipeline(tmp_pipelines / "pj-a.json", make_pipeline("pj-a", state="BLOCKED"))

        from gokrax import cmd_reset
        with patch("builtins.input", return_value="y"):
            cmd_reset(reset_args(force=False))

        out = capsys.readouterr().out
        assert "[RESET] pj-a: BLOCKED → IDLE" in out
        assert read_pipeline(tmp_pipelines / "pj-a.json")["state"] == "IDLE"
