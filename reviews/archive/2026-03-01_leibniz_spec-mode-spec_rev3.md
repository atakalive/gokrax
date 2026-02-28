# DevBar Spec Mode — 仕様書レビュー（Leibniz, rev3 / やりすぎ版）

対象: `docs/spec-mode-spec_rev3.md` (rev3, 1092行)
commit: rev2=`d7ea4d2` → rev3=`50a375b`

rev2で私が出したP0（spec modeがwatchdogで一歩も進まない）は、仕様上は修正が入っている（§5.1 / §10.1のbatch空ガード除外）。ただし、**仕様内の遷移定義（§2.4）と後段の詳細手順（§5.2, §6.4）がまだ矛盾**しており、状態機械として破綻し得る点が残っています。

---

```yaml
verdict: P0
items:
  - id: C-1
    severity: critical
    section: "§2.4, §5.2"
    title: "SPEC_REVIEW完了後の遷移先が矛盾（P1以上あり & MAX未到達 → SPEC_REVIEW と書かれている）"
    description: "§2.4の判定表 #6 が『P1以上あり & MAX未到達 → SPEC_REVIEW（ループ継続）』となっているが、状態遷移図（§2.1）および詳細手順（§5.2 step5/6）は『SPEC_REVIEW完了後はまず SPEC_REVISE へ進む』を前提にしている。SPEC_REVIEW→SPEC_REVIEW 直接遷移は、改訂（REVISE）を挟まない再レビューを意味し、revise_countやlast_commit更新とも整合しない。ここが曖昧だと実装は必ず分岐し、デッドループ/無改訂レビュー再送などの事故になる。"
    suggestion: "§2.4 #6 を `→ SPEC_REVISE` に修正し、合わせて §5.2 の『revise_count>0 のとき should_continue_review』の呼び出しタイミング（REVIEW後かREVISE後か）を1通りに固定せよ。"

  - id: C-2
    severity: critical
    section: "§10.1 (process統合の擬似コード)"
    title: "spec専用DCLブロックが“再読み込み/再計算”を満たしておらず、競合で二重遷移し得る"
    description: "§10.1のprocess統合は『DCL: 再読み込み→再計算→更新』と書きつつ、擬似コード内では lock 内で pipeline/spec_config を再ロードしていない（同一変数のまま action2 を計算している）。現行devbar実装の要点（lock待ち中に状態が変わったらスキップ）を満たしておらず、並行実行で二重送信・二重遷移が起こり得る。"
    suggestion: "仕様段階でも、DCLの手順を厳密化せよ（lock内で pipeline を再読込し `state0` が一致する場合のみ更新、のように）。既存watchdogの `state0` 比較と同じパターンに合わせるのが最小差分。"

  - id: C-3
    severity: critical
    section: "§2.3 (VALID_TRANSITIONSマージ方式)"
    title: "list(set(...)) による遷移先の非決定順序が導入され、差分/テスト/デバッグが不安定になる"
    description: "`VALID_TRANSITIONS[state] = list(set(existing + targets))` は順序が非決定（Pythonのset順）で、同一内容でも出力順が揺れる。遷移候補を表示する/ログに残す/テストで比較する場面でフレーク要因になる。仕様が“決定性最優先”を掲げている（§5.3）以上、ここで非決定性を入れるのは設計不整合。"
    suggestion: "集合化するなら `sorted(set(...))` で順序を固定、あるいは `existing` を保ちつつ `targets` を後ろに追加（重複のみ除去）する安定マージにせよ。"

  - id: M-1
    severity: major
    section: "§5.2, §6.4"
    title: "初回レビュー(revise_count=0)を無条件REVISEにする方針が、終了条件表（§2.4）と二重化しており実装が割れやすい"
    description: "§5.2で『revise_count=0は無条件REVISE』を導入したのは理解できるが、§2.4の終了条件表にも同等の分岐が存在し、さらに §6.4 に should_continue_review がある。判定ロジックが複数箇所に分散すると、改訂のたびに矛盾が再発する（rev2→rev3でも既に§2.4/#6の矛盾が出ている）。"
    suggestion: "判定ロジックの単一ソースを仕様内で決めよ（例: SPEC_REVIEW後の判定は§5.2のみ、§2.4は要約だけに落とす／あるいは§2.4を正として§5.2は参照にする）。"

  - id: M-2
    severity: major
    section: "§12.1"
    title: "レビュー原文をrepo外にするなら、pipelines_dirの正確なパス/保持期間/権限を仕様化すべき"
    description: "rev3で『原文はpipelines_dir/spec-reviewsへ、repoにはサマリーのみ』に戻したのは運用上妥当。ただし pipelines_dir の実体（現行configでは `PIPELINES_DIR` と `QUEUE_FILE` の上書き等がある）や、ログ保持期間・個人情報/秘匿の扱いが仕様に無い。ここが未定義だと“どこに保存されたか分からない”運用事故になる。"
    suggestion: "pipelines_dir を絶対パスとして記録する（pipeline.jsonにも `pipelines_dir` を入れる等）、保持期間（例: 30日）、アクセス権（chmod）、ローテーション方針を最低限決めよ。"

  - id: m-1
    severity: minor
    section: "§4.2, §2.5"
    title: "--skip-review時のauto_continue暗黙trueは良いが、CLI→pipeline写像表に“強制上書き”規則が無い"
    description: "§4.2 step5 と §2.5 で『--skip-review時はauto_continue暗黙true』を述べているが、§3.3の写像表は単純写像で、上書き規則（優先順位）が明文化されていない。実装で解釈が割れる。"
    suggestion: "オプション優先順位（例: skip_review⇒auto_continue=true強制、review_only⇒auto_continueは無視、など）を仕様の表として追加せよ。"

  - id: s-1
    severity: suggestion
    section: "§5.3"
    title: "YAML抽出は『最初の1ブロック』より『本文=YAMLのみ』要求の方が堅い（決定性をさらに上げられる）"
    description: "rev3で『YAMLブロックは1つだけ』が入ったのは良い。それでも正規表現抽出は境界条件が残る。"
    suggestion: "レビュアー指示を『返答はYAMLのみ（他のテキスト禁止）』に変更し、抽出を不要にすると、PAUSEDの発生率が下がる。"
```
