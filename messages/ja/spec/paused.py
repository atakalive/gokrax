"""SPEC_PAUSED ステートの通知。"""


def notify_paused(project: str, reason: str, **_kw) -> str:
    """パイプライン停止。"""
    return f"[Spec][{project}] ⏸️ パイプライン停止 — {reason}"


def notify_failure(project: str, kind: str, detail: str = "", **_kw) -> str:
    """失敗系通知（汎用）。"""
    suffix = f" — {detail}" if detail else ""
    return f"[Spec][{project}] ❌ {kind}{suffix}"
