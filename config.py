"""devbar config — 定数の一元管理"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath
from datetime import timezone, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------
OWNER_NAME: str = "M"
PROMPT_LANG: str = "ja"

DRY_RUN: bool = os.environ.get("DEVBAR_DRY_RUN", "").strip() not in ("", "0", "false")

# パス（テスト時は環境変数で上書き可能）
PIPELINES_DIR = Path(os.environ["DEVBAR_PIPELINES_DIR"]) if "DEVBAR_PIPELINES_DIR" in os.environ else Path.home() / ".openclaw/shared/pipelines"
DEVBAR_CLI = PurePosixPath("/home/ataka/.openclaw/shared/bin/devbar")
GLAB_BIN = "/home/ataka/bin/glab"
# GATEWAY_TOKEN_PATH removed — using direct bot token
GATEWAY_PORT = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789"))
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
# Regular: Good connection, context length, token usage
# Free: Limited daily token usage, unstable connection. (Author did not test them well)
# Short-context: Shorter context length. Local LLM etc. (64k-ctx model might be unstable)
REVIEWER_TIERS: dict[str, list[str]] = {
    "regular": ["dijkstra", "euler", "pascal"],
    "free": [],  # ping-test did not work well so far
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

_logger = logging.getLogger(__name__)

# スキルブロック合計の上限（文字数）。超過時は warning + 切り詰め
MAX_SKILL_CHARS: int = 30_000


def load_skills(agent_name: str) -> str:
    """指定エージェントに紐付けられたスキルファイルを読み込み、結合して返す。

    Args:
        agent_name: AGENT_SKILLS のキー

    Returns:
        スキル内容を結合した文字列。スキルがない場合は空文字列。
        - AGENT_SKILLS にキーがない場合 → 空文字列
        - スキル名が SKILLS に存在しない場合 → warning を出してスキップ
        - ファイル読み込みに失敗した場合 → warning を出してスキップ
        - 結合結果が MAX_SKILL_CHARS を超える場合 → warning を出して切り詰め
    """
    skill_names = AGENT_SKILLS.get(agent_name)
    if not skill_names:
        return ""

    parts: list[str] = []
    for name in skill_names:
        path_str = SKILLS.get(name)
        if path_str is None:
            _logger.warning("load_skills: unknown skill '%s' for agent '%s'", name, agent_name)
            continue
        try:
            content = Path(path_str).read_text(encoding="utf-8").rstrip("\n")
            parts.append(f"--- skill: {name} ---\n{content}")
        except OSError as e:
            _logger.warning("load_skills: failed to read '%s': %s", path_str, e)

    if not parts:
        return ""

    block = "<skills>\n" + "\n\n".join(parts) + "\n</skills>"

    _OPENING_TAG = "<skills>\n"
    _CLOSING_TAG = "\n</skills>"
    _MIN_SKILL_CHARS = len(_OPENING_TAG) + len(_CLOSING_TAG)
    # 不変条件: MAX_SKILL_CHARS >= _MIN_SKILL_CHARS（開始タグ+終了タグの長さ）。
    # これより小さい値を設定した場合、切り詰めではなく空文字列を返す。
    if len(block) > MAX_SKILL_CHARS:
        _logger.warning(
            "load_skills: skill block for '%s' exceeds %d chars (%d), truncating",
            agent_name, MAX_SKILL_CHARS, len(block),
        )
        if MAX_SKILL_CHARS < _MIN_SKILL_CHARS:
            return ""
        # 切り詰め後も closing tag を含めて MAX_SKILL_CHARS 以下を保証
        content_limit = MAX_SKILL_CHARS - len(_CLOSING_TAG)
        block = block[:content_limit] + _CLOSING_TAG

    return block

def get_tier(agent_name: str) -> str:
    """Return tier for agent. Unknown agents are conservatively marked as 'free'."""
    for tier, members in REVIEWER_TIERS.items():
        if agent_name in members:
            return tier
    return "free"


# diff のハードリミット（OOM 安全弁）。ファイル外部化により送信経路の制限は解消されたが、
# 巨大 commit（数GB）の diff を全文メモリに載せるとプロセスが落ちるため安全弁として残す。
MAX_DIFF_CHARS: int = 5_000_000

# インライン送信の上限バイトサイズ（UTF-8エンコード後）
# これ以上のメッセージはファイル外部化に切り替える
MAX_INLINE_MESSAGE_BYTES: int = 120_000

# レビューデータ外部化のディレクトリ
REVIEW_FILE_DIR: Path = Path("/tmp/devbar-review")

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


def get_current_round(data: dict[str, Any]) -> int:
    """現在のレビューラウンド番号を返す（1起算）。

    DESIGN_REVIEW/DESIGN_REVISE → design_revise_count + 1
    CODE_REVIEW/CODE_REVISE → code_revise_count + 1
    その他の状態 → 0（ラウンド検証をスキップさせる）

    注: "DESIGN" / "CODE" を含む状態名で判定するため、DESIGN_PLAN 等の
    非レビュー状態でも非0を返す。ただし cmd_review の state チェックで
    REVIEW/REVISE 以外は事前に弾かれるため、実害はない。
    """
    state = data.get("state", "IDLE")
    if "DESIGN" in state:
        return data.get("design_revise_count", 0) + 1
    elif "CODE" in state:
        return data.get("code_revise_count", 0) + 1
    return 0


def review_command(project: str, issue: int, reviewer: str, round_num: int | None = None) -> str:
    """レビュー報告コマンド文字列を生成する。単一ソース。"""
    cmd = (
        f'python3 {DEVBAR_CLI} review'
        f' --project {project}'
        f' --issue {issue}'
        f' --reviewer {reviewer}'
        f' --verdict <APPROVE/P0/P1/P2>'
        f' --summary "..."'
    )
    if round_num is not None:
        cmd += f' --round {round_num}'
    return cmd


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
