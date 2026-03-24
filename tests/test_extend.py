"""tests/test_extend.py — cmd_extend() テスト"""

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


class TestCmdExtend:

    def test_extend_adds_timeout_extension(self, tmp_pipelines, sample_pipeline):
        """cmd_extend() でtimeout_extensionが加算されること"""
        sample_pipeline["state"] = "DESIGN_PLAN"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        cmd_extend(args)

        with open(path) as f:
            data = json.load(f)
        assert data["timeout_extension"] == 600

    def test_extend_cumulative(self, tmp_pipelines, sample_pipeline):
        """複数回のextendで累積加算されること"""
        sample_pipeline["state"] = "IMPLEMENTATION"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse

        # 1回目: +600
        args = argparse.Namespace(project="test-pj", by=600)
        cmd_extend(args)

        # 2回目: +300
        args = argparse.Namespace(project="test-pj", by=300)
        cmd_extend(args)

        with open(path) as f:
            data = json.load(f)
        assert data["timeout_extension"] == 900

    def test_extend_invalid_state_idle(self, tmp_pipelines, sample_pipeline):
        """IDLE状態でextendするとSystemExit"""
        sample_pipeline["state"] = "IDLE"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        with pytest.raises(SystemExit, match="延長不可"):
            cmd_extend(args)

    def test_extend_invalid_state_review(self, tmp_pipelines, sample_pipeline):
        """DESIGN_REVIEW状態でextendするとSystemExit"""
        sample_pipeline["state"] = "DESIGN_REVIEW"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        with pytest.raises(SystemExit, match="延長不可"):
            cmd_extend(args)

    def test_extend_design_plan(self, tmp_pipelines, sample_pipeline):
        """DESIGN_PLAN状態でextendが成功すること"""
        sample_pipeline["state"] = "DESIGN_PLAN"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        cmd_extend(args)  # Should not raise

        with open(path) as f:
            data = json.load(f)
        assert data["timeout_extension"] == 600

    def test_extend_design_revise(self, tmp_pipelines, sample_pipeline):
        """DESIGN_REVISE状態でextendが成功すること"""
        sample_pipeline["state"] = "DESIGN_REVISE"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        cmd_extend(args)  # Should not raise

        with open(path) as f:
            data = json.load(f)
        assert data["timeout_extension"] == 600

    def test_extend_code_revise(self, tmp_pipelines, sample_pipeline):
        """CODE_REVISE状態でextendが成功すること"""
        sample_pipeline["state"] = "CODE_REVISE"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        cmd_extend(args)  # Should not raise

        with open(path) as f:
            data = json.load(f)
        assert data["timeout_extension"] == 600

    def test_extend_implementation(self, tmp_pipelines, sample_pipeline):
        """IMPLEMENTATION状態でextendが成功すること"""
        sample_pipeline["state"] = "IMPLEMENTATION"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        cmd_extend(args)  # Should not raise

        with open(path) as f:
            data = json.load(f)
        assert data["timeout_extension"] == 600

    def test_extend_discord_notification(self, tmp_pipelines, sample_pipeline):
        """Discord通知が送信されること"""
        sample_pipeline["state"] = "DESIGN_PLAN"
        sample_pipeline["implementer"] = "implementer1"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        with patch("commands.dev.notify_discord") as mock_discord:
            cmd_extend(args)

        mock_discord.assert_called_once()
        call_args = mock_discord.call_args[0][0]
        assert "test-pj" in call_args
        assert "implementer1" in call_args
        assert "600秒延長" in call_args
        assert "累計+600秒" in call_args
