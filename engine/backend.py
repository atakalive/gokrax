"""engine/backend.py - backend dispatch layer for agent communication.

Thin router that delegates to the selected backend (openclaw or pi).
Backend-specific state (e.g. pi starting markers) lives in the backend
module, not here.
"""

from __future__ import annotations

import config
from engine.backend_pi import SUPPORTED_BACKENDS


def _validate_backend() -> str:
    """Return current AGENT_BACKEND after validation."""
    backend = config.AGENT_BACKEND
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"Unsupported AGENT_BACKEND={backend!r}. "
            f"Supported values: {sorted(SUPPORTED_BACKENDS)}"
        )
    return backend


def send(agent_id: str, message: str, timeout: int) -> bool:
    """Dispatch send to the selected backend."""
    backend = _validate_backend()
    if backend == "pi":
        from engine.backend_pi import send as pi_send
        return pi_send(agent_id, message, timeout)
    # openclaw: delegate to the openclaw-specific implementation
    from notify import _send_to_agent_openclaw
    return _send_to_agent_openclaw(agent_id, message, timeout)


def ping(agent_id: str, timeout: int) -> bool:
    """Dispatch ping to the selected backend."""
    backend = _validate_backend()
    if backend == "pi":
        from engine.backend_pi import ping as pi_ping
        return pi_ping(agent_id, timeout)
    from notify import _ping_agent_openclaw
    return _ping_agent_openclaw(agent_id, timeout)


def is_inactive(agent_id: str, pipeline_data: dict | None = None) -> bool:
    """Dispatch is_inactive to the selected backend.

    For all backends, if pipeline_data indicates CC is running, the agent
    is considered active.  The cc_pid check lives in engine.shared (via
    _is_cc_running) and is computed here before delegating to the backend.
    """
    backend = _validate_backend()

    # Compute cc_running once (shared across backends)
    from engine.shared import _is_cc_running
    cc_running = (pipeline_data is not None and _is_cc_running(pipeline_data))

    if backend == "pi":
        from engine.backend_pi import is_inactive as pi_is_inactive
        return pi_is_inactive(agent_id, pipeline_data, cc_running=cc_running)

    # openclaw: preserve original semantics
    if cc_running:
        return False
    from engine.shared import _is_agent_inactive_openclaw
    return _is_agent_inactive_openclaw(agent_id)


def reset_session(agent_id: str) -> None:
    """Dispatch reset_session to the selected backend.

    For openclaw, this is a no-op (session reset is done via /new message).
    For pi, this deletes the session file and clears the starting marker.
    """
    backend = _validate_backend()
    if backend == "pi":
        from engine.backend_pi import reset_session as pi_reset
        pi_reset(agent_id)
