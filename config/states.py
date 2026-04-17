"""config.states — 状態遷移テーブル・パイプライン定数"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


# ---------------------------------------------------------------------------
# State enums
# ---------------------------------------------------------------------------

class State(StrEnum):
    """Pipeline states (dev + spec)."""
    # Dev mode
    IDLE = "IDLE"
    INITIALIZE = "INITIALIZE"
    DESIGN_PLAN = "DESIGN_PLAN"
    DESIGN_REVIEW = "DESIGN_REVIEW"
    DESIGN_REVIEW_NPASS = "DESIGN_REVIEW_NPASS"
    DESIGN_REVISE = "DESIGN_REVISE"
    DESIGN_APPROVED = "DESIGN_APPROVED"
    ASSESSMENT = "ASSESSMENT"
    IMPLEMENTATION = "IMPLEMENTATION"
    CODE_TEST = "CODE_TEST"
    CODE_TEST_FIX = "CODE_TEST_FIX"
    CODE_REVIEW = "CODE_REVIEW"
    CODE_REVIEW_NPASS = "CODE_REVIEW_NPASS"
    CODE_REVISE = "CODE_REVISE"
    CODE_APPROVED = "CODE_APPROVED"
    MERGE_SUMMARY_SENT = "MERGE_SUMMARY_SENT"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    # Spec mode
    SPEC_REVIEW = "SPEC_REVIEW"
    SPEC_REVISE = "SPEC_REVISE"
    SPEC_APPROVED = "SPEC_APPROVED"
    ISSUE_SUGGESTION = "ISSUE_SUGGESTION"
    ISSUE_PLAN = "ISSUE_PLAN"
    QUEUE_PLAN = "QUEUE_PLAN"
    SPEC_DONE = "SPEC_DONE"
    SPEC_STALLED = "SPEC_STALLED"
    SPEC_REVIEW_FAILED = "SPEC_REVIEW_FAILED"
    SPEC_PAUSED = "SPEC_PAUSED"


__all__ = [
    "State",
    "MAX_BATCH", "MAX_HISTORY", "MAX_REVISE_CYCLES",
    "VALID_VERDICTS", "VALID_FLAG_VERDICTS",
    "STATE_PHASE_MAP",
    "VALID_STATES", "VALID_TRANSITIONS",
    "BLOCK_TIMERS", "MAX_TIMEOUT_EXTENSION", "EXTENDABLE_STATES",
    "NUDGE_GRACE_SEC", "EXTEND_NOTICE_THRESHOLD",
    "SPEC_STATES", "SPEC_TRANSITIONS",
    "MAX_SPEC_REVISE_CYCLES",
    "SPEC_BLOCK_TIMERS",
    "SPEC_REVISE_SELF_REVIEW_PASSES", "MAX_SPEC_RETRIES",
    "SPEC_REVIEW_RAW_RETENTION_DAYS",
]

# パイプライン
MAX_BATCH = 5
MAX_HISTORY = 100
MAX_REVISE_CYCLES = 4  # max cycles for REVISE->REVIEW
VALID_VERDICTS = ["APPROVE", "P0", "P1", "P2", "REJECT"]
VALID_FLAG_VERDICTS = ["P0", "P1", "P2"]

# ---------------------------------------------------------------------------
# dev mode 基盤
# ---------------------------------------------------------------------------
# [IMPORTANT] Transition tables use raw strings for readability. Do not convert to State enum references.

# State→phase mapping (used by flag command)
STATE_PHASE_MAP: dict[str, str] = {
    "DESIGN_PLAN": "design",
    "DESIGN_REVIEW": "design",
    "DESIGN_REVIEW_NPASS": "design",
    "DESIGN_REVISE": "design",
    "DESIGN_APPROVED": "design",
    "ASSESSMENT": "design",
    "IMPLEMENTATION": "code",
    "CODE_TEST": "code",
    "CODE_TEST_FIX": "code",
    "CODE_REVIEW": "code",
    "CODE_REVIEW_NPASS": "code",
    "CODE_REVISE": "code",
    "CODE_APPROVED": "code",
    "MERGE_SUMMARY_SENT": "code",
}

VALID_STATES = [
    "IDLE", "INITIALIZE",
    "DESIGN_PLAN", "DESIGN_REVIEW", "DESIGN_REVIEW_NPASS", "DESIGN_REVISE", "DESIGN_APPROVED",
    "ASSESSMENT", "IMPLEMENTATION",
    "CODE_TEST", "CODE_TEST_FIX",
    "CODE_REVIEW", "CODE_REVIEW_NPASS", "CODE_REVISE", "CODE_APPROVED",
    "MERGE_SUMMARY_SENT", "DONE", "BLOCKED",
]

VALID_TRANSITIONS = {
    "IDLE": ["INITIALIZE"],
    "INITIALIZE": ["DESIGN_PLAN", "DESIGN_APPROVED"],
    "DESIGN_PLAN": ["DESIGN_REVIEW"],
    "DESIGN_REVIEW": ["DESIGN_APPROVED", "DESIGN_REVISE", "BLOCKED", "DESIGN_REVIEW_NPASS"],
    "DESIGN_REVIEW_NPASS": ["DESIGN_APPROVED", "DESIGN_REVISE", "DESIGN_REVIEW_NPASS"],
    "DESIGN_REVISE": ["DESIGN_REVIEW"],
    "DESIGN_APPROVED": ["ASSESSMENT", "IMPLEMENTATION"],
    "ASSESSMENT": ["IMPLEMENTATION", "IDLE"],
    "IMPLEMENTATION": ["CODE_TEST", "CODE_REVIEW"],
    "CODE_TEST": ["CODE_REVIEW", "CODE_TEST_FIX", "BLOCKED"],
    "CODE_TEST_FIX": ["CODE_TEST", "BLOCKED"],
    "CODE_REVIEW": ["CODE_APPROVED", "CODE_REVISE", "BLOCKED", "CODE_REVIEW_NPASS"],
    "CODE_REVIEW_NPASS": ["CODE_APPROVED", "CODE_REVISE", "CODE_REVIEW_NPASS"],
    "CODE_REVISE": ["CODE_TEST", "CODE_REVIEW"],
    "CODE_APPROVED": ["MERGE_SUMMARY_SENT"],
    "MERGE_SUMMARY_SENT": ["DONE"],
    "DONE": ["IDLE"],
    "BLOCKED": ["IDLE"],  # recovery: return to IDLE before restart
}

# フェーズ別タイムアウト (秒)。0 = タイムアウトなし
BLOCK_TIMERS = {
    "DESIGN_PLAN":    1800,  # 30 min
    "DESIGN_REVIEW":  3600,  # 60 min
    "DESIGN_REVISE":  1800,  # 30 min
    "ASSESSMENT":     1200,  # 20 min
    "IMPLEMENTATION": 7200,  # 120 min
    "CODE_TEST":      600,   # 10 min
    "CODE_TEST_FIX":  3600,  # 60 min
    "CODE_REVIEW":    3600,  # 60 min
    "CODE_REVISE":    1800,  # 30 min
}

# timeout_extension の上限（秒）。MAX_EXTENDS(2) × デフォルト延長(600秒) × 安全係数(3)
MAX_TIMEOUT_EXTENSION = 3600

# タイムアウト延長可能な状態
EXTENDABLE_STATES = {"DESIGN_PLAN", "DESIGN_REVISE", "ASSESSMENT", "IMPLEMENTATION", "CODE_TEST_FIX", "CODE_REVISE"}

# 状態遷移直後の催促猶予期間（秒）
NUDGE_GRACE_SEC = 600  # 10 min

# 残り時間が閾値未満で延長案内を表示（秒）
EXTEND_NOTICE_THRESHOLD = 300  # 5 min

# ---------------------------------------------------------------------------
# spec mode 基盤 — Issue #49
# ---------------------------------------------------------------------------
# [IMPORTANT] Transition tables use raw strings for readability. Do not convert to State enum references.

# 1-A. SPEC_STATES（VALID_STATES の直後に論理的に追記）
SPEC_STATES: list[str] = [
    "SPEC_REVIEW", "SPEC_REVISE", "SPEC_APPROVED",
    "ISSUE_SUGGESTION", "ISSUE_PLAN", "QUEUE_PLAN", "SPEC_DONE",
    "SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED",
]

# 1-B. SPEC_TRANSITIONS
SPEC_TRANSITIONS: dict[str, list[str]] = {
    "IDLE":               ["SPEC_REVIEW", "SPEC_APPROVED"],
    "SPEC_REVIEW":        ["SPEC_REVISE", "SPEC_APPROVED", "SPEC_STALLED",
                           "SPEC_REVIEW_FAILED", "SPEC_PAUSED"],
    "SPEC_REVISE":        ["SPEC_REVIEW", "SPEC_PAUSED"],
    "SPEC_APPROVED":      ["ISSUE_SUGGESTION", "SPEC_DONE"],
    "ISSUE_SUGGESTION":   ["ISSUE_PLAN", "SPEC_PAUSED"],
    "ISSUE_PLAN":         ["QUEUE_PLAN", "SPEC_DONE", "SPEC_PAUSED"],
    "QUEUE_PLAN":         ["SPEC_DONE", "SPEC_PAUSED"],
    "SPEC_DONE":          ["IDLE"],
    "SPEC_STALLED":       ["SPEC_APPROVED", "SPEC_REVISE"],
    "SPEC_REVIEW_FAILED": ["SPEC_REVIEW"],
    "SPEC_PAUSED":        ["SPEC_REVIEW", "SPEC_REVISE", "SPEC_APPROVED",
                           "ISSUE_SUGGESTION", "ISSUE_PLAN", "QUEUE_PLAN",
                           "SPEC_DONE"],
}

# 1-E. spec mode 定数（§3.2）
MAX_SPEC_REVISE_CYCLES: int = 10
SPEC_BLOCK_TIMERS: dict[str, int] = {
    "SPEC_REVIEW":       1800,  # 30 min
    "SPEC_REVISE":       1800,  # 30 min
    "ISSUE_SUGGESTION":   600,  # 10 min
    "ISSUE_PLAN":        1800,  # 30 min  per §10.2
    "QUEUE_PLAN":        1800,  # 30 min  per §10.2
}
SPEC_REVISE_SELF_REVIEW_PASSES: int = 2
MAX_SPEC_RETRIES: int = 3
SPEC_REVIEW_RAW_RETENTION_DAYS: int = 30

# ---------------------------------------------------------------------------
# spec 統合（states.py 末尾）
# ---------------------------------------------------------------------------
# 1-C. VALID_STATES / VALID_TRANSITIONS への統合（§2.3: sorted(set(...)) で順序固定）
VALID_STATES = sorted(set(VALID_STATES + SPEC_STATES))

for _state, _targets in SPEC_TRANSITIONS.items():
    _existing = VALID_TRANSITIONS.get(_state, [])
    VALID_TRANSITIONS[_state] = sorted(set(_existing + _targets))

# 1-D. STATE_PHASE_MAP への追加
STATE_PHASE_MAP.update({s: "spec" for s in SPEC_STATES})
