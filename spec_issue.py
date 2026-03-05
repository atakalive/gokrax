"""spec_issue.py — ISSUE_SUGGESTION / ISSUE_PLAN / QUEUE_PLAN フェーズ: プロンプト生成・応答パース"""
from __future__ import annotations

import re

import yaml

from config import QUEUE_FILE

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
    return f"""承認された仕様書に基づき、GitLab Issue への分割提案を行ってください。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})

## 依頼内容
仕様書を実装可能な単位のIssueに分割し、フェーズごとに整理した提案を作成してください。
各Issueは独立して実装・レビューできる単位にしてください。
1 Issue = 1 PR = 1つの明確なゴール。巨大Issueは分割すること。
既存コードの変更量が多い場合は、リファクタリングと機能追加を別Issueにすること。

## 出力フォーマット
```yaml
phases:
  - name: "Phase 1: 基盤実装"
    issues:
      - title: "実装タイトル"
        files:
          - "path/to/file.py"
        lines: "100-200"
        spec_refs:
          - "§6.1"
        depends_on: []
      - title: "別の実装タイトル"
        files:
          - "path/to/other.py"
        lines: ""
        spec_refs:
          - "§7"
        depends_on:
          - "実装タイトル"
  - name: "Phase 2: 統合・テスト"
    issues:
      - title: "統合テスト実装"
        files:
          - "tests/test_foo.py"
        lines: ""
        spec_refs:
          - "§11"
        depends_on:
          - "実装タイトル"
```

## 注意事項
- phases は実装順序を表す（Phase 1 → Phase 2 の順に実装）
- depends_on には同フェーズ内または前フェーズのIssueタイトルを列挙
- files は変更予定ファイルのリスト（既存 or 新規）
- spec_refs は対応する仕様書セクション番号のリスト

## 提出方法
提案を YAML ファイルに保存し、以下のコマンドで投入してください:
```
python3 /home/ataka/.openclaw/shared/bin/devbar spec suggestion-submit --pj {project} --reviewer {reviewer} --file <YAMLファイルパス>
```
"""


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

    spec_name = spec_path.replace("/", "_").replace(".", "_") if spec_path else project
    gitlab = data.get("gitlab", f"atakalive/{project}")

    return f"""以下のレビュアー提案を統合して、GitLab Issue を起票せよ。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})

## レビュアー提案
{suggestions_text}
## 統合指示
複数レビュアーの提案を統合し、重複を排除して最終的なIssue一覧を決定せよ。
類似または重複するIssueは1つにまとめ、依存関係を整理せよ。

## 起票ルール
- Issue タイトルには `[spec:{spec_name}:S-{{N}}]` プレフィックスを付ける（N は連番）。
- `glab issue list -R {gitlab} -O json` で既存Issueを確認し、重複起票を避けろ。
- Issueコメントは使用禁止。
- 各Issueの本文に「期待する振る舞い」と「テスト」セクションを必ず含めろ。
- 起票コマンド: `glab issue create -R {gitlab} --title "..." --description "..." --label "spec-mode"`
- 実装上の注意事項は本文に ⚠️ 注記として記載せよ。
- 各Issueに仕様書参照セクション（spec_refs）を明記せよ。

## 完了報告フォーマット
```yaml
status: done
created_issues:
  - 51
  - 52
  - 53
```

※ created_issues は起票したIssue番号（整数）のリスト

## 提出方法
完了報告を YAML ファイルに保存し、以下のコマンドで投入してください:
```
python3 /home/ataka/.openclaw/shared/bin/devbar spec issue-submit --pj {project} --file <YAMLファイルパス>
```
"""


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

    return f"""起票済みIssueをバッチ実行キューに登録してください。

プロジェクト: {project}
仕様書: {spec_path}
起票済みIssue番号: {issues_text}
キューファイル: {queue_file_path}

## バッチ行フォーマット
```
{{project}} {{issue_nums}} full [--keep-context] # 理由
```

- `issue_nums` はカンマ区切り（例: `{project} 51,52,53 full # Phase 1`）
- 1バッチ内のIssueは並列実装される。依存関係があるIssueは別バッチにすること
- review_mode は full / lite から選択。

実装フェーズで使用するCCモデルは、問題の難易度に応じて選択する。
- デフォルト: Sonnet （指定不要）
- コストをかけてでもOpusで計画・実装するほうががよい場合: `plan=opus` および `impl=opus`
- 計画は難しいが実装は簡単である場合、 `plan=opus` のみ指定してもよい。

コンテキスト引き継ぎは、高コストになるが必要に応じて指定可能。
- `--keep-ctt-batch` は前バッチのコンテキストを引き継ぐ場合に付与
- `--keep-ctt-intra` はDESIGNフェーズから実装後CODEレビューまでコンテキストを引き継ぐ場合に付与
- `--keep-ctx-all` は batch, intra 両方のコンテキストを引き継ぐ場合に付与

- 依存関係がある場合は別バッチに分ける
- 並列実行可能で、簡単なIssueは同じ行にまとめる

## 登録手順
1. 既存のキューファイル（{queue_file_path}）の末尾にバッチ行を追記する
2. Issue番号の依存関係を分析し、適切なバッチ分割を行う

## 完了報告フォーマット
```yaml
status: done
batches: 3
queue_file: "{queue_file_path}"
```

※ batches は追記したバッチ行数（1以上の整数）

## 提出方法
完了報告を YAML ファイルに保存し、以下のコマンドで投入してください:
```
python3 /home/ataka/.openclaw/shared/bin/devbar spec queue-submit --pj {project} --file <YAMLファイルパス>
```
"""


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
