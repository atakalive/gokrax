# DevBar Spec Mode rev2 — レビュー結果 (Pascal)

**レビュアー:** Pascal (g-reviewer)
**対象:** /mnt/s/wsl/work/project/devbar/docs/spec-mode-spec_rev2.md (rev2)

## 総評
前回の致命的な欠陥（空集合の自動承認バグ、無限ループの温床等）は塞がれた。悪くない。しかし、状態機械を複雑にした代償として、「異常系からの復帰時の時間的連続性」や「エラー判定の論理矛盾」が新たに顔を出している。特に全員パース失敗時の扱いは、ドキュメントのセクション間で完全に矛盾しており、実装時に間違いなく破綻する。さらなる厳密な推敲を要求する。

```yaml
verdict: P0
items:
  - id: P-1
    severity: critical
    section: "§5.3, §6.4"
    title: "全員パース失敗時の遷移矛盾（PAUSED vs REVIEW_FAILED）"
    description: "§5.3では「全員パース失敗時は SPEC_PAUSED に遷移する」と規定されているが、§6.4の `should_continue_review` では `parse_success == True` のものを抽出した結果 `len(valid) < MIN_VALID_REVIEWS` であれば `SPEC_REVIEW_FAILED` を返すと定義されている。全員パース失敗時は後者の条件にも合致するため、要求する状態遷移が衝突している。これは決定性の喪失である。"
    suggestion: "§6.4の終了判定ロジック内で、「誰も応答せずタイムアウトした（REVIEW_FAILED）」と「全員応答したが全件パース失敗した（PAUSED）」を明確に区別し、条件分岐の排他性を保証せよ。"
  - id: P-2
    severity: major
    section: "§3.1, §10.1"
    title: "レビュー結果の永続化場所とデータフローの欠落"
    description: "既存の `batch[]` 機構を廃止したことで、取得したレビュー本文やパース結果（`SpecReviewResult` 等）を `pipeline.json` のどこに永続化するかが未定義になっている。§10.1の引数 `review_data` がどこから供給されるのかも不明確。再起動時やPAUSEDからの復帰時にデータを喪失するリスクが高い。"
    suggestion: "§3.1のJSON定義に、現在進行中のラウンドのパース結果を保持するフィールド（例: `current_reviews`）を明記し、データの出所を確定させよ。"
  - id: P-3
    severity: major
    section: "§2.2, §10.2"
    title: "resume 時のタイムアウト即死問題"
    description: "`paused_from` を記録して `devbar spec resume` で前状態に復帰できる仕様だが、タイマー（`sent_at` や `timeout_at`）の補正について言及がない。待機中に PAUSED になり、数時間後に人間が resume した場合、復帰直後に過去の `timeout_at` に抵触し、即座にタイムアウト処理が暴発する。"
    suggestion: "resume時に `timeout_at` を現在時刻ベースで再計算（リセット）するか、PAUSED 中の経過時間をオフセットとして補正するロジックを仕様に組み込め。"
  - id: P-4
    severity: major
    section: "§8.1"
    title: "GitLab Issue起票時のレースコンディション"
    description: "`glab issue create` の成功直後、`created_issues[]` へのローカル書き込み前にプロセスが死んだ場合、次回リトライ時に同じIssueが重複して起票される。ローカルの配列のみに依存した冪等性管理は分散系において極めて脆弱だ。"
    suggestion: "起票前に `glab issue list --search` 等でリモート側の既存Issueを照会するか、起票時のタイトル/ラベルに一意なリビジョンIDを含めて重複チェックを行う手順を追加せよ。"
  - id: P-5
    severity: minor
    section: "§2.3"
    title: "SPEC_PAUSED の VALID_TRANSITIONS 網羅性不足"
    description: "`SPEC_TRANSITIONS` の定義において、`SPEC_PAUSED` の遷移先に `SPEC_APPROVED` や `SPEC_DONE` などの待機状態が含まれていない。これらの状態で通知等のマイナー処理が失敗して PAUSED に落ちた場合、resume して元の状態に戻ろうとすると VALID_TRANSITIONS 違反でクラッシュする。"
    suggestion: "`SPEC_PAUSED` の遷移先リストにすべての `SPEC_*` 状態を含めるか、復帰時の特別扱い（バリデーションバイパス）を明記せよ。"
  - id: P-6
    severity: minor
    section: "§6.3"
    title: "revise_count のインクリメント処理欠落"
    description: "改訂完了後の更新処理において、`last_commit, current_rev, rev_index` の更新は明記されているが、§6.4 の終了判定の要である `revise_count` のインクリメントについて言及がない。実装者がこれを見落とせば無限ループのバグを生む。"
    suggestion: "§6.3 のステップ3に `revise_count のインクリメント` を明記せよ。"
```