"""Tests for engine/agent_meta.py."""

from __future__ import annotations

from engine import agent_meta
from engine.agent_meta import AgentMeta, _resolve, lookup, snapshot


def _patch_backend(monkeypatch, backend: str) -> None:
    monkeypatch.setattr(
        "engine.backend.resolve_backend", lambda agent_id, **kw: backend
    )


def test_resolve_pi(monkeypatch):
    _patch_backend(monkeypatch, "pi")
    monkeypatch.setattr(
        "engine.backend_pi._load_config",
        lambda: {"kaneko": {"provider": "github-copilot",
                            "model": "claude-sonnet-4.6", "thinking": "max"}},
    )
    monkeypatch.setattr("engine.openai_codex_quota._cache_path",
                        lambda agent_id: "/nonexistent/x.json")
    meta = _resolve("kaneko")
    assert meta.backend == "pi"
    assert meta.provider == "github-copilot"
    assert meta.model == "claude-sonnet-4.6"
    assert meta.think_level == "max"
    assert meta.captured_at


def test_resolve_cc(monkeypatch):
    _patch_backend(monkeypatch, "cc")
    monkeypatch.setattr(
        "engine.backend_cc._load_config",
        lambda: {"alice": {"model": "claude-opus-4.7", "effort": "high"}},
    )
    meta = _resolve("alice")
    assert meta.backend == "cc"
    assert meta.provider == "anthropic"
    assert meta.model == "claude-opus-4.7"
    assert meta.think_level == "high"


def test_resolve_gemini(monkeypatch):
    _patch_backend(monkeypatch, "gemini")
    monkeypatch.setattr(
        "engine.backend_gemini._load_config",
        lambda: {"pascal": {"model": "gemini-3.1-pro-preview"}},
    )
    meta = _resolve("pascal")
    assert meta.backend == "gemini"
    assert meta.provider == "google"
    assert meta.model == "gemini-3.1-pro-preview"
    assert meta.think_level == ""


def test_resolve_kimi(monkeypatch):
    _patch_backend(monkeypatch, "kimi")
    monkeypatch.setattr(
        "engine.backend_kimi._load_config",
        lambda: {"k1": {"model": "kimi-k2"}},
    )
    meta = _resolve("k1")
    assert meta.backend == "kimi"
    assert meta.provider == "kimi-coder"
    assert meta.model == "kimi-k2"
    assert meta.think_level == ""


def test_resolve_openclaw(monkeypatch):
    _patch_backend(monkeypatch, "openclaw")
    meta = _resolve("oc1")
    assert meta.backend == "openclaw"
    assert meta.provider == "openclaw"
    assert meta.model is None
    assert meta.think_level == ""


def test_resolve_stage1_failure(monkeypatch):
    def boom(agent_id, **kw):
        raise ValueError("bad")
    monkeypatch.setattr("engine.backend.resolve_backend", boom)
    meta = _resolve("x")
    assert meta == AgentMeta(backend="", provider=None, model=None,
                             think_level="", captured_at=meta.captured_at)
    assert meta.captured_at


def test_resolve_stage2_failure(monkeypatch):
    _patch_backend(monkeypatch, "pi")

    def boom():
        raise RuntimeError("config broken")
    monkeypatch.setattr("engine.backend_pi._load_config", boom)
    meta = _resolve("x")
    assert meta.backend == "pi"
    assert meta.provider is None
    assert meta.model is None
    assert meta.think_level == ""


def test_resolve_stage3_failure_preserves_config(monkeypatch):
    _patch_backend(monkeypatch, "pi")
    monkeypatch.setattr(
        "engine.backend_pi._load_config",
        lambda: {"k": {"provider": "openai-codex", "model": "gpt-5.4",
                       "thinking": "medium", "fallback": True}},
    )

    def boom(path):
        raise OSError("io fail")
    monkeypatch.setattr("engine.fallback_cache.read_cache", boom)
    meta = _resolve("k")
    assert meta.provider == "openai-codex"
    assert meta.model == "gpt-5.4"
    assert meta.think_level == "medium"


def test_resolve_pi_fallback_active(monkeypatch, tmp_path):
    _patch_backend(monkeypatch, "pi")
    monkeypatch.setattr(
        "engine.backend_pi._load_config",
        lambda: {"neumann": {"provider": "openai-codex",
                              "model": "gpt-5.4", "thinking": "max",
                              "fallback": True}},
    )
    cache_file = tmp_path / "neumann.json"
    monkeypatch.setattr("engine.openai_codex_quota._cache_path",
                        lambda agent_id: cache_file)
    monkeypatch.setattr("engine.openai_codex_quota._cache_active",
                        lambda cache: True)
    monkeypatch.setattr("engine.fallback_cache.read_cache",
                        lambda path: {"active": True,
                                      "fallback_provider": "github-copilot",
                                      "fallback_model": "claude-sonnet-4.6"})
    meta = _resolve("neumann")
    assert meta.provider == "github-copilot"
    assert meta.model == "claude-sonnet-4.6"
    assert meta.think_level == "max"  # config 由来値が保持される


def test_resolve_pi_fallback_ignored_non_openai_codex(monkeypatch):
    """Active fallback cache is ignored when provider is not openai-codex."""
    _patch_backend(monkeypatch, "pi")
    monkeypatch.setattr(
        "engine.backend_pi._load_config",
        lambda: {"r1": {"provider": "github-copilot",
                        "model": "claude-sonnet-4.6", "thinking": "max",
                        "fallback": True}},
    )
    monkeypatch.setattr("engine.fallback_cache.read_cache",
                        lambda path: {"active": True,
                                      "fallback_provider": "stale-fb",
                                      "fallback_model": "stale-model"})
    monkeypatch.setattr("engine.openai_codex_quota._cache_active",
                        lambda cache: True)
    meta = _resolve("r1")
    assert meta.provider == "github-copilot"
    assert meta.model == "claude-sonnet-4.6"


def test_resolve_pi_fallback_ignored_no_fallback_flag(monkeypatch):
    """Active fallback cache is ignored when config has fallback=false."""
    _patch_backend(monkeypatch, "pi")
    monkeypatch.setattr(
        "engine.backend_pi._load_config",
        lambda: {"r2": {"provider": "openai-codex",
                        "model": "gpt-5.4", "thinking": "medium",
                        "fallback": False}},
    )
    monkeypatch.setattr("engine.fallback_cache.read_cache",
                        lambda path: {"active": True,
                                      "fallback_provider": "stale-fb",
                                      "fallback_model": "stale-model"})
    monkeypatch.setattr("engine.openai_codex_quota._cache_active",
                        lambda cache: True)
    meta = _resolve("r2")
    assert meta.provider == "openai-codex"
    assert meta.model == "gpt-5.4"
    assert meta.think_level == "medium"


def test_snapshot_idempotent_updates_captured_at(monkeypatch):
    _patch_backend(monkeypatch, "openclaw")
    data: dict = {}
    m1 = snapshot(data, "agent1")
    m2 = snapshot(data, "agent1")
    assert "reviewer_meta" in data
    assert "agent1" in data["reviewer_meta"]
    assert m1.backend == m2.backend == "openclaw"
    # captured_at always refreshed
    assert data["reviewer_meta"]["agent1"]["captured_at"] == m2.captured_at


def test_snapshot_does_not_raise(monkeypatch):
    def boom(agent_id, **kw):
        raise RuntimeError("nope")
    monkeypatch.setattr("engine.backend.resolve_backend", boom)
    data: dict = {}
    meta = snapshot(data, "x")
    assert meta.backend == ""
    assert data["reviewer_meta"]["x"]["backend"] == ""


def test_lookup_returns_agent_meta(monkeypatch):
    _patch_backend(monkeypatch, "openclaw")
    data: dict = {}
    snapshot(data, "a")
    out = lookup(data, "a")
    assert isinstance(out, AgentMeta)
    assert out.backend == "openclaw"
    assert out.provider == "openclaw"


def test_lookup_missing_returns_none():
    assert lookup({}, "missing") is None
    assert lookup({"reviewer_meta": {}}, "missing") is None


def test_lookup_malformed_returns_none():
    data = {"reviewer_meta": {"a": {"unexpected": "field"}}}
    assert lookup(data, "a") is None
    data2 = {"reviewer_meta": {"a": {"backend": "pi", "provider": None,
                                     "model": None, "think_level": "",
                                     "captured_at": "now", "extra": 1}}}
    assert lookup(data2, "a") is None


def test_lookup_roundtrip(monkeypatch):
    _patch_backend(monkeypatch, "cc")
    monkeypatch.setattr(
        "engine.backend_cc._load_config",
        lambda: {"r": {"model": "m1", "effort": "low"}},
    )
    data: dict = {}
    written = snapshot(data, "r")
    out = lookup(data, "r")
    assert out == written


def test_resolve_integration_pi_hermetic(monkeypatch, tmp_path):
    """Hermetic integration: no real config_pi.json access."""
    _patch_backend(monkeypatch, "pi")
    monkeypatch.setattr(
        "engine.backend_pi._load_config",
        lambda: {"kaneko": {"provider": "github-copilot",
                            "model": "claude-sonnet-4.6", "thinking": "max"}},
    )
    monkeypatch.setattr("engine.openai_codex_quota._cache_path",
                        lambda agent_id: tmp_path / f"{agent_id}.json")
    meta = agent_meta._resolve("kaneko")
    assert meta == AgentMeta(backend="pi", provider="github-copilot",
                             model="claude-sonnet-4.6", think_level="max",
                             captured_at=meta.captured_at)
