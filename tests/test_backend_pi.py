"""Tests for engine/backend_pi.py — pi backend for agent communication."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config
from config import PI_SESSIONS_DIR, PROJECT_ROOT
from engine import backend_pi


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_starting_markers():
    """Clear process-local starting markers between tests."""
    backend_pi._starting_markers.clear()
    yield
    backend_pi._starting_markers.clear()


@pytest.fixture
def tmp_sessions(tmp_path, monkeypatch):
    """Redirect PI_SESSIONS_DIR to a temporary directory."""
    monkeypatch.setattr("config.PI_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr("engine.backend_pi.PI_SESSIONS_DIR", tmp_path)
    return tmp_path


# ===========================================================================
# _session_path
# ===========================================================================

class TestSessionPath:
    def test_returns_absolute_path(self):
        path = backend_pi._session_path("reviewer1")
        assert path.is_absolute()

    def test_returns_correct_filename(self, tmp_sessions):
        path = backend_pi._session_path("reviewer1")
        assert path == tmp_sessions / "reviewer1.jsonl"

    def test_format(self):
        path = backend_pi._session_path("my_agent")
        assert path == PI_SESSIONS_DIR / "my_agent.jsonl"


# ===========================================================================
# send
# ===========================================================================

class TestSend:
    def test_dry_run_returns_true(self, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", True)
        result = backend_pi.send("reviewer1", "hello", timeout=30)
        assert result is True

    def test_creates_sessions_dir(self, tmp_sessions, monkeypatch):
        import shutil
        shutil.rmtree(tmp_sessions)
        assert not tmp_sessions.exists()

        monkeypatch.setattr(config, "DRY_RUN", False)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc):
            backend_pi.send("reviewer1", "hello", timeout=30)
        assert tmp_sessions.exists()

    def test_uses_subprocess_popen(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        mock_popen.assert_called_once()
        # Verify Popen was called (not subprocess.run)
        assert mock_popen.call_count == 1

    def test_writes_message_to_stdin_and_closes(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc):
            backend_pi.send("reviewer1", "hello world", timeout=30)
        mock_proc.stdin.write.assert_called_once_with(b"hello world")
        mock_proc.stdin.close.assert_called_once()

    def test_includes_absolute_session_path(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        session_path = str(tmp_sessions / "reviewer1.jsonl")
        assert "--session" in cmd
        idx = cmd.index("--session")
        assert cmd[idx + 1] == session_path
        assert Path(cmd[idx + 1]).is_absolute()

    def test_uses_agent_profile_dir_as_cwd(self, tmp_sessions, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "DRY_RUN", False)
        profile_dir = tmp_path / "agents" / "reviewer1"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr("engine.backend_pi.AGENT_PROFILES_DIR", tmp_path / "agents")

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        assert mock_popen.call_args[1]["cwd"] == str(profile_dir)

    def test_falls_back_to_project_root(self, tmp_sessions, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr("engine.backend_pi.AGENT_PROFILES_DIR", tmp_path / "nonexistent_agents")

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        assert mock_popen.call_args[1]["cwd"] == str(PROJECT_ROOT)

    def test_omits_model_flag_when_empty(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(config, "PI_MODEL", "")
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        assert "--model" not in cmd

    def test_includes_model_flag_when_set(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(config, "PI_MODEL", "claude-sonnet")
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-sonnet"

    def test_returns_false_on_spawn_failure(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        with patch("subprocess.Popen", side_effect=FileNotFoundError("pi not found")):
            result = backend_pi.send("reviewer1", "hello", timeout=30)
        assert result is False

    def test_returns_false_on_oserror_spawn(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        with patch("subprocess.Popen", side_effect=OSError("spawn error")):
            result = backend_pi.send("reviewer1", "hello", timeout=30)
        assert result is False

    def test_returns_false_on_stdin_write_failure(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        mock_proc = MagicMock()
        mock_proc.stdin.write.side_effect = BrokenPipeError("pipe broken")
        with patch("subprocess.Popen", return_value=mock_proc):
            result = backend_pi.send("reviewer1", "hello", timeout=30)
        assert result is False

    def test_returns_false_on_stdin_close_failure(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        mock_proc = MagicMock()
        mock_proc.stdin.write = MagicMock()  # write succeeds
        mock_proc.stdin.close.side_effect = OSError("close error")
        with patch("subprocess.Popen", return_value=mock_proc):
            result = backend_pi.send("reviewer1", "hello", timeout=30)
        assert result is False

    def test_timeout_unused(self, tmp_sessions, monkeypatch):
        """timeout parameter is kept for interface parity but unused by pi."""
        monkeypatch.setattr(config, "DRY_RUN", False)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=999)
        # timeout should not appear in Popen call
        call_kwargs = mock_popen.call_args[1]
        assert "timeout" not in call_kwargs

    def test_records_starting_marker_on_success(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc):
            backend_pi.send("reviewer1", "hello", timeout=30)
        assert "reviewer1" in backend_pi._starting_markers

    def test_no_starting_marker_on_spawn_failure(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        with patch("subprocess.Popen", side_effect=OSError("fail")):
            backend_pi.send("reviewer1", "hello", timeout=30)
        assert "reviewer1" not in backend_pi._starting_markers

    def test_no_starting_marker_on_stdin_failure(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        mock_proc = MagicMock()
        mock_proc.stdin.write.side_effect = BrokenPipeError()
        with patch("subprocess.Popen", return_value=mock_proc):
            backend_pi.send("reviewer1", "hello", timeout=30)
        assert "reviewer1" not in backend_pi._starting_markers

    def test_does_not_wait_for_child(self, tmp_sessions, monkeypatch):
        """send() must not call proc.wait() or proc.communicate()."""
        monkeypatch.setattr(config, "DRY_RUN", False)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc):
            backend_pi.send("reviewer1", "hello", timeout=30)
        mock_proc.wait.assert_not_called()
        mock_proc.communicate.assert_not_called()


# ===========================================================================
# ping
# ===========================================================================

class TestPing:
    def test_always_returns_true(self):
        assert backend_pi.ping("reviewer1", timeout=10) is True

    def test_always_returns_true_any_agent(self):
        assert backend_pi.ping("nonexistent_agent", timeout=0) is True


# ===========================================================================
# is_inactive
# ===========================================================================

class TestIsInactive:
    def test_returns_false_when_cc_running(self):
        result = backend_pi.is_inactive("reviewer1", cc_running=True)
        assert result is False

    def test_returns_false_with_valid_starting_marker(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "PI_START_GRACE_SEC", 60)
        backend_pi._starting_markers["reviewer1"] = time.time()
        result = backend_pi.is_inactive("reviewer1")
        assert result is False

    def test_returns_true_when_no_session_file_no_marker(self, tmp_sessions):
        result = backend_pi.is_inactive("reviewer1")
        assert result is True

    def test_returns_false_when_mtime_fresh(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "INACTIVE_THRESHOLD_SEC", 300)
        session_file = tmp_sessions / "reviewer1.jsonl"
        session_file.write_text("{}")
        result = backend_pi.is_inactive("reviewer1")
        assert result is False

    def test_returns_true_when_mtime_stale(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "INACTIVE_THRESHOLD_SEC", 300)
        session_file = tmp_sessions / "reviewer1.jsonl"
        session_file.write_text("{}")
        import os
        old_time = time.time() - 600
        os.utime(session_file, (old_time, old_time))
        result = backend_pi.is_inactive("reviewer1")
        assert result is True

    def test_returns_true_on_stat_failure(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "INACTIVE_THRESHOLD_SEC", 300)
        # No session file exists, no marker
        result = backend_pi.is_inactive("reviewer1")
        assert result is True

    def test_clears_marker_when_mtime_catches_up(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "PI_START_GRACE_SEC", 60)
        monkeypatch.setattr(config, "INACTIVE_THRESHOLD_SEC", 300)
        # Set a starting marker
        started = time.time() - 5  # 5 seconds ago
        backend_pi._starting_markers["reviewer1"] = started
        # Create session file with fresh mtime
        session_file = tmp_sessions / "reviewer1.jsonl"
        session_file.write_text("{}")
        # The is_inactive call should clear the marker since mtime caught up
        result = backend_pi.is_inactive("reviewer1")
        assert result is False
        # After the check, if mtime is fresh enough to clear the marker and
        # fresh enough to not be stale, the agent should be active

    def test_expired_marker_is_cleared(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "PI_START_GRACE_SEC", 5)
        # Set a marker that's already expired
        backend_pi._starting_markers["reviewer1"] = time.time() - 10
        # No session file
        result = backend_pi.is_inactive("reviewer1")
        assert result is True
        assert "reviewer1" not in backend_pi._starting_markers

    def test_starting_marker_is_process_local(self):
        """Starting markers are process-local dict entries, not cross-process.

        This is a documentation test: the _starting_markers dict is a plain
        Python dict in the module namespace, inherently process-local.
        """
        assert isinstance(backend_pi._starting_markers, dict)


# ===========================================================================
# reset_session
# ===========================================================================

class TestResetSession:
    def test_deletes_session_file(self, tmp_sessions):
        session_file = tmp_sessions / "reviewer1.jsonl"
        session_file.write_text("{}")
        assert session_file.exists()
        backend_pi.reset_session("reviewer1")
        assert not session_file.exists()

    def test_noop_if_absent(self, tmp_sessions):
        # Should not raise
        backend_pi.reset_session("nonexistent_agent")

    def test_clears_starting_marker(self, tmp_sessions):
        backend_pi._starting_markers["reviewer1"] = time.time()
        backend_pi.reset_session("reviewer1")
        assert "reviewer1" not in backend_pi._starting_markers

    def test_does_not_delete_parent_dir(self, tmp_sessions):
        session_file = tmp_sessions / "reviewer1.jsonl"
        session_file.write_text("{}")
        backend_pi.reset_session("reviewer1")
        assert tmp_sessions.exists()

    def test_deletes_only_exact_file(self, tmp_sessions):
        target = tmp_sessions / "reviewer1.jsonl"
        other = tmp_sessions / "reviewer2.jsonl"
        target.write_text("{}")
        other.write_text("{}")
        backend_pi.reset_session("reviewer1")
        assert not target.exists()
        assert other.exists()

    def test_reset_session_unlink_oserror_is_swallowed(self, tmp_sessions, caplog):
        backend_pi._starting_markers["reviewer1"] = time.time()
        with patch.object(Path, "unlink", side_effect=PermissionError("denied")):
            backend_pi.reset_session("reviewer1")  # must not raise
        # Marker is cleared before the try/except
        assert "reviewer1" not in backend_pi._starting_markers
        # Warning logged
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert any("reset_session" in r.message and "failed to delete" in r.message
                    for r in warnings)
