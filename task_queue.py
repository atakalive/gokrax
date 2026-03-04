"""task_queue.py — devbar タスクキューの管理

循環 import 回避のため、devbar.py と watchdog.py の両方から import される純粋関数群。
Note: Python 標準ライブラリの queue モジュールとの衝突を避けるため task_queue.py とした。
"""

import fcntl
import os
import re
import tempfile
import sys
from pathlib import Path
from typing import Optional

from pipeline_io import load_pipeline, get_path
from config import REVIEW_MODES


def parse_queue_line(line: str) -> dict:
    """キュー行を1行パースする。

    形式: PROJECT ISSUES [MODE] [OPTIONS...]
    ISSUES: "all" または カンマ区切り数値 (例: "1,2,3")
    MODE: full / standard / lite / min / skip (省略可)
    OPTIONS:
        automerge        — M承認待ちスキップ
        plan=MODEL       — CC Plan段階のモデル指定
        impl=MODEL       — CC Implementation段階のモデル指定

    Args:
        line: キューファイルの1行

    Returns:
        パース結果の dict（original_line キーを含む）

    Raises:
        ValueError: 無効行（空行/コメント/done行含む）、トークン数不足、
                    不正トークン、issues形式不正、MODE重複
    """
    # 前後の空白を除去
    stripped = line.strip()

    # 空行・コメント行
    if not stripped or stripped.startswith("#"):
        raise ValueError(f"Skip line (empty or comment): {line!r}")

    # トークン分割
    tokens = stripped.split()
    if len(tokens) < 2:
        raise ValueError(f"Invalid queue line (need PROJECT ISSUES): {line!r}")

    project = tokens[0]
    issues_raw = tokens[1]

    # プロジェクト名バリデーション (get_path が SystemExit を投げる)
    try:
        get_path(project)
    except SystemExit:
        raise ValueError(f"Unknown project: {project!r}")

    # issues バリデーション
    if issues_raw == "all":
        issues = "all"
    else:
        parts = issues_raw.split(",")
        if any(not p.strip() for p in parts):  # 空要素チェック
            raise ValueError(f"Invalid issues format (empty element): {issues_raw!r}")
        if any(not p.strip().isdigit() for p in parts):  # 数値チェック
            raise ValueError(f"Invalid issues format (non-integer): {issues_raw!r}")
        issues = issues_raw

    # オプションパース
    result = {
        "project": project,
        "issues": issues,
        "mode": None,
        "automerge": False,
        "keep_ctx_batch": False,
        "keep_ctx_intra": False,
        "cc_plan_model": None,
        "cc_impl_model": None,
        "original_line": line.rstrip("\n"),
    }

    for token in tokens[2:]:
        if token == "automerge":
            result["automerge"] = True
        elif token == "keep-ctx-batch":
            result["keep_ctx_batch"] = True
        elif token == "keep-ctx-intra":
            result["keep_ctx_intra"] = True
        elif token in ("keep-ctx-all", "keep-context"):
            result["keep_ctx_batch"] = True
            result["keep_ctx_intra"] = True
        elif token.startswith("plan="):
            result["cc_plan_model"] = token.split("=", 1)[1]
        elif token.startswith("impl="):
            result["cc_impl_model"] = token.split("=", 1)[1]
        elif token in REVIEW_MODES:
            if result["mode"] is not None:
                raise ValueError(f"Duplicate mode: already {result['mode']!r}, got {token!r}")
            result["mode"] = token
        else:
            raise ValueError(f"Unknown token in queue line: {token!r}")

    return result


def _find_active_lines(lines: list[str]) -> list[tuple[int, dict]]:
    """全行から active エントリを抽出する。

    Args:
        lines: ファイルの全行リスト（readlines() の戻り値）

    Returns:
        list of (line_index, parsed_entry_dict)。
        done 行（"# done:" prefix）・コメント行・空行はスキップ。
    """
    result = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# done:"):
            continue
        try:
            entry = parse_queue_line(line)
            result.append((i, entry))
        except ValueError:
            continue
    return result


def pop_next_queue_entry(queue_path: Path) -> Optional[dict]:
    """キューファイルから次の実行可能エントリをpopする。

    1. fcntl.flock(LOCK_EX) でロック取得
    2. 有効行を上から順に探す
    3. 対象PJがIDLEかチェック (非IDLEならスキップして次行へ)
    4. 見つかったら "# done: " prefix に書き換え
    5. 見つからなければ None

    Args:
        queue_path: キューファイルのパス

    Returns:
        実行可能エントリの dict、または None
    """
    if not queue_path.exists():
        return None

    with open(queue_path, "r+") as f:
        # ファイルロック取得 (non-blocking: 取れなければ即 None)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        try:
            lines = f.readlines()
            modified = False
            result = None

            for i, line in enumerate(lines):
                # "# done:" prefix がある行はスキップ
                if line.strip().startswith("# done:"):
                    continue

                # パース（ValueError = 無効行、スキップ）
                try:
                    entry = parse_queue_line(line)
                except ValueError:
                    continue

                # IDLE チェック
                project = entry["project"]
                try:
                    pipeline_path = get_path(project)
                    if not pipeline_path.exists():
                        # Pipeline not found, skip
                        continue
                    data = load_pipeline(pipeline_path)
                    if data.get("state", "IDLE") != "IDLE":
                        # Not IDLE, skip (Head-of-Line Blocking 回避)
                        continue
                except Exception:
                    # エラー時もスキップ
                    continue

                # 実行可能エントリ発見
                lines[i] = f"# done: {line}" if not line.endswith("\n") else f"# done: {line}"
                modified = True
                result = entry
                break

            if not modified:
                return None

            # アトミック書き込み
            f.seek(0)
            f.truncate()
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())

            return result

        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def restore_queue_entry(queue_path: Path, original_line: str) -> bool:
    """cmd_start() 失敗時に "# done: " prefix を削除してエントリを復元する。

    Args:
        queue_path: キューファイルのパス
        original_line: 元の行内容

    Returns:
        復元に成功したら True、該当行が見つからなければ False
    """
    if not queue_path.exists():
        return False

    with open(queue_path, "r+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        try:
            lines = f.readlines()
            modified = False

            for i, line in enumerate(lines):
                # "# done: {original_line}" を探す
                stripped = line.strip()
                if stripped.startswith("# done:"):
                    content = stripped[7:].strip()  # "# done: " を除去
                    if content == original_line.strip():
                        # 復元: "# done: " を削除
                        lines[i] = original_line if original_line.endswith("\n") else f"{original_line}\n"
                        modified = True
                        break

            if not modified:
                return False

            # アトミック書き込み
            f.seek(0)
            f.truncate()
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())

            return True

        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def peek_queue(queue_path: Path) -> list[dict]:
    """キューファイルの全エントリをパースして返す (dry-run 用)。

    ファイル変更なし。"# done:" prefix がある行には done=True フラグを追加。

    Args:
        queue_path: キューファイルのパス

    Returns:
        パース済みエントリのリスト
    """
    if not queue_path.exists():
        return []

    with open(queue_path) as f:
        lines = f.readlines()

    entries = []
    for line in lines:
        stripped = line.strip()
        is_done = stripped.startswith("# done:")

        try:
            if is_done:
                # "# done: " prefix を除去してパース
                actual_line = stripped[7:].strip()
                entry = parse_queue_line(actual_line)
            else:
                entry = parse_queue_line(line)
        except ValueError:
            continue

        entry["done"] = is_done
        entries.append(entry)

    return entries


def get_active_entries(queue_path: Path) -> list[dict]:
    """キューファイルの有効（done=False）エントリを返す。

    各エントリに index キー（0始まり連番）を追加する。

    Args:
        queue_path: キューファイルのパス

    Returns:
        active エントリのリスト
    """
    if not queue_path.exists():
        return []
    with open(queue_path) as f:
        lines = f.readlines()
    active = _find_active_lines(lines)
    result = []
    for i, (_, entry) in enumerate(active):
        entry["index"] = i
        entry["done"] = False
        result.append(entry)
    return result


def append_entry(queue_path: Path, line: str) -> dict:
    """キューファイルの末尾に1行追加する。

    Args:
        queue_path: キューファイルのパス
        line: 追加する行（parse_queue_line でバリデーション）

    Returns:
        パース結果の dict

    Raises:
        ValueError: 行が不正な場合
        FileNotFoundError: キューファイルが存在しない場合
    """
    entry = parse_queue_line(line)  # validate first
    with open(queue_path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0, 2)  # seek to end
            pos = f.tell()
            if pos > 0:
                f.seek(pos - 1)
                if f.read(1) != "\n":
                    f.write("\n")
            f.write(line.rstrip("\n") + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return entry


def delete_entry(queue_path: Path, index: int | str) -> Optional[dict]:
    """キューファイルから指定インデックスの active エントリを削除する。

    Args:
        queue_path: キューファイルのパス
        index: int (0始まり) または str ("last" / "-1")

    Returns:
        削除したエントリの dict、範囲外や空キューなら None
    """
    if not queue_path.exists():
        return None

    with open(queue_path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            lines = f.readlines()
            active = _find_active_lines(lines)

            if not active:
                return None

            # Resolve target
            if isinstance(index, str):
                if index in ("last", "-1"):
                    target_idx = len(active) - 1
                else:
                    try:
                        target_idx = int(index)
                    except ValueError:
                        return None
            else:
                target_idx = index

            if target_idx < 0 or target_idx >= len(active):
                return None

            line_no, entry = active[target_idx]
            del lines[line_no]

            f.seek(0)
            f.truncate()
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())

            return entry
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
