"""Tests for engine/backend_cc.py — cc backend for agent communication."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
import uuid
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config
from config import PROJECT_ROOT
from engine import backend_cc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_starting_markers():
    """Clear process-local starting markers between tests."""
    backend_cc._starting_markers.clear()
    yield
    backend_cc._starting_markers.clear()


@pytest.fixture(autouse=True)
def _reset_agent_config_cache():
    """Reset _agent_config_cache between tests to prevent cross-test pollution."""
    backend_cc._agent_config_cache = None
    yield
    backend_cc._agent_config_cache = None


@pytest.fixture
def tmp_sessions(tmp_path, monkeypatch):
    """Redirect CC_SESSIONS_DIR to a temporary directory."""
    monkeypatch.setattr("config.CC_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr("engine.backend_cc.CC_SESSIONS_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _tmp_claude_home(tmp_path, monkeypatch):
    """Redirect Path.home() so Claude jsonl paths stay inside tmp_path."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_dir))
    return home_dir


# ===========================================================================
# _load_config
# ===========================================================================

class TestLoadConfig:
    def test_file_missing_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "engine.backend_cc.CC_AGENT_CONFIG",
            tmp_path / "nonexistent.json",
        )
        result = backend_cc._load_config()
        assert result == {}

    def test_valid_json_returns_parsed(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config_cc.json"
        cfg_file.write_text(json.dumps({
            "reviewer1": {"model": "opus", "thinking": True},
        }))
        monkeypatch.setattr("engine.backend_cc.CC_AGENT_CONFIG", cfg_file)
        result = backend_cc._load_config()
        assert result == {"reviewer1": {"model": "opus", "thinking": True}}

    def test_invalid_json_returns_empty_with_warning(self, monkeypatch, tmp_path, caplog):
        cfg_file = tmp_path / "config_cc.json"
        cfg_file.write_text("{invalid json")
        monkeypatch.setattr("engine.backend_cc.CC_AGENT_CONFIG", cfg_file)
        with caplog.at_level(logging.WARNING):
            result = backend_cc._load_config()
        assert result == {}
        assert any("Invalid JSON" in r.message for r in caplog.records)

    def test_empty_file_returns_empty(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config_cc.json"
        cfg_file.write_text("   ")
        monkeypatch.setattr("engine.backend_cc.CC_AGENT_CONFIG", cfg_file)
        result = backend_cc._load_config()
        assert result == {}

    def test_non_dict_root_returns_empty_with_warning(self, monkeypatch, tmp_path, caplog):
        cfg_file = tmp_path / "config_cc.json"
        cfg_file.write_text("[]")
        monkeypatch.setattr("engine.backend_cc.CC_AGENT_CONFIG", cfg_file)
        with caplog.at_level(logging.WARNING):
            result = backend_cc._load_config()
        assert result == {}
        assert any("Expected JSON object" in r.message for r in caplog.records)

    def test_non_dict_entry_filtered_with_warning(self, monkeypatch, tmp_path, caplog):
        cfg_file = tmp_path / "config_cc.json"
        cfg_file.write_text(json.dumps({
            "good": {"model": "x"},
            "bad": "just-a-string",
        }))
        monkeypatch.setattr("engine.backend_cc.CC_AGENT_CONFIG", cfg_file)
        with caplog.at_level(logging.WARNING):
            result = backend_cc._load_config()
        assert result == {"good": {"model": "x"}}
        assert any("Skipped non-dict" in r.message for r in caplog.records)

    def test_caching_returns_same_object(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config_cc.json"
        cfg_file.write_text(json.dumps({"a": {"model": "m"}}))
        monkeypatch.setattr("engine.backend_cc.CC_AGENT_CONFIG", cfg_file)
        first = backend_cc._load_config()
        second = backend_cc._load_config()
        assert first is second


# ===========================================================================
# Path helpers
# ===========================================================================

class TestPathHelpers:
    def test_session_dir(self, tmp_sessions):
        path = backend_cc._session_dir("reviewer1")
        assert path == tmp_sessions / "reviewer1"

    def test_session_id_path(self, tmp_sessions):
        path = backend_cc._session_id_path("reviewer1")
        assert path == tmp_sessions / "reviewer1" / "session_id"

    def test_pid_path(self, tmp_sessions):
        path = backend_cc._pid_path("reviewer1")
        assert path == tmp_sessions / "reviewer1" / "pid"

    def test_claude_project_dir_converts_slashes(self):
        cwd = Path("/mnt/s/wsl/work/project/gokrax")
        result = backend_cc._claude_project_dir(cwd)
        expected_key = str(cwd.resolve()).replace("/", "-")
        assert result == Path.home() / ".claude" / "projects" / expected_key

    def test_claude_session_jsonl_path(self):
        cwd = Path("/mnt/s/wsl/work/project/gokrax")
        sid = "abc-123"
        result = backend_cc._claude_session_jsonl_path(cwd, sid)
        assert result.name == "abc-123.jsonl"
        assert result.parent == backend_cc._claude_project_dir(cwd)


# ===========================================================================
# _read_session_id
# ===========================================================================

class TestReadSessionId:
    def test_missing_file_returns_none(self, tmp_sessions):
        assert backend_cc._read_session_id("nonexistent") is None

    def test_empty_file_returns_none(self, tmp_sessions):
        d = tmp_sessions / "agent1"
        d.mkdir()
        (d / "session_id").write_text("")
        assert backend_cc._read_session_id("agent1") is None

    def test_invalid_uuid_returns_none(self, tmp_sessions):
        d = tmp_sessions / "agent1"
        d.mkdir()
        (d / "session_id").write_text("not-a-uuid")
        assert backend_cc._read_session_id("agent1") is None

    def test_valid_uuid_returns_string(self, tmp_sessions):
        d = tmp_sessions / "agent1"
        d.mkdir()
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        assert backend_cc._read_session_id("agent1") == sid


# ===========================================================================
# send
# ===========================================================================

class TestSend:
    def test_dry_run_returns_true(self, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", True)
        result = backend_cc.send("reviewer1", "hello", timeout=30)
        assert result is True

    def test_creates_sessions_dir(self, tmp_sessions, monkeypatch, tmp_path):
        import shutil
        shutil.rmtree(tmp_sessions)
        assert not tmp_sessions.exists()

        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc):
            backend_cc.send("reviewer1", "hello", timeout=30)
        assert tmp_sessions.exists()

    def test_new_session_uses_session_id_flag(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        assert "--session-id" in cmd
        assert "--resume" not in cmd

    def test_existing_valid_uuid_uses_resume(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        # Create a valid session_id file
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == sid
        assert "--session-id" not in cmd

    def test_invalid_session_id_file_fallback_to_new(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        (d / "session_id").write_text("not-valid-uuid")
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        assert "--session-id" in cmd
        assert "--resume" not in cmd

    def test_model_flag(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {
            "reviewer1": {"model": "opus"},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "opus"

    def test_thinking_enabled_adds_flag_with_mode(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {
            "reviewer1": {"thinking": "enabled"},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--thinking")
        assert cmd[idx + 1] == "enabled"

    def test_thinking_disabled_adds_flag_with_mode(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {
            "reviewer1": {"thinking": "disabled"},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--thinking")
        assert cmd[idx + 1] == "disabled"

    def test_thinking_adaptive_adds_flag_with_mode(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {
            "reviewer1": {"thinking": "adaptive"},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--thinking")
        assert cmd[idx + 1] == "adaptive"

    def test_thinking_invalid_value_warning(self, tmp_sessions, monkeypatch, caplog):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {
            "reviewer1": {"thinking": "bogus"},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with caplog.at_level(logging.WARNING):
            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        assert "--thinking" not in cmd
        assert any("invalid value" in r.message for r in caplog.records)

    def test_thinking_bool_true_fallback_to_enabled(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {
            "reviewer1": {"thinking": True},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--thinking")
        assert cmd[idx + 1] == "enabled"

    def test_thinking_bool_false_fallback_to_disabled(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {
            "reviewer1": {"thinking": False},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--thinking")
        assert cmd[idx + 1] == "disabled"

    def test_effort_flag(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {
            "reviewer1": {"effort": "medium"},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "medium"

    def test_invalid_effort_warning(self, tmp_sessions, monkeypatch, caplog):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {
            "reviewer1": {"effort": 42},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with caplog.at_level(logging.WARNING):
            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        assert "--effort" not in cmd
        assert any("invalid value" in r.message for r in caplog.records)

    def test_dangerously_skip_permissions_always_added(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd

    def test_cwd_agent_profile_dir(self, tmp_sessions, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        profile_dir = tmp_path / "agents" / "reviewer1"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "agents")

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        assert mock_popen.call_args[1]["cwd"] == str(profile_dir)

    def test_cwd_falls_back_to_project_root(self, tmp_sessions, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "nonexistent_agents")

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        assert mock_popen.call_args[1]["cwd"] == str(PROJECT_ROOT)

    def test_start_new_session_true(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_cc.send("reviewer1", "hello", timeout=30)
        assert mock_popen.call_args[1]["start_new_session"] is True

    def test_stdin_write_and_close(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc):
            backend_cc.send("reviewer1", "hello world", timeout=30)
        mock_proc.stdin.write.assert_called_once_with(b"hello world")
        mock_proc.stdin.close.assert_called_once()

    def test_success_saves_session_id_and_pid(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc):
            result = backend_cc.send("reviewer1", "hello", timeout=30)
        assert result is True
        sid_path = tmp_sessions / "reviewer1" / "session_id"
        pid_path = tmp_sessions / "reviewer1" / "pid"
        assert sid_path.exists()
        assert pid_path.exists()
        # Validate session_id is a valid UUID
        uuid.UUID(sid_path.read_text())
        assert pid_path.read_text() == "12345"

    def test_spawn_failure_returns_false(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        with patch("subprocess.Popen", side_effect=FileNotFoundError("claude not found")):
            result = backend_cc.send("reviewer1", "hello", timeout=30)
        assert result is False

    def test_stdin_failure_returns_false_no_state(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin.write.side_effect = BrokenPipeError("pipe broken")
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc):
            result = backend_cc.send("reviewer1", "hello", timeout=30)
        assert result is False
        assert "reviewer1" not in backend_cc._starting_markers

    def test_write_failure_triggers_proc_cleanup(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin.write.side_effect = BrokenPipeError("pipe broken")
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc):
            backend_cc.send("reviewer1", "hello", timeout=30)
        mock_proc.terminate.assert_called_once()

    def test_write_failure_kills_and_reaps_if_terminate_times_out(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin.write.side_effect = BrokenPipeError("pipe broken")
        mock_proc.pid = 12345
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=2),
            None,
        ]
        with patch("subprocess.Popen", return_value=mock_proc):
            backend_cc.send("reviewer1", "hello", timeout=30)
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()
        assert mock_proc.wait.call_count == 2

    def test_persist_failure_rolls_back(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345

        # Make session_id write succeed but pid write fail
        agent_dir = tmp_sessions / "reviewer1"
        agent_dir.mkdir(parents=True)
        orig_write_text = Path.write_text
        call_count = [0]

        def mock_write_text(self, content, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:  # pid file write
                raise OSError("disk full")
            return orig_write_text(self, content, *args, **kwargs)

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(Path, "write_text", mock_write_text):
            result = backend_cc.send("reviewer1", "hello", timeout=30)
        assert result is False
        assert "reviewer1" not in backend_cc._starting_markers

    def test_persist_failure_triggers_proc_cleanup(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345

        agent_dir = tmp_sessions / "reviewer1"
        agent_dir.mkdir(parents=True)
        orig_write_text = Path.write_text
        call_count = [0]

        def mock_write_text(self, content, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise OSError("disk full")
            return orig_write_text(self, content, *args, **kwargs)

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(Path, "write_text", mock_write_text):
            backend_cc.send("reviewer1", "hello", timeout=30)
        mock_proc.terminate.assert_called_once()

    def test_records_starting_marker_on_success(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc):
            backend_cc.send("reviewer1", "hello", timeout=30)
        assert "reviewer1" in backend_cc._starting_markers


# ===========================================================================
# ping
# ===========================================================================

class TestPing:
    def test_always_returns_true(self):
        assert backend_cc.ping("reviewer1", timeout=10) is True

    def test_always_returns_true_any_agent(self):
        assert backend_cc.ping("nonexistent_agent", timeout=0) is True


# ===========================================================================
# is_inactive
# ===========================================================================

class TestIsInactive:
    def test_cc_running_returns_false(self):
        result = backend_cc.is_inactive("reviewer1", cc_running=True)
        assert result is False

    def test_grace_period_no_session_jsonl_returns_false(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "CC_START_GRACE_SEC", 60)
        backend_cc._starting_markers["reviewer1"] = time.time()
        result = backend_cc.is_inactive("reviewer1")
        assert result is False

    def test_grace_period_jsonl_mtime_caught_up_clears_marker(self, tmp_sessions, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "CC_START_GRACE_SEC", 60)
        monkeypatch.setattr(config, "INACTIVE_THRESHOLD_SEC", 300)

        started = time.time() - 5
        backend_cc._starting_markers["reviewer1"] = started

        # Create session_id file
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)

        # Create session jsonl with fresh mtime
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "agents")
        cwd = PROJECT_ROOT  # no profile dir
        jsonl_dir = backend_cc._claude_project_dir(cwd)
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = jsonl_dir / f"{sid}.jsonl"
        jsonl_path.write_text("{}")

        # Mock /proc checks for normal judgment path
        pid_text = "99999"
        (d / "pid").write_text(pid_text)

        # For normal judgment we need PID + cmdline checks to pass.
        # But after clearing marker, session_id/pid/proc checks will run.
        # Simplest: mock the normal path to be active (fresh mtime)
        import os
        # Set mtime to be very recent
        now = time.time()
        os.utime(jsonl_path, (now, now))

        # We need to also make the PID check pass. Use monkeypatch for /proc
        proc_dir = Path(f"/proc/{pid_text}")
        cmdline_data = f"claude\0-p\0--session-id\0{sid}\0".encode()
        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == str(proc_dir):
                return True
            if str(self) == str(proc_dir / "cmdline"):
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == str(proc_dir / "cmdline"):
                return cmdline_data
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            result = backend_cc.is_inactive("reviewer1")
        assert result is False
        # Marker should have been cleared
        assert "reviewer1" not in backend_cc._starting_markers

    def test_invalid_session_id_returns_true(self, tmp_sessions, monkeypatch):
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        (d / "session_id").write_text("not-a-uuid")
        result = backend_cc.is_inactive("reviewer1")
        assert result is True

    def test_missing_session_id_returns_true(self, tmp_sessions):
        result = backend_cc.is_inactive("reviewer1")
        assert result is True

    def test_invalid_pid_file_returns_true(self, tmp_sessions):
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("not-a-number")
        result = backend_cc.is_inactive("reviewer1")
        assert result is True

    def test_missing_pid_file_returns_true(self, tmp_sessions):
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        result = backend_cc.is_inactive("reviewer1")
        assert result is True

    def test_dead_pid_returns_true(self, tmp_sessions, monkeypatch):
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("999999")
        # /proc/999999 doesn't exist → dead
        orig_exists = Path.exists

        def mock_exists(self):
            if str(self) == "/proc/999999":
                return False
            return orig_exists(self)

        with patch.object(Path, "exists", mock_exists):
            result = backend_cc.is_inactive("reviewer1")
        assert result is True

    def test_cmdline_mismatch_returns_true(self, tmp_sessions, monkeypatch):
        """PID reuse: process is not claude."""
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
                return b"python3\0some_script.py\0"
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            result = backend_cc.is_inactive("reviewer1")
        assert result is True

    def test_alive_pid_fresh_mtime_returns_false(self, tmp_sessions, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "INACTIVE_THRESHOLD_SEC", 300)
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "agents")
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("12345")

        # Create session jsonl
        cwd = PROJECT_ROOT
        jsonl_dir = backend_cc._claude_project_dir(cwd)
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = jsonl_dir / f"{sid}.jsonl"
        jsonl_path.write_text("{}")

        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return f"claude\0-p\0--session-id\0{sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            result = backend_cc.is_inactive("reviewer1")
        assert result is False

    def test_alive_matching_pid_stale_mtime_returns_false(self, tmp_sessions, monkeypatch, tmp_path):
        """Key regression test for #299: live matching PID + stale mtime => active."""
        monkeypatch.setattr(config, "INACTIVE_THRESHOLD_SEC", 300)
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "agents")
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("12345")

        # Create session jsonl with stale mtime
        cwd = PROJECT_ROOT
        jsonl_dir = backend_cc._claude_project_dir(cwd)
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = jsonl_dir / f"{sid}.jsonl"
        jsonl_path.write_text("{}")
        import os
        old_time = time.time() - 600
        os.utime(jsonl_path, (old_time, old_time))

        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return f"claude\0-p\0--session-id\0{sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            result = backend_cc.is_inactive("reviewer1")
        assert result is False

    def test_live_resume_owner_stale_jsonl_returns_false(self, tmp_sessions, monkeypatch, tmp_path):
        """End-to-end regression: live --resume owner keeps agent active despite stale jsonl."""
        monkeypatch.setattr(config, "INACTIVE_THRESHOLD_SEC", 300)
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "agents")
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("12345")

        cwd = PROJECT_ROOT
        jsonl_dir = backend_cc._claude_project_dir(cwd)
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = jsonl_dir / f"{sid}.jsonl"
        jsonl_path.write_text("{}")

        import os
        old_time = time.time() - 600
        os.utime(jsonl_path, (old_time, old_time))

        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return f"claude\0-p\0--resume\0{sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            result = backend_cc.is_inactive("reviewer1")

        assert result is False

    def test_alive_matching_pid_missing_jsonl_returns_false(self, tmp_sessions, monkeypatch, tmp_path):
        """Live matching PID => active even without jsonl."""
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "agents")
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
                return f"claude\0-p\0--session-id\0{sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            result = backend_cc.is_inactive("reviewer1")
        assert result is False

    def test_dead_pid_fresh_mtime_returns_false(self, tmp_sessions, monkeypatch, tmp_path):
        """Dead PID + fresh jsonl mtime => active (mtime path still works)."""
        monkeypatch.setattr(config, "INACTIVE_THRESHOLD_SEC", 300)
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "agents")
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("999999")

        cwd = PROJECT_ROOT
        jsonl_dir = backend_cc._claude_project_dir(cwd)
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = jsonl_dir / f"{sid}.jsonl"
        jsonl_path.write_text("{}")

        orig_exists = Path.exists

        def mock_exists(self):
            if str(self) == "/proc/999999":
                return False
            return orig_exists(self)

        with patch.object(Path, "exists", mock_exists):
            result = backend_cc.is_inactive("reviewer1")
        assert result is False

    def test_dead_pid_stale_mtime_returns_true(self, tmp_sessions, monkeypatch, tmp_path):
        """Dead PID + stale jsonl mtime => inactive."""
        monkeypatch.setattr(config, "INACTIVE_THRESHOLD_SEC", 300)
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "agents")
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("999999")

        cwd = PROJECT_ROOT
        jsonl_dir = backend_cc._claude_project_dir(cwd)
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = jsonl_dir / f"{sid}.jsonl"
        jsonl_path.write_text("{}")
        import os
        old_time = time.time() - 600
        os.utime(jsonl_path, (old_time, old_time))

        orig_exists = Path.exists

        def mock_exists(self):
            if str(self) == "/proc/999999":
                return False
            return orig_exists(self)

        with patch.object(Path, "exists", mock_exists):
            result = backend_cc.is_inactive("reviewer1")
        assert result is True

    def test_live_mismatched_session_pid_fresh_mtime_returns_false(self, tmp_sessions, monkeypatch, tmp_path):
        """Live PID with mismatched session + fresh mtime => active (mtime path)."""
        monkeypatch.setattr(config, "INACTIVE_THRESHOLD_SEC", 300)
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "agents")
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        other_sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("12345")

        cwd = PROJECT_ROOT
        jsonl_dir = backend_cc._claude_project_dir(cwd)
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = jsonl_dir / f"{sid}.jsonl"
        jsonl_path.write_text("{}")

        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                # Different session_id in cmdline
                return f"claude\0-p\0--session-id\0{other_sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            result = backend_cc.is_inactive("reviewer1")
        assert result is False

    def test_grace_active_live_matching_pid_missing_jsonl_returns_false(
        self, tmp_sessions, monkeypatch, tmp_path,
    ):
        """Grace active + live matching PID + missing jsonl => active (preserve fail-safe)."""
        monkeypatch.setattr(config, "CC_START_GRACE_SEC", 60)
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "agents")
        backend_cc._starting_markers["reviewer1"] = time.time()

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
                return f"claude\0-p\0--session-id\0{sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            result = backend_cc.is_inactive("reviewer1")
        assert result is False

    def test_grace_caught_up_live_owner_returns_false_and_clears_marker(
        self, tmp_sessions, monkeypatch, tmp_path,
    ):
        """Grace path explicitly honors live owner after jsonl catches up."""
        monkeypatch.setattr(config, "CC_START_GRACE_SEC", 60)
        started = time.time() - 5
        backend_cc._starting_markers["reviewer1"] = started
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "agents")

        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("12345")

        cwd = PROJECT_ROOT
        jsonl_dir = backend_cc._claude_project_dir(cwd)
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = jsonl_dir / f"{sid}.jsonl"
        jsonl_path.write_text("{}")

        import os
        now = time.time()
        os.utime(jsonl_path, (now, now))

        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return f"claude\0-p\0--session-id\0{sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            result = backend_cc.is_inactive("reviewer1")

        assert result is False
        assert "reviewer1" not in backend_cc._starting_markers


# ===========================================================================
# _read_persisted_state / _check_session_ownership
# ===========================================================================

class TestReadPersistedState:
    def test_returns_expected_snapshot(self, tmp_sessions):
        d = tmp_sessions / "agent1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("12345")
        state = backend_cc._read_persisted_state("agent1")
        assert state == backend_cc.PersistedCcState(session_id=sid, pid_text="12345")

    def test_missing_files_returns_none(self, tmp_sessions):
        state = backend_cc._read_persisted_state("nonexistent")
        assert state == backend_cc.PersistedCcState(session_id=None, pid_text=None)

    def test_invalid_uuid_returns_none_session(self, tmp_sessions):
        d = tmp_sessions / "agent1"
        d.mkdir(parents=True)
        (d / "session_id").write_text("not-a-uuid")
        (d / "pid").write_text("12345")
        state = backend_cc._read_persisted_state("agent1")
        assert state.session_id is None
        assert state.pid_text == "12345"

    def test_empty_pid_returns_none(self, tmp_sessions):
        d = tmp_sessions / "agent1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("")
        state = backend_cc._read_persisted_state("agent1")
        assert state.session_id == sid
        assert state.pid_text is None


class TestCheckSessionOwnership:
    def test_live_pid_claude_resume_match(self):
        sid = str(uuid.uuid4())
        state = backend_cc.PersistedCcState(session_id=sid, pid_text="12345")
        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return f"claude\0-p\0--resume\0{sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            ownership = backend_cc._check_session_ownership(state)
        assert ownership.has_valid_session is True
        assert ownership.has_live_owner is True

    def test_live_pid_claude_session_id_match(self):
        sid = str(uuid.uuid4())
        state = backend_cc.PersistedCcState(session_id=sid, pid_text="12345")
        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return f"claude\0-p\0--session-id\0{sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            ownership = backend_cc._check_session_ownership(state)
        assert ownership.has_valid_session is True
        assert ownership.has_live_owner is True

    def test_live_pid_non_claude_cmdline(self):
        sid = str(uuid.uuid4())
        state = backend_cc.PersistedCcState(session_id=sid, pid_text="12345")
        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return b"python3\0some_script.py\0"
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            ownership = backend_cc._check_session_ownership(state)
        assert ownership.has_live_owner is False

    def test_live_pid_claude_different_session_id(self):
        sid = str(uuid.uuid4())
        other_sid = str(uuid.uuid4())
        state = backend_cc.PersistedCcState(session_id=sid, pid_text="12345")
        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return f"claude\0-p\0--session-id\0{other_sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            ownership = backend_cc._check_session_ownership(state)
        assert ownership.has_live_owner is False

    def test_unreadable_cmdline(self):
        sid = str(uuid.uuid4())
        state = backend_cc.PersistedCcState(session_id=sid, pid_text="12345")
        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                raise OSError("Permission denied")
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            ownership = backend_cc._check_session_ownership(state)
        assert ownership.has_live_owner is False

    def test_missing_cmdline_file(self):
        sid = str(uuid.uuid4())
        state = backend_cc.PersistedCcState(session_id=sid, pid_text="12345")
        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                raise FileNotFoundError
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            ownership = backend_cc._check_session_ownership(state)
        assert ownership.has_live_owner is False

    def test_empty_cmdline_bytes(self):
        sid = str(uuid.uuid4())
        state = backend_cc.PersistedCcState(session_id=sid, pid_text="12345")
        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return b""
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            ownership = backend_cc._check_session_ownership(state)
        assert ownership.has_live_owner is False

    def test_truncated_resume_token_pair(self):
        sid = str(uuid.uuid4())
        state = backend_cc.PersistedCcState(session_id=sid, pid_text="12345")
        orig_exists = Path.exists
        orig_read_bytes = Path.read_bytes

        def mock_exists(self):
            if str(self) == "/proc/12345":
                return True
            return orig_exists(self)

        def mock_read_bytes(self):
            if str(self) == "/proc/12345/cmdline":
                return b"claude\0--resume"
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes):
            ownership = backend_cc._check_session_ownership(state)
        assert ownership.has_live_owner is False

    def test_invalid_pid_text(self):
        sid = str(uuid.uuid4())
        state = backend_cc.PersistedCcState(session_id=sid, pid_text="not-a-number")
        ownership = backend_cc._check_session_ownership(state)
        assert ownership.has_valid_session is True
        assert ownership.has_live_owner is False

    def test_invalid_session_id(self):
        state = backend_cc.PersistedCcState(session_id=None, pid_text="12345")
        ownership = backend_cc._check_session_ownership(state)
        assert ownership.has_valid_session is False
        assert ownership.has_live_owner is False

    def test_none_pid_text(self):
        sid = str(uuid.uuid4())
        state = backend_cc.PersistedCcState(session_id=sid, pid_text=None)
        ownership = backend_cc._check_session_ownership(state)
        assert ownership.has_valid_session is True
        assert ownership.has_live_owner is False

    def test_snapshot_consistency_is_inactive(self, tmp_sessions, monkeypatch, tmp_path):
        """Regression guard: is_inactive uses one snapshot for ownership + mtime."""
        monkeypatch.setattr(config, "INACTIVE_THRESHOLD_SEC", 300)
        monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path / "agents")

        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("999999")

        cwd = PROJECT_ROOT
        jsonl_dir = backend_cc._claude_project_dir(cwd)
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = jsonl_dir / f"{sid}.jsonl"
        jsonl_path.write_text("{}")

        orig_exists = Path.exists

        def mock_exists(self):
            if str(self) == "/proc/999999":
                return False
            return orig_exists(self)

        read_calls = []
        orig_read = backend_cc._read_persisted_state

        def tracking_read(agent_id: str) -> backend_cc.PersistedCcState:
            result = orig_read(agent_id)
            read_calls.append(result)
            return result

        with patch.object(Path, "exists", mock_exists), \
             patch("engine.backend_cc._read_persisted_state", side_effect=tracking_read):
            backend_cc.is_inactive("reviewer1")

        # Should be called exactly once (outside grace path)
        assert len(read_calls) == 1


# ===========================================================================
# send — single-writer invariant tests
# ===========================================================================

class TestSendSingleWriter:
    def test_live_owner_returns_false_no_popen(self, tmp_sessions, monkeypatch, caplog):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
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
                return f"claude\0-p\0--resume\0{sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes), \
             patch("subprocess.Popen") as mock_popen, \
             caplog.at_level(logging.WARNING):
            result = backend_cc.send("reviewer1", "hello", timeout=30)

        assert result is False
        mock_popen.assert_not_called()
        # Files unchanged
        assert (d / "session_id").read_text() == sid
        assert (d / "pid").read_text() == "12345"
        # Starting markers untouched
        assert "reviewer1" not in backend_cc._starting_markers
        # Log message assertions
        assert any(
            "reviewer1" in r.message
            and sid in r.message
            and "live owner still active" in r.message
            and "refusing spawn" in r.message
            and "multi-writer Claude session access" in r.message
            for r in caplog.records
        )

    def test_live_owner_session_id_returns_false_no_popen_files_unchanged_and_exact_warning(
        self, tmp_sessions, monkeypatch, caplog,
    ):
        """End-to-end regression: persisted session + live owner => refuse spawn."""
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
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
                return f"claude\0-p\0--session-id\0{sid}\0".encode()
            return orig_read_bytes(self)

        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes), \
             patch("subprocess.Popen") as mock_popen, \
             caplog.at_level(logging.WARNING):
            result = backend_cc.send("reviewer1", "hello", timeout=30)

        assert result is False
        mock_popen.assert_not_called()
        assert (d / "session_id").read_text() == sid
        assert (d / "pid").read_text() == "12345"
        assert "reviewer1" not in backend_cc._starting_markers
        assert [r.message for r in caplog.records if r.levelno == logging.WARNING] == [
            "cc send refused for reviewer1 session "
            f"{sid}: live owner still active; refusing spawn to avoid multi-writer Claude session access"
        ]

    def test_dead_pid_uses_resume(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("999999")

        orig_exists = Path.exists

        def mock_exists(self):
            if str(self) == "/proc/999999":
                return False
            return orig_exists(self)

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 54321
        with patch.object(Path, "exists", mock_exists), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            result = backend_cc.send("reviewer1", "hello", timeout=30)
        assert result is True
        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == sid

    def test_mismatched_live_pid_uses_resume(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        other_sid = str(uuid.uuid4())
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
                return f"claude\0-p\0--session-id\0{other_sid}\0".encode()
            return orig_read_bytes(self)

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 54321
        with patch.object(Path, "exists", mock_exists), \
             patch.object(Path, "read_bytes", mock_read_bytes), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            result = backend_cc.send("reviewer1", "hello", timeout=30)
        assert result is True
        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == sid

    def test_no_valid_session_creates_new_uuid(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            result = backend_cc.send("reviewer1", "hello", timeout=30)
        assert result is True
        cmd = mock_popen.call_args[0][0]
        assert "--session-id" in cmd
        assert "--resume" not in cmd

    def test_send_uses_same_snapshot(self, tmp_sessions, monkeypatch):
        """send() uses one persisted snapshot for ownership and spawn decision."""
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {})

        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        sid = str(uuid.uuid4())
        (d / "session_id").write_text(sid)
        (d / "pid").write_text("999999")

        orig_exists = Path.exists

        def mock_exists(self):
            if str(self) == "/proc/999999":
                return False
            return orig_exists(self)

        read_calls = []
        orig_read = backend_cc._read_persisted_state

        def tracking_read(agent_id: str) -> backend_cc.PersistedCcState:
            result = orig_read(agent_id)
            read_calls.append(result)
            return result

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 54321
        with patch.object(Path, "exists", mock_exists), \
             patch("engine.backend_cc._read_persisted_state", side_effect=tracking_read), \
             patch("subprocess.Popen", return_value=mock_proc):
            backend_cc.send("reviewer1", "hello", timeout=30)

        assert len(read_calls) == 1


# ===========================================================================
# _rebuild_claude_md
# ===========================================================================

class _DefaultGetDict(defaultdict):
    """defaultdict whose .get() also invokes default_factory for missing keys."""

    def get(self, key, default=None):  # type: ignore[override]
        if key in self:
            return self[key]
        if self.default_factory is not None:
            return self.default_factory()
        return default


@pytest.fixture
def tmp_profiles(tmp_path, monkeypatch):
    """Redirect AGENT_PROFILES_DIR to a temporary directory."""
    monkeypatch.setattr("config.AGENT_PROFILES_DIR", tmp_path)
    monkeypatch.setattr("engine.backend_cc.AGENT_PROFILES_DIR", tmp_path)
    monkeypatch.setattr(
        backend_cc,
        "_agent_config_cache",
        _DefaultGetDict(lambda: {"compile-startup-md": True}),
    )
    return tmp_path


class TestRebuildClaudeMd:
    def test_both_files_generates_claude_md(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("instruction")
        (d / "MEMORY.md").write_text("memory")
        backend_cc._rebuild_claude_md("agent1")
        assert (d / "CLAUDE.md").read_text() == "instruction\n\n---\n\nmemory\n"
        assert (d / ".claude_hash").exists()

    def test_hash_match_skips_regeneration(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("instruction")
        (d / "MEMORY.md").write_text("memory")
        backend_cc._rebuild_claude_md("agent1")
        (d / "CLAUDE.md").write_text("tampered")
        backend_cc._rebuild_claude_md("agent1")
        assert (d / "CLAUDE.md").read_text() == "tampered"

    def test_hash_mismatch_regenerates(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("instruction")
        (d / "MEMORY.md").write_text("memory")
        backend_cc._rebuild_claude_md("agent1")
        (d / "MEMORY.md").write_text("updated memory")
        backend_cc._rebuild_claude_md("agent1")
        assert (d / "CLAUDE.md").read_text() == "instruction\n\n---\n\nupdated memory\n"

    def test_all_missing_cleans_up(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        backend_cc._rebuild_claude_md("agent1")
        assert not (d / "CLAUDE.md").exists()
        assert not (d / ".claude_hash").exists()

    def test_identity_instruction_memory(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "IDENTITY.md").write_text("identity")
        (d / "INSTRUCTION.md").write_text("instruction")
        (d / "MEMORY.md").write_text("memory")
        backend_cc._rebuild_claude_md("agent1")
        assert (d / "CLAUDE.md").read_text() == "identity\n\n---\n\ninstruction\n\n---\n\nmemory\n"

    def test_hash_pi_compatible(self, tmp_profiles):
        """Hash formula must match PI backend exactly."""
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "IDENTITY.md").write_text("identity")
        (d / "INSTRUCTION.md").write_text("instruction")
        (d / "MEMORY.md").write_text("memory")
        backend_cc._rebuild_claude_md("agent1")

        # Compute expected hash using PI formula
        identity_bytes = b"identity"
        instruction_bytes = b"instruction"
        memory_bytes = b"memory"
        expected_hash = hashlib.sha256(
            len(identity_bytes).to_bytes(8, "big")
            + identity_bytes
            + len(instruction_bytes).to_bytes(8, "big")
            + instruction_bytes
            + memory_bytes,
        ).hexdigest()

        actual_hash = (d / ".claude_hash").read_text().strip()
        assert actual_hash == expected_hash

    def test_hash_matches_pi_backend(self, tmp_profiles, monkeypatch):
        """Same inputs must produce the same hash as PI backend."""
        from engine import backend_pi

        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "IDENTITY.md").write_text("test_identity")
        (d / "INSTRUCTION.md").write_text("test_instruction")
        (d / "MEMORY.md").write_text("test_memory")

        # Patch PI's AGENT_PROFILES_DIR to the same tmp dir
        monkeypatch.setattr("engine.backend_pi.AGENT_PROFILES_DIR", tmp_profiles)

        # Enable compile for PI too
        backend_pi._agent_config_cache = {"agent1": {"compile-startup-md": True}}
        try:
            backend_pi._rebuild_agents_md("agent1")
            pi_hash = (d / ".agents_hash").read_text().strip()

            # Reset for CC
            (d / "AGENTS.md").unlink(missing_ok=True)
            (d / ".agents_hash").unlink(missing_ok=True)

            backend_cc._rebuild_claude_md("agent1")
            cc_hash = (d / ".claude_hash").read_text().strip()

            assert cc_hash == pi_hash
        finally:
            backend_pi._agent_config_cache = None

    def test_compile_false_skips_generation(self, tmp_profiles, monkeypatch):
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {"agent1": {"compile-startup-md": False}})
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "IDENTITY.md").write_text("identity")
        backend_cc._rebuild_claude_md("agent1")
        assert not (d / "CLAUDE.md").exists()
        assert not (d / ".claude_hash").exists()

    def test_compile_key_absent_defaults_false(self, tmp_profiles, monkeypatch):
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {"agent1": {}})
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "IDENTITY.md").write_text("identity")
        backend_cc._rebuild_claude_md("agent1")
        assert not (d / "CLAUDE.md").exists()

    def test_compile_non_bool_warns_and_skips(self, tmp_profiles, monkeypatch, caplog):
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {"agent1": {"compile-startup-md": "false"}})
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "IDENTITY.md").write_text("identity")
        with caplog.at_level(logging.WARNING, logger="engine.backend_cc"):
            backend_cc._rebuild_claude_md("agent1")
        assert not (d / "CLAUDE.md").exists()
        assert "non-bool value" in caplog.text

    def test_compile_false_with_hash_deletes_generated_files(self, tmp_profiles, monkeypatch):
        """compile false + .claude_hash exists → delete CLAUDE.md and .claude_hash."""
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "CLAUDE.md").write_text("auto-generated content")
        (d / ".claude_hash").write_text("somehash\n")
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {"agent1": {"compile-startup-md": False}})
        backend_cc._rebuild_claude_md("agent1")
        assert not (d / "CLAUDE.md").exists()
        assert not (d / ".claude_hash").exists()

    def test_compile_false_without_hash_preserves_manual_claude_md(self, tmp_profiles, monkeypatch):
        """compile false + no .claude_hash → manual CLAUDE.md is preserved."""
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "CLAUDE.md").write_text("# Manual CLAUDE.md\n")
        monkeypatch.setattr(backend_cc, "_agent_config_cache", {"agent1": {"compile-startup-md": False}})
        backend_cc._rebuild_claude_md("agent1")
        assert (d / "CLAUDE.md").read_text() == "# Manual CLAUDE.md\n"

    def test_exception_swallowed(self, tmp_profiles, caplog):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("instruction")
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            backend_cc._rebuild_claude_md("agent1")  # must not raise
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("_rebuild_claude_md" in r.message for r in warnings)


# ===========================================================================
# reset_session
# ===========================================================================

class TestResetSession:
    def test_deletes_session_id_and_pid(self, tmp_sessions):
        d = tmp_sessions / "reviewer1"
        d.mkdir(parents=True)
        (d / "session_id").write_text("some-uuid")
        (d / "pid").write_text("12345")
        backend_cc.reset_session("reviewer1")
        assert not (d / "session_id").exists()
        assert not (d / "pid").exists()

    def test_clears_starting_marker(self, tmp_sessions):
        backend_cc._starting_markers["reviewer1"] = time.time()
        backend_cc.reset_session("reviewer1")
        assert "reviewer1" not in backend_cc._starting_markers

    def test_calls_rebuild_claude_md(self, tmp_sessions):
        with patch("engine.backend_cc._rebuild_claude_md") as mock_rebuild:
            backend_cc.reset_session("agent1")
        mock_rebuild.assert_called_once_with("agent1")

    def test_absent_files_noop(self, tmp_sessions):
        # Should not raise
        backend_cc.reset_session("nonexistent_agent")
