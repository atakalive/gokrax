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
VALID_VERDICTS = ["APPROVE", "P0", "P1", "REJECT"]

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
    "DESIGN_REVIEW": ["DESIGN_APPROVED", "DESIGN_REVISE"],
    "DESIGN_REVISE": ["DESIGN_REVIEW"],
    "DESIGN_APPROVED": ["IMPLEMENTATION"],
    "IMPLEMENTATION": ["CODE_REVIEW"],
    "CODE_REVIEW": ["CODE_APPROVED", "CODE_REVISE"],
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
        "members": ["pascal", "leibniz", "hanfei", "dijkstra"],
        "min_reviews": 3,
    },
    "standard": {
        "members": ["pascal", "leibniz", "hanfei"],
        "min_reviews": 2,
    },
    "lite": {
        "members": ["pascal", "leibniz"],
        "min_reviews": 2,
    },
    "skip": {
        "members": [],
        "min_reviews": 0,
    },
}

# Maximum characters for embedded review data (issue body + diff)
MAX_EMBED_CHARS = 64 * 1024  # 64KB (?) charsのバイト数による

ALLOWED_REVIEWERS = list(AGENTS.keys())

# 非アクティブ判定 (秒)
INACTIVE_THRESHOLD_SEC = 151

# /new コマンド後の待ち時間（秒）
POST_NEW_COMMAND_WAIT_SEC = 10

# エージェントセッションストアのベースパス
SESSIONS_BASE = Path.home() / ".openclaw/agents"

# Discord: MのユーザーID（マージサマリー承認者）
M_DISCORD_USER_ID = "1469758184456589550"

# マージサマリーのフッター
MERGE_SUMMARY_FOOTER = "\n---\n✅ このメッセージに「OK」とリプライすると、マージが実行されます。"

# グローバル状態ファイル（PJ 間セッション管理用）
DEVBAR_STATE_PATH = PIPELINES_DIR.parent / "devbar-state.json"
