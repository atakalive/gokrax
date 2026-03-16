"""通常モード共通の Discord 通知テンプレート。

ステート固有でない通知メッセージを定義する。
"""


def notify_state_transition(project: str, old_state: str, new_state: str, ts: str, q_prefix: str = "", **_kw) -> str:
    """状態遷移の Discord 通知。"""
    return f"{q_prefix}[{project}] {old_state} → {new_state} ({ts})"


def notify_nudge_implementer(project: str, state: str, implementer: str, ts: str, q_prefix: str = "", **_kw) -> str:
    """担当者催促の Discord 通知。"""
    return f"{q_prefix}[{project}] {state}: 担当者 {implementer} を催促 ({ts})"
