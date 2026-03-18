# Spec Mode バグレポート検証 — Euler

## C1: review_requests未リセット
- 検証結果: **confirmed**
- 根拠:
  - `gokrax.py` の `cmd_spec_continue()` は状態だけを変更しており、`spec_config.review_requests` と `spec_config.current_reviews` を一切リセットしていない。
    ```py
    def cmd_spec_continue(args):
        ...
        def do_continue(data):
            data["state"] = "ISSUE_SUGGESTION"
    ```
  - `watchdog.py` の `check_transition_spec()` でも `SPEC_APPROVED` で `auto_continue` の場合、`next_state="ISSUE_SUGGESTION"` を返すだけで `pipeline_updates` が無い。
    ```py
    elif state == "SPEC_APPROVED":
        if spec_config.get("auto_continue"):
            return SpecTransitionAction(
                next_state="ISSUE_SUGGESTION",
                ...,
            )
    ```
  - `watchdog.py` の `_check_issue_suggestion()` は「送信」は `status == "pending" and sent_at is None` のみ。
    一方、直前フェーズ `SPEC_REVIEW` を完了すると `review_requests[*].status == "received"` のまま残る（`cmd_spec_review_submit()` が `rr[reviewer]["status"] = "received"` している）ため、ISSUE_SUGGESTION に入った瞬間に送信されない。
    ```py
    if status == "pending" and req.get("sent_at") is None:
        send_to[reviewer] = prompt
        ...
    ```
  - さらに、`_check_issue_suggestion()` の完了判定は「全員が pending でない」なので、`received` のままでも `all_complete=True` になりうる。
    ```py
    all_complete = (
        bool(review_requests)
        and all(_effective_status(r) != "pending" for r in review_requests)
    )
    ```
    `issue_suggestions` が空なら即 `SPEC_PAUSED` になる。

- 修正案（具体的なコード変更）:
  - **方針**: `SPEC_APPROVED → ISSUE_SUGGESTION` に入るすべての経路で
    1) `review_requests` を全員 `pending` にリセット
    2) `current_reviews.entries` を空にする（古いレビューYAMLが「受領済み」として残り、次フェーズのパースを汚染するのを防ぐ）

  ### 修正案A（推奨）: watchdog側で auto_continue 遷移に pipeline_updates を付与
  `watchdog.py: check_transition_spec()` の `SPEC_APPROVED`/auto_continue 分岐を以下のように変更。
  
  ```py
  elif state == "SPEC_APPROVED":
      if spec_config.get("review_only"):
          ...
      if spec_config.get("auto_continue"):
          reset_patch = {
              r: {
                  "status": "pending",
                  "sent_at": None,
                  "timeout_at": None,
                  "last_nudge_at": None,
                  "response": None,
              }
              for r in (spec_config.get("review_requests", {}) or {})
          }
          return SpecTransitionAction(
              next_state="ISSUE_SUGGESTION",
              discord_notify=spec_notify_approved_auto(project, spec_config.get("current_rev", "?")),
              pipeline_updates={
                  "review_requests_patch": reset_patch,
                  "current_reviews": {"reviewed_rev": spec_config.get("current_rev", "?"), "entries": {}},
              },
          )
      return SpecTransitionAction(next_state=None)
  ```

  ### 修正案B（併用推奨）: CLI `spec continue` でも同様にリセット
  `gokrax.py: cmd_spec_continue()` を以下のように変更（watchdogを通らない手動continue対策）。

  ```py
  def cmd_spec_continue(args):
      ...
      def do_continue(data):
          sc = data.get("spec_config", {})
          # reset review_requests
          rr = sc.get("review_requests", {})
          for entry in rr.values():
              entry["status"] = "pending"
              entry["sent_at"] = None
              entry["timeout_at"] = None
              entry["last_nudge_at"] = None
              entry["response"] = None
          # clear current_reviews
          sc["current_reviews"] = {"reviewed_rev": sc.get("current_rev", "?"), "entries": {}}
          data["spec_config"] = sc
          data["state"] = "ISSUE_SUGGESTION"
  ```


## C2: implementer応答受領CLI未実装
- 検証結果: **confirmed**
- 根拠:
  - `watchdog.py` は implementer 応答として以下フィールドを読みに行くが、投入手段は `gokrax.py` に存在しない。
    - `_check_spec_revise()` → `spec_config.get("_revise_response")`
    - `_check_issue_plan()` → `spec_config.get("_issue_plan_response")`
    - `_check_queue_plan()` → `spec_config.get("_queue_plan_response")`
  - `gokrax.py` の spec サブコマンド登録は現状 `review-submit` のみ（`revise-submit/plan-submit/queue-submit` が無い）。
    ```py
    spec_cmds = {
        "start": ..., "review-submit": cmd_spec_review_submit,
    }
    ```

- 修正案（具体的なコード変更）:
  - **方針**: `review-submit` と同じパターンで「ファイル読み込み→パース/バリデーション→状態チェック→flock内で spec_config へ書き込み」を実装する。

  ### 追加コマンド案
  - `gokrax spec revise-submit --pj PROJECT --file FILE`
  - `gokrax spec plan-submit   --pj PROJECT --file FILE`
  - `gokrax spec queue-submit  --pj PROJECT --file FILE`

  ### 実装例（gokrax.py に追加）
  1) パーサは watchdog が使っているものを利用して整合性を取る。
  - revise: `from spec_revise import parse_revise_response`
  - issue_plan: `from spec_issue import parse_issue_plan_response`
  - queue_plan: `from spec_issue import parse_queue_plan_response`

  2) `cmd_spec_review_submit` と同様の SIGTERM 遅延 + `update_pipeline()` で更新。

  #### revise-submit（例）
  ```py
  def cmd_spec_revise_submit(args):
      path = get_path(args.project)
      p = Path(args.file)
      if not p.is_file():
          raise SystemExit(f"File not found: {args.file}")
      raw = p.read_text(encoding="utf-8")

      from spec_revise import parse_revise_response
      # 現在revはロック内で読むので、ここでは軽く通す or None許容

      _deferred = False
      _orig = signal.getsignal(signal.SIGTERM)
      def _defer_sigterm(signum, frame):
          nonlocal _deferred
          _deferred = True
      signal.signal(signal.SIGTERM, _defer_sigterm)
      try:
          def do_update(data):
              if data.get("state") != "SPEC_REVISE":
                  raise SystemExit(f"Not in SPEC_REVISE state: {data.get('state')}")
              sc = data.get("spec_config", {})
              cur = sc.get("current_rev", "1")
              if parse_revise_response(raw, cur) is None:
                  raise SystemExit("Invalid revise response YAML")
              sc["_revise_response"] = raw
              data["spec_config"] = sc
          update_pipeline(path, do_update)
      finally:
          signal.signal(signal.SIGTERM, _orig)
          if _deferred:
              signal.raise_signal(signal.SIGTERM)
      print(f"{args.project}: revise response submitted")
  ```

  #### plan-submit / queue-submit
  同様に `state` をそれぞれ `ISSUE_PLAN` / `QUEUE_PLAN` に限定し、
  `sc["_issue_plan_response"] = raw` / `sc["_queue_plan_response"] = raw` を書く。

  3) parser登録（help含む）
  - `spec_cmds` に 3つ追加
  - `argparse` の `spec_sub.add_parser()` に 3つ追加


## C3: MIN_VALID_REVIEWS_BY_MODE不一致
- 検証結果: **confirmed（少なくとも「実装値がレポート記載と違う」点は確実）**
- 根拠:
  - `config.py` の定義は以下。
    ```py
    MIN_VALID_REVIEWS_BY_MODE: dict[str, int] = {
        "full": 3, "standard": 2, "lite": 2, "min": 1,
    }
    ```
  - `spec_review.py` の `should_continue_review()` はこれを `min_valid` として使用しており、`received < min_valid` で `failed/paused` を返す。
    ```py
    min_valid = MIN_VALID_REVIEWS_BY_MODE.get(review_mode, 2)
    if len(received) < min_valid:
        ...
        return "failed"
    ```
  - よって full モードで 3人中2人 received + 1人 timeout の場合、現在実装では `len(received)=2 < 3` で `failed` 側に倒れる。

- 修正案（具体的なコード変更）:
  - 仕様（レポート記載）に合わせるなら `config.py` を修正。

  ```py
  MIN_VALID_REVIEWS_BY_MODE: dict[str, int] = {
      "full": 2,
      "standard": 2,
      "lite": 1,
      "min": 1,
  }
  ```

  - 併せて **`REVIEW_MODES` に存在する `lite3` 等も MIN_VALID 側に明示**しておくのが安全。
    例: `"lite3": 2`（または仕様に従う）。


## I1: reviewed_rev未設定
- 検証結果: **confirmed**
- 根拠:
  - `gokrax.py: cmd_spec_review_submit()` は `current_reviews.entries` は書くが、トップレベル `current_reviews["reviewed_rev"]` を設定していない。
    ```py
    cr = sc.setdefault("current_reviews", {})
    entries = cr.setdefault("entries", {})
    entries[args.reviewer] = {...}
    # cr["reviewed_rev"] を設定していない
    ```
  - `watchdog.py: _check_spec_review()` のタイムアウト時 `cr_patch[reviewer] = {...}` でも `reviewed_rev` は触っていない。

- 修正案（具体的なコード変更）:
  - **最低限**: `cmd_spec_review_submit()` で `reviewed_rev` をセット。

  ```py
  cr = sc.setdefault("current_reviews", {})
  if "reviewed_rev" not in cr:
      cr["reviewed_rev"] = sc.get("current_rev", "?")
  entries = cr.setdefault("entries", {})
  ...
  ```

  - **より堅牢**: watchdog側でも `SPEC_REVIEW` に入って送信/タイムアウト処理をする時点で `current_reviews.reviewed_rev` を必ず設定する。
    例として `_check_spec_review()` の `updates` に以下を追加（浅い上書きで entries を落とさないよう、既存 entries を含めて構築）。

  ```py
  effective_entries = dict(entries)
  effective_entries.update(cr_patch)
  updates["current_reviews"] = {
      "reviewed_rev": current_rev,
      "entries": effective_entries,
  }
  ```


## 追加指摘

1) **C1は review_requests リセットだけでは不十分（current_reviews クリア必須）**
   - `ISSUE_SUGGESTION` の回収ロジックは `entries[reviewer].status == "received"` を見てパースするため、`SPEC_REVIEW` の受領済みYAMLが `entries` に残ると、次フェーズで「送信後の回収」が始まった瞬間に古いYAMLを誤パースして `parse_failed` 量産→停止しうる。
   - したがって C1の修正は **`current_reviews.entries` を空にする**のが実質要件。

2) **MIN_VALID_REVIEWS_BY_MODE に `lite3`/`skip` が無い**（設定の穴）
   - `REVIEW_MODES` には `lite3`/`skip` が存在するが、`spec start --review-mode` の choices には `lite3/skip` が無い。
   - しかし `data["review_mode"]` は通常モードから流用されうる（例: PJが `lite3` に設定済みのとき `spec start` で `--review-mode` 省略）。
   - `should_continue_review(sc, review_mode)` は `MIN_VALID_REVIEWS_BY_MODE.get(review_mode, 2)` でフォールバック2なので、意図せず閾値が変わり得る。
   - 対応案: 
     - spec-start 時に `review_mode` を spec専用に正規化（choicesを `REVIEW_MODES.keys()` と一致させる／または spec用modeを別キーにする）
     - あるいは `MIN_VALID_REVIEWS_BY_MODE` を `REVIEW_MODES` と同じキー集合で埋める。
