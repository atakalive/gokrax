"""devbar config — 定数の一元管理"""

import os
from pathlib import Path
from datetime import timezone, timedelta

# パス（テスト時は環境変数で上書き可能）
PIPELINES_DIR = Path(os.environ["DEVBAR_PIPELINES_DIR"]) if "DEVBAR_PIPELINES_DIR" in os.environ else Path.home() / ".openclaw/shared/pipelines"
DEVBAR_CLI = Path(__file__).parent / "devbar.py"
GLAB_BIN = "/home/ataka/bin/glab"
GATEWAY_TOKEN_PATH = Path.home() / ".openclaw/openclaw.json"
LOG_FILE = Path("/tmp/devbar-watchdog.log")

# Discord
DISCORD_CHANNEL = "1474050582049329213"  # #dev-bar
DISCORD_BOT_ACCOUNT = "kaneko-bot"  # 金子さんの発言として投稿

# タイムゾーン
JST = timezone(timedelta(hours=9))

# パイプライン
MAX_BATCH = 5
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

REVIEWERS = ["pascal", "leibniz", "hanfei", "dijkstra"]
