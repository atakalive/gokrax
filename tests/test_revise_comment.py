"""tests/test_revise_comment.py — cmd_design_revise() / cmd_code_revise() のテスト"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_pipeline(tmp_pipelines, state="DESIGN_REVISE"):
    """指定state + issue #1 入りのパイプラインを作成して返す。"""
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
                "commit": "abc123",
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


class TestDesignRevise:

    def test_design_revise_with_comment(self, tmp_pipelines):
        """--comment あり + glab 成功 → design_revised が True に設定される。"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVISE")

        import devbar
        args = argparse.Namespace(project="test-pj", issue=1, comment="修正完了")
        with patch("devbar._post_gitlab_note", return_value=True):
            devbar.cmd_design_revise(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0].get("design_revised") is True

    def test_design_revise_comment_glab_fail(self, tmp_pipelines):
        """--comment あり + glab 失敗 → SystemExit(1) が上がり、design_revised は未設定。"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVISE")

        import devbar
        args = argparse.Namespace(project="test-pj", issue=1, comment="修正完了")
        with patch("devbar._post_gitlab_note", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                devbar.cmd_design_revise(args)

        assert exc_info.value.code == 1

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert "design_revised" not in data["batch"][0]

    def test_design_revise_without_comment(self, tmp_pipelines):
        """--comment なし → glab 投稿なし、design_revised が True に設定される。"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVISE")

        import devbar
        args = argparse.Namespace(project="test-pj", issue=1, comment=None)
        with patch("devbar._post_gitlab_note") as mock_post:
            devbar.cmd_design_revise(args)
            mock_post.assert_not_called()

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0].get("design_revised") is True

    def test_design_revise_wrong_state(self, tmp_pipelines):
        """CODE_REVISE 状態で design-revise → SystemExit。"""
        _make_pipeline(tmp_pipelines, state="CODE_REVISE")

        import devbar
        args = argparse.Namespace(project="test-pj", issue=1, comment=None)
        with pytest.raises(SystemExit):
            devbar.cmd_design_revise(args)


class TestCodeRevise:

    def test_code_revise_sets_commit_and_flag(self, tmp_pipelines):
        """CODE_REVISE 状態 + --hash → commit 更新 + code_revised=True。"""
        _make_pipeline(tmp_pipelines, state="CODE_REVISE")

        import devbar
        args = argparse.Namespace(project="test-pj", issue=1, hash="deadbeef", comment=None)
        devbar.cmd_code_revise(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["commit"] == "deadbeef"
        assert data["batch"][0].get("code_revised") is True

    def test_code_revise_with_comment(self, tmp_pipelines):
        """--comment あり + glab 成功 → commit 更新 + code_revised=True。"""
        _make_pipeline(tmp_pipelines, state="CODE_REVISE")

        import devbar
        args = argparse.Namespace(project="test-pj", issue=1, hash="deadbeef", comment="修正完了")
        with patch("devbar._post_gitlab_note", return_value=True):
            devbar.cmd_code_revise(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["commit"] == "deadbeef"
        assert data["batch"][0].get("code_revised") is True

    def test_code_revise_comment_glab_fail(self, tmp_pipelines):
        """--comment あり + glab 失敗 → SystemExit(1) が上がり、code_revised は未設定。"""
        _make_pipeline(tmp_pipelines, state="CODE_REVISE")

        import devbar
        args = argparse.Namespace(project="test-pj", issue=1, hash="deadbeef", comment="修正完了")
        with patch("devbar._post_gitlab_note", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                devbar.cmd_code_revise(args)

        assert exc_info.value.code == 1

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert "code_revised" not in data["batch"][0]

    def test_code_revise_wrong_state(self, tmp_pipelines):
        """DESIGN_REVISE 状態で code-revise → SystemExit。"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVISE")

        import devbar
        args = argparse.Namespace(project="test-pj", issue=1, hash="deadbeef", comment=None)
        with pytest.raises(SystemExit):
            devbar.cmd_code_revise(args)
