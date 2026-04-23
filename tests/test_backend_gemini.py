"""Tests for engine/backend_gemini.py."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
from engine import backend_gemini


AGENT = "reviewer1"


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch, tmp_path):
    """Reset module-level caches, isolate pid/profile dirs under tmp_path."""
    monkeypatch.setattr(backend_gemini, "_agent_config_cache", None)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "GEMINI_BIN", "gemini")

    pids = tmp_path / "pids"
    profiles = tmp_path / "agents"
    (profiles / AGENT).mkdir(parents=True)
    monkeypatch.setattr(backend_gemini, "GEMINI_PIDS_DIR", pids)
    monkeypatch.setattr(backend_gemini, "AGENT_PROFILES_DIR", profiles)
    yield


@pytest.fixture
def recorder(monkeypatch):
    """Record Popen / subprocess.run calls with configurable behavior."""
    popen_calls: list[list[str]] = []
    run_calls: list[list[str]] = []

    class FakeProc:
        def __init__(self, pid=4242):
            self.pid = pid
            self._waited = 0

        def wait(self, timeout=None):
            self._waited += 1
            return 0

    def fake_popen(cmd, **kwargs):
        popen_calls.append(list(cmd))
        return FakeProc()

    class FakeRunResult:
        def __init__(self, stdout="No sessions found for this project.\n", returncode=0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    run_result_holder = {"result": FakeRunResult()}

    def fake_run(cmd, **kwargs):
        run_calls.append(list(cmd))
        return run_result_holder["result"]

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run", fake_run)

    return {
        "popen_calls": popen_calls,
        "run_calls": run_calls,
        "set_run_result": lambda r: run_result_holder.update({"result": r}),
        "FakeRunResult": FakeRunResult,
    }


def _profile_dir() -> Path:
    return backend_gemini.AGENT_PROFILES_DIR / AGENT


# ===========================================================================
# send()
# ===========================================================================

class TestSend:
    def test_initial_no_r_latest(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: 0)
        assert backend_gemini.send(AGENT, "hi", 30) is True
        argv = recorder["popen_calls"][0]
        assert "-r" not in argv and "latest" not in argv

    def test_continuation_has_r_latest(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: 3)
        assert backend_gemini.send(AGENT, "hi", 30) is True
        argv = recorder["popen_calls"][0]
        assert argv[-2:] == ["-r", "latest"]

    def test_list_failure_is_first_run(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: None)
        backend_gemini.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]
        assert "-r" not in argv

    def test_argv_no_output_format(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: 0)
        backend_gemini.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]
        assert "--output-format" not in argv

    def test_dry_run_no_popen(self, recorder, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", True)
        assert backend_gemini.send(AGENT, "hi", 30) is True
        assert recorder["popen_calls"] == []

    def test_pid_persisted(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: 0)
        backend_gemini.send(AGENT, "hi", 30)
        pid_file = backend_gemini._pid_path(AGENT)
        assert pid_file.read_text() == "4242"

    def test_model_string(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: 0)
        monkeypatch.setattr(backend_gemini, "_load_config",
                            lambda: {AGENT: {"model": "gemini-2.5-pro"}})
        backend_gemini.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]
        idx = argv.index("-m")
        assert argv[idx + 1] == "gemini-2.5-pro"

    @pytest.mark.parametrize("model", ["", "   ", 123, None])
    def test_model_skipped_when_invalid(self, recorder, monkeypatch, model):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: 0)
        profile: dict = {} if model is None else {"model": model}
        monkeypatch.setattr(backend_gemini, "_load_config", lambda: {AGENT: profile})
        backend_gemini.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]
        assert "-m" not in argv

    def test_profile_dir_missing(self, recorder, monkeypatch, caplog, tmp_path):
        # move profile dir away
        empty_dir = tmp_path / "empty_agents"
        empty_dir.mkdir()
        monkeypatch.setattr(backend_gemini, "AGENT_PROFILES_DIR", empty_dir)
        with caplog.at_level(logging.WARNING, logger="engine.backend_gemini"):
            result = backend_gemini.send(AGENT, "hi", 30)
        assert result is False
        assert recorder["popen_calls"] == []
        assert any("profile dir" in r.message for r in caplog.records)

    def test_success_path_no_terminate(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: 0)
        terminate_calls: list = []
        monkeypatch.setattr(
            backend_gemini, "_terminate_pid_tree",
            lambda *a, **k: terminate_calls.append(1) or True,
        )
        assert backend_gemini.send(AGENT, "hi", 30) is True
        assert terminate_calls == []

    def test_pid_write_failure_terminates(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: 0)
        terminate_calls: list[tuple] = []

        def fake_terminate(pid, agent_id, proc=None):
            terminate_calls.append((pid, agent_id, proc))
            return True

        monkeypatch.setattr(backend_gemini, "_terminate_pid_tree", fake_terminate)

        orig = Path.write_text

        def fake_write_text(self, data, *a, **k):
            if self.name.endswith(".pid"):
                raise OSError("disk full")
            return orig(self, data, *a, **k)

        monkeypatch.setattr(Path, "write_text", fake_write_text)

        result = backend_gemini.send(AGENT, "hi", 30)
        assert result is False
        assert len(terminate_calls) == 1
        pid, agent_id, proc = terminate_calls[0]
        assert pid == 4242
        assert agent_id == AGENT
        assert proc is not None


# ===========================================================================
# reset_session()
# ===========================================================================

class TestResetSession:
    def test_normal_deletion_loop(self, recorder, monkeypatch):
        counts = iter([3, 2, 1, 0])
        monkeypatch.setattr(backend_gemini, "_count_sessions",
                            lambda cwd: next(counts))
        # no pid file -> no termination
        backend_gemini.reset_session(AGENT)
        del_args = [c for c in recorder["run_calls"] if "--delete-session" in c]
        assert len(del_args) == 3
        assert [c[-1] for c in del_args] == ["3", "2", "1"]

    def test_safety_cap(self, recorder, monkeypatch, caplog):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: 5)
        with caplog.at_level(logging.WARNING, logger="engine.backend_gemini"):
            backend_gemini.reset_session(AGENT)
        del_args = [c for c in recorder["run_calls"] if "--delete-session" in c]
        assert len(del_args) == 100
        assert any("safety cap" in r.message for r in caplog.records)
        assert any("remaining sessions" in r.message for r in caplog.records)

    def test_profile_dir_missing(self, recorder, monkeypatch, tmp_path):
        empty_dir = tmp_path / "empty_agents2"
        empty_dir.mkdir()
        monkeypatch.setattr(backend_gemini, "AGENT_PROFILES_DIR", empty_dir)
        # put pid file so unlink runs
        backend_gemini.GEMINI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_gemini._pid_path(AGENT).write_text("9999")
        backend_gemini.reset_session(AGENT)
        assert recorder["run_calls"] == []
        assert not backend_gemini._pid_path(AGENT).exists()

    def test_list_failure_aborts(self, recorder, monkeypatch, caplog):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: None)
        with caplog.at_level(logging.WARNING, logger="engine.backend_gemini"):
            backend_gemini.reset_session(AGENT)
        del_args = [c for c in recorder["run_calls"] if "--delete-session" in c]
        assert len(del_args) == 0
        assert any("failed to list sessions" in r.message for r in caplog.records)

    def test_terminates_live_process_before_deletion(self, monkeypatch):
        counts = iter([2, 1, 0])
        monkeypatch.setattr(backend_gemini, "_count_sessions",
                            lambda cwd: next(counts))
        monkeypatch.setattr(backend_gemini, "_is_gemini_pid_alive",
                            lambda pid: True)

        call_order: list[str] = []

        def fake_terminate(pid, agent_id, proc=None):
            call_order.append(f"terminate:{pid}")
            return True

        def fake_run(cmd, **kwargs):
            if "--delete-session" in cmd:
                call_order.append(f"delete:{cmd[-1]}")
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            return r

        monkeypatch.setattr(backend_gemini, "_terminate_pid_tree", fake_terminate)
        monkeypatch.setattr(subprocess, "run", fake_run)

        backend_gemini.GEMINI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_gemini._pid_path(AGENT).write_text("5555")

        backend_gemini.reset_session(AGENT)
        assert call_order == ["terminate:5555", "delete:2", "delete:1"]

    def test_terminate_failure_aborts_deletion(self, recorder, monkeypatch, caplog):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: 5)
        monkeypatch.setattr(backend_gemini, "_is_gemini_pid_alive",
                            lambda pid: True)
        monkeypatch.setattr(backend_gemini, "_terminate_pid_tree",
                            lambda *a, **k: False)
        backend_gemini.GEMINI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_gemini._pid_path(AGENT).write_text("5555")
        with caplog.at_level(logging.WARNING, logger="engine.backend_gemini"):
            backend_gemini.reset_session(AGENT)
        del_args = [c for c in recorder["run_calls"] if "--delete-session" in c]
        assert len(del_args) == 0
        # pid file must NOT be unlinked when termination failed — aborting keeps
        # the recorded live process visible for the next reset attempt.
        assert backend_gemini._pid_path(AGENT).exists()
        assert any("failed to terminate" in r.message for r in caplog.records)

    def test_skip_terminate_when_no_pid(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: 0)
        calls: list[int] = []
        monkeypatch.setattr(backend_gemini, "_terminate_pid_tree",
                            lambda *a, **k: calls.append(1))
        backend_gemini.reset_session(AGENT)
        assert calls == []

    def test_skip_terminate_when_pid_dead(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_gemini, "_count_sessions", lambda cwd: 0)
        monkeypatch.setattr(backend_gemini, "_is_gemini_pid_alive",
                            lambda pid: False)
        calls: list[int] = []
        monkeypatch.setattr(backend_gemini, "_terminate_pid_tree",
                            lambda *a, **k: calls.append(1))
        backend_gemini.GEMINI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_gemini._pid_path(AGENT).write_text("7777")
        backend_gemini.reset_session(AGENT)
        assert calls == []


# ===========================================================================
# _terminate_pid_tree()
# ===========================================================================

class TestTerminatePidTree:
    def test_sigterm_success(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(backend_gemini.os, "getpgid", lambda p: p)
        monkeypatch.setattr(backend_gemini.os, "killpg",
                            lambda pgid, sig: calls.append(("killpg", pgid, sig)))
        proc = MagicMock()
        proc.wait.return_value = 0
        backend_gemini._terminate_pid_tree(1234, AGENT, proc=proc)
        # Only SIGTERM sent
        assert len(calls) == 1
        assert calls[0][2] == backend_gemini.signal.SIGTERM

    def test_sigterm_then_sigkill(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(backend_gemini.os, "getpgid", lambda p: p)
        monkeypatch.setattr(backend_gemini.os, "killpg",
                            lambda pgid, sig: calls.append(sig))
        proc = MagicMock()
        proc.wait.side_effect = [subprocess.TimeoutExpired("g", 5), 0]
        backend_gemini._terminate_pid_tree(1234, AGENT, proc=proc)
        assert backend_gemini.signal.SIGTERM in calls
        assert backend_gemini.signal.SIGKILL in calls

    def test_getpgid_fallback(self, monkeypatch, caplog):
        calls: list = []
        monkeypatch.setattr(backend_gemini.os, "getpgid",
                            lambda p: (_ for _ in ()).throw(OSError("boom")))
        monkeypatch.setattr(backend_gemini.os, "killpg",
                            lambda pgid, sig: calls.append((pgid, sig)))
        proc = MagicMock()
        proc.wait.return_value = 0
        with caplog.at_level(logging.WARNING, logger="engine.backend_gemini"):
            backend_gemini._terminate_pid_tree(1234, AGENT, proc=proc)
        assert calls[0][0] == 1234  # fallback to pid
        assert any("getpgid" in r.message for r in caplog.records)

    def test_sigterm_process_lookup_returns_early(self, monkeypatch):
        monkeypatch.setattr(backend_gemini.os, "getpgid", lambda p: p)
        calls: list = []

        def killpg(pgid, sig):
            calls.append(sig)
            if sig == backend_gemini.signal.SIGTERM:
                raise ProcessLookupError()

        monkeypatch.setattr(backend_gemini.os, "killpg", killpg)
        proc = MagicMock()
        backend_gemini._terminate_pid_tree(1234, AGENT, proc=proc)
        assert backend_gemini.signal.SIGKILL not in calls
        proc.wait.assert_not_called()

    def test_sigterm_oserror_continues(self, monkeypatch, caplog):
        monkeypatch.setattr(backend_gemini.os, "getpgid", lambda p: p)
        calls: list = []

        def killpg(pgid, sig):
            calls.append(sig)
            if sig == backend_gemini.signal.SIGTERM:
                raise OSError("perm")

        monkeypatch.setattr(backend_gemini.os, "killpg", killpg)
        proc = MagicMock()
        proc.wait.side_effect = [subprocess.TimeoutExpired("g", 5), 0]
        with caplog.at_level(logging.WARNING, logger="engine.backend_gemini"):
            backend_gemini._terminate_pid_tree(1234, AGENT, proc=proc)
        assert backend_gemini.signal.SIGKILL in calls
        assert any("SIGTERM" in r.message for r in caplog.records)

    def test_sigkill_oserror_logged(self, monkeypatch, caplog):
        monkeypatch.setattr(backend_gemini.os, "getpgid", lambda p: p)

        def killpg(pgid, sig):
            if sig == backend_gemini.signal.SIGKILL:
                raise OSError("kperm")

        monkeypatch.setattr(backend_gemini.os, "killpg", killpg)
        proc = MagicMock()
        proc.wait.side_effect = [subprocess.TimeoutExpired("g", 5), 0]
        with caplog.at_level(logging.WARNING, logger="engine.backend_gemini"):
            backend_gemini._terminate_pid_tree(1234, AGENT, proc=proc)
        assert any("SIGKILL" in r.message for r in caplog.records)

    def test_proc_none_polls_alive(self, monkeypatch):
        monkeypatch.setattr(backend_gemini.os, "getpgid", lambda p: p)
        monkeypatch.setattr(backend_gemini.os, "killpg", lambda pgid, sig: None)
        alive_seq = iter([True, False])
        monkeypatch.setattr(backend_gemini, "_is_gemini_pid_alive",
                            lambda pid: next(alive_seq))
        monkeypatch.setattr(backend_gemini.time, "sleep", lambda s: None)
        # should return without SIGKILL
        backend_gemini._terminate_pid_tree(1234, AGENT, proc=None)


# ===========================================================================
# is_inactive()
# ===========================================================================

class TestIsInactive:
    def test_pid_alive(self, monkeypatch):
        backend_gemini.GEMINI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_gemini._pid_path(AGENT).write_text("111")
        monkeypatch.setattr(backend_gemini, "_is_gemini_pid_alive", lambda p: True)
        assert backend_gemini.is_inactive(AGENT) is False

    def test_no_pid_file(self):
        assert backend_gemini.is_inactive(AGENT) is True

    def test_pid_file_garbage(self):
        backend_gemini.GEMINI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_gemini._pid_path(AGENT).write_text("not-a-number")
        assert backend_gemini.is_inactive(AGENT) is True

    def test_pid_dead(self, monkeypatch):
        backend_gemini.GEMINI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_gemini._pid_path(AGENT).write_text("222")
        monkeypatch.setattr(backend_gemini, "_is_gemini_pid_alive", lambda p: False)
        assert backend_gemini.is_inactive(AGENT) is True

    def test_cc_running(self):
        assert backend_gemini.is_inactive(AGENT, cc_running=True) is False


# ===========================================================================
# ping()
# ===========================================================================

class TestPing:
    def test_always_true(self):
        assert backend_gemini.ping(AGENT, 5) is True
        assert backend_gemini.ping("whatever", 0) is True


# ===========================================================================
# _count_sessions()
# ===========================================================================

class TestCountSessions:
    def _run_mock(self, monkeypatch, stdout: str, returncode: int = 0,
                  raise_exc=None):
        def fake_run(cmd, **kwargs):
            if raise_exc is not None:
                raise raise_exc
            r = MagicMock()
            r.stdout = stdout
            r.returncode = returncode
            return r
        monkeypatch.setattr(subprocess, "run", fake_run)

    def test_count_4(self, monkeypatch, tmp_path):
        self._run_mock(monkeypatch, "Available sessions for this project (4):\n  1. ...\n")
        assert backend_gemini._count_sessions(tmp_path) == 4

    def test_count_0_header(self, monkeypatch, tmp_path):
        self._run_mock(monkeypatch, "Available sessions for this project (0):\n")
        assert backend_gemini._count_sessions(tmp_path) == 0

    def test_no_sessions_message(self, monkeypatch, tmp_path):
        self._run_mock(monkeypatch, "No sessions found for this project.\n")
        assert backend_gemini._count_sessions(tmp_path) == 0

    def test_keychain_warning_plus_header(self, monkeypatch, tmp_path):
        self._run_mock(
            monkeypatch,
            "Keychain boom\nAvailable sessions for this project (2):\n  1. a\n  2. b\n",
        )
        assert backend_gemini._count_sessions(tmp_path) == 2

    def test_whitespace_before_paren(self, monkeypatch, tmp_path):
        self._run_mock(monkeypatch, "Available sessions for this project  (5):")
        assert backend_gemini._count_sessions(tmp_path) == 5

    def test_unrecognized_output(self, monkeypatch, tmp_path):
        self._run_mock(monkeypatch, "unrelated output")
        assert backend_gemini._count_sessions(tmp_path) is None

    def test_drift_logs_warning(self, monkeypatch, tmp_path, caplog):
        self._run_mock(monkeypatch, "totally unexpected output")
        with caplog.at_level(logging.WARNING, logger="engine.backend_gemini"):
            assert backend_gemini._count_sessions(tmp_path) is None
        assert any("drift" in r.message for r in caplog.records)

    def test_empty_stdout(self, monkeypatch, tmp_path):
        self._run_mock(monkeypatch, "")
        assert backend_gemini._count_sessions(tmp_path) is None

    def test_oserror(self, monkeypatch, tmp_path):
        self._run_mock(monkeypatch, "", raise_exc=OSError("boom"))
        assert backend_gemini._count_sessions(tmp_path) is None

    def test_timeout(self, monkeypatch, tmp_path):
        self._run_mock(monkeypatch, "",
                       raise_exc=subprocess.TimeoutExpired("x", 1))
        assert backend_gemini._count_sessions(tmp_path) is None

    def test_nonzero_exit(self, monkeypatch, tmp_path):
        self._run_mock(monkeypatch, "Available sessions for this project (3):",
                       returncode=1)
        assert backend_gemini._count_sessions(tmp_path) is None


# ===========================================================================
# _is_gemini_pid_alive()
# ===========================================================================

class TestIsGeminiPidAlive:
    def _setup_proc(self, monkeypatch, exists: bool, cmdline: bytes | None,
                    read_exc=None):
        class FakePath:
            def __init__(self, p):
                self._p = str(p)

            def __truediv__(self, other):
                return FakePath(f"{self._p}/{other}")

            def exists(self):
                return exists

            def read_bytes(self):
                if read_exc is not None:
                    raise read_exc
                return cmdline

        monkeypatch.setattr(backend_gemini, "Path", FakePath)

    def test_proc_missing(self, monkeypatch):
        self._setup_proc(monkeypatch, exists=False, cmdline=None)
        assert backend_gemini._is_gemini_pid_alive(123) is False

    def test_cmdline_gemini(self, monkeypatch):
        self._setup_proc(monkeypatch, exists=True,
                         cmdline=b"gemini\x00-p\x00hello\x00")
        assert backend_gemini._is_gemini_pid_alive(123) is True

    def test_cmdline_gemini_full_path(self, monkeypatch):
        self._setup_proc(monkeypatch, exists=True,
                         cmdline=b"/usr/local/bin/gemini\x00--list-sessions\x00")
        assert backend_gemini._is_gemini_pid_alive(123) is True

    def test_cmdline_other(self, monkeypatch):
        self._setup_proc(monkeypatch, exists=True,
                         cmdline=b"python\x00my-script.py\x00")
        assert backend_gemini._is_gemini_pid_alive(123) is False

    def test_cmdline_empty(self, monkeypatch):
        self._setup_proc(monkeypatch, exists=True, cmdline=b"")
        assert backend_gemini._is_gemini_pid_alive(123) is False

    def test_cmdline_non_utf8(self, monkeypatch):
        # Binary junk followed by gemini — the junk token raises UnicodeDecodeError
        # and is skipped; the gemini token still matches.
        self._setup_proc(monkeypatch, exists=True,
                         cmdline=b"\xff\xfe\x00gemini\x00")
        assert backend_gemini._is_gemini_pid_alive(123) is True

    def test_read_oserror(self, monkeypatch):
        self._setup_proc(monkeypatch, exists=True, cmdline=None,
                         read_exc=OSError("perm"))
        assert backend_gemini._is_gemini_pid_alive(123) is False


# ===========================================================================
# _rebuild_gemini_md()
# ===========================================================================

class TestRebuildGeminiMd:
    def _enable_compile(self, monkeypatch):
        monkeypatch.setattr(backend_gemini, "_load_config",
                            lambda: {AGENT: {"compile-startup-md": True}})

    def test_compile_generates(self, monkeypatch):
        self._enable_compile(monkeypatch)
        pd = _profile_dir()
        (pd / "IDENTITY.md").write_text("id")
        (pd / "INSTRUCTION.md").write_text("instr")
        (pd / "MEMORY.md").write_text("mem")
        backend_gemini._rebuild_gemini_md(AGENT)
        gmd = pd / "GEMINI.md"
        assert gmd.exists()
        assert (pd / ".gemini_hash").exists()
        body = gmd.read_text()
        assert "id" in body and "instr" in body and "mem" in body

    def test_hash_skip_on_unchanged(self, monkeypatch):
        self._enable_compile(monkeypatch)
        pd = _profile_dir()
        (pd / "IDENTITY.md").write_text("id")
        backend_gemini._rebuild_gemini_md(AGENT)
        gmd = pd / "GEMINI.md"
        mtime = gmd.stat().st_mtime_ns
        # call again; file should not be rewritten (hash matches)
        backend_gemini._rebuild_gemini_md(AGENT)
        assert gmd.stat().st_mtime_ns == mtime

    def test_all_empty_deletes(self, monkeypatch):
        self._enable_compile(monkeypatch)
        pd = _profile_dir()
        (pd / "GEMINI.md").write_text("stale")
        (pd / ".gemini_hash").write_text("deadbeef")
        # no IDENTITY/INSTRUCTION/MEMORY => all empty
        backend_gemini._rebuild_gemini_md(AGENT)
        assert not (pd / "GEMINI.md").exists()
        assert not (pd / ".gemini_hash").exists()

    def test_compile_false_early_return_preserves(self, monkeypatch):
        monkeypatch.setattr(backend_gemini, "_load_config",
                            lambda: {AGENT: {"compile-startup-md": False}})
        pd = _profile_dir()
        (pd / "GEMINI.md").write_text("preexisting")
        backend_gemini._rebuild_gemini_md(AGENT)
        assert (pd / "GEMINI.md").read_text() == "preexisting"

    def test_non_bool_compile_flag(self, monkeypatch, caplog):
        monkeypatch.setattr(backend_gemini, "_load_config",
                            lambda: {AGENT: {"compile-startup-md": "yes"}})
        pd = _profile_dir()
        (pd / "GEMINI.md").write_text("preexisting")
        with caplog.at_level(logging.WARNING, logger="engine.backend_gemini"):
            backend_gemini._rebuild_gemini_md(AGENT)
        assert (pd / "GEMINI.md").read_text() == "preexisting"
        assert any("non-bool" in r.message for r in caplog.records)
