"""Prompts for the DESIGN_REVIEW_NPASS state.

N-pass review request message. Issue body is not re-sent (token savings).

Variables (review_request):
    project: str           - Project name
    todo_header: str       - TODO checklist (pre-assembled)
    completion: str        - Completion command list (pre-assembled)
    pass_num: int          - Current pass number
    target_pass: int       - Target pass count
    comment_line: str      - Owner comment line
"""


def review_request(
    project: str,
    todo_header: str,
    completion: str,
    pass_num: int,
    target_pass: int,
    comment_line: str,
    **_kw: object,
) -> str:
    """N-pass review request message."""
    return (
        f"[gokrax] {project}: N-pass design review (pass {pass_num}/{target_pass}){comment_line}\n\n"
        f"Re-check for anything overlooked in the previous review pass.\n"
        f"No need to repeat the same findings. Focus on new perspectives.\n\n"
        f"Issue content was already sent in the previous pass. If you need a refresher, run `glab issue view N` and `glab issue note-list N`.\n\n"
        f"{todo_header}\n\n{completion}\n\n"
        f"⚠️ Task is not complete until all Issue review commands are executed. Do not stop midway."
    )
