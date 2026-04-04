"""Notifications for the SPEC_APPROVED state.

No agent-facing prompts for this state (waiting for owner confirmation or auto-advancing).
"""


def notify_approved(project: str, rev: str | int, **_kw) -> str:
    """Normal approval (waiting for owner confirmation)."""
    return f"[Spec][{project}] spec approved (rev{rev}). Run `gokrax spec continue` to proceed to issue creation"


def notify_approved_auto(project: str, rev: str | int, **_kw) -> str:
    """Auto-advance."""
    return f"[Spec][{project}] spec approved (rev{rev}) — auto-advancing to issue creation"


def notify_approved_forced(project: str, rev: str | int, remaining_p1_plus: int, **_kw) -> str:
    """Force-approved."""
    return f"[Spec][{project}] ⚠️ force-approved ({remaining_p1_plus} P1+ findings remaining)"
