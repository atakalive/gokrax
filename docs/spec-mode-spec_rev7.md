# DevBar Spec Mode — 仕様書

**Version:** 7.0 (rev6 review reflected — Pascal/Leibniz/Dijkstra)
**Date:** 2026-03-01
**Author:** Asuka (second) + M
**Reviewers:** Pascal (Gemini 3 Pro), Leibniz (GPT-5.2), Dijkstra (Opus)

---

### 変更履歴

| Version | Date | 内容 |
|---|---|---|
| 1.0 | 2026-02-28 | 初版（821行）|
| 2.0 | 2026-02-28 | rev1レビュー反映（42件）|
| 3.0 | 2026-02-28 | rev2レビュー反映（24件）+ M指示3件 |
| 4.0 | 2026-03-01 | rev3レビュー反映（Pascal 4件、Leibniz 7件、Dijkstra 11件 → 重複排除後15件）|
| 5.0 | 2026-03-01 | rev4レビュー反映（Pascal 3件、Leibniz 5件、Dijkstra 4件 → 重複排除後8件）|
| 6.0 | 2026-03-01 | rev5レビュー反映（Pascal 2件、Leibniz 5件、Dijkstra 2件 → 9件）|
| 7.0 | 2026-03-01 | rev6レビュー反映（Pascal 2件、Leibniz 5件、Dijkstra 1件 → 7件）。**Critical 0達成** |

**v6→v7 の主要変更点:**

- **[v7] §5.5パース失敗時のstatus設定明記（Pascal P-1）**: parse_success=Falseと同時にstatus='parse_failed'を設定する指示を追記
- **[v7] SPEC_REVISE resume時の再始動地点明記（Pascal P-2）**: §6.1の改訂要求からプロセス再始動
- **[v7] current_reviewsのメタ/本体分離（Leibniz M-1）**: reviewed_revをトップレベル、reviewerエントリをentries配下にネスト
- **[v7] last_changes検証（Leibniz M-2）**: added/removed_linesはgit diff numstatを一次ソース。last_changesとの不一致時は警告
- **[v7] status遷移規則とreceived必須フィールド明記（Leibniz M-3）**: pending→received|timeout|parse_failed。received時のverdict必須条件
- **[v7] §4.6ステップ番号重複修正（Pascal P-2 / Leibniz m-1 / Dijkstra m-1）**: 再採番

---

v1→v2, v2→v3の変更履歴詳細は本文末尾の「附録A」を参照。

---

## 1. 目的と背景

### 1.1 現状の問題

仕様書（spec）の作成・レビュー・改訂サイクルは現在すべて手動:

1. アスカ（or他エージェント）がMと対話しながらspec叩き台を作成
2. `sessions_send` で3人のレビュアーに個別送信
3. レビュー結果を待つ
4. 3人分を手動で分析・重複排除・統合
5. specファイルを手動で改訂（revN → revN+1）
6. git commit & push
7. 2〜6を繰り返し（TrajOptは5ラウンド）
8. 完成specから手動でGitLab Issue起票（TrajOptは19件）
9. devbar-queue.txtにバッチ実行順を手動記述

### 1.2 目標

devbarに**spec mode**を追加し、上記2〜9を自動化する。

### 1.3 スコープ

**スコープ内:** specレビューサイクル自動化、Issue分割半自動化、キュー生成自動化

**スコープ外:** spec叩き台の自動生成、devbar実装フローとの直接接続、ブートストラップ

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
        │ (有効レビュー回収   │
        │  or タイムアウト)   │
        ▼                    │
  ┌─ SPEC_REVISE ────────────┘  ← [v4] REVISEは常にREVIEWへ戻る
  │
  │ (SPEC_REVIEW完了後の判定で P1以上なし)
  │     ▼
  │   SPEC_APPROVED ──── [--review-only] ───→ SPEC_DONE
  │     │
  │     │ [devbar spec continue] or [--auto-continue]
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
  │   SPEC_DONE ──── [devbar spec done] ───→ IDLE
  │
  │ (MAX_CYCLES到達 & P1以上残存)
  └──→ SPEC_STALLED ─→ [spec extend] → SPEC_REVISE (MAX増加、既存指摘で改訂)
                   └─→ [spec approve --force] → SPEC_APPROVED

  ※ 異常系:
  SPEC_REVIEW_FAILED ←── (有効レビュー 0 件、全員タイムアウト)
  SPEC_PAUSED ←── (MAX_RETRIES超過 / パース失敗+有効不足 / 未知状態)
```

### 2.2 状態定義

| 状態 | 説明 | 出口 |
|---|---|---|
| `SPEC_REVIEW` | レビュアーにspec送信、回収待ち | should_continue_review()（§5.3）で判定 |
| `SPEC_REVISE` | 統合レポート生成、implementer改訂 | commit完了 → SPEC_REVIEW |
| `SPEC_APPROVED` | 改訂サイクル完了 | auto_continue → ISSUE_SUGGESTION / デフォルト → M確認待ち / --review-only → DONE |
| `ISSUE_SUGGESTION` | レビュアーにIssue分割案問い合わせ | 回収完了 → ISSUE_PLAN |
| `ISSUE_PLAN` | implementerが統合→GitLab起票 | 起票完了 → QUEUE_PLAN |
| `QUEUE_PLAN` | devbar-queue.txt生成 | 生成完了 → DONE |
| `SPEC_DONE` | 全工程完了、M最終確認待ち | `spec done` → IDLE |
| `SPEC_STALLED` | MAX_CYCLES & P1残存、M介入必須 | extend → REVISE / --force → APPROVED |
| `SPEC_REVIEW_FAILED` | 有効レビュー0件（全員タイムアウト）| `spec retry` → REVIEW |
| `SPEC_PAUSED` | リトライ超過/パース失敗+有効不足/異常 | `spec resume` → paused_from |
| `IDLE` | 非稼働 | — |

### 2.3 既存ステートとの共存

```python
SPEC_STATES = [
    "SPEC_REVIEW", "SPEC_REVISE", "SPEC_APPROVED",
    "ISSUE_SUGGESTION", "ISSUE_PLAN", "QUEUE_PLAN", "SPEC_DONE",
    "SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED",
]
VALID_STATES = VALID_STATES + SPEC_STATES

SPEC_TRANSITIONS = {
    "IDLE":                 ["SPEC_REVIEW", "SPEC_APPROVED"],
    "SPEC_REVIEW":          ["SPEC_REVISE", "SPEC_APPROVED", "SPEC_STALLED",
                             "SPEC_REVIEW_FAILED", "SPEC_PAUSED"],
    # [v4] REVISEはREVIEWかPAUSEDのみ。直接APPROVEDには行かない
    "SPEC_REVISE":          ["SPEC_REVIEW", "SPEC_PAUSED"],
    "SPEC_APPROVED":        ["ISSUE_SUGGESTION", "SPEC_DONE"],
    "ISSUE_SUGGESTION":     ["ISSUE_PLAN", "SPEC_PAUSED"],
    "ISSUE_PLAN":           ["QUEUE_PLAN", "SPEC_DONE", "SPEC_PAUSED"],
    "QUEUE_PLAN":           ["SPEC_DONE", "SPEC_PAUSED"],
    "SPEC_DONE":            ["IDLE"],
    # [v5] Pascal P-1: extend→REVISE直行（空転回避）
    "SPEC_STALLED":         ["SPEC_APPROVED", "SPEC_REVISE"],
    "SPEC_REVIEW_FAILED":   ["SPEC_REVIEW"],
    "SPEC_PAUSED":          ["SPEC_REVIEW", "SPEC_REVISE", "SPEC_APPROVED",
                             "ISSUE_SUGGESTION", "ISSUE_PLAN", "QUEUE_PLAN",
                             "SPEC_DONE"],
}

# [v4] Leibniz C-3: sorted(set(...)) で順序固定
for state, targets in SPEC_TRANSITIONS.items():
    existing = VALID_TRANSITIONS.get(state, [])
    VALID_TRANSITIONS[state] = sorted(set(existing + targets))

STATE_PHASE_MAP.update({s: "spec" for s in SPEC_STATES})
```

**排他制御:** `devbar spec start` は pipeline.json を flock で排他ロック → `spec_mode = true` を atomic に設定。`spec_mode = true` の間、既存 `devbar start` / `devbar transition` はエラー。IDLE遷移時に `spec_mode = false` にクリア。

### 2.4 終了条件（要約）

<!-- [v4] Leibniz M-1: §2.4は要約。正規ロジックは§5.3 -->

SPEC_REVIEW完了後の判定は `should_continue_review()`（§5.3）が唯一の正規ソース。以下は要約:

| 条件 | 結果 |
|---|---|
| 有効レビュー0件（全員タイムアウト）| REVIEW_FAILED |
| パース失敗あり & 有効 < MIN | PAUSED |
| 有効 < MIN（パース失敗なし）| REVIEW_FAILED |
| P1以上なし | APPROVED |
| MAX到達 & P1以上 | STALLED |
| P1以上 & MAX未到達 | REVISE（→改訂→REVIEW） |

**定数:**
- `MAX_SPEC_REVISE_CYCLES = 5`
- `MIN_VALID_REVIEWS`: review_modeに追随。full=2, standard=2, lite=1

### 2.5 早期終了オプション

| --skip-review | --review-only | --no-queue | --auto-continue | 開始 | 終了 | M確認 | 用途 |
|---|---|---|---|---|---|---|---|
| ✗ | ✗ | ✗ | ✗ | REVIEW | DONE | APPROVED時 | 全工程（デフォルト）|
| ✗ | ✗ | ✗ | ✓ | REVIEW | DONE | なし | 全工程（自動進行）|
| ✗ | ✓ | — | (無視) | REVIEW | DONE | なし | レビューのみ |
| ✗ | ✗ | ✓ | ✗ | REVIEW | DONE | APPROVED時 | Issue起票まで |
| ✓ | ✗ | ✗ | (強制✓) | APPROVED | DONE | なし | Issue化+キュー |
| ✓ | ✗ | ✓ | (強制✓) | APPROVED | DONE | なし | Issue起票のみ |
| ✓ | ✓ | — | — | **エラー** | — | — | 無意味 |

### 2.6 CLIオプション優先順位

<!-- [v4] Leibniz m-1 -->

| 条件 | 上書きルール |
|---|---|
| `--skip-review` | `auto_continue` を強制true |
| `--review-only` | `auto_continue` を強制false（無視）、`no_queue` を強制true |
| `--review-only` + `--auto-continue` | `review_only` が勝つ（auto_continue無視）|
| `--skip-review` + `--review-only` | **エラー** |

これらの上書きは `devbar spec start` 内で、pipeline.json書き込み前に適用する。

---

## 3. パイプライン設定

### 3.1 pipeline.json 拡張

spec modeは既存 `batch[]` を**使用しない**。全てを `spec_config` に格納。

```json
{
  "project": "DevBar",
  "state": "SPEC_REVIEW",
  "spec_mode": true,
  "spec_config": {
    "spec_path": "docs/spec-mode-spec.md",
    "spec_implementer": "second",
    "review_only": false,
    "no_queue": false,
    "skip_review": false,
    "auto_continue": false,
    "self_review_passes": 2,
    "self_review_agent": null,
    "current_rev": "1",
    "rev_index": 1,
    "max_revise_cycles": 5,
    "revise_count": 0,
    "last_commit": null,
    "model": null,
    "review_requests": {},
    "current_reviews": {},
    "issue_suggestions": {},
    "created_issues": [],
    "review_history": [],
    "force_events": [],
    "retry_counts": {},
    "paused_from": null,
    "pipelines_dir": null,
    "last_changes": null
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
| review_only | bool | — | false | レビューサイクルのみ（Issue分割・キュースキップ）|
| no_queue | bool | — | false | キュー生成スキップ |
| skip_review | bool | — | false | レビュースキップ |
| auto_continue | bool | — | false | SPEC_APPROVED後にM確認なしでISSUE_SUGGESTIONへ自動進行 |
| self_review_passes | int | — | 2 | セルフレビュー回数 |
| self_review_agent | str\|null | — | null | パス2担当エージェント（nullならレビュアーリスト先頭）|
| current_rev | str | — | "1" | リビジョン（"1","2","2A"等）|
| rev_index | int | — | 1 | 順序管理用連番 |
| max_revise_cycles | int | — | 5 | 最大改訂サイクル数 |
| revise_count | int | — | 0 | 完了した改訂サイクル数 |
| last_commit | str\|null | — | null | 前revのcommit hash |
| model | str\|null | — | null | implementerモデル参考情報 |
| review_requests | dict | — | {} | per-reviewerタイムアウト管理（§5.1）|
| current_reviews | dict | — | {} | 進行中ラウンドのパース結果を永続化 |
| issue_suggestions | dict | — | {} | Issue分割提案 |
| created_issues | list[int] | — | [] | 起票済みIssue番号 |
| review_history | list | — | [] | ラウンド結果サマリー |
| force_events | list | — | [] | approve --force監査ログ |
| retry_counts | dict | — | {} | 状態別リトライ回数 |
| paused_from | str\|null | — | null | PAUSED復帰先 |
| pipelines_dir | str\|null | — | null | レビュー原文保存先の絶対パス |
| last_changes | dict\|null | — | null | <!-- [v6] Pascal P-1 --> 前回改訂のchangesオブジェクト（プロンプト生成用）|

**current_reviews の構造:**
<!-- [v7] Leibniz M-1: メタ/本体分離 -->
```json
"current_reviews": {
  "reviewed_rev": "2",
  "entries": {
    "pascal": {
      "verdict": "P0",
      "items": [...],
      "raw_text": "...",
      "parse_success": true,
      "status": "received"
    },
    "dijkstra": {
      "verdict": null,
      "items": [],
      "raw_text": null,
      "parse_success": false,
      "status": "timeout"
    }
  }
}
```

`reviewed_rev`: このレビュー集合が対象としたリビジョン。extend→REVISE直行時もこのrevに紐づくレビューとして扱う。review_historyへのアーカイブ時にrev番号として使用。

**per-reviewer `status` の遷移規則:**
<!-- [v7] Leibniz M-3 -->
```
pending → received    （応答あり + パース成功）
pending → timeout     （SPEC_REVIEW_TIMEOUT_SEC超過）
pending → parse_failed（応答あり + パース失敗）
```

**received時の必須フィールド（不変条件）:**
- `verdict` ∈ {"APPROVE", "P0", "P1"}（必須、null不可）
- `items`: list[SpecReviewItem]（空リスト可）
- `parse_success` = true

上記不変条件に違反する場合は `status='parse_failed'` にフォールバックする。

PAUSED/再起動からの復帰時にデータを喪失しない。ラウンド完了時にreview_historyへ移動しcurrent_reviewsをクリア。

**retry_counts の構造と規則:**
```json
"retry_counts": {
  "SPEC_REVISE": 1,
  "ISSUE_PLAN": 0
}
```
**インクリメント条件（+1するとき）:**
- SPEC_REVISE: implementerからの応答タイムアウト
- ISSUE_PLAN: implementerからの応答タイムアウト
- QUEUE_PLAN: implementerからの応答タイムアウト

**インクリメントしないとき:**
- SPEC_REVIEW: 個別レビュアーのタイムアウト（per-reviewer管理のため）
- ISSUE_SUGGESTION: 個別レビュアーのタイムアウト

状態遷移時に遷移先のretry_countsエントリをリセット（0に戻す）。MAX_SPEC_RETRIES超過で当該状態からSPEC_PAUSEDに遷移。

### 3.2 config.py 追加定数

```python
MAX_SPEC_REVISE_CYCLES = 5
MIN_VALID_REVIEWS_BY_MODE = {"full": 2, "standard": 2, "lite": 1, "min": 1}
SPEC_REVIEW_TIMEOUT_SEC = 1800
SPEC_REVISE_TIMEOUT_SEC = 1800
SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC = 600
SPEC_REVISE_SELF_REVIEW_PASSES = 2
MAX_SPEC_RETRIES = 3
# [v4] Leibniz M-2
SPEC_REVIEW_RAW_RETENTION_DAYS = 30
```

### 3.3 CLI→pipeline 写像表

| CLIフラグ | 保存先 | 型 |
|---|---|---|
| --pj | project | str |
| --spec | spec_config.spec_path | str |
| --implementer | spec_config.spec_implementer | str |
| --review-only | spec_config.review_only | bool |
| --no-queue | spec_config.no_queue | bool |
| --skip-review | spec_config.skip_review | bool |
| --max-cycles | spec_config.max_revise_cycles | int |
| --review-mode | review_mode | str |
| --model | spec_config.model | str\|null |
| --auto-continue | spec_config.auto_continue | bool |

※ §2.6の優先順位ルールが写像後に適用される。

---

## 4. CLIインターフェース

### 4.1 コマンド体系

```
devbar spec start      パイプライン開始
devbar spec approve    SPEC_APPROVEDに遷移 [--force]
devbar spec continue   APPROVED → ISSUE_SUGGESTION
devbar spec done       DONE → IDLE
devbar spec retry      FAILED → REVIEW
devbar spec resume     PAUSED → paused_from
devbar spec extend     STALLED → REVISE (MAX増加、既存指摘で改訂)
devbar spec status     ステータス表示
```

### 4.2 devbar spec start

```
devbar spec start --pj PROJECT --spec SPEC_PATH --implementer AGENT_ID
                  [--review-only] [--no-queue] [--skip-review]
                  [--max-cycles N] [--review-mode MODE] [--model MODEL]
                  [--auto-continue]
```

**前提条件:** IDLE状態、specファイル存在、implementer利用可能、`--skip-review --review-only`はエラー

**動作:**
1. pipeline.json を flock で排他ロック
2. §2.6の優先順位ルール適用
3. spec_mode=true + spec_config書き込み
4. pipelines_dir を絶対パスで記録（`PIPELINES_DIR/{project}/spec-reviews/`）
5. review_requestsにレビュアーリスト初期化（全員pending）
6. enabled=true
7. --skip-review → SPEC_APPROVED、そうでなければ → SPEC_REVIEW

### 4.3 devbar spec approve

```
devbar spec approve --pj PROJECT [--force]
```

- --forceなし: P1以上あればエラー
- --forceあり: 強制承認。以下を実行:
  1. current_reviewsから§12.2形式の要約を生成しreview_historyに追加、current_reviewsをクリア
  2. force_eventsに記録
  3. Discord監査通知

```json
{
  "at": "2026-02-28T23:00:00+09:00",
  "actor": "M",
  "from_state": "SPEC_STALLED",
  "rev": "3",
  "rev_index": 3,
  "remaining_p1_items": ["pascal:M-2", "leibniz:C-4"]
}
```

### 4.4 devbar spec status

```
DevBar [SPEC_REVIEW] rev2 (cycle 1/5, retries: REVISE=0/3)
  spec: docs/spec-mode-spec.md
  implementer: second
  reviewers: pascal(✅ P0×1), leibniz(⏳), dijkstra(⏳)
  min_valid: 2 (full mode)
  pipelines_dir: /home/ataka/.openclaw/pipelines/DevBar/spec-reviews/
```

### 4.5 devbar spec retry

```
devbar spec retry --pj PROJECT
```

**前提条件:** SPEC_REVIEW_FAILED状態のみ
**動作:**
1. _reset_review_requests()（§5.4）
2. current_reviewsをクリア
3. SPEC_REVIEWに遷移（watchdogが再送信）

### 4.6 devbar spec resume

```
devbar spec resume --pj PROJECT
```

**前提条件:** SPEC_PAUSED状態のみ
**動作:**
1. paused_fromを読み取り。nullならエラー
2. paused_fromへの遷移のみ許可（他の状態への遷移は不可）
3. paused_fromがSPEC_REVIEWの場合: _reset_review_requests()（§5.4）+ current_reviewsクリア
4. review_requests内の全pending entryのtimeout_atを現在時刻ベースで再計算
5. paused_fromがISSUE_SUGGESTIONの場合: issue_suggestions内のペンディングエントリのtimeout_atも再計算
6. retry_counts[paused_from]をリセット（0）
7. paused_fromに遷移、paused_fromをnullにクリア

### 4.7 devbar spec extend

```
devbar spec extend --pj PROJECT [--cycles N]
```

**前提条件:** SPEC_STALLED状態のみ
**動作:**
1. max_revise_cycles += N（デフォルト N=2）
2. revise_countはリセット**しない**
3. <!-- [v5] Pascal P-1 --> current_reviewsは**クリアしない**（既存の指摘を維持）
4. → **SPEC_REVISE**（既存指摘で直ちに改訂。空転レビューラウンドを回避）

---

## 5. SPEC_REVIEWフェーズ

### 5.1 レビュー依頼の送信

**watchdog.py process()への統合:**
```python
# spec_modeのときはbatch空を許容
if state != "DONE" and not batch and not pipeline.get("spec_mode"):
    logger.warning("batch empty, skipping")
    return

if pipeline.get("spec_mode") and state in SPEC_STATES:
    spec_config = pipeline.get("spec_config", {})
    action = check_transition_spec(state, spec_config, now)
    # [v5] Dijkstra M-1 + [v6] Leibniz C-2: 副作用フィールドが1つでもあれば適用
    if action.next_state or action.pipeline_updates or action.send_to or action.discord_notify:
        action.expected_state = state
        _apply_spec_action(pipeline_path, action, now)
    return
```

各レビュアーに **`send_to_agent()`**（改行保持）でレビュー依頼を送信。spec本文は**埋め込まない**。

<!-- [v5] Leibniz C-2: 送信時の事後条件 -->
**送信関数の事後条件:** レビュー依頼の送信後、対象reviewerの `review_requests[reviewer]` は以下を満たすこと:
- `sent_at != None`（送信時刻）
- `timeout_at != None`（`sent_at + SPEC_REVIEW_TIMEOUT_SEC`）
- `status == "pending"`

テストで全reviewer分を検査する。この保証が欠けると、pendingが永遠にtimeoutしない（または即死する）致命的バグになる。

**初回プロンプト:**

```
以下の仕様書をレビューしてください。**やりすぎレビュー**を依頼します。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev}, {line_count}行)

## レビュー指示
- 重篤度を必ず付与: 🔴 Critical (P0) / 🟠 Major (P1) / 🟡 Minor / 💡 Suggestion
- セクション番号を明記（例: §6.2）
- 擬似コード間の整合性に特に注意
- 既存devbarコードベースとの整合性も確認
- ステートマシン遷移の抜け穴・デッドロックを探せ
- YAMLブロックは応答内で**1つだけ**

## 出力フォーマット
```yaml
verdict: APPROVE | P0 | P1
items:
  - id: C-1
    severity: critical | major | minor | suggestion
    section: "§6.2"
    title: "タイトル"
    description: "説明"
    suggestion: "修正案"
```

## レビュー結果の保存
`{pipelines_dir}/{YYYYMMDD}T{HHMMSS}_{reviewer}_{spec_name}_rev{current_rev}.md`
```

**rev2以降のプロンプト:**

diff: `git diff --numstat {last_commit}..HEAD -- {spec_path}`。changelog: 実装者YAML報告を一次ソース。

```
以下の仕様書の改訂版をレビューしてください。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})
前回からの変更: +{added_lines}行, -{removed_lines}行
前回commit: {last_commit}

## 前回レビューからの変更点
{changelog_summary}

## レビュー指示
- 前回の指摘が適切に反映されているか確認
- 新たに追加された部分に問題がないか確認
- 重篤度・セクション番号・YAMLフォーマットは前回と同様
- YAMLブロックは応答内で**1つだけ**

## レビュー結果の保存
`{pipelines_dir}/{YYYYMMDD}T{HHMMSS}_{reviewer}_{spec_name}_rev{current_rev}.md`
```

### 5.2 レビュー回収

```json
"review_requests": {
  "pascal": {
    "sent_at": "2026-02-28T21:15:00+09:00",
    "timeout_at": "2026-02-28T21:45:00+09:00",
    "last_nudge_at": null,
    "status": "pending | received | timeout",
    "response": null
  }
}
```

**回収完了条件:** 全reviewer status = received|timeout → should_continue_review()（§5.3）を呼ぶ。

**timeoutの扱い:** タイムアウトしたreviewerも `current_reviews.entries` に `status='timeout'` で格納する（構造例は§3.1参照）。should_continue_review() では `status='timeout'` のエントリは received にも parsed_fail にも含まれない。全員タイムアウト → received=0, parsed_fail=0 → "failed"。

### 5.3 終了判定（正規ソース）

<!-- [v4] Leibniz M-1: ここが唯一の判定ロジック。§2.4は要約 -->

```python
def should_continue_review(
    spec_config: dict,
    review_mode: str,
) -> str:  # "revise"|"approved"|"stalled"|"failed"|"paused"
    """SPEC_REVIEW完了後に呼ばれる唯一の判定関数。
    データソースは spec_config["current_reviews"]。"""

    cr = spec_config.get("current_reviews", {})
    # [v7] Leibniz M-1: entries配下にreviewer辞書を格納
    reviewer_entries = cr.get("entries", {})
    received = {k: v for k, v in reviewer_entries.items() if v["status"] == "received"}
    parsed_fail = {k: v for k, v in reviewer_entries.items() if v["status"] == "parse_failed"}
    timed_out = {k: v for k, v in reviewer_entries.items() if v["status"] == "timeout"}
    parsed_ok = received  # received = パース成功済み

    # [v4] Pascal M-2: paused判定の厳密化
    min_valid = MIN_VALID_REVIEWS_BY_MODE.get(review_mode, 2)

    # 1. 誰も応答しなかった（全員タイムアウト）
    if len(received) == 0 and len(parsed_fail) == 0:
        return "failed"

    # 2. 有効レビュー（received）が閾値未満
    if len(parsed_ok) < min_valid:
        if len(parsed_fail) > 0:
            return "paused"   # パース失敗あり → 人間の介入必要
        return "failed"       # タイムアウトのみ → 再送で回復可能

    # 3. 有効レビューで判定
    has_p1 = any(v.get("verdict") in ("P0", "P1") for v in parsed_ok.values())
    if not has_p1:
        return "approved"
    if spec_config["revise_count"] >= spec_config["max_revise_cycles"]:
        return "stalled"
    return "revise"
```

**注意:** この関数は SPEC_REVIEW 完了後にのみ呼ばれる。SPEC_REVISE完了後は常にSPEC_REVIEWに戻る（§6.3）ため、REVISE側での判定は不要。

### 5.4 review_requestsリセット（共通ヘルパー）

<!-- [v4] Pascal C-2 / Dijkstra m-2: 全パスで統一 -->

```python
def _reset_review_requests(spec_config: dict, now: datetime) -> None:
    """SPEC_REVIEWへ遷移する全パスで呼ばれる。"""
    for reviewer, entry in spec_config["review_requests"].items():
        entry["status"] = "pending"
        entry["sent_at"] = None
        entry["timeout_at"] = None
        entry["last_nudge_at"] = None
        entry["response"] = None
```

**呼び出し箇所:**
- `devbar spec start`（初期化時）
- `devbar spec retry`（FAILED→REVIEW）
- `devbar spec resume`（paused_from=REVIEW時）
- SPEC_REVISE完了後のREVIEW戻り（§6.3）

<!-- [v5] extendはSTALLED→REVISEなのでリセット不要（既存current_reviews維持） -->

### 5.5 レビュー結果のパース

**決定性最優先。**

1. YAMLブロック正規表現抽出（最初の1ブロックのみ）
2. verdict/severityにエイリアスマッピング適用

```python
VERDICT_ALIASES = {
    "approve": "APPROVE", "APPROVE": "APPROVE",
    "p0": "P0", "P0": "P0",
    "reject": "P0", "REJECT": "P0",
    "p1": "P1", "P1": "P1",
}
SEVERITY_ALIASES = {
    "critical": "critical", "Critical": "critical",
    "major": "major", "Major": "major",
    "minor": "minor", "Minor": "minor",
    "suggestion": "suggestion", "Suggestion": "suggestion",
}
```

3. **不正値（マッピング外の値）→ parse_success=False, status='parse_failed'**。raw_textを保持。current_reviewsへの格納時に必ずstatusを設定すること（§3.1 status遷移規則参照）

```python
@dataclass
class SpecReviewItem:
    id: str                    # "C-1" (reviewer-local)
    severity: str              # "critical"|"major"|"minor"|"suggestion"
    section: str
    title: str
    description: str
    suggestion: str | None
    reviewer: str
    normalized_id: str         # "pascal:C-1"

@dataclass
class SpecReviewResult:
    reviewer: str
    verdict: str               # "APPROVE"|"P0"|"P1"
    items: list[SpecReviewItem]
    raw_text: str
    parse_success: bool

@dataclass
class MergedReviewReport:
    reviews: list[SpecReviewResult]
    all_items: list[SpecReviewItem]
    summary: dict              # {"critical": n, ...}
    highest_verdict: str
```

### 5.6 重複検出・統合

**初期実装:** 重複検出アルゴリズムは実装しない。統合レポートに全指摘を重篤度順で列挙し、重複判断はspec_implementer（Opus）に委ねる。将来的にembedding類似度ベースの候補提示を検討。

統合レポートフォーマット:
```markdown
# Rev{N} レビュー統合レポート
## サマリー
- レビュアー: {reviewer} ({verdict}), ...
- Critical: {n}件, Major: {n}件, Minor: {n}件, Suggestion: {n}件
## 全指摘一覧（重篤度順）
### Critical — {normalized_id}: {title} ({section})
### Major — ...
```

---

## 6. SPEC_REVISEフェーズ

### 6.1 改訂プロセス

`send_to_agent()` で改訂依頼。TrajOpt/EMCalibrator形式のchangelogを要求:
- 変更履歴テーブルに1行追加
- `[vN] 指摘元ID: 説明` 形式で全件列挙
- 擬似コード中 `# [vN] Pascal C-1: 説明` で変更理由記載

改訂完了報告YAML:
```yaml
status: done
new_rev: "3"
commit: "abc1234"
changes:
  added_lines: 350
  removed_lines: 50
  reflected_items: ["pascal:C-1", "leibniz:C-1"]
  deferred_items: ["dijkstra:m-4"]
  deferred_reasons:
    "dijkstra:m-4": "理由"
```

### 6.2 セルフレビュー

**パス1（implementer自身）:** 反映漏れ、矛盾、整合性、changelog確認

**パス2（別エージェント）:**
- **選択ロジック:** `spec_config.self_review_agent` が設定されていればそのエージェント。nullならreview_requestsのキー一覧の先頭
- **依頼プロンプト:**
```
改訂された仕様書のクロスチェックを依頼します。

仕様書: {spec_path} (rev{new_rev})
前回commit: {last_commit}

## チェック項目
1. 変更履歴のreflected_itemsが本文に実際に反映されているか
2. 新たな矛盾やregressionが発生していないか
3. 擬似コードの型・引数整合性

変更箇所に問題がなければ `status: clean`、修正が必要なら `status: issues_found` + 指摘リストをYAMLで。
```
- **タイムアウト:** SPEC_REVIEW_TIMEOUT_SEC（1800s）
- **issues_found時:** implementerに再修正依頼 → commit → パス2再実行（最大1回リトライ）
- <!-- [v4] Pascal M-1 / Dijkstra M-2 --> **リトライ超過後もissues_found:** → SPEC_PAUSED（paused_from="SPEC_REVISE"）、Discord通知
- <!-- [v7] Pascal P-2 --> **SPEC_REVISEへのresume時:** §6.1の改訂要求プロンプト送信からプロセスを再始動する（セルフレビューだけの再実行ではない）

各パスは `status: clean | issues_found` で報告。

### 6.3 改訂完了の検知

<!-- [v4] REVISE → 常にREVIEW -->

1. YAML `status: done` 確認
2. セルフレビュー パス1 + パス2（§6.2。パス2リトライ超過時は→PAUSED）
3. last_commit, current_rev, rev_index 更新。implementerのchangesオブジェクトを`last_changes`に保存
4. `added_lines`/`removed_lines`を`git diff --numstat {last_commit}..HEAD -- {spec_path}`で検証。last_changesの値と不一致の場合はDiscord警告（処理は継続）。プロンプトではgit diff numstatを一次ソース、last_changesのchangelog_summaryは補助情報
5. **revise_count += 1**
6. current_reviews から §12.2 形式の要約を生成し review_history に追加。current_reviews をクリア
7. _reset_review_requests()（§5.4）
8. → **SPEC_REVIEW**（常にレビューに戻る）

**既存CODE_REVISEとの差異:** 既存はP0のみreviseトリガ。spec modeは**P1以上でループ継続**。

---

## 7. ISSUE_SUGGESTIONフェーズ

M が `devbar spec continue` 実行後（またはauto_continue時に自動で）遷移。

**送信プロンプト（send_to_agent）:**
```
以下の仕様書が承認されました。実装に向けてIssue分割を提案してください。

仕様書: {spec_path} (rev{final_rev})
プロジェクト: {project}

## 提案の指針
- CC（Claude Code）が 1 Issue = 1 MR で実装できる粒度（1〜3ファイル / 100〜500行）
- 依存関係を明示（DAG）
- Phase分割（並行着手可能なグループ）
- 各Issueのタイトル、変更ファイル、概算行数、仕様参照セクション

## 出力フォーマット
```yaml
phases:
  - name: "Phase 1: 基盤"
    issues:
      - title: "config.py: spec mode基盤"
        files: ["config.py", "pipeline_io.py"]
        lines: 110
        spec_refs: ["§3.1", "§3.2"]
        depends_on: []
```
```

回収は `spec_config.issue_suggestions` に格納。per-reviewerタイムアウト（SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC）。

---

## 8. ISSUE_PLANフェーズ

### 8.1 Issue統合と起票

spec_implementerに`send_to_agent()`で依頼:
```
レビュアーからのIssue分割提案を統合し、GitLab Issueを起票してください。

プロジェクト: {project} (GitLab)
仕様書: {spec_path} (rev{final_rev})

## レビュアー提案
{issue_suggestions_formatted}

## 起票ルール
1. 提案を統合し、最終的なIssue一覧を決定
2. 各Issueタイトルに [spec:{spec_name}:S-{N}] プレフィックスを付与
3. 起票前に `glab issue list --search "[spec:{spec_name}]"` で重複チェック
4. `glab issue create` で起票
5. 起票済み番号を報告（created_issues[]に逐次記録）
6. Issue本文末尾に⚠️注記を含めること

起票完了後、YAMLで報告:
```yaml
status: done
created_issues: [51, 52, 53]
```
```

起票済みIssue番号は逐次 `created_issues[]` に記録し、リトライ時はスキップ。

### 8.2 注記の存在検査

起票後に `glab issue show` で読み戻し、⚠️注記を検査。欠落時は `glab issue note` で自動追記。

---

## 9. QUEUE_PLANフェーズ

spec_implementerに`send_to_agent()`で依頼:
```
起票済みIssueからdevbar-queue.txtのバッチ行を生成してください。

プロジェクト: {project}
起票済みIssue: {created_issues}
仕様書: {spec_path}

## 生成ルール
1. Issue間の依存関係を分析
2. 並行実行可能なIssueは同一バッチに
3. フォーマット: `{project} {issue_nums} full [--keep-context] # 理由`
4. 生成した行を {queue_file_path} に追記

完了後YAMLで報告:
```yaml
status: done
batches: 5
queue_file: "devbar-queue.txt"
```
```

`config.QUEUE_FILE` に追記。完了後 → SPEC_DONE。M が `devbar spec done` で IDLE。

---

## 10. Watchdog統合

### 10.1 watchdog.py拡張

```python
@dataclass
class SpecTransitionAction:
    next_state: str | None
    expected_state: str | None = None   # DCL用: 現在のstate（競合検出に使用）
    send_to: dict[str, str] | None = None  # {agent_id: message}
    discord_notify: str | None = None   # Discord通知テキスト
    pipeline_updates: dict | None = None  # spec_configへの更新差分
    error: str | None = None

def check_transition_spec(
    state: str,
    spec_config: dict,
    now: datetime,
) -> SpecTransitionAction:
    """純粋関数。副作用なし。"""
    if state not in SPEC_STATES:
        return SpecTransitionAction(
            next_state="SPEC_PAUSED",
            error=f"Unknown spec state: {state}",
            discord_notify=f"[Spec] ⚠️ 未知状態 {state} → SPEC_PAUSED",
            pipeline_updates={"paused_from": state},
        )

    if state == "SPEC_REVIEW":
        return _check_spec_review(spec_config, now)
    elif state == "SPEC_REVISE":
        return _check_spec_revise(spec_config, now)
    elif state == "SPEC_APPROVED":
        if spec_config.get("review_only"):
            return SpecTransitionAction(next_state="SPEC_DONE",
                discord_notify=f"[Spec] spec承認完了（--review-only）")
        if spec_config.get("auto_continue"):
            return SpecTransitionAction(next_state="ISSUE_SUGGESTION",
                discord_notify=f"[Spec] spec承認 → Issue分割へ自動進行")
        # デフォルト: M確認待ち。通知は遷移元（_check_spec_review approved分岐）で発火済み
        return SpecTransitionAction(next_state=None)
    elif state == "ISSUE_SUGGESTION":
        return _check_issue_suggestion(spec_config, now)
    elif state == "ISSUE_PLAN":
        return _check_issue_plan(spec_config, now)
    elif state == "QUEUE_PLAN":
        return _check_queue_plan(spec_config, now)
    elif state in ("SPEC_DONE", "SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED"):
        return SpecTransitionAction(next_state=None)  # M操作待ち
```

<!-- [v4] Dijkstra M-3: SPEC_APPROVED通知は遷移元で発火 -->
**通知の発火ルール:** 通知は状態遷移を実行するアクション内で返す。滞在中のwatchdog tickでは通知しない。例:
- _check_spec_review() が "approved" 判定 → `discord_notify="[Spec] spec承認 (rev{N})"` を含むSpecTransitionActionを返す
- SPEC_APPROVED滞在中のcheck_transition_spec → `discord_notify=None`

<!-- [v4] Leibniz C-2 / Dijkstra C-1: update_pipeline パターン -->
**process()内の統合（DCLパターン）:**
```python
def _apply_spec_action(pipeline_path: str, action: SpecTransitionAction, now: datetime):
    """既存update_pipeline()パターンを使用。ディスクから再読み込み+state一致確認。"""
    applied = False
    applied_action = None

    def _update(data):
        nonlocal applied, applied_action
        # [v5] expected_state一致のみで判定。再計算結果を常に信頼
        if data["state"] != action.expected_state:
            return  # 競合: 別プロセスが先に遷移済み
        sc = data.get("spec_config", {})
        action2 = check_transition_spec(data["state"], sc, now)
        # 状態遷移（next_stateがある場合）
        if action2.next_state:
            data["state"] = action2.next_state
        # pipeline_updatesは常に適用（next_state=Noneでも）
        if action2.pipeline_updates:
            data["spec_config"].update(action2.pipeline_updates)
        if action2.next_state or action2.pipeline_updates or action2.send_to or action2.discord_notify:
            applied = True
            applied_action = action2

    update_pipeline(pipeline_path, _update)

    # 副作用は適用された場合のみ（action2の結果を使用）
    if applied and applied_action:
        if applied_action.send_to:
            for agent_id, msg in applied_action.send_to.items():
                send_to_agent(agent_id, msg)
        if applied_action.discord_notify:
            notify_discord(applied_action.discord_notify)
```

### 10.2 タイムアウトと催促

| 状態 | タイムアウト | タイムアウト後 | MAX_RETRIES超過 |
|---|---|---|---|
| SPEC_REVIEW | 1800s/reviewer | 応答済みのみで判定 | N/A（per-reviewer） |
| SPEC_REVISE | 1800s | retry_counts[REVISE]++ & 再送 | PAUSED |
| ISSUE_SUGGESTION | 600s/reviewer | 応答済みのみで遷移 | N/A（per-reviewer） |
| ISSUE_PLAN | 1800s | retry_counts[PLAN]++ & 再送 | PAUSED |
| QUEUE_PLAN | 1800s | retry_counts[QUEUE]++ & 再送 | PAUSED |

---

## 11. notify.py拡張

箇条書きベース。2000字超過時は分割。

**状態遷移時の通知（遷移アクション内で1回だけ送信）:**
- → SPEC_REVIEW: `[Spec] {project}: rev{N} レビュー開始 ({reviewer_count}人)`
- → SPEC_REVISE: `[Spec] {project}: rev{N} レビュー完了 — C:{n} M:{n} m:{n} s:{n}`
- → SPEC_APPROVED: `[Spec] {project}: spec承認 (rev{N})。\`devbar spec continue\` でIssue分割へ`
- → SPEC_APPROVED (forced): `[Spec] ⚠️ {project}: 強制承認 (P1以上 {n}件残存)`
- → SPEC_STALLED: `[Spec] ⏸️ {project}: MAX_CYCLES到達、P1以上 {n}件残存`
- → SPEC_REVIEW_FAILED: `[Spec] ❌ {project}: 有効レビュー不足`
- → SPEC_PAUSED: `[Spec] ⏸️ {project}: パイプライン停止 — {reason}`
- → ISSUE_PLAN完了: `[Spec] {project}: {n}件 Issue起票完了`
- → QUEUE_PLAN完了: `[Spec] {project}: {n}バッチ キュー生成完了`
- → SPEC_DONE: `[Spec] ✅ {project}: spec mode完了`

**REVISE完了通知:**
- commit hashあり: `[Spec] {project}: rev{N} 改訂完了 ({commit})`
- git commit失敗（commit空）: `[Spec] ⚠️ {project}: rev{N} git commit失敗` → SPEC_PAUSED
- 変更なし（差分0）: `[Spec] ⚠️ {project}: rev{N} 変更なし（改訂が空）` → SPEC_PAUSED

**失敗系:** YAMLパース失敗、送信失敗、git push失敗、glab起票失敗

---

## 12. レビュー結果の保存

### 12.1 ファイル保存

<!-- [v4] Leibniz M-2: pipelines_dir仕様化 -->

**レビュー原文（pipelines_dir）:**
- パス: `PIPELINES_DIR/{project}/spec-reviews/`（pipeline.jsonに絶対パスで記録）
- 保持期間: 30日（`SPEC_REVIEW_RAW_RETENTION_DAYS`）。SPEC_DONE遷移時にwatchdogが期限切れファイルを削除
- <!-- [v6] Leibniz C-1 --> 権限: ディレクトリ=0700（owner rwx）、ファイル=0600（owner rw）。watchdogがディレクトリ作成時にchmod設定
- ファイル名: `{YYYYMMDD}T{HHMMSS}_{reviewer}_{spec_name}_rev{N}.md`

**レビューサマリー（repo内）:**
- パス: `reviews/` ディレクトリ
- ファイル名: `{YYYYMMDD}T{HHMMSS}_merged_{spec_name}_rev{N}.md`
- コミット主体: devbar（watchdog経由）
- メッセージ: `[spec-review] {project}: rev{N} reviews ({reviewer_count} reviewers)`
- タイミング: SPEC_REVISEに遷移する直前

### 12.2 review_history

```json
{
  "rev": "1", "rev_index": 1,
  "reviews": {"pascal": {"verdict": "P0", "counts": {...}}, ...},
  "merged_counts": {"critical": 18, "major": 14, "minor": 14, "suggestion": 8},
  "commit": "82ec516",
  "timestamp": "2026-02-28T21:15:00+09:00"
}
```

---

## 13. 実装計画

### 13.1 変更ファイル

| ファイル | 内容 | 規模 |
|---|---|---|
| devbar.py | spec CLI（8コマンド）| +280行 |
| watchdog.py | check_transition_spec + _apply_spec_action + 各状態 | +350行 |
| notify.py | 通知（遷移時+失敗系）| +120行 |
| config.py | 定数 + SPEC_STATES + TRANSITIONS（sorted merge）| +60行 |
| pipeline_io.py | 初期化・バリデーション・flock・pipelines_dir | +80行 |
| spec_review.py | **新規** パース・エイリアス・統合レポート・_reset_review_requests | +320行 |
| spec_revise.py | **新規** 改訂依頼・セルフレビュー | +280行 |
| spec_issue.py | **新規** Issue分割・起票・注記検査・キュー | +300行 |
| tests/ | テスト | +600行 |
| **合計** | | **~2,390行** |

### 13.2 Issue分割案

| Issue | タイトル | 依存 | 行数 |
|---|---|---|---|
| S-1 | config.py + pipeline_io.py: spec mode基盤（sorted merge + pipelines_dir）| なし | +140 |
| S-2 | devbar.py: spec CLI（8コマンド + CLIオプション優先順位）| S-1 | +280 |
| S-3 | spec_review.py: パース+エイリアス+統合レポート+_reset_review_requests+should_continue_review | S-1 | +320 |
| S-4 | watchdog.py: _apply_spec_action + REVIEW/REVISE判定 | S-2, S-3 | +250 |
| S-5 | spec_revise.py: 改訂+セルフレビュー（パス2リトライ+PAUSED） | S-3 | +280 |
| S-6 | spec_issue.py: SUGGESTION+PLAN+注記+QUEUE | S-1, S-3 | +300 |
| S-7 | notify.py: 遷移時通知+失敗系 | S-4 | +120 |
| S-8 | watchdog.py: ISSUE系+異常系+全状態カバー | S-6, S-7 | +150 |
| S-9 | 統合テスト | S-8 | +600 |

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

- devbar実装フローとの接続（SPEC_DONE → qrun自動開始）
- spec叩き台の自動生成
- 差分レビュー（トークン節約）
- embedding類似度による重複候補提示
- SPEC_PAUSED自動リカバリ（一過性エラー）

---

## 附録A: 過去の変更履歴

<details>
<summary>v1→v2 の全変更点（42件）— クリックで展開</summary>

- [v2] 空レビュー集合の自動承認バグ修正（Pascal C-1）
- [v2] MAX_CYCLES到達時の強制承認廃止（Pascal C-2）
- [v2] タイムアウト時の無限スタック防止（Pascal C-3 / Leibniz C-10）
- [v2] verdict/severity語彙の統一（Pascal C-4 / Leibniz s-1）
- [v2] --skip-review時のレビュアーリスト初期化（Pascal C-5 / Dijkstra M-6）
- [v2] レビューファイル名の上書き防止（Pascal C-6 / Dijkstra m-4）
- [v2] batch機構との分離（Leibniz C-1 / Dijkstra C-2）
- [v2] 送信インターフェースの固定（Leibniz C-2）
- [v2] VALID_STATES/VALID_TRANSITIONSへのSPEC_*登録（Leibniz C-3 / Dijkstra C-3）
- [v2] should_continue_review擬似コード修正（Leibniz C-4）
- [v2] spec_config JSON例とフィールド表の統一（Leibniz C-5 / Dijkstra C-1）
- [v2] 状態集合の整理（Leibniz C-6）
- [v2] QUEUE_FILE二重定義の解消（Leibniz C-7 / Dijkstra m-2）
- [v2] YAMLパースの決定性確保（Leibniz C-8 / Dijkstra C-5）
- [v2] 重複統合の安全化（Leibniz C-9）
- [v2] per-reviewerタイムアウト用データ構造（Leibniz C-10 / Dijkstra M-3）
- [v2] レビュー保存先の規約（Leibniz C-11）
- [v2] rev命名規則の統一（Leibniz M-1 / Dijkstra s-3）
- [v2] CLI→pipeline写像表の追加（Leibniz M-2 / Dijkstra m-8）
- [v2] diff情報の生成方法定義（Leibniz M-3）
- [v2] レビューitem IDの正規化（Leibniz M-4）
- [v2] 手動approveの監査ログ（Leibniz M-5）
- [v2] check_spec_modeを純粋関数化（Leibniz M-6 / Dijkstra C-3）
- [v2] REJECT verdictの扱い（Dijkstra M-1）
- [v2] YAMLコードブロックのネスト対策（Dijkstra M-2）
- [v2] ISSUE_SUGGESTIONデータフロー定義（Dijkstra M-4）
- [v2] SPEC_DONE→IDLE遷移コマンド（Dijkstra M-5）
- [v2] GitLab Issue起票の部分失敗リカバリ（Dijkstra M-7）
- [v2] セルフレビュー改善（Dijkstra M-8）
- [v2] §6.3セクション番号重複修正（Leibniz m-1 / Dijkstra C-4）
- [v2] Discord通知の文字数制限対応（Leibniz m-2）
- [v2] spec全文埋め込み廃止（Leibniz m-3 / Dijkstra m-6 / M指示）
- [v2] Issue注記の存在検査（Leibniz m-4）
- [v2] M確認ゲート追加（Dijkstra m-5）
- [v2] 失敗系通知の追加（Leibniz s-2）
- [v2] DAG修正: S-6依存先（Dijkstra m-7）
- [v2] §1.2スコープ修正（Dijkstra s-1）
- [v2] MergedReviewReport型定義追加（Dijkstra s-2）
- [v2] S-4行数見積もり修正（Dijkstra s-4）
- [v2] 早期終了オプション真理値表追加（Dijkstra s-5）
- [v2] エラーハンドリング方針（Dijkstra s-6）
- [v2] 重複検出アルゴリズム簡素化（Dijkstra m-3 / M指示）
- [v2] REVISE完了通知のcommit空対応（Dijkstra m-9）

</details>

<details>
<summary>v5→v6 の全変更点（7件）— クリックで展開</summary>

- [v6] pipelines_dir権限修正（Leibniz C-1）
- [v6] ガード条件にdiscord_notify追加（Leibniz C-2）
- [v6] last_changesフィールド追加（Pascal P-1）
- [v6] ISSUE_SUGGESTION resume時タイムアウト再計算（Pascal P-2）
- [v6] current_reviewsにreviewed_rev追加（Leibniz M-1）
- [v6] current_reviewsにstatusフィールド追加（Leibniz m-1）
- [v6] §4.1 extend説明修正（Dijkstra s-2）

</details>

<details>
<summary>v4→v5 の全変更点（8件）— クリックで展開</summary>

- [v5] DCL適用条件の緩和（Leibniz C-1 / Pascal P-3 / Dijkstra m-2）
- [v5] next_state=Noneアクションの適用（Dijkstra M-1）
- [v5] extend→SPEC_REVISE直行（Pascal P-1）
- [v5] timeout_at再設定の責務明示（Leibniz C-2）
- [v5] current_reviewsにtimeoutエントリ追加（Leibniz M-1）
- [v5] approve --force時のcurrent_reviewsアーカイブ（Pascal P-2）
- [v5] pipelines_dirパス表記統一（Leibniz m-1）
- [v5] 期限切れファイル削除の実行主体明記（Dijkstra m-1）

</details>

<details>
<summary>v3→v4 の全変更点（15件）— クリックで展開</summary>

- [v4] SPEC_REVISEフロー単純化（Leibniz C-1 / Pascal C-1 / Dijkstra C-2）
- [v4] DCL再読み込み（Leibniz C-2 / Dijkstra C-1）
- [v4] マージ順序の決定性（Leibniz C-3）
- [v4] extend/resume時のreview_requests初期化（Pascal C-2 / Dijkstra m-2）
- [v4] セルフレビューリトライ上限後PAUSED（Pascal M-1 / Dijkstra M-2）
- [v4] paused判定の厳密化（Pascal M-2）
- [v4] 判定ロジック単一ソース化（Leibniz M-1）
- [v4] pipelines_dir仕様化（Leibniz M-2）
- [v4] CLIオプション優先順位表（Leibniz m-1）
- [v4] SPEC_APPROVED通知の発火元明示（Dijkstra M-3）
- [v4] §6.3ステップ統合（Dijkstra M-1）
- [v4] review_requestsリセット：REVIEWエントリ時（Dijkstra m-2）
- [v4] --review-only + --auto-continue → review-only優先（Dijkstra m-3）
- [v4] SpecTransitionAction.expected_stateフィールド追加（セルフチェック）
- [v4] _apply_spec_action競合時の通知誤送信防止（セルフチェック）

</details>

<details>
<summary>v2→v3 の全変更点（27件）— クリックで展開</summary>

- [v3] VALID_TRANSITIONS上書きバグ修正（Dijkstra C-1）
- [v3] batch空でwatchdog早期return問題（Leibniz C-1）
- [v3] check_transition_spec I/O整理（Pascal P-2 / Leibniz C-3 / Dijkstra C-2）
- [v3] 全員パース失敗時の遷移矛盾解消（Pascal P-1）
- [v3] P1ループ暴走防止（Leibniz C-4）
- [v3] STALLED→REVIEW時のrevise_count扱い（Leibniz C-5）
- [v3] retry_count粒度の厳密化（Leibniz C-6）
- [v3] resume時タイムアウト即死防止（Pascal P-3）
- [v3] Issue起票レースコンディション対策（Pascal P-4）
- [v3] PAUSED遷移先網羅性（Pascal P-5）
- [v3] revise_countインクリメント明記（Pascal P-6 / Dijkstra C-3）
- [v3] VERDICT_ALIASES厳格化（Leibniz M-1 / Dijkstra m-2）
- [v3] reviews/コミット規約（Leibniz M-2）
- [v3] approve --force監査ログ構造化（Leibniz M-3）
- [v3] retry/resumeコマンド詳細仕様（Dijkstra M-1）
- [v3] PAUSED復帰バリデーション（Dijkstra M-2）
- [v3] プロンプト内パス統一（Dijkstra M-3）
- [v3] セルフレビューパス2詳細（Dijkstra M-4）
- [v3] ISSUE系プロンプト復元（Dijkstra M-5）
- [v3] YAMLブロック1つ制約明記（Leibniz m-1）
- [v3] STALLED→REVIEW CLI定義（Dijkstra m-1）
- [v3] check_transition_spec全状態実装（Dijkstra m-3）
- [v3] commit空の区別（Dijkstra m-5）
- [v3] changelog分離（Dijkstra s-2）
- [v3] --no-issue → --review-only リネーム（M指示）
- [v3] --auto-continue フラグ追加（M指示）
- [v3] --skip-review時はauto_continue暗黙true（M指示）

</details>
