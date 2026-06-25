"""engine/shared.py - watchdog/gokrax共通ユーティリティ"""

import json
import subprocess
from datetime import datetime
from pathlib import Path

import config
from config import LOCAL_TZ, OPENCLAW_SESSIONS_BASE, INACTIVE_THRESHOLD_SEC


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


def _is_agent_inactive_openclaw(agent_id: str) -> bool:
    """OpenClaw-specific inactivity check (session JSON mtime).

    Does NOT check cc_pid; the caller is responsible for that.
    """
    try:
        path = OPENCLAW_SESSIONS_BASE / agent_id / "sessions" / "sessions.json"
        data = json.loads(path.read_text())
        session = data.get(f"agent:{agent_id}:main")
        if not session or "updatedAt" not in session:
            return True
        last_active = datetime.fromtimestamp(session["updatedAt"] / 1000, LOCAL_TZ)
        elapsed = (datetime.now(LOCAL_TZ) - last_active).total_seconds()
        return elapsed >= INACTIVE_THRESHOLD_SEC
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return True


def _is_agent_inactive(agent_id: str, pipeline_data: dict | None = None) -> bool:
    """Return whether the agent is inactive, dispatching to the selected backend.

    CC running (cc_pid alive in /proc) is treated as active for all backends.
    """
    from engine.backend import is_inactive as _dispatch_is_inactive
    return _dispatch_is_inactive(agent_id, pipeline_data)


def working_tree_dirty(repo_path: str, timeout: int = 10) -> list[str]:
    """tracked な未コミット変更のパス一覧を返す。clean なら空リスト。

    untracked (?? 始まり) と ignored (!! 始まり) はビルド成果物等の誤検知を避けるため除外する。
    判定不能（git 非0終了・例外）のときは空リストを返す（fail-safe: 通知しない側に倒す）が、
    その場合は必ず log() で可視化する（サイレント障害を作らない）。
    """
    try:
        proc = subprocess.run(
            ["git", "-C", repo_path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as e:
        # subprocess.TimeoutExpired / OSError(FileNotFoundError 含む) / その他すべて
        log(f"working_tree_dirty: git status error (repo={repo_path}): {e}")
        return []

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        log(
            f"working_tree_dirty: git status failed "
            f"(rc={proc.returncode}, repo={repo_path}): {stderr}"
        )
        return []

    dirty: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        if status == "??" or status == "!!":
            continue
        path = line[3:].strip()
        if path:
            dirty.append(path)
    return dirty
