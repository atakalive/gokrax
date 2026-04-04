"""Notifications for the SPEC_PAUSED state."""


def notify_paused(project: str, reason: str, **_kw) -> str:
    """Pipeline paused."""
    return f"[Spec][{project}] ⏸️ pipeline paused — {reason}"


def notify_failure(project: str, kind: str, detail: str = "", **_kw) -> str:
    """Failure notification (generic)."""
    suffix = f" — {detail}" if detail else ""
    return f"[Spec][{project}] ❌ {kind}{suffix}"
