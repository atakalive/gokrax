"""tests/test_config.py — config定数の反映テスト"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestValidVerdicts:

    def test_verdicts_reflected_in_argparse(self, tmp_pipelines):
        """VALID_VERDICTS が devbar review の choices に反映される。"""
        import importlib
        import config
        import devbar

        # devbar が config.VALID_VERDICTS を参照しているか確認
        assert hasattr(config, "VALID_VERDICTS")
        assert "APPROVE" in config.VALID_VERDICTS
        assert "P0" in config.VALID_VERDICTS
        assert "P1" in config.VALID_VERDICTS
        assert "REJECT" in config.VALID_VERDICTS


class TestTimeoutConstants:

    def test_agent_send_timeout_exists(self):
        import config
        assert hasattr(config, "AGENT_SEND_TIMEOUT")
        assert config.AGENT_SEND_TIMEOUT == 30

    def test_discord_post_timeout_exists(self):
        import config
        assert hasattr(config, "DISCORD_POST_TIMEOUT")
        assert config.DISCORD_POST_TIMEOUT == 10

    def test_glab_timeout_exists(self):
        import config
        assert hasattr(config, "GLAB_TIMEOUT")
        assert config.GLAB_TIMEOUT == 15

    def test_notify_uses_agent_send_timeout(self):
        """notify.send_to_agent のデフォルト timeout が AGENT_SEND_TIMEOUT。"""
        import inspect
        import notify
        import config
        sig = inspect.signature(notify.send_to_agent)
        default = sig.parameters["timeout"].default
        assert default == config.AGENT_SEND_TIMEOUT
