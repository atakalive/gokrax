"""spec_revise.py — SPEC_REVISE フェーズ: 改訂依頼・セルフレビュー・完了検知"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    GLAB_BIN, SPEC_REVIEW_TIMEOUT_SEC, SPEC_REVISE_SELF_REVIEW_PASSES,
    PIPELINES_DIR,
)
from spec_review import (
    _reset_review_requests,
    build_review_history_entry,
    parse_review_yaml,
)

import yaml


# ---------------------------------------------------------------------------
# 1-A. build_revise_prompt
# ---------------------------------------------------------------------------

def build_revise_prompt(
    spec_config: dict,
    merged_report_md: str,
    data: dict,
) -> str:
    """改訂依頼プロンプトを生成する（§6.1）。"""
    project = data.get("project", "")
    spec_path = spec_config.get("spec_path", "")
    current_rev = spec_config.get("current_rev", "1")
    return f"""以下の仕様書を改訂してください。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})

## レビュー統合レポート
{merged_report_md}

## 改訂ルール
- 変更履歴テーブルに1行追加
- `[v{{new_rev}}] 指摘元ID: 説明` 形式で全件列挙
- 擬似コード中 `# [v{{new_rev}}] Pascal C-1: 説明` で変更理由記載
- deferred（保留）する指摘には理由を明記

## 完了報告フォーマット
```yaml
status: done
new_rev: "<現在のrev+1の数値文字列>"
commit: "<7文字以上のgit commit hash>"
changes:
  added_lines: <number>
  removed_lines: <number>
  reflected_items: ["pascal:C-1", ...]
  deferred_items: ["dijkstra:m-4", ...]
  deferred_reasons:
    "dijkstra:m-4": "理由"
```

## 提出方法
完了報告を YAML ファイルに保存し、以下のコマンドで投入してください:
```
python3 /home/ataka/.openclaw/shared/bin/devbar spec revise-submit --pj {project} --file <YAMLファイルパス>
```
"""


# ---------------------------------------------------------------------------
# 1-B. build_self_review_prompt
# ---------------------------------------------------------------------------

def build_self_review_prompt(spec_config: dict, data: dict) -> str:
    """セルフレビュー パス2 の依頼プロンプト（§6.2）。"""
    project = data.get("project", "")
    spec_path = spec_config.get("spec_path", "")
    new_rev = spec_config.get("current_rev", "1")
    last_commit = spec_config.get("last_commit", "unknown")
    return f"""改訂された仕様書のクロスチェックを依頼します。

プロジェクト: {project}
仕様書: {spec_path} (rev{new_rev})
前回commit: {last_commit}

## チェック項目
1. 変更履歴のreflected_itemsが本文に実際に反映されているか
2. 新たな矛盾やregressionが発生していないか
3. 擬似コードの型・引数整合性

変更箇所に問題がなければ `status: clean`、修正が必要なら `status: issues_found` + 指摘リストをYAMLで。"""


# ---------------------------------------------------------------------------
# 1-C. get_self_review_agent
# ---------------------------------------------------------------------------

def get_self_review_agent(spec_config: dict) -> str:
    """セルフレビュー パス2 のエージェントを選択する。

    spec_config.self_review_agent が設定されていればそのエージェント。
    None なら review_requests のキー一覧の先頭。
    """
    agent = spec_config.get("self_review_agent")
    if agent:
        return agent
    review_requests = spec_config.get("review_requests", {})
    if review_requests:
        return next(iter(review_requests))
    return "pascal"  # フォールバック


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

def parse_self_review_response(raw_text: str) -> str:
    """セルフレビュー応答をパースする。

    Returns:
        "clean" | "issues_found" | "parse_failed"
    """
    import re
    match = re.search(r"```ya?ml\s*\n(.*?)```", raw_text, re.DOTALL)
    if not match:
        # YAML ブロックなし → テキスト内に "clean" / "issues_found" を探す
        lower = raw_text.lower()
        if "status: clean" in lower or "status:clean" in lower:
            return "clean"
        if "issues_found" in lower:
            return "issues_found"
        return "parse_failed"
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return "parse_failed"
    if not isinstance(data, dict):
        return "parse_failed"
    status = str(data.get("status", "")).strip().lower()
    if status == "clean":
        return "clean"
    if status == "issues_found":
        return "issues_found"
    return "parse_failed"


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
            f"git diff不一致: git={git_added}+/{git_removed}-, "
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

    # review_history エントリ生成（§12.2）
    history_entry = build_review_history_entry(spec_config, now)

    # 既存 review_history に追加
    review_history = list(spec_config.get("review_history", []))
    review_history.append(history_entry)

    # review_requests リセット差分（§5.4 / Dijkstra P1-2）
    reset_rr: dict[str, dict] = {}
    for reviewer in spec_config.get("review_requests", {}):
        reset_rr[reviewer] = {
            "status": "pending",
            "sent_at": None,
            "timeout_at": None,
            "last_nudge_at": None,
            "response": None,
        }

    return {
        "last_commit": revise_data["commit"],
        "current_rev": str(revise_data["new_rev"]),
        "rev_index": spec_config.get("rev_index", 1) + 1,
        "last_changes": changes,
        "revise_count": spec_config.get("revise_count", 0) + 1,
        "review_history": review_history,
        # current_reviews クリア
        "current_reviews": {"entries": {}},
        # review_requests リセット（§5.4）
        "review_requests_patch": {r: v for r, v in reset_rr.items()},
        # _revise_retry_at クリア
        "_revise_retry_at": None,
    }
