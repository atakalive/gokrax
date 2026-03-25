"""Tests for engine/cc.py kill/exception handling (BA-015, BA-017)."""

import errno
import signal
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from engine.cc import _killpg_graceful


# ── BA-015: _killpg_graceful ──────────────────────────────────────────────


class TestKillpgGraceful:
    """_killpg_graceful のユニットテスト。"""

    def test_killpg_graceful_sigterm_success(self) -> None:
        """SIGTERM 後、1回目のポーリングでグループが消えたケース。"""
        def killpg_side_effect(pid: int, sig: int) -> None:
            if sig == signal.SIGTERM:
                return  # 正常
            if sig == 0:
                raise OSError(errno.ESRCH, "No such process")

        with (
            patch("os.killpg", side_effect=killpg_side_effect) as mock_killpg,
            patch("time.monotonic", side_effect=[0.0, 0.0]),
            patch("time.sleep"),
        ):
            result = _killpg_graceful(pid=12345)

        assert result is False
        assert mock_killpg.call_count == 2
        mock_killpg.assert_any_call(12345, signal.SIGTERM)
        mock_killpg.assert_any_call(12345, 0)
        # SIGKILL が呼ばれていないことを確認
        for call in mock_killpg.call_args_list:
            assert call[0][1] != signal.SIGKILL

    def test_killpg_graceful_needs_sigkill(self) -> None:
        """ポーリングでグループが消えず SIGKILL にエスカレーションするケース。"""
        with (
            patch("os.killpg") as mock_killpg,
            patch("time.monotonic", side_effect=[0.0, 0.0, 3.0]),
            patch("time.sleep"),
        ):
            result = _killpg_graceful(pid=12345)

        assert result is True
        mock_killpg.assert_any_call(12345, signal.SIGTERM)
        mock_killpg.assert_any_call(12345, 0)
        mock_killpg.assert_any_call(12345, signal.SIGKILL)

    def test_killpg_graceful_already_dead(self) -> None:
        """SIGTERM 時に既にプロセスグループが存在しないケース。"""
        with patch("os.killpg", side_effect=OSError(errno.ESRCH, "No such process")):
            result = _killpg_graceful(pid=12345)

        assert result is False


# ── BA-017: update_pipeline 失敗時の子プロセス kill ───────────────────────


class TestStartCcTestFixKillOnPipelineFailure:
    """_start_cc_test_fix で update_pipeline 失敗時に子プロセスが kill される。"""

    def test_start_cc_test_fix_kills_proc_on_pipeline_save_failure(self) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 99999

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("engine.cc.update_pipeline", side_effect=RuntimeError("disk full")),
            patch("os.killpg") as mock_killpg,
            patch("tempfile.mkstemp", side_effect=[
                (10, "/tmp/gokrax-testfix-prompt.txt"),
                (11, "/tmp/gokrax-testfix-script.sh"),
            ]),
            patch("os.write"),
            patch("os.close"),
            patch("os.chmod"),
        ):
            from engine.cc import _start_cc_test_fix

            with pytest.raises(RuntimeError):
                _start_cc_test_fix(
                    project="test",
                    batch=[{"issue": 1}],
                    data={"repo_path": "/tmp", "cc_session_id": "test-session"},
                    pipeline_path=Path("/tmp/test-pipeline.json"),
                )

        mock_killpg.assert_any_call(99999, signal.SIGKILL)


class TestStartCcKillOnPipelineFailure:
    """_start_cc で update_pipeline 失敗時に子プロセスが kill される。"""

    def test_start_cc_kills_proc_on_pipeline_save_failure(self) -> None:
        """Popen 成功後の _save_cc_info 用 update_pipeline 失敗で子プロセスが kill される。

        load_pipeline に base_commit を含めることで _set_base の update_pipeline は
        呼ばれない。よって side_effect の RuntimeError は _save_cc_info 用の呼び出しで
        発火し、BA-017 の本丸の経路を検証する。
        """
        mock_proc = MagicMock()
        mock_proc.pid = 99999

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            # base_commit ありのため _set_base は呼ばれない → この1回が _save_cc_info 用
            patch("engine.cc.update_pipeline", side_effect=RuntimeError("disk full")),
            patch("engine.cc.load_pipeline", return_value={
                "batch": [{"issue": 1}],
                "repo_path": "/tmp",
                "base_commit": "abc123",  # base_commit あり → _set_base スキップ
            }),
            patch("os.killpg") as mock_killpg,
            patch("tempfile.mkstemp", side_effect=[
                (10, "/tmp/gokrax-plan-prompt.txt"),
                (11, "/tmp/gokrax-impl-prompt.txt"),
                (12, "/tmp/gokrax-cc-script.sh"),
            ]),
            patch("os.write"),
            patch("os.close"),
            patch("os.chmod"),
            patch("notify.fetch_issue_body", return_value="test body"),
        ):
            from engine.cc import _start_cc

            with pytest.raises(RuntimeError):
                _start_cc(
                    project="test",
                    batch=[{"issue": 1}],
                    gitlab="test/repo",
                    repo_path="/tmp",
                    pipeline_path=Path("/tmp/test-pipeline.json"),
                )

        mock_killpg.assert_any_call(99999, signal.SIGKILL)
