"""CODE_REVIEW_NPASS ステートのプロンプト。

Nパスレビュー依頼メッセージ。Issue本文・diffは再送しない（トークン節約）。

Variables (review_request):
    project: str           - プロジェクト名
    todo_header: str       - TODOチェックリスト（組み立て済み）
    completion: str        - 完了コマンド一覧（組み立て済み）
    pass_num: int          - 現在のパス番号
    target_pass: int       - 目標パス数
    comment_line: str      - オーナーコメント行
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
    """Nパスレビュー依頼メッセージ。"""
    return (
        f"[gokrax] {project}: Nパスコードレビュー（パス {pass_num}/{target_pass}）{comment_line}\n\n"
        f"前回のレビューで見落とした箇所がないか再チェックせよ。\n"
        f"前回と同じ指摘を繰り返す必要はない。新たな観点で確認すること。\n\n"
        f"Issue内容やdiffは前パスで既に送信済み。内容を忘れた場合は前パスで参照したファイル/コマンドを再実行して確認せよ。\n\n"
        f"{todo_header}\n\n{completion}\n\n"
        f"⚠️ 全Issueのreviewコマンドを実行するまでタスク未完了。途中で止めるな。"
    )
