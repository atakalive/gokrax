"""SPEC_REVIEW ステートのプロンプト・通知・催促。

Variables (common):
    project: str       - プロジェクト名
    spec_path: str     - 仕様書ファイルパス
    current_rev: str   - 現在のリビジョン番号
    GOKRAX_CLI: str    - gokrax CLIパス
    reviewer: str      - レビュアー名（ファイルパス生成用）
"""

import re


# ---------------------------------------------------------------------------
# エージェント向けプロンプト
# ---------------------------------------------------------------------------

def initial(project: str, spec_path: str, current_rev: str, GOKRAX_CLI: str, reviewer: str = "", **_kw) -> str:
    """初回レビュー依頼プロンプト（§5.1）。"""
    sanitized = re.sub(r'[/\\\s]', '-', project)
    save_path = f"/tmp/gokrax-review/{sanitized}--spec-{reviewer}-rev{current_rev}.yaml" if reviewer else f"/tmp/gokrax-review/{sanitized}--spec-<YOUR_NAME>-rev{current_rev}.yaml"
    return f"""【指示】このタスクは中断せず最後まで一気に完了してください。途中で確認を求めないこと。

以下の仕様書をレビューしてください。**やりすぎレビュー**を依頼します。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})

## レビュー指示
- 重篤度を必ず付与: 🔴 Critical (P0) / 🟠 Major (P1) / 🟡 Minor / 💡 Suggestion
- セクション番号を明記（例: §6.2）
- 擬似コード間の整合性に特に注意
- 既存gokraxコードベースとの整合性も確認
- ステートマシン遷移の抜け穴・デッドロックを探せ
- YAMLブロックは応答内で**1つだけ**
- verdict の選び方: critical → P0, major → P1, minor/suggestion → P2。指摘ゼロの場合のみ APPROVE
- **指摘リストのキーは必ず `items:` を使うこと**（`findings:` 等の別名は使わない）

## 出力フォーマット
```yaml
verdict: APPROVE | P0 | P1 | P2
items:
  - id: C-1
    severity: critical | major | minor | suggestion
    section: "§6.2"
    title: "タイトル"
    description: "説明"
    suggestion: "修正案"
```

## レビュー結果の投入手順
1. YAMLファイルを以下に保存: {save_path}
2. 以下のコマンドで投入:
```bash
{GOKRAX_CLI} spec review-submit --pj {project} --reviewer {reviewer or "<YOUR_NAME>"} --file {save_path}
```

ファイルは素のYAMLでも、上記「出力フォーマット」の ```yaml ... ``` ブロックを含むMarkdownでも可。

【重要】レビュー完了・結果の提出まで、中断せず一気に完了すること。"""


def revision(
    project: str, spec_path: str, current_rev: str, GOKRAX_CLI: str, reviewer: str = "",
    changelog: str = "", added: str = "", removed: str = "", last_commit: str = "",
    **_kw,
) -> str:
    """rev2以降のレビュー依頼プロンプト（§5.1）。"""
    sanitized = re.sub(r'[/\\\s]', '-', project)
    save_path = f"/tmp/gokrax-review/{sanitized}--spec-{reviewer}-rev{current_rev}.yaml" if reviewer else f"/tmp/gokrax-review/{sanitized}--spec-<YOUR_NAME>-rev{current_rev}.yaml"
    return f"""【指示】このタスクは中断せず最後まで一気に完了してください。途中で確認を求めないこと。

以下の仕様書の改訂版をレビューしてください。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})
前回からの変更: +{added}行, -{removed}行
前回commit: {last_commit}

## 前回レビューからの変更点
{changelog}

## レビュー指示
- 前回の指摘が適切に反映されているか確認
- 新たに追加された部分に問題がないか確認
- 重篤度・セクション番号・YAMLフォーマットは前回と同様
- YAMLブロックは応答内で**1つだけ**
- verdict の選び方: critical → P0, major → P1, minor/suggestion → P2。指摘ゼロの場合のみ APPROVE
- **指摘リストのキーは必ず `items:` を使うこと**（`findings:` 等の別名は使わない）

## レビュー結果の投入手順
1. YAMLファイルを以下に保存: {save_path}
2. 以下のコマンドで投入:
```bash
{GOKRAX_CLI} spec review-submit --pj {project} --reviewer {reviewer or "<YOUR_NAME>"} --file {save_path}
```

ファイルは素のYAMLでも、上記「出力フォーマット」の ```yaml ... ``` ブロックを含むMarkdownでも可。

【重要】レビュー完了・結果の提出まで、中断せず一気に完了すること。"""


# ---------------------------------------------------------------------------
# 催促
# ---------------------------------------------------------------------------

def nudge(project: str, current_rev: str, spec_path: str, reviewer: str, GOKRAX_CLI: str, **_kw) -> str:
    """specレビュー催促メッセージ。"""
    sanitized = re.sub(r'[/\\\s]', '-', project)
    save_path = f"/tmp/gokrax-review/{sanitized}--spec-{reviewer}-rev{current_rev}.yaml"
    return (
        f"[Remind] {project} spec rev{current_rev} のレビューが未完了です。\n"
        f"仕様書: {spec_path}\n"
        f"以下のコマンドでレビュー結果を提出してください:\n"
        f"{GOKRAX_CLI} spec review-submit --pj {project} --reviewer {reviewer} --file {save_path}"
    )


# ---------------------------------------------------------------------------
# Discord通知（短文）
# ---------------------------------------------------------------------------

def notify_start(project: str, rev: str | int, reviewer_count: int, **_kw) -> str:
    """→ SPEC_REVIEW開始。"""
    return f"[Spec][{project}] rev{rev} レビュー開始 ({reviewer_count}人)"


def notify_complete(
    project: str, rev: str | int,
    critical: int, major: int, minor: int, suggestion: int,
    **_kw,
) -> str:
    """→ SPEC_REVISE遷移時。"""
    return f"[Spec][{project}] rev{rev} レビュー完了 — C:{critical} M:{major} m:{minor} s:{suggestion}"


def notify_failed(project: str, rev: str | int, **_kw) -> str:
    """→ SPEC_REVIEW_FAILED。"""
    return f"[Spec][{project}] ❌ 有効レビュー不足"
