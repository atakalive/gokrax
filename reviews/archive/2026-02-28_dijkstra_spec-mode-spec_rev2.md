# Spec Mode 仕様書 rev2 レビュー — Dijkstra

**Date:** 2026-02-28
**Spec:** docs/spec-mode-spec_rev2.md (rev2, 767行)
**Reviewer:** Dijkstra
**Verdict:** P1

---

## rev1 指摘の反映状況

rev1 の Critical 5件、Major 8件は**全て適切に反映されている**。特に:
- C-2 (batch型不整合) → batch 使用廃止、spec_config に全集約 ✅
- C-3 (VALID_STATES未登録) → SPEC_STATES + SPEC_TRANSITIONS 定義 ✅
- C-5 (LLMフォールバック) → 廃止、パース失敗→SPEC_PAUSED ✅

Minor/Suggestion も大半が反映済み。

---

## サマリー

| 重篤度 | 件数 |
|---|---|
| 🔴 Critical (P0) | 3 |
| 🟠 Major (P1) | 5 |
| 🟡 Minor | 5 |
| 💡 Suggestion | 2 |
| **合計** | **15** |

---

## 🔴 Critical (P0)

### C-1: VALID_TRANSITIONS.update() が既存 IDLE 遷移を破壊する (§2.3)

§2.3 の擬似コードで `SPEC_TRANSITIONS` に `"IDLE": ["SPEC_REVIEW", "SPEC_APPROVED"]` を定義し、`VALID_TRANSITIONS.update(SPEC_TRANSITIONS)` している。

既存 config.py の `VALID_TRANSITIONS` は `"IDLE": ["DESIGN_PLAN"]`。`dict.update()` は同一キーを**上書き**するため、update 後は IDLE → DESIGN_PLAN が消える。既存の `devbar start`（IDLE → DESIGN_PLAN）が全て壊れる。

**修正案:** IDLE キーは上書きではなくマージする:
```python
for state, targets in SPEC_TRANSITIONS.items():
    existing = VALID_TRANSITIONS.get(state, [])
    VALID_TRANSITIONS[state] = list(set(existing + targets))
```

### C-2: check_transition_spec の引数 review_data が未定義 (§10.1)

純粋関数 `check_transition_spec(state, spec_config, review_data, now)` の第3引数 `review_data` の型・構造・取得元が未定義。spec_config 内の review_requests と何が違うのか不明。`_check_spec_review()` と `_check_spec_revise()` が review_data をどう使うかも未定義。

**修正案:** review_data を廃止し、シグネチャを `check_transition_spec(state, spec_config, now)` に簡素化。必要な情報は全て spec_config に含まれている。

### C-3: revise_count のインクリメントタイミングが未定義 (§6.3 / §6.4)

`should_continue_review()` は `spec_config["revise_count"]` で MAX_CYCLES 判定するが、revise_count をいつ誰がインクリメントするか記載がない。§6.3 では last_commit, current_rev, rev_index の更新は書かれているが revise_count は欠落。

**修正案:** §6.3 に明記: "改訂完了時（commit確認後、終了判定前）に `revise_count += 1`"。

---

## 🟠 Major (P1)

### M-1: retry / resume コマンドの詳細仕様が欠落 (§4.1)

§4.1 に列挙されているが §4.2〜§4.4 に詳細なし。引数、前提条件、paused_from バリデーション、retry_count リセット有無が全て未定義。

**修正案:** §4.5 devbar spec retry, §4.6 devbar spec resume を追加。

### M-2: SPEC_PAUSED の遷移先バリデーションが不十分 (§2.3)

SPEC_PAUSED から5状態に遷移可能だが、paused_from が ISSUE_PLAN なのに SPEC_REVIEW に resume できてしまう。cmd_transition() のリストチェックだけでは防げない。

**修正案:** resume コマンド内で paused_from と遷移先の一致を強制バリデーション。

### M-3: レビュー保存先パスがプロンプト内とファイル命名規則で不一致 (§5.1 / §12.1)

§5.1 プロンプト: `{repo_path}/reviews/{date}_{reviewer}_{spec_name}_rev{current_rev}.md`
§12.1 規則: `reviews/{YYYYMMDD}T{HHMMSS}_{reviewer}_{spec_name}_rev{N}.md`

{date} が曖昧で §12.1 の {YYYYMMDD}T{HHMMSS} と不一致。レビュアーは {repo_path} を知らない。

**修正案:** プロンプト内のパスを §12.1 と完全一致させる。

### M-4: セルフレビュー パス2 の詳細仕様が未定義 (§6.2)

レビュアー選択ロジック、依頼プロンプト、タイムアウト、応答パース、Critical 指摘時のフロー分岐が全て未定義。

### M-5: ISSUE_SUGGESTION / ISSUE_PLAN / QUEUE_PLAN のプロンプトが消失 (§7 / §8 / §9)

rev1 の詳細プロンプトテンプレートが rev2 で各2-3行に圧縮され消えている。

**修正案:** rev1 のプロンプトテンプレートを復元。

---

## 🟡 Minor

### m-1: SPEC_STALLED → SPEC_REVIEW の遷移トリガ CLI が未定義 (§2.1)

### m-2: VERDICT_ALIASES と SEVERITY_ALIASES のフィールド区別制約が未記載 (§5.3)

### m-3: check_transition_spec の ISSUE_*/QUEUE_PLAN 処理が省略記号 (§10.1)

### m-4: review_requests のフィールド表での型説明が不十分 (§3.1)

### m-5: commit hash 空で SPEC_PAUSED だが「変更なし」と「git失敗」が区別不能 (§11)

---

## 💡 Suggestion

### s-1: MIN_VALID_REVIEWS = 1 は spec レビューには低い (§2.4)

full mode (3人) で1人応答なら1人で改訂に進む。MIN_VALID_REVIEWS = 2 を推奨。

### s-2: 変更履歴が本文より長くなりつつある

約120行（全体の15%）。CHANGELOG.md に分離を推奨。

---

## 総評

rev1 の致命的問題は全て解消。batch 分離、LLM フォールバック廃止、異常系3状態追加は正しい方向。

C-1（VALID_TRANSITIONS 上書き）は既存フロー全体を壊す1行バグであり最も危険。仕様の擬似コードを信じて実装すると `devbar start` が動かなくなる。

全体的に「設計方針は正しいが詳細仕様が抜けている」傾向。セルフレビュー パス2、retry/resume、ISSUE系プロンプトなど。rev3 では C-1〜C-3 + M-1/M-5 の修正を求める。
