"""Tests for engine/backend.py dispatch and integration with notify/shared/reviewer."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from engine import backend, backend_pi, reviewer as _reviewer_mod

# Save real function references before conftest's autouse fixtures replace them
# with mocks.  Module-level imports execute before per-test fixtures.
_real_reset_reviewers = _reviewer_mod._reset_reviewers
_real_reset_short_context = _reviewer_mod._reset_short_context_reviewers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_backend(monkeypatch):
    """Ensure default backend for each test."""
    monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")


@pytest.fixture(autouse=True)
def _reset_starting_markers():
    backend_pi._starting_markers.clear()
    yield
    backend_pi._starting_markers.clear()


# ===========================================================================
# Unsupported backend
# ===========================================================================

class TestUnsupportedBackend:
    def test_send_raises_valueerror(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "unknown")
        with pytest.raises(ValueError, match="Unsupported AGENT_BACKEND"):
            backend.send("reviewer1", "hello", 30)

    def test_ping_raises_valueerror(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "unknown")
        with pytest.raises(ValueError, match="Unsupported AGENT_BACKEND"):
            backend.ping("reviewer1", 30)

    def test_is_inactive_raises_valueerror(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "unknown")
        with pytest.raises(ValueError, match="Unsupported AGENT_BACKEND"):
            backend.is_inactive("reviewer1")

    def test_reset_session_raises_valueerror(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "unknown")
        with pytest.raises(ValueError, match="Unsupported AGENT_BACKEND"):
            backend.reset_session("reviewer1")


# ===========================================================================
# backend.send dispatch (test dispatch layer directly, not via notify wrappers)
# ===========================================================================

class TestBackendSendDispatch:
    def test_openclaw_calls_gateway(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")
        with patch("notify._send_to_agent_openclaw", return_value=True) as mock_oc:
            result = backend.send("reviewer1", "hello", 30)
        assert result is True
        mock_oc.assert_called_once_with("reviewer1", "hello", 30)

    def test_pi_calls_pi_send(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "pi")
        with patch("engine.backend_pi.send", return_value=True) as mock_pi:
            result = backend.send("reviewer1", "hello", 30)
        assert result is True
        mock_pi.assert_called_once_with("reviewer1", "hello", 30)


# ===========================================================================
# backend.ping dispatch
# ===========================================================================

class TestBackendPingDispatch:
    def test_openclaw_calls_openclaw_ping(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")
        with patch("notify._ping_agent_openclaw", return_value=True) as mock_oc:
            result = backend.ping("reviewer1", 20)
        assert result is True
        mock_oc.assert_called_once_with("reviewer1", 20)

    def test_pi_always_true(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "pi")
        result = backend.ping("reviewer1", 20)
        assert result is True


# ===========================================================================
# backend.is_inactive dispatch
# ===========================================================================

class TestBackendIsInactiveDispatch:
    def test_openclaw_preserves_semantics(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")
        with patch("engine.shared._is_agent_inactive_openclaw", return_value=True) as mock_oc:
            result = backend.is_inactive("reviewer1")
        assert result is True
        mock_oc.assert_called_once_with("reviewer1")

    def test_openclaw_cc_running_returns_false(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")
        with patch("engine.shared._is_cc_running", return_value=True):
            result = backend.is_inactive("reviewer1", {"cc_pid": 12345})
        assert result is False

    def test_pi_dispatches(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "pi")
        with patch("engine.backend_pi.is_inactive", return_value=False):
            result = backend.is_inactive("reviewer1")
        assert result is False

    def test_regression_openclaw_inactive_path_unchanged(self, monkeypatch, tmp_path):
        """Regression: openclaw inactivity check uses sessions.json as before."""
        monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")
        import json
        from datetime import datetime
        sessions_dir = tmp_path / "reviewer1" / "sessions"
        sessions_dir.mkdir(parents=True)
        stale_ts = int((datetime.now().timestamp() - 600) * 1000)
        sessions_file = sessions_dir / "sessions.json"
        sessions_file.write_text(json.dumps({
            "agent:reviewer1:main": {"updatedAt": stale_ts}
        }))
        monkeypatch.setattr("engine.shared.SESSIONS_BASE", tmp_path)
        result = backend.is_inactive("reviewer1")
        assert result is True


# ===========================================================================
# engine.shared._is_agent_inactive dispatches through backend
# ===========================================================================

class TestSharedIsInactiveDispatch:
    def test_shared_dispatches_to_backend(self, monkeypatch):
        """_is_agent_inactive in shared.py delegates to engine.backend."""
        monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")
        with patch("engine.backend.is_inactive", return_value=True) as mock_dispatch:
            from engine.shared import _is_agent_inactive
            result = _is_agent_inactive("reviewer1", {"some": "data"})
        assert result is True
        mock_dispatch.assert_called_once_with("reviewer1", {"some": "data"})


# ===========================================================================
# Reviewer reset dispatch (test the branching logic directly)
# ===========================================================================

class TestReviewerResetDispatch:
    @pytest.fixture(autouse=True)
    def _restore_real_reset(self, monkeypatch):
        """Restore real _reset_reviewers (conftest replaces it with a Mock)."""
        monkeypatch.setattr(_reviewer_mod, "_reset_reviewers", _real_reset_reviewers)
        monkeypatch.setattr(_reviewer_mod, "_reset_short_context_reviewers", _real_reset_short_context)

    def test_openclaw_sends_new(self, monkeypatch):
        """openclaw backend sends /new via send_to_agent_queued."""
        monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")
        monkeypatch.setattr(config, "DRY_RUN", False)
        with patch.object(_reviewer_mod, "send_to_agent_queued", return_value=True) as mock_send, \
             patch.object(_reviewer_mod, "ping_agent", return_value=True), \
             patch("time.sleep"):
            _real_reset_reviewers(review_mode="standard")
        assert mock_send.call_count > 0
        for c in mock_send.call_args_list:
            assert c[0][1] == "/new"

    def test_pi_calls_reset_session_not_new(self, monkeypatch):
        """pi backend calls reset_session, not /new."""
        monkeypatch.setattr(config, "AGENT_BACKEND", "pi")
        with patch("engine.backend.reset_session") as mock_reset, \
             patch.object(_reviewer_mod, "send_to_agent_queued") as mock_send:
            _real_reset_reviewers(review_mode="standard")
        assert mock_reset.call_count > 0
        mock_send.assert_not_called()

    def test_pi_reset_returns_empty_excluded(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "pi")
        with patch("engine.backend.reset_session"):
            excluded = _real_reset_reviewers(review_mode="standard")
        assert excluded == []

    def test_openclaw_reset_waits(self, monkeypatch):
        """Regression: openclaw reset path includes POST_NEW_COMMAND_WAIT_SEC sleep."""
        monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")
        monkeypatch.setattr(config, "DRY_RUN", False)
        with patch.object(_reviewer_mod, "send_to_agent_queued", return_value=True), \
             patch.object(_reviewer_mod, "ping_agent", return_value=True), \
             patch("time.sleep") as mock_sleep:
            _real_reset_reviewers(review_mode="standard")
        mock_sleep.assert_called()


# ===========================================================================
# Short-context reset dispatch
# ===========================================================================

class TestShortContextResetDispatch:
    @pytest.fixture(autouse=True)
    def _restore_real_reset(self, monkeypatch):
        """Restore real functions (conftest replaces them with Mocks)."""
        monkeypatch.setattr(_reviewer_mod, "_reset_reviewers", _real_reset_reviewers)
        monkeypatch.setattr(_reviewer_mod, "_reset_short_context_reviewers", _real_reset_short_context)

    def test_openclaw_sends_new_and_waits(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")
        monkeypatch.setattr(config, "DRY_RUN", False)
        with patch.object(_reviewer_mod, "send_to_agent_queued", return_value=True) as mock_send, \
             patch("time.sleep") as mock_sleep:
            _real_reset_short_context("full")
        if mock_send.call_count > 0:
            for c in mock_send.call_args_list:
                assert c[0][1] == "/new"
            mock_sleep.assert_called()

    def test_pi_calls_reset_session_no_sleep(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "pi")
        with patch("engine.backend.reset_session"), \
             patch.object(_reviewer_mod, "send_to_agent_queued") as mock_send, \
             patch("time.sleep") as mock_sleep:
            _real_reset_short_context("full")
        mock_send.assert_not_called()
        mock_sleep.assert_not_called()


# ===========================================================================
# Architecture guard
# ===========================================================================

class TestArchitectureGuard:
    def test_backend_pi_does_not_import_engine_shared(self):
        """engine.backend_pi must not import engine.shared to avoid cycles."""
        source = Path(backend_pi.__file__).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("engine.shared"), \
                        f"engine.backend_pi imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("engine.shared"):
                    pytest.fail(
                        f"engine.backend_pi imports from {node.module}"
                    )
