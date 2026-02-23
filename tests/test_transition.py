"""tests/test_transition.py — cmd_transition --force フラグテスト"""

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
        args = argparse.Namespace(project="test-pj", to="CODE_REVIEW", actor="cli", force=True)
        with patch("devbar.notify_implementer"), patch("devbar.notify_reviewers"):
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
        args = argparse.Namespace(project="test-pj", to="IDLE", actor="cli", force=False)
        with patch("devbar.notify_implementer"), patch("devbar.notify_reviewers"):
            cmd_transition(args)
        with open(path) as f:
            data = json.load(f)
        assert data["state"] == "IDLE"
        assert data["batch"] == []
        assert data["enabled"] is False


class TestTransitionNotifications:
    """cmd_transition の通知ロジックのテスト（Issue #16）"""

    _BATCH_ITEM = {
        "issue": 1, "title": "T", "commit": None, "cc_session_id": None,
        "design_reviews": {}, "code_reviews": {},
        "added_at": "2025-01-01T00:00:00+09:00",
    }

    def test_normal_transition_sends_notification(self, tmp_pipelines, sample_pipeline):
        """DESIGN_APPROVEDは自動遷移のみ（impl_msgなし）で通知は飛ばない"""
        sample_pipeline["state"] = "DESIGN_REVIEW"
        sample_pipeline["batch"] = [dict(self._BATCH_ITEM)]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_APPROVED", actor="cli", force=False, resume=False,
        )
        with patch("devbar.notify_implementer") as mock_impl, \
             patch("devbar.notify_reviewers") as mock_rev:
            cmd_transition(args)
        mock_impl.assert_not_called()
        mock_rev.assert_not_called()

    def test_force_transition_sends_notification(self, tmp_pipelines, sample_pipeline):
        """--force 遷移（IDLE→IMPLEMENTATION）: run_cc=True なので impl_msg なし（CC直接実行）"""
        path = tmp_pipelines / "test-pj.json"
        sample_pipeline["batch"] = [dict(self._BATCH_ITEM)]
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="IMPLEMENTATION", actor="cli", force=True, resume=False,
        )
        with patch("devbar.notify_implementer") as mock_impl, \
             patch("devbar.notify_reviewers") as mock_rev:
            cmd_transition(args)
        # IMPLEMENTATION は run_cc=True で impl_msg なし → notify_implementer は呼ばれない
        mock_impl.assert_not_called()
        mock_rev.assert_not_called()

    def test_resume_transition_to_design_plan_sends_notification(self, tmp_pipelines, sample_pipeline):
        """--resume 遷移（→DESIGN_PLAN）で notify_implementer が呼ばれ「（再開）」プレフィックスが含まれる"""
        path = tmp_pipelines / "test-pj.json"
        sample_pipeline["batch"] = [dict(self._BATCH_ITEM)]
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_PLAN", actor="cli", force=False, resume=True,
        )
        with patch("devbar.notify_implementer") as mock_impl, \
             patch("devbar.notify_reviewers"):
            cmd_transition(args)
        mock_impl.assert_called_once()
        call_msg = mock_impl.call_args[0][1]
        assert "（再開）" in call_msg
        assert "設計確認フェーズ" in call_msg

    def test_resume_skips_validation(self, tmp_pipelines, sample_pipeline):
        """--resume は --force と同様にバリデーションをスキップする"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        # IDLE → CODE_REVIEW は通常遷移では不正
        args = argparse.Namespace(
            project="test-pj", to="CODE_REVIEW", actor="cli", force=False, resume=True,
        )
        with patch("devbar.notify_implementer"), patch("devbar.notify_reviewers"):
            cmd_transition(args)
        with open(path) as f:
            assert json.load(f)["state"] == "CODE_REVIEW"

    def test_design_plan_notifies_implementer(self, tmp_pipelines, sample_pipeline):
        """DESIGN_PLAN 遷移では実装担当に通知が送られる"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_PLAN", actor="cli", force=False, resume=False,
        )
        with patch("devbar.notify_implementer") as mock_impl, \
             patch("devbar.notify_reviewers") as mock_rev, \
             patch("devbar.notify_discord"):
            cmd_transition(args)
        mock_impl.assert_called_once()
        mock_rev.assert_not_called()

    def test_transition_notifies_discord(self, tmp_pipelines, sample_pipeline):
        """通常遷移で notify_discord が [pj] current → target (by actor) 形式で呼ばれること"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_PLAN", actor="cli", force=False, resume=False,
        )
        with patch("devbar.notify_implementer"), \
             patch("devbar.notify_reviewers"), \
             patch("devbar.notify_discord") as mock_discord:
            cmd_transition(args)
        mock_discord.assert_called_once()
        msg = mock_discord.call_args[0][0]
        assert "[test-pj]" in msg
        assert "IDLE" in msg
        assert "DESIGN_PLAN" in msg
        assert "by cli" in msg
        assert "（再開）" not in msg

    def test_force_notifies_discord(self, tmp_pipelines, sample_pipeline):
        """--force 遷移でも notify_discord が呼ばれること"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="CODE_REVIEW", actor="M", force=True, resume=False,
        )
        with patch("devbar.notify_implementer"), \
             patch("devbar.notify_reviewers"), \
             patch("devbar.notify_discord") as mock_discord:
            cmd_transition(args)
        mock_discord.assert_called_once()
        msg = mock_discord.call_args[0][0]
        assert "CODE_REVIEW" in msg
        assert "by M" in msg

    def test_resume_notifies_discord_with_prefix(self, tmp_pipelines, sample_pipeline):
        """--resume 遷移で通知文に「（再開）」が含まれること"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from devbar import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_PLAN", actor="cli", force=False, resume=True,
        )
        with patch("devbar.notify_implementer"), \
             patch("devbar.notify_reviewers"), \
             patch("devbar.notify_discord") as mock_discord:
            cmd_transition(args)
        mock_discord.assert_called_once()
        msg = mock_discord.call_args[0][0]
        assert "（再開）" in msg
        assert "DESIGN_PLAN" in msg
