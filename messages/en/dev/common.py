"""Common Discord notification templates for normal mode.

Defines notification messages not specific to any state.
"""


def notify_state_transition(project: str, old_state: str, new_state: str, ts: str, q_prefix: str = "", **_kw) -> str:
    """State transition Discord notification."""
    return f"{q_prefix}[{project}] {old_state} → {new_state} ({ts})"


def notify_nudge_implementer(project: str, state: str, implementer: str, ts: str, q_prefix: str = "", **_kw) -> str:
    """Implementer nudge Discord notification."""
    return f"{q_prefix}[{project}] {state}: nudging implementer {implementer} ({ts})"
