"""block_dangerous_subprocess fixture の振る舞いテスト。

conftest.py の autouse fixture が正しく動作していることを直接検証する。
"""

import os
import subprocess

import pytest


class TestBlockedPatterns:
    """subprocess.run / Popen で BLOCKED_PATTERNS に該当するコマンドがブロックされる。"""

    def test_subprocess_run_blocks_claude(self):
        with pytest.raises(RuntimeError, match="blocked process"):
            subprocess.run(["claude", "--help"])

    def test_subprocess_run_blocks_glab(self):
        with pytest.raises(RuntimeError, match="blocked process"):
            subprocess.run(["glab", "issue", "list"])

    def test_subprocess_popen_blocks_claude(self):
        with pytest.raises(RuntimeError, match="blocked process"):
            subprocess.Popen(["claude", "code"])

    def test_subprocess_popen_blocks_glab(self):
        with pytest.raises(RuntimeError, match="blocked process"):
            subprocess.Popen(["glab", "mr", "list"])

    def test_subprocess_run_allows_safe_command(self):
        """BLOCKED_PATTERNS に該当しないコマンドは通過する。"""
        result = subprocess.run(["echo", "hello"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "hello" in result.stdout


class TestOsBlocked:
    """os.system / os.popen は無条件でブロックされる。"""

    def test_os_system_blocked(self):
        with pytest.raises(RuntimeError, match="os.system"):
            os.system("echo hello")

    def test_os_popen_blocked(self):
        with pytest.raises(RuntimeError, match="os.popen"):
            os.popen("echo hello")


class TestNonStrElements:
    """cmd にPathLike / int 等の非str要素が混在してもTypeErrorにならない。"""

    def test_pathlike_in_cmd(self, tmp_path):
        """PathLike要素を含むコマンドでもパターンチェックが動作する。"""
        with pytest.raises(RuntimeError, match="blocked process"):
            subprocess.run([tmp_path / "claude", "--help"])

    def test_int_in_cmd(self):
        """int要素を含むコマンドでもTypeErrorではなくパターンチェックが動作する。"""
        with pytest.raises(RuntimeError, match="blocked process"):
            subprocess.run(["claude", 42, "--verbose"])
