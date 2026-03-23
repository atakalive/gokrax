"""Notifications for the MERGE_SUMMARY_SENT state.

Variables:
    project: str       - Project name
    batch: list        - Batch items
    automerge: bool    - automerge enabled flag
    queue_mode: bool   - qrun execution flag
"""

_VERDICT_EMOJI = {"APPROVE": "🟢", "P0": "🔴", "P1": "🟡", "P2": "🔵"}


def format_merge_summary(
    project: str,
    batch: list,
    automerge: bool = False,
    queue_mode: bool = False,
    MERGE_SUMMARY_FOOTER: str = "",
    reviewer_number_map: dict | None = None,
    **_kw,
) -> str:
    """Generate merge summary for #gokrax posting.

    Messages over 2000 characters are auto-split by post_discord, so no truncation here.
    """
    from notify import mask_agent_name
    q_prefix = "[Queue]" if queue_mode else ""
    lines = [f"**{q_prefix}[{project}] merge summary**\n"]
    for item in batch:
        num = item["issue"]
        title = item.get("title", "")
        commit = item.get("commit", "?")
        lines.append(f"**#{num}: {title}** (`{commit}`)")
        # Show code review results (fall back to design reviews)
        reviews = item.get("code_reviews") or item.get("design_reviews") or {}
        for reviewer, rev in reviews.items():
            masked = mask_agent_name(reviewer, reviewer_number_map=reviewer_number_map)
            verdict = rev.get("verdict", "?")
            emoji = _VERDICT_EMOJI.get(verdict, "⚪")
            summary = rev.get("summary", "")
            # Use only first line of summary (truncate long reviews)
            first_line = summary.split("\n")[0][:120] if summary else ""
            if first_line:
                lines.append(f"  {emoji} **{masked}**: {verdict} — {first_line}")
            else:
                lines.append(f"  {emoji} **{masked}**: {verdict}")
        lines.append("")  # blank line separator

    # Footer (Issue #45: change wording when automerge is on)
    if automerge:
        lines.append("\n---\n⚡ automerge enabled — auto-merging")
    else:
        lines.append(MERGE_SUMMARY_FOOTER)

    return "\n".join(lines)
