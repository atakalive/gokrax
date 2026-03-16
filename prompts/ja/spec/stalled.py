"""SPEC_STALLED ステートの通知。"""


def notify_stalled(project: str, rev: str | int, remaining_p1_plus: int, **_kw) -> str:
    """MAX_CYCLES到達。"""
    return f"[Spec] ⏸️ {project}: MAX_CYCLES到達、P1以上 {remaining_p1_plus}件残存"
