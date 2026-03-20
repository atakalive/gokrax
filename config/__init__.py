"""gokrax config — 定数の一元管理"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath
from datetime import timezone, timedelta

from config.states import *  # noqa: F401,F403
from config.paths import *   # noqa: F401,F403

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
QUEUE_FILE = PIPELINES_DIR / "gokrax-queue.txt"  # Default. Don't delete this line.
QUEUE_FILE = Path("/mnt/s/wsl/work/project/gokrax/gokrax-queue.txt")

# デフォルトオプション: start / qrun 開始時に自動適用される。
# 明示的な CLI 引数やキュー行のオプションで上書き可能。
DEFAULT_QUEUE_OPTIONS: dict[str, bool | str] = {
    "skip_cc_plan": True,
    "keep_ctx_intra": True,
    "skip_test": True,
}

# cmd_start で DEFAULT_QUEUE_OPTIONS 適用後に None→False 正規化する bool オプションキーの一覧。
# DEFAULT_QUEUE_OPTIONS に含まれないキーでも、後続コードが bool を期待するものはここに含める。
NONE_TO_FALSE_KEYS: tuple[str, ...] = (
    "keep_ctx_batch",
    "keep_ctx_intra",
    "p2_fix",
    "skip_cc_plan",
    "skip_test",
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
MIN_REVIEWS = 3

# タイムアウト (seconds)
AGENT_SEND_TIMEOUT = 30
DISCORD_POST_TIMEOUT = 10
GLAB_TIMEOUT = 15

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
def _get_max_cli_arg_bytes() -> int:
    """OS 別の CLI 引数サイズ上限を返す（安全マージン込み）。

    - Linux: MAX_ARG_STRLEN=131,072 (単一引数の上限)
    - macOS: ARG_MAX=1,048,576 (argv+envp+ポインタ+null終端の合計上限。
      単一引数の上限ではない。環境変数が50-100KB消費する典型環境では
      実効的に使える領域は減る)
    - Windows: CreateProcess=32,767文字
    """
    if sys.platform == "darwin":
        return 900_000   # macOS ARG_MAX=1,048,576; ~14% margin (envp消費分に注意)
    elif sys.platform == "win32":
        return 30_000    # Windows CreateProcess=32,767 chars; ~8% margin
    else:
        return 120_000   # Linux MAX_ARG_STRLEN=131,072; ~8% margin

MAX_CLI_ARG_BYTES: int = _get_max_cli_arg_bytes()

# ファイル書き出しリトライ設定
REVIEW_FILE_WRITE_RETRIES: int = 3
REVIEW_FILE_WRITE_RETRY_DELAY: float = 2.0

ALLOWED_REVIEWERS = list(AGENTS.keys())

# 非アクティブ判定 (秒)
INACTIVE_THRESHOLD_SEC = 303

# /new コマンド後の待ち時間（秒）
POST_NEW_COMMAND_WAIT_SEC = 30

# Discord: マージ承認者のユーザーID
MERGE_APPROVER_DISCORD_ID = "1469758184456589550"

# Discord: Discordコマンド実行を許可するユーザーIDリスト
# M個人 + WatcherB bot
ALLOWED_COMMAND_USER_IDS: tuple[str, ...] = (
    MERGE_APPROVER_DISCORD_ID,       # マージ承認者
    "1477531618456637572",   # WatcherB bot
)

# Discord: kaneko-discord bot のユーザーID（自己投稿除外用）
BOT_USER_ID = "1313244623396913212"

# マージサマリーのフッター
MERGE_SUMMARY_FOOTER = "\n---\n✅ このメッセージに「OK」とリプライすると、マージが実行されます。"

# グローバル状態ファイル（PJ 間セッション管理用）
GOKRAX_STATE_PATH = PIPELINES_DIR.parent / "gokrax-state.json"

# メトリクス JSONL ファイル（Issue #81）
METRICS_FILE = PIPELINES_DIR.parent / "gokrax-metrics.jsonl"

# ---------------------------------------------------------------------------
# CODE_TEST ゲート — Issue #87
# ---------------------------------------------------------------------------
MAX_TEST_RETRY: int = 4

TEST_CONFIG: dict[str, dict] = {
    "gokrax": {
        "test_command": "cd /mnt/s/wsl/work/project/gokrax && python3 -m pytest -x --tb=short --ignore=tests/test_review_gitlab_note.py -k 'not (test_apply_spec_nudge_sends_after_inactive_threshold or test_design_revise_max_cycles_transitions_to_blocked or test_code_revise_max_cycles_transitions_to_blocked)'",
        "test_timeout": 300,
    },
    "EMCalibrator": {
        "test_command": "cd /mnt/s/wsl/work/project/EMCalibrator && python3 -m pytest -x --tb=short",
        "test_timeout": 300,
    },
}

# ---------------------------------------------------------------------------
# User settings override (settings.py)
# ---------------------------------------------------------------------------
import importlib.util as _importlib_util  # noqa: E402

_settings_path = Path(__file__).resolve().parent.parent / "settings.py"
if _settings_path.exists():
    _spec = _importlib_util.spec_from_file_location("_gokrax_settings", _settings_path)
    _settings_mod = _importlib_util.module_from_spec(_spec)
    _spec.loader.exec_module(_settings_mod)
    for _attr in dir(_settings_mod):
        if _attr.isupper() and not _attr.startswith("_"):
            globals()[_attr] = getattr(_settings_mod, _attr)
    del _spec, _settings_mod, _attr

    # --- 派生変数の再計算 ---
    ALLOWED_REVIEWERS = list(AGENTS.keys())
    GOKRAX_STATE_PATH = PIPELINES_DIR.parent / "gokrax-state.json"
    METRICS_FILE = PIPELINES_DIR.parent / "gokrax-metrics.jsonl"

del _settings_path, _importlib_util
