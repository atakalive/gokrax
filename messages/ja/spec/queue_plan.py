"""QUEUE_PLAN ステートのプロンプト・通知。

Variables:
    project: str          - プロジェクト名
    spec_path: str        - 仕様書ファイルパス
    issues_text: str      - 起票済みIssue番号（スペース区切り）
    queue_file_path: str  - キューファイルパス
    DEVBAR_CLI: str       - devbar CLIパス
"""



def plan(
    project: str, spec_path: str, issues_text: str, queue_file_path: str, DEVBAR_CLI: str,
    **_kw,
) -> str:
    """キュー生成指示プロンプト（§9）。"""
    return f"""【指示】このタスクは中断せず最後まで一気に完了してください。途中で確認を求めないこと。

起票済みIssueをバッチ実行キューに登録してください。

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
- review_mode は full / lite から選択。簡単で間違いそうにない場合、lite でよい。

### 実装フェーズで使用するCCモデルは、問題の難易度に応じて選択する。
- デフォルト: Sonnet （指定不要）
- 計画は難しいが実装はSonnetで十分な場合、`plan=opus` のみ指定。
- Opusで計画・実装まで行うほうが良い場合: `plan=opus` および `impl=opus`

### コンテキスト引き継ぎは、必要に応じて指定可能。実装作業は、DESIGN_REVIEW->IMPLEMENTATION->CODE_REVIEW と進行する。
- `--keep-ctx-intra` はDESIGNレビュー -> CODEレビューの間でコンテキストを引き継ぐ場合に付与
- `--keep-ctx-batch` は前バッチのCODEレビュー -> 次バッチのDESIGNレビューにコンテキストを引き継ぐ場合に付与
- `--keep-ctx-all` は batch, intra 両方のコンテキストを引き継ぐ場合に付与（つまりコンテキストリセット無し）

- 依存関係がある場合は別バッチに分ける
- 並列実行可能で、簡単なIssueは同じ行にまとめる
- 難しいタスクではコスト節約しすぎないこと。メリハリをつける。

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
{DEVBAR_CLI} spec queue-submit --pj {project} --file <YAMLファイルパス>
```

【重要】キュー登録・完了報告の提出まで、中断せず一気に完了すること。"""


# ---------------------------------------------------------------------------
# Discord通知
# ---------------------------------------------------------------------------

def notify_done(project: str, batch_count: int, **_kw) -> str:
    """QUEUE_PLAN完了。"""
    return f"[Spec] {project}: {batch_count}バッチ キュー生成完了"
