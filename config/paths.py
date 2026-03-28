"""config.paths — ファイルパス・ディレクトリ定数"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "PROJECT_ROOT",
    "AGENT_PROFILES_DIR",
    "WATCHDOG_LOOP_SCRIPT", "WATCHDOG_LOOP_PIDFILE", "WATCHDOG_LOOP_LOCKFILE",
    "WATCHDOG_LOOP_CRON_LOCKFILE", "WATCHDOG_LOOP_CRON_MARKER", "WATCHDOG_LOOP_CRON_ENTRY",
    "LOG_FILE",
    "REVIEW_FILE_DIR",
    "SESSIONS_BASE",
    "PI_SESSIONS_DIR",
    "PI_AGENT_CONFIG",
]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = _PROJECT_ROOT
AGENT_PROFILES_DIR = _PROJECT_ROOT / "agents"

# watchdog-loop
WATCHDOG_LOOP_SCRIPT = _PROJECT_ROOT / "watchdog-loop.sh"
WATCHDOG_LOOP_PIDFILE = Path("/tmp/gokrax-watchdog-loop.pid")
WATCHDOG_LOOP_LOCKFILE = Path("/tmp/gokrax-watchdog-loop.lock")
WATCHDOG_LOOP_CRON_LOCKFILE = Path("/tmp/gokrax-cron-spawn.lock")  # for cron (separate from loop.sh lock)
WATCHDOG_LOOP_CRON_MARKER = "watchdog-loop"  # marker for grep in crontab lines
WATCHDOG_LOOP_CRON_ENTRY = (
    f"* * * * * flock -n {WATCHDOG_LOOP_CRON_LOCKFILE}"
    f" setsid bash {_PROJECT_ROOT / 'watchdog-loop.sh'}"
    f" > /dev/null 2>&1 &"
)

LOG_FILE = Path(os.environ.get("GOKRAX_LOG_FILE") or "/tmp/gokrax-watchdog.log")

# レビューデータ外部化のディレクトリ
REVIEW_FILE_DIR: Path = Path("/tmp/gokrax-review")

# エージェントセッションストアのベースパス
SESSIONS_BASE = Path.home() / ".openclaw/agents"

# pi backend session directory
PI_SESSIONS_DIR = _PROJECT_ROOT / ".pi-sessions"

# pi backend per-agent config
PI_AGENT_CONFIG = AGENT_PROFILES_DIR / "config_pi.json"
