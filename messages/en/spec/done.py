"""Notifications for the SPEC_DONE state."""


def notify_done(project: str, **_kw) -> str:
    """Spec mode completed."""
    return f"[Spec] ✅ {project}: spec mode completed"
