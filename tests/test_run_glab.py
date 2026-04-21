"""tests/test_run_glab.py — engine.glab の run_glab / fetch_issue_state テスト"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.glab as _glab_mod  # noqa: E402

_REAL_FETCH = _glab_mod.fetch_issue_state


def _cp(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["glab"], returncode=rc, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _fix_config(monkeypatch):
    import config
    monkeypatch.setattr(config, "GLAB_BIN", "glab")
    monkeypatch.setattr(config, "GLAB_TIMEOUT", 15)


@pytest.fixture
def sleep_spy(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr("engine.glab.time.sleep", lambda s: calls.append(s))
    return calls


def _patch_run(monkeypatch, side_effect):
    mock = MagicMock(side_effect=side_effect)
    monkeypatch.setattr("engine.glab.subprocess.run", mock)
    return mock


class TestRunGlab:
    def test_g1_happy_path(self, monkeypatch, sleep_spy):
        from engine.glab import run_glab
        m = _patch_run(monkeypatch, [_cp(0, "OK", "")])
        r = run_glab(["issue", "show", "1"])
        assert r.ok and r.stdout == "OK" and r.returncode == 0 and r.error is None
        assert m.call_count == 1
        assert sleep_spy == []

    def test_g2_transient_recovery(self, monkeypatch, sleep_spy):
        from engine.glab import run_glab
        m = _patch_run(monkeypatch, [
            subprocess.TimeoutExpired(cmd="glab", timeout=15),
            subprocess.TimeoutExpired(cmd="glab", timeout=15),
            _cp(0, "OK", ""),
        ])
        r = run_glab(["issue", "show", "1"])
        assert r.ok and r.stdout == "OK"
        assert m.call_count == 3
        assert sleep_spy == [1.0, 2.0]

    def test_g3_all_transient(self, monkeypatch, sleep_spy):
        from engine.glab import run_glab
        _patch_run(monkeypatch, [
            subprocess.TimeoutExpired(cmd="glab", timeout=15),
            subprocess.TimeoutExpired(cmd="glab", timeout=15),
            subprocess.TimeoutExpired(cmd="glab", timeout=15),
        ])
        r = run_glab(["issue", "show", "1"])
        assert not r.ok
        assert r.returncode is None
        assert isinstance(r.error, subprocess.TimeoutExpired)

    def test_g4_glab_not_installed(self, monkeypatch, sleep_spy):
        from engine.glab import run_glab
        m = _patch_run(monkeypatch, [FileNotFoundError("no glab")])
        r = run_glab(["issue", "show", "1"])
        assert not r.ok
        assert r.returncode is None
        assert isinstance(r.error, FileNotFoundError)
        assert m.call_count == 1

    def test_g5_permanent_404(self, monkeypatch, sleep_spy):
        from engine.glab import run_glab
        m = _patch_run(monkeypatch, [_cp(1, "", "error: 404 not found")])
        r = run_glab(["issue", "show", "1"])
        assert not r.ok
        assert r.returncode == 1
        assert r.error is None
        assert m.call_count == 1

    def test_g6_transient_stderr(self, monkeypatch, sleep_spy):
        from engine.glab import run_glab
        m = _patch_run(monkeypatch, [
            _cp(1, "", "connection reset by peer"),
            _cp(1, "", "connection reset by peer"),
            _cp(0, "OK", ""),
        ])
        r = run_glab(["issue", "show", "1"])
        assert r.ok
        assert m.call_count == 3

    def test_g7_unknown_stderr(self, monkeypatch, sleep_spy):
        from engine.glab import run_glab
        m = _patch_run(monkeypatch, [_cp(1, "", "something weird")] * 3)
        r = run_glab(["issue", "show", "1"])
        assert not r.ok
        assert r.returncode == 1
        assert m.call_count == 3

    def test_g8_backoff_schedule(self, monkeypatch, sleep_spy):
        from engine.glab import run_glab
        _patch_run(monkeypatch, [subprocess.TimeoutExpired(cmd="glab", timeout=15)] * 4)
        run_glab(["issue", "show", "1"], retries=4, backoff=0.5)
        assert sleep_spy == [0.5, 1.0, 2.0]

    def test_g9_502_match(self, monkeypatch, sleep_spy):
        from engine.glab import run_glab
        m = _patch_run(monkeypatch, [_cp(1, "", "Got HTTP 502 response")] * 3)
        r = run_glab(["issue", "show", "1"])
        assert not r.ok
        assert m.call_count == 3


class TestFetchIssueState:
    """conftest autouse patches engine.glab.fetch_issue_state; use the
    module-load-time capture _REAL_FETCH to test the real implementation."""

    def _patch(self, monkeypatch, result):
        monkeypatch.setattr(_glab_mod, "run_glab", lambda *a, **kw: result)

    def test_g10a_opened(self, monkeypatch):
        from engine.glab import GlabResult
        self._patch(monkeypatch, GlabResult(True, json.dumps({"state": "opened"}), "", 0, None))
        assert _REAL_FETCH(1, "ns/proj") == "opened"

    def test_g10b_closed(self, monkeypatch):
        from engine.glab import GlabResult
        self._patch(monkeypatch, GlabResult(True, json.dumps({"state": "closed"}), "", 0, None))
        assert _REAL_FETCH(1, "ns/proj") == "closed"

    def test_g10c_failure(self, monkeypatch):
        from engine.glab import GlabResult
        self._patch(monkeypatch, GlabResult(False, "", "err", 1, None))
        assert _REAL_FETCH(1, "ns/proj") is None

    def test_g10d_invalid_json(self, monkeypatch):
        from engine.glab import GlabResult
        self._patch(monkeypatch, GlabResult(True, "not json", "", 0, None))
        assert _REAL_FETCH(1, "ns/proj") is None

    def test_g10e_unknown_state(self, monkeypatch):
        from engine.glab import GlabResult
        self._patch(monkeypatch, GlabResult(True, json.dumps({"state": "locked"}), "", 0, None))
        assert _REAL_FETCH(1, "ns/proj") is None
