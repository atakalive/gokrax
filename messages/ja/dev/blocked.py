"""BLOCKED ステートのプロンプト・通知。

Variables:
    state: str              - 現在のステート名（"DESIGN_REVIEW" 等）
    severity: str           - 重大度（"P0" or "P1"）
    MAX_REVISE_CYCLES: int  - 最大リバイスサイクル数
    OWNER_NAME: str         - オーナー名
"""


def _phase_label(state: str) -> str:
    """ステート名から日本語フェーズ名を導出する。"""
    return "設計" if "DESIGN" in state else "コード"


def blocked_max_cycles(
    state: str,
    MAX_REVISE_CYCLES: int,
    OWNER_NAME: str,
    severity: str = "P0",
    **_kw,
) -> str:
    """P0/P1 残存 + max cycles 超過時の BLOCKED メッセージ（_resolve_review_outcome）。"""
    phase = _phase_label(state)
    return (
        f"{phase}レビューサイクルが上限（{MAX_REVISE_CYCLES}回）に達しました。\n"
        f"{severity}の指摘が解消されていません。手動で対応してください。"
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


def blocked_prompt_report(
    project: str,
    state: str,
    impl_msg: str,
    GOKRAX_CLI: str,
    **_kw,
) -> str:
    """BLOCKED遷移時に実装者に送る状況報告依頼プロンプト。"""
    reason = impl_msg or "(理由未記載)"
    return (
        f"{state} から BLOCKED に遷移しました。\n"
        f"理由: {reason}\n\n"
        f"以下のコマンドで、管理者に状況を報告してください。何が起きたかを簡潔にまとめて報告し、次の指示を仰いでください。\n"
        f"{GOKRAX_CLI} blocked-report --pj {project} --summary \"<説明>\"\n\n"
        f"**[注意] 勝手に gokrax state を動かさないこと。管理者の指示を待ってください。**"
    )
