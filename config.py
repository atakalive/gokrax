"""gokrax config — 定数の一元管理"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from datetime import timezone, timedelta

# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------
OWNER_NAME: str = "M"
PROMPT_LANG: str = "ja"

DRY_RUN: bool = os.environ.get("GOKRAX_DRY_RUN", "").strip() not in ("", "0", "false")

# パス（テスト時は環境変数で上書き可能）
PIPELINES_DIR = Path(os.environ["GOKRAX_PIPELINES_DIR"]) if "GOKRAX_PIPELINES_DIR" in os.environ else Path.home() / ".openclaw/shared/pipelines"
GOKRAX_CLI = PurePosixPath("/home/ataka/.openclaw/shared/bin/gokrax")
GLAB_BIN = "/home/ataka/bin/glab"
# GATEWAY_TOKEN_PATH removed — using direct bot token
GATEWAY_PORT = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789"))
LOG_FILE = Path("/tmp/gokrax-watchdog.log")
QUEUE_FILE = PIPELINES_DIR / "gokrax-queue.txt"  # Default. Don't delete this line.
QUEUE_FILE = Path("/mnt/s/wsl/work/project/gokrax/gokrax-queue.txt")

# デフォルトオプション: start / qrun 開始時に自動適用される。
# 明示的な CLI 引数やキュー行のオプションで上書き可能。
DEFAULT_QUEUE_OPTIONS: dict[str, bool | str] = {
    "skip_cc_plan": True,
    "keep_ctx_intra": True,
}


# watchdog-loop
WATCHDOG_LOOP_SCRIPT = Path(__file__).resolve().parent / "watchdog-loop.sh"
WATCHDOG_LOOP_PIDFILE = Path("/tmp/gokrax-watchdog-loop.pid")
WATCHDOG_LOOP_CRON_MARKER = "watchdog-loop"  # crontab行のgrep用マーカー
WATCHDOG_LOOP_CRON_ENTRY = (
    f"* * * * * flock -n /tmp/gokrax-watchdog-loop.lock"
    f" setsid bash {Path(__file__).resolve().parent / 'watchdog-loop.sh'}"
    f" > /dev/null 2>&1 &"
)

# Discord
DISCORD_CHANNEL = "1474050582049329213"  # #gokrax channel ID
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
MAX_REVISE_CYCLES = 4  # REVISE→REVIEWの最大サイクル数
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
    "IDLE", "INITIALIZE", "TRIAGE",
    "DESIGN_PLAN", "DESIGN_REVIEW", "DESIGN_REVISE", "DESIGN_APPROVED",
    "IMPLEMENTATION",
    "CODE_REVIEW", "CODE_REVISE", "CODE_APPROVED",
    "MERGE_SUMMARY_SENT", "DONE", "BLOCKED",
]

VALID_TRANSITIONS = {
    "TRIAGE": ["IDLE"],
    "IDLE": ["INITIALIZE"],
    "INITIALIZE": ["DESIGN_PLAN"],
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
    "kaneko":   "agent:kaneko:main",    # Opus, Implementer
    "pascal":   "agent:pascal:main",    # Gemini 3 Pro
    "leibniz":  "agent:leibniz:main",   # GPT-4.1 64k-ctx (GitHub)
    "hanfei":   "agent:hanfei:main",    # GPT-4.1 64k-ctx (GitHub)
    "dijkstra": "agent:dijkstra:main",  # Opus
    "neumann":  "agent:neumann:main",   # Opus, Implementer
    "euler":    "agent:euler:main",     # ChatGPT-5.4
    "basho":    "agent:basho:main",     # Local, Qwen3.5-27B 
}

# Reviewer tiers means that their infrastructure capability
# Regular: Stable connection, enough context length
# Free: Limited daily token usage, may be disconnected in workflow. (Author did not test them well)
# Short-context: Shorter context length. Local LLM etc. (64k-ctx model was tested in single issue)
REVIEWER_TIERS: dict[str, list[str]] = {
    "regular": ["dijkstra", "euler", "pascal"],
    "free": [],  # ping-test did not work well so far, not reccommended
    "short-context": ["basho", "hanfei", "leibniz"],
}

# Review modes: project-level reviewer assignment
REVIEW_MODES = {
    "full": {
        "members": ["pascal", "dijkstra", "euler", "basho"],
        "min_reviews": 4,
        "grace_period_sec": 0,
    },
    "standard": {
        "members": ["pascal", "euler", "dijkstra"],
        "min_reviews": 3,
        "grace_period_sec": 0,
    },
    "lite3_woOpus": {
        "members": ["pascal", "euler", "basho"],
        "min_reviews": 3,
        "grace_period_sec": 0,
    },
    "lite3_woGoogle": {
        "members": ["euler", "dijkstra", "basho"],
        "min_reviews": 3,
        "grace_period_sec": 0,
    },
    "lite3_woOpenAI": {
        "members": ["pascal", "dijkstra", "basho"],
        "min_reviews": 3,
        "grace_period_sec": 0,
    },
    "lite": {
        "members": ["basho", "pascal"],
        "min_reviews": 2,
        "grace_period_sec": 0,
    },
    "cheap": {
        "members": ["basho", "leibniz", "hanfei"],
        "min_reviews": 3,
        "grace_period_sec": 0,
    },
    "min": {
        "members": ["pascal"],
        "min_reviews": 1,
        "grace_period_sec": 0,
    },
    "skip": {
        "members": [],
        "min_reviews": 0,
        "grace_period_sec": 0,
    },
}

# スキル定義: スキル名 → 絶対パス
SKILLS: dict[str, str] = {
    "diff-reading-guide": str(Path.home() / ".openclaw/skills/diff-reading-guide/SKILL.md"),
    "code-walkthrough": str(Path.home() / ".openclaw/skills/code-walkthrough/SKILL.md"),
    "forensic-debugging": str(Path.home() / ".openclaw/skills/forensic-debugging/SKILL.md"),
}

# エージェント名 → 使用スキル名のリスト（順序は埋め込み順序）
# AGENTS に存在しないエージェント名を含めてもよい（将来の追加に備えた予約）。
# load_skills はエージェント名の AGENTS 存在チェックを行わない。
AGENT_SKILLS: dict[str, list[str]] = {
    "pascal": ["diff-reading-guide"],
    "dijkstra": ["diff-reading-guide"],
    "euler": ["diff-reading-guide"],
    "leibniz": ["diff-reading-guide"],
    "hanfei": ["diff-reading-guide"],
    "basho": ["diff-reading-guide"],  # Local Reviewer
    "kaneko": [],  # implementer
    "neumann": [],  # implementer
}

# スキルブロック合計の上限（文字数）。超過時は warning + 切り詰め
MAX_SKILL_CHARS: int = 30_000


# diff のハードリミット（OOM 安全弁）。ファイル外部化により送信経路の制限は解消されたが、
# 巨大 commit（数GB）の diff を全文メモリに載せるとプロセスが落ちるため安全弁として残す。
MAX_DIFF_CHARS: int = 5_000_000

# インライン送信の上限バイトサイズ（UTF-8エンコード後）
# これ以上のメッセージはファイル外部化に切り替える
MAX_INLINE_MESSAGE_BYTES: int = 120_000

# レビューデータ外部化のディレクトリ
REVIEW_FILE_DIR: Path = Path("/tmp/gokrax-review")

# ファイル書き出しリトライ設定
REVIEW_FILE_WRITE_RETRIES: int = 3
REVIEW_FILE_WRITE_RETRY_DELAY: float = 2.0

ALLOWED_REVIEWERS = list(AGENTS.keys())

# フェーズ別タイムアウト (秒)。0 = タイムアウトなし
BLOCK_TIMERS = {
    "DESIGN_PLAN":    1800,  # 30 min
    "DESIGN_REVIEW":  3600,  # 60 min
    "DESIGN_REVISE":  1800,  # 30 min
    "IMPLEMENTATION": 7200,  # 120 min
    "CODE_REVIEW":    3600,  # 60 min
    "CODE_REVISE":    1800,  # 30 min
}

# タイムアウト延長可能な状態
EXTENDABLE_STATES = {"DESIGN_PLAN", "DESIGN_REVISE", "IMPLEMENTATION", "CODE_REVISE"}

# 状態遷移直後の催促猶予期間（秒）
NUDGE_GRACE_SEC = 300  # 5 min

# 残り時間が閾値未満で延長案内を表示（秒）
EXTEND_NOTICE_THRESHOLD = 300  # 5 min

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
GOKRAX_STATE_PATH = PIPELINES_DIR.parent / "gokrax-state.json"

# メトリクス JSONL ファイル（Issue #81）
METRICS_FILE = PIPELINES_DIR.parent / "gokrax-metrics.jsonl"


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
    "full": 4, "standard": 3, "lite": 2, "min": 1, "lite3": 3, "lite3_woGoogle": 3, "skip": 0,
}
SPEC_REVIEW_TIMEOUT_SEC: int = 1800
SPEC_REVISE_TIMEOUT_SEC: int = 1800
SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC: int = 600
SPEC_ISSUE_PLAN_TIMEOUT_SEC: int = 1800    # §10.2 準拠
SPEC_QUEUE_PLAN_TIMEOUT_SEC: int = 1800    # §10.2 準拠
SPEC_REVISE_SELF_REVIEW_PASSES: int = 2
MAX_SPEC_RETRIES: int = 3
SPEC_REVIEW_RAW_RETENTION_DAYS: int = 30
