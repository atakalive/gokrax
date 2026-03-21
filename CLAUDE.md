# CLAUDE.md — gokrax

## プロジェクト概要

開発パイプライン自動化ツール。Issue の起票→設計レビュー→実装→コードレビュー→マージのサイクルを自動化する。CLI + watchdog デーモン構成。

### アーキテクチャ

```
# === CLI ===
gokrax.py              # CLI エントリポイント（全コマンド定義）
commands/spec.py       # spec mode CLI サブコマンド群

# === watchdog デーモン ===
watchdog.py            # メインループ(process)、Discord handler、キュー管理
                       # ※ #127-#129 で大部分を engine/ へ切り出し済み

# === engine/ — watchdog から分離されたコアロジック ===
engine/shared.py       # watchdog/gokrax 共通ユーティリティ (#127)
engine/reviewer.py     # レビュー管理（レビュアー選定・リセット等）(#128)
engine/cc.py           # CC CLI 自動化（plan/impl 起動、pytest baseline）(#129)
engine/fsm.py          # 通常モード状態遷移（check_transition 等）(#131 で追加予定)
engine/fsm_spec.py     # spec mode 状態遷移（check_transition_spec 等）

# === 基盤 ===
config.py              # 状態定義、遷移テーブル、定数、パス
notify.py              # 通知（Discord 投稿、エージェント間通信）
pipeline_io.py         # パイプライン JSON の読み書き（flock 排他）
task_queue.py          # タスクキュー管理

# === spec mode ===
spec_issue.py          # spec mode: Issue 自動起票
spec_review.py         # spec mode: 仕様レビュー
spec_revise.py         # spec mode: 仕様修正

# === メッセージ外部化 ===
messages/              # プロンプト・通知テンプレート
  __init__.py          # render() エントリポイント
  ja/dev/              # 通常モード（design_plan, code_review 等）
  ja/spec/             # spec mode（review, revise, approved 等）

# === その他 ===
scripts/               # 検証スクリプト（verify_spec_messages.py 等）
reviews/               # レビュー依頼の外部化ファイル置き場
tests/                 # pytest テスト
docs/                  # ドキュメント
```

## コーディング規約

### Python スタイル
- **リンター:** ruff
- **型ヒント必須:** 全ての関数に引数・戻り値の型ヒントを書く
  - `list[str]` / `dict[str, Any]`（PEP 585）
  - `X | None`（PEP 604）
- **テスト:** pytest。`tests/` ディレクトリに配置
- **明示的 > 暗黙的**

### コミット規約
- **1 issue = 1 commit** を基本とする
- コミットメッセージ形式: `fix: <description>. Closes #N`
  - type: `fix`, `feat`, `refactor`, `test`, `docs`
- **⚠️ `Closes #N` を必ず含めること。**
- **⚠️ 実装が終わったら必ず `git add` → `git commit` すること。コミットせずに終了するな。**
- main ブランチに直接 push

### 改行コード
- **LF 統一。CRLF は使わない。**

## テスト

```bash
# テスト実行
pytest tests/ -v

# リンター
ruff check *.py tests/
```

### テストの禁止事項
- 本プロジェクトはLinux上で開発している。テストをWindows仕様に変更してはならない。
- **`time.sleep()` をテストコードで直接呼ぶな。** conftest で `time.sleep` はグローバルにモック済み。プロダクションコードの sleep がテスト中に走ると累積して timeout する
- sleep の動作を検証したい場合は `patch("time.sleep") as mock_sleep` で呼び出し回数・引数を assert する
- **外部通信（Discord, agent 送信）はテストで実行するな。** conftest の `_block_external_calls` でモック済み。新しい外部通信関数を追加したら conftest にもモックを追加すること
- **`_reset_reviewers` / `_reset_short_context_reviewers` はテストで実行するな。** conftest でモック済み。直接テストする場合は `test_short_context.py` のように個別にモックを構成する

## 設計上の注意

### パイプライン JSON
- **パイプライン JSON を直接編集するな。** 必ず `pipeline_io.py` の `update_pipeline()` 経由で操作する
- `update_pipeline()` は flock(LOCK_EX) でブロッキング排他ロック。LOCK_NB は使わない
- pipeline JSON のパス: `~/.openclaw/shared/pipelines/<project>.json`

### 状態遷移
- 有効な状態と遷移は `config.py` の `VALID_STATES` / `VALID_TRANSITIONS` に定義
- 遷移は `gokrax transition` CLI コマンド or watchdog の `check_transition()` で実行
- spec mode は別系統: `SPEC_STATES` / `SPEC_TRANSITIONS` / `check_transition_spec()`

### watchdog
- `watchdog-loop.sh` で5秒おきにポーリング
- 各プロジェクトの状態をチェックし、条件を満たせば自動遷移
- CC 起動は `_start_cc()` で bash スクリプトを生成→バックグラウンド実行

### 触ってはいけないもの
- `pipeline_io.py` のロック方式（flock LOCK_EX ブロッキング）
- 通知フォーマットのうち、他エージェントがパースに依存している部分
- `settings.py` の既存状態名・遷移テーブル（追加は OK、変更・削除は慎重に）
- `messages_custom/` — ユーザーがカスタマイズしたプロンプト。編集・削除するな
- `config/states.py` の遷移テーブル（`VALID_TRANSITIONS`, `SPEC_TRANSITIONS`, `STATE_PHASE_MAP`, `BLOCK_TIMERS` 等）は文字列のまま維持する。可読性のため `State.XX` 参照に変換するな

### 絶対に実行してはいけないコマンド
以下の gokrax CLI コマンドはパイプラインの停止・状態破壊を引き起こす。実装・テスト中に絶対に実行するな：
- `gokrax reset` — 全プロジェクトを IDLE に強制リセット
- `gokrax transition` — パイプライン状態を手動遷移
- `gokrax disable` — watchdog を停止
- `gokrax enable` — watchdog を起動
- `gokrax start` / `gokrax qrun` — 新しいバッチを開始

### 既知の癖
- `gokrax.py` の `cmd_transition`（CLI 経路）と `watchdog.py` の `do_transition`（watchdog 経路）は別パス。片方だけ修正すると主経路に乗らないことがある
- `cmd_qrun`（CLI）と `_handle_qrun`（Discord）も同様の2経路問題がある

## GitLab 操作

- **このプロジェクトは GitLab。`gh` (GitHub CLI) は使わない。**
- `glab` CLI を使う
