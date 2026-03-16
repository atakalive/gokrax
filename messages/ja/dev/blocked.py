"""BLOCKED ステートのプロンプト・通知。

Variables:
    phase: str              - フェーズ名（"設計" or "コード"）
    MAX_REVISE_CYCLES: int  - 最大リバイスサイクル数
    OWNER_NAME: str         - オーナー名
"""


def blocked_max_cycles(
    phase: str,
    MAX_REVISE_CYCLES: int,
    OWNER_NAME: str,
    **_kw,
) -> str:
    """P0 残存 + max cycles 超過時の BLOCKED メッセージ（_resolve_review_outcome）。"""
    return (
        f"{phase}レビューサイクルが上限（{MAX_REVISE_CYCLES}回）に達しました。\n"
        f"P0の指摘が解消されていません。手動で対応してください。Discordで{OWNER_NAME}に報告してください。"
    )


def blocked_timeout(state: str, **_kw) -> str:
    """タイムアウトによる BLOCKED メッセージ（_check_nudge）。"""
    return f"{state} タイムアウト。応答がありませんでした。"


def blocked_cc_no_commit(project: str, **_kw) -> str:
    """CC がコミットを作成しなかった場合の BLOCKED メッセージ（run_cc スクリプト内）。"""
    return (
        f"[{project}] ❌ CC がコミットを作成しなかった（2回リトライ後）→ BLOCKED"
    )


# ---------------------------------------------------------------------------
# Discord通知（短文）
# ---------------------------------------------------------------------------

def notify_recovery_merge_summary(project: str, **_kw) -> str:
    """merge_summary通知の復旧警告。"""
    return f"[{project}] ⚠️ merge_summary通知が中断されていました。手動確認してください。"


def notify_recovery_cc(project: str, **_kw) -> str:
    """CC起動の復旧警告。"""
    return f"[{project}] ⚠️ CC起動が中断されていました。手動確認してください。"
