# DevBar Spec Mode — 仕様書レビュー（Leibniz, rev5 / やりすぎ版）

対象: `docs/spec-mode-spec_rev5.md` (rev5, 1185行)
commit: rev4=`45dca2b` → rev5=`2755ac2`

rev4で私が出した主要なP0（DCLの誤条件、timeout責務、received定義、pipelines_dir表記、期限切れ削除主体など）は概ね反映されています。残る致命点は **pipelines_dir の権限指定がディレクトリに適用されると即死**する点です。

---

```yaml
verdict: P0
items:
  - id: C-1
    severity: critical
    section: "§12.1（レビュー原文: pipelines_dir）"
    title: "pipelines_dir の権限0600はディレクトリに適用するとアクセス不能（実装即死）"
    description: "§12.1で『権限: 0600（owner read/write only）』とあるが、これはファイルには妥当でもディレクトリに適用すると execute ビットが落ち、ls/open/create ができず実用不能になる。仕様は pipelines_dir 自体（ディレクトリ）を示しているため、実装者が `chmod 0600 pipelines_dir` と解釈すると spec mode が必ず壊れる。"
    suggestion: "ディレクトリは原則0700（または0750）、ファイルは0600と明確に分けて仕様化せよ（例: `spec-reviews/` dir=0700、配下md=0600）。また“権限を設定する主体（watchdog）”と“設定対象（dir vs file）”を明記せよ。"

  - id: C-2
    severity: critical
    section: "§5.1（process()ガード）, §10.1（_apply_spec_action）"
    title: "ガード条件がdiscord_notifyのみのアクションを適用できず、通知専用アクションが死ぬ"
    description: "rev5で `next_state=None` のアクション適用を可能にしたが、process()側のガードは `next_state or pipeline_updates or send_to`（§5.1）、_apply_spec_action側の applied 判定も同様（§10.1）。この条件だと `discord_notify` のみ返すアクションは適用されず、通知が欠落する。現仕様は“通知は遷移時に返す”方針なので当面は露見しにくいが、将来 `notifyのみ`（例: nudge、警告、監査ログのDiscordのみ）が追加された瞬間に沈黙する罠。"
    suggestion: "適用条件に `discord_notify` を含めるか、より原則的に『SpecTransitionAction は副作用を持つフィールドが1つでもあれば適用』と定義せよ。"

  - id: M-1
    severity: major
    section: "§4.7（spec extend）, §6.3（REVISE完了処理）"
    title: "extend→REVISE直行でcurrent_reviews維持は良いが、review_requests/current_reviews整合の不変条件を明文化すべき"
    description: "rev5で STALLED→REVISE 直行し current_reviews を維持する（§4.7）。これは空転回避として合理的だが、current_reviews が“どのrevのレビュー集合か”を `current_rev/rev_index/last_commit` と整合させる不変条件が仕様に無い。extend直後に改訂してreview_historyへアーカイブする際、rev番号・commitの紐づけを誤ると監査ログとして価値が落ちる。"
    suggestion: "current_reviews に `reviewed_rev`（または review_round_id）を持たせる、もしくは『SPEC_STALLED到達時点の current_rev を保持し、extend後もそのrevに紐づくレビューとして扱う』等の不変条件を1文で断言せよ。"

  - id: m-1
    severity: minor
    section: "§5.2（timeout→current_reviews追加）"
    title: "timeoutエントリのparse_success=falseは妥当だが、statusフィールドが無く診断性が落ちる"
    description: "timeout reviewer を `raw_text=None, parse_success=False` で current_reviews に入れるのは判定上は良い。ただし parse_fail（パース失敗）とtimeoutがどちらも parse_success=False になり得て、デバッグ時に識別しづらい。今は raw_text の有無で分岐しているが、仕様上はステータスを明示した方が安全。"
    suggestion: "current_reviews に `status: received|timeout|parse_failed` を追加し、should_continue_review もそれを一次ソースにせよ（raw_text有無に依存しない）。"

  - id: s-1
    severity: suggestion
    section: "§5.5（パース）"
    title: "YAML抽出regex依存は残る（運用で減らせる）"
    description: "『YAMLブロックは1つだけ』で実害は減ったが、抽出工程自体は境界条件を残す。"
    suggestion: "レビュアー指示を『返答はYAMLのみ（他テキスト禁止）』にして抽出不要にすると、決定性がさらに上がる。"
```
