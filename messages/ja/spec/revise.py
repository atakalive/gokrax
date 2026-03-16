"""SPEC_REVISE ステートのプロンプト・通知・催促。

Variables (common):
    project: str           - プロジェクト名
    spec_path: str         - 仕様書ファイルパス
    current_rev: str       - 現在のリビジョン番号
    DEVBAR_CLI: str        - devbar CLIパス
"""



# ---------------------------------------------------------------------------
# エージェント向けプロンプト
# ---------------------------------------------------------------------------

def revise(
    project: str, spec_path: str, current_rev: str, DEVBAR_CLI: str,
    next_rev: int, new_spec_path: str, merged_report_md: str,
    **_kw,
) -> str:
    """改訂依頼プロンプト（§6.1）。"""
    return f"""【指示】このタスクは中断せず最後まで一気に完了してください。途中で確認を求めないこと。

以下の仕様書を改訂してください。

プロジェクト: {project}
仕様書（現行）: {spec_path} (rev{current_rev})
改訂先ファイル: {new_spec_path} (rev{next_rev})

## 改訂手順
1. 現行仕様書 `{spec_path}` をコピーして `{new_spec_path}` を作成
2. `{new_spec_path}` を編集（改訂内容を反映）
3. `{new_spec_path}` を git add + commit
4. 完了報告を投入

## レビュー統合レポート
{merged_report_md}

## 改訂ルール
- 変更履歴テーブルに1行追加
- `[v{next_rev}] 指摘元ID: 説明` 形式で全件列挙
- 擬似コード中 `# [v{next_rev}] Pascal C-1: 説明` で変更理由記載
- deferred（保留）する指摘には理由を明記

## 完了報告フォーマット
```yaml
status: done
new_rev: "{next_rev}"
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
{DEVBAR_CLI} spec revise-submit --pj {project} --file <YAMLファイルパス>
```

【重要】改訂・コミット・完了報告の提出まで、中断せず一気に完了すること。"""


def self_review(
    project: str, spec_path: str, current_rev: str, DEVBAR_CLI: str,
    last_commit: str, checklist_text: str, example_yaml: str,
    **_kw,
) -> str:
    """セルフレビュー依頼プロンプト。"""
    return f"""【指示】このタスクは中断せず最後まで一気に完了してください。途中で確認を求めないこと。

改訂された仕様書のセルフレビューを依頼します。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})
前回commit: {last_commit}

## チェック項目
{checklist_text}

## 回答フォーマット
以下のYAMLで回答してください。各項目の result は "Yes" または "No" のみ有効です。

```yaml
{example_yaml}
```

result が "No" の場合は evidence に具体的な問題箇所を記述してください。

## 提出方法
チェック結果を YAML ファイルに保存し、以下のコマンドで投入してください:
{DEVBAR_CLI} spec self-review-submit --pj {project} --file <YAMLファイルパス>

※ YAMLブロック（```yaml ... ```）で囲うことを推奨します。囲わなくても CLI が自動でフェンスを補完しますが、確実なパースのため囲ってください。

【重要】チェック完了まで中断せず一気に完了すること。"""


# ---------------------------------------------------------------------------
# 催促
# ---------------------------------------------------------------------------

def nudge(project: str, current_rev: str, DEVBAR_CLI: str, **_kw) -> str:
    """spec改訂催促メッセージ。"""
    return (
        f"[Remind] {project} spec rev{current_rev} のリバイス作業が未完了です。\n"
        f"レビュー指摘を反映し、以下のコマンドで完了報告してください:\n"
        f"{DEVBAR_CLI} spec revise-submit --pj {project} --file <完了報告YAMLファイルパス>"
    )


# ---------------------------------------------------------------------------
# Discord通知（短文）
# ---------------------------------------------------------------------------

def notify_done(project: str, rev: str | int, commit: str, **_kw) -> str:
    """REVISE完了（commit hashあり）。"""
    return f"[Spec] {project}: rev{rev} 改訂完了 ({commit[:7]})"


def notify_commit_failed(project: str, rev: str | int, **_kw) -> str:
    """REVISE完了（git commit失敗）。"""
    return f"[Spec] ⚠️ {project}: rev{rev} git commit失敗"


def notify_no_changes(project: str, rev: str | int, **_kw) -> str:
    """REVISE完了（差分0）→ SPEC_PAUSED。"""
    return f"[Spec] ⚠️ {project}: rev{rev} 変更なし（改訂が空）"


def notify_self_review_failed(project: str, failed_count: int, **_kw) -> str:
    """セルフレビュー差し戻し通知。"""
    return f"🔁 [{project}] セルフレビュー: {failed_count}件の問題検出。implementer に差し戻し"
