"""Notifications for the SPEC_STALLED state."""


def notify_stalled(project: str, rev: str | int, remaining_p1_plus: int, **_kw) -> str:
    """MAX_CYCLES reached."""
    return f"[Spec] ⏸️ {project}: MAX_CYCLES reached, {remaining_p1_plus} P1+ findings remaining"
