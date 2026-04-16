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
        "gitlab": "testns/test-pj",
        "state": state,
        "enabled": True,
        "implementer": "implementer1",
        "review_mode": "standard",
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
        cmd = review_command("test-pj", 1, "reviewer1", round_num=2)
        assert "--round 2" in cmd

    def test_review_command_without_round(self):
        """round_num 省略 → 出力文字列に --round が含まれない"""
        from notify import review_command
        cmd = review_command("test-pj", 1, "reviewer1")
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
            reviewer="reviewer1",
            verdict="APPROVE",
            summary="LGTM",
            force=False,
            round=1,
            phase="design",
        )
        with patch("commands.dev.subprocess.run", return_value=mock_result):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        reviews = data["batch"][0]["design_reviews"]
        assert "reviewer1" in reviews
        assert reviews["reviewer1"]["verdict"] == "APPROVE"

    def test_round_mismatch_rejected(self, tmp_pipelines):
        """--round が現在のラウンドと不一致 → SystemExit、design_reviews は空のまま"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVIEW", design_revise_count=0)

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="reviewer1",
            verdict="APPROVE",
            summary="LGTM",
            force=False,
            round=2,  # 現在は round 1 なのに round 2 を指定
            phase="design",
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
            reviewer="reviewer1",
            verdict="P1",
            summary="minor issue",
            force=False,
            round=None,  # 省略
            phase="design",
        )
        with patch("commands.dev.subprocess.run", return_value=mock_result):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        reviews = data["batch"][0]["design_reviews"]
        assert "reviewer1" in reviews
        assert reviews["reviewer1"]["verdict"] == "P1"


class TestReviewCommandPhase:

    def test_review_command_includes_phase(self):
        """phase="code" 指定 → 出力文字列に --phase code が含まれる"""
        from notify import review_command
        cmd = review_command("test-pj", 1, "reviewer1", round_num=1, phase="code")
        assert "--phase code" in cmd

    def test_review_command_phase_none(self):
        """phase=None → 出力文字列に --phase が含まれない"""
        from notify import review_command
        cmd = review_command("test-pj", 1, "reviewer1", phase=None)
        assert "--phase" not in cmd


class TestPhaseValidation:

    def test_phase_mismatch_discards_review(self, tmp_pipelines):
        """CODE_REVIEW + --phase design → 破棄"""
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW")

        import gokrax
        args = argparse.Namespace(
            project="test-pj", issue=1, reviewer="reviewer1",
            verdict="APPROVE", summary="LGTM", force=False,
            round=1, phase="design",
        )
        with patch("commands.dev.subprocess.run"):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["code_reviews"] == {}

    def test_phase_match_accepts_review(self, tmp_pipelines):
        """CODE_REVIEW + --phase code → 正常受理"""
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        import gokrax
        args = argparse.Namespace(
            project="test-pj", issue=1, reviewer="reviewer1",
            verdict="APPROVE", summary="LGTM", force=False,
            round=1, phase="code",
        )
        with patch("commands.dev.subprocess.run", return_value=mock_result):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        reviews = data["batch"][0]["code_reviews"]
        assert "reviewer1" in reviews
        assert reviews["reviewer1"]["verdict"] == "APPROVE"

    def test_phase_omitted_discards_review(self, tmp_pipelines):
        """CODE_REVIEW + --phase 省略 → 旧コマンドとして破棄"""
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW")

        import gokrax
        args = argparse.Namespace(
            project="test-pj", issue=1, reviewer="reviewer1",
            verdict="APPROVE", summary="LGTM", force=False,
            round=1, phase=None,
        )
        with patch("commands.dev.subprocess.run"):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["code_reviews"] == {}

    def test_phase_design_mismatch_code_review(self, tmp_pipelines):
        """DESIGN_REVIEW + --phase code → 破棄"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVIEW")

        import gokrax
        args = argparse.Namespace(
            project="test-pj", issue=1, reviewer="reviewer1",
            verdict="APPROVE", summary="LGTM", force=False,
            round=1, phase="code",
        )
        with patch("commands.dev.subprocess.run"):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"] == {}

    def test_phase_npass_match(self, tmp_pipelines):
        """CODE_REVIEW_NPASS + --phase code → 正常受理"""
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW_NPASS")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        import gokrax
        args = argparse.Namespace(
            project="test-pj", issue=1, reviewer="reviewer1",
            verdict="APPROVE", summary="LGTM", force=False,
            round=1, phase="code",
        )
        with patch("commands.dev.subprocess.run", return_value=mock_result):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        reviews = data["batch"][0]["code_reviews"]
        assert "reviewer1" in reviews

    def test_phase_npass_mismatch(self, tmp_pipelines):
        """DESIGN_REVIEW_NPASS + --phase code → 破棄"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVIEW_NPASS")

        import gokrax
        args = argparse.Namespace(
            project="test-pj", issue=1, reviewer="reviewer1",
            verdict="APPROVE", summary="LGTM", force=False,
            round=1, phase="code",
        )
        with patch("commands.dev.subprocess.run"):
            with patch("commands.dev.time.sleep"):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"] == {}
