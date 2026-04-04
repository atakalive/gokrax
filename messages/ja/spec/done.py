"""SPEC_DONE ステートの通知。"""


def notify_done(project: str, **_kw) -> str:
    """spec mode完了。"""
    return f"[Spec][{project}] ✅ spec mode完了"
