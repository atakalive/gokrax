"""engine/shared.py - watchdog/gokrax共通ユーティリティ"""

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import config
from config import LOCAL_TZ, OPENCLAW_SESSIONS_BASE, INACTIVE_THRESHOLD_SEC


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _visualize_control_char(m: "re.Match[str]") -> str:
    code = ord(m.group(0))
    # U+2400..U+241F は C0 制御文字(0x00..0x1F)の Control Pictures。
    # DEL(0x7F) は U+2421 (␡)。
    return "␡" if code == 0x7f else chr(0x2400 + code)


def sanitize_agent_message(message: str) -> str:
    """エージェント宛 message 中の生 C0/DEL 制御文字（TAB/LF/CR を除く）を
    可視化 Unicode（Control Pictures）へ置換する。

    2つの目的を1関数で担う:
    - argv 直載せ backend（agy/gemini/kimi）の安全化: message を argv(-p)へ
      直接載せる backend で生 NUL による `ValueError: embedded null byte` を防ぐ。
    - 全 backend にわたるレビュアー向け可視化: レビュアーが読む用途なので、
      黙って削除せず可視化して「元コードに制御文字が混入している」事実を残す
      （#387 と同方針）。pi/cc/cci/openclaw は生 NUL を安全に扱えるが、これらの
      受信側はいずれも LLM プロンプトであり NUL/制御文字に意味依存しないため、
      可視化しても互換性の問題はない（むしろ可読性が上がる）。

    冪等: 置換先(U+2400..U+2421)は正規表現クラスの範囲外なので二重適用しても不変。
    前提: message は str。bytes を渡すと re.sub(str パターン)が TypeError になる
    （現行の型注釈で str が保証されている）。
    """
    return _CONTROL_CHAR_RE.sub(_visualize_control_char, message)


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
