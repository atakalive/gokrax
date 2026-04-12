"""Prompts and notifications for the BLOCKED state.

Variables:
    state: str              - Current state name (e.g. "DESIGN_REVIEW")
    severity: str           - Severity ("P0" or "P1")
    MAX_REVISE_CYCLES: int  - Maximum revise cycle count
    OWNER_NAME: str         - Owner name
"""


def _phase_label(state: str) -> str:
    """Derive phase label from state name."""
    return "design" if "DESIGN" in state else "code"


def blocked_max_cycles(
    state: str,
    MAX_REVISE_CYCLES: int,
    OWNER_NAME: str,
    severity: str = "P0",
    **_kw,
) -> str:
    """BLOCKED message when P0/P1 remain + max cycles exceeded (_resolve_review_outcome)."""
    phase = _phase_label(state)
    return (
        f"{phase} review cycle has reached the limit ({MAX_REVISE_CYCLES} cycles).\n"
        f"{severity} findings remain unresolved. Address manually."
    )


def blocked_timeout(state: str, **_kw) -> str:
    """BLOCKED message on timeout (_check_nudge)."""
    return f"{state} timed out. No response received."


def blocked_cc_no_commit(project: str, **_kw) -> str:
    """BLOCKED message when CC did not create a commit (in run_cc script)."""
    return (
        f"[{project}] ❌ CC did not create a commit (after 2 retries) → BLOCKED"
    )


# ---------------------------------------------------------------------------
# Discord notifications (short)
# ---------------------------------------------------------------------------

def notify_recovery_merge_summary(project: str, **_kw) -> str:
    """Recovery warning for merge_summary notification."""
    return f"[{project}] ⚠️ merge_summary notification was interrupted. Check manually."


def notify_recovery_cc(project: str, **_kw) -> str:
    """Recovery warning for CC startup."""
    return f"[{project}] ⚠️ CC startup was interrupted. Check manually."


def blocked_prompt_report(
    project: str,
    state: str,
    impl_msg: str,
    GOKRAX_CLI: str,
    **_kw,
) -> str:
    """Prompt sent to implementer on BLOCKED, requesting a situation report."""
    reason = impl_msg or "(no reason provided)"
    return (
        f"Transitioned to BLOCKED from {state}.\n"
        f"Reason: {reason}\n\n"
        f"Report your situation: what happened and what is needed.\n"
        f"{GOKRAX_CLI} blocked-report --pj {project} --summary \"<description>\""
    )
