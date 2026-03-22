"""Prompts for the CODE_REVIEW_NPASS state.

N-pass review request message. Issue body and diff are not re-sent (token savings).

Variables (review_request):
    project: str           - Project name
    todo_header: str       - TODO checklist (pre-assembled)
    completion: str        - Completion command list (pre-assembled)
    pass_num: int          - Current pass number
    target_pass: int       - Target pass count
    comment_line: str      - Owner comment line
    refresher_cmds: str    - Refresher commands (pre-assembled)
"""


def review_request(
    project: str,
    todo_header: str,
    completion: str,
    pass_num: int,
    target_pass: int,
    comment_line: str,
    refresher_cmds: str = "",
    **_kw: object,
) -> str:
    """N-pass review request message."""
    refresher = refresher_cmds or "Re-run the files/commands you referenced in the previous pass."
    return (
        f"[gokrax] {project}: N-pass code review (pass {pass_num}/{target_pass}){comment_line}\n\n"
        f"Re-check for anything overlooked in the previous review pass.\n"
        f"No need to repeat the same findings. Focus on new perspectives.\n\n"
        f"Issue content and diff were already sent in the previous pass. If you need a refresher:\n{refresher}\n\n"
        f"{todo_header}\n\n{completion}\n\n"
        f"⚠️ Task is not complete until all Issue review commands are executed. Do not stop midway."
    )
