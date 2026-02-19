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

    def test_4xx_response(self, caplog):
        import notify
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.post", return_value=mock_resp):
                with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                    result = notify.post_discord("123456", "test message")
        assert result is False
        assert "Discord post failed" in caplog.text

    def test_request_exception(self, caplog):
        import notify
        import requests
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.post", side_effect=requests.ConnectionError("refused")):
                with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                    result = notify.post_discord("123456", "test message")
        assert result is False
        assert "Discord post error" in caplog.text


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
