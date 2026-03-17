"""DESIGN_REVIEW ステートのプロンプト・催促。

レビュー依頼メッセージ生成は notify.py の format_review_request() がデータ整形を担う。
ここにはプロンプトテンプレート（テキスト部分）のみ定義する。

Variables (review_request):
    project: str           - プロジェクト名
    todo_header: str       - TODOチェックリスト（組み立て済み）
    guidance: str          - レビュー観点テキスト
    body: str              - Issue本文＋前回指摘＋dispute等（組み立て済み）
    completion: str        - 完了コマンド一覧（組み立て済み）
    comment_line: str      - オーナーコメント行
    phase_note: str        - フェーズ注記
"""


def review_request(
    project: str,
    todo_header: str,
    guidance: str,
    body: str,
    completion: str,
    comment_line: str,
    phase_note: str,
    **_kw,
) -> str:
    """設計レビュー依頼メッセージ（notify.py format_review_request の組み立て部分）。

    skill_block の挿入は呼び出し側で行う。
    """
    return (
        f"[devbar] {project}: 設計レビュー依頼{comment_line}{phase_note}\n\n"
        f"{todo_header}\n\n{guidance}\n\n{body}{completion}"
    )


def file_review_request(
    project: str,
    n: int,
    file_path: str,
    cmds_block: str,
    **_kw,
) -> str:
    """ファイル外部化時の設計レビュー依頼メッセージ（notify.py _build_file_review_message）。

    skill_block の挿入は呼び出し側で行う。
    """
    return (
        f"[devbar] {project}: 設計レビュー依頼（{n}件）\n\n"
        f"レビューデータファイルを読み込み、全件レビューせよ。\n\n"
        f"Read {file_path}\n\n"
        f"完了後、各Issueについて以下を実行:\n"
        f"{cmds_block}\n"
        f"（上記コマンドを各Issue分繰り返す）\n\n"
        f"⚠️ 全Issueのreviewコマンドを実行するまでタスク未完了。途中で止めるな。"
    )


def phase_note(**_kw) -> str:
    """設計レビューのフェーズ注記。"""
    return "\n⚠️ これは設計レビュー DESIGN_REVIEW です。コードやdiffはまだ存在しません。\n"
def guidance_design(**_kw) -> str:
    """設計レビュー観点テキスト。"""
    return (
        "レビュー観点:\n"
        "- 数理的に精確か（厳しく検証せよ）\n"
        "- Issue本文の仕様が明確か、実装可能か\n"
        "- 矛盾やエッジケースがないか、思わぬ落とし穴がないか"
    )


# ---------------------------------------------------------------------------
# 催促
# ---------------------------------------------------------------------------

def nudge_review(
    project: str,
    issues_display: str,
    cmd_lines: str,
    **_kw,
) -> str:
    """通常レビュー催促メッセージ（watchdog.py レビュー催促ブロック）。

    issues_display は呼び出し側で組み立て済み（例: "#1, #2"）。
    """
    return (
        f"[Remind] {project} のレビューが未完了です。対象: {issues_display}\n"
        f"以下のコマンドで各 Issue のレビューを報告してください:\n"
        f"{cmd_lines}"
    )


def notify_nudge_reviewers(project: str, reviewers: str, q_prefix: str = "", **_kw) -> str:
    """レビュアー催促の Discord 通知。reviewers は組み立て済み。"""
    return f"{q_prefix}[{project}] レビュアーを催促: {reviewers}"


def nudge_dispute(
    project: str,
    dispute_lines: str,
    **_kw,
) -> str:
    """dispute 回答催促メッセージ（watchdog.py dispute催促ブロック）。"""
    return (
        f"【異議申し立て — 回答催促】\n"
        f"{project} であなたの判定に対して異議が出ています。\n"
        f"再評価した上で --force 付きで判定を報告してください:\n\n"
        f"{dispute_lines}"
    )
