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
        monkeypatch.setitem(config.REVIEWER_TIERS, "short-context", ["localllm"])
        assert config.get_tier("localllm") == "short-context"

    def test_get_tier_unknown_still_returns_free(self):
        """未知のエージェントは "free" を返す既存挙動が維持されること。"""
        import config
        assert config.get_tier("unknown_agent_xyz") == "free"

    def test_tier_uniqueness_warning(self, monkeypatch, caplog):
        """同一レビュアーを複数 tier に入れた場合に warning が出ること。"""
        import logging
        import config

        original = dict(config.REVIEWER_TIERS)
        monkeypatch.setattr(config, "REVIEWER_TIERS", {
            "regular": ["dup_reviewer"],
            "semi": ["dup_reviewer"],
            "free": [],
            "short-context": [],
        })

        with caplog.at_level(logging.WARNING, logger="config"):
            config._validate_reviewer_tiers()

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
                "proj", "DESIGN_REVIEW", [_make_batch_item(1)], "atakalive/proj",
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
                "proj", "DESIGN_REVIEW", [_make_batch_item(1)], "atakalive/proj",
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
                "proj", "DESIGN_REVIEW", [_make_batch_item(1)], "atakalive/proj",
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
                "proj", "DESIGN_REVIEW", [_make_batch_item(1)], "atakalive/proj",
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

        reviewer = self._setup_short_ctx_reviewer(monkeypatch)
        monkeypatch.setattr(config, "DRY_RUN", True)

        with patch("notify.send_to_agent_queued", return_value=True), \
             patch("notify.send_to_agent", return_value=True), \
             patch("notify.fetch_issue_body", return_value="body"), \
             patch("notify.format_review_request", return_value="msg"), \
             patch("time.sleep") as mock_sleep:
            notify.notify_reviewers(
                "proj", "DESIGN_REVIEW", [_make_batch_item(1)], "atakalive/proj",
                review_mode="sc_mode",
                already_reset=False,
            )

        mock_sleep.assert_not_called()
