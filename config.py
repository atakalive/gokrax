"""devbar config — 定数の一元管理"""

import os
from pathlib import Path, PurePosixPath
from datetime import timezone, timedelta

DRY_RUN: bool = os.environ.get("DEVBAR_DRY_RUN", "").strip() not in ("", "0", "false")

# パス（テスト時は環境変数で上書き可能）
PIPELINES_DIR = Path(os.environ["DEVBAR_PIPELINES_DIR"]) if "DEVBAR_PIPELINES_DIR" in os.environ else Path.home() / ".openclaw/shared/pipelines"
DEVBAR_CLI = PurePosixPath("/home/ataka/.openclaw/shared/bin/devbar")
GLAB_BIN = "/home/ataka/bin/glab"
GATEWAY_TOKEN_PATH = Path.home() / ".openclaw/openclaw.json"
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
DISCORD_BOT_ACCOUNT = "kaneko-discord"  # 金子さんの発言として投稿

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
VALID_VERDICTS = ["APPROVE", "P0", "P1", "REJECT"]
VALID_FLAG_VERDICTS = ["P0", "P1"]

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
    "kaneko":   "agent:kaneko:main",
    "pascal":   "agent:pascal:main",
    "leibniz":  "agent:leibniz:main",
    "hanfei":   "agent:hanfei:main",
    "dijkstra": "agent:dijkstra:main",
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
        "members": ["pascal", "leibniz", "dijkstra"],
        "min_reviews": 3,
        "grace_period_sec": 0,
    },
    "standard": {
        "members": ["pascal", "leibniz", "hanfei"],
        "min_reviews": 2,
        "grace_period_sec": 300,
    },
    "lite": {
        "members": ["leibniz", "pascal"],
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

# Reviewer tiers: regular, semi, free
REVIEWER_TIERS: dict[str, list[str]] = {
    "regular": ["leibniz", "dijkstra"],
    "semi": ["pascal"],
    "free": ["hanfei"],
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
NUDGE_GRACE_SEC = 180  # 3分

# 残り時間が閾値未満で延長案内を表示（秒）
EXTEND_NOTICE_THRESHOLD = 300  # 5分

# 非アクティブ判定 (秒)
INACTIVE_THRESHOLD_SEC = 181

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


def _validate_reviewer_tiers():
    """Warn if REVIEW_MODES contains reviewers not in REVIEWER_TIERS."""
    import logging
    logger = logging.getLogger(__name__)

    all_tier_members = set()
    for members in REVIEWER_TIERS.values():
        all_tier_members.update(members)

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
        f' --verdict <APPROVE/P0/P1>'
        f' --summary "..."'
    )


_validate_reviewer_tiers()
