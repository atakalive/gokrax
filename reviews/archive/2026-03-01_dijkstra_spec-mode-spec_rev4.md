# Spec Mode 仕様書 rev4 レビュー — Dijkstra

**Date:** 2026-03-01
**Spec:** docs/spec-mode-spec_rev4.md (rev4, 1136行)
**Reviewer:** Dijkstra
**Verdict:** APPROVE

---

## rev3 指摘の反映状況

rev3 の Critical 2件、Major 3件は**全て適切に反映**:
- C-1 (DCL stale data) → update_pipeline(path, callback) パターン + applied フラグ ✅
- C-2 (should_continue_review タイミング) → REVISE→常にREVIEW + 判定1箇所集約 ✅
- M-1〜M-3 全件 ✅

---

## サマリー

| 重篤度 | 件数 |
|---|---|
| 🔴 Critical (P0) | 0 |
| 🟠 Major (P1) | 1 |
| 🟡 Minor | 2 |
| 💡 Suggestion | 1 |
| **合計** | **4** |

---

## 🟠 Major (P1)

### M-1: next_state=None のアクションが破棄される (§5.1 / §10.1)

process() で `if action.next_state:` のガードにより、状態遷移なしのアクション（タイムアウト再送、retry_counts更新、current_reviews保存）が全て破棄される。SPEC_REVISE/ISSUE_PLAN/QUEUE_PLAN のタイムアウト再送（§10.2）が機能しない。

**修正案:** ガードを `if action.next_state or action.pipeline_updates or action.send_to:` に変更。_apply_spec_action 内も next_state=None 時は状態遷移スキップだが updates/send は適用。

---

## 🟡 Minor

### m-1: 期限切れファイル削除の実行主体が未指定 (§12.1)

### m-2: _apply_spec_action の再計算結果不一致時の1 tick遅延が意図的であることの明記なし (§10.1)

---

## 💡 Suggestion

### s-1: should_continue_review が current_reviews を内部参照する設計変更は良い判断 (§5.3)

---

## 総評

APPROVE。rev1→rev4で4ラウンド、延べ57件の指摘が処理された。

REVISE→常にREVIEW の単純化、update_pipeline パターン採用、_apply_spec_action の applied フラグ — いずれも正しい設計判断。仕様の構造は堅牢。

M-1 は実装時に必ず踏む問題だが修正は局所的。仕様全体の品質に影響しない。
