# Spec Mode 不具合調査レポート

**Date:** 2026-03-04
**対象:** spec-mode-spec_rev7.md (v7.0) vs 実装コード (commit 0daf387)
**調査者:** CC-Opus

---

## 調査サマリー

テストは全168件パスするが、テスト側が本番に存在しない回避策（手動リセット・直接フィールド設定）を含んでおり、テスト通過が本番動作の保証になっていない。以下4件の問題を特定した。

| ID | 重篤度 | 概要 | 影響 |
|---|---|---|---|
| C1 | Critical | ISSUE_SUGGESTION 遷移時 review_requests 未リセット | 即座に SPEC_PAUSED |
| C2 | Critical | implementer 応答受領 CLI 未実装 (3フェーズ) | 常にタイムアウト → SPEC_PAUSED |
| C3 | Major | MIN_VALID_REVIEWS_BY_MODE 仕様不一致 | 1人タイムアウトで即 failed |
| I1 | Minor | reviewed_rev 未設定 | データ整合性のみ |

---

## C1: SPEC_APPROVED → ISSUE_SUGGESTION 遷移時に review_requests がリセットされない

### 重篤度: Critical

### 根本原因

`cmd_spec_continue` ([gokrax.py:1185-1188](gokrax.py#L1185-L1188)) および `check_transition_spec` の auto_continue 分岐 ([watchdog.py:1556-1559](watchdog.py#L1556-L1559)) が、状態遷移のみ実行し `review_requests` のリセットと `current_reviews` のクリアを行わない。

**cmd_spec_continue の現状:**
```python
def do_continue(data):
    data["state"] = "ISSUE_SUGGESTION"
    # ← review_requests リセットなし
    # ← current_reviews クリアなし
```

**check_transition_spec auto_continue の現状:**
```python
if spec_config.get("auto_continue"):
    return SpecTransitionAction(
        next_state="ISSUE_SUGGESTION",
        discord_notify=...,
        # ← pipeline_updates なし（リセットなし）
    )
```

### 症状

1. SPEC_REVIEW フェーズで全レビュアーが `status="received"` になる
2. SPEC_APPROVED → ISSUE_SUGGESTION に遷移（`review_requests` はそのまま）
3. `_check_issue_suggestion` ([watchdog.py:1234-1361](watchdog.py#L1234-L1361)) が実行される
4. 全レビュアーの `status` が `"received"`（`"pending"` ではない）ため:
   - 送信分岐 (`status == "pending" and sent_at is None`) に入らない → プロンプト未送信
   - タイムアウト分岐 (`status == "pending" and timeout_at`) にも入らない
5. `_effective_status()` が全員 `"received"` を返す → `all_complete = True`
6. `issue_suggestions` は空（誰にも送信していないため）→ `SPEC_PAUSED` ("Issue分割提案: 有効応答なし")

### テストが通る理由

E2E テスト ([test_spec_mode_integration.py:189](tests/test_spec_mode_integration.py#L189)) が本番コードには存在しない手動リセットを行っている:

```python
# --- tick 1: ISSUE_SUGGESTION — 送信 ---
# review_requests をリセット
sc["review_requests"] = _pending_review_requests()   # ← テスト専用の回避策
```

この行により、テスト内では全レビュアーが `pending` に戻り、正常にプロンプト送信→回収のフローが動作する。しかし本番の `_apply_spec_action` にはこのリセット処理がない。

### 再現手順

```bash
gokrax spec start --pj gokrax --spec docs/test.md --implementer kaneko --auto-continue
# → SPEC_REVIEW（レビュアーにプロンプト送信）
gokrax spec review-submit --pj gokrax --reviewer pascal --file review_pascal.yaml
gokrax spec review-submit --pj gokrax --reviewer leibniz --file review_leibniz.yaml
gokrax spec review-submit --pj gokrax --reviewer dijkstra --file review_dijkstra.yaml
# → should_continue_review() が "approved" → SPEC_APPROVED
# → auto_continue → ISSUE_SUGGESTION
# → 次の watchdog tick で即座に SPEC_PAUSED
```

---

## C2: implementer 応答受領 CLI が存在しない

### 重篤度: Critical

### 根本原因

commit 067bb46 で `review-submit` サブコマンドが追加され、レビュアーが CLI 経由でレビュー結果を投入できるようになった。しかし、implementer フェーズ（SPEC_REVISE / ISSUE_PLAN / QUEUE_PLAN）には等価のコマンドが実装されていない。

| フェーズ | 内部フィールド | watchdog 読み取り箇所 | 受領 CLI |
|---|---|---|---|
| SPEC_REVIEW | `current_reviews.entries[reviewer]` | [watchdog.py:1280](watchdog.py#L1280) | `review-submit` ✅ |
| SPEC_REVISE | `spec_config._revise_response` | [watchdog.py:1145](watchdog.py#L1145) | **未実装** ❌ |
| ISSUE_PLAN | `spec_config._issue_plan_response` | [watchdog.py:1382](watchdog.py#L1382) | **未実装** ❌ |
| QUEUE_PLAN | `spec_config._queue_plan_response` | [watchdog.py:1463](watchdog.py#L1463) | **未実装** ❌ |

### 症状

**SPEC_REVISE フェーズ:**
1. watchdog が `send_to_agent()` で implementer に改訂依頼を送信
2. implementer が改訂作業を完了し、YAML 報告を生成
3. `_revise_response` を pipeline.json に書き込む手段がない
4. `_check_spec_revise` ([watchdog.py:1135-1227](watchdog.py#L1135-L1227)) は `_revise_response` が None のためタイムアウト分岐に進む
5. 1800秒後にタイムアウト → リトライ → 3回後 SPEC_PAUSED

**ISSUE_PLAN / QUEUE_PLAN フェーズ:** 同様のメカニズムで常にタイムアウト。

### テストが通る理由

テストは `spec_config` を直接操作して応答フィールドを設定している:

```python
# test_spec_mode_integration.py:274
sc["_revise_response"] = _revise_yaml("2", "abc1234", 50, 10)

# test_spec_mode_integration.py:223
sc["_issue_plan_response"] = _yaml_block("status: done\ncreated_issues:\n  - 51\n  - 52\n")

# test_spec_mode_integration.py:239
sc["_queue_plan_response"] = _yaml_block("status: done\nbatches: 2\nqueue_file: /tmp/q.txt\n")
```

本番では pipeline.json に `_revise_response` 等を書き込む CLI がないため、このフィールドは常に `None` のまま。

### 不足している CLI コマンド

仕様 §6.1, §8.1, §9 に基づき、以下3コマンドが必要:

```
gokrax spec revise-submit --pj PROJECT --file FILE    # SPEC_REVISE 完了報告
gokrax spec plan-submit   --pj PROJECT --file FILE    # ISSUE_PLAN 完了報告
gokrax spec queue-submit  --pj PROJECT --file FILE    # QUEUE_PLAN 完了報告
```

各コマンドは既存の `review-submit` ([gokrax.py:1356-1471](gokrax.py#L1356-L1471)) と同パターンで:
- ファイル読み込み → 既存パーサーでバリデーション → 状態チェック → flock 内で pipeline.json 更新

---

## C3: MIN_VALID_REVIEWS_BY_MODE が仕様と不一致

### 重篤度: Major

### 根本原因

[config.py:296-298](config.py#L296-L298) の値が spec §3.2 の定義と異なる。

**仕様 §3.2:**
```python
MIN_VALID_REVIEWS_BY_MODE = {"full": 2, "standard": 2, "lite": 1, "min": 1}
```

**実装 (config.py:296-298):**
```python
MIN_VALID_REVIEWS_BY_MODE = {"full": 3, "standard": 2, "lite": 2, "min": 1}
```

| モード | レビュアー数 | 仕様の min_valid | 実装の min_valid | タイムアウト耐性 |
|---|---|---|---|---|
| full | 3 | 2 | **3** | 仕様: 1人まで許容 / 実装: **0人（全員必須）** |
| standard | 3 | 2 | 2 | 一致 |
| lite | 2 | 1 | **2** | 仕様: 1人まで許容 / 実装: **0人（全員必須）** |
| min | 1 | 1 | 1 | 一致 |

### 症状

`should_continue_review()` ([spec_review.py:179-231](spec_review.py#L179-L231)) の判定:

```python
min_valid = MIN_VALID_REVIEWS_BY_MODE.get(review_mode, 2)
# ...
if len(received) < min_valid:
    if len(parsed_fail) > 0:
        return "paused"
    return "failed"
```

`full` モード（デフォルト）で3人中1人がタイムアウトした場合:
- 仕様: `received=2 >= min_valid=2` → P1/P0判定へ進む
- 実装: `received=2 < min_valid=3` → `"failed"` → SPEC_REVIEW_FAILED

### 経緯

commit 223be57 で `REVIEW_MODES["full"]["min_reviews"]` が 3 に変更された際、`MIN_VALID_REVIEWS_BY_MODE` も追従して変更されたと推測される。しかし `min_reviews`（通常レビューの最低応答数）と `MIN_VALID_REVIEWS_BY_MODE`（spec mode の有効レビュー閾値）は別概念であり、仕様は後者を 2 と定義している。

### テストが通る理由

テスト ([test_spec_mode_integration.py:268](tests/test_spec_mode_integration.py#L268)) は全レビュアーを `received` に設定するため、min_valid=3 でも条件を満たす:

```python
_set_all_received(sc, {"pascal": "P0", "leibniz": "APPROVE", "dijkstra": "APPROVE"})
```

タイムアウト混在のテストケースが不足している。

---

## I1: reviewed_rev が本番コードで設定されない

### 重篤度: Minor

### 根本原因

spec §3.1 [v7] Leibniz M-1 で `current_reviews` のトップレベルに `reviewed_rev` フィールドが定義されている:

```json
"current_reviews": {
  "reviewed_rev": "2",
  "entries": { ... }
}
```

しかし以下の本番コードで `reviewed_rev` が設定されない:

1. `cmd_spec_review_submit` ([gokrax.py:1407-1441](gokrax.py#L1407-L1441)):
   ```python
   cr = sc.setdefault("current_reviews", {})
   entries = cr.setdefault("entries", {})
   entries[args.reviewer] = { ... }
   # ← cr["reviewed_rev"] の設定なし
   ```

2. `_check_spec_review` のタイムアウト処理 ([watchdog.py:1055-1058](watchdog.py#L1055-L1058)):
   ```python
   cr_patch[reviewer] = {
       "verdict": None, "items": [], "raw_text": None,
       "parse_success": False, "status": "timeout",
   }
   # ← reviewed_rev の設定なし
   ```

### 影響

- `gokrax spec status` ([gokrax.py:1014](gokrax.py#L1014)) は fallback を使用するため表示上は問題なし:
  ```python
  cr.get("reviewed_rev", spec_config.get("current_rev", "?"))
  ```
- `build_review_history_entry` ([spec_review.py:362-363](spec_review.py#L362-L363)) も `current_rev` を使用するため `review_history` は正しい
- 仕様準拠の観点でのみ問題

### テストが通る理由

テストヘルパー `_set_all_received` ([test_spec_mode_integration.py:130-131](tests/test_spec_mode_integration.py#L130-L131)) が直接設定:

```python
sc["current_reviews"] = {
    "reviewed_rev": sc.get("current_rev", "1"),
    "entries": { ... },
}
```

---

## 問題間の関係図

```
gokrax spec start
    │
    ▼
SPEC_REVIEW ── C3: min_valid=3 → 1人タイムアウトで即failed
    │
    │ (全員応答成功時)
    ▼
SPEC_APPROVED
    │
    ├── auto_continue ── C1: review_requests未リセット → 即PAUSED
    │
    ├── cmd_spec_continue ── C1: 同上
    │
    ▼
ISSUE_SUGGESTION ── (C1により到達不能)
    │
    ▼
SPEC_REVISE ── C2: _revise_response投入手段なし → 常にタイムアウト
    │
    ▼
ISSUE_PLAN ── C2: _issue_plan_response投入手段なし → 同上
    │
    ▼
QUEUE_PLAN ── C2: _queue_plan_response投入手段なし → 同上
```

C1 により ISSUE_SUGGESTION 以降に到達できず、仮に到達できても C2 により SPEC_REVISE / ISSUE_PLAN / QUEUE_PLAN で停止する。C3 は SPEC_REVIEW フェーズ自体の耐障害性を低下させる。

---

## 関連ファイル

| ファイル | 問題 |
|---|---|
| [config.py:296-298](config.py#L296-L298) | C3 |
| [gokrax.py:1185-1188](gokrax.py#L1185-L1188) | C1 (cmd_spec_continue) |
| [gokrax.py:1356-1471](gokrax.py#L1356-L1471) | C2 (review-submit パターン参照) |
| [gokrax.py:1407-1441](gokrax.py#L1407-L1441) | I1 (cmd_spec_review_submit) |
| [watchdog.py:1055-1058](watchdog.py#L1055-L1058) | I1 (_check_spec_review) |
| [watchdog.py:1135-1227](watchdog.py#L1135-L1227) | C2 (_check_spec_revise) |
| [watchdog.py:1368-1444](watchdog.py#L1368-L1444) | C2 (_check_issue_plan) |
| [watchdog.py:1451-1522](watchdog.py#L1451-L1522) | C2 (_check_queue_plan) |
| [watchdog.py:1556-1559](watchdog.py#L1556-L1559) | C1 (auto_continue) |
| [spec_review.py:179-231](spec_review.py#L179-L231) | C3 (should_continue_review) |
| [tests/test_spec_mode_integration.py:189](tests/test_spec_mode_integration.py#L189) | C1 テスト回避策 |
