"""DONE ステートのプロンプト・通知。

Variables:
    project: str       - プロジェクト名
    content: str       - マージサマリー本文
"""


def batch_done(
    project: str,
    content: str,
    **_kw,
) -> str:
    """バッチ完了通知（実装者への振り返り指示、watchdog.py DONE遷移時）。"""
    return (
        f"[devbar] {project}: バッチ完了\n"
        f"{content}\n\n"
        "上記の作業を振り返り、以下だけを記録してください:\n"
        "- 踏んだ罠、ハマったこと（あれば）\n"
        "- レビュアー指摘で学んだこと（あれば）\n"
        "- 今後の作業に影響する判断（あれば）\n"
        "記録すべきことがなければ NO_REPLY で構いません。"
    )
