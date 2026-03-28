"""Prompts and reminders for the DESIGN_REVISE state.

Variables:
    project: str       - Project name
    issues_str: str    - Target Issue numbers string
    comment_line: str  - Owner comment line
    fix_label: str     - Fix target label ("P0/P1 findings" or "P0/P1/P2 findings")
    p2_note: str       - p2_fix mode note (empty string or note text)
    GOKRAX_CLI: str    - gokrax CLI path
"""


def transition(
    project: str,
    issues_str: str,
    comment_line: str,
    fix_label: str,
    p2_note: str,
    GOKRAX_CLI: str,
    repo_path: str = "",
    **_kw,
) -> str:
    """Design revise phase instruction message (watchdog.py get_notification_for_state DESIGN_REVISE)."""
    repo_line = f"Repository: {repo_path}\n" if repo_path else ""

    return (
        f"[gokrax] {project}: design revise phase\n"
        f"{comment_line}"
        f"Target Issues: {issues_str}\n"
        f"{repo_line}"
        f"{p2_note}"
        f"[Steps]\n"
        f"1. Read {fix_label} and update the Issue body (glab issue update)\n"
        f"   View findings: `glab issue view N --comments --per-page 100`\n"
        f"2. Report completion to gokrax:\n"
        f"   {GOKRAX_CLI} design-revise --pj {project} --issue N [N...] --summary \"<brief summary of changes>\"\n\n"
        f"If multiple reviewers raise the same P2/Suggestion, the finding is likely correct — fix it.\n"
        f"If reviewer findings conflict with design decisions, create a new Issue to discuss the design decision.\n"
        f"Note: If you are confident that a P0/P1 finding is incorrect, you can file a dispute before completing the revise:\n"
        f"{GOKRAX_CLI} dispute --pj {project} --issue N --reviewer REVIEWER --reason \"reason\"\n"
        f"State the reason in detail, at a granularity a child could understand. If the dispute is accepted, the relevant P0/P1 will be withdrawn.\n"
        f"[Request] Complete the work without interruption."
    )


def nudge(**_kw) -> str:
    """DESIGN_REVISE reminder."""
    return (
        "[Remind] Proceed with and complete the scheduled revise work.\n"
        "Report completion with gokrax design-revise --pj <project> --issue <N>."
    )


# ---------------------------------------------------------------------------
# Discord notifications (short)
# ---------------------------------------------------------------------------

def notify_revise_summary(project: str, revise_lines: str, q_prefix: str = "", **_kw) -> str:
    """P0 summary notification on REVISE transition. revise_lines is pre-assembled."""
    return f"{q_prefix}[{project}] REVISE target:\n{revise_lines}"
