"""tests/test_round_validation.py — レビューラウンド番号によるstaleレビュー拒否機構テスト (Issue #117)"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_pipeline(tmp_pipelines, state="DESIGN_REVIEW", design_revise_count=0, code_revise_count=0):
    """指定状態 + issue #1 入りのパイプラインを作成して返す。"""
    data = {
        "project": "test-pj",
        "gitlab": "atakalive/test-pj",
        "state": state,
        "enabled": True,
        "implementer": "kaneko",
        "design_revise_count": design_revise_count,
        "code_revise_count": code_revise_count,
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


class TestGetCurrentRound:

    def test_get_current_round_design(self):
        """DESIGN_REVIEW 状態 + design_revise_count=2 → 3 を返す"""
        from pipeline_io import get_current_round
        data = {"state": "DESIGN_REVIEW", "design_revise_count": 2}
        assert get_current_round(data) == 3

    def test_get_current_round_code(self):
        """CODE_REVIEW 状態 + code_revise_count=0 → 1 を返す"""
        from pipeline_io import get_current_round
        data = {"state": "CODE_REVIEW", "code_revise_count": 0}
        assert get_current_round(data) == 1

    def test_get_current_round_idle(self):
        """IDLE 状態 → 0 を返す"""
        from pipeline_io import get_current_round
        data = {"state": "IDLE"}
        assert get_current_round(data) == 0


class TestReviewCommand:

    def test_review_command_with_round(self):
        """round_num=2 指定 → 出力文字列に --round 2 が含まれる"""
        from notify import review_command
        cmd = review_command("test-pj", 1, "pascal", round_num=2)
        assert "--round 2" in cmd

    def test_review_command_without_round(self):
        """round_num 省略 → 出力文字列に --round が含まれない"""
        from notify import review_command
        cmd = review_command("test-pj", 1, "pascal")
        assert "--round" not in cmd


class TestRoundValidation:

    def test_round_match_accepted(self, tmp_pipelines):
        """--round が現在のラウンドと一致 → 正常受理（レビューが design_reviews に記録される）"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVIEW", design_revise_count=0)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="APPROVE",
            summary="LGTM",
            force=False,
            round=1,
        )
        with patch("commands.dev.subprocess.run", return_value=mock_result):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        reviews = data["batch"][0]["design_reviews"]
        assert "pascal" in reviews
        assert reviews["pascal"]["verdict"] == "APPROVE"

    def test_round_mismatch_rejected(self, tmp_pipelines):
        """--round が現在のラウンドと不一致 → SystemExit、design_reviews は空のまま"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVIEW", design_revise_count=0)

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="APPROVE",
            summary="LGTM",
            force=False,
            round=2,  # 現在は round 1 なのに round 2 を指定
        )
        with pytest.raises(SystemExit) as exc_info:
            gokrax.cmd_review(args)

        assert "Round mismatch" in str(exc_info.value)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        reviews = data["batch"][0]["design_reviews"]
        assert reviews == {}

    def test_round_omitted_accepted(self, tmp_pipelines):
        """--round 省略 → 後方互換で正常受理"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVIEW", design_revise_count=1)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="P1",
            summary="minor issue",
            force=False,
            round=None,  # 省略
        )
        with patch("commands.dev.subprocess.run", return_value=mock_result):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        reviews = data["batch"][0]["design_reviews"]
        assert "pascal" in reviews
        assert reviews["pascal"]["verdict"] == "P1"
