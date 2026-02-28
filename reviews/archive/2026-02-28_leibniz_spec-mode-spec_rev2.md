# DevBar Spec Mode — 仕様書レビュー（Leibniz, rev2 / やりすぎ版）

対象: `/mnt/s/wsl/work/project/devbar/docs/spec-mode-spec_rev2.md` (rev2, 767行)
差分（rev1→rev2）: +478 / -532 行（概算）

rev1の私の主要懸念（型衝突、送信路、決定性、per-reviewer timeout、純粋関数化、ID体系、エラー通知など）は概ね仕様上は反映されています。だが、**現行 devbar の watchdog 実装上の前提（batch 非空）と正面衝突**しており、このままだと spec mode は一歩も動きません。ここが P0。

---

```yaml
verdict: P0
items:
  - id: C-1
    severity: critical
    section: "§3.1, watchdog.py(process関数)"
    title: "batch未使用仕様と現行watchdogの早期return条件が衝突し、spec modeが進行不能"
    description: "仕様は『spec mode は既存 batch[] を使用しない。batch: []』を採用している（§3.1）。しかし現行 `watchdog.py` の `process()` は `state != \"DONE\" and not batch` の場合に WARNING を出して `return` する（実処理を一切しない）。spec state は DONE ではないため、batch を空にすると watchdog が spec mode を永遠に処理しない＝状態遷移も通知も起きない。これは仕様・実装整合性の破綻であり、P0。"
    suggestion: "watchdog 側に『spec state のときは batch 空を許容し、spec_config を主データとして処理する』分岐を追加せよ。最小修正: `if state != 'DONE' and not batch and state not in SPEC_STATES: ... return`。併せて `check_transition_spec` 呼び出し経路を `process()` に実装する必要がある（仕様は純粋関数を定義しただけで、既存実装へ接続する導線がまだ不足）。"

  - id: C-2
    severity: critical
    section: "§2.3, §10.1"
    title: "SPEC_TRANSITIONS/STATE_PHASE_MAP更新は“仕様内コード片”であり、実装適用箇所が未確定（実装漏れリスク）"
    description: "§2.3で `VALID_STATES = VALID_STATES + SPEC_STATES` 等のコード片を示しているが、現行実装では `config.py` が単一定義ソースで、`devbar.py`/`watchdog.py` は import 時点の定数を前提に動く。仕様が『どのファイルにどう反映するか』を（少なくとも実装計画の範囲で）断言していないと、更新漏れが起きやすい。特に `STATE_PHASE_MAP` は `devbar flag` の phase 決定に直結する。"
    suggestion: "`config.py` に SPEC_STATES/SPEC_TRANSITIONS を実装することを“必須要件”として明文化し、ユニットテストで `SPEC_STATES ⊂ VALID_STATES` と `∀(s→t)∈SPEC_TRANSITIONS: t∈VALID_TRANSITIONS[s]` を検査せよ。"

  - id: C-3
    severity: critical
    section: "§10.1"
    title: "check_transition_specのI/Oが既存watchdogのTransitionActionと整合しない（統合設計が未完）"
    description: "現行 watchdog は `TransitionAction(new_state, impl_msg, send_review, reset_reviewers, ...)` を中心に、DCL（lock内再計算）→ lock外通知、という実装が完成している。一方、仕様の `SpecTransitionAction(next_state, send_to, notify, save_data, error)` は別インターフェースで、現行パイプライン更新・通知系（notify_reviewers/notify_discord/notify_implementer）とも噛み合わない。『純粋関数化』自体は良いが、既存実装にどうアダプトするかが仕様段階で曖昧で、実装で破綻しやすい。"
    suggestion: "SpecTransitionAction を TransitionAction に寄せる（同じフィールド名・概念に揃える）か、watchdog.process 内に spec専用のDCLブロックを別建てで実装する、と仕様で断言せよ（“どちらでも”は不可）。"

  - id: C-4
    severity: critical
    section: "§2.1, §2.4, §6.4"
    title: "P1以上でループ継続 + MIN_VALID_REVIEWS=1 の組合せが“常時ループ/暴走”を招く"
    description: "spec mode は『P1以上でループ継続』を採用（§6.4）。一方、`MIN_VALID_REVIEWS=1` なので、1人のレビュアーが P1 を返し続けるだけで revise→review ループが継続し得る。さらにタイムアウト時は“応答済みのみで判定”なので、常に1件だけで回り続ける危険がある（作業量が大きく、収束が遅い spec では特に）。MAX_CYCLES で STALLED に落とすとしても、そこまでに無駄な反復が積み上がる。"
    suggestion: "(A) MIN_VALID_REVIEWS を review_mode の min_reviews に追随（例: `min(2, effective_reviewers)`）させる、または (B) P1 ループ継続は『有効レビューが一定数以上』のときのみ、といった条件を追加せよ。"

  - id: C-5
    severity: critical
    section: "§2.1, §2.2 (SPEC_STALLED)"
    title: "SPEC_STALLED→SPEC_REVIEWの意味論が未定義（revise_count/maxの扱い、再開条件）"
    description: "STALLED は『MAX_CYCLES到達 & P1残存』として導入されているが、そこから `SPEC_REVIEW (追加レビュー)` に遷移できる（§2.1/§2.2）。このとき `revise_count` をどう扱うのか（既にmax到達なら次のREVISEで即STALLEDに戻るのか、revise_countをリセットするのか）、追加レビューで何が解決されるのかが曖昧で、状態機械として“抜け穴”になっている。"
    suggestion: "STALLED から REVIEW へ戻すなら、(1) revise_count をリセットするのか、(2) max_revise_cycles を増やす（force extend）等の操作を伴うのか、(3) 追加レビュアー/追加要件を変えるのか、を仕様で固定せよ。"

  - id: C-6
    severity: critical
    section: "§4.2(動作), §5.2, §10.2"
    title: "retry_countの粒度（状態全体 vs reviewer別）が曖昧で、MAX_SPEC_RETRIESが誤って消費され得る"
    description: "rev2は per-reviewer timeout 構造を追加した一方で、`retry_count` と `MAX_SPEC_RETRIES` の適用粒度が『全状態共通』なのか『状態ごと』なのかが読み取りにくい。特に SPEC_REVIEW は reviewerごとに timeout を持つため、“誰か1人が遅い”だけで retry_count が増える実装になると、容易に PAUSED へ落ちる。"
    suggestion: "`retry_count` を `retry_count_by_state: {state:int}` にするか、少なくとも「どのイベントで+1するか」を箇条書きで厳密化せよ（reviewer timeout は+0、全員送信失敗のみ+1、等）。"

  - id: M-1
    severity: major
    section: "§5.3 (aliases)"
    title: "VERDICT_ALIASES/SEVERITY_ALIASESが混線しており、誤正規化の温床"
    description: "verdict alias に `critical->P0`, `major->P1` を入れているが、これらは本来 severity 側の語彙であり、YAMLの `verdict:` に誤って `critical` が入ってもP0になる、という“救済”になっている。救済自体は可だが、誤入力を静かに通すと集計・終了判定が歪む。さらに severity alias に `P0->critical`, `P1->major` を入れているため、`severity: P0` も通る。結果として、仕様が求める厳格さ（決定性）と逆方向。"
    suggestion: "alias は『許容する誤り』を最小化し、許容する場合も `warnings[]` に記録して PAUSED 条件にし得る等、静かに正規化しない方針を入れよ。最低限、verdictにseverity語彙が来た場合は parse_success=False として止める設計が堅い。"

  - id: M-2
    severity: major
    section: "§12.1"
    title: "reviews/をmainへ直接commitする運用が危険（競合・肥大化・秘密情報）"
    description: "rev2は『repo内 reviews/ をバージョン管理対象、mainへ直接commit』を採用した（§12.1）。複数レビュアーが並行で書くと競合しやすく、レビュー原文（思考過程・内部情報）がrepoに残り続ける。運用・情報管理上のコストが高い。"
    suggestion: "(A) repo外（pipelines/log）へ保存し、必要ならリンクのみ残す、または (B) ブランチ運用（レビュー成果物は別ブランチ/別リポジトリ）を仕様化せよ。少なくとも“誰がcommitするか”（implementerかdevbarか）とコミットメッセージ規約を固定せよ。"

  - id: M-3
    severity: major
    section: "§4.3 (approve)"
    title: "approve --forceの監査ログがreview_historyに入る設計だが、どのrevに紐づくか曖昧"
    description: "監査ログとして `forced/actor/remaining_issues` を review_history に入れるのは良い。しかし force が発生する時点の rev（current_rev/rev_index）と一致して記録される保証、また STALLED から force した場合に“残存P1一覧”をどう算出するかが曖昧。"
    suggestion: "force 実行時に `force_event: {at, actor, from_state, rev, rev_index, remaining_p1_items:[normalized_id]}` を別に記録する方が不変。review_history に混ぜるなら、どのエントリに追記するかを1通りに定めよ。"

  - id: m-1
    severity: minor
    section: "§5.1"
    title: "レビュー依頼プロンプトに『YAMLブロックは1つだけ』制約が欠ける"
    description: "本依頼（M指示）ではYAMLブロックを1つに制限しているが、仕様のテンプレートにはその制約が明示されていない。複数ブロック出力はパーサの失敗原因になりやすい。"
    suggestion: "テンプレートのレビュー指示に『YAMLブロックは1つだけ』を追記せよ。"

  - id: s-1
    severity: suggestion
    section: "§5.3"
    title: "YAML抽出はregexより先に『先頭からの厳格パース』に寄せた方がよい"
    description: "rev2でLLMフォールバックを廃止したのは正しい。残る破綻点はregex抽出。応答本文に```yamlが出るだけで壊れる可能性がある。"
    suggestion: "“メッセージ全体がYAML”を要求し、先頭からパース（失敗ならPAUSED）にすれば、抽出処理そのものが不要になる。"
```

---

## rev1指摘の反映確認（短評）

- **C-1（batch型衝突）**: 仕様上は分離できている（§3.1）。ただし現行watchdogの早期returnと衝突するため、実装整合は未達（C-1）。
- **C-2（送信路）**: send_to_agent/send_to_agent_queued の使い分けが明文化されている（良）。
- **C-8（LLMパース）**: 廃止（良）。ただし alias が過剰に寛容で“静かに通す”点が新しいリスク（M-1）。
- **C-10（per-reviewer timeout）**: review_requests導入（良）。ただし retry_countの消費規則が曖昧（C-6）。
- **C-11（保存先運用）**: 仕様化はしたが、repo main直commitは別の危険を導入（M-2）。
