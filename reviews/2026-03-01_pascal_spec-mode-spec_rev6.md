# DevBar Spec Mode rev6 — レビュー結果 (Pascal)

**レビュアー:** Pascal (g-reviewer)
**対象:** docs/spec-mode-spec_rev6.md (rev6)

## 総評
Magnifique. 状態機械の各歯車が完璧に噛み合い、数理的な美しさを備えた仕様となった。境界条件の穴も塞がれ、堅牢性は申し分ない。
しかし「やりすぎレビュー」の要請に応え、実装者が躓く可能性のある「内部状態の語彙マッピング」の微細な隙間と、例外復帰時の挙動の曖昧さを指摘する。論理の破綻ではないが、実装の解釈ブレを防ぐための最終仕上げだ。

```yaml
verdict: P1
items:
  - id: P-1
    severity: major
    section: "§5.3, §5.5"
    title: "current_reviews の status: parse_failed 設定処理の明記漏れ"
    description: "§5.3の `should_continue_review` では `parsed_fail` を `v['status'] == 'parse_failed'` で抽出し、`parsed_ok` を `v['status'] == 'received'` として判定している。しかし、§5.5のパース処理（ステップ3）には「parse_success=False とし、raw_text を保持」としか記載されておらず、`status` を `'parse_failed'` に設定・上書きする指示がない。実装者が `status='received'` のまま格納した場合、パース失敗データが `parsed_ok` に混入し、直後の `v.get('verdict')` の評価でシステムがクラッシュする。"
    suggestion: "§5.5のステップ3に「不正値やパース失敗時は、`parse_success=False` とするとともに、`status='parse_failed'` として `current_reviews` に格納する」旨を明確に追記せよ。"
  - id: P-2
    severity: minor
    section: "§6.2, §4.6"
    title: "パス2リトライ超過からの resume 後の復帰地点の明確化"
    description: "§6.2でパス2リトライ超過時に `SPEC_PAUSED (paused_from='SPEC_REVISE')` となる。§4.6の resume 実行により `retry_counts` がリセットされて `SPEC_REVISE` に戻る仕様は正しい。しかし、REVISE の「どこ」から再開されるかが暗黙的である（改訂要求プロンプトの再送信から始まるのが自然だが、セルフレビューだけを再実行しようとすると状態が噛み合わない）。"
    suggestion: "実装時の解釈ブレを防ぐため、§6.2 または §4.6 に「SPEC_REVISE に resume した場合、再度 implementer への改訂要求（§6.1）からプロセスを再始動する」という一文を注記しておくのが安全だ。"
```