"""Prompts and reminders for the CODE_REVIEW state.

Variables (review_request):
    project: str           - Project name
    todo_header: str       - TODO checklist (pre-assembled)
    guidance: str          - Review guidelines text
    body: str              - Issue body + previous findings + diff + disputes etc. (pre-assembled)
    completion: str        - Completion command list (pre-assembled)
    comment_line: str      - Owner comment line
"""

__all__ = [
    "review_request",
    "file_review_request",
    "guidance_code",
    "nudge_review",
    "nudge_dispute",
    "notify_nudge_reviewers",
]


def review_request(
    project: str,
    todo_header: str,
    guidance: str,
    body: str,
    completion: str,
    comment_line: str,
    repo_line: str = "",
    **_kw,
) -> str:
    """Code review request message (notify.py format_review_request assembly part).

    skill_block insertion is done by the caller.
    """
    return (
        f"[gokrax] {project}: code review request{comment_line}\n"
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
    """Code review request message for file externalization (notify.py _build_file_review_message).

    skill_block insertion is done by the caller.
    """
    return (
        f"[gokrax] {project}: code review request ({n} issues)\n\n"
        f"Read the review data file and review all items.\n\n"
        f"Read {file_path}\n\n"
        f"After completion, run the following for each Issue:\n"
        f"{cmds_block}\n"
        f"(Repeat the above commands for each Issue)\n\n"
        f"⚠️ Task is not complete until all Issue review commands are executed. Do not stop midway."
    )


def guidance_code(**_kw) -> str:
    """Code review guidelines text."""
    return (
        "Review guidelines:\n"
        "- Is the implementation consistent with the design approved in the design review\n"
        "- Bugs, edge cases, missing type hints\n"
        "- Evaluate test validity\n\n"
        "Scope constraints:\n"
        "- When raising P0/P1, confirm the code in question is in the current diff\n"
        "- Do not attribute changes already merged in a previous batch to the current batch\n"
        "- Report issues found outside the diff as P2 (suggestion)\n\n"
        "Context constraints (important):\n"
        "- You can only see the diff and its surrounding context. You do not have visibility into the entire repository\n"
        "- Do not assert that files, functions, or variables not in the diff \"do not exist\"\n"
        "- Findings of \"X is not found\" must remain P2 (suggestion) and must not be P0/P1\n"
        "- When raising a P0/P1 finding that depends on code outside the diff, confirm the evidence explicitly exists within the diff"
        "\n\nVerdict selection:\n"
        "- If you have any P0/P1/P2 findings, set the verdict to the most severe one\n"
        "- Use APPROVE only when you have zero findings\n"
        "- \"APPROVE with P2 in summary\" is not allowed. If you have P2 findings, use --verdict P2"
    )


def notify_nudge_reviewers(project: str, reviewers: str, q_prefix: str = "", **_kw) -> str:
    """Reviewer nudge Discord notification. reviewers is pre-assembled."""
    return f"{q_prefix}[{project}] nudging reviewers: {reviewers}"


def nudge_review(
    project: str,
    issues_display: str,
    cmd_lines: str,
    **_kw,
) -> str:
    """Review reminder message."""
    return (
        f"[Remind] {project} review is incomplete. Target: {issues_display}\n"
        f"Submit review for each Issue with the following commands:\n"
        f"{cmd_lines}"
    )


def nudge_dispute(
    project: str,
    dispute_lines: str,
    **_kw,
) -> str:
    """Dispute response reminder message."""
    return (
        f"[Dispute — Response Required]\n"
        f"A dispute has been filed against your verdict in {project}.\n"
        f"Re-evaluate and submit your verdict with --force:\n\n"
        f"{dispute_lines}"
    )
