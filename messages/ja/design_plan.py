"""DESIGN_PLAN ステートのプロンプト・催促。

Variables:
    project: str       - プロジェクト名
    issues_str: str    - 対象Issue番号の文字列（例: "#1, #2"）
    comment_line: str  - オーナーコメント行（空文字 or "{OWNER_NAME}からの要望: ...\n"）
    DEVBAR_CLI: str    - devbar CLIパス
"""


def transition(
    project: str,
    issues_str: str,
    comment_line: str,
    DEVBAR_CLI: str,
    **_kw,
) -> str:
    """設計確認フェーズの指示メッセージ（watchdog.py get_notification_for_state DESIGN_PLAN）。"""
    return (
        f"[devbar] {project}: 設計確認フェーズ\n"
        f"{comment_line}"
        f"対象Issue: {issues_str}\n"
        f"Claude Codeが確実に実装できる粒度まで、**対象Issue本文の説明を修正せよ** (glab issue update)。\n"
        f"コメントによる補足は禁止する。\n"
        f"全て修正後、問題がなければ plan-done して完了せよ（一括報告できる）。\n"
        f"python3 {DEVBAR_CLI} plan-done --project {project} --issue N [N...]\n"
        f"[お願い] 仕事は中断せず、完了まで一気にやること。"
    )


# ---------------------------------------------------------------------------
# Discord通知（短文）
# ---------------------------------------------------------------------------

def notify_issues(project: str, issue_lines: str, q_prefix: str = "", **_kw) -> str:
    """バッチ開始時の対象Issue一覧通知。issue_lines は組み立て済み。"""
    return f"{q_prefix}[{project}] 対象Issue:\n{issue_lines}"
