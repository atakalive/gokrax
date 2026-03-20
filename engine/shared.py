"""engine/shared.py - watchdog/gokrax共通ユーティリティ"""

import json
from datetime import datetime
from pathlib import Path

import config
from config import LOCAL_TZ, SESSIONS_BASE, INACTIVE_THRESHOLD_SEC


def log(msg: str) -> None:
    """タイムスタンプ付きログをLOG_FILEに書き込む。"""
    ts = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    with open(config.LOG_FILE, "a") as f:
        f.write(line + "\n")


def _is_ok_reply(content: str) -> bool:
    """マージサマリーへのOK返信を判定。ok, OK, おk, おｋ 等に対応。"""
    s = content.strip().lower()
    return s.startswith("ok") or s.startswith("おk") or s.startswith("おｋ")


def _is_cc_running(data: dict) -> bool:
    """パイプラインに記録されたCC PIDが生存中か判定。"""
    pid = data.get("cc_pid")
    if not pid:
        return False
    return Path(f"/proc/{pid}").exists()


def _is_agent_inactive(agent_id: str, pipeline_data: dict | None = None) -> bool:
    """エージェントが非アクティブ(81秒以上更新なし)かどうか判定。

    CC実行中（cc_pid が /proc に存在）はアクティブと判定する。
    """
    if pipeline_data is not None and _is_cc_running(pipeline_data):
        return False
    try:
        path = SESSIONS_BASE / agent_id / "sessions" / "sessions.json"
        data = json.loads(path.read_text())
        session = data.get(f"agent:{agent_id}:main")
        if not session or "updatedAt" not in session:
            return True
        last_active = datetime.fromtimestamp(session["updatedAt"] / 1000, LOCAL_TZ)
        elapsed = (datetime.now(LOCAL_TZ) - last_active).total_seconds()
        return elapsed >= INACTIVE_THRESHOLD_SEC
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return True
