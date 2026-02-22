"""tests/test_notify.py — notify.py の例外処理テスト"""

import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestGetBotToken:

    def test_file_not_found(self, tmp_path, caplog):
        import notify
        with patch.object(notify, "GATEWAY_TOKEN_PATH", tmp_path / "nonexistent.json"):
            with caplog.at_level(logging.ERROR, logger="devbar.notify"):
                result = notify.get_bot_token()
        assert result is None
        assert "Gateway config not found" in caplog.text

    def test_invalid_json(self, tmp_path, caplog):
        import notify
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{invalid json")
        with patch.object(notify, "GATEWAY_TOKEN_PATH", bad_file):
            with caplog.at_level(logging.ERROR, logger="devbar.notify"):
                result = notify.get_bot_token()
        assert result is None
        assert "Invalid JSON" in caplog.text

    def test_key_not_found(self, tmp_path, caplog):
        import notify
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"channels": {}}))
        with patch.object(notify, "GATEWAY_TOKEN_PATH", cfg):
            with caplog.at_level(logging.ERROR, logger="devbar.notify"):
                result = notify.get_bot_token()
        assert result is None
        assert "key not found" in caplog.text

    def test_success(self, tmp_path):
        import notify
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({
            "channels": {
                "discord": {
                    "accounts": {
                        "kaneko-bot": {"token": "test-token-123"}
                    }
                }
            }
        }))
        with patch.object(notify, "GATEWAY_TOKEN_PATH", cfg):
            with patch.object(notify, "DISCORD_BOT_ACCOUNT", "kaneko-bot"):
                result = notify.get_bot_token()
        assert result == "test-token-123"


class TestSendToAgent:

    def test_openclaw_not_found(self, caplog):
        import notify
        with patch("notify.subprocess.run", side_effect=FileNotFoundError("openclaw")):
            with caplog.at_level(logging.ERROR, logger="devbar.notify"):
                result = notify.send_to_agent("test-agent", "hello")
        assert result is False
        assert "openclaw CLI not found" in caplog.text

    def test_timeout(self, caplog):
        import notify
        with patch("notify.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
            with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                result = notify.send_to_agent("test-agent", "hello", timeout=1)
        assert result is False
        assert "timed out" in caplog.text


class TestPostDiscord:

    def test_no_token_returns_none(self):
        import notify
        with patch.object(notify, "get_bot_token", return_value=None):
            result = notify.post_discord("123456", "test message")
        assert result is None

    def test_4xx_response(self, caplog):
        import notify
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.post", return_value=mock_resp):
                with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                    result = notify.post_discord("123456", "test message")
        assert result is None
        assert "Discord post failed" in caplog.text

    def test_request_exception(self, caplog):
        import notify
        import requests
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.post", side_effect=requests.ConnectionError("refused")):
                with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                    result = notify.post_discord("123456", "test message")
        assert result is None
        assert "Discord post error" in caplog.text

    def test_success_returns_message_id(self):
        import notify
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "9999000111222333"}
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.post", return_value=mock_resp):
                result = notify.post_discord("123456", "test message")
        assert result == "9999000111222333"


class TestFormatReviewRequest:

    def _make_batch_item(self, issue_num, title="t", commit=None):
        return {
            "issue": issue_num, "title": title, "commit": commit,
            "design_reviews": {}, "code_reviews": {},
            "cc_session_id": None, "added_at": "",
        }

    def test_command_uses_devbar_cli_path(self):
        """format_review_request() のコマンドが DEVBAR_CLI パスを使うこと。"""
        import notify
        import config
        batch = [self._make_batch_item(42, "Test Issue", "abc123")]
        result = notify.format_review_request(
            project="test-pj", state="DESIGN_REVIEW",
            batch=batch, gitlab="atakalive/test-pj", reviewer="pascal",
        )
        assert str(config.DEVBAR_CLI) in result
        assert "/home/ataka/.openclaw/shared/bin/devbar" in result

    def test_command_contains_reviewer_name(self):
        """format_review_request() のコマンドに reviewer 名が含まれること。"""
        import notify
        batch = [self._make_batch_item(10)]
        result = notify.format_review_request(
            project="test-pj", state="CODE_REVIEW",
            batch=batch, gitlab="atakalive/test-pj", reviewer="leibniz",
        )
        assert "--reviewer leibniz" in result

    def test_command_structure(self):
        """生成コマンドが python3 <DEVBAR_CLI> review ... 形式であること。"""
        import notify
        import config
        batch = [self._make_batch_item(5)]
        result = notify.format_review_request(
            project="proj", state="DESIGN_REVIEW",
            batch=batch, gitlab="atakalive/proj", reviewer="hanfei",
        )
        assert f"python3 {config.DEVBAR_CLI} review" in result


class TestNotifyImplementer:

    def test_known_agent_sends_with_agent_id(self):
        """notify_implementer('kaneko', ...) → send_to_agent が agentId 'kaneko' で呼ばれること。"""
        import notify
        with patch("notify.send_to_agent") as mock_send:
            notify.notify_implementer("kaneko", "test message")
        mock_send.assert_called_once_with("kaneko", "test message")

    def test_unknown_agent_logs_error_no_send(self, caplog):
        """未知のキー → logger.error が呼ばれ、send_to_agent は呼ばれないこと。"""
        import notify
        import logging
        with patch("notify.send_to_agent") as mock_send:
            with caplog.at_level(logging.ERROR, logger="devbar.notify"):
                notify.notify_implementer("unknown_agent", "test message")
        mock_send.assert_not_called()
        assert "Unknown agent" in caplog.text


class TestNotifyReviewers:

    def _make_batch_item(self, issue_num):
        return {
            "issue": issue_num, "title": "t", "commit": None,
            "design_reviews": {}, "code_reviews": {},
            "cc_session_id": None, "added_at": "",
        }

    def test_each_reviewer_uses_agent_id(self):
        """notify_reviewers → 各レビュアーの agentId で send_to_agent が呼ばれること。"""
        import notify
        import config
        batch = [self._make_batch_item(1)]
        with patch("notify.send_to_agent") as mock_send:
            notify.notify_reviewers("proj", "DESIGN_REVIEW", batch, "atakalive/proj")

        called_agents = [c.args[0] for c in mock_send.call_args_list]
        for r in config.DESIGN_REVIEWERS:
            assert r in called_agents, \
                f"{r} が send_to_agent に渡されていない"

    def test_unknown_reviewer_logs_error_and_continues(self, caplog, monkeypatch):
        """未知のレビュアーはスキップされ、既知のレビュアーには送信が継続されること。"""
        import notify
        import config
        import logging

        # CODE_REVIEWERS に未知のキーを混入（CODE_REVIEW状態で使われる）
        monkeypatch.setattr(config, "CODE_REVIEWERS", ["pascal", "unknown_reviewer"])
        monkeypatch.setattr(notify, "CODE_REVIEWERS", ["pascal", "unknown_reviewer"])

        batch = [self._make_batch_item(1)]
        with patch("notify.send_to_agent") as mock_send:
            with caplog.at_level(logging.ERROR, logger="devbar.notify"):
                notify.notify_reviewers("proj", "CODE_REVIEW", batch, "atakalive/proj")

        # 未知レビュアーのエラーログ
        assert "Unknown reviewer" in caplog.text
        # 既知の pascal には送信される
        called_agents = [c.args[0] for c in mock_send.call_args_list]
        assert "pascal" in called_agents
        # unknown_reviewer には送信されない
        assert len(called_agents) == 1


class TestFetchDiscordReplies:
    """fetch_discord_replies のテスト（#18）"""

    def test_no_token_returns_empty(self):
        import notify
        with patch.object(notify, "get_bot_token", return_value=None):
            result = notify.fetch_discord_replies("123456", "999")
        assert result == []

    def test_success_returns_messages(self):
        import notify
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"id": "1001", "content": "ok", "author": {"id": "user1"}},
            {"id": "1002", "content": "lgtm", "author": {"id": "user2"}},
        ]
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.get", return_value=mock_resp):
                result = notify.fetch_discord_replies("123456", "999")
        assert len(result) == 2
        assert result[0]["id"] == "1001"

    def test_4xx_returns_empty(self, caplog):
        import notify
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.get", return_value=mock_resp):
                with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                    result = notify.fetch_discord_replies("123456", "999")
        assert result == []
        assert "Discord fetch failed" in caplog.text

    def test_request_exception_returns_empty(self, caplog):
        import notify
        import requests
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.get", side_effect=requests.ConnectionError("refused")):
                with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                    result = notify.fetch_discord_replies("123456", "999")
        assert result == []
        assert "Discord fetch error" in caplog.text

    def test_passes_after_param(self):
        """after パラメータが正しく渡されること"""
        import notify
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.get", return_value=mock_resp) as mock_get:
                notify.fetch_discord_replies("ch123", "msg456")
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["after"] == "msg456"
        assert kwargs["params"]["limit"] == 50


class TestNotifyDiscord:
    """notify_discord のテスト（#19）"""

    def test_calls_post_discord_with_channel(self):
        import notify
        import config
        with patch.object(notify, "post_discord", return_value="msg-id") as mock_post:
            notify.notify_discord("test message")
        mock_post.assert_called_once_with(config.DISCORD_CHANNEL, "test message")
