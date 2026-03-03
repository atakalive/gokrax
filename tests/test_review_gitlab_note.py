"""tests/test_review_gitlab_note.py — _post_gitlab_note() のリトライ・ログテスト"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_pipeline(tmp_pipelines, state="DESIGN_REVIEW"):
    """DESIGN_REVIEW 状態 + issue #1 入りのパイプラインを作成して返す。"""
    data = {
        "project": "test-pj",
        "gitlab": "atakalive/test-pj",
        "state": state,
        "enabled": True,
        "implementer": "kaneko",
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


class TestReviewGitlabNoteRetry:

    def test_retry_success_on_third_attempt(self, tmp_pipelines):
        """glab が2回失敗→3回目成功: subprocess.run が3回呼ばれ、pipeline に review が記録される。"""
        _make_pipeline(tmp_pipelines)

        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            m = MagicMock()
            if call_count < 3:
                m.returncode = 1
                m.stderr = "connection error"
            else:
                m.returncode = 0
                m.stderr = ""
            return m

        import devbar
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="APPROVE",
            summary="LGTM",
            force=False,
        )
        with patch("devbar.subprocess.run", side_effect=mock_run):
            with patch("devbar.time.sleep"):
                devbar.cmd_review(args)

        assert call_count == 3

        # pipeline JSON に review が記録されていること
        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        reviews = data["batch"][0]["design_reviews"]
        assert "pascal" in reviews
        assert reviews["pascal"]["verdict"] == "APPROVE"

    def test_all_fail_pipeline_updated_and_stderr_warning(self, tmp_pipelines, capsys):
        """3回全失敗: pipeline JSON には review が記録され、stderr に警告が出る。"""
        _make_pipeline(tmp_pipelines)

        fail_result = MagicMock()
        fail_result.returncode = 1
        fail_result.stderr = "server error"

        import devbar
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="leibniz",
            verdict="P1",
            summary="minor issue",
            force=False,
        )
        with patch("devbar.subprocess.run", return_value=fail_result):
            with patch("devbar.time.sleep"):
                devbar.cmd_review(args)

        # pipeline JSON には review が記録される（失敗でも）
        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        reviews = data["batch"][0]["design_reviews"]
        assert "leibniz" in reviews
        assert reviews["leibniz"]["verdict"] == "P1"

        # stderr に警告が出る
        captured = capsys.readouterr()
        assert "⚠" in captured.err
        assert "3 attempts" in captured.err


class TestReviewForce:

    def test_force_overwrites_existing_review(self, tmp_pipelines):
        """--force あり: 既存レビューが新しい verdict/at で上書きされる。"""
        _make_pipeline(tmp_pipelines)

        import devbar

        ok_result = MagicMock()
        ok_result.returncode = 0
        ok_result.stderr = ""

        # 1回目: pascal が P0 を投稿
        args1 = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="P0",
            summary="",
            force=False,
        )
        with patch("devbar.subprocess.run", return_value=ok_result):
            with patch("devbar.time.sleep"):
                with patch("devbar.now_iso", return_value="2026-01-01T00:00:00+09:00"):
                    devbar.cmd_review(args1)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"]["pascal"]["verdict"] == "P0"
        assert data["batch"][0]["design_reviews"]["pascal"]["at"] == "2026-01-01T00:00:00+09:00"

        # 2回目: pascal が APPROVE を --force で上書き
        args2 = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="APPROVE",
            summary="",
            force=True,
        )
        with patch("devbar.subprocess.run", return_value=ok_result):
            with patch("devbar.time.sleep"):
                with patch("devbar.now_iso", return_value="2026-01-01T01:00:00+09:00"):
                    devbar.cmd_review(args2)

        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"]["pascal"]["verdict"] == "APPROVE"
        assert data["batch"][0]["design_reviews"]["pascal"]["at"] == "2026-01-01T01:00:00+09:00"

    def test_without_force_skips_existing_review(self, tmp_pipelines):
        """--force なし: 既存レビューはスキップされ、GitLab note も投稿されない。"""
        _make_pipeline(tmp_pipelines)

        import devbar

        ok_result = MagicMock()
        ok_result.returncode = 0
        ok_result.stderr = ""

        # 1回目: pascal が P0 を投稿
        args1 = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="P0",
            summary="",
            force=False,
        )
        with patch("devbar.subprocess.run", return_value=ok_result):
            with patch("devbar.time.sleep"):
                devbar.cmd_review(args1)

        # 2回目: pascal が APPROVE を --force なしで投稿（スキップされるはず）
        call_count = 0

        def mock_run_2nd(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return ok_result

        args2 = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="APPROVE",
            summary="",
            force=False,
        )
        with patch("devbar.subprocess.run", side_effect=mock_run_2nd):
            with patch("devbar.time.sleep"):
                devbar.cmd_review(args2)

        # verdict は P0 のまま（上書きされていない）
        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"]["pascal"]["verdict"] == "P0"

        # スキップ時は GitLab note 投稿（subprocess.run）が呼ばれない
        assert call_count == 0

    def test_force_on_new_reviewer_works_normally(self, tmp_pipelines):
        """--force あり + 新規レビュアー: 通常通りレビューが記録される。"""
        _make_pipeline(tmp_pipelines)

        import devbar

        ok_result = MagicMock()
        ok_result.returncode = 0
        ok_result.stderr = ""

        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="APPROVE",
            summary="LGTM",
            force=True,
        )
        with patch("devbar.subprocess.run", return_value=ok_result):
            with patch("devbar.time.sleep"):
                devbar.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"]["pascal"]["verdict"] == "APPROVE"
