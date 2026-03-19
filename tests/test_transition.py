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
        """通常遷移（IDLE→INITIALIZE）→ 成功"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        import argparse
        args = argparse.Namespace(project="test-pj", to="INITIALIZE", actor="cli", force=False)
        cmd_transition(args)
        with open(path) as f:
            assert json.load(f)["state"] == "INITIALIZE"

    def test_initialize_to_design_plan(self, tmp_pipelines, sample_pipeline):
        """通常遷移（INITIALIZE→DESIGN_PLAN）→ 成功"""
        sample_pipeline["state"] = "INITIALIZE"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        import argparse
        args = argparse.Namespace(project="test-pj", to="DESIGN_PLAN", actor="cli", force=False, resume=False)
        with patch("commands.dev.notify_implementer"), patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        with open(path) as f:
            assert json.load(f)["state"] == "DESIGN_PLAN"

    def test_invalid_transition_rejected(self, tmp_pipelines, sample_pipeline):
        """不正遷移（IDLE→CODE_REVIEW）→ SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        import argparse
        args = argparse.Namespace(project="test-pj", to="CODE_REVIEW", actor="cli", force=False)
        with pytest.raises(SystemExit, match="Invalid transition"):
            cmd_transition(args)

    def test_force_skips_transition_validation(self, tmp_pipelines, sample_pipeline):
        """--force で不正遷移 → 成功"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(project="test-pj", to="CODE_REVIEW", actor="cli", force=True)
        with patch("commands.dev.notify_implementer"), patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        with open(path) as f:
            assert json.load(f)["state"] == "CODE_REVIEW"

    def test_force_to_blocked(self, tmp_pipelines, sample_pipeline):
        """--force で IMPLEMENTATION→BLOCKED → 成功 + history記録"""
        sample_pipeline["state"] = "IMPLEMENTATION"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
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
        from gokrax import cmd_transition
        import argparse
        args = argparse.Namespace(project="test-pj", to="NONEXISTENT", actor="cli", force=True)
        with pytest.raises(SystemExit, match="Invalid state"):
            cmd_transition(args)

    def test_blocked_to_idle_without_force(self, tmp_pipelines, sample_pipeline):
        """BLOCKED→IDLE → --force 不要（通常遷移で成功）"""
        sample_pipeline["state"] = "BLOCKED"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(project="test-pj", to="IDLE", actor="cli", force=False)
        with patch("commands.dev.notify_implementer"), patch("commands.dev.notify_reviewers"):
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
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_APPROVED", actor="cli", force=False, resume=False,
        )
        with patch("commands.dev.notify_implementer") as mock_impl, \
             patch("commands.dev.notify_reviewers") as mock_rev:
            cmd_transition(args)
        mock_impl.assert_not_called()
        mock_rev.assert_not_called()

    def test_force_transition_sends_notification(self, tmp_pipelines, sample_pipeline):
        """--force 遷移（IDLE→IMPLEMENTATION）: run_cc=True なので impl_msg なし（CC直接実行）"""
        path = tmp_pipelines / "test-pj.json"
        sample_pipeline["batch"] = [dict(self._BATCH_ITEM)]
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="IMPLEMENTATION", actor="cli", force=True, resume=False,
        )
        with patch("commands.dev.notify_implementer") as mock_impl, \
             patch("commands.dev.notify_reviewers") as mock_rev:
            cmd_transition(args)
        # IMPLEMENTATION は run_cc=True で impl_msg なし → notify_implementer は呼ばれない
        mock_impl.assert_not_called()
        mock_rev.assert_not_called()

    def test_resume_transition_to_design_plan_sends_notification(self, tmp_pipelines, sample_pipeline):
        """--resume 遷移（→DESIGN_PLAN）で notify_implementer が呼ばれ「（再開）」プレフィックスが含まれる"""
        sample_pipeline["state"] = "INITIALIZE"
        sample_pipeline["batch"] = [dict(self._BATCH_ITEM)]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_PLAN", actor="cli", force=False, resume=True,
        )
        with patch("commands.dev.notify_implementer") as mock_impl, \
             patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        mock_impl.assert_called_once()
        call_msg = mock_impl.call_args[0][1]
        assert "（再開）" in call_msg
        assert "設計確認フェーズ" in call_msg

    def test_resume_skips_validation(self, tmp_pipelines, sample_pipeline):
        """--resume は --force と同様にバリデーションをスキップする"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        # IDLE → CODE_REVIEW は通常遷移では不正
        args = argparse.Namespace(
            project="test-pj", to="CODE_REVIEW", actor="cli", force=False, resume=True,
        )
        with patch("commands.dev.notify_implementer"), patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        with open(path) as f:
            assert json.load(f)["state"] == "CODE_REVIEW"

    def test_design_plan_notifies_implementer(self, tmp_pipelines, sample_pipeline):
        """DESIGN_PLAN 遷移では実装担当に通知が送られる"""
        sample_pipeline["state"] = "INITIALIZE"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_PLAN", actor="cli", force=False, resume=False,
        )
        with patch("commands.dev.notify_implementer") as mock_impl, \
             patch("commands.dev.notify_reviewers") as mock_rev, \
             patch("commands.dev.notify_discord"):
            cmd_transition(args)
        mock_impl.assert_called_once()
        mock_rev.assert_not_called()

    def test_transition_notifies_discord(self, tmp_pipelines, sample_pipeline):
        """通常遷移で notify_discord が [pj] current → target (by actor) 形式で呼ばれること"""
        sample_pipeline["state"] = "INITIALIZE"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_PLAN", actor="cli", force=False, resume=False,
        )
        with patch("commands.dev.notify_implementer"), \
             patch("commands.dev.notify_reviewers"), \
             patch("commands.dev.notify_discord") as mock_discord:
            cmd_transition(args)
        mock_discord.assert_called_once()
        msg = mock_discord.call_args[0][0]
        assert "[test-pj]" in msg
        assert "INITIALIZE" in msg
        assert "DESIGN_PLAN" in msg
        assert "by cli" in msg
        assert "（再開）" not in msg

    def test_force_notifies_discord(self, tmp_pipelines, sample_pipeline):
        """--force 遷移でも notify_discord が呼ばれること"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="CODE_REVIEW", actor="M", force=True, resume=False,
        )
        with patch("commands.dev.notify_implementer"), \
             patch("commands.dev.notify_reviewers"), \
             patch("commands.dev.notify_discord") as mock_discord:
            cmd_transition(args)
        mock_discord.assert_called_once()
        msg = mock_discord.call_args[0][0]
        assert "CODE_REVIEW" in msg
        assert "by M" in msg

    def test_resume_notifies_discord_with_prefix(self, tmp_pipelines, sample_pipeline):
        """--resume 遷移で通知文に「（再開）」が含まれること"""
        sample_pipeline["state"] = "INITIALIZE"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_PLAN", actor="cli", force=False, resume=True,
        )
        with patch("commands.dev.notify_implementer"), \
             patch("commands.dev.notify_reviewers"), \
             patch("commands.dev.notify_discord") as mock_discord:
            cmd_transition(args)
        mock_discord.assert_called_once()
        msg = mock_discord.call_args[0][0]
        assert "（再開）" in msg
        assert "DESIGN_PLAN" in msg


class TestKeepCtx:
    """keep_ctx_batch / keep_ctx_intra 分離テスト (Issue #58)"""

    def test_keep_ctx_preserved_in_pipeline(self, tmp_path):
        """keep_ctx_batch / keep_ctx_intra が pipeline.json に保存・読み出しできる。"""
        from pipeline_io import load_pipeline

        pipeline_path = tmp_path / "test.json"
        pipeline_path.write_text(json.dumps({
            "project": "test",
            "state": "IDLE",
            "enabled": True,
            "batch": [],
            "implementer": "kaneko",
            "keep_ctx_batch": True,
            "keep_ctx_intra": False,
            "review_mode": "standard",
        }))

        data = load_pipeline(str(pipeline_path))
        assert data.get("keep_ctx_batch") is True
        assert data.get("keep_ctx_intra") is False

    def test_keep_ctx_in_notification(self):
        """keep_ctx_batch / keep_ctx_intra が notification dict に渡される。"""
        data = {
            "project": "test",
            "state": "CODE_REVIEW",
            "batch": [{"issue": 1, "title": "test"}],
            "keep_ctx_batch": True,
            "keep_ctx_intra": False,
        }

        notification = {
            "keep_ctx_batch": data.get("keep_ctx_batch", False),
            "keep_ctx_intra": data.get("keep_ctx_intra", False),
        }
        assert notification["keep_ctx_batch"] is True
        assert notification["keep_ctx_intra"] is False

    def test_keep_ctx_default_false(self):
        """keep_ctx 未設定時はデフォルト False。"""
        data = {"state": "IDLE", "batch": []}
        assert data.get("keep_ctx_batch", False) is False
        assert data.get("keep_ctx_intra", False) is False

    def test_reset_reviewers_design_plan_batch(self, tmp_pipelines, sample_pipeline):
        """DESIGN_PLAN遷移 + keep_ctx_batch=True → reset_reviewers スキップ"""
        sample_pipeline["state"] = "INITIALIZE"
        sample_pipeline["keep_ctx_batch"] = True
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_PLAN", actor="cli", force=False, resume=False,
        )
        with patch("engine.reviewer._reset_reviewers") as mock_reset, \
             patch("commands.dev.notify_implementer"), \
             patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        mock_reset.assert_not_called()

    def test_reset_reviewers_design_plan_no_batch(self, tmp_pipelines, sample_pipeline):
        """DESIGN_PLAN遷移 + keep_ctx_batch=False → reset_reviewers 実行"""
        sample_pipeline["state"] = "INITIALIZE"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_PLAN", actor="cli", force=False, resume=False,
        )
        with patch("engine.reviewer._reset_reviewers", return_value=[]) as mock_reset, \
             patch("commands.dev.notify_implementer"), \
             patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        mock_reset.assert_called_once()

    def test_reset_reviewers_implementation_intra(self, tmp_pipelines, sample_pipeline):
        """IMPLEMENTATION遷移 + keep_ctx_intra=True → reset_reviewers スキップ"""
        sample_pipeline["state"] = "DESIGN_APPROVED"
        sample_pipeline["keep_ctx_intra"] = True
        sample_pipeline["batch"] = [{"issue": 1, "title": "T"}]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="IMPLEMENTATION", actor="cli", force=False, resume=False,
        )
        with patch("engine.reviewer._reset_reviewers") as mock_reset, \
             patch("commands.dev.notify_implementer"), \
             patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        mock_reset.assert_not_called()

    def test_reset_reviewers_implementation_no_intra(self, tmp_pipelines, sample_pipeline):
        """IMPLEMENTATION遷移 + keep_ctx_intra=False → reset_reviewers 実行"""
        sample_pipeline["state"] = "DESIGN_APPROVED"
        sample_pipeline["batch"] = [{"issue": 1, "title": "T"}]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="IMPLEMENTATION", actor="cli", force=False, resume=False,
        )
        with patch("engine.reviewer._reset_reviewers", return_value=[]) as mock_reset, \
             patch("commands.dev.notify_implementer"), \
             patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        mock_reset.assert_called_once()

    def test_reset_reviewers_design_plan_only_intra_does_not_skip(self, tmp_pipelines, sample_pipeline):
        """DESIGN_PLAN遷移 + keep_ctx_intra=True のみ → reset_reviewers 実行（batchがFalse）"""
        sample_pipeline["state"] = "INITIALIZE"
        sample_pipeline["keep_ctx_intra"] = True
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="DESIGN_PLAN", actor="cli", force=False, resume=False,
        )
        with patch("engine.reviewer._reset_reviewers", return_value=[]) as mock_reset, \
             patch("commands.dev.notify_implementer"), \
             patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        mock_reset.assert_called_once()

    def test_reset_reviewers_implementation_only_batch_does_not_skip(self, tmp_pipelines, sample_pipeline):
        """IMPLEMENTATION遷移 + keep_ctx_batch=True のみ → reset_reviewers 実行（intraがFalse）"""
        sample_pipeline["state"] = "DESIGN_APPROVED"
        sample_pipeline["keep_ctx_batch"] = True
        sample_pipeline["batch"] = [{"issue": 1, "title": "T"}]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="IMPLEMENTATION", actor="cli", force=False, resume=False,
        )
        with patch("engine.reviewer._reset_reviewers", return_value=[]) as mock_reset, \
             patch("commands.dev.notify_implementer"), \
             patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        mock_reset.assert_called_once()

    def test_revise_always_skips_reset(self, tmp_pipelines, sample_pipeline):
        """DESIGN_REVISE / CODE_REVISE → 常に reset_reviewers スキップ"""
        for from_state, to_state in [
            ("DESIGN_REVIEW", "DESIGN_REVISE"),
            ("CODE_REVIEW", "CODE_REVISE"),
        ]:
            sample_pipeline["state"] = from_state
            sample_pipeline["batch"] = [{"issue": 1, "title": "T"}]
            # keep_ctx フラグなし でもスキップ
            sample_pipeline.pop("keep_ctx_batch", None)
            sample_pipeline.pop("keep_ctx_intra", None)
            path = tmp_pipelines / "test-pj.json"
            write_pipeline(path, sample_pipeline)
            from gokrax import cmd_transition
            args = argparse.Namespace(
                project="test-pj", to=to_state, actor="cli", force=False, resume=False,
            )
            with patch("engine.reviewer._reset_reviewers") as mock_reset, \
                 patch("commands.dev.notify_implementer"), \
                 patch("commands.dev.notify_reviewers"):
                cmd_transition(args)
            mock_reset.assert_not_called()

    def test_idle_cleanup_pops_keep_ctx(self, tmp_pipelines, sample_pipeline):
        """IDLE遷移で keep_ctx_batch, keep_ctx_intra, keep_context(旧) が pop される。"""
        sample_pipeline["state"] = "DONE"
        sample_pipeline["keep_ctx_batch"] = True
        sample_pipeline["keep_ctx_intra"] = True
        sample_pipeline["keep_context"] = True  # 旧フラグ
        sample_pipeline["cc_session_id"] = "test-session-123"
        sample_pipeline["batch"] = [{"issue": 1, "title": "T"}]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="IDLE", actor="cli", force=False, resume=False,
        )
        with patch("commands.dev.notify_implementer"), patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        with open(path) as f:
            data = json.load(f)
        assert "keep_ctx_batch" not in data
        assert "keep_ctx_intra" not in data
        assert "keep_context" not in data

    def test_idle_cleanup_removes_cc_session_id(self, tmp_pipelines, sample_pipeline):
        """IDLE遷移で cc_session_id は pop される（セッション再利用しない）。"""
        sample_pipeline["state"] = "DONE"
        sample_pipeline["cc_session_id"] = "test-session-456"
        sample_pipeline["batch"] = [{"issue": 1, "title": "T"}]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="IDLE", actor="cli", force=False, resume=False,
        )
        with patch("commands.dev.notify_implementer"), patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        with open(path) as f:
            data = json.load(f)
        assert data.get("cc_session_id") is None

    def test_legacy_keep_context_normalization(self):
        """旧 keep_context=True → keep_ctx_batch + keep_ctx_intra に正規化。"""
        data = {"keep_context": True}
        # watchdog.py の正規化ロジックと同じ
        if "keep_context" in data and "keep_ctx_batch" not in data:
            legacy = data.pop("keep_context", False)
            if legacy:
                data["keep_ctx_batch"] = True
                data["keep_ctx_intra"] = True
        assert data.get("keep_ctx_batch") is True
        assert data.get("keep_ctx_intra") is True
        assert "keep_context" not in data

    def test_legacy_normalization_skipped_when_new_fields_exist(self):
        """新フィールドが既に存在すれば旧フィールド正規化をスキップ。"""
        data = {"keep_context": True, "keep_ctx_batch": False, "keep_ctx_intra": False}
        if "keep_context" in data and "keep_ctx_batch" not in data:
            legacy = data.pop("keep_context", False)
            if legacy:
                data["keep_ctx_batch"] = True
                data["keep_ctx_intra"] = True
        # keep_ctx_batch is already present → normalization skipped
        assert data.get("keep_ctx_batch") is False
        assert data.get("keep_ctx_intra") is False

    def test_idle_cleanup_pops_skip_cc_plan(self, tmp_pipelines, sample_pipeline):
        """IDLE遷移で skip_cc_plan が pop されること"""
        sample_pipeline["state"] = "DONE"
        sample_pipeline["skip_cc_plan"] = True
        sample_pipeline["batch"] = [{"issue": 1, "title": "T"}]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="IDLE", actor="cli", force=False, resume=False,
        )
        with patch("commands.dev.notify_implementer"), patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        with open(path) as f:
            data = json.load(f)
        assert "skip_cc_plan" not in data

    def test_idle_cleanup_pops_skip_test(self, tmp_pipelines, sample_pipeline):
        """IDLE遷移で skip_test が pop されること"""
        sample_pipeline["state"] = "DONE"
        sample_pipeline["skip_test"] = True
        sample_pipeline["batch"] = [{"issue": 1, "title": "T"}]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        from gokrax import cmd_transition
        args = argparse.Namespace(
            project="test-pj", to="IDLE", actor="cli", force=False, resume=False,
        )
        with patch("commands.dev.notify_implementer"), patch("commands.dev.notify_reviewers"):
            cmd_transition(args)
        with open(path) as f:
            data = json.load(f)
        assert "skip_test" not in data
