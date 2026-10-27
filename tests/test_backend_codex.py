"""Tests for engine/backend_codex.py."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
from engine import backend_codex
from engine.backend_types import SendResult

_REAL_PROC_STARTTIME = backend_codex._proc_starttime

AGENT = "reviewer1"
SID = "01a0f3e0-76ee-7071-9bde-11562a04dace"
OTHER_SID = "11111111-2222-4333-8444-555555555555"
FLAGS = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]

THREAD_STARTED = json.dumps({"type": "thread.started", "thread_id": SID}) + "\n"
TURN_STARTED = '{"type":"turn.started"}\n'
TURN_COMPLETED = '{"type":"turn.completed"}\n'
TURN_FAILED = '{"type":"turn.failed"}\n'
ITEM_COMPLETED = '{"type":"item.completed","item":{"type":"agent_message","text":"DONE"}}\n'
NO_ROLLOUT = (
    f"Error: thread/resume: thread/resume failed: no rollout found for thread id {SID} "
    "(code -32600)\n"
)


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch, tmp_path):
    """Reset module-level caches, isolate pid/profile dirs under tmp_path."""
    monkeypatch.setattr(backend_codex, "_agent_config_cache", None)
    monkeypatch.setattr(backend_codex, "CODEX_AGENT_CONFIG", tmp_path / "config_codex.json")
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "CODEX_BIN", "codex")

    pids = tmp_path / "pids"
    profiles = tmp_path / "agents"
    (profiles / AGENT).mkdir(parents=True)
    monkeypatch.setattr(backend_codex, "CODEX_PIDS_DIR", pids)
    monkeypatch.setattr(backend_codex, "AGENT_PROFILES_DIR", profiles)
    monkeypatch.setattr(backend_codex, "_proc_starttime", lambda pid: 777)
    yield


@pytest.fixture
def recorder(monkeypatch):
    """Record Popen calls; the fake child holds no lock and writes nothing."""
    calls: list[dict] = []
    state: dict = {"raise": None}

    class FakeProc:
        def __init__(self, pid: int = 4242) -> None:
            self.pid = pid
            self.wait = MagicMock(return_value=0)

    def fake_popen(cmd, **kwargs):
        calls.append({
            "argv": list(cmd),
            "kwargs": kwargs,
            "stdin": kwargs["stdin"].name,
            "stdout": kwargs["stdout"].name,
            "stderr": kwargs["stderr"].name,
            "in_progress": backend_codex._run_in_progress(AGENT),
            "out_bytes": Path(kwargs["stdout"].name).read_bytes(),
        })
        if state["raise"] is not None:
            raise state["raise"]
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return {"calls": calls, "state": state}


@pytest.fixture
def hold_lock():
    """Hold a flock on the out file, simulating a live codex process group."""
    held: list = []

    def _hold(mode: int = fcntl.LOCK_EX):
        path = backend_codex._out_path(AGENT)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(backend_codex._launch_header(SID))
        f = open(path, "rb")
        fcntl.flock(f.fileno(), mode)
        held.append(f)
        return f

    yield _hold
    for f in held:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        f.close()


def _release(f) -> None:
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _pids() -> Path:
    backend_codex.CODEX_PIDS_DIR.mkdir(parents=True, exist_ok=True)
    return backend_codex.CODEX_PIDS_DIR


def _profile_dir() -> Path:
    return backend_codex.AGENT_PROFILES_DIR / AGENT


def _write_out(header_sid: str | None | bool, *lines: str) -> Path:
    """header_sid=False means no header."""
    _pids()
    body = b"" if header_sid is False else backend_codex._launch_header(header_sid)
    body += "".join(lines).encode("utf-8")
    path = backend_codex._out_path(AGENT)
    path.write_bytes(body)
    return path


def _write_sid(sid: str) -> None:
    _pids()
    backend_codex._session_id_path(AGENT).write_text(sid + "\n")


def _read_sid() -> str | None:
    p = backend_codex._session_id_path(AGENT)
    return p.read_text().strip() if p.exists() else None


def _set_config(monkeypatch, profile: dict) -> None:
    monkeypatch.setattr(backend_codex, "_load_config", lambda: {AGENT: profile})


def _make_stale() -> None:
    old = time.time() - backend_codex.INACTIVE_THRESHOLD_SEC - 10
    for p in (backend_codex._out_path(AGENT), backend_codex._err_path(AGENT),
              backend_codex._pid_path(AGENT)):
        if p.exists():
            os.utime(p, (old, old))


def _fail_unlink_for(monkeypatch, target: Path) -> None:
    real_unlink = Path.unlink

    def spy(self, *args, **kwargs):
        if self == target:
            raise OSError("unlink denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy)


# ===========================================================================
# argv
# ===========================================================================

class TestArgv:
    def test_new_session_minimal(self, recorder):
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.OK
        assert recorder["calls"][0]["argv"] == ["codex", "exec", *FLAGS, "-"]

    def test_model_and_effort(self, recorder, monkeypatch):
        _set_config(monkeypatch, {"model": "m1", "effort": "high"})
        backend_codex.send(AGENT, "hi", 30)
        assert recorder["calls"][0]["argv"] == [
            "codex", "exec", *FLAGS, "-m", "m1", "-c", 'model_reasoning_effort="high"', "-",
        ]

    def test_resume_with_session_id(self, recorder):
        _write_sid(SID)
        backend_codex.send(AGENT, "hi", 30)
        assert recorder["calls"][0]["argv"] == ["codex", "exec", "resume", *FLAGS, SID, "-"]

    @pytest.mark.parametrize("bad", ["not-a-uuid", "-x", SID.upper()])
    def test_invalid_session_id_starts_new(self, recorder, bad):
        _write_sid(bad)
        backend_codex.send(AGENT, "hi", 30)
        assert recorder["calls"][0]["argv"] == ["codex", "exec", *FLAGS, "-"]


class TestModelEffort:
    @pytest.mark.parametrize("model", ["", "   ", 123, None])
    def test_model_skipped_silently(self, monkeypatch, caplog, model):
        profile: dict = {} if model is None else {"model": model}
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            cmd = backend_codex._build_cmd(AGENT, profile, None)
        assert "-m" not in cmd
        assert caplog.records == []

    @pytest.mark.parametrize("model", ["-x", "a\x00b"])
    def test_model_rejected_with_warning(self, caplog, model):
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            cmd = backend_codex._build_cmd(AGENT, {"model": model}, None)
        assert "-m" not in cmd
        assert any("model" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("effort", ['x"\nk=1', "HIGH", 5, "high\n"])
    def test_effort_rejected_with_warning(self, caplog, effort):
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            cmd = backend_codex._build_cmd(AGENT, {"effort": effort}, None)
        assert "-c" not in cmd
        assert any("effort" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("effort", [None, ""])
    def test_effort_skipped_silently(self, caplog, effort):
        profile: dict = {} if effort is None else {"effort": effort}
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            cmd = backend_codex._build_cmd(AGENT, profile, None)
        assert "-c" not in cmd
        assert caplog.records == []


# ===========================================================================
# send(): kwargs / lock / header
# ===========================================================================

class TestSendKwargs:
    def test_stdio_paths_and_prompt(self, recorder):
        backend_codex.send(AGENT, "hello world", 30)
        c = recorder["calls"][0]
        assert c["stdin"] == str(backend_codex._prompt_path(AGENT))
        assert backend_codex._prompt_path(AGENT).read_text() == "hello world"
        assert c["stdout"] == str(backend_codex._out_path(AGENT))
        assert c["stderr"] == str(backend_codex._err_path(AGENT))

    def test_cwd_session_close_fds(self, recorder):
        backend_codex.send(AGENT, "hi", 30)
        kw = recorder["calls"][0]["kwargs"]
        assert kw["cwd"] == str(_profile_dir())
        assert kw["start_new_session"] is True
        assert "close_fds" not in kw

    def test_lock_held_during_popen_and_released_after(self, recorder):
        backend_codex.send(AGENT, "hi", 30)
        assert recorder["calls"][0]["in_progress"] is True
        assert backend_codex._run_in_progress(AGENT) is False

    def test_pid_record(self, recorder):
        backend_codex.send(AGENT, "hi", 30)
        assert backend_codex._pid_path(AGENT).read_text() == "4242 777\n"

    def test_header_new(self, recorder):
        backend_codex.send(AGENT, "hi", 30)
        header = backend_codex._launch_header(None)
        assert recorder["calls"][0]["out_bytes"] == header
        assert backend_codex._out_path(AGENT).read_bytes() == header

    def test_header_resume(self, recorder):
        _write_sid(SID)
        backend_codex.send(AGENT, "hi", 30)
        header = backend_codex._launch_header(SID)
        assert recorder["calls"][0]["out_bytes"] == header
        assert backend_codex._out_path(AGENT).read_bytes() == header


# ===========================================================================
# send(): failure paths
# ===========================================================================

class TestSendFailures:
    def test_dry_run(self, recorder, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", True)
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.OK
        assert recorder["calls"] == []

    def test_profile_dir_missing(self, recorder, monkeypatch, tmp_path):
        monkeypatch.setattr(backend_codex, "AGENT_PROFILES_DIR", tmp_path / "nope")
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.FAIL
        assert recorder["calls"] == []

    def test_popen_oserror(self, recorder):
        recorder["state"]["raise"] = OSError("no codex")
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.FAIL
        assert not backend_codex._out_path(AGENT).exists()

    def test_prompt_write_failure(self, recorder, monkeypatch, tmp_path):
        d = tmp_path / "prompt_dir"
        d.mkdir()
        monkeypatch.setattr(backend_codex, "_prompt_path", lambda a: d)
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.FAIL
        assert recorder["calls"] == []

    def test_out_lock_contended_returns_busy(self, recorder, monkeypatch):
        real_flock = fcntl.flock

        def spy(fd, op):
            if op == fcntl.LOCK_EX | fcntl.LOCK_NB:
                raise BlockingIOError("contended")
            return real_flock(fd, op)

        monkeypatch.setattr(backend_codex.fcntl, "flock", spy)
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.BUSY
        assert recorder["calls"] == []
        assert not backend_codex._out_path(AGENT).exists()

    def test_pid_record_failure_terminated(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_codex, "_write_pid_record", lambda *a: False)
        term = MagicMock(return_value=True)
        monkeypatch.setattr(backend_codex, "_terminate_run", term)
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.FAIL
        term.assert_called_once()
        assert term.call_args.kwargs.get("proc") is not None
        assert not backend_codex._out_path(AGENT).exists()

    def test_starttime_none_terminated(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_codex, "_proc_starttime", lambda pid: None)
        term = MagicMock(return_value=True)
        monkeypatch.setattr(backend_codex, "_terminate_run", term)
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.FAIL
        term.assert_called_once()
        assert term.call_args.kwargs.get("proc") is not None
        assert not backend_codex._out_path(AGENT).exists()

    def test_no_double_spawn_when_terminate_fails(
        self, recorder, monkeypatch, hold_lock, caplog,
    ):
        monkeypatch.setattr(backend_codex, "_write_pid_record", lambda *a: False)
        monkeypatch.setattr(backend_codex, "_terminate_run", lambda *a, **k: False)
        with caplog.at_level(logging.ERROR, logger="engine.backend_codex"):
            assert backend_codex.send(AGENT, "hi", 30) is SendResult.FAIL
        assert backend_codex._out_path(AGENT).exists()
        assert any(r.levelno == logging.ERROR for r in caplog.records)
        # The (still alive) child holds the lock.
        hold_lock()
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.BUSY
        assert len(recorder["calls"]) == 1


# ===========================================================================
# send(): harvest failures stop the send
# ===========================================================================

class TestSendHarvestFailure:
    def test_session_write_failure_fails(self, recorder, monkeypatch):
        _write_out(None, THREAD_STARTED, TURN_STARTED)
        monkeypatch.setattr(backend_codex, "_write_session_id", lambda *a: False)
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.FAIL
        assert recorder["calls"] == []
        assert backend_codex._out_path(AGENT).exists()

    def test_parse_oserror_fails(self, recorder, monkeypatch):
        out = _write_out(None, THREAD_STARTED, TURN_STARTED)
        before = out.read_bytes()
        _write_sid(OTHER_SID)

        def boom(path):
            raise OSError("read error")

        monkeypatch.setattr(backend_codex, "_parse_run_output", boom)
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.FAIL
        assert out.read_bytes() == before
        assert _read_sid() == OTHER_SID

    def test_out_unlink_failure_continues(self, recorder, monkeypatch):
        out = _write_out(None, THREAD_STARTED, TURN_STARTED)
        _fail_unlink_for(monkeypatch, out)
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.OK
        assert _read_sid() == SID


# ===========================================================================
# send(): BUSY / reap
# ===========================================================================

class TestSendBusyReap:
    def test_fresh_run_busy(self, recorder, hold_lock):
        hold_lock()
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.BUSY
        assert recorder["calls"] == []

    def _stale_setup(self, hold_lock, pid_text: str | None = "1234 777"):
        _write_sid(SID)
        _write_out(SID, THREAD_STARTED, TURN_STARTED)
        backend_codex._err_path(AGENT).write_bytes(b"")
        if pid_text is not None:
            backend_codex._pid_path(AGENT).write_text(pid_text)
        f = hold_lock()
        _make_stale()
        return f

    def test_stale_owned_reaped_and_resumed(self, recorder, monkeypatch, hold_lock):
        f = self._stale_setup(hold_lock)
        monkeypatch.setattr(backend_codex, "_is_owned_codex_pid", lambda p, s: True)
        term_calls: list = []

        def fake_term(agent_id, pid, proc=None):
            term_calls.append((agent_id, pid))
            _release(f)
            return True

        monkeypatch.setattr(backend_codex, "_terminate_run", fake_term)
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.OK
        assert term_calls == [(AGENT, 1234)]
        assert recorder["calls"][0]["argv"] == ["codex", "exec", "resume", *FLAGS, SID, "-"]
        assert backend_codex._pid_path(AGENT).read_text() == "4242 777\n"

    def test_stale_terminate_fails_busy(self, recorder, monkeypatch, hold_lock):
        self._stale_setup(hold_lock)
        monkeypatch.setattr(backend_codex, "_is_owned_codex_pid", lambda p, s: True)
        monkeypatch.setattr(backend_codex, "_terminate_run", lambda *a, **k: False)
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.BUSY
        assert backend_codex._pid_path(AGENT).exists()
        assert recorder["calls"] == []

    def test_pid_reused_not_killed(self, recorder, monkeypatch, hold_lock, caplog):
        self._stale_setup(hold_lock)
        monkeypatch.setattr(backend_codex, "_is_owned_codex_pid", lambda p, s: False)
        term = MagicMock(return_value=True)
        monkeypatch.setattr(backend_codex, "_terminate_run", term)
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            assert backend_codex.send(AGENT, "hi", 30) is SendResult.BUSY
        term.assert_not_called()
        assert any("fuser" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("pid_text", [None, "1234", "x y"])
    def test_pid_record_missing_or_broken(self, recorder, monkeypatch, hold_lock, pid_text):
        self._stale_setup(hold_lock, pid_text=pid_text)
        owned = MagicMock(return_value=True)
        monkeypatch.setattr(backend_codex, "_is_owned_codex_pid", owned)
        term = MagicMock(return_value=True)
        monkeypatch.setattr(backend_codex, "_terminate_run", term)
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.BUSY
        term.assert_not_called()
        owned.assert_not_called()

    def test_stale_pid_file_replaced_when_not_running(self, recorder, monkeypatch):
        _pids()
        backend_codex._pid_path(AGENT).write_text("1234 999\n")
        term = MagicMock(return_value=True)
        monkeypatch.setattr(backend_codex, "_terminate_run", term)
        real_unlink = Path.unlink
        unlinked: list = []

        def spy(self, *args, **kwargs):
            unlinked.append(self)
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", spy)
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.OK
        assert backend_codex._pid_path(AGENT) in unlinked
        term.assert_not_called()
        assert backend_codex._pid_path(AGENT).read_text() == "4242 777\n"


# ===========================================================================
# _harvest_session table
# ===========================================================================

class TestHarvest:
    def test_row0_no_header_untouched(self, caplog):
        _write_out(False, THREAD_STARTED, TURN_STARTED)
        _write_sid(OTHER_SID)
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() == OTHER_SID
        assert caplog.records

    def test_row0_empty_out(self):
        _write_out(False)
        _write_sid(OTHER_SID)
        assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() == OTHER_SID

    def test_row1_resume_failed_discards(self, caplog):
        _write_out(SID)
        backend_codex._err_path(AGENT).write_text(NO_ROLLOUT)
        _write_sid(SID)
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() is None
        assert any("no rollout found" in r.getMessage() for r in caplog.records)

    def test_row1_thread_started_without_turn_discards(self):
        _write_out(SID, THREAD_STARTED)
        _write_sid(SID)
        assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() is None

    def test_row1_compare_and_delete(self):
        _write_out(SID)
        backend_codex._err_path(AGENT).write_text(NO_ROLLOUT)
        _write_sid(OTHER_SID)
        assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() == OTHER_SID

    def test_row1prime_no_evidence_keeps(self, caplog):
        _write_out(SID)
        backend_codex._err_path(AGENT).write_bytes(b"")
        _write_sid(SID)
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() == SID
        assert caplog.records

    def test_row2_resume_ok_turn_failed(self):
        _write_out(SID, THREAD_STARTED, TURN_FAILED)
        _write_sid(SID)
        assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() == SID

    def test_row3_resume_turn_only(self, caplog):
        _write_out(SID, TURN_STARTED, TURN_COMPLETED)
        _write_sid(SID)
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() == SID
        assert caplog.records

    def test_row4_new_ok(self):
        out = _write_out(None, THREAD_STARTED, TURN_STARTED, ITEM_COMPLETED, TURN_COMPLETED)
        assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() == SID
        assert not out.exists()

    def test_row5_new_thread_only(self, caplog):
        _write_out(None, THREAD_STARTED)
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() == SID
        assert caplog.records

    def test_row6_new_turn_only(self, caplog):
        _write_out(None, TURN_STARTED)
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() is None
        assert caplog.records

    def test_row7_new_nothing(self, caplog):
        out = _write_out(None)
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() is None
        assert caplog.records
        assert not out.exists()

    def test_no_out_is_noop(self):
        assert backend_codex._harvest_session(AGENT) is True


class TestHarvestIdempotent:
    def test_row5_twice(self, monkeypatch):
        out = _write_out(None, THREAD_STARTED)
        _fail_unlink_for(monkeypatch, out)
        assert backend_codex._harvest_session(AGENT) is True
        assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() == SID

    def test_row4_twice(self, monkeypatch):
        out = _write_out(None, THREAD_STARTED, TURN_STARTED)
        _fail_unlink_for(monkeypatch, out)
        assert backend_codex._harvest_session(AGENT) is True
        assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() == SID

    def test_row1_twice(self, monkeypatch):
        out = _write_out(SID)
        backend_codex._err_path(AGENT).write_text(NO_ROLLOUT)
        _write_sid(SID)
        _fail_unlink_for(monkeypatch, out)
        assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() is None
        assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() is None
        _write_sid(OTHER_SID)
        assert backend_codex._harvest_session(AGENT) is True
        assert _read_sid() == OTHER_SID

    def test_send_path_keeps_session(self, recorder, monkeypatch):
        """Unlink of out fails and Popen fails → only header resume(T) remains."""
        out = _write_out(None, THREAD_STARTED)
        _fail_unlink_for(monkeypatch, out)
        recorder["state"]["raise"] = OSError("spawn failed")
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.FAIL
        assert out.read_bytes() == backend_codex._launch_header(SID)
        assert backend_codex._err_path(AGENT).read_bytes() == b""
        assert _read_sid() == SID

        recorder["state"]["raise"] = None
        assert backend_codex.send(AGENT, "hi", 30) is SendResult.OK
        assert _read_sid() == SID
        assert recorder["calls"][1]["argv"] == ["codex", "exec", "resume", *FLAGS, SID, "-"]


# ===========================================================================
# _parse_run_output
# ===========================================================================

class TestParseRunOutput:
    def test_first_thread_started_wins(self):
        second = json.dumps({"type": "thread.started", "thread_id": OTHER_SID}) + "\n"
        out = _write_out(None, THREAD_STARTED, second, TURN_STARTED)
        assert backend_codex._parse_run_output(out).thread_id == SID

    def test_invalid_first_thread_id(self):
        first = json.dumps({"type": "thread.started", "thread_id": "bogus"}) + "\n"
        out = _write_out(None, first, THREAD_STARTED, TURN_STARTED)
        r = backend_codex._parse_run_output(out)
        assert r.thread_id is None
        assert r.saw_thread_started is True

    def test_non_json_ignored(self):
        out = _write_out(None, "garbage\n", "\xff\n", THREAD_STARTED, TURN_STARTED)
        assert backend_codex._parse_run_output(out).thread_id == SID

    def test_max_lines(self):
        junk = ["junk\n"] * backend_codex._PARSE_MAX_LINES
        out = _write_out(None, *junk, THREAD_STARTED)
        r = backend_codex._parse_run_output(out)
        assert r.thread_id is None
        assert r.saw_thread_started is False

    @pytest.mark.parametrize("first", [
        THREAD_STARTED,
        json.dumps({"type": "thread.started", "thread_id": "bogus"}) + "\n",
    ])
    def test_early_stop(self, monkeypatch, first):
        out = _write_out(None, first, TURN_STARTED, *([ITEM_COMPLETED] * 50))
        real = backend_codex._parse_event_line
        spy = MagicMock(side_effect=real)
        monkeypatch.setattr(backend_codex, "_parse_event_line", spy)
        backend_codex._parse_run_output(out)
        assert spy.call_count == 3

    def test_header_new(self):
        r = backend_codex._parse_run_output(_write_out(None))
        assert r.header_ok is True and r.launched_sid is None

    def test_header_resume(self):
        r = backend_codex._parse_run_output(_write_out(SID))
        assert r.header_ok is True and r.launched_sid == SID

    def test_header_invalid_sid(self):
        r = backend_codex._parse_run_output(
            _write_out(False, '{"type":"gokrax.launch","session_id":"-x"}\n'))
        assert r.header_ok is False

    def test_header_on_second_line(self):
        hdr = backend_codex._launch_header(None).decode()
        r = backend_codex._parse_run_output(_write_out(False, TURN_STARTED, hdr))
        assert r.header_ok is False

    def test_first_line_thread_started(self):
        r = backend_codex._parse_run_output(_write_out(False, THREAD_STARTED, TURN_STARTED))
        assert r.header_ok is False
        assert r.thread_id == SID


# ===========================================================================
# Liveness primitives
# ===========================================================================

class TestRunInProgress:
    def test_no_out(self):
        assert backend_codex._run_in_progress(AGENT) is False

    def test_lock_cycle(self, hold_lock):
        f = hold_lock()
        assert backend_codex._run_in_progress(AGENT) is True
        _release(f)
        assert backend_codex._run_in_progress(AGENT) is False

    def test_shared_lock_does_not_conflict(self, hold_lock):
        hold_lock(fcntl.LOCK_SH)
        assert backend_codex._run_in_progress(AGENT) is False


class _FakeProcPath:
    """Fake pathlib.Path for /proc lookups."""

    files: dict[str, object] = {}

    def __init__(self, p):
        self._p = str(p)

    def __truediv__(self, other):
        return _FakeProcPath(f"{self._p}/{other}")

    def exists(self):
        return any(k == self._p or k.startswith(self._p + "/") for k in self.files)

    def _get(self):
        v = self.files.get(self._p)
        if v is None:
            raise FileNotFoundError(self._p)
        return v

    def read_bytes(self):
        return self._get()

    def read_text(self, encoding=None, errors=None):
        return self._get().decode("utf-8", errors="replace")


def _fake_proc(monkeypatch, files: dict[str, bytes]) -> None:
    monkeypatch.setattr(_FakeProcPath, "files", files)
    monkeypatch.setattr(backend_codex, "Path", _FakeProcPath)


class TestProcStarttime:
    def _stat(self, comm: str) -> bytes:
        fields = ["S"] + [str(i) for i in range(4, 22)] + ["98765"] + ["0"] * 10
        return f"123 ({comm}) {' '.join(fields)}\n".encode()

    @pytest.mark.parametrize("comm", ["codex", "we ird) name", "a b c"])
    def test_field_22(self, monkeypatch, comm):
        _fake_proc(monkeypatch, {"/proc/123/stat": self._stat(comm)})
        assert _REAL_PROC_STARTTIME(123) == 98765

    def test_missing(self, monkeypatch):
        _fake_proc(monkeypatch, {})
        assert _REAL_PROC_STARTTIME(123) is None

    def test_too_few_fields(self, monkeypatch):
        _fake_proc(monkeypatch, {"/proc/123/stat": b"123 (codex) S 1 2 3\n"})
        assert _REAL_PROC_STARTTIME(123) is None


class TestIsOwnedCodexPid:
    @pytest.mark.parametrize("cmdline", [
        b"node\x00/home/u/.local/bin/codex\x00exec\x00--json\x00-\x00",
        b"/x/vendor/x86_64-unknown-linux-musl/bin/codex\x00exec\x00resume\x00--json\x00",
        b"node\x00/x/@openai/codex/bin/codex.js\x00exec\x00-\x00",
    ])
    def test_owned(self, monkeypatch, cmdline):
        _fake_proc(monkeypatch, {"/proc/123/cmdline": cmdline})
        assert backend_codex._is_owned_codex_pid(123, 777) is True

    def test_starttime_mismatch(self, monkeypatch):
        _fake_proc(monkeypatch, {"/proc/123/cmdline": b"codex\x00exec\x00-\x00"})
        assert backend_codex._is_owned_codex_pid(123, 778) is False

    @pytest.mark.parametrize("cmdline", [
        b"node\x00/home/u/.local/bin/codex\x00",
        b"vim\x00exec\x00",
        b"",
    ])
    def test_not_owned(self, monkeypatch, cmdline):
        _fake_proc(monkeypatch, {"/proc/123/cmdline": cmdline})
        assert backend_codex._is_owned_codex_pid(123, 777) is False

    def test_proc_missing(self, monkeypatch):
        _fake_proc(monkeypatch, {})
        assert backend_codex._is_owned_codex_pid(123, 777) is False


class TestTerminateRun:
    def test_released_after_sigterm(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(backend_codex.os, "killpg", lambda p, s: calls.append((p, s)))
        monkeypatch.setattr(backend_codex, "_run_in_progress", MagicMock(side_effect=[False]))
        assert backend_codex._terminate_run(AGENT, 1234) is True
        assert calls == [(1234, signal.SIGTERM)]

    def test_never_released(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(backend_codex.os, "killpg", lambda p, s: calls.append(s))
        monkeypatch.setattr(backend_codex, "_run_in_progress", lambda a: True)
        assert backend_codex._terminate_run(AGENT, 1234) is False
        assert calls == [signal.SIGTERM, signal.SIGKILL]

    def test_proc_wait_called(self, monkeypatch):
        monkeypatch.setattr(backend_codex.os, "killpg", lambda p, s: None)
        monkeypatch.setattr(backend_codex, "_run_in_progress", lambda a: False)
        proc = MagicMock()
        assert backend_codex._terminate_run(AGENT, 1234, proc=proc) is True
        proc.wait.assert_called_with(timeout=5)


class TestLastActivity:
    def test_one_stat_fails(self, monkeypatch):
        _pids()
        out = backend_codex._out_path(AGENT)
        err = backend_codex._err_path(AGENT)
        pid = backend_codex._pid_path(AGENT)
        for p in (out, err, pid):
            p.write_text("x")
        os.utime(out, (1000, 1000))
        os.utime(err, (3000, 3000))
        os.utime(pid, (2000, 2000))
        real_stat = Path.stat

        def spy(self, *args, **kwargs):
            if self == err:
                raise OSError("stat denied")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", spy)
        assert backend_codex._codex_last_activity(AGENT) == 2000

    def test_none(self):
        assert backend_codex._codex_last_activity(AGENT) is None


class TestIsInactive:
    def test_not_running(self):
        assert backend_codex.is_inactive(AGENT) is True

    def test_running_fresh(self, hold_lock):
        hold_lock()
        assert backend_codex.is_inactive(AGENT) is False

    def test_running_stale(self, hold_lock):
        hold_lock()
        _make_stale()
        assert backend_codex.is_inactive(AGENT) is True

    def test_cc_running(self):
        assert backend_codex.is_inactive(AGENT, cc_running=True) is False


# ===========================================================================
# soft_reap / reset_session
# ===========================================================================

class TestSoftReap:
    def test_not_running_clears_pid(self, monkeypatch):
        _pids()
        backend_codex._pid_path(AGENT).write_text("1234 777\n")
        term = MagicMock(return_value=True)
        monkeypatch.setattr(backend_codex, "_terminate_run", term)
        backend_codex.soft_reap(AGENT)
        assert not backend_codex._pid_path(AGENT).exists()
        term.assert_not_called()

    def test_running_owned(self, monkeypatch, hold_lock):
        _write_sid(SID)
        _write_out(SID, THREAD_STARTED)
        backend_codex._pid_path(AGENT).write_text("1234 777\n")
        hold_lock()
        monkeypatch.setattr(backend_codex, "_is_owned_codex_pid", lambda p, s: True)
        term = MagicMock(return_value=True)
        monkeypatch.setattr(backend_codex, "_terminate_run", term)
        backend_codex.soft_reap(AGENT)
        term.assert_called_once()
        assert not backend_codex._pid_path(AGENT).exists()
        assert _read_sid() == SID
        assert backend_codex._out_path(AGENT).exists()

    @pytest.mark.parametrize("owned,term_ok", [(False, True), (True, False)])
    def test_keeps_pid_on_failure(self, monkeypatch, hold_lock, owned, term_ok):
        _pids()
        backend_codex._pid_path(AGENT).write_text("1234 777\n")
        hold_lock()
        monkeypatch.setattr(backend_codex, "_is_owned_codex_pid", lambda p, s: owned)
        monkeypatch.setattr(backend_codex, "_terminate_run", lambda *a, **k: term_ok)
        backend_codex.soft_reap(AGENT)
        assert backend_codex._pid_path(AGENT).exists()

    def test_dry_run(self, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", True)
        kill = MagicMock(return_value=True)
        monkeypatch.setattr(backend_codex, "_kill_running", kill)
        backend_codex.soft_reap(AGENT)
        kill.assert_not_called()


class TestResetSession:
    def _populate(self) -> list[Path]:
        _write_sid(SID)
        _write_out(SID, THREAD_STARTED)
        paths = [
            backend_codex._pid_path(AGENT),
            backend_codex._session_id_path(AGENT),
            backend_codex._out_path(AGENT),
            backend_codex._err_path(AGENT),
            backend_codex._prompt_path(AGENT),
        ]
        for p in paths:
            if not p.exists():
                p.write_text("x")
        return paths

    def test_kill_fails_keeps_everything(self, monkeypatch):
        paths = self._populate()
        monkeypatch.setattr(backend_codex, "_kill_running", lambda a, w: False)
        backend_codex.reset_session(AGENT)
        assert all(p.exists() for p in paths)

    def test_removes_state(self, monkeypatch):
        paths = self._populate()
        monkeypatch.setattr(backend_codex, "_kill_running", lambda a, w: True)
        backend_codex.reset_session(AGENT)
        assert not any(p.exists() for p in paths)

    def test_calls_rebuild(self, monkeypatch):
        rebuild = MagicMock()
        monkeypatch.setattr(backend_codex, "_rebuild_codex_md", rebuild)
        backend_codex.reset_session(AGENT)
        rebuild.assert_called_once_with(AGENT)


# ===========================================================================
# _rebuild_codex_md
# ===========================================================================

class TestRebuildCodexMd:
    def _enable_compile(self, monkeypatch):
        _set_config(monkeypatch, {"compile-startup-md": True})

    def test_compile_generates(self, monkeypatch):
        self._enable_compile(monkeypatch)
        pd = _profile_dir()
        (pd / "IDENTITY.md").write_text("id")
        (pd / "INSTRUCTION.md").write_text("instr")
        (pd / "MEMORY.md").write_text("mem")
        backend_codex._rebuild_codex_md(AGENT)
        md = pd / "AGENTS.md"
        assert md.exists()
        assert (pd / ".codex_hash").exists()
        body = md.read_text()
        assert "id" in body and "instr" in body and "mem" in body

    def test_compile_false_preserves(self, monkeypatch):
        _set_config(monkeypatch, {"compile-startup-md": False})
        pd = _profile_dir()
        (pd / "AGENTS.md").write_text("handwritten")
        backend_codex._rebuild_codex_md(AGENT)
        assert (pd / "AGENTS.md").read_text() == "handwritten"

    def test_other_hash_files_untouched(self, monkeypatch):
        self._enable_compile(monkeypatch)
        pd = _profile_dir()
        (pd / "IDENTITY.md").write_text("id")
        (pd / ".agy_hash").write_text("agy")
        backend_codex._rebuild_codex_md(AGENT)
        assert not (pd / ".agents_hash").exists()
        assert (pd / ".agy_hash").read_text() == "agy"

    def test_hash_match_skips_rewrite(self, monkeypatch):
        self._enable_compile(monkeypatch)
        pd = _profile_dir()
        (pd / "IDENTITY.md").write_text("id")
        backend_codex._rebuild_codex_md(AGENT)
        md = pd / "AGENTS.md"
        os.utime(md, (1000, 1000))
        backend_codex._rebuild_codex_md(AGENT)
        assert md.stat().st_mtime == 1000

    def test_oversize_warns(self, monkeypatch, caplog):
        self._enable_compile(monkeypatch)
        pd = _profile_dir()
        (pd / "IDENTITY.md").write_text("a" * 32769)
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            backend_codex._rebuild_codex_md(AGENT)
        assert (pd / "AGENTS.md").exists()
        assert any("project_doc_max_bytes" in r.getMessage() for r in caplog.records)

    def test_all_empty_deletes(self, monkeypatch):
        self._enable_compile(monkeypatch)
        pd = _profile_dir()
        (pd / "AGENTS.md").write_text("stale")
        (pd / ".codex_hash").write_text("deadbeef")
        backend_codex._rebuild_codex_md(AGENT)
        assert not (pd / "AGENTS.md").exists()
        assert not (pd / ".codex_hash").exists()


# ===========================================================================
# Misc
# ===========================================================================

class TestPing:
    def test_always_true(self):
        assert backend_codex.ping(AGENT, 10) is True


class TestLoadConfig:
    def test_missing_file(self):
        assert backend_codex._load_config() == {}

    def test_invalid_json(self, caplog):
        backend_codex.CODEX_AGENT_CONFIG.write_text("{not json")
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            assert backend_codex._load_config() == {}
        assert any("Invalid JSON" in r.getMessage() for r in caplog.records)

    def test_skips_non_dict(self, caplog):
        backend_codex.CODEX_AGENT_CONFIG.write_text(
            json.dumps({"a": {"model": "m"}, "b": "str", "_comment": {"x": "y"}}))
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            cfg = backend_codex._load_config()
        assert cfg == {"a": {"model": "m"}, "_comment": {"x": "y"}}
        assert any("Skipped non-dict" in r.getMessage() for r in caplog.records)

    def test_root_not_dict(self, caplog):
        backend_codex.CODEX_AGENT_CONFIG.write_text("[1, 2]")
        with caplog.at_level(logging.WARNING, logger="engine.backend_codex"):
            assert backend_codex._load_config() == {}
        assert any("Expected JSON object" in r.getMessage() for r in caplog.records)


# ===========================================================================
# Issue #400: boost overlay
# ===========================================================================

class TestSendBoost:
    _PROFILE = {"model": "m1", "effort": "medium", "boost": {"model": "m2", "effort": "xhigh"}}

    def test_boost_true_overlays(self, recorder, monkeypatch):
        _set_config(monkeypatch, self._PROFILE)
        backend_codex.send(AGENT, "hi", 30, boost=True)
        assert recorder["calls"][0]["argv"] == [
            "codex", "exec", *FLAGS, "-m", "m2", "-c", 'model_reasoning_effort="xhigh"', "-",
        ]

    def test_boost_false_keeps_base(self, recorder, monkeypatch):
        _set_config(monkeypatch, self._PROFILE)
        backend_codex.send(AGENT, "hi", 30, boost=False)
        assert recorder["calls"][0]["argv"] == [
            "codex", "exec", *FLAGS, "-m", "m1", "-c", 'model_reasoning_effort="medium"', "-",
        ]
