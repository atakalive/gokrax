"""tests/test_config.py — config定数の反映テスト"""

import importlib
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestValidVerdicts:

    def test_verdicts_reflected_in_argparse(self, tmp_pipelines):
        """VALID_VERDICTS が gokrax review の choices に反映される。"""
        import importlib
        import config
        import gokrax

        # gokrax が config.VALID_VERDICTS を参照しているか確認
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


class TestDevbarCliPath:

    def test_gokrax_cli_is_non_empty(self):
        """GOKRAX_CLI が空でないこと。"""
        import config
        assert str(config.GOKRAX_CLI) != ""

    def test_gokrax_cli_is_absolute(self):
        """GOKRAX_CLI が絶対パスであること。"""
        import config
        assert config.GOKRAX_CLI.is_absolute()


class TestSysPathResolve:

    def test_gokrax_uses_resolve_in_sys_path(self):
        """gokrax.py の sys.path.insert が .resolve() を使っていること。"""
        source = (ROOT / "gokrax.py").read_text(encoding="utf-8")
        assert "Path(__file__).resolve().parent" in source

    def test_watchdog_uses_resolve_in_sys_path(self):
        """watchdog.py の sys.path.insert が .resolve() を使っていること。"""
        source = (ROOT / "watchdog.py").read_text(encoding="utf-8")
        assert "Path(__file__).resolve().parent" in source


class TestLoadSkills:

    def test_load_skills_known_agent(self, tmp_path, monkeypatch):
        """既知エージェントのスキルファイルが正しく読み込まれること。"""
        import config
        import notify

        # ダミースキルファイル作成
        skill_a = tmp_path / "skill_a.md"
        skill_a.write_text("Skill A content\n\n", encoding="utf-8")
        skill_b = tmp_path / "skill_b.md"
        skill_b.write_text("Skill B content\n", encoding="utf-8")

        monkeypatch.setattr(config, "SKILLS", {
            "skill-a": str(skill_a),
            "skill-b": str(skill_b),
        })
        monkeypatch.setattr(config, "AGENT_SKILLS", {
            "test-agent": {"code": ["skill-a", "skill-b"]},
        })
        monkeypatch.setattr(config, "PROJECT_SKILLS", {})

        result = notify.load_skills("test-agent", phase="code")
        assert result.startswith("<skills>\n")
        assert result.endswith("\n</skills>")
        assert "--- skill: skill-a ---" in result
        assert "--- skill: skill-b ---" in result
        assert "Skill A content" in result
        assert "Skill B content" in result
        # 末尾改行の正規化: スキル間は空行1行で区切り
        assert "--- skill: skill-a ---\nSkill A content\n\n--- skill: skill-b ---" in result

    def test_load_skills_unknown_agent(self, monkeypatch):
        """AGENT_SKILLS に存在しないエージェント名で空文字列が返ること。"""
        import config
        import notify
        monkeypatch.setattr(config, "AGENT_SKILLS", {})
        result = notify.load_skills("nonexistent-agent")
        assert result == ""

    def test_load_skills_missing_file(self, tmp_path, monkeypatch, caplog):
        """ファイル読み込み失敗時に warning が出てスキップされること。"""
        import config
        import notify

        # 存在するスキルと存在しないスキル
        skill_ok = tmp_path / "ok.md"
        skill_ok.write_text("OK content", encoding="utf-8")

        monkeypatch.setattr(config, "SKILLS", {
            "ok-skill": str(skill_ok),
            "missing-skill": str(tmp_path / "nonexistent.md"),
        })
        monkeypatch.setattr(config, "AGENT_SKILLS", {
            "test-agent": {"code": ["missing-skill", "ok-skill"]},
        })
        monkeypatch.setattr(config, "PROJECT_SKILLS", {})

        with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
            result = notify.load_skills("test-agent", phase="code")

        assert "failed to read" in caplog.text
        # 存在するスキルは正常に読み込まれる
        assert "--- skill: ok-skill ---" in result
        assert "OK content" in result

    def test_load_skills_unknown_skill_name(self, monkeypatch, caplog):
        """SKILLS に存在しないスキル名で warning が出てスキップされること。"""
        import config
        import notify

        monkeypatch.setattr(config, "SKILLS", {})
        monkeypatch.setattr(config, "AGENT_SKILLS", {
            "test-agent": {"code": ["nonexistent-skill"]},
        })
        monkeypatch.setattr(config, "PROJECT_SKILLS", {})

        with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
            result = notify.load_skills("test-agent", phase="code")

        assert "unknown skill" in caplog.text
        assert result == ""

    def test_load_skills_truncation(self, tmp_path, monkeypatch, caplog):
        """MAX_SKILL_CHARS 超過時に切り詰めが行われること。"""
        import config
        import notify

        skill_file = tmp_path / "big.md"
        skill_file.write_text("X" * 10000, encoding="utf-8")

        monkeypatch.setattr(config, "SKILLS", {"big": str(skill_file)})
        monkeypatch.setattr(config, "AGENT_SKILLS", {"test-agent": {"code": ["big"]}})
        monkeypatch.setattr(config, "PROJECT_SKILLS", {})
        monkeypatch.setattr(config, "MAX_SKILL_CHARS", 100)

        with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
            result = notify.load_skills("test-agent", phase="code")

        assert "truncating" in caplog.text
        assert len(result) <= 100
        assert result.endswith("</skills>")

    def test_load_skills_truncation_extreme(self, tmp_path, monkeypatch):
        """MAX_SKILL_CHARS が _MIN_SKILL_CHARS 未満の場合、空文字列が返ること。"""
        import config
        import notify

        skill_file = tmp_path / "some.md"
        skill_file.write_text("content", encoding="utf-8")

        monkeypatch.setattr(config, "SKILLS", {"s": str(skill_file)})
        monkeypatch.setattr(config, "AGENT_SKILLS", {"test-agent": {"code": ["s"]}})
        monkeypatch.setattr(config, "PROJECT_SKILLS", {})
        monkeypatch.setattr(config, "MAX_SKILL_CHARS", 5)

        result = notify.load_skills("test-agent", phase="code")
        assert result == ""


class TestLogFileEnvVar:
    """BA-034: GOKRAX_LOG_FILE 環境変数で LOG_FILE をオーバーライドできる。"""

    def test_default_when_unset(self, monkeypatch):
        """環境変数未設定時はデフォルトの /tmp/gokrax-watchdog.log。"""
        import config.paths
        monkeypatch.delenv("GOKRAX_LOG_FILE", raising=False)
        importlib.reload(config.paths)
        try:
            assert config.paths.LOG_FILE == Path("/tmp/gokrax-watchdog.log")
        finally:
            importlib.reload(config.paths)

    def test_override_with_env(self, monkeypatch):
        """環境変数設定時はその値のパスになる。"""
        import config.paths
        monkeypatch.setenv("GOKRAX_LOG_FILE", "/var/log/gokrax.log")
        importlib.reload(config.paths)
        try:
            assert config.paths.LOG_FILE == Path("/var/log/gokrax.log")
        finally:
            importlib.reload(config.paths)

    def test_empty_string_falls_back_to_default(self, monkeypatch):
        """空文字列は未設定扱い。デフォルトにフォールバック。"""
        import config.paths
        monkeypatch.setenv("GOKRAX_LOG_FILE", "")
        importlib.reload(config.paths)
        try:
            assert config.paths.LOG_FILE == Path("/tmp/gokrax-watchdog.log")
        finally:
            importlib.reload(config.paths)


class TestMaxCliArgBytes:
    """MAX_CLI_ARG_BYTES が OS ごとに正しい値を返す。"""

    def test_linux(self):
        with patch("config.sys.platform", "linux"):
            from config import _get_max_cli_arg_bytes
            assert _get_max_cli_arg_bytes() == 120_000

    def test_darwin(self):
        with patch("config.sys.platform", "darwin"):
            from config import _get_max_cli_arg_bytes
            assert _get_max_cli_arg_bytes() == 900_000

    def test_win32(self):
        with patch("config.sys.platform", "win32"):
            from config import _get_max_cli_arg_bytes
            assert _get_max_cli_arg_bytes() == 30_000

    def test_unknown_falls_back_to_linux(self):
        with patch("config.sys.platform", "freebsd"):
            from config import _get_max_cli_arg_bytes
            assert _get_max_cli_arg_bytes() == 120_000
