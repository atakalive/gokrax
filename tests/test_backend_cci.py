"""Tests for engine/backend_cci.py — cci backend for agent communication."""

from __future__ import annotations

import errno
import json
import logging
import os
import signal
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import config
from config import PROJECT_ROOT
from engine import backend_cci
from engine.backend_types import SendResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_starting_markers():
    backend_cci._starting_markers.clear()
    yield
    backend_cci._starting_markers.clear()


@pytest.fixture(autouse=True)
def _reset_agent_config_cache():
    backend_cci._agent_config_cache = None
    yield
    backend_cci._agent_config_cache = None


@pytest.fixture
def tmp_sessions(tmp_path, monkeypatch):
    """Redirect CCI_SESSIONS_DIR (both binding sites) to a temporary directory."""
    monkeypatch.setattr("config.CCI_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr("engine.backend_cci.CCI_SESSIONS_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _tmp_claude_home(tmp_path, monkeypatch):
    """Redirect Path.home() so Claude jsonl paths stay inside tmp_path."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_dir))
    return home_dir


def _mock_proc(pid: int = 12345) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    return proc


# ===========================================================================
# _load_config
# ===========================================================================

class TestLoadConfig:
    def test_file_missing_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "engine.backend_cci.CCI_AGENT_CONFIG", tmp_path / "nope.json",
        )
        assert backend_cci._load_config() == {}

    def test_valid_json_returns_parsed(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config_cci.json"
        cfg.write_text(json.dumps({"reviewer1": {"model": "opus"}}))
        monkeypatch.setattr("engine.backend_cci.CCI_AGENT_CONFIG", cfg)
        assert backend_cci._load_config() == {"reviewer1": {"model": "opus"}}

    def test_invalid_json_returns_empty(self, monkeypatch, tmp_path, caplog):
        cfg = tmp_path / "config_cci.json"
        cfg.write_text("{nope")
        monkeypatch.setattr("engine.backend_cci.CCI_AGENT_CONFIG", cfg)
        with caplog.at_level(logging.WARNING):
            assert backend_cci._load_config() == {}
        assert any("Invalid JSON" in r.message for r in caplog.records)


# ===========================================================================
# Path helpers
# ===========================================================================

class TestPathHelpers:
    def test_session_dir(self, tmp_sessions):
        assert backend_cci._session_dir("reviewer1") == tmp_sessions / "reviewer1"

    def test_session_id_path(self, tmp_sessions):
        assert backend_cci._session_id_path("reviewer1") == tmp_sessions / "reviewer1" / "session_id"

    def test_pid_path(self, tmp_sessions):
        assert backend_cci._pid_path("reviewer1") == tmp_sessions / "reviewer1" / "pid"

    def test_claude_session_jsonl_path(self):
        cwd = Path("/mnt/s/wsl/work/project/gokrax")
        result = backend_cci._claude_session_jsonl_path(cwd, "abc-123")
        assert result.name == "abc-123.jsonl"
        assert result.parent == backend_cci._claude_project_dir(cwd)


# ===========================================================================
# send
# ===========================================================================

class TestSend:
    def test_dry_run_returns_ok(self, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", True)
        assert backend_cci.send("reviewer1", "hi", timeout=30) is SendResult.OK

    def test_new_session_uses_session_id_flag(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        with patch("subprocess.Popen", return_value=_mock_proc()) as mp:
            backend_cci.send("reviewer1", "hello", timeout=30)
        cmd = mp.call_args[0][0]
        assert "--session-id" in cmd
        assert "--resume" not in cmd
        assert "engine.cci_runner" in cmd
        assert cmd[0] == sys.executable

    def test_runner_module_invoked(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        with patch("subprocess.Popen", return_value=_mock_proc()) as mp:
            backend_cci.send("reviewer1", "hello", timeout=30)
        cmd = mp.call_args[0][0]
        assert cmd[1] == "-m"
        assert cmd[2] == "engine.cci_runner"

    def test_resume_when_live_owner_absent(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("999999")  # dead pid

        orig_exists = Path.exists

        def mock_exists(self):
            if str(self) == "/proc/999999":
                return False
            return orig_exists(self)

        with patch.object(Path, "exists", mock_exists), \
             patch("subprocess.Popen", return_value=_mock_proc(54321)) as mp:
            result = backend_cci.send("reviewer1", "hello", timeout=30)
        assert result is SendResult.OK
        cmd = mp.call_args[0][0]
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == sid

    def test_completion_timeout_uses_config_not_send_timeout(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(config, "CCI_COMPLETION_TIMEOUT_SEC", 1234)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        with patch("subprocess.Popen", return_value=_mock_proc()) as mp:
            backend_cci.send("reviewer1", "hello", timeout=30)
        cmd = mp.call_args[0][0]
        assert "--completion-timeout" in cmd
        assert cmd[cmd.index("--completion-timeout") + 1] == "1234"
        # send() timeout (30) must NOT appear as the completion timeout
        assert cmd[cmd.index("--completion-timeout") + 1] != "30"

    def test_startup_timeout_not_passed_to_runner(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        with patch("subprocess.Popen", return_value=_mock_proc()) as mp:
            backend_cci.send("reviewer1", "hello", timeout=30)
        cmd = mp.call_args[0][0]
        # Screen-scraping handshake removed: no startup deadline flag, but the
        # completion timeout is still passed.
        assert not any("startup" in str(c) for c in cmd)
        assert "--completion-timeout" in cmd

    def test_agent_id_passed_to_runner(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        with patch("subprocess.Popen", return_value=_mock_proc()) as mp:
            backend_cci.send("reviewer1", "hello", timeout=30)
        cmd = mp.call_args[0][0]
        assert "--agent-id" in cmd
        assert cmd[cmd.index("--agent-id") + 1] == "reviewer1"

    def test_send_clears_stale_last_error(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        (d / "last_error").write_text('{"reason": "startup_timeout"}')
        with patch("subprocess.Popen", return_value=_mock_proc()):
            backend_cci.send("reviewer1", "hello", timeout=30)
        assert not (d / "last_error").exists()

    def test_delete_prompt_file_flag_always_added(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        with patch("subprocess.Popen", return_value=_mock_proc()) as mp:
            backend_cci.send("reviewer1", "hello", timeout=30)
        cmd = mp.call_args[0][0]
        assert "--delete-prompt-file" in cmd

    def test_optional_flags(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {
            "reviewer1": {"model": "opus", "thinking": "enabled", "effort": "high"},
        })
        with patch("subprocess.Popen", return_value=_mock_proc()) as mp:
            backend_cci.send("reviewer1", "hello", timeout=30)
        cmd = mp.call_args[0][0]
        assert cmd[cmd.index("--model") + 1] == "opus"
        assert cmd[cmd.index("--thinking") + 1] == "enabled"
        assert cmd[cmd.index("--effort") + 1] == "high"

    def test_effort_ultracode_passthrough(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {
            "reviewer1": {"effort": "ultracode"},
        })
        with patch("subprocess.Popen", return_value=_mock_proc()) as mp:
            backend_cci.send("reviewer1", "hello", timeout=30)
        cmd = mp.call_args[0][0]
        assert cmd[cmd.index("--effort") + 1] == "ultracode"

    def test_cwd_passed_as_flag_popen_runs_in_project_root(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        with patch("subprocess.Popen", return_value=_mock_proc()) as mp:
            backend_cci.send("reviewer1", "hello", timeout=30)
        # runner launched with PROJECT_ROOT cwd (import path preserved)
        assert mp.call_args[1]["cwd"] == str(PROJECT_ROOT)
        assert mp.call_args[1]["start_new_session"] is True
        # --cwd flag points at the agent's working dir (PROJECT_ROOT when no profile)
        cmd = mp.call_args[0][0]
        assert "--cwd" in cmd

    def test_stderr_runner_log_opened_a_mode(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        # Pre-create runner.log with stale content; "a" mode must preserve it.
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        (d / "runner.log").write_text("STALE OLD CONTENT")
        with patch("subprocess.Popen", return_value=_mock_proc()):
            backend_cci.send("reviewer1", "hello", timeout=30)
        assert (d / "runner.log").read_text().startswith("STALE OLD CONTENT")

    def test_prompt_file_permissions_0600(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        captured = {}
        orig_mkstemp = backend_cci.tempfile.mkstemp

        def tracking(*a, **k):
            fd, path = orig_mkstemp(*a, **k)
            captured["path"] = path
            return fd, path

        with patch.object(backend_cci.tempfile, "mkstemp", side_effect=tracking), \
             patch("subprocess.Popen", return_value=_mock_proc()):
            backend_cci.send("reviewer1", "hello", timeout=30)
        mode = os.stat(captured["path"]).st_mode & 0o777
        assert mode == 0o600
        os.unlink(captured["path"])

    def test_mkstemp_failure_returns_fail(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        with patch.object(backend_cci.tempfile, "mkstemp", side_effect=OSError("no space")):
            result = backend_cci.send("reviewer1", "hello", timeout=30)
        assert result is SendResult.FAIL
        assert "reviewer1" not in backend_cci._starting_markers

    def test_popen_failure_returns_fail_and_unlinks_prompt(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        captured = {}
        orig_mkstemp = backend_cci.tempfile.mkstemp

        def tracking(*a, **k):
            fd, path = orig_mkstemp(*a, **k)
            captured["path"] = path
            return fd, path

        with patch.object(backend_cci.tempfile, "mkstemp", side_effect=tracking), \
             patch("subprocess.Popen", side_effect=OSError("boom")):
            result = backend_cci.send("reviewer1", "hello", timeout=30)
        assert result is SendResult.FAIL
        assert not os.path.exists(captured["path"])
        assert "reviewer1" not in backend_cci._starting_markers

    def test_success_persists_session_and_pid_and_marker(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        with patch("subprocess.Popen", return_value=_mock_proc(4242)):
            result = backend_cci.send("reviewer1", "hello", timeout=30)
        assert result is SendResult.OK
        d = tmp_sessions / "reviewer1"
        uuid.UUID((d / "session_id").read_text())
        assert (d / "pid").read_text() == "4242"
        assert "reviewer1" in backend_cci._starting_markers

    def test_live_owner_returns_busy_no_popen(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("12345")

        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return f"python3\0-m\0engine.cci_runner\0--resume\0{sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes), \
             patch("subprocess.Popen") as mp:
            result = backend_cci.send("reviewer1", "hello", timeout=30)
        assert result is SendResult.BUSY
        mp.assert_not_called()


# ===========================================================================
# ping
# ===========================================================================

class TestPing:
    def test_always_true(self):
        assert backend_cci.ping("reviewer1", timeout=10) is True


# ===========================================================================
# _check_session_ownership (runner cmdline matching)
# ===========================================================================

class TestCheckSessionOwnership:
    def _state(self, sid, pid="12345"):
        return backend_cci.PersistedCcState(session_id=sid, pid_text=pid)

    def _run(self, cmdline_bytes, pid="12345"):
        sid = str(uuid.uuid4())
        state = backend_cci.PersistedCcState(session_id=sid, pid_text=pid)
        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == f"/proc/{pid}":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == f"/proc/{pid}/cmdline":
                return cmdline_bytes(sid)
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            return sid, backend_cci._check_session_ownership(state)

    def test_runner_session_id_match_is_owner(self):
        _, ownership = self._run(
            lambda sid: f"python3\0-m\0engine.cci_runner\0--session-id\0{sid}\0".encode()
        )
        assert ownership.has_valid_session is True
        assert ownership.has_live_owner is True

    def test_runner_resume_match_is_owner(self):
        _, ownership = self._run(
            lambda sid: f"python3\0-m\0engine.cci_runner\0--resume\0{sid}\0".encode()
        )
        assert ownership.has_live_owner is True

    def test_uuid_present_but_not_after_flag_is_not_owner(self):
        # uuid appears as a bare token, not after --session-id/--resume
        _, ownership = self._run(
            lambda sid: f"python3\0-m\0engine.cci_runner\0{sid}\0".encode()
        )
        assert ownership.has_live_owner is False

    def test_no_runner_token_is_not_owner(self):
        _, ownership = self._run(
            lambda sid: f"python3\0-m\0some.other\0--session-id\0{sid}\0".encode()
        )
        assert ownership.has_live_owner is False

    def test_different_session_id_is_not_owner(self):
        other = str(uuid.uuid4())
        _, ownership = self._run(
            lambda sid: f"python3\0-m\0engine.cci_runner\0--session-id\0{other}\0".encode()
        )
        assert ownership.has_live_owner is False

    def test_dead_pid_is_not_owner(self):
        sid = str(uuid.uuid4())
        state = self._state(sid, pid="999999")
        orig_exists = Path.exists

        def mock_exists(self):
            if str(self) == "/proc/999999":
                return False
            return orig_exists(self)

        with patch.object(Path, "exists", mock_exists):
            ownership = backend_cci._check_session_ownership(state)
        assert ownership.has_valid_session is True
        assert ownership.has_live_owner is False


# ===========================================================================
# is_inactive
# ===========================================================================

class TestIsInactive:
    def test_cc_running_returns_false(self):
        assert backend_cci.is_inactive("reviewer1", cc_running=True) is False

    def test_missing_session_returns_true(self, tmp_sessions):
        assert backend_cci.is_inactive("reviewer1") is True

    def test_grace_no_jsonl_returns_false(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "CCI_START_GRACE_SEC", 60)
        backend_cci._starting_markers["reviewer1"] = __import__("time").time()
        assert backend_cci.is_inactive("reviewer1") is False

    def test_live_owner_returns_false(self, tmp_sessions, monkeypatch, tmp_path):
        monkeypatch.setattr("engine.backend_cci.AGENT_PROFILES_DIR", tmp_path / "agents")
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("12345")

        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return f"python3\0-m\0engine.cci_runner\0--resume\0{sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            assert backend_cci.is_inactive("reviewer1") is False

    def test_dead_pid_stale_mtime_returns_true(self, tmp_sessions, monkeypatch, tmp_path):
        monkeypatch.setattr("engine.backend_cci.INACTIVE_THRESHOLD_SEC", 300)
        monkeypatch.setattr("engine.backend_cci.AGENT_PROFILES_DIR", tmp_path / "agents")
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("999999")

        jsonl_dir = backend_cci._claude_project_dir(PROJECT_ROOT)
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = jsonl_dir / f"{sid}.jsonl"
        jsonl_path.write_text("{}")
        old = __import__("time").time() - 600
        os.utime(jsonl_path, (old, old))

        orig_exists = Path.exists

        def mock_exists(self):
            if str(self) == "/proc/999999":
                return False
            return orig_exists(self)

        with patch.object(Path, "exists", mock_exists):
            assert backend_cci.is_inactive("reviewer1") is True

    def _setup_dead_owner(self, tmp_sessions, monkeypatch, tmp_path):
        """Create a reviewer with a dead pid (no live owner) and a fresh jsonl."""
        monkeypatch.setattr("engine.backend_cci.INACTIVE_THRESHOLD_SEC", 300)
        monkeypatch.setattr("engine.backend_cci.AGENT_PROFILES_DIR", tmp_path / "agents")
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("999999")
        jsonl_dir = backend_cci._claude_project_dir(PROJECT_ROOT)
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        (jsonl_dir / f"{sid}.jsonl").write_text("{}")
        return d

    def test_last_error_logged_and_deleted(self, tmp_sessions, monkeypatch, tmp_path, caplog):
        d = self._setup_dead_owner(tmp_sessions, monkeypatch, tmp_path)
        (d / "last_error").write_text('{"reason": "startup_timeout", "ts": 1.0}')

        orig_exists = Path.exists

        def mock_exists(self):
            if str(self) == "/proc/999999":
                return False
            return orig_exists(self)

        with patch.object(Path, "exists", mock_exists), \
             caplog.at_level(logging.WARNING):
            backend_cci.is_inactive("reviewer1")
        assert "startup_timeout" in caplog.text
        assert not (d / "last_error").exists()

    def test_last_error_malformed_does_not_raise(self, tmp_sessions, monkeypatch, tmp_path):
        d = self._setup_dead_owner(tmp_sessions, monkeypatch, tmp_path)

        orig_exists = Path.exists

        def mock_exists(self):
            if str(self) == "/proc/999999":
                return False
            return orig_exists(self)

        for bad in ("", "[]", "not json", "123"):
            (d / "last_error").write_text(bad)
            with patch.object(Path, "exists", mock_exists):
                # must not raise on empty file, JSON array, scalar, or garbage
                backend_cci.is_inactive("reviewer1")
            assert not (d / "last_error").exists()


# ===========================================================================
# reset_session
# ===========================================================================

class TestResetSession:
    def test_deletes_files(self, tmp_sessions):
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        (d / "session_id").write_text(str(uuid.uuid4()))
        (d / "pid").write_text("")  # empty → no SIGTERM path
        (d / "last_error").write_text('{"reason": "startup_timeout"}')
        backend_cci.reset_session("reviewer1")
        assert not (d / "session_id").exists()
        assert not (d / "pid").exists()
        assert not (d / "last_error").exists()

    def test_clears_marker_and_rebuilds(self, tmp_sessions):
        backend_cci._starting_markers["reviewer1"] = 1.0
        with patch("engine.backend_cci._rebuild_claude_md") as mock_rebuild:
            backend_cci.reset_session("reviewer1")
        mock_rebuild.assert_called_once_with("reviewer1")
        assert "reviewer1" not in backend_cci._starting_markers

    def test_sends_sigterm_to_live_runner(self, tmp_sessions):
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        (d / "session_id").write_text(str(uuid.uuid4()))
        (d / "pid").write_text("12345")

        orig_read_bytes = Path.read_bytes

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return b"python3\0-m\0engine.cci_runner\0--resume\0xyz\0"
            return orig_read_bytes(self)

        with patch.object(Path, "read_bytes", mock_read_bytes), \
             patch("os.kill") as mock_kill:
            backend_cci.reset_session("reviewer1")
        import signal as _signal
        mock_kill.assert_called_once_with(12345, _signal.SIGTERM)
        assert not (d / "pid").exists()

    def test_pid_parse_error_swallowed(self, tmp_sessions):
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        (d / "pid").write_text("not-a-number")
        with patch("os.kill") as mock_kill:
            backend_cci.reset_session("reviewer1")  # must not raise
        mock_kill.assert_not_called()

    def test_absent_files_noop(self, tmp_sessions):
        backend_cci.reset_session("nonexistent")  # must not raise


# ---------------------------------------------------------------------------
# Helpers for procfs mocking (#377)
# ---------------------------------------------------------------------------

def _patch_proc(monkeypatch, mapping: dict[str, object]) -> None:
    """Patch Path.read_text so /proc reads return canned content.

    Keys are posix-style paths (e.g. "/proc/123/stat"). A value that is an
    Exception instance is raised; otherwise it is returned as the text.
    Unmatched paths fall through to the real read_text.
    """
    orig = Path.read_text

    def fake(self, *a, **k):
        key = self.as_posix()
        if key in mapping:
            val = mapping[key]
            if isinstance(val, Exception):
                raise val
            return val
        return orig(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", fake)


def _stat_line(pid: int, state: str = "S", starttime: int = 1000) -> str:
    """Build a minimal /proc/<pid>/stat line. comm has a ')' to exercise rfind."""
    # field1=pid, field2=comm "(claude)", field3=state, then padding up to
    # field22=starttime. After comm: index0=state ... index19=starttime.
    after_comm = [state] + ["0"] * 18 + [str(starttime)]
    return f"{pid} (claude) " + " ".join(after_comm) + "\n"


# ---------------------------------------------------------------------------
# _proc_age_sec (#377)
# ---------------------------------------------------------------------------

class TestProcAgeSec:
    def test_proc_age_sec_normal(self, monkeypatch):
        _patch_proc(monkeypatch, {
            "/proc/123/stat": _stat_line(123, starttime=1000),
            "/proc/uptime": "5000.0 1234.5\n",
        })
        monkeypatch.setattr(os, "sysconf", lambda name: 100)
        # age = uptime - starttime/clk = 5000 - 1000/100 = 4990
        assert backend_cci._proc_age_sec(123) == 4990.0

    def test_proc_age_sec_missing_proc(self, monkeypatch):
        _patch_proc(monkeypatch, {
            "/proc/123/stat": FileNotFoundError(),
        })
        assert backend_cci._proc_age_sec(123) is None

    def test_proc_age_sec_malformed_stat(self, monkeypatch):
        _patch_proc(monkeypatch, {
            "/proc/123/stat": "no paren here\n",
            "/proc/uptime": "5000.0 1234.5\n",
        })
        assert backend_cci._proc_age_sec(123) is None


# ---------------------------------------------------------------------------
# _is_zombie (#377)
# ---------------------------------------------------------------------------

class TestIsZombie:
    def test_is_zombie_true(self, monkeypatch):
        _patch_proc(monkeypatch, {"/proc/123/stat": _stat_line(123, state="Z")})
        assert backend_cci._is_zombie(123) is True

    def test_is_zombie_false(self, monkeypatch):
        _patch_proc(monkeypatch, {"/proc/123/stat": _stat_line(123, state="S")})
        assert backend_cci._is_zombie(123) is False

    def test_is_zombie_missing_proc(self, monkeypatch):
        _patch_proc(monkeypatch, {"/proc/123/stat": FileNotFoundError()})
        assert backend_cci._is_zombie(123) is False


# ---------------------------------------------------------------------------
# _kill_pid (#377)
# ---------------------------------------------------------------------------

class TestKillPid:
    def test_kill_pid_already_gone(self):
        with patch("os.kill", side_effect=ProcessLookupError()) as mk:
            assert backend_cci._kill_pid(999, "x") is True
        mk.assert_called_once_with(999, signal.SIGTERM)

    def test_kill_pid_eperm_on_signal(self):
        with patch("os.kill", side_effect=PermissionError()):
            assert backend_cci._kill_pid(999, "x") is False

    def test_kill_pid_sigterm_success(self, monkeypatch):
        # SIGTERM ok, first probe → ESRCH → gone.
        calls = []

        def fake_kill(pid, sig):
            calls.append(sig)
            if sig == 0:
                raise OSError(errno.ESRCH, "no such process")

        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(backend_cci, "_is_zombie", lambda pid: False)
        assert backend_cci._kill_pid(123, "x") is True
        assert signal.SIGKILL not in calls

    def test_kill_pid_zombie_after_sigterm(self, monkeypatch):
        # SIGTERM ok, probe alive but process is a zombie → gone.
        def fake_kill(pid, sig):
            return  # all calls succeed (probe says "alive")

        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(backend_cci, "_is_zombie", lambda pid: True)
        assert backend_cci._kill_pid(123, "x") is True

    def test_kill_pid_eperm_on_probe(self, monkeypatch):
        # SIGTERM ok, probe raises EPERM → PID reused → original gone.
        def fake_kill(pid, sig):
            if sig == 0:
                raise OSError(errno.EPERM, "operation not permitted")

        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(backend_cci, "_is_zombie", lambda pid: False)
        assert backend_cci._kill_pid(123, "x") is True

    def test_kill_pid_sigkill_escalation(self, monkeypatch):
        # Survives SIGTERM (probe alive through grace), dies after SIGKILL.
        state = {"sigkilled": False}

        def fake_kill(pid, sig):
            if sig == signal.SIGKILL:
                state["sigkilled"] = True
                return
            if sig == 0:
                if state["sigkilled"]:
                    raise OSError(errno.ESRCH, "no such process")
                return  # alive
            return  # SIGTERM ok

        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(backend_cci, "_is_zombie", lambda pid: False)
        # monotonic: A, B1(<deadline), B2(>=deadline → exit), C, D1(<kill_deadline)
        mono = iter([0.0, 0.0, 6.0, 6.0, 6.0, 6.0])
        monkeypatch.setattr("time.monotonic", lambda: next(mono))
        assert backend_cci._kill_pid(123, "x") is True
        assert state["sigkilled"] is True

    def test_kill_pid_unkillable(self, monkeypatch):
        # Survives both SIGTERM and SIGKILL (never zombie) → False.
        def fake_kill(pid, sig):
            return  # everything succeeds; probe always "alive"

        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(backend_cci, "_is_zombie", lambda pid: False)
        # A, B1(<d), B2(exit), C, D1(<d), D2(exit)
        mono = iter([0.0, 0.0, 6.0, 6.0, 6.0, 12.0])
        monkeypatch.setattr("time.monotonic", lambda: next(mono))
        assert backend_cci._kill_pid(123, "x") is False


# ---------------------------------------------------------------------------
# _reap_stale_owner (#377)
# ---------------------------------------------------------------------------

class TestReapStaleOwner:
    @pytest.fixture
    def _max_age(self):
        return (
            config.CCI_COMPLETION_TIMEOUT_SEC
            + config.CCI_BOOT_GRACE_SEC
            + config.CCI_REAP_MARGIN_SEC
        )

    def test_reap_stale_owner_young(self, monkeypatch, _max_age):
        monkeypatch.setattr(backend_cci, "_proc_age_sec", lambda pid: _max_age - 1)
        kill = MagicMock()
        monkeypatch.setattr(backend_cci, "_kill_pid", kill)
        assert backend_cci._reap_stale_owner(100, "r1") is False
        kill.assert_not_called()

    def test_reap_stale_owner_age_none(self, monkeypatch):
        monkeypatch.setattr(backend_cci, "_proc_age_sec", lambda pid: None)
        kill = MagicMock()
        monkeypatch.setattr(backend_cci, "_kill_pid", kill)
        assert backend_cci._reap_stale_owner(100, "r1") is False
        kill.assert_not_called()

    def test_reap_stale_owner_kills_child_first(self, monkeypatch, _max_age):
        monkeypatch.setattr(backend_cci, "_proc_age_sec", lambda pid: _max_age + 1)
        monkeypatch.setattr(backend_cci, "_read_child_pid", lambda aid: (200, 1000))
        monkeypatch.setattr(backend_cci, "_proc_starttime_ticks", lambda pid: 1000)
        order = []

        def fake_kill(pid, label):
            order.append(pid)
            return True

        monkeypatch.setattr(backend_cci, "_kill_pid", fake_kill)
        assert backend_cci._reap_stale_owner(100, "r1") is True
        assert order == [200, 100]  # child before runner

    def test_reap_stale_owner_child_pid_reused_falls_through(self, monkeypatch, _max_age):
        monkeypatch.setattr(backend_cci, "_proc_age_sec", lambda pid: _max_age + 1)
        monkeypatch.setattr(backend_cci, "_read_child_pid", lambda aid: (200, 1000))
        # current starttime differs → PID reused → don't kill recorded child.
        monkeypatch.setattr(backend_cci, "_proc_starttime_ticks", lambda pid: 9999)
        monkeypatch.setattr(backend_cci, "_proc_children", lambda pid: [201])
        killed = []

        def fake_kill(pid, label):
            killed.append(pid)
            return True

        monkeypatch.setattr(backend_cci, "_kill_pid", fake_kill)
        assert backend_cci._reap_stale_owner(100, "r1") is True
        assert 200 not in killed  # stale recorded child NOT killed
        assert killed == [201, 100]  # discovered child + runner

    def test_reap_stale_owner_stale_child_pid_no_live_children(self, monkeypatch, _max_age):
        monkeypatch.setattr(backend_cci, "_proc_age_sec", lambda pid: _max_age + 1)
        monkeypatch.setattr(backend_cci, "_read_child_pid", lambda aid: (200, 1000))
        monkeypatch.setattr(backend_cci, "_proc_starttime_ticks", lambda pid: None)
        monkeypatch.setattr(backend_cci, "_proc_children", lambda pid: [])
        killed = []
        monkeypatch.setattr(
            backend_cci, "_kill_pid",
            lambda pid, label: killed.append(pid) or True,
        )
        assert backend_cci._reap_stale_owner(100, "r1") is True
        assert killed == [100]  # only the runner

    def test_reap_stale_owner_stale_child_pid_proc_unreadable(self, monkeypatch, _max_age):
        monkeypatch.setattr(backend_cci, "_proc_age_sec", lambda pid: _max_age + 1)
        monkeypatch.setattr(backend_cci, "_read_child_pid", lambda aid: (200, 1000))
        monkeypatch.setattr(backend_cci, "_proc_starttime_ticks", lambda pid: None)
        monkeypatch.setattr(backend_cci, "_proc_children", lambda pid: None)
        kill = MagicMock(return_value=True)
        monkeypatch.setattr(backend_cci, "_kill_pid", kill)
        assert backend_cci._reap_stale_owner(100, "r1") is False
        kill.assert_not_called()

    def test_reap_stale_owner_no_child_pid_discovers_child(self, monkeypatch, _max_age):
        monkeypatch.setattr(backend_cci, "_proc_age_sec", lambda pid: _max_age + 1)
        monkeypatch.setattr(backend_cci, "_read_child_pid", lambda aid: None)
        monkeypatch.setattr(backend_cci, "_proc_children", lambda pid: [201])
        killed = []
        monkeypatch.setattr(
            backend_cci, "_kill_pid",
            lambda pid, label: killed.append(pid) or True,
        )
        assert backend_cci._reap_stale_owner(100, "r1") is True
        assert killed == [201, 100]

    def test_reap_stale_owner_no_child_pid_no_children(self, monkeypatch, _max_age):
        monkeypatch.setattr(backend_cci, "_proc_age_sec", lambda pid: _max_age + 1)
        monkeypatch.setattr(backend_cci, "_read_child_pid", lambda aid: None)
        monkeypatch.setattr(backend_cci, "_proc_children", lambda pid: [])
        killed = []
        monkeypatch.setattr(
            backend_cci, "_kill_pid",
            lambda pid, label: killed.append(pid) or True,
        )
        assert backend_cci._reap_stale_owner(100, "r1") is True
        assert killed == [100]

    def test_reap_stale_owner_no_child_pid_proc_unreadable(self, monkeypatch, _max_age):
        monkeypatch.setattr(backend_cci, "_proc_age_sec", lambda pid: _max_age + 1)
        monkeypatch.setattr(backend_cci, "_read_child_pid", lambda aid: None)
        monkeypatch.setattr(backend_cci, "_proc_children", lambda pid: None)
        kill = MagicMock(return_value=True)
        monkeypatch.setattr(backend_cci, "_kill_pid", kill)
        assert backend_cci._reap_stale_owner(100, "r1") is False
        kill.assert_not_called()

    def test_reap_stale_owner_child_kill_fails(self, monkeypatch, _max_age):
        monkeypatch.setattr(backend_cci, "_proc_age_sec", lambda pid: _max_age + 1)
        monkeypatch.setattr(backend_cci, "_read_child_pid", lambda aid: (200, 1000))
        monkeypatch.setattr(backend_cci, "_proc_starttime_ticks", lambda pid: 1000)
        killed = []

        def fake_kill(pid, label):
            killed.append(pid)
            return pid != 200  # child (200) fails

        monkeypatch.setattr(backend_cci, "_kill_pid", fake_kill)
        assert backend_cci._reap_stale_owner(100, "r1") is False
        assert killed == [200]  # runner never attempted

    def test_reap_stale_owner_child_zombie(self, monkeypatch, _max_age):
        # _kill_pid returns True for a zombie child; runner then killed too.
        monkeypatch.setattr(backend_cci, "_proc_age_sec", lambda pid: _max_age + 1)
        monkeypatch.setattr(backend_cci, "_read_child_pid", lambda aid: (200, 1000))
        monkeypatch.setattr(backend_cci, "_proc_starttime_ticks", lambda pid: 1000)
        killed = []
        monkeypatch.setattr(
            backend_cci, "_kill_pid",
            lambda pid, label: killed.append(pid) or True,
        )
        assert backend_cci._reap_stale_owner(100, "r1") is True
        assert killed == [200, 100]

    def test_reap_stale_owner_runner_kill_fails(self, monkeypatch, _max_age):
        monkeypatch.setattr(backend_cci, "_proc_age_sec", lambda pid: _max_age + 1)
        monkeypatch.setattr(backend_cci, "_read_child_pid", lambda aid: (200, 1000))
        monkeypatch.setattr(backend_cci, "_proc_starttime_ticks", lambda pid: 1000)

        def fake_kill(pid, label):
            return pid != 100  # runner (100) fails

        monkeypatch.setattr(backend_cci, "_kill_pid", fake_kill)
        assert backend_cci._reap_stale_owner(100, "r1") is False


# ---------------------------------------------------------------------------
# soft_reap (#377)
# ---------------------------------------------------------------------------

class TestSoftReap:
    def test_soft_reap_no_live_owner(self, monkeypatch):
        monkeypatch.setattr(
            backend_cci, "_check_session_ownership",
            lambda state: backend_cci.SessionOwnership(
                state=state, has_valid_session=True, has_live_owner=False,
            ),
        )
        reap = MagicMock()
        monkeypatch.setattr(backend_cci, "_reap_stale_owner", reap)
        backend_cci.soft_reap("r1")
        reap.assert_not_called()

    def test_soft_reap_dry_run(self, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", True)
        reap = MagicMock()
        monkeypatch.setattr(backend_cci, "_reap_stale_owner", reap)
        backend_cci.soft_reap("r1")
        reap.assert_not_called()

    def test_soft_reap_reaps_and_clears_files(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        d = tmp_sessions / "r1"
        d.mkdir(parents=True)
        (d / "session_id").write_text(str(uuid.uuid4()))
        (d / "pid").write_text("100")
        (d / "child_pid").write_text("200 1000")
        backend_cci._starting_markers["r1"] = 1.0

        monkeypatch.setattr(
            backend_cci, "_check_session_ownership",
            lambda state: backend_cci.SessionOwnership(
                state=state, has_valid_session=True, has_live_owner=True,
            ),
        )
        monkeypatch.setattr(backend_cci, "_reap_stale_owner", lambda pid, aid: True)

        backend_cci.soft_reap("r1")

        assert not (d / "pid").exists()
        assert not (d / "child_pid").exists()
        assert (d / "session_id").exists()  # session preserved
        assert "r1" not in backend_cci._starting_markers
