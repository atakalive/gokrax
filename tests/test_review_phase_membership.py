"""tests/test_review_phase_membership.py — フェーズメンバーシップ検証テスト (Issue #315)"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_pipeline(tmp_pipelines: Path, state: str = "DESIGN_REVIEW") -> Path:
    """指定状態 + issue #1 入りのパイプラインを作成して返す。"""
    data = {
        "project": "test-pj",
        "gitlab": "testns/test-pj",
        "state": state,
        "enabled": True,
        "implementer": "implementer1",
        "review_mode": "standard",
        "design_revise_count": 0,
        "code_revise_count": 0,
        "batch": [
            {
                "issue": 1,
                "title": "Test Issue",
                "commit": None,
                "cc_session_id": None,
                "design_reviews": {},
                "code_reviews": {},
                "added_at": "2025-01-01T00:00:00+09:00",
            }
        ],
        "history": [],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    path = tmp_pipelines / "test-pj.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path


class TestPhaseMembership:

    def test_member_reviewer_accepted(self, tmp_pipelines: Path) -> None:
        """REVIEW 状態 + フェーズ内レビュアー + --phase 指定 → 正常にレビュー記録"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVIEW")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        import gokrax
        args = argparse.Namespace(
            project="test-pj", issue=1, reviewer="reviewer1",
            verdict="APPROVE", summary="LGTM", force=False,
            round=1, phase="design",
        )
        with patch("commands.dev.subprocess.run", return_value=mock_result):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        reviews = data["batch"][0]["design_reviews"]
        assert "reviewer1" in reviews
        assert reviews["reviewer1"]["verdict"] == "APPROVE"

    def test_non_member_reviewer_discarded(self, tmp_pipelines: Path, capsys) -> None:
        """REVIEW 状態 + フェーズ外レビュアー + --phase 指定 → 破棄"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVIEW")

        # reviewer2 is not in standard mode members (reviewer1, reviewer3, reviewer6)
        import gokrax
        args = argparse.Namespace(
            project="test-pj", issue=1, reviewer="reviewer2",
            verdict="APPROVE", summary="LGTM", force=False,
            round=1, phase="design",
        )
        with patch("commands.dev.subprocess.run"):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"] == {}

        captured = capsys.readouterr()
        assert "not a design phase member" in captured.out

    def test_phase_omitted_discards_before_membership(self, tmp_pipelines: Path, capsys) -> None:
        """REVIEW 状態 + --phase 省略 → --phase 検証で先に破棄（メンバーシップ未到達）"""
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW")

        import gokrax
        args = argparse.Namespace(
            project="test-pj", issue=1, reviewer="reviewer2",
            verdict="APPROVE", summary="LGTM", force=False,
            round=1, phase=None,
        )
        with patch("commands.dev.subprocess.run"):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        captured = capsys.readouterr()
        assert "--phase not specified" in captured.out

    def test_phase_mismatch_discards_before_membership(self, tmp_pipelines: Path, capsys) -> None:
        """REVIEW 状態 + --phase 不一致 → phase mismatch で先に破棄"""
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW")

        import gokrax
        args = argparse.Namespace(
            project="test-pj", issue=1, reviewer="reviewer2",
            verdict="APPROVE", summary="LGTM", force=False,
            round=1, phase="design",
        )
        with patch("commands.dev.subprocess.run"):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        captured = capsys.readouterr()
        assert "phase mismatch" in captured.out
