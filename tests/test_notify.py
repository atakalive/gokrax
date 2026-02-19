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
