# Spec Mode バグレポート検証 — Leibniz

検証対象: `docs/2026-03-04_spec-mode-bug-report.md` 記載の C1/C2/C3/I1。
以下では、レポート主張が「現行ソース（/mnt/s/wsl/work/project/gokrax/ 以下）に対して事実か」をコード読みにより判定する。

## C1: review_requests未リセット
- 検証結果: **confirmed**
- 根拠:
  - CLI `spec continue` は状態遷移のみで `spec_config` を一切更新しない。
    - `gokrax.py`:
      ```py
      def cmd_spec_continue(args):
          ...
          def do_continue(data):
              data["state"] = "ISSUE_SUGGESTION"
      ```
      （`review_requests`/`current_reviews` のリセットなし）
  - watchdog の auto_continue も同様に state のみ遷移させ、`pipeline_updates` がない。
    - `watchdog.py`:
      ```py
      elif state == "SPEC_APPROVED":
          ...
          if spec_config.get("auto_continue"):
              return SpecTransitionAction(
                  next_state="ISSUE_SUGGESTION",
                  discord_notify=...,  # pipeline_updatesなし
              )
      ```
  - `ISSUE_SUGGESTION` 側は `review_requests` の `status == "pending"` を送信条件としているため、SPEC_REVIEWで全員`received`のまま入ると送信されず、かつ完了判定だけ進み得る。
    - `watchdog.py`（ISSUE_SUGGESTION）:
      ```py
      if status == "pending" and req.get("sent_at") is None:
          ...  # 送信
      ...
      all_complete = ... all(_effective_status(r) != "pending" ...)
      if all_complete:
          if issue_suggestions:
              ...
          else:
              return SpecTransitionAction(next_state="SPEC_PAUSED", ...)
      ```

- 修正案（具体）:
  1) **正攻法（推奨）**: `check_transition_spec(SPEC_APPROVED)` が `ISSUE_SUGGESTION` へ遷移する際に `pipeline_updates` を積み、同一トランザクションでリセットする。
     - 例（watchdog.py）:
       - `SPEC_APPROVED` 分岐の auto_continue / manual continue 両方で、
         - `review_requests_patch`: 全レビュアー `pending` + `sent_at/timeout_at/last_nudge_at/response=None`
         - `current_reviews`: `{}`（または `current_reviews.entries={}`）
         を適用。
  2) CLI 側も整合させる（watchdogを止めた運用でも事故らない）:
     - `cmd_spec_continue()` で `data["spec_config"]` の `review_requests` と `current_reviews` をリセットする。
     - さらに `spec_mode` の有効性チェック（`data.get("spec_mode")`）と `add_history` の付与（通常遷移との整合）も行う。

## C2: implementer応答受領CLI未実装
- 検証結果: **confirmed**
- 根拠:
  - watchdog は implementer 応答を `spec_config["_revise_response"]` / `spec_config["_issue_plan_response"]` / `spec_config["_queue_plan_response"]` から読む。
    - `watchdog.py`:
      ```py
      revise_response = spec_config.get("_revise_response")
      issue_plan_response = spec_config.get("_issue_plan_response")
      queue_plan_response = spec_config.get("_queue_plan_response")
      ```
  - しかし `gokrax.py` の spec サブコマンドは現状 `review-submit` しか存在せず、上記3フィールドを書き込むCLIが存在しない。
    - `gokrax.py`:
      ```py
      spec_cmds = {
          ...
          "review-submit": cmd_spec_review_submit,
      }
      ```
  - したがって、本番フローでは（外部からpipeline.jsonを直接編集しない限り）これらフィールドが `None` のままで、各フェーズはタイムアウト→PAUSED/リトライへ進む。

- 修正案（具体）:
  - `review-submit` と同型のコマンドを追加し、**flock内で** `spec_config` を更新する。
    - `gokrax spec revise-submit --pj PJ --file FILE` → `_revise_response` に raw_text を格納
    - `gokrax spec issue-plan-submit --pj PJ --file FILE` → `_issue_plan_response` に raw_text を格納
    - `gokrax spec queue-plan-submit --pj PJ --file FILE` → `_queue_plan_response` に raw_text を格納
  - 各コマンドで最低限チェック:
    - state がそれぞれ `SPEC_REVISE` / `ISSUE_PLAN` / `QUEUE_PLAN` であること
    - `spec_mode` が有効であること
    - file存在 + UTF-8読み込み
  - パース/バリデーション方針:
    - 早期に `parse_revise_response/parse_issue_plan_response/parse_queue_plan_response` を呼んで弾く（fail-fast）か、
    - watchdog側に委ねるなら raw_text のみ格納（ただしこの場合でも最低「YAMLブロックらしさ」程度は検査推奨）

## C3: MIN_VALID_REVIEWS_BY_MODE不一致
- 検証結果: **partially correct**（「コード値がレポート記載どおりで、挙動が変わる」までは確認できるが、仕様書§3.2の正誤はこの依頼範囲のファイルだけでは断定不能）
- 根拠（コード側の事実）:
  - `config.py` の定義はレポート記載と一致（full=3, lite=2）。
    - `config.py`:
      ```py
      MIN_VALID_REVIEWS_BY_MODE: dict[str, int] = {
          "full": 3, "standard": 2, "lite": 2, "min": 1,
      }
      ```
  - `should_continue_review()` は `len(received) < min_valid` を満たすと `failed/paused` に落とす。
    - `spec_review.py`:
      ```py
      min_valid = MIN_VALID_REVIEWS_BY_MODE.get(review_mode, 2)
      if len(received) < min_valid:
          if len(parsed_fail) > 0:
              return "paused"
          return "failed"
      ```
  - よって「fullモードで1人timeout → received=2」の場合、現行コードでは `2 < 3` で `failed/paused` 側に入る（レポートが指摘する“耐タイムアウト性低下”は事実）。

- 修正案（具体）:
  1) 仕様がレポート引用どおり（full=2, lite=1）なら、`config.MIN_VALID_REVIEWS_BY_MODE` をその値へ戻す。
  2) 概念分離を明確化する（将来の混入防止）:
     - `REVIEW_MODES[...]["min_reviews"]`（通常gokraxのレビュー収集要件）と
       `MIN_VALID_REVIEWS_BY_MODE`（spec-modeの「有効レビュー閾値」）は別概念なので、
       変数名を `SPEC_MIN_VALID_REVIEWS_BY_MODE` 等に変え、コメントで“通常レビューの min_reviews と連動させない”と明記する。
  3) テスト追加:
     - `full` で「2 received + 1 timeout」を作り、仕様どおりに `approved/revise/stalled` 判定へ進むことを検証（現状はこのケースが未カバー）。

## I1: reviewed_rev未設定
- 検証結果: **confirmed**
- 根拠:
  - `cmd_spec_review_submit()` は `current_reviews.entries[...]` を書くが、トップレベル `current_reviews["reviewed_rev"]` を設定しない。
    - `gokrax.py`:
      ```py
      cr = sc.setdefault("current_reviews", {})
      entries = cr.setdefault("entries", {})
      entries[args.reviewer] = {...}
      # reviewed_rev を設定していない
      ```
  - watchdog の timeout 生成も `current_reviews_patch` に `reviewed_rev` を含めない。
    - `watchdog.py`（SPEC_REVIEW timeout処理）:
      ```py
      cr_patch[reviewer] = { ... "status": "timeout" }
      ```

- 修正案（具体）:
  1) `cmd_spec_review_submit` の flock内で、初回の submit 時点で `cr["reviewed_rev"] = sc.get("current_rev", "?")` を必ずセット。
  2) watchdog 側も一貫させる:
     - `_check_spec_review()` が `cr_patch` を生成する際、少なくとも `pipeline_updates` に `current_reviews_reviewed_rev = current_rev` のようなトップレベル更新（または `sc.update({"current_reviews": {...}})`）を積み、timeoutでも必ず入るようにする。

## 追加指摘

### A1: ISSUE_SUGGESTION 応答投入手段が（少なくともこのリポジトリ内では）存在しない
- 現状 `ISSUE_SUGGESTION` の応答回収は `current_reviews.entries[reviewer].status == "received"` を見て `raw_text` をパースするが、
  - その `entries[...]` を「外部入力として投入するCLI」が見当たらない（`review-submit` は verdict/items形式であり、Issue suggestion YAMLとは別物）。
  - テストは `sc["current_reviews"]["entries"][r] = {"status":"received", "raw_text": ...}` を直接代入して回避している。
    - `tests/test_spec_mode_integration.py`:
      ```py
      sc["current_reviews"].setdefault("entries", {})[r] = {
          "status": "received",
          "raw_text": issue_yaml,
          "response": issue_yaml,
      }
      ```
- 修正案:
  - `gokrax spec issue-suggestion-submit --pj PJ --reviewer R --file FILE` を追加し、
    `ISSUE_SUGGESTION` state のときに `current_reviews.entries[R] = {status:"received", raw_text: <file> ...}` を投入できるようにする。
  - あるいは `review_requests[reviewer]["response"]` を使用する設計に寄せ、`current_reviews` をレビュー専用に戻す（表記・データモデルの整理）。

### A2: cmd_spec_continue が履歴(history)を残さない
- watchdog遷移は `add_history(..., actor="watchdog")` を行うが、CLI `cmd_spec_continue` は `data["state"]` を直接変更するだけで `history` 更新がない。
  - 監査性・デバッグ性の観点で不整合。
- 修正案:
  - `cmd_spec_continue` でも `add_history(data, "SPEC_APPROVED", "ISSUE_SUGGESTION", actor="cli")` を呼ぶ。

