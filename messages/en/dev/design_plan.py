"""Prompts and reminders for the DESIGN_PLAN state.

Variables:
    project: str       - Project name
    issues_str: str    - Target Issue numbers string (e.g. "#1, #2")
    comment_line: str  - Owner comment line (empty string or "{OWNER_NAME}'s request: ...\n")
    GOKRAX_CLI: str    - gokrax CLI path
"""


def transition(
    project: str,
    issues_str: str,
    comment_line: str,
    GOKRAX_CLI: str,
    repo_path: str = "",
    **_kw,
) -> str:
    """Design plan phase instruction message (watchdog.py get_notification_for_state DESIGN_PLAN)."""
    repo_line = f"Repository: {repo_path}\n" if repo_path else ""

    return (
        f"[gokrax] {project}: design plan phase\n"
        f"{comment_line}"
        f"Target Issues: {issues_str}\n"
        f"{repo_line}"
        f"**Update the target Issue descriptions** to a granularity that Claude Code can reliably implement. Do not supplement via comments.\n"
        f"Write the revised body to /tmp/gokrax-{project}-N.md and apply it via:\n"
        f"  {GOKRAX_CLI} issue-update --pj {project} --issue N --body-file /tmp/gokrax-{project}-N.md\n"
        f"After all updates, run plan-done to complete (batch reporting supported).\n"
        f"  {GOKRAX_CLI} plan-done --project {project} --issue N [N...]\n"
        f"[Request] Complete the work without interruption."
    )


def nudge(
    **_kw,
) -> str:
    """DESIGN_PLAN reminder."""
    return (
        "[Remind] Proceed with and complete the design plan.\n"
        "Report completion with gokrax plan-done --project <project> --issue <N>."
    )


# ---------------------------------------------------------------------------
# Discord notifications (short)
# ---------------------------------------------------------------------------

def notify_issues(project: str, issue_lines: str, q_prefix: str = "", **_kw) -> str:
    """Batch start target Issue list notification. issue_lines is pre-assembled."""
    return f"{q_prefix}[{project}] Target Issues:\n{issue_lines}"
