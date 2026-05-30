"""Tests for agy hang detection (#359).

Covers `_agy_last_activity`, the staleness-aware `is_inactive`, the inline
reap in `send()`'s BUSY guard, and the nudge template refresher/phase/guidance
additions. Pure stat/tmp_path based — no external calls, so no conftest
exemption is needed.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

import config
from engine import backend_agy
from engine.backend_types import SendResult
from messages import render

AGENT = "reviewer1"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Isolate pid/profile dirs under tmp_path."""
    monkeypatch.setattr(backend_agy, "_agent_config_cache", None)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "AGY_BIN", "agy")

    pids = tmp_path / "pids"
    profiles = tmp_path / "agents"
    (profiles / AGENT).mkdir(parents=True)
    pids.mkdir(parents=True)
    monkeypatch.setattr(backend_agy, "AGY_PIDS_DIR", pids)
    monkeypatch.setattr(backend_agy, "AGENT_PROFILES_DIR", profiles)
    yield


def _cli_dir() -> Path:
    return (
        backend_agy.AGENT_PROFILES_DIR / AGENT / ".gemini" / "antigravity-cli"
    )


def _set_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


# ===========================================================================
# _agy_last_activity
# ===========================================================================

class TestLastActivity:
    def test_returns_max_mtime(self):
        cli_dir = _cli_dir()
        conv = cli_dir / "conversations"
        conv.mkdir(parents=True)
        pb = conv / "turn.pb"
        pb.write_text("x")
        cli_log = cli_dir / "cli.log"
        cli_log.write_text("log")
        pid_file = backend_agy._pid_path(AGENT)
        pid_file.write_text("123")

        # Excluded files updated by unrelated activity — must be ignored.
        (cli_dir / "last_check.timestamp").write_text("x")

        now = time.time()
        _set_mtime(pb, now - 100)
        _set_mtime(cli_log, now - 200)
        _set_mtime(pid_file, now - 300)
        _set_mtime(cli_dir / "last_check.timestamp", now)  # newest but excluded

        la = backend_agy._agy_last_activity(AGENT)
        assert la == pytest.approx(now - 100, abs=2)

    def test_returns_none_when_no_files(self):
        # conversations empty/absent, no pid file, no cli.log
        assert backend_agy._agy_last_activity(AGENT) is None

    def test_pid_file_mtime_used_when_no_pb(self):
        pid_file = backend_agy._pid_path(AGENT)
        pid_file.write_text("123")
        now = time.time()
        _set_mtime(pid_file, now - 50)
        la = backend_agy._agy_last_activity(AGENT)
        assert la == pytest.approx(now - 50, abs=2)


# ===========================================================================
# is_inactive
# ===========================================================================

class TestIsInactive:
    def test_alive_but_stale_returns_true(self, monkeypatch):
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)
        pid_file = backend_agy._pid_path(AGENT)
        pid_file.write_text("111")
        old = time.time() - backend_agy.INACTIVE_THRESHOLD_SEC - 10
        _set_mtime(pid_file, old)
        assert backend_agy.is_inactive(AGENT) is True

    def test_alive_and_recent_returns_false(self, monkeypatch):
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)
        backend_agy._pid_path(AGENT).write_text("111")  # fresh mtime
        assert backend_agy.is_inactive(AGENT) is False

    def test_alive_no_activity_returns_true(self, monkeypatch):
        # pid in memory but pid file removed after read is not possible here;
        # simulate la is None by deleting pid file is also impossible (read
        # needs it). Instead: pid file exists & fresh would be active, so to
        # get la=None while alive we mock _agy_last_activity.
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)
        backend_agy._pid_path(AGENT).write_text("111")
        monkeypatch.setattr(backend_agy, "_agy_last_activity", lambda a: None)
        assert backend_agy.is_inactive(AGENT) is True

    def test_cc_running_returns_false(self, monkeypatch):
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)
        backend_agy._pid_path(AGENT).write_text("111")
        old = time.time() - backend_agy.INACTIVE_THRESHOLD_SEC - 10
        _set_mtime(backend_agy._pid_path(AGENT), old)
        assert backend_agy.is_inactive(AGENT, cc_running=True) is False

    def test_dead_pid_returns_true(self, monkeypatch):
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: False)
        backend_agy._pid_path(AGENT).write_text("111")  # fresh mtime, but dead
        assert backend_agy.is_inactive(AGENT) is True

    def test_launch_grace_pid_mtime_floor(self, monkeypatch):
        """Fresh pid file mtime keeps a just-spawned process active even with
        no .pb yet."""
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)
        (_cli_dir() / "conversations").mkdir(parents=True)  # empty
        backend_agy._pid_path(AGENT).write_text("111")  # fresh
        assert backend_agy.is_inactive(AGENT) is False


# ===========================================================================
# send() BUSY-guard inline reap
# ===========================================================================

class _FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def send_harness(monkeypatch):
    """Stub the heavy spawn machinery so send() reaches Popen quickly."""
    popen_calls: list[dict] = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(backend_agy, "_rebuild_agy_md", lambda a: None)
    monkeypatch.setattr(backend_agy, "_remove_stale_gemini_md", lambda a: True)
    monkeypatch.setattr(backend_agy, "_load_config", lambda: {AGENT: {}})
    monkeypatch.setattr(
        backend_agy, "_ensure_agy_home",
        lambda a, m: backend_agy.AGENT_PROFILES_DIR / a,
    )
    # Guard: send() must NEVER call soft_reap (flock self-deadlock).
    monkeypatch.setattr(
        backend_agy, "soft_reap",
        lambda *a, **k: (_ for _ in ()).throw(ZeroDivisionError("soft_reap called")),
    )
    return {"popen_calls": popen_calls}


def _make_stale_pid(monkeypatch, pid: int = 9999) -> None:
    pid_file = backend_agy._pid_path(AGENT)
    pid_file.write_text(str(pid))
    old = time.time() - backend_agy.INACTIVE_THRESHOLD_SEC - 10
    _set_mtime(pid_file, old)
    monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)


class TestSendReap:
    def test_stale_terminate_success_respawns(self, monkeypatch, send_harness):
        _make_stale_pid(monkeypatch)
        term_calls: list = []
        monkeypatch.setattr(
            backend_agy, "_terminate_pid_tree",
            lambda pid, aid, proc=None: term_calls.append(pid) or True,
        )
        result = backend_agy.send(AGENT, "hi", 30)
        assert result is SendResult.OK
        assert term_calls == [9999]
        assert len(send_harness["popen_calls"]) == 1
        # pid file now holds the new (spawned) pid
        assert backend_agy._pid_path(AGENT).read_text() == "4242"

    def test_stale_terminate_failure_keeps_busy(self, monkeypatch, send_harness):
        _make_stale_pid(monkeypatch)
        monkeypatch.setattr(
            backend_agy, "_terminate_pid_tree",
            lambda pid, aid, proc=None: False,
        )
        result = backend_agy.send(AGENT, "hi", 30)
        assert result is SendResult.BUSY
        assert send_harness["popen_calls"] == []
        # pid file preserved (dual-spawn guard)
        assert backend_agy._pid_path(AGENT).read_text() == "9999"

    def test_live_and_recent_returns_busy(self, monkeypatch, send_harness):
        pid_file = backend_agy._pid_path(AGENT)
        pid_file.write_text("9999")  # fresh mtime
        monkeypatch.setattr(backend_agy, "_is_agy_pid_alive", lambda p: True)
        term_calls: list = []
        monkeypatch.setattr(
            backend_agy, "_terminate_pid_tree",
            lambda pid, aid, proc=None: term_calls.append(pid) or True,
        )
        result = backend_agy.send(AGENT, "hi", 30)
        assert result is SendResult.BUSY
        assert term_calls == []  # not reaped
        assert send_harness["popen_calls"] == []

    def test_session_marker_preserved_and_continue_flag(self, monkeypatch, send_harness):
        _make_stale_pid(monkeypatch)
        backend_agy._session_marker_path(AGENT).touch()
        monkeypatch.setattr(
            backend_agy, "_terminate_pid_tree",
            lambda pid, aid, proc=None: True,
        )
        result = backend_agy.send(AGENT, "hi", 30)
        assert result is SendResult.OK
        assert backend_agy._session_marker_path(AGENT).exists()
        assert "-c" in send_harness["popen_calls"][0]["cmd"]

    def test_no_pb_after_reap_warns(self, monkeypatch, send_harness, caplog):
        _make_stale_pid(monkeypatch)
        (_cli_dir() / "conversations").mkdir(parents=True)  # empty, no .pb
        monkeypatch.setattr(
            backend_agy, "_terminate_pid_tree",
            lambda pid, aid, proc=None: True,
        )
        with caplog.at_level("WARNING"):
            backend_agy.send(AGENT, "hi", 30)
        assert any("no .pb found after reap" in r.message for r in caplog.records)


# ===========================================================================
# nudge_review template additions (§3-6)
# ===========================================================================

class TestNudgeTemplate:
    def test_blocks_included_when_passed(self):
        out = render(
            "dev.design_review", "nudge_review",
            project="p", issues_display="#1", cmd_lines="cmd",
            refresher_cmds="REFRESH", phase_note="PHASE", guidance="GUIDE",
        )
        assert "REFRESH" in out
        assert "PHASE" in out
        assert "GUIDE" in out
        assert "Anonymous review" in out

    def test_blocks_omitted_when_empty(self):
        out = render(
            "dev.design_review", "nudge_review",
            project="p", issues_display="#1", cmd_lines="cmd",
        )
        assert "Context refresh" not in out
        # Anonymity constraint is always present
        assert "Anonymous review" in out

    def test_code_review_template_same_contract(self):
        out = render(
            "dev.code_review", "nudge_review",
            project="p", issues_display="#1", cmd_lines="cmd",
            refresher_cmds="REFRESH", guidance="GUIDE",
        )
        assert "REFRESH" in out
        assert "GUIDE" in out
        assert "Anonymous review" in out
