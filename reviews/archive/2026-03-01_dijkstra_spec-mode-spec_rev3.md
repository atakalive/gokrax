# Spec Mode 仕様書 rev3 レビュー — Dijkstra

**Date:** 2026-03-01
**Spec:** docs/spec-mode-spec_rev3.md (rev3, 1092行)
**Reviewer:** Dijkstra
**Verdict:** P1

---

## rev2 指摘の反映状況

rev2 の Critical 3件、Major 5件は**全て適切に反映**:
- C-1 (VALID_TRANSITIONS上書き) → マージ方式 ✅
- C-2 (review_data廃止) → spec_configに集約 ✅
- C-3 (revise_countタイミング) → §6.3 ステップ4に明記 ✅
- M-1〜M-5 全件 ✅

---

## サマリー

| 重篤度 | 件数 |
|---|---|
| 🔴 Critical (P0) | 2 |
| 🟠 Major (P1) | 3 |
| 🟡 Minor | 4 |
| 💡 Suggestion | 2 |
| **合計** | **11** |

---

## 🔴 Critical (P0)

### C-1: DCL ブロックで spec_config を再読み込みしていない (§10.1)

process() 内の DCL ブロックで、ロック内の再計算に stale な spec_config を使用。既存 watchdog.py は `update_pipeline(path, callback)` 内でディスクから再読み込みする。仕様の擬似コードはこの重要なステップが欠落。

並行実行（watchdog tick と devbar spec approve が同時）で古い spec_config に基づいて遷移し、approve の効果が消失する等の競合が起きる。

**修正案:** 既存パターンに合わせ update_pipeline(path, callback) を使用:
```python
if action.next_state:
    def _update(data):
        sc = data.get("spec_config", {})
        action2 = check_transition_spec(data["state"], sc, now)
        if action2.next_state:
            data["state"] = action2.next_state
            if action2.pipeline_updates:
                data["spec_config"].update(action2.pipeline_updates)
    pipeline = update_pipeline(path, _update)
```

### C-2: should_continue_review の呼び出しタイミングとデータソースが矛盾 (§5.2 / §6.3 / §6.4)

§5.2 注記で「should_continue_review() は SPEC_REVIEW 完了後と SPEC_REVISE 完了後の両方で呼ばれる」とあるが、§6.3 ステップ5で current_reviews をクリアした後に呼ぶと reviews が空。

SPEC_REVISE 完了後に APPROVED/STALLED に直行するのか、常に SPEC_REVIEW に戻るのか、設計が曖昧。

**推奨:** REVISE は常に REVIEW に戻す。改訂後に再レビューせず承認するのは品質保証として不十分。SPEC_TRANSITIONS を `SPEC_REVISE → [SPEC_REVIEW, SPEC_PAUSED]` に修正。

---

## 🟠 Major (P1)

### M-1: §6.3 ステップ5とステップ6が重複 (§6.3)

ステップ5「current_reviews → review_history 移動」とステップ6「review_history にラウンド結果追加」が同じ操作の別表現か別操作か不明。

**修正案:** 統合して「current_reviews から §12.2 形式の要約を生成し review_history に追加。current_reviews をクリア」。

### M-2: セルフレビュー パス2 リトライ上限後の挙動が未定義 (§6.2)

最大1回リトライ後も issues_found の場合の遷移先が未定義。

**修正案:** → SPEC_PAUSED（paused_from="SPEC_REVISE"）。

### M-3: SPEC_APPROVED 通知の発火元が暗黙的 (§10.1 / §11)

check_transition_spec で state=="SPEC_APPROVED" → discord_notify=None。しかし §11 では SPEC_APPROVED 通知が定義。遷移元（_check_spec_revise）が通知を返す設計であることが明示されていない。

---

## 🟡 Minor

### m-1: retry が retry_counts["SPEC_REVIEW"] をリセットするが SPEC_REVIEW は retry_counts を使わない (§4.5)

### m-2: extend 後の SPEC_REVIEW で review_requests が stale (§4.7)

全エントリが received/timeout のまま → watchdog が即座に「全員回収済み」と判定する可能性。review_requests のリセットが必要。

### m-3: --review-only --auto-continue の組み合わせ挙動が未定義 (§2.5)

### m-4: SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC = 600 は短い可能性 (§3.2)

---

## 💡 Suggestion

### s-1: process() のロックパターンが既存 update_pipeline() と乖離 (§10.1)

### s-2: ISSUE_SUGGESTION プロンプトにレビュー統合レポートを含めるべき (§7)

---

## 総評

rev1 (28件) → rev2 (15件) → rev3 (11件) で着実に収束。ステートマシンの成熟度は高い。

C-1（DCL stale data）と C-2（should_continue_review タイミング）を解消すれば APPROVE できる。

rev3 の品質は APPROVE に近い。
