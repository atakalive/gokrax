"""engine/backend.py - backend dispatch layer for agent communication.

Thin router that delegates to the selected backend (openclaw, pi, cc, gemini,
or kimi). Backend is resolved per-agent: AGENT_BACKEND_OVERRIDE[agent_id]
takes precedence over DEFAULT_AGENT_BACKEND.  Backend-specific state (e.g.
pi/cc starting markers) lives in the backend module, not here.
"""

from __future__ import annotations

import config
from engine.backend_pi import SUPPORTED_BACKENDS
from engine.backend_types import SendResult
from engine.shared import log


def resolve_backend(agent_id: str, *, ignore_fallback: bool = False) -> str:
    """Resolve backend for the given agent.

    Override > default > quota fallback (cache file read only; gemini/agy/kimi).
    Raises ValueError if the resolved backend is not in SUPPORTED_BACKENDS.

    If ``ignore_fallback`` is True, skip all quota fallback checks
    and return the configured backend as-is. Used by reset paths that need
    to operate on the configured backend directly (and additionally on the
    active fallback target when applicable).
    """
    backend = config.AGENT_BACKEND_OVERRIDE.get(agent_id, config.DEFAULT_AGENT_BACKEND)
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"Unsupported backend={backend!r} for agent={agent_id!r}. "
            f"Supported values: {sorted(SUPPORTED_BACKENDS)}"
        )
    if not ignore_fallback and backend == "gemini":
        from engine.gemini_quota import resolve_fallback
        fb = resolve_fallback(agent_id)
        if fb in SUPPORTED_BACKENDS and fb != "gemini":
            return fb
    if not ignore_fallback and backend == "agy":
        from engine.agy_quota import resolve_fallback
        fb = resolve_fallback(agent_id)
        if fb in SUPPORTED_BACKENDS and fb != "agy":
            return fb
    if not ignore_fallback and backend == "kimi":
        from engine.kimi_quota import resolve_fallback
        fb = resolve_fallback(agent_id)
        if fb in SUPPORTED_BACKENDS and fb != "kimi":
            return fb
    if not ignore_fallback and backend == "pi":
        from engine.openai_codex_quota import resolve_fallback_backend
        fb = resolve_fallback_backend(agent_id)
        if fb in SUPPORTED_BACKENDS and fb != "pi":
            return fb
    return backend


def validate_overrides() -> list[str]:
    """Warn about AGENT_BACKEND_OVERRIDE keys not found in config.AGENTS.

    Returns list of unknown agent names (for testability).
    Called at watchdog startup or on demand.
    """
    unknown = [
        agent_id for agent_id in config.AGENT_BACKEND_OVERRIDE
        if agent_id not in config.AGENTS
    ]
    for name in unknown:
        log(f"WARNING: AGENT_BACKEND_OVERRIDE contains unknown agent '{name}' (not in AGENTS)")
    return unknown


def send(agent_id: str, message: str, timeout: int) -> SendResult:
    """Dispatch send to the selected backend."""
    backend = resolve_backend(agent_id)
    if backend == "gemini":
        from engine.gemini_quota import should_fallback
        active, fallback_to, _new_period = should_fallback(agent_id)
        if active and fallback_to in SUPPORTED_BACKENDS and fallback_to != "gemini":
            backend = fallback_to
    if backend == "agy":
        from engine.agy_quota import should_fallback as agy_should_fallback
        active, fallback_to, _new_period = agy_should_fallback(agent_id)
        if active and fallback_to in SUPPORTED_BACKENDS and fallback_to != "agy":
            backend = fallback_to
    if backend == "kimi":
        # Guard: only evaluate kimi quota fallback when kimi IS the agent's
        # configured backend. When kimi is itself a fallback target (e.g. agy→kimi),
        # skip — prevents transitive fallback (agy→kimi→pi) and state view
        # inconsistency between send() and resolve_backend/ping/reset_session.
        if resolve_backend(agent_id, ignore_fallback=True) == "kimi":
            from engine.kimi_quota import should_fallback as kimi_should_fallback
            active, fallback_to, _new_period = kimi_should_fallback(agent_id)
            if active and fallback_to in SUPPORTED_BACKENDS and fallback_to != "kimi":
                backend = fallback_to
    if backend == "pi":
        # Mode B (backend redirect): only when pi IS the agent's configured
        # backend (not a fallback target). Prevents transitive fallback
        # (e.g. agy→pi→cc) and state view inconsistency. Same pattern as the
        # kimi guard above.
        if resolve_backend(agent_id, ignore_fallback=True) == "pi":
            from engine.backend_pi import _load_config as _load_pi_cfg
            _profile = _load_pi_cfg().get(agent_id, {})
            from engine.openai_codex_quota import is_mode_b_backend
            if is_mode_b_backend(_profile.get("fallback_backend", "")):
                from engine.openai_codex_quota import should_fallback as codex_should_fallback
                active, fb_target, _, _new = codex_should_fallback(agent_id)
                if active and fb_target in SUPPORTED_BACKENDS and fb_target != "pi":
                    backend = fb_target
    if backend == "pi":
        from engine.backend_pi import send as pi_send
        return pi_send(agent_id, message, timeout)
    elif backend == "cc":
        from engine.backend_cc import send as cc_send
        return cc_send(agent_id, message, timeout)
    elif backend == "cci":
        from engine.backend_cci import send as cci_send
        return cci_send(agent_id, message, timeout)
    elif backend == "gemini":
        from engine.backend_gemini import send as gm_send
        return gm_send(agent_id, message, timeout)
    elif backend == "kimi":
        from engine.backend_kimi import send as km_send
        return km_send(agent_id, message, timeout)
    elif backend == "agy":
        from engine.backend_agy import send as agy_send
        return agy_send(agent_id, message, timeout)
    # openclaw: delegate to the openclaw-specific implementation
    from engine.backend_openclaw import send as oc_send
    return oc_send(agent_id, message, timeout)


def ping(agent_id: str, timeout: int) -> bool:
    """Dispatch ping to the selected backend."""
    backend = resolve_backend(agent_id)
    if backend == "pi":
        from engine.backend_pi import ping as pi_ping
        return pi_ping(agent_id, timeout)
    elif backend == "cc":
        from engine.backend_cc import ping as cc_ping
        return cc_ping(agent_id, timeout)
    elif backend == "cci":
        from engine.backend_cci import ping as cci_ping
        return cci_ping(agent_id, timeout)
    elif backend == "gemini":
        from engine.backend_gemini import ping as gm_ping
        return gm_ping(agent_id, timeout)
    elif backend == "kimi":
        from engine.backend_kimi import ping as km_ping
        return km_ping(agent_id, timeout)
    elif backend == "agy":
        from engine.backend_agy import ping as agy_ping
        return agy_ping(agent_id, timeout)
    from engine.backend_openclaw import ping as oc_ping
    return oc_ping(agent_id, timeout)


def is_inactive(agent_id: str, pipeline_data: dict | None = None) -> bool:
    """Dispatch is_inactive to the selected backend.

    For all backends, if pipeline_data indicates CC is running, the agent
    is considered active.  The cc_pid check lives in engine.shared (via
    _is_cc_running) and is computed here before delegating to the backend.
    """
    backend = resolve_backend(agent_id)

    # Compute cc_running once (shared across backends)
    from engine.shared import _is_cc_running
    cc_running = (pipeline_data is not None and _is_cc_running(pipeline_data))

    if backend == "pi":
        from engine.backend_pi import is_inactive as pi_is_inactive
        return pi_is_inactive(agent_id, pipeline_data, cc_running=cc_running)
    elif backend == "cc":
        from engine.backend_cc import is_inactive as cc_is_inactive
        return cc_is_inactive(agent_id, pipeline_data, cc_running=cc_running)
    elif backend == "cci":
        from engine.backend_cci import is_inactive as cci_is_inactive
        return cci_is_inactive(agent_id, pipeline_data, cc_running=cc_running)
    elif backend == "gemini":
        from engine.backend_gemini import is_inactive as gm_is_inactive
        return gm_is_inactive(agent_id, pipeline_data, cc_running=cc_running)
    elif backend == "kimi":
        from engine.backend_kimi import is_inactive as km_is_inactive
        return km_is_inactive(agent_id, pipeline_data, cc_running=cc_running)
    elif backend == "agy":
        from engine.backend_agy import is_inactive as agy_is_inactive
        return agy_is_inactive(agent_id, pipeline_data, cc_running=cc_running)

    # openclaw: preserve original semantics
    if cc_running:
        return False
    from engine.shared import _is_agent_inactive_openclaw
    return _is_agent_inactive_openclaw(agent_id)


def soft_reap(agent_id: str) -> None:
    """Context-preserving process reap. No-op for backends without a
    one-live-process model (e.g. openclaw gateway queue)."""
    configured = resolve_backend(agent_id, ignore_fallback=True)
    if configured == "agy":
        from engine.backend_agy import soft_reap as agy_soft_reap
        agy_soft_reap(agent_id)
    elif configured == "cci":
        from engine.backend_cci import soft_reap as cci_soft_reap
        cci_soft_reap(agent_id)


def reset_session(agent_id: str) -> None:
    """Dispatch reset_session to the selected backend.

    Resets both the configured backend and any active quota fallback target
    (gemini, agy, or kimi). This ensures that when an agent has a fallback
    active, both the primary session and the fallback session are cleared —
    otherwise stale context could resume on either side when the fallback
    expires or a phase begins.

    For openclaw, this is a no-op (session reset is done via /new message).
    For pi, this is best-effort: deletes the session file and clears the
    starting marker.  Does not terminate processes or wait for quiescence.
    A bounded false-active window of up to INACTIVE_THRESHOLD_SEC may occur
    if an old process recreates the file after reset.
    See backend_pi.reset_session docstring and #246 for design rationale.
    """
    configured = resolve_backend(agent_id, ignore_fallback=True)

    if configured == "pi":
        from engine.backend_pi import reset_session as pi_reset
        pi_reset(agent_id)
    elif configured == "cc":
        from engine.backend_cc import reset_session as cc_reset
        cc_reset(agent_id)
    elif configured == "cci":
        from engine.backend_cci import reset_session as cci_reset
        cci_reset(agent_id)
    elif configured == "gemini":
        from engine.backend_gemini import reset_session as gm_reset
        gm_reset(agent_id)
    elif configured == "kimi":
        from engine.backend_kimi import reset_session as km_reset
        km_reset(agent_id)
    elif configured == "agy":
        from engine.backend_agy import reset_session as agy_reset
        agy_reset(agent_id)
    # openclaw: no-op (session reset is done via /new message)

    if configured == "gemini":
        from engine.gemini_quota import resolve_fallback
        fb = resolve_fallback(agent_id)
        if fb in SUPPORTED_BACKENDS and fb != configured:
            if fb == "pi":
                from engine.backend_pi import reset_session as pi_reset
                pi_reset(agent_id)
            elif fb == "cc":
                from engine.backend_cc import reset_session as cc_reset
                cc_reset(agent_id)

    if configured == "agy":
        from engine.agy_quota import resolve_fallback as agy_resolve_fallback
        fb = agy_resolve_fallback(agent_id)
        if fb in SUPPORTED_BACKENDS and fb != configured:
            if fb == "pi":
                from engine.backend_pi import reset_session as pi_reset
                pi_reset(agent_id)
            elif fb == "cc":
                from engine.backend_cc import reset_session as cc_reset
                cc_reset(agent_id)
            elif fb == "kimi":
                from engine.backend_kimi import reset_session as km_reset
                km_reset(agent_id)
            # openclaw: no-op (session reset via /new)

    if configured == "kimi":
        from engine.kimi_quota import resolve_fallback as kimi_resolve_fallback
        fb = kimi_resolve_fallback(agent_id)
        if fb in SUPPORTED_BACKENDS and fb != configured:
            if fb == "pi":
                from engine.backend_pi import reset_session as pi_reset
                pi_reset(agent_id)
            elif fb == "cc":
                from engine.backend_cc import reset_session as cc_reset
                cc_reset(agent_id)
            # openclaw: no-op (session reset via /new)

    if configured == "pi":
        from engine.openai_codex_quota import resolve_fallback_backend
        fb = resolve_fallback_backend(agent_id)
        if fb in SUPPORTED_BACKENDS and fb != configured:
            if fb == "cc":
                from engine.backend_cc import reset_session as cc_reset
                cc_reset(agent_id)
            # openclaw: no-op (session reset via /new)
