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


# ---------------------------------------------------------------------------
# spec mode 基盤 — Issue #49
# ---------------------------------------------------------------------------

def default_spec_config() -> dict:
    """spec_config のデフォルト値を生成する（§3.1）。

    数値デフォルトは config 定数を参照する（ハードコード禁止）。
    """
    from config import (
        MAX_SPEC_REVISE_CYCLES,
        SPEC_REVISE_SELF_REVIEW_PASSES,
    )
    return {
        "spec_path": "",
        "spec_implementer": "",
        "review_only": False,
        "no_queue": False,
        "skip_review": False,
        "auto_continue": False,
        "self_review_passes": SPEC_REVISE_SELF_REVIEW_PASSES,
        "self_review_agent": None,
        "current_rev": "1",
        "rev_index": 1,
        "max_revise_cycles": MAX_SPEC_REVISE_CYCLES,
        "revise_count": 0,
        "last_commit": None,
        "model": None,
        "review_requests": {},
        "current_reviews": {},
        "issue_suggestions": {},
        "created_issues": [],
        "review_history": [],
        "force_events": [],
        "retry_counts": {},
        "paused_from": None,
        "pipelines_dir": None,
        "last_changes": None,
    }


def validate_spec_config(spec_config: dict) -> list[str]:
    """spec_config の必須フィールドをバリデーションする。

    Returns:
        エラーメッセージのリスト（空=OK）。
    """
    errors: list[str] = []
    if not spec_config.get("spec_path"):
        errors.append("spec_config.spec_path is required")
    if not spec_config.get("spec_implementer"):
        errors.append("spec_config.spec_implementer is required")
    return errors


def check_spec_mode_exclusive(data: dict) -> None:
    """spec_mode=true の間、通常モードの操作を拒否する（§2.3）。

    Raises:
        SystemExit: spec_mode が有効な場合。
    """
    if data.get("spec_mode"):
        raise SystemExit(
            "Pipeline is in spec mode. Use 'devbar spec' commands, "
            "or finish/abort spec mode first."
        )


def ensure_spec_reviews_dir(project: str) -> Path:
    """spec-reviews ディレクトリを作成して返す。

    パス: PIPELINES_DIR/{project}/spec-reviews/
    パーミッション: 0o700（owner only）
    """
    d = PIPELINES_DIR / project / "spec-reviews"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d
