"""tests/test_short_context.py — Issue #90: short-context tier のテスト"""

import sys
from pathlib import Path
from unittest.mock import patch, call

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# config.py テスト
# ---------------------------------------------------------------------------

class TestShortContextTier:

    def test_short_context_tier_exists(self):
        """REVIEWER_TIERS に "short-context" キーが存在すること。"""
        import config
        assert "short-context" in config.REVIEWER_TIERS

    def test_get_tier_returns_short_context(self, monkeypatch):
        """short-context tier のメンバーに get_tier() が "short-context" を返すこと。"""
        import config
        from engine.reviewer import get_tier
        monkeypatch.setitem(config.REVIEWER_TIERS, "short-context", ["localllm"])
        assert get_tier("localllm") == "short-context"

    def test_get_tier_unknown_still_returns_free(self):
        """未知のエージェントは "free" を返す既存挙動が維持されること。"""
        from engine.reviewer import get_tier
        assert get_tier("unknown_agent_xyz") == "free"

    def test_tier_uniqueness_warning(self, monkeypatch, caplog):
        """同一レビュアーを複数 tier に入れた場合に warning が出ること。"""
        import logging
        import config
        from engine.reviewer import _validate_reviewer_tiers

        monkeypatch.setattr(config, "REVIEWER_TIERS", {
            "regular": ["dup_reviewer"],
            "free": [],
            "short-context": ["dup_reviewer"],
        })

        with caplog.at_level(logging.WARNING, logger="engine.reviewer"):
            _validate_reviewer_tiers()

        assert any("dup_reviewer" in r.message and "multiple tiers" in r.message
                   for r in caplog.records)


# ---------------------------------------------------------------------------
# notify.py テスト — short-context tier の /new 送信制御
# ---------------------------------------------------------------------------

def _make_batch_item(issue_num):
    return {
        "issue": issue_num, "title": "t", "commit": None,
        "design_reviews": {}, "code_reviews": {},
        "cc_session_id": None, "added_at": "",
    }


class TestNotifyReviewersShortContext:

    def _setup_short_ctx_reviewer(self, monkeypatch, reviewer_name="localllm"):
        """short-context tier のレビュアーを含むテスト用 REVIEW_MODES / REVIEWER_TIERS / AGENTS を設定。"""
        import config
        monkeypatch.setitem(config.REVIEWER_TIERS, "short-context", [reviewer_name])
        monkeypatch.setitem(config.REVIEW_MODES, "sc_mode", {
            "members": [reviewer_name],
            "min_reviews": 1,
            "grace_period_sec": 0,
        })
        monkeypatch.setitem(config.AGENTS, reviewer_name, {"id": reviewer_name})
        return reviewer_name

    def test_notify_reviewers_short_context_no_extra_new_when_not_reset(self, monkeypatch):
        """Issue #96: already_reset=False でも short-context tier に追加の /new が送られないこと（regular と同一挙動）。"""
        import notify
        import config

        reviewer = self._setup_short_ctx_reviewer(monkeypatch)
        monkeypatch.setattr(config, "DRY_RUN", True)

        with patch("notify.send_to_agent_queued") as mock_queued, \
             patch("notify.send_to_agent", return_value=True), \
             patch("notify.fetch_issue_body", return_value="body"), \
             patch("notify.format_review_request", return_value="msg"):
            notify.notify_reviewers(
                "proj", "DESIGN_REVIEW", [_make_batch_item(1)], "testns/proj",
                review_mode="sc_mode",
                already_reset=False,
            )

        new_calls = [c for c in mock_queued.call_args_list if c == call(reviewer, "/new")]
        assert len(new_calls) == 0, f"Expected no /new calls (regular-equivalent), got {mock_queued.call_args_list}"

    def test_notify_reviewers_short_context_skips_new_when_already_reset(self, monkeypatch):
        """already_reset=True 時、追加の /new が送られないこと。"""
        import notify
        import config

        reviewer = self._setup_short_ctx_reviewer(monkeypatch)
        monkeypatch.setattr(config, "DRY_RUN", True)

        with patch("notify.send_to_agent_queued") as mock_queued, \
             patch("notify.send_to_agent", return_value=True), \
             patch("notify.fetch_issue_body", return_value="body"), \
             patch("notify.format_review_request", return_value="msg"):
            notify.notify_reviewers(
                "proj", "DESIGN_REVIEW", [_make_batch_item(1)], "testns/proj",
                review_mode="sc_mode",
                already_reset=True,
            )

        new_calls = [c for c in mock_queued.call_args_list if c == call(reviewer, "/new")]
        assert len(new_calls) == 0, f"Expected no /new calls, got {mock_queued.call_args_list}"

    def test_notify_reviewers_short_context_no_sleep(self, monkeypatch):
        """Issue #96: short-context レビュアーが複数人いても追加の time.sleep が呼ばれないこと（regular と同一挙動）。"""
        import notify
        import config

        for r in ["localllm1", "localllm2"]:
            monkeypatch.setitem(config.AGENTS, r, {"id": r})
        monkeypatch.setitem(config.REVIEWER_TIERS, "short-context", ["localllm1", "localllm2"])
        monkeypatch.setitem(config.REVIEW_MODES, "sc_mode2", {
            "members": ["localllm1", "localllm2"],
            "min_reviews": 1,
            "grace_period_sec": 0,
        })
        monkeypatch.setattr(config, "DRY_RUN", False)

        with patch("notify.send_to_agent_queued", return_value=True), \
             patch("notify.send_to_agent", return_value=True), \
             patch("notify.fetch_issue_body", return_value="body"), \
             patch("notify.format_review_request", return_value="msg"), \
             patch("time.sleep") as mock_sleep:
            notify.notify_reviewers(
                "proj", "DESIGN_REVIEW", [_make_batch_item(1)], "testns/proj",
                review_mode="sc_mode2",
                already_reset=False,
            )

        mock_sleep.assert_not_called()

    def test_notify_reviewers_regular_no_extra_new(self, monkeypatch):
        """regular tier のレビュアーには追加 /new が送られないこと。"""
        import notify
        import config

        monkeypatch.setattr(config, "DRY_RUN", True)

        with patch("notify.send_to_agent_queued") as mock_queued, \
             patch("notify.send_to_agent", return_value=True), \
             patch("notify.fetch_issue_body", return_value="body"), \
             patch("notify.format_review_request", return_value="msg"):
            notify.notify_reviewers(
                "proj", "DESIGN_REVIEW", [_make_batch_item(1)], "testns/proj",
                review_mode="standard",
                already_reset=False,
            )

        # regular tier なので /new は送られない
        new_calls = [c for c in mock_queued.call_args_list
                     if len(c.args) >= 2 and c.args[1] == "/new"]
        assert len(new_calls) == 0, f"Expected no /new calls for regular tier, got {mock_queued.call_args_list}"

    def test_notify_reviewers_dry_run_no_sleep(self, monkeypatch):
        """DRY_RUN=True 時は time.sleep が呼ばれないこと。"""
        import notify
        import config

        self._setup_short_ctx_reviewer(monkeypatch)
        monkeypatch.setattr(config, "DRY_RUN", True)

        with patch("notify.send_to_agent_queued", return_value=True), \
             patch("notify.send_to_agent", return_value=True), \
             patch("notify.fetch_issue_body", return_value="body"), \
             patch("notify.format_review_request", return_value="msg"), \
             patch("time.sleep") as mock_sleep:
            notify.notify_reviewers(
                "proj", "DESIGN_REVIEW", [_make_batch_item(1)], "testns/proj",
                review_mode="sc_mode",
                already_reset=False,
            )

        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# watchdog.py テスト — _reset_short_context_reviewers()
# ---------------------------------------------------------------------------

class TestResetShortContextReviewers:

    def _setup(self, monkeypatch, short_ctx_members=None, other_members=None, mode_name="test_mode"):
        """テスト用の config を構築して返す。"""
        import config
        short_ctx_members = short_ctx_members or []
        other_members = other_members or []
        all_members = short_ctx_members + other_members

        monkeypatch.setitem(config.REVIEWER_TIERS, "short-context", short_ctx_members)
        for m in short_ctx_members:
            monkeypatch.setitem(config.AGENTS, m, {"id": m})
        for m in other_members:
            monkeypatch.setitem(config.AGENTS, m, {"id": m})
        monkeypatch.setitem(config.REVIEW_MODES, mode_name, {
            "members": all_members,
            "min_reviews": 1,
            "grace_period_sec": 0,
        })
        # Ensure openclaw backend so send_to_agent_queued path is exercised
        # (project settings may set DEFAULT_AGENT_BACKEND="pi")
        monkeypatch.setattr(config, "DEFAULT_AGENT_BACKEND", "openclaw")
        return mode_name

    def test_keep_ctx_sends_new_to_short_context(self, monkeypatch):
        """keep_ctx=True + short-context tier メンバーがいるモードで /new が short-context メンバーに送信されること。"""
        import config
        import engine.reviewer

        mode = self._setup(monkeypatch, short_ctx_members=["reviewer5", "reviewer4"], other_members=["regular1"])
        monkeypatch.setattr(config, "DRY_RUN", True)

        with patch("engine.reviewer.send_to_agent_queued", return_value=True) as mock_queued, \
             patch("engine.reviewer.log"):
            engine.reviewer._reset_short_context_reviewers(mode)

        assert call("reviewer5", "/new") in mock_queued.call_args_list
        assert call("reviewer4", "/new") in mock_queued.call_args_list

    def test_keep_ctx_does_not_send_new_to_regular(self, monkeypatch):
        """short-context 以外の tier のメンバーには /new が送信されないこと。"""
        import config
        import engine.reviewer

        mode = self._setup(monkeypatch, short_ctx_members=["reviewer5"], other_members=["regular1"])
        monkeypatch.setattr(config, "DRY_RUN", True)

        with patch("engine.reviewer.send_to_agent_queued", return_value=True) as mock_queued, \
             patch("engine.reviewer.log"):
            engine.reviewer._reset_short_context_reviewers(mode)

        sent_to = [c.args[0] for c in mock_queued.call_args_list]
        assert "regular1" not in sent_to

    def test_no_short_context_members_noop(self, monkeypatch):
        """short-context メンバーがいないモードでは send_to_agent_queued が呼ばれないこと。"""
        import config
        import engine.reviewer

        mode = self._setup(monkeypatch, short_ctx_members=[], other_members=["regular1"])
        monkeypatch.setattr(config, "DRY_RUN", True)

        with patch("engine.reviewer.send_to_agent_queued") as mock_queued, \
             patch("engine.reviewer.log"):
            engine.reviewer._reset_short_context_reviewers(mode)

        mock_queued.assert_not_called()

    def test_unknown_review_mode_raises(self, monkeypatch):
        """未知の review_mode を渡したとき KeyError を raise すること。"""
        import engine.reviewer

        with pytest.raises(KeyError):
            engine.reviewer._reset_short_context_reviewers("nonexistent_mode_xyz")

    def test_dry_run_skips_sleep(self, monkeypatch):
        """DRY_RUN=True 時に time.sleep が呼ばれないこと。"""
        import config
        import engine.reviewer

        mode = self._setup(monkeypatch, short_ctx_members=["reviewer5"])
        monkeypatch.setattr(config, "DRY_RUN", True)

        with patch("engine.reviewer.send_to_agent_queued", return_value=True), \
             patch("engine.reviewer.log"), \
             patch("time.sleep") as mock_sleep:
            engine.reviewer._reset_short_context_reviewers(mode)

        mock_sleep.assert_not_called()

    def test_non_dry_run_sleeps(self, monkeypatch):
        """DRY_RUN=False 時に time.sleep(POST_NEW_COMMAND_WAIT_SEC) が呼ばれること。"""
        import config
        import engine.reviewer

        mode = self._setup(monkeypatch, short_ctx_members=["reviewer5"])
        monkeypatch.setattr(config, "DRY_RUN", False)

        with patch("engine.reviewer.send_to_agent_queued", return_value=True), \
             patch("engine.reviewer.log"), \
             patch("time.sleep") as mock_sleep:
            engine.reviewer._reset_short_context_reviewers(mode)

        mock_sleep.assert_called_once_with(config.POST_NEW_COMMAND_WAIT_SEC)
