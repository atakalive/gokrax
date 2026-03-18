# Spec Mode バグレポート検証 — Pascal

## C1: ISSUE_SUGGESTION 遷移時 review_requests 未リセット
- 検証結果: **Confirmed** (正しい)
- 根拠: `gokrax.py:1185-1188` の `cmd_spec_continue` および `watchdog.py:1556-1559` の `check_transition_spec` (`SPEC_APPROVED` の `auto_continue` 分岐) において、状態遷移 (`next_state="ISSUE_SUGGESTION"`) のみが指定されており、`review_requests` をリセットする `pipeline_updates` や直接の辞書操作が存在しない。テスト側では `tests/test_spec_mode_integration.py:189` にて直接リセットする回避策が取られている。
- 修正案:
  - `gokrax.py` の `cmd_spec_continue` 内で、遷移と共に `spec_review._reset_review_requests` を呼び出して状態をリセットする。
  - `watchdog.py` の `check_transition_spec` における `auto_continue` 分岐の `pipeline_updates` に、全レビュアーの `status` を `pending` 等に戻す `review_requests_patch` を追加する。

## C2: implementer 応答受領 CLI 未実装
- 検証結果: **Confirmed** (正しい)
- 根拠: `watchdog.py` は `_check_spec_revise`, `_check_issue_plan`, `_check_queue_plan` の各処理で `_revise_response`, `_issue_plan_response`, `_queue_plan_response` フィールドを読み取って処理を進めるが、`gokrax.py` の `cmd_spec` サブコマンド群にはこれらを `pipeline.json` に書き込むためのインターフェースが存在しない。外部からの入力経路が断たれている。
- 修正案:
  - `gokrax.py` に `revise-submit`, `plan-submit`, `queue-submit` の各コマンドを追加し、それぞれ指定されたYAMLファイルの内容を読み込んで該当する `_response` フィールドに書き込む処理を実装する。

## C3: MIN_VALID_REVIEWS_BY_MODE不一致
- 検証結果: **Confirmed** (正しい)
- 根拠: `config.py:296-298` にて `MIN_VALID_REVIEWS_BY_MODE = {"full": 3, "standard": 2, "lite": 2, "min": 1}` と定義されている。しかし仕様上 `full` モードは「3人中2人の有効レビュー」で進行すべきであり、実装が厳しすぎる（1人のタイムアウトで即 `failed` になる論理的矛盾）。
- 修正案:
  - `config.py` の値を仕様通り `{"full": 2, "standard": 2, "lite": 1, "min": 1}` に修正する。

## I1: reviewed_rev 未設定
- 検証結果: **Confirmed** (正しい)
- 根拠: `gokrax.py:1407-1441` の `cmd_spec_review_submit` および `watchdog.py:1055-1058` のタイムアウト処理において、`current_reviews` ディクショナリを初期化または操作する際、トップレベルの `reviewed_rev` フィールドを設定する記述が存在しない。
- 修正案:
  - `cmd_spec_review_submit` にて、`cr["reviewed_rev"] = sc.get("current_rev", "1")` を明示的に設定する。
  - `watchdog.py` の `_check_spec_review` のパッチ生成処理においても、`current_reviews_patch` 経由等で `reviewed_rev` を設定する。

## 追加指摘: 正常承認(APPROVE)時の `current_reviews` アーカイブおよびクリア漏れ
- 概要: `SPEC_REVIEW` で全員APPROVEとなり `SPEC_APPROVED` に自動遷移する際、`current_reviews` が `review_history` にアーカイブされず、クリアもされない。
- 根拠: `gokrax.py` の `cmd_spec_approve --force` や `SPEC_REVISE` 完了時にはアーカイブ処理が走るが、`watchdog.py` の `should_continue_review` が `"approved"` を返した自動遷移ルートでは、`current_reviews` を操作・クリアする指示が `pipeline_updates` に含まれていない。
- 影響: `SPEC_APPROVED` から `ISSUE_SUGGESTION` へ遷移した直後に、`_check_issue_suggestion` が前フェーズの `current_reviews.entries` に残っている `"received"` 状態のレビューテキストを誤って読み取り、パースエラー等の異常な動作を引き起こす。C1と組み合わさって致命的な進行停止を招くP0級の欠陥である。
- 修正案: `watchdog.py` の `_check_spec_review` における完了判定 (`result == "approved"`) の際、`build_review_history_entry` を用いて履歴を追加し、かつ `current_reviews` を空にする更新を `pipeline_updates` に明示的に含めること。