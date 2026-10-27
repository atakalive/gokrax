"""engine/agent_meta.py - Reviewer agent provider/model/think_level snapshot.

Captures the agent's actual provider/model/think_level at the time a review is
dispatched, so retrospective metrics aren't disturbed by config edits or quota
fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from engine.boost import boost_applies, overlay_profile
from pipeline_io import find_issue, now_iso


@dataclass(frozen=True)
class AgentMeta:
    backend: str
    provider: str | None
    model: str | None
    think_level: str
    captured_at: str
    # Issue #400: whether config_<backend>.json "boost" was overlaid on the send.
    boosted: bool = False


def _resolve(agent_id: str, boost: bool = False) -> AgentMeta:
    from engine.backend import resolve_backend

    try:
        backend = resolve_backend(agent_id)
    except Exception:
        return AgentMeta(backend="", provider=None, model=None,
                         think_level="", captured_at=now_iso())

    provider: str | None = None
    model: str | None = None
    think_level: str = ""
    boosted: bool = False

    try:
        if backend == "pi":
            from engine.backend_pi import _load_config as _load_pi_config
            entry = _load_pi_config().get(agent_id, {})
            if boost and boost_applies(entry, "pi"):
                entry = overlay_profile(entry, "pi")
                boosted = True
            provider = entry.get("provider")
            model = entry.get("model")
            think_level = entry.get("thinking", "") or ""
        elif backend == "cc":
            from engine.backend_cc import _load_config as _load_cc_config
            entry = _load_cc_config().get(agent_id, {})
            if boost and boost_applies(entry, "cc"):
                entry = overlay_profile(entry, "cc")
                boosted = True
            # Fixed "anthropic" today; change to a config lookup if Bedrock etc. is added.
            provider = "anthropic"
            model = entry.get("model")
            think_level = entry.get("effort", "") or ""
        elif backend == "cci":
            from engine.backend_cci import _load_config as _load_cci_config
            entry = _load_cci_config().get(agent_id, {})
            if boost and boost_applies(entry, "cci"):
                entry = overlay_profile(entry, "cci")
                boosted = True
            provider = "anthropic"
            model = entry.get("model")
            think_level = entry.get("effort", "") or ""
        elif backend == "gemini":
            from engine.backend_gemini import _load_config as _load_gemini_config
            entry = _load_gemini_config().get(agent_id, {})
            if boost and boost_applies(entry, "gemini"):
                entry = overlay_profile(entry, "gemini")
                boosted = True
            # Fixed "google" today; change to a config lookup if alternative routes are added.
            provider = "google"
            model = entry.get("model")
            think_level = ""
        elif backend == "kimi":
            from engine.backend_kimi import _load_config as _load_kimi_config
            entry = _load_kimi_config().get(agent_id, {})
            if boost and boost_applies(entry, "kimi"):
                entry = overlay_profile(entry, "kimi")
                boosted = True
            provider = "kimi-coder"
            model = entry.get("model")
            think_level = ""
        elif backend == "agy":
            from engine.backend_agy import _load_config as _load_agy_config
            entry = _load_agy_config().get(agent_id, {})
            if boost and boost_applies(entry, "agy"):
                entry = overlay_profile(entry, "agy")
                boosted = True
            provider = "google"
            model = entry.get("model", "")
            think_level = ""
        elif backend == "codex":
            from engine.backend_codex import _load_config as _load_codex_config
            from engine.backend_codex import _valid_effort, _valid_model
            entry = _load_codex_config().get(agent_id, {})
            if boost and boost_applies(entry, "codex"):
                entry = overlay_profile(entry, "codex")
                boosted = True
            provider = "openai-codex"
            model = _valid_model(entry)
            think_level = _valid_effort(entry) or ""
        elif backend == "openclaw":
            provider = "openclaw"
            model = None
            think_level = ""
    except Exception:
        return AgentMeta(backend=backend, provider=None, model=None,
                         think_level="", captured_at=now_iso())

    if (backend == "pi"
            and provider == "openai-codex"
            and entry.get("fallback")):
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
                     think_level=think_level, captured_at=now_iso(),
                     boosted=boosted)


def snapshot(data: dict, agent_id: str, boost: bool = False) -> AgentMeta:
    """Resolve current meta and write snapshot into pipeline data."""
    meta = _resolve(agent_id, boost=boost)
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


def record_boost_sent(data: dict, agent_id: str, phase: str,
                      issue_nums: list[int], boosted: bool) -> None:
    """Record the boost result of a successful send for each issue it included.

    Writes ``issue["boost_sent"][phase][agent_id] = bool(boosted)``.
    No-op when the batch is not a boost batch (``data["boost"]`` falsy) or
    when ``batch`` is not a list. Issue numbers not in the batch are skipped.
    """
    if not data.get("boost"):
        return
    batch = data.get("batch")
    if not isinstance(batch, list):
        return
    for num in issue_nums:
        issue = find_issue(batch, num)
        if issue is None:
            continue
        sent = issue.get("boost_sent")
        if not isinstance(sent, dict):
            sent = {}
            issue["boost_sent"] = sent
        per_phase = sent.get(phase)
        if not isinstance(per_phase, dict):
            per_phase = {}
            sent[phase] = per_phase
        per_phase[agent_id] = bool(boosted)


def was_boosted_for(data: dict, issue_num: int, phase: str, agent_id: str) -> bool:
    """True only when ``issue["boost_sent"][phase][agent_id] is True``."""
    batch = data.get("batch")
    if not isinstance(batch, list):
        return False
    issue = find_issue(batch, issue_num)
    if not isinstance(issue, dict):
        return False
    sent = issue.get("boost_sent")
    if not isinstance(sent, dict):
        return False
    per_phase = sent.get(phase)
    if not isinstance(per_phase, dict):
        return False
    return per_phase.get(agent_id) is True
