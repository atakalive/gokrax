"""tests/test_watchdog_loop_signal.py — watchdog-loop.sh の SIGTERM 伝搬テスト

設計: time.sleep / busy polling を使わず、FIFO・proc.wait・pidfd_open+select で
純粋にイベント駆動の同期を行う。conftest の time.sleep mock の影響を一切受けない。
"""
import os
import select
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "watchdog-loop.sh"

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "linux",
        reason="watchdog-loop.sh は Linux 専用 (FIFO/pidfd_open は Linux 5.3+, py3.9+)",
    ),
    pytest.mark.skipif(
        not hasattr(os, "pidfd_open"),
        reason="pidfd_open requires Python 3.9+ on Linux 5.3+",
    ),
]


def _wait_pid_death(pid: int, timeout: float) -> bool:
    """pidfd_open + select で pid の死亡を待つ。time.sleep 不使用。"""
    try:
        pidfd = os.pidfd_open(pid)
    except (ProcessLookupError, OSError):
        return True
    try:
        rlist, _, _ = select.select([pidfd], [], [], timeout)
        return bool(rlist)
    finally:
        os.close(pidfd)


def _is_alive(pid: int) -> bool:
    """zombie は除外して生死判定。/proc/PID/status の State 行で判別。"""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    return "Z" not in line
        return False
    except OSError:
        return False


def _build_script(tmp_path: Path, dummy_python: str) -> Path:
    """watchdog-loop.sh のテスト用コピー (パスを tmp に、python3 を dummy に差し替え)。

    dummy script は別ファイルに書き出して `python3 path/to/dummy.py` で実行する。
    bash -c '...' の単一引用符内に Python repr を埋め込むと引用符が衝突するため。
    """
    dummy_path = tmp_path / "dummy.py"
    dummy_path.write_text(dummy_python)

    script = tmp_path / "watchdog-loop.sh"
    src = SCRIPT.read_text()
    src = src.replace("/tmp/gokrax-watchdog-loop.lock", str(tmp_path / "loop.lock"))
    src = src.replace("/tmp/gokrax-watchdog-loop.pid", str(tmp_path / "loop.pid"))
    src = src.replace("/tmp/gokrax-watchdog-loop-child.pgid", str(tmp_path / "child.pgid"))
    src = src.replace("/tmp/gokrax-watchdog.lock", str(tmp_path / "watchdog.lock"))
    src = src.replace("/tmp/gokrax-watchdog.log", str(tmp_path / "watchdog.log"))
    src = src.replace(
        "exec python3 watchdog.py",
        f"exec python3 {dummy_path}",
    )
    script.write_text(src)
    script.chmod(0o755)
    return script


def test_sigterm_kills_child_python(tmp_path):
    """SIGTERM 受信時、watchdog-loop の子プロセスツリー全体が即座に殺される。"""
    ready_fifo = tmp_path / "ready.fifo"
    os.mkfifo(ready_fifo)

    dummy = (
        "import os; "
        f"open({str(ready_fifo)!r}, 'w').write(str(os.getpid())); "
        "os.pause()"
    )
    script = _build_script(tmp_path, dummy_python=dummy)
    pidfile = tmp_path / "loop.pid"

    proc = subprocess.Popen(
        ["bash", str(script)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        with open(ready_fifo, "r") as f:
            child_pid = int(f.read().strip())

        outer_pid = int(pidfile.read_text().strip())
        assert outer_pid == proc.pid
        assert _is_alive(child_pid), "child python should be alive before SIGTERM"

        os.kill(outer_pid, signal.SIGTERM)

        proc.wait(timeout=3.0)
        assert proc.returncode == 0

        assert _wait_pid_death(child_pid, timeout=1.0), \
            f"child {child_pid} did not die after outer bash exit"
        assert not _is_alive(child_pid)

        assert not pidfile.exists()
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def test_sigterm_during_sleep_exits_cleanly(tmp_path):
    """sleep INTERVAL 中の SIGTERM でもクリーンに exit する (CHILD_PGID 空のパス)。"""
    done_fifo = tmp_path / "done.fifo"
    os.mkfifo(done_fifo)

    dummy = (
        f"open({str(done_fifo)!r}, 'w').write('done')"
    )
    script = _build_script(tmp_path, dummy_python=dummy)
    src = script.read_text().replace("INTERVAL=10", "INTERVAL=30")
    script.write_text(src)

    pidfile = tmp_path / "loop.pid"
    proc = subprocess.Popen(
        ["bash", str(script)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        with open(done_fifo, "r") as f:
            assert f.read().strip() == "done"

        outer_pid = int(pidfile.read_text().strip())
        os.kill(outer_pid, signal.SIGTERM)

        proc.wait(timeout=2.0)
        assert proc.returncode == 0
        assert not pidfile.exists()
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def test_script_syntax():
    """watchdog-loop.sh の bash 構文チェック。"""
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"syntax error: {result.stderr}"
