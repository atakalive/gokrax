# gokrax Spec Mode — 仕様書レビュー（Leibniz, rev4 / やりすぎ版）

対象: `docs/spec-mode-spec_rev4.md` (rev4, 1136行)
commit: rev3=`50a375b` → rev4=`45dca2b`

rev3での私のP0群（REVIEW後遷移矛盾、DCL不備、遷移順序非決定、pipelines_dir未仕様、CLI優先順位未明文化）は、仕様上かなり改善しています。とはいえ **_apply_spec_action のDCL適用条件が誤っており、遷移が“起こるべきときに起きない”** 可能性が残っています。ここがP0。

---

```yaml
verdict: P0
items:
  - id: C-1
    severity: critical
    section: "§10.1（_apply_spec_action 擬似コード）"
    title: "DCL再計算結果(action2)を捨てる条件があり、正しい遷移が抑止され得る"
    description: "_apply_spec_action の lock 内で `action2 = check_transition_spec(...)` を再計算しているのは正しい。しかし適用条件が `if action2.next_state and action2.next_state == action.next_state:` となっており、再計算結果が“元のactionと一致した場合のみ”遷移を適用する。DCLの目的は『lock待ち中に状態/入力が変わったときは“最新の再計算結果”に従う』ことであり、一致を要求するのは逆。例えばタイムアウト境界・review_requests更新・current_reviews更新のタイミングで action.next_state が stale になった場合、action2が別遷移を返しても applied=False となり、そのtickでは何も起きず（通知も送られず）、結果として停滞/取りこぼしが起き得る。"
    suggestion: "適用条件は『expected_state一致』のみで十分。`action2.next_state` が存在するなら **action2に従って** 遷移・pipeline_updates・通知を行うべき。少なくとも `== action.next_state` 条件は削除し、`applied_action = action2` を常に採用せよ。"

  - id: C-2
    severity: critical
    section: "§5.4（_reset_review_requests）/ §5.1（送信フロー）"
    title: "review_requestsのresetがsent_at/timeout_atをNone化するが、再送/再開時のtimeout再設定の責務が曖昧"
    description: "_reset_review_requests は timeout_at を None に戻す。良いが、どの関数が timeout_at を必ず再設定するのか（送信直前に常に設定されることの保証）が仕様上は“暗黙”で、実装漏れすると pending が永遠にtimeoutしない（または即死）という致命的バグになる。rev4では呼び出し箇所列挙があるが、再設定箇所（送信時に sent_at/timeout_at を必ず埋める）を仕様として断言していない。"
    suggestion: "送信関数（例: `_send_spec_review_request`）の事後条件として `sent_at!=None && timeout_at!=None` を明記し、テストで検査せよ（reviewer全員分）。"

  - id: M-1
    severity: major
    section: "§5.3（should_continue_review）"
    title: "receivedの定義が current_reviews のみで、timeout reviewer を含めない設計に見える"
    description: "should_continue_review は `reviews = spec_config.get('current_reviews', {})` から `received` を作っている。current_reviews が『応答を受け取ったreviewerのみ』を格納する設計だと、タイムアウトしたreviewerは辞書に現れず、received/parsed_ok/parsed_fail の数が“応答者のみ”になる。これは判定自体は可能だが、『全reviewer status = received|timeout になったら判定開始』（§5.2）という前提と、データソース（current_reviewsのみ）の間にギャップがある。"
    suggestion: "(A) current_reviews に timeout も含め `raw_text=None, parse_success=False, status='timeout'` を格納する、または (B) should_continue_review の入力を `review_requests` + `current_reviews` の合成に変更し、received=timeout+received を明確に定義せよ。"

  - id: m-1
    severity: minor
    section: "§12.1（pipelines_dir）"
    title: "pipelines_dirのパス例が複数表記で揺れる可能性（PIPELINES_DIR/{project} vs project_pipelines_dir）"
    description: "§4.2 step4で `project_pipelines_dir` という語が出るが、定義が本文中で一意でない。仕様としては『PIPELINES_DIR/{project}/spec-reviews/』のように単一表記に揃えるべき。"
    suggestion: "変数名・パス組み立て規則を1つに固定し、例もその表記だけに揃えよ。"

  - id: s-1
    severity: suggestion
    section: "§5.5（パース）"
    title: "YAML抽出regex依存をさらに下げる余地"
    description: "rev4でも『最初の1ブロック』抽出のまま。『YAMLブロックは1つだけ』があるので致命傷ではないが、依然として境界条件は残る。"
    suggestion: "レビュアー指示を『返答はYAMLのみ（他テキスト禁止）』に変更し、抽出工程を不要にすると決定性が上がる。"
```
