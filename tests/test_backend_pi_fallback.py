"""Tests for backend_pi.send() integration with openai_codex_quota.should_fallback."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from engine import backend_pi
from engine.backend_types import SendResult


@pytest.fixture(autouse=True)
def _reset_pi_state(tmp_path, monkeypatch):
    backend_pi._starting_markers.clear()
    backend_pi._agent_config_cache = None
    monkeypatch.setattr("config.PI_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr("engine.backend_pi.PI_SESSIONS_DIR", tmp_path / "sessions")
    yield
    backend_pi._starting_markers.clear()
    backend_pi._agent_config_cache = None


def _set_config(monkeypatch, tmp_path, profile):
    cfg_path = tmp_path / "config_pi.json"
    cfg_path.write_text(json.dumps({"impl1": profile}))
    monkeypatch.setattr("config.PI_AGENT_CONFIG", cfg_path)
    monkeypatch.setattr("engine.backend_pi.PI_AGENT_CONFIG", cfg_path)


def _capture_popen():
    """Build a mock subprocess.Popen that captures invocation args."""
    proc = MagicMock()
    proc.stdin = MagicMock()
    factory = MagicMock(return_value=proc)
    return factory, proc


def test_send_uses_fallback_provider_when_active(tmp_path, monkeypatch):
    _set_config(monkeypatch, tmp_path, {
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "fallback": True,
        "fallback_provider": "github-copilot",
        "fallback_model": "gpt-5.4",
    })
    factory, _ = _capture_popen()
    with patch("subprocess.Popen", factory), \
         patch("engine.openai_codex_quota.should_fallback",
               return_value=(True, "github-copilot", "gpt-5.4", True)):
        result = backend_pi.send("impl1", "hi", timeout=10)
    assert result is SendResult.OK
    cmd = factory.call_args[0][0]
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "github-copilot/gpt-5.4"


def test_send_uses_original_provider_when_inactive(tmp_path, monkeypatch):
    _set_config(monkeypatch, tmp_path, {
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "fallback": True,
        "fallback_provider": "github-copilot",
        "fallback_model": "gpt-5.4",
    })
    factory, _ = _capture_popen()
    with patch("subprocess.Popen", factory), \
         patch("engine.openai_codex_quota.should_fallback",
               return_value=(False, "", "", False)):
        backend_pi.send("impl1", "hi", timeout=10)
    cmd = factory.call_args[0][0]
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "openai-codex/gpt-5.4"


def test_send_skips_fallback_check_when_disabled(tmp_path, monkeypatch):
    _set_config(monkeypatch, tmp_path, {
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "fallback": False,
    })
    factory, _ = _capture_popen()
    with patch("subprocess.Popen", factory), \
         patch("engine.openai_codex_quota.should_fallback") as mock_sf:
        backend_pi.send("impl1", "hi", timeout=10)
    mock_sf.assert_not_called()
    cmd = factory.call_args[0][0]
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "openai-codex/gpt-5.4"


def test_send_skips_fallback_for_non_codex_provider(tmp_path, monkeypatch):
    _set_config(monkeypatch, tmp_path, {
        "provider": "google-gemini-cli",
        "model": "gemini-3.1-pro",
        "fallback": True,
    })
    factory, _ = _capture_popen()
    with patch("subprocess.Popen", factory), \
         patch("engine.openai_codex_quota.should_fallback") as mock_sf:
        backend_pi.send("impl1", "hi", timeout=10)
    mock_sf.assert_not_called()


def test_send_does_not_mutate_cached_profile(tmp_path, monkeypatch):
    """When fallback active, profile dict in _agent_config_cache must be unchanged."""
    _set_config(monkeypatch, tmp_path, {
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "fallback": True,
        "fallback_provider": "github-copilot",
        "fallback_model": "gpt-5.4",
    })
    factory, _ = _capture_popen()
    with patch("subprocess.Popen", factory), \
         patch("engine.openai_codex_quota.should_fallback",
               return_value=(True, "github-copilot", "gpt-5.4", True)):
        backend_pi.send("impl1", "hi", timeout=10)
    cached = backend_pi._agent_config_cache["impl1"]
    assert cached["provider"] == "openai-codex"
    assert cached["model"] == "gpt-5.4"
