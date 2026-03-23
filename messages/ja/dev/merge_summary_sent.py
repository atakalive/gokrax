"""MERGE_SUMMARY_SENT ステートの通知。

Variables:
    project: str       - プロジェクト名
    batch: list        - バッチアイテム
    automerge: bool    - automerge有効フラグ
    queue_mode: bool   - qrun実行フラグ
"""

_VERDICT_EMOJI = {"APPROVE": "🟢", "P0": "🔴", "P1": "🟡", "P2": "🔵"}


def format_merge_summary(
    project: str,
    batch: list,
    automerge: bool = False,
    queue_mode: bool = False,
    MERGE_SUMMARY_FOOTER: str = "",
    **_kw,
) -> str:
    """#gokrax 投稿用マージサマリーを生成する。

    2000文字超は post_discord が自動分割するので、ここでは切り詰めない。
    """
    from notify import mask_agent_name
    q_prefix = "[Queue]" if queue_mode else ""
    lines = [f"**{q_prefix}[{project}] マージサマリー**\n"]
    for item in batch:
        num = item["issue"]
        title = item.get("title", "")
        commit = item.get("commit", "?")
        lines.append(f"**#{num}: {title}** (`{commit}`)")
        # コードレビュー結果を表示（なければ設計レビュー）
        reviews = item.get("code_reviews") or item.get("design_reviews") or {}
        for reviewer, rev in reviews.items():
            masked = mask_agent_name(reviewer)
            verdict = rev.get("verdict", "?")
            emoji = _VERDICT_EMOJI.get(verdict, "⚪")
            summary = rev.get("summary", "")
            # summary の1行目だけ使う（長いレビューは切る）
            first_line = summary.split("\n")[0][:120] if summary else ""
            if first_line:
                lines.append(f"  {emoji} **{masked}**: {verdict} — {first_line}")
            else:
                lines.append(f"  {emoji} **{masked}**: {verdict}")
        lines.append("")  # 空行で区切り

    # Footer (Issue #45: automerge時は文言変更)
    if automerge:
        lines.append("\n---\n⚡ automerge有効 — 自動マージします")
    else:
        lines.append(MERGE_SUMMARY_FOOTER)

    return "\n".join(lines)
