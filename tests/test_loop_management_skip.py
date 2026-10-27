"""tests/test_loop_management_skip.py — GOKRAX_SKIP_LOOP_MANAGEMENT のテスト (Issue #402)

env が真なら、loop と crontab を管理する関数は副作用を一切起こさない。
偽・未設定なら本物の経路を通る（陽性対照。Popen・kill・killpg・run は mock）。
pidfile の pid 4242424 と子 pgid 4242425 は、どちらも pid_max の上限 (2^22 = 4194304) より大きい。
mock が外れても、実在のプロセスにもプロセスグループにも当たらない。
"""
import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gokrax  # noqa: E402
import commands.dev.lifecycle as lifecycle  # noqa: E402

ENV = "GOKRAX_SKIP_LOOP_MANAGEMENT"
FAKE_PID = 4242424
FAKE_CHILD_PGID = 4242425


def _skip_line(what: str) -> str:
    """skip したときに stderr へ出る 1 行（改行込み）。"""
    return f"gokrax: {what} skipped (GOKRAX_SKIP_LOOP_MANAGEMENT is set)\n"


def _apply_env(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)


@pytest.fixture
def fake_loop_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pidfile = tmp_path / "loop.pid"
    pidfile.write_text(str(FAKE_PID))
    pgidfile = tmp_path / "child.pgid"
    pgidfile.write_text(str(FAKE_CHILD_PGID))
    monkeypatch.setattr(gokrax, "WATCHDOG_LOOP_PIDFILE", pidfile)
    monkeypatch.setattr(gokrax, "WATCHDOG_LOOP_CHILD_PGIDFILE", pgidfile)
    monkeypatch.setattr(lifecycle, "WATCHDOG_LOOP_PIDFILE", pidfile)
    return pidfile


@pytest.mark.parametrize("value", ["1", "true", " TRUE "])
class TestSkipped:
    def test_start_loop(self, value, monkeypatch, capsys):
        _apply_env(monkeypatch, value)
        with patch.object(gokrax, "_is_loop_running", return_value=False), \
             patch("subprocess.Popen") as popen, \
             patch("subprocess.run") as run:
            gokrax._start_loop()
        popen.assert_not_called()
        run.assert_not_called()
        assert capsys.readouterr().err == _skip_line("_start_loop")

    def test_stop_loop(self, value, monkeypatch, fake_loop_files, capsys):
        _apply_env(monkeypatch, value)
        with patch("os.kill") as kill, patch("os.killpg") as killpg:
            gokrax._stop_loop()
        kill.assert_not_called()
        killpg.assert_not_called()
        assert fake_loop_files.exists()
        assert capsys.readouterr().err == _skip_line("_stop_loop")

    def test_ensure_cron_entry(self, value, monkeypatch, capsys):
        _apply_env(monkeypatch, value)
        with patch("subprocess.run") as run:
            gokrax._ensure_cron_entry()
        run.assert_not_called()
        assert capsys.readouterr().err == _skip_line("_ensure_cron_entry")

    def test_remove_cron_entry(self, value, monkeypatch, capsys):
        _apply_env(monkeypatch, value)
        with patch("subprocess.run") as run:
            gokrax._remove_cron_entry()
        run.assert_not_called()
        assert capsys.readouterr().err == _skip_line("_remove_cron_entry")

    def test_cmd_disable_keeps_pidfile(self, value, monkeypatch, fake_loop_files, tmp_path, capsys):
        """本物の _stop_loop を通す。kill もしないし、pidfile も消さない。"""
        _apply_env(monkeypatch, value)
        with patch("os.kill") as kill, patch("os.killpg") as killpg, \
             patch.object(gokrax, "_any_pj_enabled", return_value=False), \
             patch.object(lifecycle, "update_pipeline", side_effect=lambda path, fn: fn({})), \
             patch.object(lifecycle, "get_path", return_value=tmp_path / "test.json"):
            args = MagicMock()
            args.project = "test"
            gokrax.cmd_disable(args)
        kill.assert_not_called()
        killpg.assert_not_called()
        assert fake_loop_files.exists()
        assert capsys.readouterr().err == _skip_line("_stop_loop") + _skip_line("WATCHDOG_LOOP_PIDFILE unlink")


@pytest.mark.parametrize("value", ["", "0", "false", " False ", None])
class TestNotSkipped:
    """陽性対照: env が偽・未設定なら本物の経路を通る。"""

    def test_start_loop_spawns_script(self, value, monkeypatch, capsys):
        _apply_env(monkeypatch, value)
        with patch.object(gokrax, "_is_loop_running", return_value=False), \
             patch.object(gokrax, "_ensure_cron_entry") as ensure, \
             patch("subprocess.Popen") as popen:
            gokrax._start_loop()
        popen.assert_called_once()
        assert popen.call_args.args[0] == ["nohup", "bash", str(gokrax.WATCHDOG_LOOP_SCRIPT)]
        ensure.assert_called_once_with()
        assert capsys.readouterr().err == ""

    def test_stop_loop_sends_sigterm(self, value, monkeypatch, fake_loop_files, capsys):
        _apply_env(monkeypatch, value)
        with patch("os.kill") as kill, patch("os.killpg") as killpg, \
             patch.object(gokrax, "_is_process_alive", return_value=False):
            gokrax._stop_loop()
        kill.assert_called_once_with(FAKE_PID, signal.SIGTERM)
        killpg.assert_called_once_with(FAKE_CHILD_PGID, signal.SIGKILL)
        assert capsys.readouterr().err == ""

    def test_ensure_cron_entry_writes_crontab(self, value, monkeypatch, capsys):
        _apply_env(monkeypatch, value)
        with patch("subprocess.run") as run:
            run.return_value.stdout = ""
            gokrax._ensure_cron_entry()
        assert [c.args[0] for c in run.call_args_list] == [["crontab", "-l"], ["crontab", "-"]]
        assert capsys.readouterr().err == ""

    def test_remove_cron_entry_rewrites_crontab(self, value, monkeypatch, capsys):
        _apply_env(monkeypatch, value)
        with patch("subprocess.run") as run:
            run.return_value.stdout = ""
            gokrax._remove_cron_entry()
        assert [c.args[0] for c in run.call_args_list] == [["crontab", "-l"], ["crontab", "-"]]
        assert capsys.readouterr().err == ""
