# DevBar Spec Mode rev5 — レビュー結果 (Pascal)

**レビュアー:** Pascal (g-reviewer)
**対象:** docs/spec-mode-spec_rev5.md (rev5)

## 総評
Magnifique. 状態遷移の論理はほぼ完成の域に達した。`extend` で `current_reviews` を維持したまま `SPEC_REVISE` に直行する設計は、無駄な API コールを省きつつデータのライフサイクルを美しく保っている。
だが、状態の境界を跨ぐデータの「受け渡し」に一つだけ重大な穴が残っている。プロンプトを生成するための情報が虚空に消えているのだ。実装に入れば即座に KeyError や None 参照で落ちるだろう。以下に指摘する。

```yaml
verdict: P1
items:
  - id: P-1
    severity: major
    section: "§3.1, §5.1, §6.3"
    title: "改訂完了時の changelog 情報の永続化漏れによるプロンプト生成失敗"
    description: "§5.1のrev2以降のプロンプトでは `{changelog_summary}` や `{added_lines}` などを埋め込む仕様となっている。しかし、§6.3の改訂完了時（implementerからのYAML報告パース時）にこれらの情報を `spec_config` に保存するステップがなく、§3.1のJSONスキーマにも該当フィールドが存在しない。状態が SPEC_REVISE から SPEC_REVIEW に遷移した直後、watchdog がレビュー依頼のプロンプトを構築しようとしても、前ラウンドの改訂内容データ（changes オブジェクト）が揮発しており参照できない。"
    suggestion: "§3.1の `spec_config` に `last_changes` などのフィールドを追加し、§6.3のステップ3に「implementerからの `changes` オブジェクトを `last_changes` に保存する」処理を明記せよ。"
  - id: P-2
    severity: minor
    section: "§4.6, §7"
    title: "resume時のISSUE_SUGGESTIONタイムアウト再計算漏れ"
    description: "§4.6の `resume` コマンド仕様では、ステップ4で `review_requests` の pending エントリのタイムアウトを再計算している。しかし、`ISSUE_SUGGESTION` フェーズにおけるレビュアーへの問い合わせ状況とタイムアウトは `review_requests` ではなく `issue_suggestions` 側に格納されると推測される（§7）。したがって `paused_from == 'ISSUE_SUGGESTION'` で復帰した場合、タイムアウトの再計算が適用されず、直後にタイムアウトが即時暴発するリスクがある。"
    suggestion: "§4.6のステップ4に「`paused_from` が `ISSUE_SUGGESTION` の場合は `issue_suggestions` 内のペンディングエントリのタイムアウトも再計算する」旨を明記するか、タイムアウト管理を `review_requests` に統合する設計に統一せよ。"
```