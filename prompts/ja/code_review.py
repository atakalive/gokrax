"""CODE_REVIEW ステートのプロンプト・催促。

コードレビューと設計レビューは共通構造を持つ。
催促メッセージは design_review.py と共通のため、そちらからインポート。

Variables (review_request):
    project: str           - プロジェクト名
    todo_header: str       - TODOチェックリスト（組み立て済み）
    guidance: str          - レビュー観点テキスト
    body: str              - Issue本文＋前回指摘＋diff＋dispute等（組み立て済み）
    completion: str        - 完了コマンド一覧（組み立て済み）
    comment_line: str      - オーナーコメント行
"""

from prompts.ja.design_review import (
    nudge_review,
    nudge_dispute,
)

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
