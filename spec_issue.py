"""spec_issue.py — ISSUE_SUGGESTION / ISSUE_PLAN / QUEUE_PLAN フェーズ: プロンプト生成・応答パース"""
from __future__ import annotations

import re

import yaml

from config import DEVBAR_CLI, QUEUE_FILE
from messages import render

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

_YAML_BLOCK_RE = re.compile(
    r"```ya?ml\s*\n(.*?)```",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# 1-A. build_issue_suggestion_prompt（§7）
# ---------------------------------------------------------------------------

def build_issue_suggestion_prompt(spec_config: dict, data: dict, reviewer: str = "") -> str:
    """ISSUE_SUGGESTION フェーズ: レビュアー向けIssue分割提案プロンプトを生成する（§7）。"""
    project = data.get("project", "")
    spec_path = spec_config.get("spec_path", "")
    current_rev = spec_config.get("current_rev", "1")
    return render("spec.issue_suggestion", "suggestion",
        project=project, spec_path=spec_path, current_rev=current_rev,
        reviewer=reviewer, DEVBAR_CLI=str(DEVBAR_CLI),
    )


# ---------------------------------------------------------------------------
# 1-B. parse_issue_suggestion_response（§7）
# ---------------------------------------------------------------------------

def parse_issue_suggestion_response(raw_text: str) -> dict | None:
    """レビュアーのIssue分割提案をパースする（§7）。

    Returns:
        成功: {"phases": [...]} の dict
        失敗: None
    """
    match = _YAML_BLOCK_RE.search(raw_text)
    if not match:
        return None

    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None

    if not isinstance(data, dict):
        return None

    phases = data.get("phases")
    if not isinstance(phases, list):
        return None
    if len(phases) == 0:
        return None

    for phase in phases:
        if not isinstance(phase, dict):
            return None
        name = phase.get("name")
        if not isinstance(name, str) or not name:
            return None
        issues = phase.get("issues")
        if not isinstance(issues, list) or len(issues) == 0:
            return None
        for issue in issues:
            if not isinstance(issue, dict):
                return None
            title = issue.get("title")
            if not isinstance(title, str) or not title:
                return None

    return data


# ---------------------------------------------------------------------------
# 1-C. build_issue_plan_prompt（§8.1）
# ---------------------------------------------------------------------------

def build_issue_plan_prompt(spec_config: dict, data: dict) -> str:
    """ISSUE_PLAN フェーズ: implementer 向けIssue起票プロンプトを生成する（§8.1）。"""
    project = data.get("project", "")
    spec_path = spec_config.get("spec_path", "")
    current_rev = spec_config.get("current_rev", "1")
    issue_suggestions: dict = spec_config.get("issue_suggestions", {})

    # 各レビュアーの提案を整形
    suggestions_text = ""
    for reviewer, suggestion in issue_suggestions.items():
        suggestions_text += f"### {reviewer}\n"
        suggestions_text += yaml.dump(suggestion, allow_unicode=True, default_flow_style=False)
        suggestions_text += "\n"

    spec_filename = spec_path.split("/")[-1] if spec_path else "spec"
    gitlab = data.get("gitlab", f"atakalive/{project}")

    return render("spec.issue_plan", "plan",
        project=project, spec_path=spec_path, current_rev=current_rev,
        suggestions_text=suggestions_text, gitlab=gitlab,
        spec_filename=spec_filename, DEVBAR_CLI=str(DEVBAR_CLI),
    )


# ---------------------------------------------------------------------------
# 1-D. parse_issue_plan_response（§8.1）
# ---------------------------------------------------------------------------

def parse_issue_plan_response(raw_text: str) -> dict | None:
    """implementer のIssue起票完了報告をパースする（§8.1）。

    Returns:
        成功: {"status": "done", "created_issues": [int, ...]} の dict
        失敗: None
    """
    match = _YAML_BLOCK_RE.search(raw_text)
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

    created_issues = data.get("created_issues")
    if not isinstance(created_issues, list) or len(created_issues) == 0:
        return None

    for v in created_issues:
        # int 型のみ受理（bool は除外、数値文字列 "51" は不可）
        if not isinstance(v, int) or isinstance(v, bool):
            return None

    return data


# ---------------------------------------------------------------------------
# 1-E. build_queue_plan_prompt（§9）
# ---------------------------------------------------------------------------

def build_queue_plan_prompt(spec_config: dict, data: dict) -> str:
    """QUEUE_PLAN フェーズ: implementer 向けキュー生成プロンプトを生成する（§9）。"""
    project = data.get("project", "")
    spec_path = spec_config.get("spec_path", "")
    created_issues: list = spec_config.get("created_issues", [])
    queue_file_path = str(QUEUE_FILE)

    issues_text = " ".join(str(n) for n in created_issues)

    return render("spec.queue_plan", "plan",
        project=project, spec_path=spec_path, issues_text=issues_text,
        queue_file_path=queue_file_path, DEVBAR_CLI=str(DEVBAR_CLI),
    )


# ---------------------------------------------------------------------------
# 1-F. parse_queue_plan_response（§9）
# ---------------------------------------------------------------------------

def parse_queue_plan_response(raw_text: str) -> dict | None:
    """implementer のキュー生成完了報告をパースする（§9）。

    Returns:
        成功: {"status": "done", "batches": int, "queue_file": str} の dict
        失敗: None
    """
    match = _YAML_BLOCK_RE.search(raw_text)
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

    batches = data.get("batches")
    # int 型かつ bool でなく、1 以上であること（float/str 不可）
    if not isinstance(batches, int) or isinstance(batches, bool) or batches < 1:
        return None

    queue_file = data.get("queue_file")
    if not isinstance(queue_file, str) or not queue_file:
        return None

    return data
