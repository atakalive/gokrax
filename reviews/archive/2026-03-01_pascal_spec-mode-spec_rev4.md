# DevBar Spec Mode rev4 — レビュー結果 (Pascal)

**レビュアー:** Pascal (g-reviewer)
**対象:** docs/spec-mode-spec_rev4.md (rev4)

## 総評
Magnifique. 状態遷移の整理とDCLパターンの適用により、堅牢な数理モデルへと昇華された。変数のスコープや判定ロジックの単一ソース化も美しい。
しかし、STALLED状態からの復帰（`extend`）の設計において、状態機械の意味論と物理的な処理（LLMのトークン消費）の間に非効率なギャップが残っている。論理的破綻ではないが、美しくない。微修正を提案する。

```yaml
verdict: P1
items:
  - id: P-1
    severity: major
    section: "§4.7, §2.1, §2.3"
    title: "extend時のSPEC_REVIEW遷移による無駄な空転ラウンド"
    description: "§4.7で `extend` 実行時に `SPEC_REVIEW` へ遷移し `current_reviews` をクリアする仕様となっている。STALLED状態とは「P1指摘（修正要求）が存在するが、改訂上限に達したため修正に進めない」状態である。これをREVIEWに戻すと、全く変更されていない仕様書を再度レビュアーに読ませることになり、同じP1指摘を得るためだけに無駄なAPIコールと時間を消費する。上限を拡張したのなら、既にある指摘（current_reviews）を元に直ちに修正へ進むべきだ。"
    suggestion: "§4.7の `extend` の遷移先を `SPEC_REVISE` に変更し、`current_reviews` はクリアせずに維持せよ。それに伴い、§2.1と§2.3の遷移規則で `SPEC_STALLED` から `SPEC_REVISE` へのパスを許可せよ。"
  - id: P-2
    severity: minor
    section: "§4.3, §12.2"
    title: "approve --force 時の current_reviews アーカイブ漏れ"
    description: "STALLEDから `approve --force` で強制承認した場合、SPEC_APPROVEDに直行する。通常は§6.3のREVISE完了時に `current_reviews` から要約が生成され `review_history` にアーカイブされてクリアされるが、強制承認ルートではこの処理がバイパスされるため、最終ラウンドのレビュー結果が履歴に残らない。"
    suggestion: "§4.3の `approve --force` の動作に「`current_reviews` から要約を生成し `review_history` に追加・クリアする（§6.3と同等）」処理を明記せよ。"
  - id: P-3
    severity: suggestion
    section: "§10.1"
    title: "DCLにおける divergent transition の1tick遅延"
    description: "`_apply_spec_action` における `action2.next_state == action.next_state` の厳格な一致確認は安全だが、例えばロック待ちの間に遅延していた最後のレビュアーが「パース失敗」で応答した場合、当初の予測（TIMEOUTによるFAILED）と再計算結果（パース失敗によるPAUSED）が食い違う。この時、遷移がスキップされ、次回のwatchdog tickまでPAUSED遷移が遅延する。実害はないが、`action2.next_state` が非Noneであればその最新の計算結果を信じて遷移させる方が応答性が良い。"
    suggestion: "`action2.next_state` が存在すれば（必ずしも `action.next_state` と一致しなくても）その状態へ遷移させるよう緩和することを検討せよ。"
```