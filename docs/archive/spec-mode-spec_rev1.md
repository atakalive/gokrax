# DevBar Spec Mode — 仕様書

**Version:** 1.0 (draft)
**Date:** 2026-02-28
**Author:** Asuka (second) + M

---

## 1. 目的と背景

### 1.1 現状の問題

仕様書（spec）の作成・レビュー・改訂サイクルは現在すべて手動で行われている:

1. アスカ（or 他エージェント）がMと対話しながら spec 叩き台を作成
2. アスカが `sessions_send` で3人のレビュアーに個別送信
3. レビュー結果が返ってくるのを待つ
4. 3人分のレビュー結果を手動で分析・重複排除・統合
5. spec ファイルを手動で編集して改訂（revN → revN+1）
6. git commit & push
7. 2〜6 を繰り返し（TrajOpt は 5 ラウンド）
8. 完成した spec から手動で GitLab Issue を起票（TrajOpt は 19件）
9. devbar-queue.txt にバッチ実行順・モデル指定を手動で記述

TrajOpt unified-gui-spec では rev1 → rev4A まで 5 ラウンド、延べ 60+ 件のレビュー指摘を処理した。各ラウンドで数時間の手作業が発生している。

### 1.2 目標

devbar に **spec mode** を追加し、上記 1〜9 を自動化する:

- spec ファイルのレビュー依頼・回収・状態遷移を devbar が管理
- レビュー結果の構造化パース・重複統合を自動実行
- spec 改訂（revN+1 作成）をエージェント（spec_implementer）が自動実行
- レビューループの終了判定（P1 以上なし or MAX_CYCLES 到達）
- 完成 spec から Issue 分割案の収集・統合・GitLab 起票
- devbar-queue.txt へのバッチ行生成（モデル・keep-context・理由コメント付き）

### 1.3 スコープ

**スコープ内:**
- spec レビューサイクルの自動化（SPEC_REVIEW → SPEC_REVISE ループ）
- Issue 分割の半自動化（レビュアー提案収集 → implementer 統合 → GitLab 起票）
- キュー生成の自動化（依存関係分析 → バッチ構成 → queue ファイル書き込み）

**スコープ外:**
- spec 叩き台の自動生成（人間 + エージェントが事前に作成する前提）
- devbar 実装フロー（DESIGN_PLAN → IMPLEMENTATION → ...）との直接接続（将来課題）
- spec mode 自身の spec を spec mode で回すこと（ブートストラップ問題）

---

## 2. ステートマシン

### 2.1 状態遷移図

```
[devbar spec start]
        │
        ├─── [--skip-review] ───→ SPEC_APPROVED
        │
        ▼
  SPEC_REVIEW ◄──────────────┐
        │                    │
        │ (全レビュアー回収   │
        │  or タイムアウト)   │
        ▼                    │
  SPEC_REVISE ───────────────┘
        │       (P1以上あり: ループ継続)
        │
        │ (P1以上なし or MAX_SPEC_REVISE_CYCLES 到達)
        ▼
  SPEC_APPROVED ─────────────── [--no-issue 指定時: ここで終了]
        │
        ▼
  ISSUE_SUGGESTION
        │ (レビュアーから分割案収集)
        ▼
  ISSUE_PLAN
        │ (spec_implementer が統合 → GitLab 起票)
        ▼
  QUEUE_PLAN ──────────────── [--no-queue 指定時: ここで終了]
        │ (devbar-queue.txt 生成)
        ▼
  SPEC_DONE
        │ (M 確認)
        ▼
  IDLE
```

### 2.2 状態定義

| 状態 | 説明 | 入り口 | 出口 |
|---|---|---|---|
| `SPEC_REVIEW` | レビュアーに spec を送信し、レビュー回収を待つ | `devbar spec start` or SPEC_REVISE 完了 | 全レビュアー回収 or grace_period 満了 |
| `SPEC_REVISE` | レビュー結果を統合し、spec_implementer が改訂を実行 | SPEC_REVIEW 完了 | 改訂 commit & push 完了 |
| `SPEC_APPROVED` | spec 改訂サイクル完了。Issue 分割に進むか終了 | SPEC_REVISE（終了条件成立）| 自動遷移 or 終了（`--no-issue`）|
| `ISSUE_SUGGESTION` | レビュアーに Issue 分割案を問い合わせ | SPEC_APPROVED | 全レビュアー回収 or タイムアウト |
| `ISSUE_PLAN` | spec_implementer が分割案を統合し、GitLab Issue を起票 | ISSUE_SUGGESTION 完了 | Issue 起票完了 |
| `QUEUE_PLAN` | Issue 依存関係を分析し、devbar-queue.txt にバッチ行を生成 | ISSUE_PLAN 完了 | queue 書き込み完了 |
| `SPEC_DONE` | 全工程完了。M の確認待ち | QUEUE_PLAN 完了 | M が確認 → IDLE |

### 2.3 既存ステートとの共存

spec mode のステート（`SPEC_*`, `ISSUE_*`, `QUEUE_PLAN`, `SPEC_DONE`）は既存の実装フローステート（`DESIGN_PLAN`, `IMPLEMENTATION` 等）と**排他**。同一プロジェクトで同時に両方は動かない。

pipeline.json に `"spec_mode": true` フラグを持たせ、watchdog はこのフラグで分岐する。

### 2.4 終了条件

SPEC_REVISE → SPEC_REVIEW ループの終了判定:

| 条件 | 動作 |
|---|---|
| 全レビュアーの最高重篤度が Suggestion 以下（P1 なし）| → SPEC_APPROVED |
| `MAX_SPEC_REVISE_CYCLES` 到達 | → SPEC_APPROVED（警告付き）|
| M が手動で `devbar spec approve --pj X` | → SPEC_APPROVED（強制）|

**`MAX_SPEC_REVISE_CYCLES`**: デフォルト 5（コード実装の `MAX_REVISE_CYCLES=3` より多い。spec は設計議論のため収束が遅い）。`config.py` で設定。

### 2.5 早期終了オプションとスキップ

| オプション | 効果 |
|---|---|
| `--no-issue` | SPEC_APPROVED で終了。Issue 分割・キュー生成をスキップ |
| `--no-queue` | ISSUE_PLAN 完了（Issue 起票）で終了。キュー生成をスキップ |
| `--skip-review` | spec レビューサイクルをスキップし、SPEC_APPROVED から開始 |
| なし（デフォルト）| SPEC_DONE まで全工程実行 |

用途:
- `--no-issue`: コード以外の spec（運用手順書、設計ガイドライン等）
- `--no-queue`: Issue 起票はするが、実行順序は人間が決めたい場合
- `--skip-review`: spec は既に完成済みで、Issue 化とキュー生成だけ行いたい場合
- `--skip-review --no-queue`: Issue 起票だけ行いたい場合
- `--skip-review --no-issue`: 無意味（何もしない）。エラーにする

---

## 3. パイプライン設定

### 3.1 pipeline.json 拡張

spec mode 時に pipeline.json に追加されるフィールド:

```json
{
  "project": "TrajOpt",
  "state": "SPEC_REVIEW",
  "spec_mode": true,
  "spec_config": {
    "spec_path": "docs/unified-gui-spec.md",
    "spec_implementer": "second",
    "no_issue": false,
    "no_queue": false,
    "current_rev": 1,
    "max_revise_cycles": 5,
    "revise_count": 0,
    "review_history": []
  },
  "enabled": true,
  "review_mode": "full",
  "batch": [
    {
      "issue": "spec",
      "title": "docs/unified-gui-spec.md",
      "spec_reviews": {},
      "added_at": "2026-02-28T..."
    }
  ]
}
```

**`spec_config` フィールド:**

| フィールド | 型 | 説明 |
|---|---|---|
| `spec_path` | str | spec ファイルのリポジトリ相対パス |
| `spec_implementer` | str | 改訂を実行するエージェント ID |
| `no_issue` | bool | SPEC_APPROVED で終了するか |
| `no_queue` | bool | ISSUE_PLAN で終了するか |
| `skip_review` | bool | spec レビューをスキップするか |
| `self_review_passes` | int | 改訂後のセルフレビュー回数 |
| `queue_file` | str | キュー出力先パス |
| `current_rev` | int | 現在のリビジョン番号 |
| `max_revise_cycles` | int | 最大改訂サイクル数 |
| `revise_count` | int | 現在の改訂サイクル数 |
| `review_history` | list | 各ラウンドのレビュー結果サマリー |

### 3.2 config.py 追加定数

```python
# Spec mode
MAX_SPEC_REVISE_CYCLES = 5
SPEC_REVIEW_TIMEOUT_SEC = 600       # レビュアー個別のタイムアウト（10分）
SPEC_REVISE_TIMEOUT_SEC = 1800      # 改訂作業のタイムアウト（30分）
SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC = 300  # Issue分割提案のタイムアウト（5分）
SPEC_REVISE_SELF_REVIEW_PASSES = 2  # 改訂後のセルフレビュー回数（反映漏れ防止）
SPEC_QUEUE_FILE = PIPELINES_DIR / "devbar-queue.txt"  # キュー出力先
```

---

## 4. CLI インターフェース

### 4.1 コマンド体系

既存の `devbar` サブコマンドと並列に `devbar spec` サブコマンドグループを追加:

```
devbar spec start     起動（spec mode パイプライン開始）
devbar spec approve   手動で SPEC_APPROVED に遷移（ループ強制終了）
devbar spec status    spec mode 固有のステータス表示
```

### 4.2 devbar spec start

```
devbar spec start --pj PROJECT --spec SPEC_PATH --implementer AGENT_ID
                  [--no-issue] [--no-queue] [--skip-review]
                  [--max-cycles N] [--review-mode full|standard|lite|min]
                  [--model MODEL]
```

| 引数 | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `--pj` | ✅ | — | プロジェクト名 |
| `--spec` | ✅ | — | spec ファイルのリポジトリ相対パス |
| `--implementer` | ✅ | — | 改訂エージェント（例: `second`, `kaneko`）|
| `--no-issue` | — | false | SPEC_APPROVED で終了 |
| `--no-queue` | — | false | ISSUE_PLAN で終了 |
| `--skip-review` | — | false | spec レビューをスキップし SPEC_APPROVED から開始 |
| `--max-cycles` | — | 5 | 最大改訂サイクル数 |
| `--review-mode` | — | full | レビューモード |
| `--model` | — | — | spec_implementer の使用モデル（plan/impl 共通）|

**前提条件:**
- プロジェクトが IDLE 状態であること（既存パイプライン実行中は不可）
- spec ファイルが存在すること
- spec_implementer のエージェントが利用可能であること

**実行時の動作:**
1. pipeline.json に spec_config を書き込み
2. `enabled = true` に設定
3. SPEC_REVIEW に遷移
4. watchdog が検知してレビュー依頼を送信

### 4.3 devbar spec approve

```
devbar spec approve --pj PROJECT
```

現在の SPEC_REVIEW / SPEC_REVISE ループを強制終了し、SPEC_APPROVED に遷移する。「もう十分だからこれで行く」場合に使用。

### 4.4 devbar spec status

```
devbar spec status [--pj PROJECT]
```

出力例:
```
TrajOpt [SPEC_REVIEW] rev3 (cycle 2/5)
  spec: docs/unified-gui-spec.md
  implementer: second
  reviewers: pascal(✅ APPROVE), leibniz(✅ P0×2), dijkstra(⏳ pending)
  rev1: C:6 M:8 m:12 S:4
  rev2: C:2 M:3 m:8 S:2
  rev3: (in progress)
```

---

## 5. SPEC_REVIEW フェーズ

### 5.1 レビュー依頼の送信

SPEC_REVIEW に遷移した時点で、watchdog が以下を実行:

1. spec ファイルの内容を読み込む
2. レビュアーリスト（`review_mode` に基づく）を取得
3. 各レビュアーに `sessions_send` でレビュー依頼を送信

**送信プロンプト（初回）:**

```
以下の仕様書をレビューしてください。

仕様書パス: {spec_path} (rev{current_rev})
プロジェクト: {project}

## レビュー指示
- 重篤度を必ず付与: 🔴 Critical (P0) / 🟠 Major (P1) / 🟡 Minor / 💡 Suggestion
- セクション番号を明記（例: §6.2）
- 擬似コード間の整合性（引数・型・呼び出し規約）に特に注意
- 実装時に詰まりそうな曖昧さを指摘

## 出力フォーマット
以下の構造化フォーマットで出力してください:

```yaml
verdict: APPROVE | P0 | P1
items:
  - id: C-1
    severity: critical | major | minor | suggestion
    section: "§6.2"
    title: "簡潔なタイトル"
    description: "詳細な説明"
    suggestion: "修正案（あれば）"
```

---

{spec_content}
```

**送信プロンプト（rev2 以降）:**

```
以下の仕様書の改訂版をレビューしてください。

仕様書パス: {spec_path} (rev{current_rev})
プロジェクト: {project}
前回からの変更: +{added_lines}行, -{removed_lines}行

## 前回レビューからの変更点
{changelog_summary}

## レビュー指示
- 前回の指摘が適切に反映されているか確認
- 新たに追加された部分に問題がないか確認
- 重篤度・セクション番号・構造化フォーマットは前回と同様

{spec_content}
```

### 5.2 レビュー回収

既存の devbar レビュー回収メカニズムを流用:

- レビュアーの応答を `spec_reviews` に格納（pipeline.json）
- 全レビュアー回収 or `grace_period` 満了で SPEC_REVISE に遷移
- 未応答レビュアーへの催促は既存の watchdog 催促ロジックを流用

### 5.3 レビュー結果のパース

レビュアーの応答テキストから構造化データを抽出する。

**パース戦略:**

1. YAML ブロック（```yaml ... ```）を正規表現で抽出
2. YAML パース → `verdict` + `items` リストを取得
3. YAML ブロックがない場合のフォールバック: LLM（spec_implementer）にパース依頼

```python
@dataclass
class SpecReviewItem:
    id: str                    # "C-1", "M-2" 等
    severity: str              # "critical", "major", "minor", "suggestion"
    section: str               # "§6.2"
    title: str
    description: str
    suggestion: str | None
    reviewer: str              # "pascal", "leibniz" 等

@dataclass
class SpecReviewResult:
    reviewer: str
    verdict: str               # "APPROVE", "P0", "P1"
    items: list[SpecReviewItem]
    raw_text: str              # 元テキスト（パース失敗時の参照用）
```

### 5.4 重複検出・統合

複数レビュアーが同一箇所を指摘するケースの処理:

```python
def merge_reviews(reviews: list[SpecReviewResult]) -> MergedReviewReport:
    """複数レビュアーの指摘を統合。

    統合ルール:
    1. 同一セクション + 類似タイトル → 重複候補としてグルーピング
    2. 重複グループ内で最も高い severity を採用
    3. 各レビュアーの具体的な提案は全て保持（異なる視点がある）
    """
```

**統合レポートのフォーマット（spec_implementer に渡す）:**

```markdown
# Rev{N} レビュー統合レポート

## サマリー
- レビュアー: Pascal (APPROVE), Leibniz (P0×2, P1×1), Dijkstra (P1×1)
- Critical: 2件, Major: 2件（うち重複1件）, Minor: 5件, Suggestion: 3件

## Critical (対応必須)
### C-1: process.log の所有者矛盾 (§9)
- **Leibniz C-NEW-1**: Popen redirect と RotatingFileHandler が共存不可
- **Dijkstra m2**: 同一指摘（RotatingFileHandler 混在問題）→ 統合
- 提案: writer 側直接管理に統一

## Major (対応推奨)
### M-1: cumulative_elapsed_sec の更新ロジック (§6.2)
- **Pascal M-1**: save_state 時にセッション経過を加算していない
- **Leibniz M-NEW-2**: 同一指摘
- **Dijkstra m3**: 同一指摘
- 提案: _session_start = time.monotonic() で記録

## Minor
...

## Suggestion
...
```

---

## 6. SPEC_REVISE フェーズ

### 6.1 改訂プロセス

SPEC_REVISE に遷移すると、spec_implementer に改訂を依頼する。

**改訂依頼プロンプト（sessions_send で spec_implementer に送信）:**

```
以下のレビュー統合レポートに基づき、仕様書を改訂してください。

プロジェクト: {project}
仕様書: {repo_path}/{spec_path}
現在のリビジョン: rev{current_rev}
改訂後: rev{current_rev + 1} (rev{current_rev}{suffix} の場合もあり)

## 改訂ルール
1. Critical (P0) は必ず反映
2. Major (P1) は原則反映。却下する場合は理由を changelog に明記
3. Minor / Suggestion は判断に任せる
4. 変更履歴セクションを更新（何を反映したか、行数変化）
5. 改訂完了後、git commit & push

## レビュー統合レポート
{merged_review_report}

改訂が完了したら、以下のフォーマットで報告してください:

```yaml
status: done
new_rev: "4A"
commit: "abc1234"
changes:
  added_lines: 350
  removed_lines: 50
  reflected_items: ["C-1", "C-2", "M-1", "M-2", "m-1", "m-2", "m-3"]
  deferred_items: ["m-4"]
  deferred_reasons:
    m-4: "bounds二重管理の解消は別Issueで対応"
```
```

### 6.2 セルフレビュー（反映漏れ防止）

改訂後、spec_implementer に `SPEC_REVISE_SELF_REVIEW_PASSES` 回（デフォルト2回）のセルフレビューを実行させる。Opus でも1回の改訂では見落としが発生するため、複数パスで品質を担保する。

**セルフレビュー依頼プロンプト（改訂完了報告の後に送信）:**

```
改訂した仕様書を再読し、以下を確認してください（セルフレビュー {pass}/{total} パス目）。

## 確認項目
1. レビュー統合レポートの全 Critical / Major 項目が反映されているか
2. 反映した変更が他のセクションと矛盾していないか
3. 擬似コード間の引数・型・呼び出し規約の整合性
4. changelog に反映内容が正確に記載されているか

問題を発見した場合は修正し、git commit & push してから報告してください。
問題がなければ `status: clean` で報告してください。

```yaml
status: clean | fixed
fixes: ["修正内容1", "修正内容2"]  # fixed の場合のみ
commit: "def5678"                   # fixed の場合のみ
```
```

全パス完了後（または全パスで `clean`）、改訂完了として次のステップに進む。

### 6.3 改訂完了の検知

spec_implementer からの応答を待ち、完了レポートをパース:

1. YAML ブロックから `status: done` を確認
2. セルフレビューを `SPEC_REVISE_SELF_REVIEW_PASSES` 回実行
3. 最終 `commit` ハッシュを pipeline.json に記録
4. `current_rev` を更新
5. `review_history` にラウンド結果を追加

### 6.3 終了判定

改訂完了後、次のラウンドに進むか終了するかを判定:

```python
def should_continue_review(pipeline: dict, reviews: list[SpecReviewResult]) -> bool:
    """レビューループ継続判定。"""
    config = pipeline["spec_config"]

    # MAX_CYCLES 到達
    if config["revise_count"] >= config["max_revise_cycles"]:
        return False

    # 全レビュアーの verdict が APPROVE（P1 以上なし）
    has_p1_or_higher = any(
        r.verdict in ("P0", "P1") for r in reviews
    )
    if not has_p1_or_higher:
        return False

    return True
```

---

## 7. ISSUE_SUGGESTION フェーズ

### 7.1 Issue 分割案の収集

SPEC_APPROVED 後、レビュアーに Issue 分割の提案を求める。

**送信プロンプト:**

```
以下の仕様書が承認されました。実装に向けて Issue 分割を提案してください。

仕様書: {spec_path} (rev{final_rev})
プロジェクト: {project}

## 提案の指針
- CC（Claude Code）が 1 Issue = 1 MR で実装できる粒度（1〜3ファイル / 100〜500行）
- 依存関係を明示（DAG）
- Phase 分割（並行着手可能なグループ）
- 各 Issue のタイトル、変更ファイル、概算行数、仕様参照セクション

## 出力フォーマット
```yaml
phases:
  - name: "Phase 1: 基盤"
    issues:
      - title: "TaskBase + SubprocessConfig"
        files: ["unified_gui/task_base.py"]
        lines: 250
        spec_refs: ["§3.1", "§14.3"]
        depends_on: []
      - title: "StandardOptTask"
        files: ["unified_gui/tasks/standard_opt.py"]
        lines: 250
        spec_refs: ["§3.3", "§14.4"]
        depends_on: ["TaskBase + SubprocessConfig"]
```
```

### 7.2 提案の回収

レビュー回収と同じメカニズム。タイムアウトは `SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC`。

---

## 8. ISSUE_PLAN フェーズ

### 8.1 Issue 統合と起票

spec_implementer に提案統合 + GitLab 起票を依頼:

**依頼プロンプト:**

```
レビュアーから以下の Issue 分割提案が集まりました。
これらを統合し、GitLab Issue を起票してください。

## 提案
{reviewer_suggestions}

## 起票ルール
1. 各 Issue の冒頭に以下の注記を入れること（削除不可）:
   > ⚠️ この Issue のコード片は参考実装であり、正ではありません。
   > 正式な仕様は `{spec_path}` です。実装・レビュー時は必ず spec を参照してください。
   > この注記を削除しないでください。
2. spec のパスとコミットハッシュを冒頭に記載
3. 仕様参照セクション（§番号）を明記
4. 完了条件をチェックリストで記載
5. 依存 Issue を明記
6. 参考コードを spec から引用（長すぎる場合は § 参照のみ）
7. `glab issue create` で起票

起票完了後、以下のフォーマットで報告:

```yaml
status: done
issues:
  - number: 32
    title: "P1-1: trajboy_mcmc.py --config"
    phase: 1
    depends_on: []
    est_lines: 110
    spec_refs: ["§8.3"]
  - number: 33
    ...
```
```

---

## 9. QUEUE_PLAN フェーズ

> キュー出力先は `config.SPEC_QUEUE_FILE`（デフォルト: `PIPELINES_DIR / "devbar-queue.txt"`）。

### 9.1 キュー行の生成

Issue 起票結果と spec の内容から、devbar-queue.txt に追記するバッチ行を生成する。

**spec_implementer への依頼プロンプト:**

```
以下の Issue リストから devbar-queue.txt のバッチ行を生成してください。

## Issue リスト
{issue_list_with_deps}

## 判断基準
1. **バッチ構成**: 相互依存のない Issue はまとめる。密結合はまとめる
2. **モデル選定**:
   - plan=opus: 複雑な設計判断がある Issue
   - impl=opus: OS分岐、数学的精密さ、エッジケース処理が必要な Issue
   - デフォルト（Sonnet）: spec に擬似コード全体がある場合
3. **keep-context**: 同一ファイルを連続で触る Issue 間
4. **レビューモード**: 全バッチ {review_mode}
5. 各バッチにコメントで理由を付記

## 出力フォーマット
devbar-queue.txt に直接追記できる形式:

```
# {project} {spec_name} — 依存順キュー ({n} Issues, spec rev{rev})
#
# ===== Phase 1: {phase_name} =====
#
# Batch 1: {理由}
{project} {issues} {review_mode} {options}
```
```

### 9.2 M の確認

QUEUE_PLAN 完了後、SPEC_DONE に遷移。Discord #dev-bar に完了通知を送信:

```
[Spec Mode] {project}: 全工程完了

spec: {spec_path} (rev{final_rev}, {total_revise_cycles} cycles)
Issues: {n_issues}件 (#{first}〜#{last})
Queue: {n_batches} バッチ

キュー内容:
{queue_preview}

M の確認を待っています。`devbar qrun` で実行開始できます。
```

---

## 10. Watchdog 統合

### 10.1 watchdog.py の拡張

既存の watchdog ループに spec mode の処理を追加:

```python
def check_project(project: str, pipeline: dict):
    if pipeline.get("spec_mode"):
        check_spec_mode(project, pipeline)
    else:
        check_implementation_mode(project, pipeline)  # 既存ロジック

def check_spec_mode(project: str, pipeline: dict):
    state = pipeline["state"]

    if state == "SPEC_REVIEW":
        check_spec_review_progress(project, pipeline)
    elif state == "SPEC_REVISE":
        check_spec_revise_progress(project, pipeline)
    elif state == "SPEC_APPROVED":
        if not pipeline["spec_config"]["no_issue"]:
            transition_to(project, "ISSUE_SUGGESTION")
        else:
            transition_to(project, "SPEC_DONE")
    elif state == "ISSUE_SUGGESTION":
        check_issue_suggestion_progress(project, pipeline)
    elif state == "ISSUE_PLAN":
        check_issue_plan_progress(project, pipeline)
    elif state == "QUEUE_PLAN":
        check_queue_create_progress(project, pipeline)
    elif state == "SPEC_DONE":
        pass  # M の確認待ち
```

### 10.2 タイムアウトと催促

| 状態 | タイムアウト | 催促 | タイムアウト後の動作 |
|---|---|---|---|
| SPEC_REVIEW | `SPEC_REVIEW_TIMEOUT_SEC` per reviewer | 既存催促ロジック流用 | 応答済みレビュアーのみで SPEC_REVISE に遷移 |
| SPEC_REVISE | `SPEC_REVISE_TIMEOUT_SEC` | spec_implementer に催促 | M に通知（手動介入要求）|
| ISSUE_SUGGESTION | `SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC` per reviewer | 既存催促ロジック流用 | 応答済み提案のみで ISSUE_PLAN に遷移 |
| ISSUE_PLAN | `SPEC_REVISE_TIMEOUT_SEC` | spec_implementer に催促 | M に通知 |
| QUEUE_PLAN | `SPEC_REVISE_TIMEOUT_SEC` | spec_implementer に催促 | M に通知 |

---

## 11. notify.py 拡張

### 11.1 spec mode 固有の通知

Discord #dev-bar への通知を追加:

| イベント | 通知内容 |
|---|---|
| SPEC_REVIEW 開始 | `[Spec] {project}: rev{N} レビュー開始（{n_reviewers}人）` |
| SPEC_REVIEW 完了 | `[Spec] {project}: rev{N} レビュー完了 — C:{n} M:{n} m:{n} S:{n}` |
| SPEC_REVISE 開始 | `[Spec] {project}: rev{N}→rev{N+1} 改訂開始` |
| SPEC_REVISE 完了 | `[Spec] {project}: rev{N+1} 完了 (commit {hash})` |
| SPEC_APPROVED | `[Spec] {project}: spec 承認 (rev{N}, {cycles} cycles)` |
| ISSUE_PLAN 完了 | `[Spec] {project}: {n}件の Issue 起票完了 (#{first}〜#{last})` |
| QUEUE_PLAN 完了 | `[Spec] {project}: キュー生成完了 ({n} batches). M 確認待ち` |

---

## 12. レビュー結果の保存

### 12.1 ファイル保存

各ラウンドのレビュー結果を spec と同じリポジトリに保存:

```
{repo_path}/
├── docs/
│   ├── {spec_name}.md                    # spec 本体
│   └── {spec_name}_rev{N}.md             # 改訂版（上書き or 別名）
└── reviews/
    ├── {date}_{reviewer}-{spec_name}-rev{N}.md    # レビュー原文
    └── {date}_merged-{spec_name}-rev{N}.md        # 統合レポート
```

### 12.2 pipeline.json の review_history

```json
"review_history": [
  {
    "rev": 1,
    "reviews": {
      "pascal": {"verdict": "P0", "counts": {"critical": 1, "major": 2, "minor": 3, "suggestion": 1}},
      "leibniz": {"verdict": "P0", "counts": {"critical": 2, "major": 1, "minor": 2, "suggestion": 0}},
      "dijkstra": {"verdict": "APPROVE", "counts": {"critical": 0, "major": 0, "minor": 5, "suggestion": 2}}
    },
    "merged_counts": {"critical": 2, "major": 2, "minor": 8, "suggestion": 3},
    "commit": "abc1234",
    "timestamp": "2026-02-28T..."
  }
]
```

---

## 13. 実装計画

### 13.1 変更ファイル

| ファイル | 変更内容 | 規模 |
|---|---|---|
| `devbar.py` | `spec` サブコマンドグループ（start/approve/status）| +200行 |
| `watchdog.py` | `check_spec_mode()` + 各状態の処理 | +250行 |
| `notify.py` | spec mode 通知テンプレート | +80行 |
| `config.py` | spec mode 定数（タイムアウト、MAX_CYCLES）| +15行 |
| `pipeline_io.py` | spec_config の初期化・バリデーション | +30行 |
| `spec_review.py` | **新規**: レビュー結果パース、重複検出、統合レポート生成 | +300行 |
| `spec_revise.py` | **新規**: 改訂依頼プロンプト生成、完了レポートパース | +200行 |
| `spec_issue.py` | **新規**: Issue 分割提案パース、GitLab 起票、キュー生成 | +250行 |
| `tests/` | spec mode テスト | +400行 |
| **合計** | | **~1,725行** |

### 13.2 Issue 分割案

| Issue | タイトル | 依存 | 概算行数 |
|---|---|---|---|
| S-1 | config.py + pipeline_io.py: spec mode 基盤定数・初期化 | なし | +45行 |
| S-2 | devbar.py: `spec start/approve/status` CLI | S-1 | +200行 |
| S-3 | spec_review.py: レビュー結果パース + 重複検出 + 統合レポート | S-1 | +300行 |
| S-4 | watchdog.py: SPEC_REVIEW + SPEC_REVISE ステート処理 | S-2, S-3 | +150行 |
| S-5 | spec_revise.py: 改訂依頼プロンプト + 完了パース + 終了判定 | S-3 | +200行 |
| S-6 | spec_issue.py: ISSUE_SUGGESTION + ISSUE_PLAN + QUEUE_PLAN | S-4 | +250行 |
| S-7 | notify.py: spec mode 通知 | S-4 | +80行 |
| S-8 | watchdog.py: ISSUE_* + QUEUE_PLAN ステート処理 | S-6, S-7 | +100行 |
| S-9 | 統合テスト | S-8 | +400行 |

### 13.3 依存関係 DAG

```
S-1 ──┬── S-2 ──┐
      │         ├── S-4 ── S-7
      └── S-3 ──┘    │
           │         │
           └── S-5   ├── S-8 ── S-9
                     │
              S-6 ───┘
```

---

## 14. 将来の拡張

- **devbar 実装フローとの接続**: SPEC_DONE → 自動的に `devbar qrun` 開始
- **spec 叩き台の自動生成**: 既存コードベースから spec 初稿を生成
- **差分レビュー**: rev 間の diff のみをレビュアーに送信（全文送信のトークン節約）
- **レビュアー学習**: 過去のレビュー傾向から、どのレビュアーがどの種の指摘に強いかを分析
