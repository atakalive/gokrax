"""Tests for engine/backend_agy.py."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
from engine import backend_agy
from engine.backend_types import SendResult


AGENT = "reviewer1"


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch, tmp_path):
    """Reset module-level caches, isolate pid/profile dirs + fake HOME under tmp_path."""
    monkeypatch.setattr(backend_agy, "_agent_config_cache", None)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "AGY_BIN", "agy")

    pids = tmp_path / "pids"
    profiles = tmp_path / "agents"
    (profiles / AGENT).mkdir(parents=True)
    monkeypatch.setattr(backend_agy, "AGY_PIDS_DIR", pids)
    monkeypatch.setattr(backend_agy, "AGENT_PROFILES_DIR", profiles)

    # Fake real HOME with the canonical auth/identity files.
    fake_home = tmp_path / "home"
    (fake_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (fake_home / ".gemini" / "oauth_creds.json").write_text("{}")
    (fake_home / ".gemini" / "google_accounts.json").write_text("{}")
    (fake_home / ".gemini" / "installation_id").write_text("real-id")
    (fake_home / ".gemini" / "antigravity-cli" / "installation_id").write_text("agy-id")
    (fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text("{}")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    yield


@pytest.fixture
def recorder(monkeypatch):
    popen_calls: list[dict] = []

    class FakeProc:
        def __init__(self, pid=4242):
            self.pid = pid

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        popen_calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return {"popen_calls": popen_calls}


def _profile_dir() -> Path:
    return backend_agy.AGENT_PROFILES_DIR / AGENT


# ===========================================================================
# argv flags
# ===========================================================================

class TestSendArgv:
    def test_print_flag_in_argv(self, recorder):
        backend_agy.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]["cmd"]
        idx = argv.index("-p")
        assert argv[idx + 1] == "hi"

    def test_dangerously_skip_permissions_in_argv(self, recorder):
        backend_agy.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]["cmd"]
        assert "--dangerously-skip-permissions" in argv

    def test_initial_no_continue_flag(self, recorder):
        assert backend_agy.send(AGENT, "hi", 30) is SendResult.OK
        argv = recorder["popen_calls"][0]["cmd"]
        assert "-c" not in argv

    def test_continuation_has_c_flag(self, recorder):
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_agy._session_marker_path(AGENT).touch()
        backend_agy.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]["cmd"]
        assert argv[-1] == "-c"

    def test_print_timeout_24h_always_in_argv(self, recorder):
        backend_agy.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]["cmd"]
        idx = argv.index("--print-timeout")
        assert argv[idx + 1] == "24h"

    def test_dry_run_no_popen(self, recorder, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", True)
        assert backend_agy.send(AGENT, "hi", 30) is SendResult.OK
        assert recorder["popen_calls"] == []

    def test_nul_message_refused_before_popen(self, recorder, monkeypatch):
        """生 NUL 混入 message は Popen 手前で FAIL（#390 の argv ガード）。"""
        monkeypatch.setattr(config, "DRY_RUN", False)
        assert backend_agy.send(AGENT, "a\x00b", 30) is SendResult.FAIL
        assert recorder["popen_calls"] == []

    def test_send_includes_add_dir_flag(self, recorder):
        backend_agy.send(AGENT, "hi", 30)
        argv = recorder["popen_calls"][0]["cmd"]
        idx = argv.index("--add-dir")
        assert argv[idx + 1] == str(_profile_dir())

    def test_send_busy_while_live_process_exists(self, recorder, monkeypatch):
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_agy._pid_path(AGENT).write_text("9999")
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)

        rebuild_calls: list = []
        monkeypatch.setattr(
            backend_agy, "_rebuild_agy_md",
            lambda aid: rebuild_calls.append(aid),
        )

        result = backend_agy.send(AGENT, "hi", 30)
        assert result is SendResult.BUSY
        assert recorder["popen_calls"] == []
        # Workspace operations must not run when live process detected
        assert rebuild_calls == []


# ===========================================================================
# HOME isolation & settings.json merge
# ===========================================================================

class TestHomeIsolation:
    def test_home_env_equals_agent_profile_dir(self, recorder):
        backend_agy.send(AGENT, "hi", 30)
        env = recorder["popen_calls"][0]["kwargs"]["env"]
        assert env["HOME"] == str(_profile_dir())

    def test_agy_cli_disable_auto_update_set_to_1(self, recorder):
        backend_agy.send(AGENT, "hi", 30)
        env = recorder["popen_calls"][0]["kwargs"]["env"]
        assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"

    def test_settings_json_merges_with_real_home_base(self, recorder, monkeypatch):
        real_settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        real_settings.write_text(json.dumps({"proxy": "http://x", "model": "BaseModel"}))
        monkeypatch.setattr(
            backend_agy, "_load_config",
            lambda: {AGENT: {"model": "OverrideModel"}},
        )
        backend_agy.send(AGENT, "hi", 30)
        per = _profile_dir() / ".gemini" / "antigravity-cli" / "settings.json"
        data = json.loads(per.read_text())
        assert data["proxy"] == "http://x"
        assert data["model"] == "OverrideModel"
        assert data["trustedWorkspaces"] == [str(_profile_dir())]

    def test_settings_json_preserves_base_proxy_config(self, recorder, monkeypatch):
        real_settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        real_settings.write_text(json.dumps({"proxy": "http://corp:8080"}))
        backend_agy.send(AGENT, "hi", 30)
        per = _profile_dir() / ".gemini" / "antigravity-cli" / "settings.json"
        data = json.loads(per.read_text())
        assert data["proxy"] == "http://corp:8080"

    def test_settings_json_overrides_model_and_trusted_workspaces(self, recorder, monkeypatch):
        real_settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        real_settings.write_text(json.dumps({"trustedWorkspaces": ["/elsewhere"]}))
        monkeypatch.setattr(
            backend_agy, "_load_config",
            lambda: {AGENT: {"model": "M2"}},
        )
        backend_agy.send(AGENT, "hi", 30)
        per = _profile_dir() / ".gemini" / "antigravity-cli" / "settings.json"
        data = json.loads(per.read_text())
        assert data["model"] == "M2"
        assert data["trustedWorkspaces"] == [str(_profile_dir())]

    def test_oauth_creds_symlinked_from_real_home(self, recorder):
        backend_agy.send(AGENT, "hi", 30)
        link = _profile_dir() / ".gemini" / "oauth_creds.json"
        assert link.is_symlink()
        import os
        assert os.readlink(link) == str(Path.home() / ".gemini" / "oauth_creds.json")

    def test_antigravity_oauth_token_symlinked_from_real_home(self, recorder):
        backend_agy.send(AGENT, "hi", 30)
        link = _profile_dir() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        assert link.is_symlink()
        import os
        assert os.readlink(link) == str(
            Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        )

    def test_ensure_agy_home_is_idempotent(self, recorder):
        backend_agy._ensure_agy_home(AGENT, "X")
        link = _profile_dir() / ".gemini" / "oauth_creds.json"
        assert link.is_symlink()
        # call again — should not raise
        backend_agy._ensure_agy_home(AGENT, "X")
        assert link.is_symlink()

    def test_mcp_config_zero_byte_seeded(self, recorder):
        cfg_dir = _profile_dir() / ".gemini" / "config"
        cfg_dir.mkdir(parents=True)
        mcp = cfg_dir / "mcp_config.json"
        mcp.write_text("")
        assert mcp.stat().st_size == 0
        result = backend_agy._ensure_agy_home(AGENT, None)
        assert result is not None
        assert mcp.read_text() == "{}\n"

    def test_mcp_config_valid_not_overwritten(self, recorder):
        cfg_dir = _profile_dir() / ".gemini" / "config"
        cfg_dir.mkdir(parents=True)
        mcp = cfg_dir / "mcp_config.json"
        mcp.write_text('{"servers":{}}')
        result = backend_agy._ensure_agy_home(AGENT, None)
        assert result is not None
        assert mcp.read_text() == '{"servers":{}}'

    def test_cli_log_rotation_keeps_latest_10(self, recorder):
        import os as _os
        log_dir = _profile_dir() / ".gemini" / "antigravity-cli" / "log"
        log_dir.mkdir(parents=True)
        logs = []
        for i in range(11):
            p = log_dir / f"cli-2026010{i // 10}_{i:06d}.log"
            p.write_text(f"log{i}")
            _os.utime(p, (1000 + i, 1000 + i))
            logs.append(p)
        result = backend_agy._ensure_agy_home(AGENT, None)
        assert result is not None
        remaining = sorted(log_dir.glob("cli-*.log"))
        assert len(remaining) == 10
        # Oldest (logs[0], mtime=1000) should be gone
        assert not logs[0].exists()
        # Newest must remain
        assert logs[-1].exists()

    def test_cli_log_rotation_survives_broken_symlink(self, recorder, tmp_path):
        log_dir = _profile_dir() / ".gemini" / "antigravity-cli" / "log"
        log_dir.mkdir(parents=True)
        (log_dir / "cli-normal.log").write_text("ok")
        broken = log_dir / "cli-broken.log"
        broken.symlink_to(tmp_path / "nonexistent_target_for_broken_symlink")
        result = backend_agy._ensure_agy_home(AGENT, None)
        assert result is not None


class TestSymlinkRepair:
    def test_agent_home_root_symlink_refuses_send(
        self, recorder, monkeypatch, tmp_path,
    ):
        import shutil as _shutil
        profile = _profile_dir()
        if profile.exists():
            _shutil.rmtree(profile)
        real_target = tmp_path / "real_agent"
        real_target.mkdir()
        profile.symlink_to(real_target)
        result = backend_agy.send(AGENT, "hi", 30)
        assert result is SendResult.FAIL
        assert recorder["popen_calls"] == []

    def test_ensure_symlink_replaces_regular_file_with_symlink(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("real")
        link = tmp_path / "link.txt"
        link.write_text("stale regular file")
        backend_agy._ensure_symlink(link, target)
        assert link.is_symlink()
        import os
        assert os.readlink(link) == str(target)

    def test_ensure_symlink_noop_when_target_missing(self, tmp_path, caplog):
        target = tmp_path / "missing"
        link = tmp_path / "link"
        with caplog.at_level(logging.WARNING, logger="engine.backend_agy"):
            backend_agy._ensure_symlink(link, target)
        assert not link.exists()
        assert not link.is_symlink()

    def test_dotgemini_symlink_does_not_pollute_real_home(
        self, recorder, monkeypatch, tmp_path,
    ):
        """If .gemini is a symlink (e.g. from manual setup), _ensure_agy_home
        must remove it and create a real directory — never writing through
        the symlink into the real HOME."""
        profile = _profile_dir()
        dotgemini = profile / ".gemini"
        # Remove whatever _reset_module_state created and replace with symlink
        import shutil
        if dotgemini.exists():
            shutil.rmtree(dotgemini)
        fake_real_home_gemini = tmp_path / "real_home_gemini"
        fake_real_home_gemini.mkdir()
        (fake_real_home_gemini / "antigravity-cli").mkdir()
        dotgemini.symlink_to(fake_real_home_gemini)
        assert dotgemini.is_symlink()

        result = backend_agy._ensure_agy_home(AGENT, "SomeModel")
        assert result is not None
        # .gemini should now be a real directory, not a symlink
        assert not dotgemini.is_symlink()
        assert dotgemini.is_dir()
        # Real HOME dir should NOT have per-agent settings.json
        assert not (fake_real_home_gemini / "antigravity-cli" / "settings.json").exists()

    def test_dotgemini_symlink_removal_failure_returns_none(
        self, recorder, monkeypatch, tmp_path,
    ):
        """If we cannot remove a .gemini symlink, _ensure_agy_home returns None."""
        profile = _profile_dir()
        dotgemini = profile / ".gemini"
        import shutil
        if dotgemini.exists():
            shutil.rmtree(dotgemini)
        fake_target = tmp_path / "target"
        fake_target.mkdir()
        dotgemini.symlink_to(fake_target)

        # Make unlink fail
        def _failing_unlink(*a, **kw):
            raise PermissionError("denied")
        monkeypatch.setattr(type(dotgemini), "unlink", lambda self, *a, **kw: _failing_unlink())

        result = backend_agy._ensure_agy_home(AGENT, "M")
        assert result is None

    def test_mutable_subdir_symlink_does_not_write_to_real_home(
        self, recorder, tmp_path,
    ):
        """If cache/ or conversations/ is a symlink, _ensure_agy_home unlinks
        it so agy writes stay per-agent and don't escape to real HOME."""
        profile = _profile_dir()
        agy_cli = profile / ".gemini" / "antigravity-cli"
        agy_cli.mkdir(parents=True, exist_ok=True)

        # Make cache/ a symlink to a fake real HOME path
        fake_real_cache = tmp_path / "real_cache"
        fake_real_cache.mkdir()
        cache_dir = agy_cli / "cache"
        if cache_dir.exists():
            cache_dir.rmdir()
        cache_dir.symlink_to(fake_real_cache)
        assert cache_dir.is_symlink()

        result = backend_agy._ensure_agy_home(AGENT, "TestModel")
        assert result is not None
        # cache should now be a real directory
        assert not cache_dir.is_symlink()
        assert cache_dir.is_dir()
        # Real HOME cache dir should remain empty (no writes through symlink)
        assert list(fake_real_cache.iterdir()) == []

    def test_conversations_symlink_removed_before_agy_launch(
        self, recorder, tmp_path,
    ):
        """conversations/ symlink is removed to prevent per-agent .pb leaking."""
        profile = _profile_dir()
        agy_cli = profile / ".gemini" / "antigravity-cli"
        agy_cli.mkdir(parents=True, exist_ok=True)

        fake_real_convos = tmp_path / "real_conversations"
        fake_real_convos.mkdir()
        convos_dir = agy_cli / "conversations"
        convos_dir.symlink_to(fake_real_convos)
        assert convos_dir.is_symlink()

        result = backend_agy._ensure_agy_home(AGENT, "M")
        assert result is not None
        assert not convos_dir.is_symlink()

    def test_antigravitycli_symlink_removed_by_mutable_paths_guard(
        self, recorder, tmp_path,
    ):
        """`.antigravitycli/` (agy 1.0.2 writes project ID symlinks here) must
        be unlinked if it is a symlink pointing outside agent_home."""
        profile = _profile_dir()
        fake_target = tmp_path / "real_antigravitycli"
        fake_target.mkdir()
        link = profile / ".antigravitycli"
        link.symlink_to(fake_target)
        assert link.is_symlink()

        result = backend_agy._ensure_agy_home(AGENT, "M")
        assert result is not None
        assert not link.is_symlink()
        # Real target should not have received any writes through the symlink
        assert list(fake_target.iterdir()) == []

    def test_settings_json_symlink_does_not_overwrite_real_home(
        self, recorder, tmp_path,
    ):
        """If settings.json is a symlink, _ensure_agy_home unlinks it so the
        write doesn't escape to real HOME's settings.json."""
        profile = _profile_dir()
        agy_cli = profile / ".gemini" / "antigravity-cli"
        agy_cli.mkdir(parents=True, exist_ok=True)

        real_settings = tmp_path / "real_settings.json"
        real_settings.write_text('{"model": "original"}')
        settings_link = agy_cli / "settings.json"
        settings_link.symlink_to(real_settings)
        assert settings_link.is_symlink()

        result = backend_agy._ensure_agy_home(AGENT, "OverrideModel")
        assert result is not None
        assert not settings_link.is_symlink()
        # Real HOME settings must be untouched
        assert json.loads(real_settings.read_text()) == {"model": "original"}
        # Per-agent settings must have the override
        assert json.loads(settings_link.read_text())["model"] == "OverrideModel"


# ===========================================================================
# Model fallback semantics
# ===========================================================================

class TestModelFallback:
    def _per_settings(self) -> dict:
        per = _profile_dir() / ".gemini" / "antigravity-cli" / "settings.json"
        return json.loads(per.read_text())

    def test_model_field_written_to_settings_json(self, recorder, monkeypatch):
        monkeypatch.setattr(
            backend_agy, "_load_config",
            lambda: {AGENT: {"model": "Gemini 3.5 Flash (Medium)"}},
        )
        backend_agy.send(AGENT, "hi", 30)
        assert self._per_settings()["model"] == "Gemini 3.5 Flash (Medium)"

    def test_no_model_falls_back_to_base_settings(self, recorder, monkeypatch):
        real_settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        real_settings.write_text(json.dumps({"model": "BaseM"}))
        monkeypatch.setattr(backend_agy, "_load_config", lambda: {AGENT: {}})
        backend_agy.send(AGENT, "hi", 30)
        assert self._per_settings()["model"] == "BaseM"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_string_model_treated_as_unset(self, recorder, monkeypatch, value):
        real_settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        real_settings.write_text(json.dumps({"model": "BaseM"}))
        monkeypatch.setattr(
            backend_agy, "_load_config",
            lambda: {AGENT: {"model": value}},
        )
        backend_agy.send(AGENT, "hi", 30)
        assert self._per_settings()["model"] == "BaseM"

    def test_no_model_no_base_omits_model_key(self, recorder, monkeypatch):
        monkeypatch.setattr(backend_agy, "_load_config", lambda: {AGENT: {}})
        backend_agy.send(AGENT, "hi", 30)
        assert "model" not in self._per_settings()


# ===========================================================================
# pid / marker / liveness
# ===========================================================================

class TestPidMarker:
    def test_pid_written(self, recorder):
        backend_agy.send(AGENT, "hi", 30)
        assert backend_agy._pid_path(AGENT).read_text() == "4242"

    def test_session_marker_touched(self, recorder):
        assert not backend_agy._session_marker_path(AGENT).exists()
        backend_agy.send(AGENT, "hi", 30)
        assert backend_agy._session_marker_path(AGENT).exists()

    def test_pid_write_failure_terminates_proc(self, recorder, monkeypatch):
        terminate_calls: list = []

        def fake_terminate(pid, agent_id, proc=None):
            terminate_calls.append((pid, agent_id, proc))
            return True

        monkeypatch.setattr(backend_agy, "_terminate_pid_tree", fake_terminate)

        orig = Path.write_text

        def fake_write_text(self, data, *a, **k):
            if self.name.endswith(".pid"):
                raise OSError("disk full")
            return orig(self, data, *a, **k)

        monkeypatch.setattr(Path, "write_text", fake_write_text)
        result = backend_agy.send(AGENT, "hi", 30)
        assert result is SendResult.FAIL
        assert len(terminate_calls) == 1
        assert not backend_agy._session_marker_path(AGENT).exists()


class TestIsInactive:
    def test_is_inactive_no_pid_file_returns_true(self):
        assert backend_agy.is_inactive(AGENT) is True

    def test_is_inactive_cc_running_returns_false(self):
        assert backend_agy.is_inactive(AGENT, cc_running=True) is False

    def test_is_inactive_alive_returns_false(self, monkeypatch):
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_agy._pid_path(AGENT).write_text("111")
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)
        assert backend_agy.is_inactive(AGENT) is False


# ===========================================================================
# reset_session
# ===========================================================================

class TestResetSession:
    def test_reset_session_removes_marker_and_pid(self, monkeypatch):
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_agy._pid_path(AGENT).write_text("9999")
        backend_agy._session_marker_path(AGENT).touch()
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: False)
        backend_agy.reset_session(AGENT)
        assert not backend_agy._pid_path(AGENT).exists()
        assert not backend_agy._session_marker_path(AGENT).exists()

    def test_reset_session_terminates_live_process(self, monkeypatch):
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)
        calls: list = []
        monkeypatch.setattr(
            backend_agy, "_terminate_pid_tree",
            lambda pid, agent_id, proc=None: calls.append(pid) or True,
        )
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_agy._pid_path(AGENT).write_text("5555")
        backend_agy._session_marker_path(AGENT).touch()
        backend_agy.reset_session(AGENT)
        assert calls == [5555]
        assert not backend_agy._pid_path(AGENT).exists()

    def test_reset_session_does_not_touch_settings_json(self, monkeypatch):
        per_dir = _profile_dir() / ".gemini" / "antigravity-cli"
        per_dir.mkdir(parents=True)
        settings = per_dir / "settings.json"
        settings.write_text('{"keep": "me"}')
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: False)
        backend_agy.reset_session(AGENT)
        assert settings.read_text() == '{"keep": "me"}'

    def test_reset_session_purges_conversations_directory(self, monkeypatch):
        conv_dir = _profile_dir() / ".gemini" / "antigravity-cli" / "conversations"
        conv_dir.mkdir(parents=True)
        old = conv_dir / "old.pb"
        old.write_text("x")
        recent = conv_dir / "recent.pb"
        recent.write_text("y")
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: False)
        backend_agy.reset_session(AGENT)
        assert not old.exists()
        assert not recent.exists()
        assert not conv_dir.exists()

    def test_reset_session_noop_when_agent_home_is_symlink(
        self, monkeypatch, tmp_path,
    ):
        import shutil as _shutil
        profile = _profile_dir()
        if profile.exists():
            _shutil.rmtree(profile)
        real_target = tmp_path / "real_agent"
        real_target.mkdir()
        profile.symlink_to(real_target)

        terminate_calls: list = []
        monkeypatch.setattr(
            backend_agy, "_terminate_pid_tree",
            lambda *a, **k: terminate_calls.append(a) or True,
        )
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_agy._pid_path(AGENT).write_text("1234")
        backend_agy._session_marker_path(AGENT).touch()
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)

        backend_agy.reset_session(AGENT)
        assert terminate_calls == []
        assert backend_agy._pid_path(AGENT).exists()
        assert backend_agy._session_marker_path(AGENT).exists()

    def test_reset_session_skips_purge_when_agent_home_is_symlink(
        self, monkeypatch, tmp_path,
    ):
        import shutil as _shutil
        profile = _profile_dir()
        if profile.exists():
            _shutil.rmtree(profile)
        real_target = tmp_path / "real_agent"
        real_target.mkdir()
        conv_dir = real_target / ".gemini" / "antigravity-cli" / "conversations"
        conv_dir.mkdir(parents=True)
        (conv_dir / "x.pb").write_text("keep")
        profile.symlink_to(real_target)
        # Direct test of _purge_session_data_on_reset
        backend_agy._purge_session_data_on_reset(AGENT)
        assert (conv_dir / "x.pb").exists()

    def test_reset_session_skips_purge_when_gemini_is_symlink(
        self, monkeypatch, tmp_path,
    ):
        profile = _profile_dir()
        fake_real = tmp_path / "real_gemini"
        (fake_real / "antigravity-cli" / "conversations").mkdir(parents=True)
        (fake_real / "antigravity-cli" / "conversations" / "x.pb").write_text("keep")
        (profile / ".gemini").symlink_to(fake_real)
        backend_agy._purge_session_data_on_reset(AGENT)
        assert (fake_real / "antigravity-cli" / "conversations" / "x.pb").exists()

    def test_reset_session_skips_purge_when_antigravity_cli_is_symlink(
        self, tmp_path,
    ):
        profile = _profile_dir()
        gemini = profile / ".gemini"
        gemini.mkdir()
        fake_cli = tmp_path / "real_cli"
        (fake_cli / "conversations").mkdir(parents=True)
        (fake_cli / "conversations" / "x.pb").write_text("keep")
        (gemini / "antigravity-cli").symlink_to(fake_cli)
        backend_agy._purge_session_data_on_reset(AGENT)
        assert (fake_cli / "conversations" / "x.pb").exists()

    def test_reset_session_purges_brain_directory(self, monkeypatch):
        cli_dir = _profile_dir() / ".gemini" / "antigravity-cli"
        conv_dir = cli_dir / "conversations"
        brain_dir = cli_dir / "brain"
        conv_dir.mkdir(parents=True)
        brain_dir.mkdir(parents=True)
        (conv_dir / "abc.pb").write_text("c")
        (brain_dir / "abc").mkdir()
        (brain_dir / "abc" / "memory.bin").write_text("b")
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: False)
        backend_agy.reset_session(AGENT)
        assert not conv_dir.exists()
        assert not brain_dir.exists()

    def test_reset_session_skips_purge_when_brain_is_symlink(
        self, tmp_path,
    ):
        profile = _profile_dir()
        cli_dir = profile / ".gemini" / "antigravity-cli"
        cli_dir.mkdir(parents=True)
        fake_brain = tmp_path / "real_brain"
        fake_brain.mkdir()
        (fake_brain / "kept.bin").write_text("keep")
        (cli_dir / "brain").symlink_to(fake_brain)
        backend_agy._purge_session_data_on_reset(AGENT)
        assert (fake_brain / "kept.bin").exists()

    def test_reset_session_skips_purge_when_conv_dir_is_symlink(
        self, tmp_path,
    ):
        profile = _profile_dir()
        cli_dir = profile / ".gemini" / "antigravity-cli"
        cli_dir.mkdir(parents=True)
        fake_conv = tmp_path / "real_conv"
        fake_conv.mkdir()
        (fake_conv / "x.pb").write_text("keep")
        (cli_dir / "conversations").symlink_to(fake_conv)
        backend_agy._purge_session_data_on_reset(AGENT)
        assert (fake_conv / "x.pb").exists()


class TestSoftReap:
    def test_terminates_live_process_and_removes_pid(self, monkeypatch):
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)
        calls: list = []
        monkeypatch.setattr(
            backend_agy, "_terminate_pid_tree",
            lambda pid, agent_id, proc=None: calls.append(pid) or True,
        )
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_agy._pid_path(AGENT).write_text("5555")
        backend_agy._session_marker_path(AGENT).touch()
        backend_agy.soft_reap(AGENT)
        assert calls == [5555]
        assert not backend_agy._pid_path(AGENT).exists()

    def test_preserves_conversations_and_session_marker(self, monkeypatch):
        # Differentiate from reset_session: context-preserving primitives must
        # NOT be invoked.
        purge_calls: list = []
        monkeypatch.setattr(
            backend_agy, "_purge_session_data_on_reset",
            lambda agent_id: purge_calls.append(agent_id),
        )
        cli_dir = _profile_dir() / ".gemini" / "antigravity-cli"
        conv_dir = cli_dir / "conversations"
        conv_dir.mkdir(parents=True)
        (conv_dir / "abc.pb").write_text("keep")
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)
        monkeypatch.setattr(
            backend_agy, "_terminate_pid_tree",
            lambda pid, agent_id, proc=None: True,
        )
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_agy._pid_path(AGENT).write_text("5555")
        backend_agy._session_marker_path(AGENT).touch()
        backend_agy.soft_reap(AGENT)
        assert purge_calls == []
        assert backend_agy._session_marker_path(AGENT).exists()
        assert (conv_dir / "abc.pb").read_text() == "keep"
        assert not backend_agy._pid_path(AGENT).exists()

    def test_kill_failure_keeps_pid_and_early_returns(self, monkeypatch):
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)
        monkeypatch.setattr(
            backend_agy, "_terminate_pid_tree",
            lambda pid, agent_id, proc=None: False,
        )
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_agy._pid_path(AGENT).write_text("5555")
        backend_agy._session_marker_path(AGENT).touch()
        backend_agy.soft_reap(AGENT)
        # pid file kept to preserve the BUSY guard (prevents double-spawn)
        assert backend_agy._pid_path(AGENT).exists()
        assert backend_agy._session_marker_path(AGENT).exists()

    def test_stale_pid_dead_process_removed_marker_kept(self, monkeypatch):
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: False)
        terminate_calls: list = []
        monkeypatch.setattr(
            backend_agy, "_terminate_pid_tree",
            lambda *a, **k: terminate_calls.append(a) or True,
        )
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_agy._pid_path(AGENT).write_text("9999")
        backend_agy._session_marker_path(AGENT).touch()
        backend_agy.soft_reap(AGENT)
        assert terminate_calls == []
        assert not backend_agy._pid_path(AGENT).exists()
        assert backend_agy._session_marker_path(AGENT).exists()

    def test_noop_when_no_pid_file(self, monkeypatch):
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        # No exception should be raised when there is no live process.
        backend_agy.soft_reap(AGENT)
        assert not backend_agy._pid_path(AGENT).exists()

    def test_dry_run_returns_immediately(self, monkeypatch):
        monkeypatch.setattr(config, "DRY_RUN", True)
        terminate_calls: list = []
        monkeypatch.setattr(
            backend_agy, "_terminate_pid_tree",
            lambda *a, **k: terminate_calls.append(a) or True,
        )
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_agy._pid_path(AGENT).write_text("5555")
        backend_agy.soft_reap(AGENT)
        assert terminate_calls == []
        # pid untouched in dry-run
        assert backend_agy._pid_path(AGENT).exists()

    def test_pid_unlink_oserror_logs_and_does_not_raise(
        self, monkeypatch, caplog,
    ):
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: False)
        backend_agy.AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
        backend_agy._pid_path(AGENT).write_text("9999")

        orig_unlink = Path.unlink

        def _fail_unlink(self, *a, **k):
            if self == backend_agy._pid_path(AGENT):
                raise OSError("permission denied")
            return orig_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", _fail_unlink)
        with caplog.at_level(logging.WARNING):
            backend_agy.soft_reap(AGENT)
        assert any("failed to delete pid file" in r.message for r in caplog.records)


# ===========================================================================
# AGENTS.md compile
# ===========================================================================

class TestRebuildAgyMd:
    def _enable_compile(self, monkeypatch):
        monkeypatch.setattr(
            backend_agy, "_load_config",
            lambda: {AGENT: {"compile-startup-md": True}},
        )

    def test_rebuild_agy_md_writes_agents_md_in_cwd(self, monkeypatch):
        self._enable_compile(monkeypatch)
        pd = _profile_dir()
        (pd / "IDENTITY.md").write_text("id")
        (pd / "INSTRUCTION.md").write_text("instr")
        (pd / "MEMORY.md").write_text("mem")
        backend_agy._rebuild_agy_md(AGENT)
        amd = pd / "AGENTS.md"
        assert amd.exists()
        assert (pd / ".agy_hash").exists()
        body = amd.read_text()
        assert "id" in body and "instr" in body and "mem" in body

    def test_rebuild_agy_md_hash_skip_unchanged(self, monkeypatch):
        self._enable_compile(monkeypatch)
        pd = _profile_dir()
        (pd / "IDENTITY.md").write_text("id")
        backend_agy._rebuild_agy_md(AGENT)
        amd = pd / "AGENTS.md"
        first = amd.stat().st_mtime_ns
        import time as _t
        _t.sleep(0.01)
        backend_agy._rebuild_agy_md(AGENT)
        # If unchanged the file is not rewritten
        assert amd.stat().st_mtime_ns == first


# ===========================================================================
# stale GEMINI.md handling
# ===========================================================================

class TestStaleGeminiMd:
    def test_gemini_md_hardlink_preserved_to_timestamped_bak(self):
        pd = _profile_dir()
        gmd = pd / "GEMINI.md"
        gmd.write_text("legacy content")
        ok = backend_agy._remove_stale_gemini_md(AGENT)
        assert ok is True
        assert not gmd.exists()
        baks = list(pd.glob("GEMINI.md.bak.*"))
        assert len(baks) == 1
        assert baks[0].read_text() == "legacy content"

    def test_gemini_hash_deleted(self):
        pd = _profile_dir()
        (pd / ".gemini_hash").write_text("deadbeef")
        ok = backend_agy._remove_stale_gemini_md(AGENT)
        assert ok is True
        assert not (pd / ".gemini_hash").exists()

    def test_no_gemini_md_is_noop(self):
        assert backend_agy._remove_stale_gemini_md(AGENT) is True

    def test_gemini_md_link_failure_fails_send(self, monkeypatch, recorder):
        pd = _profile_dir()
        (pd / "GEMINI.md").write_text("x")
        import os as _os
        def boom(src, dst, *a, **k):
            raise OSError("EXDEV")
        monkeypatch.setattr(_os, "link", boom)
        result = backend_agy.send(AGENT, "hi", 30)
        assert result is SendResult.FAIL
        assert recorder["popen_calls"] == []

    def test_existing_bak_same_timestamp_not_overwritten(self, monkeypatch):
        pd = _profile_dir()
        gmd = pd / "GEMINI.md"
        gmd.write_text("new content")
        # Force time_ns to a fixed value, pre-create bak with different content
        fixed_ns = 1234567890
        monkeypatch.setattr(backend_agy.time, "time_ns", lambda: fixed_ns)
        bak = pd / f"GEMINI.md.bak.{fixed_ns}"
        bak.write_text("existing backup")
        # Simulate cleanup failure so the pre-existing bak survives, exercising
        # the os.link FileExistsError defensive guard.
        orig_unlink = Path.unlink

        def fail_for_bak(self, *a, **k):
            if self.name.startswith("GEMINI.md.bak."):
                raise PermissionError("denied")
            return orig_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", fail_for_bak)
        result = backend_agy._remove_stale_gemini_md(AGENT)
        assert result is False
        assert bak.read_text() == "existing backup"
        # Original GEMINI.md must remain (we did not unlink it on failure)
        assert gmd.exists()

    def test_old_baks_cleaned_up_keeping_only_latest(self):
        pd = _profile_dir()
        old1 = pd / "GEMINI.md.bak.111"
        old2 = pd / "GEMINI.md.bak.222"
        old1.write_text("old1")
        old2.write_text("old2")
        (pd / "GEMINI.md").write_text("current")
        ok = backend_agy._remove_stale_gemini_md(AGENT)
        assert ok is True
        assert not old1.exists()
        assert not old2.exists()
        baks = list(pd.glob("GEMINI.md.bak.*"))
        assert len(baks) == 1
        assert baks[0].read_text() == "current"

    def test_edited_gemini_md_with_hash_still_preserved(self):
        pd = _profile_dir()
        (pd / "GEMINI.md").write_text("user edited")
        (pd / ".gemini_hash").write_text("stalehash")
        ok = backend_agy._remove_stale_gemini_md(AGENT)
        assert ok is True
        baks = list(pd.glob("GEMINI.md.bak.*"))
        assert len(baks) == 1
        assert baks[0].read_text() == "user edited"


# ===========================================================================
# ping
# ===========================================================================

class TestPing:
    def test_always_true(self):
        assert backend_agy.ping(AGENT, 5) is True


# ===========================================================================
# _load_config
# ===========================================================================

class TestLoadConfig:
    def test_missing_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(backend_agy, "AGY_AGENT_CONFIG", tmp_path / "missing.json")
        assert backend_agy._load_config() == {}

    def test_invalid_json(self, monkeypatch, tmp_path, caplog):
        cfg = tmp_path / "bad.json"
        cfg.write_text("{ not valid")
        monkeypatch.setattr(backend_agy, "AGY_AGENT_CONFIG", cfg)
        with caplog.at_level(logging.WARNING, logger="engine.backend_agy"):
            assert backend_agy._load_config() == {}
        assert any("Invalid JSON" in r.message for r in caplog.records)


# ===========================================================================
# _is_agy_pid_alive
# ===========================================================================

class TestIsAgyPidAlive:
    def _setup_proc(self, monkeypatch, exists, cmdline):
        class FakePath:
            def __init__(self, p):
                self._p = str(p)

            def __truediv__(self, other):
                return FakePath(f"{self._p}/{other}")

            def exists(self):
                return exists

            def read_bytes(self):
                return cmdline

        monkeypatch.setattr(backend_agy, "Path", FakePath)

    def test_cmdline_agy_exact(self, monkeypatch):
        self._setup_proc(monkeypatch, True, b"agy\x00-p\x00hi\x00")
        assert backend_agy._is_agy_pid_alive(1) is True

    def test_cmdline_agy_full_path(self, monkeypatch):
        self._setup_proc(monkeypatch, True, b"/home/me/.local/bin/agy\x00-p\x00")
        assert backend_agy._is_agy_pid_alive(1) is True

    def test_cmdline_other(self, monkeypatch):
        self._setup_proc(monkeypatch, True, b"python\x00x.py\x00")
        assert backend_agy._is_agy_pid_alive(1) is False

    def test_proc_missing(self, monkeypatch):
        self._setup_proc(monkeypatch, False, b"")
        assert backend_agy._is_agy_pid_alive(1) is False


# ===========================================================================
# _terminate_pid_tree smoke
# ===========================================================================

class TestTerminatePidTree:
    def test_sigterm_success(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(backend_agy.os, "getpgid", lambda p: p)
        monkeypatch.setattr(backend_agy.os, "killpg",
                            lambda pgid, sig: calls.append(sig))
        proc = MagicMock()
        proc.wait.return_value = 0
        backend_agy._terminate_pid_tree(1234, AGENT, proc=proc)
        assert calls == [backend_agy.signal.SIGTERM]


# ===========================================================================
# Concurrent send / reset (flock + live-process guard)
# ===========================================================================

class TestConcurrentSend:
    def test_concurrent_send_serialized_by_flock(self, monkeypatch):
        """Two concurrent sends must serialize via flock and both spawn."""
        import threading
        # Live-process check returns False so neither send is rejected as BUSY.
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: False)

        popen_calls: list[dict] = []
        in_popen = threading.Event()
        release_popen = threading.Event()
        call_index = {"i": 0}
        lock = threading.Lock()

        class FakeProc:
            def __init__(self, pid):
                self.pid = pid
            def wait(self, timeout=None):
                return 0

        def fake_popen(cmd, **kwargs):
            with lock:
                call_index["i"] += 1
                idx = call_index["i"]
            popen_calls.append({"cmd": list(cmd), "idx": idx})
            if idx == 1:
                in_popen.set()
                # Wait so thread 2 attempts to spawn while we hold the lock
                release_popen.wait(timeout=2.0)
            return FakeProc(pid=4000 + idx)

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        results: list = []

        def runner():
            results.append(backend_agy.send(AGENT, "hi", 30))

        t1 = threading.Thread(target=runner)
        t2 = threading.Thread(target=runner)
        t1.start()
        assert in_popen.wait(timeout=2.0), "thread 1 should enter Popen"
        t2.start()
        # Give thread 2 time to attempt the lock and block on flock
        import time as _t
        _t.sleep(0.1)
        # Only thread 1's Popen should have run so far
        assert len(popen_calls) == 1
        release_popen.set()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)
        assert len(popen_calls) == 2
        assert all(r is SendResult.OK for r in results)

    def test_reset_session_blocks_concurrent_send(self, monkeypatch):
        import threading
        # Both sequences will see no live agy
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: False)

        events = {
            "reset_entered": threading.Event(),
            "release_reset": threading.Event(),
            "send_done": threading.Event(),
        }

        original_purge = backend_agy._purge_session_data_on_reset

        def slow_purge(agent_id):
            events["reset_entered"].set()
            events["release_reset"].wait(timeout=2.0)
            original_purge(agent_id)

        monkeypatch.setattr(
            backend_agy, "_purge_session_data_on_reset", slow_purge,
        )

        popen_calls: list = []

        class FakeProc:
            pid = 4242
            def wait(self, timeout=None):
                return 0

        def fake_popen(cmd, **kwargs):
            popen_calls.append(cmd)
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        def reset_runner():
            backend_agy.reset_session(AGENT)

        def send_runner():
            backend_agy.send(AGENT, "hi", 30)
            events["send_done"].set()

        t_reset = threading.Thread(target=reset_runner)
        t_reset.start()
        assert events["reset_entered"].wait(timeout=2.0)

        t_send = threading.Thread(target=send_runner)
        t_send.start()
        import time as _t
        _t.sleep(0.1)
        # send must be blocked by reset's lock
        assert popen_calls == []
        events["release_reset"].set()
        t_reset.join(timeout=5.0)
        t_send.join(timeout=5.0)
        assert len(popen_calls) == 1

    def test_dry_run_bypasses_flock(self, monkeypatch, recorder):
        import contextlib as _c
        monkeypatch.setattr(config, "DRY_RUN", True)
        lock_calls: list = []

        @_c.contextmanager
        def fake_lock(agent_id):
            lock_calls.append(agent_id)
            yield

        monkeypatch.setattr(backend_agy, "_per_agent_lock", fake_lock)
        result = backend_agy.send(AGENT, "hi", 30)
        assert result is SendResult.OK
        assert lock_calls == []
