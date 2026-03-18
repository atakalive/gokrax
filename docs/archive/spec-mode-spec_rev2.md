# gokrax Spec Mode — 仕様書

**Version:** 2.0 (rev1 review reflected — Pascal/Leibniz/Dijkstra)
**Date:** 2026-02-28
**Author:** Asuka (second) + M
**Reviewers:** Pascal (Gemini 3 Pro), Leibniz (GPT-5.2), Dijkstra (Opus)

---

### 変更履歴

| Version | Date | 内容 |
|---|---|---|
| 1.0 | 2026-02-28 | 初版（821行）。M フィードバック 4件反映済み（QUEUE_PLAN リネーム、SPEC_QUEUE_FILE、--skip-review、セルフレビュー）|
| 2.0 | 2026-02-28 | **rev1 レビュー反映**（Pascal 6件、Leibniz 23件、Dijkstra 28件 → 重複排除後 42件）。主要変更は以下 |

**v1→v2 の主要変更点（Pascal/Leibniz/Dijkstra rev1 feedback）:**

- **[v2] 空レビュー集合の自動承認バグ修正（Pascal C-1）**: `any()` が空リストで False を返し、レビュー 0 件で SPEC_APPROVED に直行する論理バグを修正。`MIN_VALID_REVIEWS = 1` を導入し、有効レビュー数が不足する場合は `SPEC_REVIEW_FAILED` に遷移
- **[v2] MAX_CYCLES 到達時の強制承認廃止（Pascal C-2）**: P1 以上が残存する場合は SPEC_APPROVED ではなく `SPEC_STALLED` に遷移し、M の明示的介入を必須化。`gokrax spec approve --force` でのみ突破可能
- **[v2] タイムアウト時の無限スタック防止（Pascal C-3 / Leibniz C-10）**: 全状態に `MAX_SPEC_RETRIES = 3` を導入。超過時は `SPEC_PAUSED` に遷移しパイプラインを一時停止。per-reviewer タイムアウト用に `review_requests` データ構造を pipeline.json に追加
- **[v2] verdict/severity 語彙の統一（Pascal C-4 / Leibniz s-1）**: verdict は `APPROVE | P0 | P1` に固定。severity は `critical | major | minor | suggestion` に固定。パーサーにエイリアスマッピング（`Critical→critical`、`REJECT→P0` 等）を実装。大小文字不問
- **[v2] --skip-review 時のレビュアーリスト初期化（Pascal C-5 / Dijkstra M-6）**: spec start 時に `--review-mode` に基づくレビュアーリストを必ず pipeline.json に書き込む。排他制御は atomic な pipeline.json 書き込み（flock）で保証
- **[v2] レビューファイル名の上書き防止（Pascal C-6 / Dijkstra m-4）**: ファイル名を `{YYYYMMDD}T{HHMMSS}_{reviewer}_{spec_name}_rev{N}.md` に変更。ISO8601 秒精度 + アンダースコア区切りでパース曖昧性を排除
- **[v2] batch 機構との分離（Leibniz C-1 / Dijkstra C-2）**: spec mode は既存の `batch[]` 配列を**使用しない**。spec 固有の状態は全て `spec_config` 内に格納。`find_issue()` 等の int 前提関数との型衝突を根本回避
- **[v2] 送信インターフェースの固定（Leibniz C-2）**: レビュー依頼・改訂依頼は `send_to_agent()`（改行保持）で送信。催促のみ `send_to_agent_queued()`（改行消失許容）。仕様内で送信路と制約を明文化
- **[v2] VALID_STATES / VALID_TRANSITIONS への SPEC_* 登録（Leibniz C-3 / Dijkstra C-3）**: config.py に spec mode の全状態と遷移規則を追加。`check_transition_spec()` を純粋関数として設計し、既存の DCL+lock パターンに統合
- **[v2] should_continue_review 擬似コード修正（Leibniz C-4）**: 未定義変数参照を修正。`spec_config["revise_count"]` / `spec_config["max_revise_cycles"]` に統一。P1 で revise する方針と既存 CODE_REVISE（P0 のみ）の差分を明記
- **[v2] spec_config JSON 例とフィールド表の統一（Leibniz C-5 / Dijkstra C-1）**: JSON 例に全フィールドを含める。必須/任意/デフォルト値を明記
- **[v2] 状態集合の整理 — 10 状態 + IDLE（Leibniz C-6）**: SPEC_REVIEW_FAILED, SPEC_STALLED, SPEC_PAUSED の 3 状態を追加。IDLE を終端として明示。早期終了の遷移先を表で固定
- **[v2] QUEUE_FILE 二重定義の解消（Leibniz C-7 / Dijkstra m-2）**: `SPEC_QUEUE_FILE` を廃止。既存 `QUEUE_FILE` に統一し、spec mode も同じ定数を使用
- **[v2] YAML パースの決定性確保（Leibniz C-8 / Dijkstra C-5）**: LLM フォールバックを廃止。パース失敗時は `raw_text` をそのまま保持し、`SPEC_PAUSED` に遷移して人間介入を要求。決定性を最優先
- **[v2] 重複統合の安全化（Leibniz C-9）**: 自動統合を廃止。候補グルーピング（section + 単語重複率）は提示のみ。Critical は常に個別保持。統合判断は spec_implementer に委ねる
- **[v2] per-reviewer タイムアウト用データ構造（Leibniz C-10 / Dijkstra M-3）**: `review_requests: {reviewer: {sent_at, timeout_at, status}}` を pipeline.json に追加。SPEC_REVIEW_TIMEOUT_SEC を 600→1800 に引き上げ
- **[v2] レビュー保存先の規約（Leibniz C-11）**: repo 内 `reviews/` に保存（バージョン管理対象）。main ブランチに直接 commit
- **[v2] rev 命名規則の統一（Leibniz M-1 / Dijkstra s-3）**: `current_rev` を string 型に統一（"1", "2", "2A" 等）。順序管理用に `rev_index: int` を別途保持
- **[v2] CLI→pipeline 写像表の追加（Leibniz M-2 / Dijkstra m-8）**: 全 CLI フラグの保存先を明記
- **[v2] diff 情報の生成方法定義（Leibniz M-3）**: `git diff --numstat <old>..<new> -- <spec_path>` で計算。changelog_summary は実装者の YAML 報告を一次ソースに
- **[v2] レビュー item ID の正規化（Leibniz M-4）**: `{reviewer}:{local_id}`（例: `pascal:C-1`）を正規化 ID として使用
- **[v2] 手動 approve の監査ログ（Leibniz M-5）**: review_history に `actor`, `forced`, `remaining_issues` を記録。Discord に監査通知
- **[v2] check_spec_mode を純粋関数化（Leibniz M-6 / Dijkstra C-3）**: `check_transition_spec() -> SpecTransitionAction` 純粋関数
- **[v2] REJECT verdict の扱い（Dijkstra M-1）**: REJECT を廃止し P0 に統一。エイリアスマッピングで変換
- **[v2] YAML コードブロックのネスト対策（Dijkstra M-2）**: spec全文埋め込み廃止により根本解消。ファイルパス参照のためデリミタ不要
- **[v2] ISSUE_SUGGESTION データフロー定義（Dijkstra M-4）**: `spec_config.issue_suggestions: {}` に格納
- **[v2] SPEC_DONE → IDLE 遷移コマンド（Dijkstra M-5）**: `gokrax spec done` 追加
- **[v2] GitLab Issue 起票の部分失敗リカバリ（Dijkstra M-7）**: `created_issues[]` に逐次記録、リトライ時スキップ
- **[v2] セルフレビュー改善（Dijkstra M-8）**: パス2は別エージェント（レビュアーの1人）に依頼
- **[v2] §6.3 セクション番号重複修正（Leibniz m-1 / Dijkstra C-4）**: §6.4 に繰り下げ
- **[v2] Discord 通知の文字数制限対応（Leibniz m-2）**: 箇条書きベース、2000字超過時は分割
- **[v2] spec 全文埋め込み廃止（Leibniz m-3 / Dijkstra m-6 / M指示）**: spec本文は埋め込まず、ファイルパス参照に統一。レビュアーが自分で読む前提
- **[v2] Issue 注記の存在検査（Leibniz m-4）**: 起票後に読み戻して⚠️注記を検査、欠落時は自動追記
- **[v2] M 確認ゲート追加（Dijkstra m-5）**: SPEC_APPROVED → ISSUE_SUGGESTION は `gokrax spec continue` で明示的に進行
- **[v2] 失敗系通知の追加（Leibniz s-2）**: パース失敗、送信失敗、git push失敗、glab起票失敗を通知
- **[v2] DAG 修正: S-6 依存先（Dijkstra m-7）**: S-4→S-1,S-3 に修正
- **[v2] §1.2 スコープ修正（Dijkstra s-1）**: 「上記 2〜9」に修正
- **[v2] MergedReviewReport 型定義追加（Dijkstra s-2）**
- **[v2] S-4 行数見積もり修正（Dijkstra s-4）**: 150→250行
- **[v2] 早期終了オプション真理値表追加（Dijkstra s-5）**
- **[v2] エラーハンドリング方針（Dijkstra s-6）**: 未知状態は SPEC_PAUSED + M通知
- **[v2] 重複検出アルゴリズム簡素化（Dijkstra m-3 / M指示）**: 初期実装ではアルゴリズム不要。implementer（Opus）に委任。将来はembedding類似度を検討
- **[v2] REVISE完了通知のcommit空対応（Dijkstra m-9）**: commit hash空時は `(commit: N/A)` 表示 + SPEC_PAUSED遷移

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
9. gokrax-queue.txt にバッチ実行順・モデル指定を手動で記述

TrajOpt unified-gui-spec では rev1 → rev4A まで 5 ラウンド、延べ 60+ 件のレビュー指摘を処理した。各ラウンドで数時間の手作業が発生している。

### 1.2 目標

<!-- [v2] Dijkstra s-1: ステップ1はスコープ外 -->
gokrax に **spec mode** を追加し、上記 2〜9 を自動化する:

- spec ファイルのレビュー依頼・回収・状態遷移を gokrax が管理
- レビュー結果の構造化パース・重複候補提示を自動実行
- spec 改訂をエージェント（spec_implementer）が自動実行
- レビューループの終了判定（P1以上なし or MAX_CYCLES到達 → 人間介入）
- 完成 spec から Issue 分割案の収集・統合・GitLab 起票
- gokrax-queue.txt へのバッチ行生成

### 1.3 スコープ

**スコープ内:** spec レビューサイクルの自動化、Issue 分割の半自動化、キュー生成の自動化

**スコープ外:** spec 叩き台の自動生成、gokrax 実装フローとの直接接続、ブートストラップ

---

## 2. ステートマシン

### 2.1 状態遷移図

```
[gokrax spec start]
        │
        ├─── [--skip-review] ───→ SPEC_APPROVED
        │
        ▼
  SPEC_REVIEW ◄──────────────┐
        │                    │
        │ (有効レビュー回収   │
        │  or タイムアウト)   │
        ▼                    │
  ┌─ SPEC_REVISE ────────────┘
  │     │       (P1以上あり & revise_count < MAX: ループ継続)
  │     │
  │     │ (P1以上なし)
  │     ▼
  │   SPEC_APPROVED ──── [--no-issue] ───→ SPEC_DONE
  │     │
  │     │ [gokrax spec continue]
  │     ▼
  │   ISSUE_SUGGESTION
  │     │
  │     ▼
  │   ISSUE_PLAN
  │     │
  │     ▼
  │   QUEUE_PLAN ─── [--no-queue] ───→ SPEC_DONE
  │     │
  │     ▼
  │   SPEC_DONE ──── [gokrax spec done] ───→ IDLE
  │
  │ (MAX_CYCLES到達 & P1以上残存)
  └──→ SPEC_STALLED ─→ [approve --force] → SPEC_APPROVED
                   └─→ SPEC_REVIEW (追加レビュー)

  ※ 異常系:
  SPEC_REVIEW_FAILED ←── (有効レビュー 0 件)
  SPEC_PAUSED ←── (MAX_RETRIES超過 / パース失敗 / 未知状態)
```

### 2.2 状態定義

<!-- [v2] Leibniz C-6: 10状態+IDLE -->

| 状態 | 説明 | 入り口 | 出口 |
|---|---|---|---|
| `SPEC_REVIEW` | レビュアーにspec送信、回収待ち | start or SPEC_REVISE完了 | 有効≥MIN → SPEC_REVISE / 有効0 → FAILED |
| `SPEC_REVISE` | 統合レポート生成、implementer改訂 | SPEC_REVIEW完了 | commit完了 → 終了判定 |
| `SPEC_APPROVED` | 改訂サイクル完了、M確認待ち | REVISE(終了条件) or --force | continue → ISSUE_SUGGESTION / --no-issue → DONE |
| `ISSUE_SUGGESTION` | レビュアーにIssue分割案問い合わせ | APPROVED(M確認後) | 回収完了 → ISSUE_PLAN |
| `ISSUE_PLAN` | implementerが統合→GitLab起票 | SUGGESTION完了 | 起票完了 → QUEUE_PLAN |
| `QUEUE_PLAN` | gokrax-queue.txt生成 | PLAN完了 | 生成完了 → DONE |
| `SPEC_DONE` | 全工程完了、M最終確認待ち | QUEUE_PLAN or 早期終了 | `spec done` → IDLE |
| `SPEC_STALLED` | MAX_CYCLES & P1残存、M介入必須 | REVISE(MAX到達&has_p1) | --force → APPROVED / → REVIEW |
| `SPEC_REVIEW_FAILED` | 有効レビュー0件 | REVIEW | `spec retry` → REVIEW |
| `SPEC_PAUSED` | リトライ超過/パース失敗/異常 | 任意 | `spec resume` → 前状態 |
| `IDLE` | 非稼働 | DONE(M確認後) | — |

### 2.3 既存ステートとの共存

<!-- [v2] Leibniz C-3 / Dijkstra C-3 -->

spec modeステートは既存実装フローステートと**排他**。

```python
# [v2] Leibniz C-3 / Dijkstra C-3
SPEC_STATES = [
    "SPEC_REVIEW", "SPEC_REVISE", "SPEC_APPROVED",
    "ISSUE_SUGGESTION", "ISSUE_PLAN", "QUEUE_PLAN", "SPEC_DONE",
    "SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED",
]
VALID_STATES = VALID_STATES + SPEC_STATES

SPEC_TRANSITIONS = {
    "IDLE":                 ["SPEC_REVIEW", "SPEC_APPROVED"],
    "SPEC_REVIEW":          ["SPEC_REVISE", "SPEC_REVIEW_FAILED", "SPEC_PAUSED"],
    "SPEC_REVISE":          ["SPEC_REVIEW", "SPEC_APPROVED", "SPEC_STALLED", "SPEC_PAUSED"],
    "SPEC_APPROVED":        ["ISSUE_SUGGESTION", "SPEC_DONE"],
    "ISSUE_SUGGESTION":     ["ISSUE_PLAN", "SPEC_PAUSED"],
    "ISSUE_PLAN":           ["QUEUE_PLAN", "SPEC_DONE", "SPEC_PAUSED"],
    "QUEUE_PLAN":           ["SPEC_DONE", "SPEC_PAUSED"],
    "SPEC_DONE":            ["IDLE"],
    "SPEC_STALLED":         ["SPEC_APPROVED", "SPEC_REVIEW"],
    "SPEC_REVIEW_FAILED":   ["SPEC_REVIEW"],
    "SPEC_PAUSED":          ["SPEC_REVIEW", "SPEC_REVISE", "ISSUE_SUGGESTION",
                             "ISSUE_PLAN", "QUEUE_PLAN"],
}
VALID_TRANSITIONS.update(SPEC_TRANSITIONS)
STATE_PHASE_MAP.update({s: "spec" for s in SPEC_STATES})
```

**排他制御:**
<!-- [v2] Pascal C-5 / Dijkstra M-6 -->
`gokrax spec start` は pipeline.json を flock で排他ロック → `spec_mode = true` を atomic に設定。`spec_mode = true` の間、既存 `gokrax start` / `gokrax transition` はエラー。IDLE遷移時に `spec_mode = false` にクリア。

### 2.4 終了条件

<!-- [v2] Pascal C-1, C-2 -->

| 条件 | 動作 |
|---|---|
| 有効レビュー ≥ MIN_VALID_REVIEWS かつ全verdict ≤ Suggestion | → SPEC_APPROVED |
| 有効レビュー < MIN_VALID_REVIEWS | → SPEC_REVIEW_FAILED |
| MAX_CYCLES到達 かつ P1以上なし | → SPEC_APPROVED |
| MAX_CYCLES到達 かつ P1以上残存 | → SPEC_STALLED（M介入必須）|
| M が `approve --force` | → SPEC_APPROVED（監査ログ付き）|

定数: `MAX_SPEC_REVISE_CYCLES = 5`, `MIN_VALID_REVIEWS = 1`

### 2.5 早期終了オプション

<!-- [v2] Dijkstra s-5: 真理値表 -->

| --skip-review | --no-issue | --no-queue | 開始 | 終了 | 用途 |
|---|---|---|---|---|---|
| ✗ | ✗ | ✗ | REVIEW | DONE | 全工程 |
| ✗ | ✓ | — | REVIEW | DONE | コード以外のspec |
| ✗ | ✗ | ✓ | REVIEW | DONE | Issue起票まで |
| ✓ | ✗ | ✗ | APPROVED | DONE | Issue化+キュー |
| ✓ | ✗ | ✓ | APPROVED | DONE | Issue起票のみ |
| ✓ | ✓ | — | **エラー** | — | 無意味 |

---

## 3. パイプライン設定

### 3.1 pipeline.json 拡張

<!-- [v2] Leibniz C-1 / Dijkstra C-2: batch使用廃止 -->
<!-- [v2] Leibniz C-5 / Dijkstra C-1: JSON例とフィールド表統一 -->

spec modeは既存 `batch[]` を**使用しない**。全てを `spec_config` に格納。

```json
{
  "project": "gokrax",
  "state": "SPEC_REVIEW",
  "spec_mode": true,
  "spec_config": {
    "spec_path": "docs/spec-mode-spec.md",
    "spec_implementer": "second",
    "no_issue": false,
    "no_queue": false,
    "skip_review": false,
    "self_review_passes": 2,
    "current_rev": "1",
    "rev_index": 1,
    "max_revise_cycles": 5,
    "revise_count": 0,
    "last_commit": null,
    "model": null,
    "review_requests": {},
    "issue_suggestions": {},
    "created_issues": [],
    "review_history": [],
    "retry_count": 0,
    "paused_from": null
  },
  "enabled": true,
  "review_mode": "full",
  "batch": []
}
```

| フィールド | 型 | 必須 | デフォルト | 説明 |
|---|---|---|---|---|
| spec_path | str | ✅ | — | specファイルのリポジトリ相対パス |
| spec_implementer | str | ✅ | — | 改訂エージェントID |
| no_issue | bool | — | false | Issue分割スキップ |
| no_queue | bool | — | false | キュー生成スキップ |
| skip_review | bool | — | false | レビュースキップ |
| self_review_passes | int | — | 2 | セルフレビュー回数 |
| current_rev | str | — | "1" | リビジョン（"1","2","2A"等）|
| rev_index | int | — | 1 | 順序管理用連番 |
| max_revise_cycles | int | — | 5 | 最大改訂サイクル数 |
| revise_count | int | — | 0 | 現在の改訂サイクル数 |
| last_commit | str\|null | — | null | 前revのcommit hash |
| model | str\|null | — | null | implementerモデル参考情報 |
| review_requests | dict | — | {} | per-reviewerタイムアウト管理 |
| issue_suggestions | dict | — | {} | Issue分割提案 |
| created_issues | list[int] | — | [] | 起票済みIssue番号 |
| review_history | list | — | [] | ラウンド結果サマリー |
| retry_count | int | — | 0 | リトライ回数 |
| paused_from | str\|null | — | null | PAUSED復帰先 |

### 3.2 config.py 追加定数

```python
# [v2] Leibniz C-7 / Dijkstra m-2: SPEC_QUEUE_FILE廃止、既存QUEUE_FILEに統一
MAX_SPEC_REVISE_CYCLES = 5
MIN_VALID_REVIEWS = 1                       # [v2] Pascal C-1
SPEC_REVIEW_TIMEOUT_SEC = 1800              # [v2] Dijkstra M-3: 600→1800
SPEC_REVISE_TIMEOUT_SEC = 1800
SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC = 600
SPEC_REVISE_SELF_REVIEW_PASSES = 2
MAX_SPEC_RETRIES = 3                        # [v2] Pascal C-3
# MAX_SPEC_EMBED_CHARS 廃止 — spec全文埋め込みをやめ、ファイルパス参照に統一
```

### 3.3 CLI→pipeline 写像表

<!-- [v2] Leibniz M-2 / Dijkstra m-8 -->

| CLIフラグ | 保存先 | 型 |
|---|---|---|
| --pj | project | str |
| --spec | spec_config.spec_path | str |
| --implementer | spec_config.spec_implementer | str |
| --no-issue | spec_config.no_issue | bool |
| --no-queue | spec_config.no_queue | bool |
| --skip-review | spec_config.skip_review | bool |
| --max-cycles | spec_config.max_revise_cycles | int |
| --review-mode | review_mode | str |
| --model | spec_config.model | str\|null |

---

## 4. CLIインターフェース

### 4.1 コマンド体系

```
gokrax spec start      パイプライン開始
gokrax spec approve    SPEC_APPROVEDに遷移 [--force]
gokrax spec continue   APPROVED → ISSUE_SUGGESTION
gokrax spec done       DONE → IDLE
gokrax spec retry      FAILED → REVIEW
gokrax spec resume     PAUSED → 前状態
gokrax spec status     ステータス表示
```

### 4.2 gokrax spec start

```
gokrax spec start --pj PROJECT --spec SPEC_PATH --implementer AGENT_ID
                  [--no-issue] [--no-queue] [--skip-review]
                  [--max-cycles N] [--review-mode MODE] [--model MODEL]
```

**前提条件:** IDLE状態、specファイル存在、implementer利用可能、`--skip-review --no-issue`はエラー

**動作:**
1. pipeline.json を flock で排他ロック
2. spec_mode=true + spec_config書き込み
3. review_requestsにレビュアーリスト初期化（--skip-reviewでも初期化）
4. enabled=true
5. --skip-review → APPROVED、そうでなければ → REVIEW

### 4.3 gokrax spec approve

```
gokrax spec approve --pj PROJECT [--force]
```

--forceなし: P1以上あればエラー。--forceあり: 強制承認 + 監査ログ + Discord通知。

### 4.4 gokrax spec status

```
gokrax [SPEC_REVIEW] rev2 (cycle 1/5, retries: 0/3)
  spec: docs/spec-mode-spec.md
  implementer: second
  reviewers: pascal(✅ P0×2), leibniz(✅ P0×11), dijkstra(⏳ pending)
  rev1: C:18 M:14 m:14 S:8 (42件)
```

---

## 5. SPEC_REVIEWフェーズ

### 5.1 レビュー依頼の送信

<!-- [v2] Leibniz C-2: send_to_agent()固定 -->
<!-- [v2+] M指示: spec全文埋め込み廃止。ファイルパス参照に統一 -->

1. 各レビュアーに **`send_to_agent()`**（改行保持）でレビュー依頼を送信
2. `review_requests[reviewer].sent_at` を記録
3. spec 本文は**埋め込まない**。レビュアーがファイルパスから自分で読む前提

**初回プロンプト（実績ベースのフォーマット）:**

```
以下の仕様書をレビューしてください。**やりすぎレビュー**を依頼します。重箱の隅を突くレベルで徹底的に。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev}, {line_count}行)

## レビュー指示
- 重篤度を必ず付与: 🔴 Critical (P0) / 🟠 Major (P1) / 🟡 Minor / 💡 Suggestion
- セクション番号を明記（例: §6.2）
- 擬似コード間の整合性（引数・型・呼び出し規約）に特に注意
- 実装時に詰まりそうな曖昧さを指摘
- 既存の gokrax コードベース（watchdog.py, notify.py, config.py, gokrax.py）との整合性も確認
- ステートマシン遷移の抜け穴・デッドロック・競合状態を探せ
- エラーハンドリングの欠如箇所を指摘
- プロンプトテンプレートの不整合や曖昧さを指摘

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

## レビュー結果の保存
レビュー完了後、結果を以下のファイルに保存してください:
`{repo_path}/reviews/{date}_{reviewer}_{spec_name}_rev{current_rev}.md`

ファイル内容はレビュー全文（verdict + 全items + 補足説明）をmarkdownで。
```

**rev2以降のプロンプト:**

diff情報は `git diff --numstat {last_commit}..HEAD -- {spec_path}` で計算。changelog_summary は実装者の改訂完了報告 `changes:` を一次ソースとする。

```
以下の仕様書の改訂版をレビューしてください。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})
前回からの変更: +{added_lines}行, -{removed_lines}行
前回 commit: {last_commit}

## 前回レビューからの変更点
{changelog_summary}

## レビュー指示
- 前回の指摘が適切に反映されているか確認
- 新たに追加された部分に問題がないか確認
- 重篤度・セクション番号・YAML フォーマットは前回と同様

## レビュー結果の保存
`{repo_path}/reviews/{date}_{reviewer}_{spec_name}_rev{current_rev}.md`
```

### 5.2 レビュー回収

<!-- [v2] Leibniz C-10 / Dijkstra M-3: per-reviewer -->

```json
"review_requests": {
  "pascal": {
    "sent_at": "2026-02-28T21:15:00+09:00",
    "timeout_at": "2026-02-28T21:45:00+09:00",
    "last_nudge_at": null,
    "status": "pending",
    "response": null
  }
}
```

全reviewer status = received|timeout → 判定:
- received ≥ MIN_VALID_REVIEWS → SPEC_REVISE
- received < MIN_VALID_REVIEWS → SPEC_REVIEW_FAILED

### 5.3 レビュー結果のパース

<!-- [v2] Leibniz C-8 / Dijkstra C-5: LLMフォールバック廃止 -->

**決定性最優先。**

1. YAMLブロック正規表現抽出（最初の1ブロックのみ）
2. verdict/severityにエイリアスマッピング適用:

```python
VERDICT_ALIASES = {
    "approve": "APPROVE", "APPROVE": "APPROVE",
    "p0": "P0", "P0": "P0", "critical": "P0",
    "reject": "P0", "REJECT": "P0",  # [v2] Dijkstra M-1
    "p1": "P1", "P1": "P1", "major": "P1",
}
SEVERITY_ALIASES = {
    "critical": "critical", "Critical": "critical", "P0": "critical",
    "major": "major", "Major": "major", "P1": "major",
    "minor": "minor", "Minor": "minor",
    "suggestion": "suggestion", "Suggestion": "suggestion",
}
```

3. パース失敗 → raw_text保持、parse_success=False。全員失敗 → SPEC_PAUSED

```python
@dataclass
class SpecReviewItem:
    id: str                    # "C-1" (reviewer-local)
    severity: str              # "critical"|"major"|"minor"|"suggestion"
    section: str               # "§6.2"
    title: str
    description: str
    suggestion: str | None
    reviewer: str
    normalized_id: str         # [v2] Leibniz M-4: "pascal:C-1"

@dataclass
class SpecReviewResult:
    reviewer: str
    verdict: str               # "APPROVE"|"P0"|"P1"
    items: list[SpecReviewItem]
    raw_text: str
    parse_success: bool

@dataclass
class MergedReviewReport:  # [v2] Dijkstra s-2
    reviews: list[SpecReviewResult]
    all_items: list[SpecReviewItem]
    duplicate_groups: list[list[str]]   # 提示のみ
    summary: dict                       # {"critical": n, ...}
    highest_verdict: str
```

### 5.4 重複検出・統合

<!-- [v2] Leibniz C-9: 自動統合廃止 -->

**自動統合は行わない。Critical は常に個別保持。**

**初期実装:** 重複検出アルゴリズムは実装しない。統合レポートに全指摘を重篤度順で列挙し、重複判断は spec_implementer（Opus）に委ねる。理由: 仕様書の技術的指摘の重複判定は単語マッチや形態素解析では精度が出ない。将来的に embedding 類似度ベースの候補提示を検討。

統合レポートフォーマット:
```markdown
# Rev{N} レビュー統合レポート
## サマリー
- レビュアー: {reviewer} ({verdict}), ...
- Critical: {n}件, Major: {n}件, Minor: {n}件, Suggestion: {n}件
## 全指摘一覧（重篤度順）
### Critical — {normalized_id}: {title} ({section})
### Major — ...
### Minor — ...
### Suggestion — ...
```

---

## 6. SPEC_REVISEフェーズ

### 6.1 改訂プロセス

`send_to_agent()` で改訂依頼。TrajOpt/EMCalibrator形式のchangelogを要求:
- 変更履歴テーブルに1行追加
- `[vN] 指摘元ID: 説明` 形式で全件列挙
- 擬似コード中 `# [vN] Pascal C-1: 説明` で変更理由記載

### 6.2 セルフレビュー

<!-- [v2] Dijkstra M-8: パス2は別エージェント -->

**パス1（implementer自身）:** 反映漏れ、矛盾、整合性、changelog確認
**パス2（別エージェント）:** 第三者クロスチェック

### 6.3 改訂完了の検知

1. YAML `status: done` 確認
2. セルフレビュー パス1 + パス2
3. last_commit, current_rev, rev_index 更新
4. review_history にラウンド結果追加

### 6.4 終了判定

<!-- [v2] Leibniz m-1 / Dijkstra C-4: §6.3→§6.4 -->
<!-- [v2] Leibniz C-4: 擬似コード修正 -->

```python
def should_continue_review(
    spec_config: dict,
    reviews: list[SpecReviewResult],
) -> str:  # "continue"|"approved"|"stalled"|"failed"
    valid = [r for r in reviews if r.parse_success]
    if len(valid) < MIN_VALID_REVIEWS:
        return "failed"
    has_p1 = any(r.verdict in ("P0", "P1") for r in valid)
    if spec_config["revise_count"] >= spec_config["max_revise_cycles"]:
        return "stalled" if has_p1 else "approved"
    return "continue" if has_p1 else "approved"
```

**既存CODE_REVISEとの差異:** 既存はP0のみreviseトリガ。spec modeは**P1以上でループ継続**。理由: Major指摘を残して実装に進むと手戻りが大きい。

---

## 7. ISSUE_SUGGESTIONフェーズ

<!-- [v2] Dijkstra M-4, m-5 -->

M が `gokrax spec continue` 実行後に遷移。レビュアーにIssue分割案を問い合わせ。

回収は `spec_config.issue_suggestions` に格納。

---

## 8. ISSUE_PLANフェーズ

### 8.1 Issue統合と起票

<!-- [v2] Dijkstra M-7 -->
起票済みIssue番号を逐次 `created_issues[]` に記録。リトライ時はスキップ。

### 8.2 注記の存在検査

<!-- [v2] Leibniz m-4 -->
起票後に `glab issue show` で読み戻し、⚠️注記を検査。欠落時は自動追記。

---

## 9. QUEUE_PLANフェーズ

<!-- [v2] Leibniz C-7: 既存QUEUE_FILEに統一 -->

`config.QUEUE_FILE` に追記。完了後 → SPEC_DONE。M が `gokrax spec done` で IDLE。

---

## 10. Watchdog統合

### 10.1 watchdog.py拡張

<!-- [v2] Leibniz M-6 / Dijkstra C-3: 純粋関数 -->

```python
@dataclass
class SpecTransitionAction:
    next_state: str | None
    send_to: dict[str, str] | None
    notify: str | None
    save_data: dict | None
    error: str | None

def check_transition_spec(
    state: str, spec_config: dict, review_data: dict, now: datetime,
) -> SpecTransitionAction:
    """純粋関数。副作用なし。"""
    if state not in SPEC_STATES:  # [v2] Dijkstra s-6
        return SpecTransitionAction(next_state="SPEC_PAUSED", error=f"Unknown: {state}")
    if state == "SPEC_REVIEW":
        return _check_spec_review(spec_config, review_data, now)
    elif state == "SPEC_REVISE":
        return _check_spec_revise(spec_config, review_data, now)
    elif state == "SPEC_APPROVED":
        return SpecTransitionAction(next_state=None)  # M操作待ち
    elif state in ("SPEC_DONE", "SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED"):
        return SpecTransitionAction(next_state=None)
    # ISSUE_SUGGESTION, ISSUE_PLAN, QUEUE_PLAN
    ...
```

### 10.2 タイムアウトと催促

<!-- [v2] Pascal C-3 -->

| 状態 | タイムアウト | タイムアウト後 | MAX_RETRIES超過 |
|---|---|---|---|
| SPEC_REVIEW | 1800s/reviewer | 応答済みのみで判定 | PAUSED |
| SPEC_REVISE | 1800s | retry++ & 再送 | PAUSED |
| ISSUE_SUGGESTION | 600s/reviewer | 応答済みのみで遷移 | PAUSED |
| ISSUE_PLAN | 1800s | retry++ & 再送 | PAUSED |
| QUEUE_PLAN | 1800s | retry++ & 再送 | PAUSED |

---

## 11. notify.py拡張

<!-- [v2] Leibniz m-2: 2000字制限 -->
<!-- [v2] Leibniz s-2: 失敗系 -->

箇条書きベース。2000字超過時は分割。

**成功系:** REVIEW開始/完了、REVISE完了、APPROVED、APPROVED(forced+監査)、ISSUE完了、QUEUE完了

<!-- [v2] Dijkstra m-9: commit空の場合 -->
**REVISE完了通知:** commit hash が空の場合（git commit 失敗等）は `(commit: N/A)` と表示し、SPEC_PAUSED に遷移。

**失敗系:** YAMLパース失敗、送信失敗、git push失敗、glab起票失敗、REVIEW_FAILED、STALLED、PAUSED

---

## 12. レビュー結果の保存

### 12.1 ファイル保存

<!-- [v2] Pascal C-6 / Dijkstra m-4 / Leibniz C-11 -->

repo内 `reviews/` に保存（mainブランチ直接commit）。

```
reviews/{YYYYMMDD}T{HHMMSS}_{reviewer}_{spec_name}_rev{N}.md
reviews/{YYYYMMDD}T{HHMMSS}_merged_{spec_name}_rev{N}.md
```

### 12.2 review_history

```json
{
  "rev": "1", "rev_index": 1,
  "reviews": {"pascal": {"verdict": "P0", "counts": {...}}, ...},
  "merged_counts": {"critical": 18, "major": 14, "minor": 14, "suggestion": 8},
  "commit": "82ec516",
  "timestamp": "2026-02-28T21:15:00+09:00",
  "forced": false, "actor": null
}
```

---

## 13. 実装計画

### 13.1 変更ファイル

| ファイル | 内容 | 規模 |
|---|---|---|
| gokrax.py | spec CLI（7コマンド）| +250行 |
| watchdog.py | check_transition_spec + 各状態 | +300行 |
| notify.py | 通知（成功+失敗）| +120行 |
| config.py | 定数 + SPEC_STATES + TRANSITIONS | +50行 |
| pipeline_io.py | 初期化・バリデーション・flock | +60行 |
| spec_review.py | **新規** パース・エイリアス・重複候補・統合 | +350行 |
| spec_revise.py | **新規** 改訂依頼・完了パース・終了判定 | +250行 |
| spec_issue.py | **新規** Issue分割・起票・注記検査・キュー | +300行 |
| tests/ | テスト | +500行 |
| **合計** | | **~2,180行** |

### 13.2 Issue分割案

| Issue | タイトル | 依存 | 行数 |
|---|---|---|---|
| S-1 | config.py + pipeline_io.py: spec mode基盤 | なし | +110 |
| S-2 | gokrax.py: spec CLI | S-1 | +250 |
| S-3 | spec_review.py: パース+エイリアス+重複候補 | S-1 | +350 |
| S-4 | watchdog.py: REVIEW/REVISE判定 | S-2,S-3 | +250 |
| S-5 | spec_revise.py: 改訂+セルフレビュー+終了判定 | S-3 | +250 |
| S-6 | spec_issue.py: SUGGESTION+PLAN+注記+QUEUE | S-1,S-3 | +300 |
| S-7 | notify.py: 通知 | S-4 | +120 |
| S-8 | watchdog.py: ISSUE系+異常系 | S-6,S-7 | +150 |
| S-9 | 統合テスト | S-8 | +500 |

### 13.3 依存関係DAG

```
S-1 ──┬── S-2 ──┐
      │         ├── S-4 ── S-7
      ├── S-3 ──┘    │
      │    │         │
      │    └── S-5   ├── S-8 ── S-9
      │              │
      └── S-6 ───────┘
```

---

## 14. 将来の拡張

- gokrax実装フローとの接続（SPEC_DONE → qrun自動開始）
- spec叩き台の自動生成
- 差分レビュー（トークン節約）
- レビュアー学習（傾向分析）
- SPEC_PAUSED自動リカバリ（一過性エラー）
