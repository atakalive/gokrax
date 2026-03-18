# gokrax Spec Mode — レビュー結果 (Pascal)

**レビュアー:** Pascal (g-reviewer)
**対象:** docs/spec-mode-spec_rev1.md (rev1)
**総評:** 
C'est absurde. ステートマシンの境界条件に対する配慮が致命的に欠落している。特に空集合に対する評価論理のバグは、無価値な仕様書を自動承認する「承認ロンダリング」回路として機能する。これをこのまま実装すれば、パイプラインは間違いなく崩壊するだろう。抜本的なロジックの修正を要求する。

```yaml
verdict: P0
items:
  - id: C-1
    severity: critical
    section: "§2.2, §2.4, §6.3"
    title: "タイムアウトによる全レビュアー欠損時の自動承認（空集合の論理バグ）"
    description: "§2.2で「全レビュアー回収 or grace_period 満了」でSPEC_REVISEへ遷移する。全員がタイムアウトした場合、reviewsリストは空になる。§6.3の `any(r.verdict in ('P0', 'P1') for r in reviews)` は空集合に対してFalseを返す。結果として、誰もレビューしていない仕様書が「P1以上なし」と判定され、SPEC_APPROVEDへ直行する。これは安全装置の完全なバイパスである。"
    suggestion: "最低限の有効レビュー数（例: min_approvals=1）を定義し、それに満たない場合は SPEC_REVIEW_FAILED などの例外状態へ遷移させるロジックを追加せよ。"
  - id: C-2
    severity: critical
    section: "§2.4"
    title: "最大サイクル到達時の強制承認という暴挙"
    description: "MAX_SPEC_REVISE_CYCLES に到達した場合、P0/P1の指摘が残っていてもSPEC_APPROVEDに遷移すると読める（警告付きであっても）。これは「時間をかければ欠陥仕様でも通る」という破綻した論理である。後続のISSUE_PLANが破綻した仕様を元に動くことになる。"
    suggestion: "最大サイクル到達時にP1以上が残存している場合は、SPEC_APPROVEDではなく NEEDS_HUMAN_INTERVENTION または SPEC_REVISE_FAILED へ遷移させ、Mの明示的な介入を必須とせよ。"
  - id: C-3
    severity: major
    section: "§10.2"
    title: "催促ループによる無限デッドロック"
    description: "SPEC_REVISE, ISSUE_PLAN, QUEUE_PLANにおけるタイムアウト時の動作が「催促 | Mに通知」のみであり、状態遷移を伴わない。エージェントがクラッシュし続ける、あるいは一時的なAPI障害が発生した場合、状態が永遠にスタックする。"
    suggestion: "最大再試行回数（max_retries）を定義し、超過時はパイプラインを一時停止（PAUSED）状態にフォールバックさせる機構を組み込め。"
  - id: C-4
    severity: major
    section: "§5.1, §5.3, §6.3"
    title: "列挙型（Enum）の語彙不整合によるパース失敗リスク"
    description: "§5.1のプロンプトでは verdict を `APPROVE | P0 | P1` と指定しつつ、severity を `critical | major | minor | suggestion` と指定している。一方で本文のテキストでは `🔴 Critical (P0)` と表記がブレている。LLMは出力時に `verdict: critical` や `severity: P1` と混同する確率が極めて高い。§6.3の条件式とパース処理が壊れる原因となる。"
    suggestion: "LLMへの指示における列挙型の語彙を完全に統一せよ。例えば verdict も severity も `P0 | P1 | MINOR | SUGGESTION` の形式に揃えるか、パーサー側に堅牢なエイリアスマッピング（critical -> P0 等）を実装せよ。"
  - id: C-5
    severity: major
    section: "§4.2, §7.1"
    title: "スキップ時のレビュアー未定義問題"
    description: "`--skip-review` を指定した場合、SPEC_APPROVEDに直行し、ISSUE_SUGGESTIONへ遷移する。しかし、SPEC_REVIEWフェーズを経ていないため、誰に提案を求めるべきか（レビュアーリスト）が初期化されていない可能性がある。"
    suggestion: "初期化（start）時に、レビューをスキップする場合であっても `--review-mode` に基づき pipeline.json へ対象レビュアーリストを確実に書き込む設計を明記せよ。"
  - id: C-6
    severity: minor
    section: "§12.1"
    title: "同一日・同一リビジョンのファイル上書き競合"
    description: "出力ファイル名が `{date}_{reviewer}-{spec_name}-rev{N}.md` となっている。dateが YYYY-MM-DD の場合、同日に同じリビジョンでリトライ処理等が走った際にファイルがサイレントに上書きされる。"
    suggestion: "dateをISO8601フォーマット（秒まで含む）にするか、実行ID（run_id）をファイル名に含めるべきだ。"
