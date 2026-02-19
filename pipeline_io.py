"""pipeline_io — pipeline JSONの読み書きを一元管理"""

import json
import re
import tempfile
import os
import sys
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    import msvcrt
    import time

    def _lock(f):
        for _ in range(100):
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except (OSError, PermissionError):
                time.sleep(0.05)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f):
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)

from config import PIPELINES_DIR, JST, MAX_HISTORY


def now_iso() -> str:
    return datetime.now(JST).isoformat()


def load_pipeline(path: Path) -> dict:
    """pipeline JSONを読み込む（ロックなし、atomic write前提）。"""
    with open(path) as f:
        return json.load(f)


def _replace_with_retry(src: str, dst: str, retries: int = 20, delay: float = 0.05) -> None:
    """os.replace with retry for Windows PermissionError."""
    for i in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == retries - 1:
                raise
            import time
            time.sleep(delay)


def _atomic_write(path: Path, data: dict) -> None:
    """tmpfile + rename で atomic write。"""
    data["updated_at"] = now_iso()
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_pipeline(path: Path, callback) -> dict:
    """read-modify-write を排他ロック内で一貫実行。

    callback(data: dict) -> None で data を直接変更する。
    ロックファイル: path.with_suffix('.lock')
    """
    lock_path = path.with_suffix(".lock")
    # ロックファイルをアトミックに初期化（競合を回避）
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, b"\0")
        os.close(fd)
    except FileExistsError:
        pass
    with open(lock_path, "r+b") as lock_f:
        _lock(lock_f)
        try:
            data = load_pipeline(path)
            callback(data)
            _atomic_write(path, data)
            return data
        finally:
            _unlock(lock_f)


def save_pipeline(path: Path, data: dict) -> None:
    """単純な書き込み（初期化用）。"""
    _atomic_write(path, data)


def add_history(data: dict, from_s: str, to_s: str, actor: str = "cli") -> None:
    history = data.setdefault("history", [])
    history.append({
        "from": from_s, "to": to_s, "at": now_iso(), "actor": actor,
    })
    if len(history) > MAX_HISTORY:
        data["history"] = history[-MAX_HISTORY:]


_PROJECT_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def get_path(project: str) -> Path:
    """プロジェクト名をバリデーションしてパスを返す。"""
    if not _PROJECT_RE.match(project):
        raise SystemExit(
            f"Invalid project name: '{project}' "
            f"(allowed: alphanumeric, hyphen, underscore)"
        )
    path = (PIPELINES_DIR / f"{project}.json").resolve()
    if not str(path).startswith(str(PIPELINES_DIR.resolve())):
        raise SystemExit(f"Path traversal detected: {project}")
    return path


def find_issue(batch: list, issue_num: int) -> dict | None:
    for i in batch:
        if i.get("issue") == issue_num:
            return i
    return None
