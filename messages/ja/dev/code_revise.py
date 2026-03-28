"""CODE_REVISE ステートのプロンプト・催促。

Variables:
    project: str       - プロジェクト名
    issues_str: str    - 対象Issue番号の文字列
    comment_line: str  - オーナーコメント行
    fix_label: str     - 修正対象ラベル（"P0/P1指摘" or "P0/P1/P2指摘"）
    p2_note: str       - p2_fix モード注記（空文字 or 注意書き）
    GOKRAX_CLI: str    - gokrax CLIパス
"""


def transition(
    project: str,
    issues_str: str,
    comment_line: str,
    fix_label: str,
    p2_note: str,
    GOKRAX_CLI: str,
    **_kw,
) -> str:
    """コード修正フェーズの指示メッセージ（watchdog.py get_notification_for_state CODE_REVISE）。"""
    return (
        f"[gokrax] {project}: コード修正フェーズ\n"
        f"{comment_line}"
        f"対象Issue: {issues_str}\n"
        f"{p2_note}"
        f"【手順】\n"
        f"1. {fix_label}を読み、コードを修正する\n"
        f"   指摘の確認: `glab issue view N --comments --per-page 100`\n"
        f"2. git commit する\n"
        f"3. gokrax に完了報告:\n"
        f"   {GOKRAX_CLI} code-revise --pj {project} --issue N [N...] --hash <commit> --summary \"<変更内容の簡潔なサマリー>\"\n\n"
        f"複数レビュアーから同一のP2/Suggestionがある場合、その指摘は正しい可能性が高いため修正せよ。\n"
        f"--hash <commit> を忘れずに添付して送信すること。\n"
        f"レビュアー指摘と設計判断が相違する場合は、新規Issueを立てて設計判断を議論する場所を用意せよ。\n"
        f"※ P0/P1指摘に誤りがあると確信した場合、revise完了前に異議を申し立てることができます:\n"
        f"{GOKRAX_CLI} dispute --pj {project} --issue N --reviewer REVIEWER --reason \"理由\"\n"
        f"理由は詳細に、子供でも理解できる粒度で記載してください。disputeが認められた場合、該当P0/P1は取り下げられます。\n"
        f"[お願い] 仕事は中断せず、完了まで一気にやること。"
    )


def nudge(**_kw) -> str:
    """CODE_REVISE 催促メッセージ。"""
    return (
        "[Remind] 予定のリバイス作業を進め、完了してください。\n"
        "gokrax code-revise --pj <project> --issue <N> --hash <commit> で修正コミットを報告してください。"
    )


# ---------------------------------------------------------------------------
# Discord通知（短文）
# design_revise.notify_revise_summary を共用する。
# ---------------------------------------------------------------------------
