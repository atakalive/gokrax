"""devbar config — 定数の一元管理"""

import os
from pathlib import Path, PurePosixPath
from datetime import timezone, timedelta

DRY_RUN: bool = os.environ.get("DEVBAR_DRY_RUN", "").strip() not in ("", "0", "false")

# パス（テスト時は環境変数で上書き可能）
PIPELINES_DIR = Path(os.environ["DEVBAR_PIPELINES_DIR"]) if "DEVBAR_PIPELINES_DIR" in os.environ else Path.home() / ".openclaw/shared/pipelines"
DEVBAR_CLI = PurePosixPath("/home/ataka/.openclaw/shared/bin/devbar")
GLAB_BIN = "/home/ataka/bin/glab"
# GATEWAY_TOKEN_PATH removed — using direct bot token
LOG_FILE = Path("/tmp/devbar-watchdog.log")
QUEUE_FILE = PIPELINES_DIR / "devbar-queue.txt"  # Default. Don't delete this line.
QUEUE_FILE = Path("/mnt/s/wsl/work/project/DevBar/devbar-queue.txt")


# watchdog-loop
WATCHDOG_LOOP_SCRIPT = Path(__file__).resolve().parent / "watchdog-loop.sh"
WATCHDOG_LOOP_PIDFILE = Path("/tmp/devbar-watchdog-loop.pid")
WATCHDOG_LOOP_CRON_MARKER = "watchdog-loop"  # crontab行のgrep用マーカー
WATCHDOG_LOOP_CRON_ENTRY = (
    f"* * * * * flock -n /tmp/devbar-watchdog-loop.lock"
    f" setsid bash {Path(__file__).resolve().parent / 'watchdog-loop.sh'}"
    f" > /dev/null 2>&1 &"
)

# Discord
DISCORD_CHANNEL = "1474050582049329213"  # #dev-bar channel ID
DISCORD_BOT_TOKEN = "***REDACTED***"

# CC model
CC_MODEL_PLAN = "sonnet"     # DESIGN_PLAN フェーズ
CC_MODEL_IMPL = "sonnet"   # IMPLEMENTATION フェーズ

# タイムゾーン
JST = timezone(timedelta(hours=9))

# パイプライン
MAX_BATCH = 5
MAX_HISTORY = 100
MIN_REVIEWS = 3
MAX_REVISE_CYCLES = 3  # REVISE→REVIEWの最大サイクル数
VALID_VERDICTS = ["APPROVE", "P0", "P1", "P2", "REJECT"]
VALID_FLAG_VERDICTS = ["P0", "P1", "P2"]

# State→phase mapping (used by flag command)
STATE_PHASE_MAP: dict[str, str] = {
    "DESIGN_PLAN": "design",
    "DESIGN_REVIEW": "design",
    "DESIGN_REVISE": "design",
    "DESIGN_APPROVED": "design",
    "TRIAGE": "design",
    "IMPLEMENTATION": "code",
    "CODE_REVIEW": "code",
    "CODE_REVISE": "code",
    "CODE_APPROVED": "code",
    "MERGE_SUMMARY_SENT": "code",
}

# タイムアウト (seconds)
AGENT_SEND_TIMEOUT = 30
DISCORD_POST_TIMEOUT = 10
GLAB_TIMEOUT = 15

VALID_STATES = [
    "IDLE", "TRIAGE",
    "DESIGN_PLAN", "DESIGN_REVIEW", "DESIGN_REVISE", "DESIGN_APPROVED",
    "IMPLEMENTATION",
    "CODE_REVIEW", "CODE_REVISE", "CODE_APPROVED",
    "MERGE_SUMMARY_SENT", "DONE", "BLOCKED",
]

VALID_TRANSITIONS = {
    "TRIAGE": ["IDLE"],
    "IDLE": ["DESIGN_PLAN"],
    "DESIGN_PLAN": ["DESIGN_REVIEW"],
    "DESIGN_REVIEW": ["DESIGN_APPROVED", "DESIGN_REVISE", "BLOCKED"],
    "DESIGN_REVISE": ["DESIGN_REVIEW"],
    "DESIGN_APPROVED": ["IMPLEMENTATION"],
    "IMPLEMENTATION": ["CODE_REVIEW"],
    "CODE_REVIEW": ["CODE_APPROVED", "CODE_REVISE", "BLOCKED"],
    "CODE_REVISE": ["CODE_REVIEW"],
    "CODE_APPROVED": ["MERGE_SUMMARY_SENT"],
    "MERGE_SUMMARY_SENT": ["DONE"],
    "DONE": ["IDLE"],
    "BLOCKED": ["IDLE"],  # 復帰はIDLEに戻してから再開
}

# triageで投入可能な状態
TRIAGE_ALLOWED_STATES = ["IDLE", "TRIAGE"]

# エージェント
AGENTS = {
    "kaneko":   "agent:kaneko:main",    # Opus, Lead
    "pascal":   "agent:pascal:main",    # Gemini
    "leibniz":  "agent:leibniz:main",   # ChatGPT
    "hanfei":   "agent:hanfei:main",    # Qwen
    "dijkstra": "agent:dijkstra:main",  # Opus
    "neumann":  "agent:neumann:main",   # Opus, Lead
    "euler":    "agent:euler:main",     # ChatGPT
    "basho":    "agent:basho:main",     # Local
}

# REVIEWERS = ["pascal", "leibniz", "hanfei", "dijkstra"]

# 段階別レビュアー設定
# DEPRECATED: Use REVIEW_MODES instead (kept for backward compat)
DESIGN_REVIEWERS = ["pascal", "leibniz"]  # "dijkstra"
CODE_REVIEWERS = ["pascal", "leibniz"]

DESIGN_MIN_REVIEWS = 2
CODE_MIN_REVIEWS = 2

# Review modes: project-level reviewer assignment
REVIEW_MODES = {
    "full": {
        "members": ["pascal", "leibniz", "dijkstra", "euler", "basho"],
        "min_reviews": 5,
        "grace_period_sec": 0,
    },
    "standard": {
        "members": ["pascal", "leibniz", "dijkstra", "basho"],
        "min_reviews": 4,
        "grace_period_sec": 0,
    },
    "lite3_woOpus": {
        "members": ["leibniz", "pascal", "euler"],
        "min_reviews": 3,
        "grace_period_sec": 0,
    },
    "lite3_woGoogle": {
        "members": ["leibniz", "euler", "dijkstra"],
        "min_reviews": 3,
        "grace_period_sec": 0,
    },
    "lite": {
        "members": ["euler", "pascal"],
        "min_reviews": 2,
        "grace_period_sec": 0,
    },
    "min": {
        "members": ["leibniz"],
        "min_reviews": 1,
        "grace_period_sec": 0,
    },
    "skip": {
        "members": [],
        "min_reviews": 0,
        "grace_period_sec": 0,
    },
}

# Reviewer tiers: regular, semi, free, short-context
REVIEWER_TIERS: dict[str, list[str]] = {
    "regular": ["leibniz", "dijkstra", "euler"],
    "semi": ["pascal"],
    "free": ["hanfei"],
    "short-context": ["basho"],  # ローカルLLM等、コンテキスト長が短いレビュアー
}


def get_tier(agent_name: str) -> str:
    """Return tier for agent. Unknown agents are conservatively marked as 'free'."""
    for tier, members in REVIEWER_TIERS.items():
        if agent_name in members:
            return tier
    return "free"


# Maximum characters for embedded review data (issue body + diff)
# レビュアーの最小コンテキスト200k中、プロンプト等で40k消費 → 残り160k
# 安全マージンを取って128k chars（英語コードなら≒128kトークン）
MAX_EMBED_CHARS = 512 * 1024
MAX_DIFF_CHARS = 50_000

ALLOWED_REVIEWERS = list(AGENTS.keys())

# フェーズ別タイムアウト (秒)。0 = タイムアウトなし
BLOCK_TIMERS = {
    "DESIGN_PLAN":    1800,  # 30分
    "DESIGN_REVIEW":  3600,  # 60分
    "DESIGN_REVISE":  1800,  # 30分
    "IMPLEMENTATION": 5400,  # 90分
    "CODE_REVIEW":    3600,  # 60分
    "CODE_REVISE":    1800,  # 30分
}

# タイムアウト延長可能な状態
EXTENDABLE_STATES = {"DESIGN_PLAN", "DESIGN_REVISE", "IMPLEMENTATION", "CODE_REVISE"}

# 状態遷移直後の催促猶予期間（秒）
NUDGE_GRACE_SEC = 300  # 5分

# 残り時間が閾値未満で延長案内を表示（秒）
EXTEND_NOTICE_THRESHOLD = 300  # 5分

# 非アクティブ判定 (秒)
INACTIVE_THRESHOLD_SEC = 303

# /new コマンド後の待ち時間（秒）
POST_NEW_COMMAND_WAIT_SEC = 30

# エージェントセッションストアのベースパス
SESSIONS_BASE = Path.home() / ".openclaw/agents"

# Discord: MのユーザーID（マージサマリー承認者）
M_DISCORD_USER_ID = "1469758184456589550"

# Discord: kaneko-discord bot のユーザーID（自己投稿除外用）
BOT_USER_ID = "1313244623396913212"

# マージサマリーのフッター
MERGE_SUMMARY_FOOTER = "\n---\n✅ このメッセージに「OK」とリプライすると、マージが実行されます。"

# グローバル状態ファイル（PJ 間セッション管理用）
DEVBAR_STATE_PATH = PIPELINES_DIR.parent / "devbar-state.json"

# メトリクス JSONL ファイル（Issue #81）
METRICS_FILE = PIPELINES_DIR.parent / "devbar-metrics.jsonl"


def _validate_reviewer_tiers():
    """Warn if REVIEW_MODES contains reviewers not in REVIEWER_TIERS,
    or if a reviewer appears in multiple tiers."""
    import logging
    logger = logging.getLogger(__name__)

    all_tier_members = set()
    member_to_tiers: dict[str, list[str]] = {}
    for tier, members in REVIEWER_TIERS.items():
        for m in members:
            member_to_tiers.setdefault(m, []).append(tier)
        all_tier_members.update(members)

    # 一意性チェック
    for member, tiers in member_to_tiers.items():
        if len(tiers) > 1:
            logger.warning(
                "[config] Reviewer '%s' appears in multiple tiers: %s. "
                "get_tier() will return the first match (dict order).",
                member, tiers
            )

    for mode_name, config in REVIEW_MODES.items():
        for reviewer in config["members"]:
            if reviewer not in all_tier_members:
                logger.warning(
                    "[config] Reviewer '%s' in mode '%s' not found in REVIEWER_TIERS, will be treated as 'free'",
                    reviewer, mode_name
                )


def review_command(project: str, issue: int, reviewer: str) -> str:
    """レビュー報告コマンド文字列を生成する。単一ソース。

    Args:
        project: プロジェクト名
        issue: Issue番号
        reviewer: レビュアー名

    Returns:
        コピペ可能な devbar review コマンド文字列
    """
    return (
        f'python3 {DEVBAR_CLI} review'
        f' --project {project}'
        f' --issue {issue}'
        f' --reviewer {reviewer}'
        f' --verdict <APPROVE/P0/P1/P2>'
        f' --summary "..."'
    )


_validate_reviewer_tiers()


# ---------------------------------------------------------------------------
# spec mode 基盤 — Issue #49
# ---------------------------------------------------------------------------

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

# 1-C. VALID_STATES / VALID_TRANSITIONS への統合（§2.3: sorted(set(...)) で順序固定）
VALID_STATES = sorted(set(VALID_STATES + SPEC_STATES))

for _state, _targets in SPEC_TRANSITIONS.items():
    _existing = VALID_TRANSITIONS.get(_state, [])
    VALID_TRANSITIONS[_state] = sorted(set(_existing + _targets))

# 1-D. STATE_PHASE_MAP への追加
STATE_PHASE_MAP.update({s: "spec" for s in SPEC_STATES})

# 1-E. spec mode 定数（§3.2）
MAX_SPEC_REVISE_CYCLES: int = 10
MIN_VALID_REVIEWS_BY_MODE: dict[str, int] = {
    "full": 3, "standard": 2, "lite": 2, "min": 1, "lite3": 3, "lite3_woGoogle": 3, "skip": 0,
}
SPEC_REVIEW_TIMEOUT_SEC: int = 1800
SPEC_REVISE_TIMEOUT_SEC: int = 1800
SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC: int = 600
SPEC_ISSUE_PLAN_TIMEOUT_SEC: int = 1800    # §10.2 準拠
SPEC_QUEUE_PLAN_TIMEOUT_SEC: int = 1800    # §10.2 準拠
SPEC_REVISE_SELF_REVIEW_PASSES: int = 2
MAX_SPEC_RETRIES: int = 3
SPEC_REVIEW_RAW_RETENTION_DAYS: int = 30
