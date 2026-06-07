"""Tests for engine/backend_cci.py — cci backend for agent communication."""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_stderr_runner_log_opened_w_mode(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cci, "_agent_config_cache", {})
        # Pre-create runner.log with stale content; "w" mode must truncate it.
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        (d / "runner.log").write_text("STALE OLD CONTENT")
        with patch("subprocess.Popen", return_value=_mock_proc()):
            backend_cci.send("reviewer1", "hello", timeout=30)
        assert (d / "runner.log").read_text() == ""

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


# ===========================================================================
# reset_session
# ===========================================================================

class TestResetSession:
    def test_deletes_files(self, tmp_sessions):
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        (d / "session_id").write_text(str(uuid.uuid4()))
        (d / "pid").write_text("")  # empty → no SIGTERM path
        backend_cci.reset_session("reviewer1")
        assert not (d / "session_id").exists()
        assert not (d / "pid").exists()

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
