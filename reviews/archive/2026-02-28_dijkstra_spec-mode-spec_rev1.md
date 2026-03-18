# Spec Mode 仕様書 レビュー — Dijkstra

**Date:** 2026-02-28
**Spec:** docs/spec-mode-spec_rev1.md (rev1, 821行)
**Reviewer:** Dijkstra
**Verdict:** P1

---

## サマリー

| 重篤度 | 件数 |
|---|---|
| 🔴 Critical (P0) | 5 |
| 🟠 Major (P1) | 8 |
| 🟡 Minor | 9 |
| 💡 Suggestion | 6 |
| **合計** | **28** |

---

## 🔴 Critical (P0)

### C-1: pipeline.json の spec_config フィールド — JSON例とフィールド表の不一致 (§3.1)

フィールド定義表には `skip_review`, `self_review_passes`, `queue_file` の3フィールドが記載されているが、直上の JSON 例にはこれら3つが含まれていない。実装者はどちらを信じるべきか判断できない。

**修正案:** JSON例に3フィールドを追加するか、「省略時はデフォルト値を使用」と明記する。

### C-2: batch[].issue が文字列 'spec' — 既存コード find_issue() が int 前提で壊れる (§3.1 / §10.1)

JSON例で `"issue": "spec"` と文字列が指定されているが、既存の `pipeline_io.py` の `find_issue(batch, issue_num: int)` は `i.get("issue") == issue_num` で int 比較している。watchdog.py の `count_reviews()`, `_revise_target_issues()` も `i["issue"]` を `#{i['issue']}` でフォーマットしており、整数前提のコードパスが多数存在する。

**修正案:**
- A: batch を使わない。spec_config 内に spec 固有の状態を持ち、batch は空のままにする。
- B: batch[].issue に整数ダミー値を入れ、spec mode では find_issue を使わない。
- C: find_issue 等を Union[int, str] 対応に改修する（影響範囲大）。

### C-3: SPEC_* ステートが VALID_STATES / VALID_TRANSITIONS に未登録 (§2.2 / §10.1)

config.py の `VALID_STATES` リストには SPEC_REVIEW, SPEC_REVISE 等が存在しない。`VALID_TRANSITIONS` にも遷移規則がない。`cmd_transition()` はこれらでバリデーションしているため、手動遷移が拒否される。`STATE_PHASE_MAP` にも SPEC_* が不在で、flag コマンドが spec mode 中に使えない。

**修正案:** config.py への追加方針を仕様に明記:
1. VALID_STATES に7つの SPEC_*/ISSUE_*/QUEUE_PLAN/SPEC_DONE を追加
2. VALID_TRANSITIONS に遷移規則を追加
3. STATE_PHASE_MAP に "spec" フェーズとしてマッピングを追加

### C-4: セクション番号 §6.3 が重複 (§6.3)

§6.3 が「改訂完了の検知」と「終了判定」の2つのサブセクションに使われている。仕様書のセクション番号体系が壊れており、レビュー指摘でのセクション参照が曖昧になる。

**修正案:** 後者を §6.4 に繰り下げ、以降のセクション番号を調整。

### C-5: YAML パースフォールバックの LLM 呼び出し仕様が未定義 (§5.3 / §6.1)

§5.3 で「YAML ブロックがない場合のフォールバック: LLM（spec_implementer）にパース依頼」とあるが:
1. パース依頼のプロンプトが未定義
2. LLM パース結果のバリデーション方法が未定義
3. LLM パースも失敗した場合のフォールバックが未定義
4. spec_implementer に依頼する = sessions_send が必要で、非同期待ちになる（watchdog の同期処理と矛盾）
5. この LLM 呼び出しのタイムアウトが未定義

**修正案:** パース失敗時は raw_text をそのまま保持し、統合レポートに「パース不能」として含める。

---

## 🟠 Major (P1)

### M-1: should_continue_review の verdict に REJECT が含まれていない (§2.4 / §6.3)

既存 config.py では `VALID_VERDICTS = ["APPROVE", "P0", "P1", "REJECT"]` だが、§6.3 の `should_continue_review` は `r.verdict in ("P0", "P1")` のみチェック。レビュアーが REJECT を返した場合、ループ終了判定で APPROVE 扱いになる。§5.1 のレビュー出力フォーマットでも REJECT が欠落。

**修正案:** spec レビューでは REJECT を廃止し P0 に統一するなら、その旨を明記。

### M-2: レビュー依頼プロンプト内の YAML コードブロックがネスト不可 (§5.1)

spec 本文内に ````yaml` ブロックが含まれる場合、レビュアーの出力 YAML ブロックとの区別が困難。§5.3 のパーサーが誤抽出するリスク。

**修正案:** spec 本文を明示的なデリミタ（`---BEGIN SPEC---` / `---END SPEC---`）で囲む。

### M-3: SPEC_REVIEW タイムアウト 600秒は短すぎる (§10.2)

既存の DESIGN_REVIEW は 3600秒。spec レビューは設計レビューより負荷が大きい。10分ではまず終わらない。

**修正案:** SPEC_REVIEW_TIMEOUT_SEC = 1800 以上に変更。

### M-4: ISSUE_SUGGESTION → ISSUE_PLAN の入力データフロー未定義 (§7.1 / §8.1)

レビュアーから収集した提案がどこに格納されるか、`{reviewer_suggestions}` がどこから読まれるか、パース方法が未定義。

**修正案:** pipeline.json の spec_config に `issue_suggestions: {}` フィールドを追加し格納方針を明記。

### M-5: SPEC_DONE → IDLE 遷移メカニズムが未定義 (§2.2 / §9.2)

M の「確認」をどう検知するか未定義。タイムアウトもなし。遷移コマンドもなし。

**修正案:** `gokrax spec done --pj X` コマンドを追加、または自動 IDLE 遷移。

### M-6: 排他制御の詳細が不十分 (§2.3)

IDLE 状態で `gokrax start` と `gokrax spec start` が同時に呼ばれた場合の競合防止策なし。spec_mode=true の解除タイミング不明。

**修正案:** `gokrax spec start` が atomic に spec_mode=true を設定し、spec_mode=true 時に既存コマンドをエラーにする規則を明記。

### M-7: GitLab Issue 起票の部分失敗リカバリが未定義 (§8.1)

10件中5件目で glab タイムアウトした場合のリカバリ策なし。

**修正案:** 起票済み Issue 番号を逐次 pipeline.json に記録。リトライ時は記録済み番号をスキップ。

### M-8: セルフレビューの判断主体が改訂者自身 (§6.2)

同一モデル・同一コンテキストでは同じ見落としを繰り返す可能性が高い。

**修正案:** セルフレビューのうち1回は別エージェントに依頼する方式を検討。

---

## 🟡 Minor

### m-1: --skip-review --no-issue の検出ロジック実装箇所が未指定 (§4.2)

### m-2: SPEC_QUEUE_FILE と既存 QUEUE_FILE のパス重複 (§3.2)

### m-3: 重複検出の「類似タイトル」マッチングアルゴリズムが未定義 (§5.4)

### m-4: レビュー保存ファイル名のハイフン区切りがパース曖昧 (§12.1)

**修正案:** `{date}_{reviewer}_{spec_name}_rev{N}.md` に統一。

### m-5: SPEC_APPROVED → ISSUE_SUGGESTION 自動遷移に M の承認機会がない (§10.1)

### m-6: rev2以降のプロンプトに spec 全文が含まれトークン効率が悪い (§5.1)

### m-7: DAG で S-6 の依存が S-4 だが論理的に不自然 (§13.3)

**修正案:** S-6 の依存を S-1 (+ S-3) に変更。

### m-8: --model オプションの用途が曖昧 (§4.2)

sessions_send ではモデル指定不可のため、削除を検討。

### m-9: SPEC_REVISE 完了通知の commit ハッシュが空になりうる (§11.1)

---

## 💡 Suggestion

### s-1: §1.2「上記 1〜9」→「上記 2〜9」が正確（ステップ1はスコープ外）

### s-2: MergedReviewReport の型定義がない (§5.4)

### s-3: new_rev が文字列型 "4A" だが current_rev は int 型 (§6.1)

### s-4: S-4 の150行見積もりは過小。200〜250行が妥当 (§13.2)

### s-5: 早期終了オプションの全組み合わせ真理値表があると親切 (§2.5)

### s-6: check_spec_mode のエラーハンドリング方針が未記載 (§10.1)

---

## 総評

致命的な設計矛盾（C-2: batch の型不整合、C-3: 状態定義の欠落）が実装を阻む。C-5 の LLM フォールバック未定義も実装者が最も判断に迷う箇所。

良い点: 既存 watchdog の Double-Checked Locking パターンとの整合を意識した設計、プロンプトテンプレートの具体性、TrajOpt での実体験に基づく MAX_CYCLES=5 の根拠は説得力がある。

ただし「既存コードとの接合部」を甘く見ている。config.py, pipeline_io.py, watchdog.py との具体的な統合ポイント（どの関数に何を追加するか、既存の型制約をどう緩和するか）が不足。C-2 と C-3 を解決しないまま実装に入ると、Issue S-1 の段階で設計判断が必要になり手戻りが発生する。

rev2 では最低限 C-1〜C-5 と M-1 の解消を求める。
