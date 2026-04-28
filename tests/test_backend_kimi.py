"""Tests for engine/backend_kimi.py."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
from engine import backend_kimi
from engine.backend_types import SendResult


AGENT = "reviewer1"


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch, tmp_path):
    """Reset module-level caches, isolate pid/profile dirs under tmp_path."""
    monkeypatch.setattr(backend_kimi, "_agent_config_cache", None)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "KIMI_BIN", "kimi")

    pids = tmp_path / "pids"
    profiles = tmp_path / "agents"
    (profiles / AGENT).mkdir(parents=True)
    monkeypatch.setattr(backend_kimi, "KIMI_PIDS_DIR", pids)
    monkeypatch.setattr(backend_kimi, "AGENT_PROFILES_DIR", profiles)
    yield


@pytest.fixture
def recorder(monkeypatch):
    """Record Popen calls with configurable behavior."""
    popen_calls: list[list[str]] = []

    class FakeProc:
        def __init__(self, pid=4242):
            self.pid = pid

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        popen_calls.append(list(cmd))
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return {"popen_calls": popen_calls}


def _profile_dir() -> Path:
    return backend_kimi.AGENT_PROFILES_DIR / AGENT


# ===========================================================================
# send()
# ===========================================================================

class TestSend:
    def test_initial_no_continue(self, recorder):
        assert backend_kimi.send(AGENT, "hi", 30) is SendResult.OK
        argv = recorder["popen_calls"][0]
        assert "-C" not in argv

    def test_continuation_has_continue(self, recorder):
        # Pre-create the marker
        backend_kimi.KIMI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_kimi._session_marker_path(AGENT).touch()
        assert backend_kimi.send(AGENT, "hi", 30) is SendResult.OK
        argv = recorder["popen_calls"][0]
        assert argv[-1] == "-C"

    def test_quiet_in_argv(self, recorder):
        backend_kimi.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]
        assert "--quiet" in argv

    def test_yolo_in_argv(self, recorder):
        """-y must always be present to guarantee tool-use auto-approval."""
        backend_kimi.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]
        assert "-y" in argv

    def test_prompt_arg(self, recorder):
        backend_kimi.send(AGENT, "hello world", 30)
        argv = recorder["popen_calls"][0]
        idx = argv.index("-p")
        assert argv[idx + 1] == "hello world"

    def test_model_string(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_kimi, "_load_config",
                            lambda: {AGENT: {"model": "kimi-k2"}})
        backend_kimi.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]
        idx = argv.index("-m")
        assert argv[idx + 1] == "kimi-k2"

    @pytest.mark.parametrize("model", ["", "   ", 123, None])
    def test_model_skipped_when_invalid(self, recorder, monkeypatch, model):
        profile: dict = {} if model is None else {"model": model}
        monkeypatch.setattr(backend_kimi, "_load_config", lambda: {AGENT: profile})
        backend_kimi.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]
        assert "-m" not in argv

    def test_dry_run_no_popen(self, recorder, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", True)
        assert backend_kimi.send(AGENT, "hi", 30) is SendResult.OK
        assert recorder["popen_calls"] == []

    def test_pid_persisted(self, recorder):
        backend_kimi.send(AGENT, "hi", 30)
        assert backend_kimi._pid_path(AGENT).read_text() == "4242"

    def test_marker_created_on_success(self, recorder):
        assert not backend_kimi._session_marker_path(AGENT).exists()
        backend_kimi.send(AGENT, "hi", 30)
        assert backend_kimi._session_marker_path(AGENT).exists()

    def test_profile_dir_missing(self, recorder, monkeypatch, caplog, tmp_path):
        empty_dir = tmp_path / "empty_agents"
        empty_dir.mkdir()
        monkeypatch.setattr(backend_kimi, "AGENT_PROFILES_DIR", empty_dir)
        with caplog.at_level(logging.WARNING, logger="engine.backend_kimi"):
            result = backend_kimi.send(AGENT, "hi", 30)
        assert result is SendResult.FAIL
        assert recorder["popen_calls"] == []
        assert any("profile dir" in r.message for r in caplog.records)

    def test_pid_write_failure_terminates_and_no_marker(self, recorder, monkeypatch):
        terminate_calls: list[tuple] = []

        def fake_terminate(pid, agent_id, proc=None):
            terminate_calls.append((pid, agent_id, proc))
            return True

        monkeypatch.setattr(backend_kimi, "_terminate_pid_tree", fake_terminate)

        orig = Path.write_text

        def fake_write_text(self, data, *a, **k):
            if self.name.endswith(".pid"):
                raise OSError("disk full")
            return orig(self, data, *a, **k)

        monkeypatch.setattr(Path, "write_text", fake_write_text)

        result = backend_kimi.send(AGENT, "hi", 30)
        assert result is SendResult.FAIL
        assert len(terminate_calls) == 1
        # marker MUST NOT be created when pid persistence fails
        assert not backend_kimi._session_marker_path(AGENT).exists()


# ===========================================================================
# is_inactive()
# ===========================================================================

class TestIsInactive:
    def test_no_pid_file(self):
        assert backend_kimi.is_inactive(AGENT) is True

    def test_pid_alive(self, monkeypatch):
        backend_kimi.KIMI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_kimi._pid_path(AGENT).write_text("111")
        monkeypatch.setattr(backend_kimi, "_is_kimi_pid_alive", lambda p: True)
        assert backend_kimi.is_inactive(AGENT) is False

    def test_pid_dead(self, monkeypatch):
        backend_kimi.KIMI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_kimi._pid_path(AGENT).write_text("222")
        monkeypatch.setattr(backend_kimi, "_is_kimi_pid_alive", lambda p: False)
        assert backend_kimi.is_inactive(AGENT) is True

    def test_cc_running(self):
        assert backend_kimi.is_inactive(AGENT, cc_running=True) is False


# ===========================================================================
# _is_kimi_pid_alive — token matching
# ===========================================================================

class TestIsKimiPidAlive:
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

        monkeypatch.setattr(backend_kimi, "Path", FakePath)

    def test_proc_missing(self, monkeypatch):
        self._setup_proc(monkeypatch, exists=False, cmdline=None)
        assert backend_kimi._is_kimi_pid_alive(123) is False

    def test_cmdline_kimi_exact(self, monkeypatch):
        self._setup_proc(monkeypatch, exists=True,
                         cmdline=b"kimi\x00--quiet\x00-p\x00hi\x00")
        assert backend_kimi._is_kimi_pid_alive(123) is True

    def test_cmdline_kimi_full_path(self, monkeypatch):
        self._setup_proc(monkeypatch, exists=True,
                         cmdline=b"/home/me/.local/bin/kimi\x00--quiet\x00")
        assert backend_kimi._is_kimi_pid_alive(123) is True

    def test_cmdline_other(self, monkeypatch):
        self._setup_proc(monkeypatch, exists=True,
                         cmdline=b"python\x00my-script.py\x00")
        assert backend_kimi._is_kimi_pid_alive(123) is False

    def test_cmdline_empty(self, monkeypatch):
        self._setup_proc(monkeypatch, exists=True, cmdline=b"")
        assert backend_kimi._is_kimi_pid_alive(123) is False


# ===========================================================================
# reset_session()
# ===========================================================================

class TestResetSession:
    def test_no_pid_no_marker(self, recorder):
        # Should not raise
        backend_kimi.reset_session(AGENT)

    def test_terminates_live_process(self, monkeypatch):
        monkeypatch.setattr(backend_kimi, "_is_kimi_pid_alive", lambda pid: True)
        terminate_calls: list = []
        monkeypatch.setattr(
            backend_kimi, "_terminate_pid_tree",
            lambda pid, agent_id, proc=None: terminate_calls.append(pid) or True,
        )
        backend_kimi.KIMI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_kimi._pid_path(AGENT).write_text("5555")
        backend_kimi._session_marker_path(AGENT).touch()

        backend_kimi.reset_session(AGENT)
        assert terminate_calls == [5555]
        assert not backend_kimi._pid_path(AGENT).exists()
        assert not backend_kimi._session_marker_path(AGENT).exists()

    def test_terminate_failure_aborts(self, monkeypatch):
        monkeypatch.setattr(backend_kimi, "_is_kimi_pid_alive", lambda pid: True)
        monkeypatch.setattr(backend_kimi, "_terminate_pid_tree",
                            lambda *a, **k: False)
        backend_kimi.KIMI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_kimi._pid_path(AGENT).write_text("5555")
        backend_kimi._session_marker_path(AGENT).touch()

        backend_kimi.reset_session(AGENT)
        # pid file and marker preserved on terminate failure
        assert backend_kimi._pid_path(AGENT).exists()
        assert backend_kimi._session_marker_path(AGENT).exists()

    def test_dead_pid_unlinks(self, monkeypatch):
        monkeypatch.setattr(backend_kimi, "_is_kimi_pid_alive", lambda pid: False)
        terminate_calls: list = []
        monkeypatch.setattr(backend_kimi, "_terminate_pid_tree",
                            lambda *a, **k: terminate_calls.append(1))
        backend_kimi.KIMI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_kimi._pid_path(AGENT).write_text("9999")
        backend_kimi._session_marker_path(AGENT).touch()

        backend_kimi.reset_session(AGENT)
        assert terminate_calls == []
        assert not backend_kimi._pid_path(AGENT).exists()
        assert not backend_kimi._session_marker_path(AGENT).exists()

    def test_calls_rebuild(self, monkeypatch):
        called: list = []
        monkeypatch.setattr(backend_kimi, "_rebuild_kimi_md",
                            lambda agent_id: called.append(agent_id))
        backend_kimi.reset_session(AGENT)
        assert called == [AGENT]


# ===========================================================================
# ping()
# ===========================================================================

class TestPing:
    def test_always_true(self):
        assert backend_kimi.ping(AGENT, 5) is True


# ===========================================================================
# _load_config()
# ===========================================================================

class TestLoadConfig:
    def test_missing_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(backend_kimi, "KIMI_AGENT_CONFIG", tmp_path / "missing.json")
        assert backend_kimi._load_config() == {}

    def test_invalid_json(self, monkeypatch, tmp_path, caplog):
        cfg = tmp_path / "bad.json"
        cfg.write_text("{ not valid")
        monkeypatch.setattr(backend_kimi, "KIMI_AGENT_CONFIG", cfg)
        with caplog.at_level(logging.WARNING, logger="engine.backend_kimi"):
            assert backend_kimi._load_config() == {}
        assert any("Invalid JSON" in r.message for r in caplog.records)

    def test_skips_non_dict(self, monkeypatch, tmp_path, caplog):
        cfg = tmp_path / "mixed.json"
        cfg.write_text('{"a": {"model": "x"}, "b": "string"}')
        monkeypatch.setattr(backend_kimi, "KIMI_AGENT_CONFIG", cfg)
        with caplog.at_level(logging.WARNING, logger="engine.backend_kimi"):
            cfg_data = backend_kimi._load_config()
        assert "a" in cfg_data
        assert "b" not in cfg_data

    def test_root_not_dict(self, monkeypatch, tmp_path, caplog):
        cfg = tmp_path / "list.json"
        cfg.write_text("[]")
        monkeypatch.setattr(backend_kimi, "KIMI_AGENT_CONFIG", cfg)
        with caplog.at_level(logging.WARNING, logger="engine.backend_kimi"):
            assert backend_kimi._load_config() == {}


# ===========================================================================
# _rebuild_kimi_md()
# ===========================================================================

class TestRebuildKimiMd:
    def _enable_compile(self, monkeypatch):
        monkeypatch.setattr(backend_kimi, "_load_config",
                            lambda: {AGENT: {"compile-startup-md": True}})

    def test_compile_generates(self, monkeypatch):
        self._enable_compile(monkeypatch)
        pd = _profile_dir()
        (pd / "IDENTITY.md").write_text("id")
        (pd / "INSTRUCTION.md").write_text("instr")
        (pd / "MEMORY.md").write_text("mem")
        backend_kimi._rebuild_kimi_md(AGENT)
        kmd = pd / "KIMI.md"
        assert kmd.exists()
        assert (pd / ".kimi_hash").exists()
        body = kmd.read_text()
        assert "id" in body and "instr" in body and "mem" in body

    def test_compile_false_preserves(self, monkeypatch):
        monkeypatch.setattr(backend_kimi, "_load_config",
                            lambda: {AGENT: {"compile-startup-md": False}})
        pd = _profile_dir()
        (pd / "KIMI.md").write_text("preexisting")
        backend_kimi._rebuild_kimi_md(AGENT)
        assert (pd / "KIMI.md").read_text() == "preexisting"

    def test_all_empty_deletes(self, monkeypatch):
        self._enable_compile(monkeypatch)
        pd = _profile_dir()
        (pd / "KIMI.md").write_text("stale")
        (pd / ".kimi_hash").write_text("deadbeef")
        backend_kimi._rebuild_kimi_md(AGENT)
        assert not (pd / "KIMI.md").exists()
        assert not (pd / ".kimi_hash").exists()


# ===========================================================================
# _terminate_pid_tree (smoke tests)
# ===========================================================================

class TestTerminatePidTree:
    def test_sigterm_success(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(backend_kimi.os, "getpgid", lambda p: p)
        monkeypatch.setattr(backend_kimi.os, "killpg",
                            lambda pgid, sig: calls.append(sig))
        proc = MagicMock()
        proc.wait.return_value = 0
        backend_kimi._terminate_pid_tree(1234, AGENT, proc=proc)
        assert calls == [backend_kimi.signal.SIGTERM]
