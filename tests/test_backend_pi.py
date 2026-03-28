"""Tests for engine/backend_pi.py — pi backend for agent communication."""

from __future__ import annotations

import json
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


@pytest.fixture(autouse=True)
def _reset_agent_config_cache():
    """Reset _agent_config_cache between tests to prevent cross-test pollution."""
    backend_pi._agent_config_cache = None
    yield
    backend_pi._agent_config_cache = None


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
# _load_config
# ===========================================================================

class TestLoadConfig:
    def test_file_missing_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "engine.backend_pi.PI_AGENT_CONFIG",
            tmp_path / "nonexistent.json",
        )
        result = backend_pi._load_config()
        assert result == {}

    def test_valid_json_returns_parsed(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config_pi.json"
        cfg_file.write_text(json.dumps({
            "reviewer1": {"model": "claude-sonnet", "thinking": "high"},
        }))
        monkeypatch.setattr("engine.backend_pi.PI_AGENT_CONFIG", cfg_file)
        result = backend_pi._load_config()
        assert result == {"reviewer1": {"model": "claude-sonnet", "thinking": "high"}}

    def test_invalid_json_returns_empty_with_warning(self, monkeypatch, tmp_path, caplog):
        cfg_file = tmp_path / "config_pi.json"
        cfg_file.write_text("{invalid json")
        monkeypatch.setattr("engine.backend_pi.PI_AGENT_CONFIG", cfg_file)
        with caplog.at_level(logging.WARNING):
            result = backend_pi._load_config()
        assert result == {}
        assert any("Invalid JSON" in r.message for r in caplog.records)

    def test_empty_file_returns_empty(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config_pi.json"
        cfg_file.write_text("   ")
        monkeypatch.setattr("engine.backend_pi.PI_AGENT_CONFIG", cfg_file)
        result = backend_pi._load_config()
        assert result == {}

    def test_non_dict_root_returns_empty_with_warning(self, monkeypatch, tmp_path, caplog):
        cfg_file = tmp_path / "config_pi.json"
        cfg_file.write_text("[]")
        monkeypatch.setattr("engine.backend_pi.PI_AGENT_CONFIG", cfg_file)
        with caplog.at_level(logging.WARNING):
            result = backend_pi._load_config()
        assert result == {}
        assert any("Expected JSON object" in r.message for r in caplog.records)

    def test_non_dict_entry_filtered_with_warning(self, monkeypatch, tmp_path, caplog):
        cfg_file = tmp_path / "config_pi.json"
        cfg_file.write_text(json.dumps({
            "good": {"model": "x"},
            "bad": "just-a-string",
        }))
        monkeypatch.setattr("engine.backend_pi.PI_AGENT_CONFIG", cfg_file)
        with caplog.at_level(logging.WARNING):
            result = backend_pi._load_config()
        assert result == {"good": {"model": "x"}}
        assert any("Skipped non-dict" in r.message for r in caplog.records)

    def test_caching_returns_same_object(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config_pi.json"
        cfg_file.write_text(json.dumps({"a": {"model": "m"}}))
        monkeypatch.setattr("engine.backend_pi.PI_AGENT_CONFIG", cfg_file)
        first = backend_pi._load_config()
        second = backend_pi._load_config()
        assert first is second


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

    def test_omits_model_flag_when_no_profile(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_pi, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        assert "--model" not in cmd

    def test_includes_model_flag_from_profile(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_pi, "_agent_config_cache", {
            "reviewer1": {"model": "claude-sonnet"},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-sonnet"

    def test_provider_and_model_combined(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_pi, "_agent_config_cache", {
            "reviewer1": {"provider": "anthropic", "model": "claude-sonnet-4"},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "anthropic/claude-sonnet-4"

    def test_model_only_no_provider(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_pi, "_agent_config_cache", {
            "reviewer1": {"model": "gpt-4.1"},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "gpt-4.1"

    def test_provider_without_model_warns(self, tmp_sessions, monkeypatch, caplog):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_pi, "_agent_config_cache", {
            "reviewer1": {"provider": "anthropic"},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with caplog.at_level(logging.WARNING):
            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                backend_pi.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        assert "--model" not in cmd
        assert any("'provider' set without 'model'" in r.message for r in caplog.records)

    def test_thinking_flag(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_pi, "_agent_config_cache", {
            "reviewer1": {"thinking": "high"},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--thinking")
        assert cmd[idx + 1] == "high"

    def test_tools_flag(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_pi, "_agent_config_cache", {
            "reviewer1": {"tools": "read,grep,find,ls"},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--tools")
        assert cmd[idx + 1] == "read,grep,find,ls"

    def test_no_profile_unknown_agent_debug_log(self, tmp_sessions, monkeypatch, caplog):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_pi, "_agent_config_cache", {})
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with caplog.at_level(logging.DEBUG):
            with patch("subprocess.Popen", return_value=mock_proc):
                backend_pi.send("unknown_agent", "hello", timeout=30)
        assert any("No pi profile" in r.message for r in caplog.records)

    def test_all_flags_combined(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_pi, "_agent_config_cache", {
            "reviewer1": {
                "provider": "anthropic",
                "model": "claude-sonnet-4",
                "thinking": "high",
                "tools": "read,grep",
            },
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "anthropic/claude-sonnet-4"
        thinking_idx = cmd.index("--thinking")
        assert cmd[thinking_idx + 1] == "high"
        tools_idx = cmd.index("--tools")
        assert cmd[tools_idx + 1] == "read,grep"

    def test_non_string_values_coerced_to_str(self, tmp_sessions, monkeypatch):
        """Non-string values (int, bool) in profile are str()-coerced, not TypeError."""
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_pi, "_agent_config_cache", {
            "reviewer1": {
                "model": 12345,
                "thinking": True,
                "tools": 42,
            },
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            result = backend_pi.send("reviewer1", "hello", timeout=30)
        assert result is True
        cmd = mock_popen.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "12345"
        thinking_idx = cmd.index("--thinking")
        assert cmd[thinking_idx + 1] == "True"
        tools_idx = cmd.index("--tools")
        assert cmd[tools_idx + 1] == "42"

    def test_empty_profile_no_flags(self, tmp_sessions, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", False)
        monkeypatch.setattr(backend_pi, "_agent_config_cache", {
            "reviewer1": {},
        })
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            backend_pi.send("reviewer1", "hello", timeout=30)
        cmd = mock_popen.call_args[0][0]
        assert "--model" not in cmd
        assert "--thinking" not in cmd
        assert "--tools" not in cmd

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
# _rebuild_agents_md
# ===========================================================================

@pytest.fixture
def tmp_profiles(tmp_path, monkeypatch):
    """Redirect AGENT_PROFILES_DIR to a temporary directory."""
    monkeypatch.setattr("config.AGENT_PROFILES_DIR", tmp_path)
    monkeypatch.setattr("engine.backend_pi.AGENT_PROFILES_DIR", tmp_path)
    return tmp_path


class TestRebuildAgentsMd:
    def test_both_files_generates_agents_md(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("instruction")
        (d / "MEMORY.md").write_text("memory")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").read_text() == "instruction\n\n---\n\nmemory\n"
        assert (d / ".agents_hash").exists()

    def test_hash_match_skips_regeneration(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("instruction")
        (d / "MEMORY.md").write_text("memory")
        backend_pi._rebuild_agents_md("agent1")
        (d / "AGENTS.md").write_text("tampered")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").read_text() == "tampered"

    def test_hash_mismatch_regenerates(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("instruction")
        (d / "MEMORY.md").write_text("memory")
        backend_pi._rebuild_agents_md("agent1")
        (d / "MEMORY.md").write_text("updated memory")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").read_text() == "instruction\n\n---\n\nupdated memory\n"

    def test_neither_file_is_noop(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        backend_pi._rebuild_agents_md("agent1")
        assert not (d / "AGENTS.md").exists()
        assert not (d / ".agents_hash").exists()

    def test_instruction_only(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("instruction")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").read_text() == "instruction\n"

    def test_memory_only(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "MEMORY.md").write_text("memory")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").read_text() == "memory\n"

    def test_io_error_is_swallowed(self, tmp_profiles, caplog):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("instruction")
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            backend_pi._rebuild_agents_md("agent1")  # must not raise
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("_rebuild_agents_md" in r.message for r in warnings)

    def test_corrupt_hash_triggers_rebuild(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("instruction")
        (d / "MEMORY.md").write_text("memory")
        (d / ".agents_hash").write_text("not-a-valid-hash\n")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").read_text() == "instruction\n\n---\n\nmemory\n"

    def test_nonexistent_profile_dir(self, tmp_profiles):
        backend_pi._rebuild_agents_md("nonexistent_agent")  # must not raise

    def test_reset_session_calls_rebuild(self, tmp_profiles, tmp_sessions):
        with patch("engine.backend_pi._rebuild_agents_md") as mock_rebuild:
            backend_pi.reset_session("agent1")
        mock_rebuild.assert_called_once_with("agent1")

    def test_both_deleted_cleans_up(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("instruction")
        (d / "MEMORY.md").write_text("memory")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").exists()
        (d / "INSTRUCTION.md").unlink()
        (d / "MEMORY.md").unlink()
        backend_pi._rebuild_agents_md("agent1")
        assert not (d / "AGENTS.md").exists()
        assert not (d / ".agents_hash").exists()

    def test_whitespace_only_files_clean_up(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("  \n\n")
        (d / "MEMORY.md").write_text("  \n\n")
        backend_pi._rebuild_agents_md("agent1")
        assert not (d / "AGENTS.md").exists()
        assert not (d / ".agents_hash").exists()

    def test_missing_agents_md_triggers_rebuild(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "INSTRUCTION.md").write_text("instruction")
        (d / "MEMORY.md").write_text("memory")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").exists()
        (d / "AGENTS.md").unlink()
        assert (d / ".agents_hash").exists()
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").read_text() == "instruction\n\n---\n\nmemory\n"

    def test_identity_instruction_memory_generates_agents_md(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "IDENTITY.md").write_text("identity")
        (d / "INSTRUCTION.md").write_text("instruction")
        (d / "MEMORY.md").write_text("memory")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").read_text() == "identity\n\n---\n\ninstruction\n\n---\n\nmemory\n"
        assert (d / ".agents_hash").exists()

    def test_identity_only(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "IDENTITY.md").write_text("identity")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").read_text() == "identity\n"

    def test_identity_and_instruction_only(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "IDENTITY.md").write_text("identity")
        (d / "INSTRUCTION.md").write_text("instruction")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").read_text() == "identity\n\n---\n\ninstruction\n"

    def test_identity_and_memory_only(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "IDENTITY.md").write_text("identity")
        (d / "MEMORY.md").write_text("memory")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").read_text() == "identity\n\n---\n\nmemory\n"

    def test_identity_change_triggers_rebuild(self, tmp_profiles):
        d = tmp_profiles / "agent1"
        d.mkdir()
        (d / "IDENTITY.md").write_text("identity")
        (d / "INSTRUCTION.md").write_text("instruction")
        backend_pi._rebuild_agents_md("agent1")
        (d / "AGENTS.md").write_text("tampered")
        (d / "IDENTITY.md").write_text("updated identity")
        backend_pi._rebuild_agents_md("agent1")
        assert (d / "AGENTS.md").read_text() == "updated identity\n\n---\n\ninstruction\n"


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
