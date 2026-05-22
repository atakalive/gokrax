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

    def test_ensure_agy_home_is_idempotent(self, recorder):
        backend_agy._ensure_agy_home(AGENT, "X")
        link = _profile_dir() / ".gemini" / "oauth_creds.json"
        assert link.is_symlink()
        # call again — should not raise
        backend_agy._ensure_agy_home(AGENT, "X")
        assert link.is_symlink()


class TestSymlinkRepair:
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

    def test_reset_session_cleans_old_pb_files(self, monkeypatch):
        import os
        conv_dir = _profile_dir() / ".gemini" / "antigravity-cli" / "conversations"
        conv_dir.mkdir(parents=True)
        old = conv_dir / "old.pb"
        old.write_text("x")
        old_t = (Path(__file__).stat().st_mtime - 100 * 86400)
        os.utime(old, (old_t, old_t))
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: False)
        backend_agy.reset_session(AGENT)
        assert not old.exists()

    def test_reset_session_keeps_recent_pb_files(self, monkeypatch):
        conv_dir = _profile_dir() / ".gemini" / "antigravity-cli" / "conversations"
        conv_dir.mkdir(parents=True)
        recent = conv_dir / "recent.pb"
        recent.write_text("x")
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: False)
        backend_agy.reset_session(AGENT)
        assert recent.exists()


class TestCleanupLogs:
    def test_cleanup_logs_warning_on_oserror(self, monkeypatch, caplog):
        conv_dir = _profile_dir() / ".gemini" / "antigravity-cli" / "conversations"
        conv_dir.mkdir(parents=True)
        pb = conv_dir / "x.pb"
        pb.write_text("x")
        import os
        old_t = (Path(__file__).stat().st_mtime - 100 * 86400)
        os.utime(pb, (old_t, old_t))

        orig_unlink = Path.unlink

        def boom(self, *a, **k):
            if self.name == "x.pb":
                raise OSError("no perm")
            return orig_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", boom)
        with caplog.at_level(logging.WARNING, logger="engine.backend_agy"):
            backend_agy._cleanup_old_conversations(AGENT)
        assert any("failed to remove old conversation" in r.message for r in caplog.records)


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
        result = backend_agy._remove_stale_gemini_md(AGENT)
        assert result is False
        assert bak.read_text() == "existing backup"
        # Original GEMINI.md must remain (we did not unlink it on failure)
        assert gmd.exists()

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
