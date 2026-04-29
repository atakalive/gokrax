"""Prompts and reminders for the DESIGN_REVIEW state.

Review request message generation is handled by notify.py's format_review_request() for data formatting.
Only prompt templates (text parts) are defined here.

Variables (review_request):
    project: str           - Project name
    todo_header: str       - TODO checklist (pre-assembled)
    guidance: str          - Review guidelines text
    body: str              - Issue body + previous findings + disputes etc. (pre-assembled)
    completion: str        - Completion command list (pre-assembled)
    comment_line: str      - Owner comment line
    phase_note: str        - Phase note
"""


def review_request(
    project: str,
    todo_header: str,
    guidance: str,
    body: str,
    completion: str,
    comment_line: str,
    phase_note: str,
    repo_line: str = "",
    **_kw,
) -> str:
    """Design review request message (notify.py format_review_request assembly part).

    skill_block insertion is done by the caller.
    """
    return (
        f"[gokrax] {project}: design review request{comment_line}{phase_note}\n"
        f"{repo_line}"
        f"\n{todo_header}\n\n{guidance}\n\n{body}{completion}"
    )


def file_review_request(
    project: str,
    n: int,
    file_path: str,
    cmds_block: str,
    **_kw,
) -> str:
    """Design review request message for file externalization (notify.py _build_file_review_message).

    skill_block insertion is done by the caller.
    """
    return (
        f"[gokrax] {project}: design review request ({n} issues)\n\n"
        f"Read the review data file and review all items.\n\n"
        f"Read {file_path}\n\n"
        f"After completion, run the following for each Issue:\n"
        f"{cmds_block}\n"
        f"(Repeat the above commands for each Issue)\n\n"
        f"⚠️ Task is not complete until all Issue review commands are executed. Do not stop midway."
    )


def phase_note(**_kw) -> str:
    """Design review phase note."""
    return "\n⚠️ This is a design review (DESIGN_REVIEW). No code or diff exists yet.\n"
def guidance_design(**_kw) -> str:
    """Design review guidelines text."""
    return (
        "Review guidelines:\n"
        "- Is it mathematically precise (verify rigorously)\n"
        "- Is the spec in the Issue body clear and implementable\n"
        "- Are there any contradictions, edge cases, or unexpected pitfalls"
        "\n\nVerdict selection:\n"
        "- If you have any P0/P1/P2 findings, set the verdict to the most severe one\n"
        "- Use APPROVE only when you have zero findings\n"
        "- \"APPROVE with P2 in summary\" is not allowed. If you have P2 findings, use --verdict P2"
    )


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def nudge_review(
    project: str,
    issues_display: str,
    cmd_lines: str,
    **_kw,
) -> str:
    """Review reminder message (watchdog.py review reminder block).

    issues_display is pre-assembled by the caller (e.g. "#1, #2").
    """
    return (
        f"[Remind] {project} review is incomplete. Target: {issues_display}\n"
        f"Submit review for each Issue with the following commands:\n"
        f"{cmd_lines}\n"
        f"Note: Execute the command via the bash tool. Plain-text suggestions are not recorded and reminders will keep coming."
    )


def notify_nudge_reviewers(project: str, reviewers: str, q_prefix: str = "", **_kw) -> str:
    """Reviewer nudge Discord notification. reviewers is pre-assembled."""
    return f"{q_prefix}[{project}] nudging reviewers: {reviewers}"


def nudge_dispute(
    project: str,
    dispute_lines: str,
    **_kw,
) -> str:
    """Dispute response reminder message (watchdog.py dispute reminder block)."""
    return (
        f"[Dispute — Response Required]\n"
        f"A dispute has been filed against your verdict in {project}.\n"
        f"Re-evaluate and submit your verdict with --force:\n\n"
        f"{dispute_lines}\n"
        f"Note: Execute the command via the bash tool with --force. Do not assume a previous submission was already recorded."
    )
