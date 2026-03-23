# DOMAIN_RISK.md (gokrax)

分類の粒度は「責務 + ファイル/関数パス」。同一ファイルが複数リスクレベルにまたがる場合は責務ごとに記載する。

High Risk = バグがパイプライン状態の破壊・不正な遷移・データロスに直結する領域。

## High Risk Changes
- pipeline.json の読み書き・ロック処理（`pipeline_io.py`）
- 状態遷移ロジック（`engine/fsm.py`, `engine/fsm_spec.py`, `watchdog.py`, `config/states.py`）
- CC CLI 起動・パラメータ組み立て（`engine/cc.py` の `_start_cc`, `_start_cc_test_fix`）
- レビュー集計・verdict 算出・クリアロジック（`engine/reviewer.py`）
- state/history/spec_config を更新する CLI コマンド（`commands/dev.py` の `cmd_transition`, `cmd_review`, `cmd_commit`, `cmd_start` 等、`commands/spec.py` の `cmd_spec_start`, `cmd_spec_approve` 等）

Low Risk = バグの影響が限定的、または失敗が検出可能で手動リカバリできる領域。

## Low Risk Changes
- キューファイル書き込み（`task_queue.py` の pop/restore/append/replace/delete）
- キューパース・トークン正規化（`task_queue.py` の `parse_queue_line`）
- glab API 呼び出し — Issue close, タイトル変更, コメント投稿（`commands/dev.py` の `_update_issue_title_with_assessment` 等, `engine/cc.py` の `_auto_push_and_close`）
- git push / merge 処理（`engine/cc.py` の `_auto_push_and_close`）
- プロンプトテンプレートの構造変更 — 出力キー追加等（`messages/` 配下）
- テストインフラ（`tests/conftest.py`）
- settings 更新スクリプト（`update_settings.py`）
- CLI エントリポイント・argparse 定義（`gokrax.py`）
- 通知送信ロジック — Discord/agent 通知・GitLab note 投稿（`notify.py`）
- spec レビュー・リビジョン処理（`spec_review.py`, `spec_revise.py`, `spec_issue.py`）
- watchdog ヘルパー — プロセス状態確認等（`engine/shared.py`）

No Risk = ランタイム動作に影響しない変更。

## No Risk
- Discord 通知メッセージのフォーマット変更（`messages/` 配下の文言のみ）
- プロンプトテンプレートの文言のみの変更（`messages/` 配下、出力キー変更を伴わないもの）
- ドキュメント変更（`README.md`, `CLI.md`, `docs/` 等）
- テストコード（`tests/conftest.py` を除く `tests/` 配下）
- スクリプト（`scripts/` 配下）
