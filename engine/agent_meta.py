"""engine/agent_meta.py - Reviewer agent provider/model/think_level snapshot.

Captures the agent's actual provider/model/think_level at the time a review is
dispatched, so retrospective metrics aren't disturbed by config edits or quota
fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from pipeline_io import now_iso


@dataclass(frozen=True)
class AgentMeta:
    backend: str
    provider: str | None
    model: str | None
    think_level: str
    captured_at: str


def _resolve(agent_id: str) -> AgentMeta:
    from engine.backend import resolve_backend

    try:
        backend = resolve_backend(agent_id)
    except Exception:
        return AgentMeta(backend="", provider=None, model=None,
                         think_level="", captured_at=now_iso())

    provider: str | None = None
    model: str | None = None
    think_level: str = ""

    try:
        if backend == "pi":
            from engine.backend_pi import _load_config as _load_pi_config
            entry = _load_pi_config().get(agent_id, {})
            provider = entry.get("provider")
            model = entry.get("model")
            think_level = entry.get("thinking", "") or ""
        elif backend == "cc":
            from engine.backend_cc import _load_config as _load_cc_config
            entry = _load_cc_config().get(agent_id, {})
            # Fixed "anthropic" today; change to a config lookup if Bedrock etc. is added.
            provider = "anthropic"
            model = entry.get("model")
            think_level = entry.get("effort", "") or ""
        elif backend == "gemini":
            from engine.backend_gemini import _load_config as _load_gemini_config
            entry = _load_gemini_config().get(agent_id, {})
            # Fixed "google" today; change to a config lookup if alternative routes are added.
            provider = "google"
            model = entry.get("model")
            think_level = ""
        elif backend == "kimi":
            from engine.backend_kimi import _load_config as _load_kimi_config
            entry = _load_kimi_config().get(agent_id, {})
            provider = "kimi-coder"
            model = entry.get("model")
            think_level = ""
        elif backend == "openclaw":
            provider = "openclaw"
            model = None
            think_level = ""
    except Exception:
        return AgentMeta(backend=backend, provider=None, model=None,
                         think_level="", captured_at=now_iso())

    if backend == "pi":
        try:
            from engine.openai_codex_quota import _cache_path, _cache_active
            from engine import fallback_cache
            cache = fallback_cache.read_cache(_cache_path(agent_id))
            if _cache_active(cache):
                provider = cache["fallback_provider"]
                model = cache["fallback_model"]
        except Exception:
            pass

    return AgentMeta(backend=backend, provider=provider, model=model,
                     think_level=think_level, captured_at=now_iso())


def snapshot(data: dict, agent_id: str) -> AgentMeta:
    """Resolve current meta and write snapshot into pipeline data."""
    meta = _resolve(agent_id)
    data.setdefault("reviewer_meta", {})[agent_id] = asdict(meta)
    return meta


def lookup(data: dict, agent_id: str) -> AgentMeta | None:
    """Read snapshotted meta from pipeline data."""
    entry = data.get("reviewer_meta", {}).get(agent_id)
    if entry is None:
        return None
    try:
        return AgentMeta(**entry)
    except (TypeError, KeyError):
        return None
