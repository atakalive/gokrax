# gokrax Spec Mode — 仕様書

**Version:** 3.0 (rev2 review reflected — Pascal/Leibniz/Dijkstra)
**Date:** 2026-02-28
**Author:** Asuka (second) + M
**Reviewers:** Pascal (Gemini 3 Pro), Leibniz (GPT-5.2), Dijkstra (Opus)

---

### 変更履歴

| Version | Date | 内容 |
|---|---|---|
| 1.0 | 2026-02-28 | 初版（821行）|
| 2.0 | 2026-02-28 | rev1レビュー反映（42件）|
| 3.0 | 2026-02-28 | rev2レビュー反映（Pascal 6件、Leibniz 11件、Dijkstra 15件 → 重複排除後24件）|

**v2→v3 の主要変更点:**

- **[v3] VALID_TRANSITIONS上書きバグ修正（Dijkstra C-1）**: `dict.update()` が既存IDLE→DESIGN_PLANを消す致命的バグ。マージ方式に変更
- **[v3] batch空でwatchdog早期return問題（Leibniz C-1）**: 現行watchdogの `not batch` ガードにspec_mode除外条件を追加。process()内のspec専用分岐を明記
- **[v3] check_transition_spec I/O整理（Pascal P-2 / Leibniz C-3 / Dijkstra C-2）**: review_data引数を廃止。全データはspec_config内。SpecTransitionActionのフィールドを既存TransitionActionに寄せる
- **[v3] 全員パース失敗時の遷移矛盾解消（Pascal P-1）**: §5.2で「全員パース失敗」をSPEC_PAUSEDに、§6.4のshould_continue_reviewには「全員タイムアウト/未応答」のみREVIEW_FAILEDに。判定順序を明確化
- **[v3] P1ループ暴走防止（Leibniz C-4）**: MIN_VALID_REVIEWS をreview_modeのmin_reviewsに追随（full=2, standard=2, lite=1）
- **[v3] STALLED→REVIEW時のrevise_count扱い（Leibniz C-5）**: `gokrax spec extend --pj X --cycles N` でmax_revise_cyclesを増加。revise_countはリセットしない
- **[v3] retry_count粒度の厳密化（Leibniz C-6）**: 状態ごとにretry_countを持つ（retry_counts: {state: int}）。インクリメント条件を箇条書きで列挙
- **[v3] resume時タイムアウト即死防止（Pascal P-3）**: resume時にtimeout_atを現在時刻ベースで再計算
- **[v3] Issue起票レースコンディション対策（Pascal P-4）**: 起票時タイトルに `[spec:{spec_name}:S-{N}]` プレフィックスを含め、起票前にglab issue listで重複チェック
- **[v3] PAUSED遷移先網羅性（Pascal P-5）**: 全SPEC_*状態をPAUSED遷移先に追加
- **[v3] revise_countインクリメント明記（Pascal P-6 / Dijkstra C-3）**: §6.3のステップに明記
- **[v3] VERDICT_ALIASES厳格化（Leibniz M-1 / Dijkstra m-2）**: verdict側からseverity語彙を除外。不正値はparse_success=False
- **[v3] reviews/コミット規約（Leibniz M-2）**: gokraxがcommit。コミットメッセージ規約固定。レビュー原文はrepo外（pipelines/）に保存し、repoにはサマリーのみ
- **[v3] approve --force監査ログ構造化（Leibniz M-3）**: force_eventを別フィールドで記録
- **[v3] retry/resumeコマンド詳細仕様（Dijkstra M-1）**: §4.5, §4.6追加
- **[v3] PAUSED復帰バリデーション（Dijkstra M-2）**: resume先をpaused_fromに限定
- **[v3] プロンプト内パス統一（Dijkstra M-3）**: §5.1と§12.1のファイル名フォーマットを完全一致
- **[v3] セルフレビューパス2詳細（Dijkstra M-4）**: レビュアー選択ロジック・プロンプト・タイムアウト定義
- **[v3] ISSUE系プロンプト復元（Dijkstra M-5）**: §7, §8, §9のプロンプトテンプレート復元
- **[v3] YAMLブロック1つ制約明記（Leibniz m-1）**: プロンプトに追記
- **[v3] STALLED→REVIEW CLI定義（Dijkstra m-1）**: `gokrax spec extend`コマンド
- **[v3] check_transition_spec全状態実装（Dijkstra m-3）**: 省略記号を展開
- **[v3] commit空の区別（Dijkstra m-5）**: git失敗 vs 変更なし を分離
- **[v3] changelog分離（Dijkstra s-2）**: v1→v2のchangelogをCHANGELOG節に移動、本文から除去
- **[v3] --no-issue → --review-only リネーム（M指示）**: 直感的な命名に変更。レビューサイクルのみ実行（Issue分割・キューをスキップ）
- **[v3] --auto-continue フラグ追加（M指示）**: SPEC_APPROVED後にM確認なしでISSUE_SUGGESTIONへ自動進行。デフォルトはM確認待ち（既存gokraxのマージサマリー→M確認パターンを踏襲）
- **[v3] --spec-only → --review-only リネーム（M指示）**: より直感的な命名に変更
- **[v3] SPEC_APPROVED通知の重複送信防止**: 通知は状態遷移時に1回だけ。滞在中のwatchdog tickでは通知しない
- **[v3] should_continue_review呼び出しタイミング整理**: 初回レビュー(revise_count=0)は無条件REVISE、再レビュー以降でshould_continue_review判定
- **[v3] --skip-review時はauto_continue暗黙true**: レビュー済specの後工程（Issue化+キュー）はM確認不要

---

v1→v2の変更履歴詳細は本文末尾の「附録A: v1→v2変更履歴」を参照。

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
9. gokrax-queue.txtにバッチ実行順を手動記述

### 1.2 目標

gokraxに**spec mode**を追加し、上記2〜9を自動化する。

### 1.3 スコープ

**スコープ内:** specレビューサイクル自動化、Issue分割半自動化、キュー生成自動化

**スコープ外:** spec叩き台の自動生成、gokrax実装フローとの直接接続、ブートストラップ

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
  │     │       (P1以上あり & revise_count < MAX)
  │     │
  │     │ (P1以上なし)
  │     ▼
  │   SPEC_APPROVED ──── [--review-only] ───→ SPEC_DONE
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
  └──→ SPEC_STALLED ─→ [spec extend] → SPEC_REVIEW (MAX増加)
                   └─→ [spec approve --force] → SPEC_APPROVED

  ※ 異常系:
  SPEC_REVIEW_FAILED ←── (有効レビュー 0 件、全員タイムアウト)
  SPEC_PAUSED ←── (MAX_RETRIES超過 / 全員パース失敗 / 未知状態)
```
<!-- [v3] Leibniz C-5: STALLED→REVIEW は spec extend 経由のみ -->

### 2.2 状態定義

| 状態 | 説明 | 出口 |
|---|---|---|
| `SPEC_REVIEW` | レビュアーにspec送信、回収待ち | 有効≥MIN → REVISE / 全員パース失敗 → PAUSED / 有効0(タイムアウト) → FAILED |
| `SPEC_REVISE` | 統合レポート生成、implementer改訂 | commit完了 → 終了判定 |
| `SPEC_APPROVED` | 改訂サイクル完了 | auto_continue → 自動でISSUE_SUGGESTION / デフォルト → M確認待ち（`spec continue`）/ --review-only → DONE |
| `ISSUE_SUGGESTION` | レビュアーにIssue分割案問い合わせ | 回収完了 → ISSUE_PLAN |
| `ISSUE_PLAN` | implementerが統合→GitLab起票 | 起票完了 → QUEUE_PLAN |
| `QUEUE_PLAN` | gokrax-queue.txt生成 | 生成完了 → DONE |
| `SPEC_DONE` | 全工程完了、M最終確認待ち | `spec done` → IDLE |
| `SPEC_STALLED` | MAX_CYCLES & P1残存、M介入必須 | extend → REVIEW / --force → APPROVED |
| `SPEC_REVIEW_FAILED` | 有効レビュー0件（全員タイムアウト/未応答）| `spec retry` → REVIEW |
| `SPEC_PAUSED` | リトライ超過/全員パース失敗/異常 | `spec resume` → paused_from |
| `IDLE` | 非稼働 | — |

<!-- [v3] Pascal P-1: REVIEW_FAILED=全員タイムアウト、PAUSED=全員パース失敗 を明確に分離 -->

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
    "SPEC_REVIEW":          ["SPEC_REVISE", "SPEC_REVIEW_FAILED", "SPEC_PAUSED"],
    "SPEC_REVISE":          ["SPEC_REVIEW", "SPEC_APPROVED", "SPEC_STALLED", "SPEC_PAUSED"],
    "SPEC_APPROVED":        ["ISSUE_SUGGESTION", "SPEC_DONE"],
    "ISSUE_SUGGESTION":     ["ISSUE_PLAN", "SPEC_PAUSED"],
    "ISSUE_PLAN":           ["QUEUE_PLAN", "SPEC_DONE", "SPEC_PAUSED"],
    "QUEUE_PLAN":           ["SPEC_DONE", "SPEC_PAUSED"],
    "SPEC_DONE":            ["IDLE"],
    "SPEC_STALLED":         ["SPEC_APPROVED", "SPEC_REVIEW"],
    "SPEC_REVIEW_FAILED":   ["SPEC_REVIEW"],
    # [v3] Pascal P-5: 全SPEC_*状態をPAUSED復帰先に
    "SPEC_PAUSED":          ["SPEC_REVIEW", "SPEC_REVISE", "SPEC_APPROVED",
                             "ISSUE_SUGGESTION", "ISSUE_PLAN", "QUEUE_PLAN",
                             "SPEC_DONE"],
}

# [v3] Dijkstra C-1: dict.update()はIDLEキーを上書きする。マージ方式に変更
for state, targets in SPEC_TRANSITIONS.items():
    existing = VALID_TRANSITIONS.get(state, [])
    VALID_TRANSITIONS[state] = list(set(existing + targets))

STATE_PHASE_MAP.update({s: "spec" for s in SPEC_STATES})
```

**排他制御:** `gokrax spec start` は pipeline.json を flock で排他ロック → `spec_mode = true` を atomic に設定。`spec_mode = true` の間、既存 `gokrax start` / `gokrax transition` はエラー。IDLE遷移時に `spec_mode = false` にクリア。

### 2.4 終了条件

<!-- [v3] Pascal P-1: 判定順序を明確化 -->

SPEC_REVIEW完了後の判定（この順序で評価）:

| # | 条件 | 動作 |
|---|---|---|
| 1 | received = 0（全員タイムアウト/未応答）| → SPEC_REVIEW_FAILED |
| 2 | received > 0 かつ parse_success全員False | → SPEC_PAUSED（全員パース失敗）|
| 3 | parse_success=True の件数 < MIN_VALID_REVIEWS | → SPEC_REVIEW_FAILED |
| 4 | P1以上なし | → SPEC_APPROVED |
| 5 | MAX_CYCLES到達 かつ P1以上残存 | → SPEC_STALLED |
| 6 | P1以上あり かつ MAX未到達 | → SPEC_REVIEW（ループ継続）|

**定数:**
- `MAX_SPEC_REVISE_CYCLES = 5`
- <!-- [v3] Leibniz C-4 --> `MIN_VALID_REVIEWS`: review_modeに追随。full=2, standard=2, lite=1

### 2.5 早期終了オプション

| --skip-review | --review-only | --no-queue | --auto-continue | 開始 | 終了 | M確認 | 用途 |
|---|---|---|---|---|---|---|---|
| ✗ | ✗ | ✗ | ✗ | REVIEW | DONE | APPROVED時 | 全工程（デフォルト）|
| ✗ | ✗ | ✗ | ✓ | REVIEW | DONE | なし | 全工程（自動進行）|
| ✗ | ✓ | — | — | REVIEW | DONE | なし | レビューのみ |
| ✗ | ✗ | ✓ | ✗ | REVIEW | DONE | APPROVED時 | Issue起票まで |
| ✓ | ✗ | ✗ | (強制✓) | APPROVED | DONE | なし | Issue化+キュー（レビュー済specの後工程）|
| ✓ | ✗ | ✓ | (強制✓) | APPROVED | DONE | なし | Issue起票のみ |
| ✓ | ✓ | — | — | **エラー** | — | — | 無意味 |

---

## 3. パイプライン設定

### 3.1 pipeline.json 拡張

spec modeは既存 `batch[]` を**使用しない**。全てを `spec_config` に格納。

```json
{
  "project": "gokrax",
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
| review_only | bool | — | false | レビューサイクルのみ（Issue分割・キュースキップ） |
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
| review_requests | dict | — | {} | per-reviewerタイムアウト管理（§5.2）|
| current_reviews | dict | — | {} | <!-- [v3] Pascal P-2 --> 進行中ラウンドのパース結果を永続化 |
| issue_suggestions | dict | — | {} | Issue分割提案 |
| created_issues | list[int] | — | [] | 起票済みIssue番号 |
| review_history | list | — | [] | ラウンド結果サマリー |
| force_events | list | — | [] | <!-- [v3] Leibniz M-3 --> approve --force監査ログ |
| retry_counts | dict | — | {} | <!-- [v3] Leibniz C-6 --> 状態別リトライ回数 |
| paused_from | str\|null | — | null | PAUSED復帰先 |

<!-- [v3] Pascal P-2: current_reviews の構造 -->
**current_reviews の構造:**
```json
"current_reviews": {
  "pascal": {
    "verdict": "P0",
    "items": [...],
    "raw_text": "...",
    "parse_success": true
  }
}
```
PAUSED/再起動からの復帰時にデータを喪失しない。ラウンド完了時にreview_historyへ移動しcurrent_reviewsをクリア。

<!-- [v3] Leibniz C-6: retry_counts の構造とインクリメント条件 -->
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

**インクリメントしないとき（+0）:**
- SPEC_REVIEW: 個別レビュアーのタイムアウト（per-reviewer管理のため）
- ISSUE_SUGGESTION: 個別レビュアーのタイムアウト

状態遷移時に遷移先のretry_countsエントリをリセット（0に戻す）。MAX_SPEC_RETRIES超過で当該状態からSPEC_PAUSEDに遷移。

### 3.2 config.py 追加定数

```python
MAX_SPEC_REVISE_CYCLES = 5
# [v3] Leibniz C-4: review_modeに追随
MIN_VALID_REVIEWS_BY_MODE = {"full": 2, "standard": 2, "lite": 1, "min": 1}
SPEC_REVIEW_TIMEOUT_SEC = 1800
SPEC_REVISE_TIMEOUT_SEC = 1800
SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC = 600
SPEC_REVISE_SELF_REVIEW_PASSES = 2
MAX_SPEC_RETRIES = 3
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

---

## 4. CLIインターフェース

### 4.1 コマンド体系

```
gokrax spec start      パイプライン開始
gokrax spec approve    SPEC_APPROVEDに遷移 [--force]
gokrax spec continue   APPROVED → ISSUE_SUGGESTION
gokrax spec done       DONE → IDLE
gokrax spec retry      FAILED → REVIEW
gokrax spec resume     PAUSED → paused_from
gokrax spec extend     STALLED → REVIEW (MAX増加)
gokrax spec status     ステータス表示
```

### 4.2 gokrax spec start

```
gokrax spec start --pj PROJECT --spec SPEC_PATH --implementer AGENT_ID
                  [--review-only] [--no-queue] [--skip-review]
                  [--max-cycles N] [--review-mode MODE] [--model MODEL]
                  [--auto-continue]
```

**前提条件:** IDLE状態、specファイル存在、implementer利用可能、`--skip-review --review-only`はエラー

**動作:**
1. pipeline.json を flock で排他ロック
2. spec_mode=true + spec_config書き込み
3. review_requestsにレビュアーリスト初期化（--skip-reviewでも初期化）
4. enabled=true
5. --skip-review → APPROVED（--auto-continueも暗黙にtrue。M確認なしでISSUE_SUGGESTIONへ直行）
6. それ以外 → REVIEW

### 4.3 gokrax spec approve

```
gokrax spec approve --pj PROJECT [--force]
```

- --forceなし: P1以上あればエラー
- --forceあり: 強制承認。force_eventsに記録 + Discord監査通知

<!-- [v3] Leibniz M-3: force_event構造 -->
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

### 4.4 gokrax spec status

```
gokrax [SPEC_REVIEW] rev2 (cycle 1/5, retries: REVIEW=0/3)
  spec: docs/spec-mode-spec.md
  implementer: second
  reviewers: pascal(✅ P0×1), leibniz(⏳), dijkstra(⏳)
  min_valid: 2 (full mode)
```

### 4.5 gokrax spec retry

<!-- [v3] Dijkstra M-1 -->
```
gokrax spec retry --pj PROJECT
```

**前提条件:** SPEC_REVIEW_FAILED状態のみ
**動作:**
1. review_requestsの全エントリをpending/nullにリセット
2. retry_counts["SPEC_REVIEW"]をリセット（0）
3. SPEC_REVIEWに遷移（watchdogが再送信）

### 4.6 gokrax spec resume

<!-- [v3] Dijkstra M-1, M-2 -->
```
gokrax spec resume --pj PROJECT
```

**前提条件:** SPEC_PAUSED状態のみ
**動作:**
1. paused_fromを読み取り
2. paused_fromがnullならエラー
<!-- [v3] Dijkstra M-2: 復帰先バリデーション -->
3. paused_fromへの遷移のみ許可（他の状態への遷移は不可）
<!-- [v3] Pascal P-3: タイムアウト再計算 -->
4. review_requests内の全pending entryのtimeout_atを現在時刻ベースで再計算
5. retry_counts[paused_from]をリセット（0）
6. paused_fromに遷移、paused_fromをnullにクリア

### 4.7 gokrax spec extend

<!-- [v3] Leibniz C-5 / Dijkstra m-1 -->
```
gokrax spec extend --pj PROJECT [--cycles N]
```

**前提条件:** SPEC_STALLED状態のみ
**動作:**
1. max_revise_cycles += N（デフォルト N=2）
2. revise_countはリセット**しない**（既存の改訂履歴を保持）
3. SPEC_REVIEWに遷移（次のレビューラウンド開始）

---

## 5. SPEC_REVIEWフェーズ

### 5.1 レビュー依頼の送信

<!-- [v3] Leibniz C-1: watchdog process()のbatch空ガードに対応 -->

**watchdog.py process()への統合:**
```python
# [v3] Leibniz C-1: spec_modeのときはbatch空を許容
if state != "DONE" and not batch and not pipeline.get("spec_mode"):
    logger.warning("batch empty, skipping")
    return

if pipeline.get("spec_mode") and state in SPEC_STATES:
    action = check_transition_spec(state, spec_config, now)
    # spec専用のDCLブロックで処理
    ...
    return
```

各レビュアーに **`send_to_agent()`**（改行保持）でレビュー依頼を送信。spec本文は**埋め込まない**。

**初回プロンプト:**

```
以下の仕様書をレビューしてください。**やりすぎレビュー**を依頼します。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev}, {line_count}行)

## レビュー指示
- 重篤度を必ず付与: 🔴 Critical (P0) / 🟠 Major (P1) / 🟡 Minor / 💡 Suggestion
- セクション番号を明記（例: §6.2）
- 擬似コード間の整合性に特に注意
- 既存gokraxコードベースとの整合性も確認
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
<!-- [v3] Dijkstra M-3: §12.1と完全一致 -->
`{repo_path}/reviews/{YYYYMMDD}T{HHMMSS}_{reviewer}_{spec_name}_rev{current_rev}.md`
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
`{repo_path}/reviews/{YYYYMMDD}T{HHMMSS}_{reviewer}_{spec_name}_rev{current_rev}.md`
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

**遷移判定（§2.4の順序で評価）:**
1. 全reviewer status = received|timeout → 判定開始
2. received=0 → SPEC_REVIEW_FAILED
3. received>0 かつ 全員parse_success=False → SPEC_PAUSED（paused_from="SPEC_REVIEW"）
4. parse_success=True件数 < MIN_VALID_REVIEWS → SPEC_REVIEW_FAILED
5. revise_count=0（初回レビュー）→ SPEC_REVISE（改訂サイクルへ）
6. revise_count>0（改訂後の再レビュー）→ `should_continue_review()`（§6.4）で終了判定

**注:** `should_continue_review()` は SPEC_REVIEW 完了後（ステップ6）と SPEC_REVISE 完了後の両方で呼ばれる。初回レビュー（revise_count=0）は無条件にREVISEへ進む（終了判定不要）。

### 5.3 レビュー結果のパース

**決定性最優先。**

1. YAMLブロック正規表現抽出（最初の1ブロックのみ）
2. verdict/severityにエイリアスマッピング適用

<!-- [v3] Leibniz M-1 / Dijkstra m-2: verdict側からseverity語彙を除外 -->
```python
# [v3] 厳格化: verdictにはverdict語彙のみ許容
VERDICT_ALIASES = {
    "approve": "APPROVE", "APPROVE": "APPROVE",
    "p0": "P0", "P0": "P0",
    "reject": "P0", "REJECT": "P0",
    "p1": "P1", "P1": "P1",
    # "critical", "major" 等のseverity語彙はverdictとして不許可 → parse_success=False
}
SEVERITY_ALIASES = {
    "critical": "critical", "Critical": "critical",
    "major": "major", "Major": "major",
    "minor": "minor", "Minor": "minor",
    "suggestion": "suggestion", "Suggestion": "suggestion",
    # "P0", "P1" 等のverdict語彙はseverityとして不許可 → parse_success=False
}
```

3. **不正値（マッピング外の値）→ parse_success=False**。raw_textを保持
4. 全員パース失敗 → SPEC_PAUSED

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

### 5.4 重複検出・統合

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

<!-- [v3] Dijkstra M-4: パス2詳細 -->

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

各パスは `status: clean | issues_found` で報告。

### 6.3 改訂完了の検知

1. YAML `status: done` 確認
2. セルフレビュー パス1 + パス2
3. last_commit, current_rev, rev_index 更新
4. <!-- [v3] Pascal P-6 / Dijkstra C-3 --> **revise_count += 1**（commit確認後、終了判定前）
5. current_reviewsの内容をreview_historyへ移動、current_reviewsをクリア
6. review_history にラウンド結果追加

### 6.4 終了判定

```python
def should_continue_review(
    spec_config: dict,
    reviews: list[SpecReviewResult],
    review_mode: str,
) -> str:  # "continue"|"approved"|"stalled"|"failed"|"paused"
    # [v3] Pascal P-1: 判定順序を明確化
    received = [r for r in reviews if r.raw_text is not None]
    if len(received) == 0:
        return "failed"  # 全員タイムアウト/未応答

    valid = [r for r in received if r.parse_success]
    if len(valid) == 0:
        return "paused"  # 全員パース失敗

    # [v3] Leibniz C-4: review_modeに追随
    min_valid = MIN_VALID_REVIEWS_BY_MODE.get(review_mode, 2)
    if len(valid) < min_valid:
        return "failed"

    has_p1 = any(r.verdict in ("P0", "P1") for r in valid)
    if spec_config["revise_count"] >= spec_config["max_revise_cycles"]:
        return "stalled" if has_p1 else "approved"
    return "continue" if has_p1 else "approved"
```

**既存CODE_REVISEとの差異:** 既存はP0のみreviseトリガ。spec modeは**P1以上でループ継続**。

---

## 7. ISSUE_SUGGESTIONフェーズ

<!-- [v3] Dijkstra M-5: プロンプト復元 -->

M が `gokrax spec continue` 実行後に遷移。

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

<!-- [v3] Dijkstra M-5: プロンプト復元 -->

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

<!-- [v3] Pascal P-4: レースコンディション対策 -->
起票時タイトルに `[spec:{spec_name}:S-{N}]` を含め、起票前にglab issue listで重複チェック。起票済みIssue番号は逐次 `created_issues[]` に記録し、リトライ時はスキップ。

### 8.2 注記の存在検査

起票後に `glab issue show` で読み戻し、⚠️注記を検査。欠落時は `glab issue note` で自動追記。

---

## 9. QUEUE_PLANフェーズ

<!-- [v3] Dijkstra M-5: プロンプト復元 -->

spec_implementerに`send_to_agent()`で依頼:
```
起票済みIssueからgokrax-queue.txtのバッチ行を生成してください。

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
queue_file: "gokrax-queue.txt"
```
```

`config.QUEUE_FILE` に追記。完了後 → SPEC_DONE。M が `gokrax spec done` で IDLE。

---

## 10. Watchdog統合

### 10.1 watchdog.py拡張

<!-- [v3] Leibniz C-3 / Dijkstra C-2: I/O整理 -->

```python
@dataclass
class SpecTransitionAction:
    # [v3] 既存TransitionActionに寄せたフィールド名
    next_state: str | None
    send_to: dict[str, str] | None      # {agent_id: message}
    discord_notify: str | None          # Discord通知テキスト
    pipeline_updates: dict | None       # spec_configへの更新差分
    error: str | None

def check_transition_spec(
    state: str,
    spec_config: dict,
    # [v3] Dijkstra C-2: review_data引数廃止。全データはspec_config内
    now: datetime,
) -> SpecTransitionAction:
    """純粋関数。副作用なし。"""
    if state not in SPEC_STATES:
        return SpecTransitionAction(
            next_state="SPEC_PAUSED",
            error=f"Unknown spec state: {state}",
            discord_notify=f"[Spec] ⚠️ 未知状態 {state} → SPEC_PAUSED",
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
                discord_notify=f"[Spec] spec承認 → Issue分割へ自動進行（--auto-continue）")
        # デフォルト: M確認待ち。通知は状態遷移時（→SPEC_APPROVEDへの遷移時）に1回だけ送信。
        # watchdogのtick毎ではなく、SPEC_REVISEからの遷移実行時にdiscord_notifyを発火する。
        # SPEC_APPROVED滞在中のcheck_transition_specは next_state=None, discord_notify=None を返す。
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

<!-- [v3] Leibniz C-1: process()内のspec専用DCLブロック -->
**process()内の統合:**
```python
# 既存のbatch空チェックを修正
if state != "DONE" and not batch and not pipeline.get("spec_mode"):
    logger.warning("batch empty, skipping")
    return

# spec mode専用処理
if pipeline.get("spec_mode") and state in SPEC_STATES:
    spec_config = pipeline.get("spec_config", {})
    action = check_transition_spec(state, spec_config, now)
    if action.next_state:
        with pipeline_lock:
            # DCL: 再読み込み→再計算→更新
            action2 = check_transition_spec(state, spec_config, now)
            if action2.next_state:
                pipeline["state"] = action2.next_state
                if action2.pipeline_updates:
                    pipeline["spec_config"].update(action2.pipeline_updates)
                save_pipeline(pipeline)
        if action2.send_to:
            for agent_id, msg in action2.send_to.items():
                send_to_agent(agent_id, msg)
        if action2.discord_notify:
            notify_discord(action2.discord_notify)
    return
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

**成功系:** REVIEW開始/完了、REVISE完了、ISSUE完了、QUEUE完了

**状態遷移時の通知（遷移実行時に1回だけ送信）:**
- → SPEC_APPROVED: `[Spec] {project}: spec承認 (rev{N})。\`gokrax spec continue\` でIssue分割へ`
- → SPEC_APPROVED (forced): `[Spec] ⚠️ {project}: 強制承認 (P1以上 {n}件残存, actor: {actor})`
- → SPEC_STALLED: `[Spec] ⏸️ {project}: MAX_CYCLES到達、P1以上 {n}件残存`
- → SPEC_REVIEW_FAILED: `[Spec] ❌ {project}: 有効レビュー不足`
- → SPEC_PAUSED: `[Spec] ⏸️ {project}: パイプライン停止 — {reason}`

<!-- [v3] Dijkstra m-5: commit空の区別 -->
**REVISE完了通知:**
- commit hashあり: `[Spec] {project}: rev{N} 完了 ({commit})`
- git commit失敗（commit空）: `[Spec] ⚠️ {project}: rev{N} git commit失敗` → SPEC_PAUSED
- 変更なし（差分0）: `[Spec] ⚠️ {project}: rev{N} 変更なし（改訂が空）` → SPEC_PAUSED

**失敗系:** YAMLパース失敗、送信失敗、git push失敗、glab起票失敗、REVIEW_FAILED、STALLED、PAUSED

---

## 12. レビュー結果の保存

### 12.1 ファイル保存

<!-- [v3] Leibniz M-2: コミット規約整理 -->

**レビュー原文:** `{pipelines_dir}/spec-reviews/` に保存（repo外、バージョン管理対象外）。PAUSED復帰時のデータ源。

**レビューサマリー:** repo内 `reviews/` に保存（バージョン管理対象、mainブランチ直接commit）。

```
reviews/{YYYYMMDD}T{HHMMSS}_{reviewer}_{spec_name}_rev{N}.md
reviews/{YYYYMMDD}T{HHMMSS}_merged_{spec_name}_rev{N}.md
```

**コミット規約:**
- コミット主体: gokrax（watchdog経由）
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
| gokrax.py | spec CLI（8コマンド）| +280行 |
| watchdog.py | check_transition_spec + process統合 + 各状態 | +350行 |
| notify.py | 通知（成功+失敗）| +120行 |
| config.py | 定数 + SPEC_STATES + TRANSITIONS（マージ方式）| +60行 |
| pipeline_io.py | 初期化・バリデーション・flock | +60行 |
| spec_review.py | **新規** パース・エイリアス・統合レポート | +300行 |
| spec_revise.py | **新規** 改訂依頼・セルフレビュー・終了判定 | +280行 |
| spec_issue.py | **新規** Issue分割・起票・注記検査・キュー | +300行 |
| tests/ | テスト | +550行 |
| **合計** | | **~2,300行** |

### 13.2 Issue分割案

| Issue | タイトル | 依存 | 行数 |
|---|---|---|---|
| S-1 | config.py + pipeline_io.py: spec mode基盤（TRANSITIONS マージ方式含む）| なし | +120 |
| S-2 | gokrax.py: spec CLI（8コマンド）| S-1 | +280 |
| S-3 | spec_review.py: パース+エイリアス+統合レポート | S-1 | +300 |
| S-4 | watchdog.py: process統合 + REVIEW/REVISE判定 | S-2, S-3 | +250 |
| S-5 | spec_revise.py: 改訂+セルフレビュー+終了判定 | S-3 | +280 |
| S-6 | spec_issue.py: SUGGESTION+PLAN+注記+QUEUE | S-1, S-3 | +300 |
| S-7 | notify.py: 通知 | S-4 | +120 |
| S-8 | watchdog.py: ISSUE系+異常系+全状態カバー | S-6, S-7 | +150 |
| S-9 | 統合テスト | S-8 | +550 |

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
- embedding類似度による重複候補提示
- SPEC_PAUSED自動リカバリ（一過性エラー）

---

## 附録A: v1→v2変更履歴

<!-- [v3] Dijkstra s-2: 本文からchangelogを分離 -->

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
