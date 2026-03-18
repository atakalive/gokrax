"""ISSUE_SUGGESTION ステートのプロンプト。

Variables:
    project: str       - プロジェクト名
    spec_path: str     - 仕様書ファイルパス
    current_rev: str   - 現在のリビジョン番号
    reviewer: str      - レビュアー名
    GOKRAX_CLI: str    - gokrax CLIパス
"""



def suggestion(
    project: str, spec_path: str, current_rev: str, reviewer: str, GOKRAX_CLI: str,
    **_kw,
) -> str:
    """Issue分割提案プロンプト（§7）。"""
    return f"""【指示】このタスクは中断せず最後まで一気に完了してください。途中で確認を求めないこと。

承認された仕様書に基づき、GitLab Issue への分割提案を行ってください。

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
{GOKRAX_CLI} spec suggestion-submit --pj {project} --reviewer {reviewer} --file <YAMLファイルパス>
```

【重要】提案作成・提出まで、中断せず一気に完了すること。"""
