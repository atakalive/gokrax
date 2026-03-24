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
OWNER_NAME: str = "User"
PROMPT_LANG: str = "en"

DRY_RUN: bool = os.environ.get("GOKRAX_DRY_RUN", "").strip() not in ("", "0", "false")

# パス（テスト時は環境変数で上書き可能）
PIPELINES_DIR = Path(os.environ["GOKRAX_PIPELINES_DIR"]) if "GOKRAX_PIPELINES_DIR" in os.environ else Path.home() / ".openclaw/shared/pipelines"
GOKRAX_CLI = PurePosixPath("")
GLAB_BIN: str = "glab"
GITLAB_NAMESPACE: str = "YOUR_NAMESPACE"  # i.e., gitlab.com/YOUR_NAMESPACE/ProjectName/
GATEWAY_PORT: int = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789"))
QUEUE_FILE = PROJECT_ROOT / "gokrax-queue.txt"

# Discord (must be set in settings.py)
DISCORD_CHANNEL: str = ""
DISCORD_BOT_TOKEN: str = ""

DEFAULT_QUEUE_OPTIONS: dict[str, bool | str] = {}

# cmd_start で DEFAULT_QUEUE_OPTIONS 適用後に None→False 正規化する bool オプションキーの一覧。
# DEFAULT_QUEUE_OPTIONS に含まれないキーでも、後続コードが bool を期待するものはここに含める。
NONE_TO_FALSE_KEYS: tuple[str, ...] = (
    "keep_ctx_batch",
    "keep_ctx_intra",
    "p2_fix",
    "skip_cc_plan",
    "skip_test",
    "skip_assess",
    "skip_design",
)

# CC model
CC_MODEL_PLAN = "sonnet"     # DESIGN_PLAN フェーズ
CC_MODEL_IMPL = "sonnet"   # IMPLEMENTATION フェーズ

# Timezone
LOCAL_TZ = timezone(timedelta(hours=9))

# パイプライン


# タイムアウト (seconds)
AGENT_SEND_TIMEOUT = 30
DISCORD_POST_TIMEOUT = 10
GLAB_TIMEOUT = 15

# スキル定義
SKILLS: dict[str, str] = {}
AGENT_SKILLS: dict[str, dict[str, list[str]]] = {}
PROJECT_SKILLS: dict[str, dict[str, list[str]]] = {}
PROJECT_RISK_FILES: dict[str, str] = {}
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

REVIEWERS: list[str] = []
IMPLEMENTERS: list[str] = []
AGENTS: dict[str, str] = {}
MASK_AGENT_NAMES: bool = True

REVIEWER_TIERS: dict[str, list[str]] = {"regular": [], "free": [], "short-context": []}
REVIEW_MODES: dict[str, dict] = {}

# 非アクティブ判定 (秒)
INACTIVE_THRESHOLD_SEC = 303

# /new コマンド後の待ち時間（秒）
POST_NEW_COMMAND_WAIT_SEC = 30

# Discord
MERGE_APPROVER_DISCORD_ID: str = ""
ALLOWED_COMMAND_USER_IDS: tuple[str, ...] = ()
ANNOUNCE_BOT_USER_ID: str = ""
COMMAND_BOT_USER_ID: str = ""

# マージサマリーのフッター
MERGE_SUMMARY_FOOTER = "\n---\n✅ Reply \"OK\" to this message to execute the merge."

# グローバル状態ファイル（PJ 間セッション管理用）
GOKRAX_STATE_PATH = PIPELINES_DIR.parent / "gokrax-state.json"

# メトリクス JSONL ファイル（Issue #81）
METRICS_FILE = PIPELINES_DIR.parent / "gokrax-metrics.jsonl"

# ---------------------------------------------------------------------------
# CODE_TEST ゲート — Issue #87
# ---------------------------------------------------------------------------
MAX_TEST_RETRY: int = 4

TEST_CONFIG: dict = {
    "myproject": {
        "test_command": "cd /path/to/project && python3 -m pytest -x --tb=short",
        "test_timeout": 300,
    },
}

# ---------------------------------------------------------------------------
# User settings override (settings.py)
# ---------------------------------------------------------------------------
import importlib.util as _importlib_util  # noqa: E402


def _validate_review_modes(review_modes: dict, reviewers: list[str]) -> None:
    """REVIEW_MODES のフェーズ上書きをバリデーションする。起動時に呼ばれる。"""
    _VALID_PHASE_KEYS = {"design", "code"}
    _VALID_OVERRIDE_FIELDS = {"members", "min_reviews", "n_pass", "grace_period_sec"}
    for mode_name, mode_cfg in review_modes.items():
        for key in mode_cfg:
            if key in _VALID_PHASE_KEYS:
                override = mode_cfg[key]
                if not isinstance(override, dict):
                    raise ValueError(
                        f"REVIEW_MODES['{mode_name}']['{key}'] must be a dict, got {type(override).__name__}"
                    )
                unknown = set(override.keys()) - _VALID_OVERRIDE_FIELDS
                if unknown:
                    raise ValueError(
                        f"REVIEW_MODES['{mode_name}']['{key}'] has unknown keys: {unknown}. "
                        f"Valid keys: {sorted(_VALID_OVERRIDE_FIELDS)}"
                    )
                if "members" in override:
                    unknown_members = set(override["members"]) - set(reviewers)
                    if unknown_members:
                        raise ValueError(
                            f"REVIEW_MODES['{mode_name}']['{key}']['members'] contains "
                            f"unknown reviewers: {unknown_members}. "
                            f"All members must be in REVIEWERS."
                        )


if os.environ.get("GOKRAX_SKIP_USER_SETTINGS", "").strip().lower() not in ("", "0", "false"):
    # テスト用: settings.py を読み込まず、config デフォルト値のみで動作
    pass
else:
    _settings_path = Path(os.environ["GOKRAX_SETTINGS"]) if "GOKRAX_SETTINGS" in os.environ else Path(__file__).resolve().parent.parent / "settings.py"
    if not _settings_path.exists():
        raise FileNotFoundError(
            f"settings.py not found at {_settings_path}. "
            "Run: cp settings.example.py settings.py"
        )
    if _settings_path.exists():
        _spec = _importlib_util.spec_from_file_location("_gokrax_settings", _settings_path)
        _settings_mod = _importlib_util.module_from_spec(_spec)
        _spec.loader.exec_module(_settings_mod)
        for _attr in dir(_settings_mod):
            if _attr.isupper() and not _attr.startswith("_"):
                globals()[_attr] = getattr(_settings_mod, _attr)
        del _spec, _settings_mod, _attr

        # --- 派生変数の再計算 ---

        # 1. 後方互換バリデーション: REVIEWERS/IMPLEMENTERS が未定義の場合フェイルファスト
        if not REVIEWERS and not IMPLEMENTERS:
            raise RuntimeError(
                "settings.py に REVIEWERS と IMPLEMENTERS が定義されていません。\n"
                "settings.py を更新してください:\n"
                '  REVIEWERS = ["reviewer1", "reviewer2"]\n'
                '  IMPLEMENTERS = ["impl1"]\n'
                "詳細は settings.example.py を参照。"
            )

        # 2. REVIEWERS / IMPLEMENTERS 重複チェック
        _overlap = set(REVIEWERS) & set(IMPLEMENTERS)
        if _overlap:
            raise ValueError(
                f"REVIEWERS と IMPLEMENTERS に重複があります: {_overlap}\n"
                "同一エージェントを両方のリストに含めることはできません。"
            )
        del _overlap

        # 3. AGENTS 自動生成（settings.py に AGENTS 明示定義がない場合のみ）
        if not AGENTS:
            AGENTS = {name: f"agent:{name}:main" for name in REVIEWERS + IMPLEMENTERS}

        # 4. REVIEW_MODES フェーズ上書きバリデーション
        _validate_review_modes(REVIEW_MODES, REVIEWERS)

        GOKRAX_STATE_PATH = PIPELINES_DIR.parent / "gokrax-state.json"
        METRICS_FILE = PIPELINES_DIR.parent / "gokrax-metrics.jsonl"

del _importlib_util
