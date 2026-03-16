"""ISSUE_PLAN ステートのプロンプト・通知。

Variables:
    project: str              - プロジェクト名
    spec_path: str            - 仕様書ファイルパス
    current_rev: str          - 現在のリビジョン番号
    suggestions_text: str     - レビュアー提案のYAMLテキスト
    gitlab: str               - GitLabリポジトリ（例: "atakalive/DevBar"）
    spec_filename: str        - 仕様書ファイル名
    DEVBAR_CLI: str           - devbar CLIパス
"""

from prompts._common import no_interrupt_header, no_interrupt_footer


def plan(
    project: str, spec_path: str, current_rev: str,
    suggestions_text: str, gitlab: str, spec_filename: str, DEVBAR_CLI: str,
    **_kw,
) -> str:
    """Issue起票指示プロンプト（§8.1）。"""
    return f"""{no_interrupt_header()}

以下のレビュアー提案を統合して、GitLab Issue を起票せよ。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})

## レビュアー提案
{suggestions_text}
## 統合指示
複数レビュアーの提案を統合し、重複を排除して最終的なIssue一覧を決定せよ。
類似または重複するIssueは1つにまとめ、依存関係を整理せよ。

## 起票ルール
- Issue タイトルには `[spec:{spec_filename}:S-{{N}}]` プレフィックスを付ける（N は連番）。
- `glab issue list -R {gitlab} -O json` で既存Issueを確認し、重複起票を避けろ。
- Issueコメントは使用禁止。
- 各Issueの本文に「期待する振る舞い」と「テスト」セクションを必ず含めろ。
- 起票コマンド: `glab issue create -R {gitlab} --title "..." --description "..." --label "spec-mode"`
- 実装上の注意事項は本文に ⚠️ 注記として記載せよ。
- **[重要] 起票するIssueの冒頭に、仕様書のファイルパスを明記せよ。(例: `仕様書: {spec_path}`)**

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
{DEVBAR_CLI} spec issue-submit --pj {project} --file <YAMLファイルパス>
```

{no_interrupt_footer("Issue起票・完了報告の提出")}"""


# ---------------------------------------------------------------------------
# Discord通知（短文）
# ---------------------------------------------------------------------------

def notify_done(project: str, issue_count: int, **_kw) -> str:
    """ISSUE_PLAN完了。"""
    return f"[Spec] {project}: {issue_count}件 Issue起票完了"
