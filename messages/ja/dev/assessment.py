"""ASSESSMENT ステートのプロンプト・催促。

Variables:
    project: str       - プロジェクト名
    issues_str: str    - 対象Issue番号の文字列（例: "#1, #2"）
    comment_line: str  - オーナーコメント行（空文字 or "{OWNER_NAME}からの要望: ...\n"）
    GOKRAX_CLI: str    - gokrax CLIパス
"""


def transition(
    project: str,
    issues_str: str,
    comment_line: str,
    GOKRAX_CLI: str,
    **_kw,
) -> str:
    """ASSESSMENT フェーズの指示メッセージ。"""
    return (
        f"[gokrax] {project}: 難易度判定フェーズ\n"
        f"{comment_line}"
        f"対象Issue: {issues_str}\n"
        f"以下の基準でバッチ全体の難易度レベル (Lvl 1-5) を判定し、assess-done を実行せよ。\n"
        f"\n"
        f"判定基準（コード複雑性）:\n"
        f"  Lvl 1: 1ファイル完結、定型的な変更（定数変更、テキスト修正等）\n"
        f"  Lvl 2: 数ファイル、既存パターンの踏襲（新オプション追加等）\n"
        f"  Lvl 3: 複数モジュールにまたがる、新しいロジック追加\n"
        f"  Lvl 4: 大規模な書き換え、複数の既存処理に影響\n"
        f"  Lvl 5: 全体に波及する構造変更\n"
        f"\n"
        f"{GOKRAX_CLI} assess-done --project {project} --level N --summary \"判定理由\"\n"
        f"[お願い] 仕事は中断せず、完了まで一気にやること。"
    )


def nudge(
    **_kw,
) -> str:
    """ASSESSMENT 催促メッセージ。"""
    return (
        "[Remind] 難易度判定を行い、assess-done を実行してください。\n"
        "gokrax assess-done --project <project> --level N --summary \"理由\""
    )
