"""SPEC_APPROVED ステートの通知。

このステートにはエージェント向けプロンプトなし（オーナー確認待ちまたは自動進行）。
"""


def notify_approved(project: str, rev: str | int, **_kw) -> str:
    """通常承認（オーナー確認待ち）。"""
    return f"[Spec] {project}: spec承認 (rev{rev})。`devbar spec continue` でIssue分割へ"


def notify_approved_auto(project: str, rev: str | int, **_kw) -> str:
    """自動進行。"""
    return f"[Spec] {project}: spec承認 (rev{rev}) → Issue分割へ自動進行"


def notify_approved_forced(project: str, rev: str | int, remaining_p1_plus: int, **_kw) -> str:
    """強制承認。"""
    return f"[Spec] ⚠️ {project}: 強制承認 (P1以上 {remaining_p1_plus}件残存)"
