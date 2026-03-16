"""CODE_REVIEW ステートのプロンプト・催促。

Variables (review_request):
    project: str           - プロジェクト名
    todo_header: str       - TODOチェックリスト（組み立て済み）
    guidance: str          - レビュー観点テキスト
    body: str              - Issue本文＋前回指摘＋diff＋dispute等（組み立て済み）
    completion: str        - 完了コマンド一覧（組み立て済み）
    comment_line: str      - オーナーコメント行
"""

__all__ = [
    "review_request",
    "file_review_request",
    "guidance_code",
    "nudge_review",
    "nudge_dispute",
]


def review_request(
    project: str,
    todo_header: str,
    guidance: str,
    body: str,
    completion: str,
    comment_line: str,
    **_kw,
) -> str:
    """コードレビュー依頼メッセージ（notify.py format_review_request の組み立て部分）。

    skill_block の挿入は呼び出し側で行う。
    """
    return (
        f"[devbar] {project}: コードレビュー依頼{comment_line}\n\n"
        f"{todo_header}\n\n{guidance}\n\n{body}{completion}"
    )


def file_review_request(
    project: str,
    n: int,
    file_path: str,
    cmds_block: str,
    **_kw,
) -> str:
    """ファイル外部化時のコードレビュー依頼メッセージ（notify.py _build_file_review_message）。

    skill_block の挿入は呼び出し側で行う。
    """
    return (
        f"[devbar] {project}: コードレビュー依頼（{n}件）\n\n"
        f"レビューデータファイルを読み込み、全件レビューせよ。\n\n"
        f"Read {file_path}\n\n"
        f"完了後、各Issueについて以下を実行:\n"
        f"{cmds_block}\n"
        f"（上記コマンドを各Issue分繰り返す）\n\n"
        f"⚠️ 全Issueのreviewコマンドを実行するまでタスク未完了。途中で止めるな。"
    )


def guidance_code(**_kw) -> str:
    """コードレビュー観点テキスト。"""
    return (
        "レビュー観点:\n"
        "- 設計レビューで承認された仕様通りに実装されているか\n"
        "- バグ、エッジケース、型ヒントの欠落\n"
        "- テストの妥当性を判定\n\n"
        "スコープ制約:\n"
        "- P0/P1 を出す場合、該当コードが今回の diff に含まれることを確認せよ\n"
        "- 前バッチで既に入った変更を現バッチの責任にしない\n"
        "- diff 外で気づいた問題は P2（提案）として報告せよ\n\n"
        "コンテキスト制約（重要）:\n"
        "- あなたに見えているのは diff とその周辺コンテキストのみである。リポジトリ全体のコードは見えていない\n"
        "- diff に含まれないファイル・関数・変数について「存在しない」と断定してはならない\n"
        "- 「〜が見当たらない」という指摘は P2（提案）に留め、P0/P1 にしてはならない\n"
        "- diff 外のコードに依存する指摘を P0/P1 で出す場合、その根拠が diff 内に明示的に存在することを確認せよ"
    )


def nudge_review(
    project: str,
    issues_display: str,
    cmd_lines: str,
    **_kw,
) -> str:
    """通常レビュー催促メッセージ。"""
    return (
        f"[Remind] {project} のレビューが未完了です。対象: {issues_display}\n"
        f"以下のコマンドで各 Issue のレビューを報告してください:\n"
        f"{cmd_lines}"
    )


def nudge_dispute(
    project: str,
    dispute_lines: str,
    **_kw,
) -> str:
    """dispute 回答催促メッセージ。"""
    return (
        f"【異議申し立て — 回答催促】\n"
        f"{project} であなたの判定に対して異議が出ています。\n"
        f"再評価した上で --force 付きで判定を報告してください:\n\n"
        f"{dispute_lines}"
    )
