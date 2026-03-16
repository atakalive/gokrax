# CLAUDE.md — devbar

## プロジェクト概要

開発パイプライン自動化ツール。Issue の起票→設計レビュー→実装→コードレビュー→マージのサイクルを自動化する。CLI + watchdog デーモン構成。

### アーキテクチャ

```
devbar.py         # CLI エントリポイント（全コマンド定義）
watchdog.py       # watchdog デーモン（状態遷移、CC起動、レビュー管理）
config.py         # 状態定義、遷移テーブル、定数
notify.py         # 通知（Discord、エージェント間通信）
pipeline_io.py    # パイプライン JSON の読み書き（flock排他）
task_queue.py     # タスクキュー管理
spec.md           # 仕様書
spec_issue.py     # spec mode: Issue 自動起票
spec_review.py    # spec mode: 仕様レビュー
spec_revise.py    # spec mode: 仕様修正
reviews/          # レビュー依頼の外部化ファイル置き場
tests/            # pytest テスト
docs/             # ドキュメント
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

## 設計上の注意

### パイプライン JSON
- **パイプライン JSON を直接編集するな。** 必ず `pipeline_io.py` の `update_pipeline()` 経由で操作する
- `update_pipeline()` は flock(LOCK_EX) でブロッキング排他ロック。LOCK_NB は使わない
- pipeline JSON のパス: `~/.openclaw/shared/pipelines/<project>.json`

### 状態遷移
- 有効な状態と遷移は `config.py` の `VALID_STATES` / `VALID_TRANSITIONS` に定義
- 遷移は `devbar transition` CLI コマンド or watchdog の `check_transition()` で実行
- spec mode は別系統: `SPEC_STATES` / `SPEC_TRANSITIONS` / `check_transition_spec()`

### watchdog
- `watchdog-loop.sh` で5秒おきにポーリング
- 各プロジェクトの状態をチェックし、条件を満たせば自動遷移
- CC 起動は `_start_cc()` で bash スクリプトを生成→バックグラウンド実行

### 触ってはいけないもの
- `pipeline_io.py` のロック方式（flock LOCK_EX ブロッキング）
- 通知フォーマットのうち、他エージェントがパースに依存している部分
- `config.py` の既存状態名・遷移テーブル（追加は OK、変更・削除は慎重に）

### 既知の癖
- `devbar.py` の `cmd_transition`（CLI 経路）と `watchdog.py` の `do_transition`（watchdog 経路）は別パス。片方だけ修正すると主経路に乗らないことがある
- `cmd_qrun`（CLI）と `_handle_qrun`（Discord）も同様の2経路問題がある

## GitLab 操作

- **このプロジェクトは GitLab。`gh` (GitHub CLI) は使わない。**
- `glab` CLI を使う
