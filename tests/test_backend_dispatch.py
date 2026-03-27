"""Tests for engine/backend.py dispatch and integration with notify/shared/reviewer."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

import config
import notify
from engine import backend, backend_openclaw, backend_pi, reviewer as _reviewer_mod

# Save real function references before conftest's autouse fixtures replace them
# with mocks. Module-level imports execute before per-test fixtures.
_real_reset_reviewers = _reviewer_mod._reset_reviewers
_real_reset_short_context = _reviewer_mod._reset_short_context_reviewers
_real_send_to_agent = notify.send_to_agent
_real_ping_agent = notify.ping_agent
_real_backend_send = backend.send
_real_backend_ping = backend.ping


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
# Precondition: verify required functions exist in the codebase
# ===========================================================================

class TestPreconditions:
    """Guard: all target/helper functions must exist."""

    @pytest.fixture(autouse=True)
    def _restore_real_bindings(self, monkeypatch):
        """Restore real bindings so precondition checks are not fooled by autouse mocks."""
        monkeypatch.setattr(notify, "send_to_agent", _real_send_to_agent)
        monkeypatch.setattr(notify, "ping_agent", _real_ping_agent)
        monkeypatch.setattr(backend, "send", _real_backend_send)
        monkeypatch.setattr(backend, "ping", _real_backend_ping)

    def test_engine_backend_send_exists(self):
        assert getattr(backend, "send", None) is _real_backend_send

    def test_engine_backend_ping_exists(self):
        assert getattr(backend, "ping", None) is _real_backend_ping

    def test_notify_send_to_agent_exists(self):
        assert getattr(notify, "send_to_agent", None) is _real_send_to_agent

    def test_notify_ping_agent_exists(self):
        assert getattr(notify, "ping_agent", None) is _real_ping_agent

    def test_backend_openclaw_send_exists(self):
        assert callable(getattr(backend_openclaw, "send", None))

    def test_backend_openclaw_ping_exists(self):
        assert callable(getattr(backend_openclaw, "ping", None))


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
# Thin-wrapper propagation: ValueError surfaces unchanged through notify
# ===========================================================================

class TestThinWrapperPropagation:
    """notify.send_to_agent / ping_agent must not swallow dispatch ValueError."""

    @pytest.fixture(autouse=True)
    def _restore_real_wrappers(self, monkeypatch):
        """Restore real wrappers (conftest replaces them with Mocks)."""
        monkeypatch.setattr(notify, "send_to_agent", _real_send_to_agent)
        monkeypatch.setattr(notify, "ping_agent", _real_ping_agent)

    def test_send_to_agent_propagates_same_valueerror_instance(self):
        sentinel = ValueError("sentinel send")
        with patch("engine.backend.send", side_effect=sentinel):
            with pytest.raises(ValueError) as excinfo:
                notify.send_to_agent("reviewer1", "hello", 30)
        assert excinfo.value is sentinel

    def test_ping_agent_propagates_same_valueerror_instance(self):
        sentinel = ValueError("sentinel ping")
        with patch("engine.backend.ping", side_effect=sentinel):
            with pytest.raises(ValueError) as excinfo:
                notify.ping_agent("reviewer1", 20)
        assert excinfo.value is sentinel


# ===========================================================================
# backend.send dispatch (test dispatch layer directly, not via notify wrappers)
# ===========================================================================

class TestBackendSendDispatch:
    def test_openclaw_calls_backend_openclaw_send(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")
        with patch("engine.backend_openclaw.send", return_value=True) as mock_oc:
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
    def test_openclaw_calls_backend_openclaw_ping(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")
        with patch("engine.backend_openclaw.ping", return_value=True) as mock_oc:
            result = backend.ping("reviewer1", 20)
        assert result is True
        mock_oc.assert_called_once_with("reviewer1", 20)

    def test_pi_calls_pi_ping(self, monkeypatch):
        monkeypatch.setattr(config, "AGENT_BACKEND", "pi")
        with patch("engine.backend_pi.ping", return_value=True) as mock_pi:
            result = backend.ping("reviewer1", 20)
        assert result is True
        mock_pi.assert_called_once_with("reviewer1", 20)


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

    def test_shared_cc_running_forces_active_openclaw(self, monkeypatch):
        """At shared._is_agent_inactive boundary, live cc_pid forces active on openclaw."""
        monkeypatch.setattr(config, "AGENT_BACKEND", "openclaw")
        from engine.shared import _is_agent_inactive
        with patch("engine.shared._is_cc_running", return_value=True), \
             patch("engine.shared._is_agent_inactive_openclaw") as mock_oc:
            result = _is_agent_inactive("reviewer1", {"cc_pid": 12345})
        assert result is False
        # Backend-specific check must not be reached when cc_pid is alive
        mock_oc.assert_not_called()


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
        from engine.reviewer import get_tier
        mode_config = config.REVIEW_MODES["full"]
        expected_targets = sorted(
            m for m in mode_config["members"]
            if get_tier(m) == "short-context" and m in config.AGENTS
        )
        with patch("engine.backend.reset_session") as mock_reset, \
             patch.object(_reviewer_mod, "send_to_agent_queued") as mock_send, \
             patch("time.sleep") as mock_sleep:
            _real_reset_short_context("full")
        mock_send.assert_not_called()
        mock_sleep.assert_not_called()
        # Assert reset_session call targets and count
        assert mock_reset.call_count == len(expected_targets)
        actual_targets = sorted(c[0][0] for c in mock_reset.call_args_list)
        assert actual_targets == expected_targets


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

    def test_backend_does_not_import_notify(self):
        """engine/backend.py must not import notify (acyclic goal)."""
        source = Path(backend.__file__).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "notify", \
                        "engine.backend imports notify"
            elif isinstance(node, ast.ImportFrom):
                if node.module and (node.module == "notify" or node.module.startswith("notify.")):
                    pytest.fail(
                        f"engine.backend imports from {node.module}"
                    )

    def test_notify_does_not_define_removed_names(self):
        """notify.py must not define or re-export openclaw backend helpers."""
        removed = [
            "_gateway_chat_send_cli",
            "_gateway_chat_send",
            "_send_to_agent_openclaw",
            "_ping_agent_openclaw",
        ]
        source = Path(notify.__file__).read_text()
        tree = ast.parse(source)
        defined = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined.add(target.id)
        for name in removed:
            assert name not in defined, \
                f"notify.py still defines {name!r}"

    def test_backend_openclaw_exposes_send_and_ping(self):
        """engine/backend_openclaw.py must expose send and ping as public API."""
        source = Path(backend_openclaw.__file__).read_text()
        tree = ast.parse(source)
        top_level_funcs = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                top_level_funcs.add(node.name)
        assert "send" in top_level_funcs, "backend_openclaw missing public send()"
        assert "ping" in top_level_funcs, "backend_openclaw missing public ping()"
