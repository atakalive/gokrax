"""tests/test_triage.py — cmd_triage 複数Issue対応テスト"""

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


class TestTriageMultiIssue:

    def test_multiple_issues_added(self, tmp_pipelines, sample_pipeline):
        """複数Issue一括投入 → 全件バッチに追加されること"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_triage
        import argparse
        args = argparse.Namespace(project="test-pj", issue=[35, 36, 37], title=[])
        cmd_triage(args)
        with open(path) as f:
            data = json.load(f)
        assert len(data["batch"]) == 3
        assert data["batch"][0]["issue"] == 35
        assert data["batch"][1]["issue"] == 36
        assert data["batch"][2]["issue"] == 37

    def test_batch_overflow(self, tmp_pipelines, sample_pipeline):
        """バッチ上限超過（既存3件 + 新規3件、MAX_BATCH=5）→ SystemExit"""
        sample_pipeline["batch"] = [
            {"issue": i, "title": "", "commit": None, "cc_session_id": None,
             "design_reviews": {}, "code_reviews": {}, "added_at": ""}
            for i in range(3)
        ]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_triage
        import argparse
        args = argparse.Namespace(project="test-pj", issue=[10, 11, 12], title=[])
        with pytest.raises(SystemExit, match="Batch overflow"):
            cmd_triage(args)

    def test_duplicate_in_existing_batch(self, tmp_pipelines, sample_pipeline):
        """既存バッチにある Issue を指定 → SystemExit"""
        sample_pipeline["batch"] = [
            {"issue": 42, "title": "", "commit": None, "cc_session_id": None,
             "design_reviews": {}, "code_reviews": {}, "added_at": ""}
        ]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_triage
        import argparse
        args = argparse.Namespace(project="test-pj", issue=[42, 43], title=[])
        with pytest.raises(SystemExit, match="already in batch"):
            cmd_triage(args)

    def test_title_omitted_defaults_to_empty(self, tmp_pipelines, sample_pipeline):
        """--title 省略時 → 空文字がセットされること"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_triage
        import argparse
        args = argparse.Namespace(project="test-pj", issue=[1, 2], title=[])
        cmd_triage(args)
        with open(path) as f:
            data = json.load(f)
        assert data["batch"][0]["title"] == ""
        assert data["batch"][1]["title"] == ""

    def test_title_fewer_than_issues_padded(self, tmp_pipelines, sample_pipeline):
        """--title が --issue より少ない → 不足分は空文字"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_triage
        import argparse
        args = argparse.Namespace(project="test-pj", issue=[1, 2, 3], title=["機能A"])
        cmd_triage(args)
        with open(path) as f:
            data = json.load(f)
        assert data["batch"][0]["title"] == "機能A"
        assert data["batch"][1]["title"] == ""
        assert data["batch"][2]["title"] == ""

    def test_invalid_state_rejected(self, tmp_pipelines, sample_pipeline):
        """不許可状態(IMPLEMENTATION) → SystemExit"""
        sample_pipeline["state"] = "IMPLEMENTATION"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_triage
        import argparse
        args = argparse.Namespace(project="test-pj", issue=[1], title=[])
        with pytest.raises(SystemExit, match="Cannot add issues"):
            cmd_triage(args)
