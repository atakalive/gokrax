"""tests/test_cc_resource_warning.py — BA-035: Popen ResourceWarning 抑制テスト"""

import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestStartCcReturncode:
    """_start_cc の proc.returncode 設定テスト。"""

    def test_returncode_set_on_success(self):
        """正常系: update_pipeline 成功後に proc.returncode が 0 に設定される。"""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        # MagicMock の returncode はデフォルトで MagicMock インスタンス
        # 0 に設定されることを確認するため、初期値を None にしておく
        mock_proc.returncode = None

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("engine.cc.update_pipeline"),
            patch("engine.cc.load_pipeline", return_value={
                "batch": [{"issue": 1}],
                "repo_path": "/tmp",
                "base_commit": "abc123",
            }),
            patch("engine.cc.log"),
            patch("tempfile.mkstemp", side_effect=[
                (10, "/tmp/gokrax-plan-x.txt"),
                (11, "/tmp/gokrax-impl-x.txt"),
                (12, "/tmp/gokrax-cc-x.sh"),
            ]),
            patch("os.write"),
            patch("os.close"),
            patch("os.chmod"),
            patch("notify.fetch_issue_body", return_value="test body"),
        ):
            from engine.cc import _start_cc
            _start_cc(
                project="test",
                batch=[{"issue": 1, "title": "T"}],
                gitlab="test/repo",
                repo_path="/tmp",
                pipeline_path=Path("/tmp/test.json"),
            )

        assert mock_proc.returncode == 0

    def test_returncode_not_set_on_pipeline_failure(self):
        """異常系: update_pipeline 失敗時に proc.returncode は 0 にならない。"""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("engine.cc.update_pipeline", side_effect=RuntimeError("disk full")),
            patch("engine.cc.load_pipeline", return_value={
                "batch": [{"issue": 1}],
                "repo_path": "/tmp",
                "base_commit": "abc123",
            }),
            patch("engine.cc.log"),
            patch("tempfile.mkstemp", side_effect=[
                (10, "/tmp/gokrax-plan-x.txt"),
                (11, "/tmp/gokrax-impl-x.txt"),
                (12, "/tmp/gokrax-cc-x.sh"),
            ]),
            patch("os.write"),
            patch("os.close"),
            patch("os.chmod"),
            patch("os.killpg") as mock_killpg,
            patch("notify.fetch_issue_body", return_value="test body"),
        ):
            from engine.cc import _start_cc
            with pytest.raises(RuntimeError):
                _start_cc(
                    project="test",
                    batch=[{"issue": 1, "title": "T"}],
                    gitlab="test/repo",
                    repo_path="/tmp",
                    pipeline_path=Path("/tmp/test.json"),
                )

        assert mock_proc.returncode != 0
        mock_killpg.assert_any_call(12345, signal.SIGKILL)


class TestStartCodeTestReturncode:
    """_start_code_test の proc.returncode 設定テスト。"""

    def test_returncode_set_on_success(self, monkeypatch):
        """正常系: update_pipeline 成功後に proc.returncode が 0 に設定される。"""
        import config
        monkeypatch.setattr(config, "TEST_CONFIG", {
            "testpj": {"test_command": "pytest -x", "test_timeout": 300},
        })

        mock_proc = MagicMock()
        mock_proc.pid = 22222
        mock_proc.returncode = None

        mock_git = MagicMock()
        mock_git.stdout = "abc1234\n"

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("subprocess.run", return_value=mock_git),
            patch("engine.cc.update_pipeline"),
            patch("engine.cc.log"),
            patch("tempfile.mkstemp", side_effect=[
                (20, "/tmp/fake-output.txt"),
                (21, "/tmp/fake-script.sh"),
            ]),
            patch("os.write"),
            patch("os.close"),
            patch("os.chmod"),
        ):
            from engine.cc import _start_code_test
            _start_code_test("testpj", {"repo_path": "/tmp/repo"}, Path("/tmp/p.json"))

        assert mock_proc.returncode == 0


class TestStartCcTestFixReturncode:
    """_start_cc_test_fix の proc.returncode 設定テスト。"""

    def test_returncode_set_on_success(self):
        """正常系: update_pipeline 成功後に proc.returncode が 0 に設定される。"""
        mock_proc = MagicMock()
        mock_proc.pid = 33333
        mock_proc.returncode = None

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("engine.cc.update_pipeline"),
            patch("engine.cc.log"),
            patch("tempfile.mkstemp", side_effect=[
                (30, "/tmp/gokrax-testfix-prompt.txt"),
                (31, "/tmp/gokrax-testfix-script.sh"),
            ]),
            patch("os.write"),
            patch("os.close"),
            patch("os.chmod"),
        ):
            from engine.cc import _start_cc_test_fix
            _start_cc_test_fix(
                project="testpj",
                batch=[{"issue": 1}],
                data={"repo_path": "/tmp/repo", "test_output": "FAIL", "test_retry_count": 1},
                pipeline_path=Path("/tmp/p.json"),
            )

        assert mock_proc.returncode == 0
