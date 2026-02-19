"""pipeline_io — pipeline JSONの読み書きを一元管理"""

import json
import tempfile
import os
import sys
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    import msvcrt

    def _lock(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f):
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)

from config import PIPELINES_DIR, JST


def now_iso() -> str:
    return datetime.now(JST).isoformat()


def load_pipeline(path: Path) -> dict:
    """pipeline JSONを読み込む（ロックなし、atomic write前提）。"""
    with open(path) as f:
        return json.load(f)


def _atomic_write(path: Path, data: dict) -> None:
    """tmpfile + rename で atomic write。"""
    data["updated_at"] = now_iso()
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
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
    # ロックファイルが存在しなければ1バイト書き込んで作成
    if not lock_path.exists():
        lock_path.write_bytes(b"\0")
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
    data.setdefault("history", []).append({
        "from": from_s, "to": to_s, "at": now_iso(), "actor": actor,
    })


def get_path(project: str) -> Path:
    return PIPELINES_DIR / f"{project}.json"


def find_issue(batch: list, issue_num: int) -> dict | None:
    for i in batch:
        if i.get("issue") == issue_num:
            return i
    return None
