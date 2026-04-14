"""spec_revise.py — SPEC_REVISE フェーズ: 改訂依頼・セルフレビュー・完了検知"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    GLAB_BIN, SPEC_BLOCK_TIMERS, SPEC_REVISE_SELF_REVIEW_PASSES,
    PIPELINES_DIR, GOKRAX_CLI,
)
from spec_review import (
    _reset_review_requests,
    build_review_history_entry,
    parse_review_yaml,
)

import yaml


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

DEFAULT_SELF_REVIEW_CHECKLIST: list[dict] = [
    {
        "id": "reflected_items_match",
        "question": "Whether reflected_items in the revise report are actually reflected in the spec body",
    },
    {
        "id": "no_new_contradictions",
        "question": "Whether the current changes introduced new contradictions",
    },
    {
        "id": "pseudocode_consistency",
        "question": "Whether pseudocode types, arguments, and function names match the body description",
    },
    {
        "id": "deferred_reasons_valid",
        "question": "Whether deferred_items have explicit reasons",
    },
]


# ---------------------------------------------------------------------------
# ユーティリティ: revN ファイル名操作
# ---------------------------------------------------------------------------

def make_rev_path(spec_path: str, new_rev: int) -> str:
    """spec_path のファイル名に -revN サフィックスを付与/更新して返す。

    Args:
        spec_path: 元の仕様書パス（絶対パス）。空文字列は ValueError。
        new_rev: 新しい revision 番号（正の整数）。

    Returns:
        -revN サフィックスを付与/更新した新しいパス文字列。

    Raises:
        ValueError: spec_path が空文字列の場合。

    例:
      make_rev_path("/abs/docs/foo-spec.md", 2) -> "/abs/docs/foo-spec-rev2.md"
      make_rev_path("/abs/docs/foo-spec-rev3.md", 4) -> "/abs/docs/foo-spec-rev4.md"
    """
    if not spec_path:
        raise ValueError("spec_path must not be empty")
    p = Path(spec_path)
    stem = re.sub(r"-rev\d+$", "", p.stem)
    return str(p.with_name(f"{stem}-rev{new_rev}{p.suffix}"))


def extract_rev_from_path(spec_path: str) -> int | None:
    """spec_path のファイル名から -revN の N を抽出する。

    Returns:
        revN の N（int、1以上）。-revN サフィックスがないか N < 1 なら None。

    例:
      extract_rev_from_path("/abs/docs/foo-spec-rev4.md") -> 4
      extract_rev_from_path("/abs/docs/foo-spec.md") -> None
      extract_rev_from_path("/abs/docs/foo-spec-rev0.md") -> None
    """
    m = re.search(r"-rev(\d+)$", Path(spec_path).stem)
    if not m:
        return None
    n = int(m.group(1))
    return n if n >= 1 else None


# ---------------------------------------------------------------------------
# 1-A. build_revise_prompt
# ---------------------------------------------------------------------------

def build_revise_prompt(
    spec_config: dict,
    merged_report_md: str,
    data: dict,
) -> str:
    """改訂依頼プロンプトを生成する（§6.1）。"""
    from messages import render
    project = data.get("project", "")
    spec_path = spec_config.get("spec_path", "")
    if not spec_path:
        raise ValueError("spec_config['spec_path'] is empty; cannot build revise prompt")
    current_rev = spec_config.get("current_rev", "1")
    rev_index = spec_config.get("rev_index", 1)
    next_rev = rev_index + 1
    new_spec_path = make_rev_path(spec_path, next_rev)
    return render("spec.revise", "revise",
        project=project, spec_path=spec_path, current_rev=current_rev,
        GOKRAX_CLI=GOKRAX_CLI, next_rev=next_rev, new_spec_path=new_spec_path,
        merged_report_md=merged_report_md,
    )


# ---------------------------------------------------------------------------
# 1-B. build_self_review_prompt
# ---------------------------------------------------------------------------

def build_self_review_prompt(
    spec_config: dict,
    data: dict,
    checklist: list[dict] | None = None,
) -> str:
    """セルフレビュー依頼プロンプト（チェックリスト方式）。

    Args:
        spec_config: 現在の spec_config
        data: pipeline data（project 等）
        checklist: チェックリスト定義。None の場合は DEFAULT_SELF_REVIEW_CHECKLIST を使用。
                   spec_config に self_review_checklist キーがあればそちらを優先。
    """
    from messages import render
    project = data.get("project", "")
    spec_path = spec_config.get("spec_path", "")
    new_rev = spec_config.get("current_rev", "1")
    last_commit = spec_config.get("last_commit", "unknown")

    if checklist is None:
        checklist = spec_config.get("self_review_checklist", DEFAULT_SELF_REVIEW_CHECKLIST)

    checklist_lines = []
    for item in checklist:
        checklist_lines.append(f'- **{item["id"]}**: {item["question"]}')
    checklist_text = "\n".join(checklist_lines)

    example_items = []
    for item in checklist:
        example_items.append(
            f'  - id: "{item["id"]}"\n'
            f'    result: "Yes"\n'
            f'    evidence: "(describe verification details)"'
        )
    example_yaml = "checklist:\n" + "\n".join(example_items)

    return render("spec.revise", "self_review",
        project=project, spec_path=spec_path, current_rev=new_rev,
        GOKRAX_CLI=GOKRAX_CLI, last_commit=last_commit,
        checklist_text=checklist_text, example_yaml=example_yaml,
    )


# ---------------------------------------------------------------------------
# 1-C. get_self_review_agent
# ---------------------------------------------------------------------------

def get_self_review_agent(spec_config: dict) -> str:
    """セルフレビュー担当エージェントを選択する。

    優先順位:
    1. spec_config.self_review_agent が設定されていればそのエージェント
    2. spec_config.spec_implementer（改訂担当者 = セルフレビューの本来の実行者）
    3. フォールバック: review_requests のキー一覧の先頭
    """
    agent = spec_config.get("self_review_agent")
    if agent:
        return agent
    # セルフレビューは implementer が自分の改訂を検証するもの (#112)
    implementer = spec_config.get("spec_implementer")
    if implementer:
        return implementer
    review_requests = spec_config.get("review_requests", {})
    if review_requests:
        return next(iter(review_requests))
    from config import IMPLEMENTERS  # function-level import (to avoid circular imports)
    if IMPLEMENTERS:
        return IMPLEMENTERS[0]
    raise RuntimeError("get_self_review_agent: no implementer configured (IMPLEMENTERS is empty)")


# ---------------------------------------------------------------------------
# 1-D. parse_revise_response
# ---------------------------------------------------------------------------

def parse_revise_response(raw_text: str, current_rev: str = "1") -> dict | None:
    """改訂完了報告の YAML をパースする（Leibniz P0-3 強化版）。

    検証項目:
    - status == "done"
    - new_rev: 数値文字列かつ current_rev + 1 と一致
    - commit: 7文字以上の hex 文字列
    - changes: dict で added_lines/removed_lines が非負整数

    Returns:
        パース成功時は dict（status, new_rev, commit, changes）。
        失敗時は None。
    """
    import re
    match = re.search(r"```ya?ml\s*\n(.*?)```", raw_text, re.DOTALL)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("status") != "done":
        return None

    # new_rev: 数値性 + 単調増加チェック
    new_rev = str(data.get("new_rev", ""))
    try:
        new_rev_int = int(new_rev)
    except ValueError:
        return None
    try:
        current_rev_int = int(current_rev)
    except ValueError:
        current_rev_int = 0
    if new_rev_int != current_rev_int + 1:
        return None

    # commit: 7文字以上の hex
    commit = str(data.get("commit", ""))
    if len(commit) < 7 or not re.fullmatch(r"[0-9a-fA-F]+", commit):
        return None

    # changes: dict + 必須キー
    changes = data.get("changes", {})
    if not isinstance(changes, dict):
        return None
    for key in ("added_lines", "removed_lines"):
        val = changes.get(key)
        if not isinstance(val, int) or val < 0:
            return None

    return data


# ---------------------------------------------------------------------------
# 1-E. parse_self_review_response
# ---------------------------------------------------------------------------

def parse_self_review_response(
    raw_text: str,
    expected_ids: list[str] | None = None,
) -> dict:
    """セルフレビュー応答をパースする（チェックリスト方式）。

    Args:
        raw_text: エージェントからの応答テキスト
        expected_ids: 期待するチェックリストID一覧。None の場合は
                      DEFAULT_SELF_REVIEW_CHECKLIST の id を使用。

    Returns:
        {
            "verdict": "clean" | "issues_found" | "parse_failed",
            "items": [  # verdict が "issues_found" の場合のみ有効
                {"id": "...", "result": "No", "evidence": "..."},
                ...
            ]
        }
    """
    import re

    _fail: dict = {"verdict": "parse_failed", "items": []}

    # YAML ブロック抽出（テキストフォールバックなし）
    match = re.search(r"```ya?ml\s*\n(.*?)```", raw_text, re.DOTALL)
    if not match:
        return _fail

    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return _fail

    if not isinstance(data, dict):
        return _fail

    items_raw = data.get("checklist")
    if not isinstance(items_raw, list):
        return _fail

    # expected_ids 決定
    if expected_ids is None:
        expected_ids = [c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST]

    # ID 集合の完全一致チェック（unhashable id 防御: Leibniz P0）
    response_ids = []
    for item in items_raw:
        if not isinstance(item, dict):
            return _fail
        rid = item.get("id")
        if not isinstance(rid, str):
            return _fail
        response_ids.append(rid)

    try:
        if set(response_ids) != set(expected_ids):
            return _fail
    except TypeError:
        return _fail

    # 重複 ID チェック
    if len(response_ids) != len(set(response_ids)):
        return _fail

    # 各項目を検証
    no_items = []
    for item in items_raw:
        if not isinstance(item, dict):
            return _fail
        result_raw = str(item.get("result", "")).strip().lower()
        if result_raw not in ("yes", "no"):
            return _fail
        evidence = item.get("evidence", "")
        if not isinstance(evidence, str):
            return _fail
        if result_raw == "no":
            no_items.append({
                "id": item["id"],
                "result": "No",
                "evidence": evidence,
            })

    if no_items:
        return {"verdict": "issues_found", "items": no_items}
    return {"verdict": "clean", "items": []}


# ---------------------------------------------------------------------------
# 1-F. verify_git_diff
# ---------------------------------------------------------------------------

def verify_git_diff(
    repo_path: str,
    last_commit: str,
    reported_commit: str,
    spec_path: str,
    reported_changes: dict,
) -> str | None:
    """git diff --numstat で added/removed_lines を検証する（Leibniz P0-2 修正）。

    last_commit..reported_commit の差分を使用（HEAD ではなく報告された commit）。

    Returns:
        不一致時は警告メッセージ文字列。一致時は None。
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--numstat",
             f"{last_commit}..{reported_commit}", "--", spec_path],
            capture_output=True, text=True, timeout=15,
            cwd=repo_path,
        )
        if result.returncode != 0:
            return f"git diff failed: {result.stderr.strip()}"
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return f"git diff error: {e}"

    # numstat format: "added\tremoved\tfile"
    lines = result.stdout.strip().split("\n")
    if not lines or not lines[0].strip():
        return f"git diff returned empty output"

    parts = lines[0].split("\t")
    if len(parts) < 2:
        return f"git diff unexpected format: {lines[0]}"

    try:
        git_added = int(parts[0])
        git_removed = int(parts[1])
    except ValueError:
        return f"git diff non-numeric: {parts[0]}, {parts[1]}"

    reported_added = reported_changes.get("added_lines", 0)
    reported_removed = reported_changes.get("removed_lines", 0)

    if git_added != reported_added or git_removed != reported_removed:
        return (
            f"git diff mismatch: git={git_added}+/{git_removed}-, "
            f"reported={reported_added}+/{reported_removed}-"
        )

    return None


# ---------------------------------------------------------------------------
# 1-G. build_revise_completion_updates
# ---------------------------------------------------------------------------

def build_revise_completion_updates(
    spec_config: dict,
    revise_data: dict,
    now: datetime,
) -> dict:
    """改訂完了時の spec_config 更新差分を構築する（§6.3）。

    Args:
        spec_config: 現在の spec_config（読み取り専用）
        revise_data: parse_revise_response() の結果 dict
        now: 現在時刻

    Returns:
        pipeline_updates dict
    """
    changes = revise_data.get("changes", {})
    new_rev_int = int(revise_data["new_rev"])
    spec_path = spec_config.get("spec_path", "")
    if not spec_path:
        raise ValueError(
            "spec_config['spec_path'] is empty; cannot build revise completion updates"
        )
    new_spec_path = make_rev_path(spec_path, new_rev_int)

    # review_history エントリ生成（§12.2）
    history_entry = build_review_history_entry(spec_config, now)

    # 既存 review_history に追加
    review_history = list(spec_config.get("review_history", []))
    review_history.append(history_entry)

    # review_requests リセット差分（§5.4 / Dijkstra P1-2）
    # APPROVE 済みレビュアーは approved_prior に（1ラウンド限定 #307）
    current_entries = spec_config.get("current_reviews", {}).get("entries", {})
    reset_rr: dict[str, dict] = {}
    for reviewer in spec_config.get("review_requests", {}):
        entry = current_entries.get(reviewer, {})
        if entry.get("verdict") == "APPROVE":
            status = "approved_prior"
        else:
            status = "pending"
        reset_rr[reviewer] = {
            "status": status,
            "sent_at": None,
            "timeout_at": None,
            "last_nudge_at": None,
            "response": None,
        }

    return {
        "spec_path": new_spec_path,
        "last_commit": revise_data["commit"],
        "current_rev": str(revise_data["new_rev"]),
        "rev_index": new_rev_int,  # computed directly from new_rev (prevents drift)
        "last_changes": changes,
        "review_history": review_history,
        # current_reviews クリア
        "current_reviews": {"entries": {}},
        # review_requests リセット（§5.4）
        "review_requests_patch": {r: v for r, v in reset_rr.items()},
        # _revise_retry_at クリア
        "_revise_retry_at": None,
    }
