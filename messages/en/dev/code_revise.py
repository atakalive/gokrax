"""Prompts and reminders for the CODE_REVISE state.

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
    """Code revise phase instruction message (watchdog.py get_notification_for_state CODE_REVISE)."""
    repo_line = f"Repository: {repo_path}\n" if repo_path else ""

    return (
        f"[gokrax] {project}: code revise phase\n"
        f"{comment_line}"
        f"Target Issues: {issues_str}\n"
        f"{repo_line}"
        f"{p2_note}"
        f"[Steps]\n"
        f"1. Read {fix_label} and fix the code\n"
        f"   View findings: `{GOKRAX_CLI} get-comments --pj {project} --issue N`\n"
        f"2. git commit\n"
        f"3. Report completion to gokrax:\n"
        f"   {GOKRAX_CLI} code-revise --pj {project} --issue N [N...] --hash <commit> --summary \"<brief summary of changes>\"\n\n"
        f"--summary: Ensure each entry is on a new line. \n"
        f"If multiple reviewers raise the same P2/Suggestion, the finding is likely correct — fix it.\n"
        f"Do not forget to include --hash <commit> when submitting.\n"
        f"If reviewer findings conflict with design decisions, create a new Issue to discuss the design decision.\n"
        f"Note: If you are confident that a P0/P1 finding is incorrect, you can file a dispute before completing the revise:\n"
        f"{GOKRAX_CLI} dispute --pj {project} --issue N --reviewer REVIEWER --reason \"reason\"\n"
        f"State the reason in detail, at a granularity a child could understand. If the dispute is accepted, the relevant P0/P1 will be withdrawn.\n"
        f"[Request] Complete the work without interruption."
    )


def nudge(**_kw) -> str:
    """CODE_REVISE reminder."""
    return (
        "[Remind] Proceed with and complete the scheduled revise work.\n"
        "Report the fix commit with gokrax code-revise --pj <project> --issue <N> --hash <commit>."
    )


# ---------------------------------------------------------------------------
# Discord notifications (short)
# Shared with design_revise.notify_revise_summary.
# ---------------------------------------------------------------------------
