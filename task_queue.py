"""task_queue.py — gokrax タスクキューの管理

循環 import 回避のため、gokrax.py と watchdog.py の両方から import される純粋関数群。
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
from config import REVIEW_MODES, resolve_queue_options, GITLAB_NAMESPACE, MAX_BATCH


class QueueSkipError(Exception):
    """qrun でエントリを復元せずスキップすべき永続的エラー。"""

# resolve_queue_options() が返す dict のキーを内部キーにマッピングする。
# "key=value" 形式: キー名を内部キーに変換し、value（= の右辺）を値として使う。
# "key" 形式（value が str）: キー名を内部キーに変換し、dict の value を値として使う。
# 同一エイリアスの重複（パターン A/B 混在）は dict 挿入順で後勝ち。
_QUEUE_OPT_ALIASES: dict[str, str] = {
    "impl": "cc_impl_model",
    "plan": "cc_plan_model",
    "no-cc": "no_cc",
}


def sanitize_comment(raw: str) -> str | None:
    """コメント文字列をサニタイズして返す。空なら None。"""
    s = raw.strip()
    # 改行を半角スペースに正規化
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    # Discord @メンション抑止: @ を @\u200b に置換（@everyone/@here 等の誤爆防止）
    s = s.replace("@", "@\u200b")
    # Markdown コードブロック崩れ抑止: ``` を `\u200b`` に置換
    s = s.replace("```", "`\u200b``")
    return s if s else None


def parse_queue_line(line: str) -> dict:
    """キュー行を1行パースする。

    形式: PROJECT ISSUES [MODE] [OPTIONS...]
    ISSUES: "all" または カンマ区切り数値 (例: "1,2,3")
    MODE: full / standard / lite / min / skip (省略可)
    OPTIONS:
        automerge        — M承認待ちスキップ
        plan=MODEL       — CC Plan段階のモデル指定
        impl=MODEL       — CC Implementation段階のモデル指定
        comment=TEXT     — バッチへの注意事項（末尾専用: comment= 以降の全テキストがコメントになる）

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

    # インラインコメント除去: 空白+# 以降を除去
    stripped = re.sub(r'\s+#.*', '', stripped).strip()

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
        if any(not p.strip() for p in parts):  # check for empty elements
            raise ValueError(f"Invalid issues format (empty element): {issues_raw!r}")
        if any(not p.strip().isdigit() for p in parts):  # numeric check
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
        "p2_fix": False,
        "cc_plan_model": None,
        "cc_impl_model": None,
        "comment": None,
        "skip_cc_plan": False,
        "skip_test": False,
        "skip_assess": False,
        "skip_design": False,
        "no_cc": False,
        "exclude_high_risk": False,
        "exclude_any_risk": False,
        "allow_closed": False,
        "original_line": line.rstrip("\n"),
    }
    result["_explicit_keys"] = set()

    i = 2
    while i < len(tokens):
        raw_token = tokens[i]
        # ハイフン/アンダーバー正規化: "=" 左辺のみ（右辺はモデル名やコメント等なので変換しない）
        # REVIEW_MODES にアンダーバー含みキーが存在するため、raw_token が REVIEW_MODES に
        # マッチする場合は変換せずそのまま使う。それ以外は従来通り _ → - 変換。
        if "=" in raw_token:
            lhs, rhs = raw_token.split("=", 1)
            token = lhs.replace("_", "-") + "=" + rhs
        else:
            if raw_token in REVIEW_MODES:
                token = raw_token
            else:
                token = raw_token.replace("_", "-")
        if token.startswith("comment="):
            if result["comment"] is not None:
                raise ValueError("Duplicate comment= token")
            # comment= 以降の残り全てを結合（貪欲パース）
            comment_parts = [token.split("=", 1)[1]]
            comment_parts.extend(tokens[i + 1:])
            raw_comment = " ".join(comment_parts)
            result["comment"] = sanitize_comment(raw_comment)
            break  # everything after comment= is a comment, break loop
        elif token == "automerge":
            if result.get("_seen_no_automerge"):
                raise ValueError("automerge and no-automerge cannot be used together")
            result["automerge"] = True
            result["_explicit_keys"].add("automerge")
            result["_seen_automerge"] = True
        elif token == "no-automerge":
            if result.get("_seen_automerge"):
                raise ValueError("automerge and no-automerge cannot be used together")
            result["automerge"] = False
            result["_explicit_keys"].add("automerge")
            result["_seen_no_automerge"] = True
        elif token == "keep-ctx-batch":
            result["keep_ctx_batch"] = True
            result["_explicit_keys"].add("keep_ctx_batch")
        elif token == "keep-ctx-intra":
            result["keep_ctx_intra"] = True
            result["_explicit_keys"].add("keep_ctx_intra")
        elif token in ("keep-ctx-all", "keep-context"):
            result["keep_ctx_batch"] = True
            result["keep_ctx_intra"] = True
            result["_explicit_keys"].update(("keep_ctx_batch", "keep_ctx_intra"))
        elif token == "keep-ctx-none":
            result["keep_ctx_batch"] = False
            result["keep_ctx_intra"] = False
            result["_explicit_keys"].update(("keep_ctx_batch", "keep_ctx_intra"))
        elif token.startswith("plan="):
            result["cc_plan_model"] = token.split("=", 1)[1]
            result["_explicit_keys"].add("cc_plan_model")
        elif token == "p2-fix":
            result["p2_fix"] = True
            result["_explicit_keys"].add("p2_fix")
        elif token.startswith("impl="):
            result["cc_impl_model"] = token.split("=", 1)[1]
            result["_explicit_keys"].add("cc_impl_model")
        elif token == "skip-cc-plan":
            result["skip_cc_plan"] = True
            result["_explicit_keys"].add("skip_cc_plan")
        elif token == "no-skip-cc-plan":
            result["skip_cc_plan"] = False
            result["_explicit_keys"].add("skip_cc_plan")
        elif token == "skip-test":
            result["skip_test"] = True
            result["_explicit_keys"].add("skip_test")
        elif token == "no-skip-test":
            result["skip_test"] = False
            result["_explicit_keys"].add("skip_test")
        elif token == "skip-assess":
            result["skip_assess"] = True
            result["_explicit_keys"].add("skip_assess")
        elif token == "no-skip-assess":
            result["skip_assess"] = False
            result["_explicit_keys"].add("skip_assess")
        elif token == "skip-design":
            result["skip_design"] = True
            result["_explicit_keys"].add("skip_design")
        elif token == "no-skip-design":
            result["skip_design"] = False
            result["_explicit_keys"].add("skip_design")
        elif token == "no-cc":
            result["no_cc"] = True
            result["_explicit_keys"].add("no_cc")
        elif token == "no-no-cc":
            result["no_cc"] = False
            result["_explicit_keys"].add("no_cc")
        elif token == "exclude-high-risk":
            result["exclude_high_risk"] = True
            result["_explicit_keys"].add("exclude_high_risk")
        elif token == "no-exclude-high-risk":
            result["exclude_high_risk"] = False
            result["_explicit_keys"].add("exclude_high_risk")
        elif token == "exclude-any-risk":
            result["exclude_any_risk"] = True
            result["_explicit_keys"].add("exclude_any_risk")
        elif token == "no-exclude-any-risk":
            result["exclude_any_risk"] = False
            result["_explicit_keys"].add("exclude_any_risk")
        elif token == "allow-closed":
            result["allow_closed"] = True
            result["_explicit_keys"].add("allow_closed")
        elif token in REVIEW_MODES:
            if result["mode"] is not None:
                raise ValueError(f"Duplicate mode: already {result['mode']!r}, got {token!r}")
            result["mode"] = token
        else:
            raise ValueError(f"Unknown token in queue line: {token!r}")
        i += 1

    result.pop("_seen_automerge", None)
    result.pop("_seen_no_automerge", None)
    explicit = result.pop("_explicit_keys")
    resolved = resolve_queue_options(result["project"])
    for key, default_val in resolved.items():
        if "=" in key:
            # パターン A: "impl=opus": True → lhs="impl", rhs="opus"
            if not default_val:
                continue
            lhs, rhs = key.split("=", 1)
            if not rhs:
                continue
            internal_key = _QUEUE_OPT_ALIASES.get(lhs)
            if internal_key and internal_key in result and internal_key not in explicit:
                result[internal_key] = rhs
        else:
            internal_key = _QUEUE_OPT_ALIASES.get(key, key)
            if internal_key not in result:
                continue
            if internal_key in explicit:
                continue
            result[internal_key] = default_val
    result["_explicit_keys"] = explicit
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


def _find_next_idle_candidate_readonly(queue_path: Path) -> tuple[int, str, dict] | None:
    """LOCK_EX|LOCK_NB を取り、最初の IDLE 候補行を探して (line_idx, original_line, entry) を返す。

    ファイルは書き換えない。候補なし / ロック取得失敗 / ファイル不存在 → None。
    """
    if not queue_path.exists():
        return None
    with open(queue_path, "r") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        try:
            lines = f.readlines()
            for i, line in enumerate(lines):
                if line.strip().startswith("# done:"):
                    continue
                try:
                    entry = parse_queue_line(line)
                except ValueError:
                    continue
                project = entry["project"]
                try:
                    pipeline_path = get_path(project)
                    if not pipeline_path.exists():
                        continue
                    data = load_pipeline(pipeline_path)
                    if data.get("state", "IDLE") != "IDLE":
                        continue
                except Exception:
                    continue
                return (i, line, entry)
            return None
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _mark_line_done_if_matches(queue_path: Path, original_line: str) -> bool:
    """content-based match で `original_line` と一致する active 行を "# done:" 化する。

    True: 該当 active 行を done 化した。
    False: 既に done 化 / 行消失 / ロック取得失敗 → 次候補を探すべき。
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
            target_stripped = original_line.rstrip("\n")
            for i, line in enumerate(lines):
                if line.strip().startswith("# done:"):
                    continue
                if line.rstrip("\n") == target_stripped:
                    lines[i] = f"# done: {line}"
                    f.seek(0)
                    f.truncate()
                    f.writelines(lines)
                    f.flush()
                    os.fsync(f.fileno())
                    return True
            return False
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def pop_next_queue_entry(queue_path: Path) -> Optional[dict]:
    """キューから次の実行可能エントリを pop（crash-safe 3-phase 版）。

    Phase A (read-only, locked): 候補行特定。
    Phase B (unlocked): glab API で closed Issue スキップ判定。
    Phase C (commit, locked): content-based revalidation で該当行を done 化。
    Phase D (unlocked): Discord 通知。
    """
    closed_skipped: list[tuple[str, list[int]]] = []
    unverified_entries: list[tuple[str, list[int]]] = []
    overflow_skipped: list[tuple[str, int]] = []
    result: Optional[dict] = None

    while True:
        found = _find_next_idle_candidate_readonly(queue_path)
        if found is None:
            break
        _line_idx, original_line, entry = found

        if entry.get("allow_closed", False) or entry.get("issues") == "all":
            if _mark_line_done_if_matches(queue_path, original_line):
                result = entry
                break
            continue

        project = entry["project"]
        try:
            data = load_pipeline(get_path(project))
        except Exception:
            if _mark_line_done_if_matches(queue_path, original_line):
                result = entry
                break
            continue

        gitlab = data.get("gitlab", f"{GITLAB_NAMESPACE}/{project}")

        issues_list = entry["issues"].split(",")
        if len(issues_list) > MAX_BATCH:
            print(
                f"[queue] Skipped entry: {len(issues_list)} issues exceeds MAX_BATCH={MAX_BATCH}",
                file=sys.stderr,
            )
            if _mark_line_done_if_matches(queue_path, original_line):
                overflow_skipped.append((project, len(issues_list)))
            continue

        from engine.glab import fetch_issue_state
        closed_nums: list[int] = []
        unverified_nums: list[int] = []
        for num_str in entry["issues"].split(","):
            try:
                num = int(num_str)
            except ValueError:
                # parse_queue_line で数値検証済みのはずだが、契約崩壊時の safety net
                continue
            state = fetch_issue_state(num, gitlab)
            if state == "closed":
                closed_nums.append(num)
            elif state is None:
                unverified_nums.append(num)

        if closed_nums:
            if _mark_line_done_if_matches(queue_path, original_line):
                closed_skipped.append((project, closed_nums))
            continue

        if _mark_line_done_if_matches(queue_path, original_line):
            if unverified_nums:
                unverified_entries.append((project, unverified_nums))
            result = entry
            break
        continue

    if closed_skipped or unverified_entries or overflow_skipped:
        from notify import post_discord
        from config import DISCORD_CHANNEL
        if DISCORD_CHANNEL:
            for pj, nums in closed_skipped:
                nums_str = ", ".join(f"#{n}" for n in nums)
                post_discord(DISCORD_CHANNEL,
                             f"⚠️ Queue entry skipped: {pj} {nums_str} (closed)")
            for pj, nums in unverified_entries:
                nums_str = ", ".join(f"#{n}" for n in nums)
                post_discord(DISCORD_CHANNEL,
                             f"⚠️ Queue entry proceeded with unverified state: {pj} {nums_str}")
            for pj, count in overflow_skipped:
                post_discord(DISCORD_CHANNEL,
                             f"⚠️ Queue entry skipped: {pj} ({count} issues exceeds MAX_BATCH={MAX_BATCH})")

    return result


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
                    content = stripped[7:].strip()  # strip "# done: " prefix
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


def save_queue_options_to_pipeline(data: dict, entry: dict) -> None:
    """キューエントリのオプションを pipeline dict に書き込む。

    - queue_mode は呼び出し元が事前に設定済みであること（本関数では設定しない）。
    - 非真値のフィールドは書き込みも削除もしない（truthy な項目のみ上書き）。
      cleanup 時に engine/cleanup.py の _cleanup_batch_state() が全フィールドを削除する。
    """
    if "automerge" in entry:
        data["automerge"] = entry["automerge"]
    if entry.get("p2_fix"):
        data["p2_fix"] = True
    if entry.get("cc_plan_model"):
        data["cc_plan_model"] = entry["cc_plan_model"]
    if entry.get("cc_impl_model"):
        data["cc_impl_model"] = entry["cc_impl_model"]
    if entry.get("comment"):
        data["comment"] = entry["comment"]
    if entry.get("skip_cc_plan"):
        data["skip_cc_plan"] = True
    if entry.get("skip_test"):
        data["skip_test"] = True
    if entry.get("skip_assess"):
        data["skip_assess"] = True
    if entry.get("skip_design"):
        data["skip_design"] = True
    if entry.get("no_cc"):
        data["no_cc"] = True
    if entry.get("exclude_high_risk"):
        data["exclude_high_risk"] = True
    if entry.get("exclude_any_risk"):
        data["exclude_any_risk"] = True
    if entry.get("allow_closed"):
        data["allow_closed"] = True


def rollback_queue_mode(path: Path) -> None:
    """cmd_start() 失敗時に queue_mode を pipeline から除去する。

    qrun の CLI / Discord 両経路の except ブロックから呼ばれる共通ヘルパー。
    キューファイル操作ではなく pipeline JSON の更新を行う。
    task_queue.py に配置するのは cmd_qrun / _handle_qrun の共通ロジックを
    集約する目的であり、save_queue_options_to_pipeline と対になる関数である。
    """
    from pipeline_io import update_pipeline

    def _rollback(data: dict) -> None:
        data.pop("queue_mode", None)
    update_pipeline(path, _rollback)


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
    issues_raw = entry.get("issues", "")
    if issues_raw != "all":
        parts = issues_raw.split(",")
        if len(parts) > MAX_BATCH:
            raise ValueError(
                f"Too many issues ({len(parts)}) exceeds MAX_BATCH={MAX_BATCH}"
            )
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


def replace_entry(queue_path: Path, index: int | str, new_line: str) -> Optional[dict]:
    """キューファイルの指定インデックスの active エントリを新しい行で置換する。

    Args:
        queue_path: キューファイルのパス
        index: int (0始まり) または str ("last" / "-1")
        new_line: 置換する新しい行

    Returns:
        置換後エントリの dict、範囲外や空キューなら None

    Raises:
        ValueError: new_line が不正な場合（ファイル操作より先に発生）
    """
    # 1. バリデーションが常に最初
    entry = parse_queue_line(new_line)

    # 2. ファイル不存在 → None
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

            line_no, _ = active[target_idx]
            lines[line_no] = new_line.rstrip("\n") + "\n"

            f.seek(0)
            f.truncate()
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())

            return entry
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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
