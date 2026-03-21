"""Prompts and notifications for the DONE state.

Variables:
    project: str       - Project name
    content: str       - Merge summary body
"""


def batch_done(
    project: str,
    content: str,
    **_kw,
) -> str:
    """Batch completed notification (retrospective instructions for implementer, watchdog.py DONE transition)."""
    return (
        f"[gokrax] {project}: batch completed\n"
        f"{content}\n\n"
        "Review the work above and record only the following:\n"
        "- Pitfalls or issues encountered (if any)\n"
        "- Lessons learned from reviewer findings (if any)\n"
        "- Decisions that affect future work (if any)\n"
        "If nothing to record, NO_REPLY is fine."
    )
