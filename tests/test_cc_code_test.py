"""tests/test_cc_code_test.py — _start_code_test のテスト (Issue #216)"""

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestStartCodeTestUnknownProject:
    """BA-008: TEST_CONFIG に未登録のプロジェクトで RuntimeError が発生する"""

    def test_start_code_test_unknown_project_raises(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "TEST_CONFIG", {})

        from engine.cc import _start_code_test

        data = {"repo_path": "/some/repo/path"}
        with pytest.raises(RuntimeError, match="TEST_CONFIG has no entry"):
            _start_code_test("nonexistent_project", data, Path("/tmp/dummy.json"))

        assert "_code_test" not in data


class TestStartCodeTestScriptContainsCd:
    """BA-009: 生成スクリプトに cd が含まれる"""

    def test_start_code_test_script_contains_cd(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "TEST_CONFIG", {
            "testpj": {
                "test_command": "pytest -x",
                "test_timeout": 300,
            },
        })

        from engine.cc import _start_code_test

        data = {"repo_path": "/some/repo/path"}

        written_content = {}

        def fake_write(fd, content):
            written_content["data"] = content

        with patch("tempfile.mkstemp", side_effect=[
            (99, "/tmp/fake-output.txt"),
            (100, "/tmp/fake-script.sh"),
        ]), \
             patch("os.close"), \
             patch("os.write", side_effect=fake_write), \
             patch("os.chmod"), \
             patch("subprocess.Popen", return_value=MagicMock(pid=12345)), \
             patch("subprocess.run", return_value=MagicMock(stdout="abc123\n")), \
             patch("engine.cc.update_pipeline"):
            _start_code_test("testpj", data, Path("/tmp/dummy.json"))

        script = written_content["data"].decode()
        assert "cd /some/repo/path" in script
