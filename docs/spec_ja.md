# gokrax -- 開発パイプライン仕様書

> 現行コード (2026-03-20 時点) に基づく正式仕様。金子さん含むエージェント全員はこの文書に従うこと。
> 定数値は config のデフォルト値。settings.py で上書き可能 (14 章参照)。

## 1. 概要

gokrax は **Issue -> 設計 -> 実装 -> テスト -> レビュー -> マージ** のパイプラインを管理する CLI + watchdog システム。LLM を使わない純粋なオーケストレーターで、pipeline JSON を状態マシンとして駆動する。

## 2. アーキテクチャ

```
gokrax.py            -- CLI エントリポイント + watchdog loop 管理 + main()
commands/dev.py       -- 通常モード CLI コマンド (cmd_start, cmd_review, etc.)
commands/spec.py      -- spec モード CLI コマンド (cmd_spec_start, etc.)
config/               -- パッケージ化済み
  __init__.py          -- 定数の一元管理 + settings.py override
  states.py            -- 状態遷移テーブル・パイプライン定数
  paths.py             -- ファイルパス・ディレクトリ定数
engine/
  fsm.py               -- 通常モード状態遷移判定 (純粋関数)
  fsm_spec.py          -- spec モード状態遷移判定
  cc.py                -- Claude Code 自動起動・テスト実行
  reviewer.py          -- レビュアー管理 (tier, pending, revise 判定)
  shared.py            -- 共有ユーティリティ (log, is_cc_running, is_ok_reply)
watchdog.py           -- watchdog ループ + Discord コマンド処理
notify.py             -- エージェント通知 + Discord 投稿 + 二層送信
pipeline_io.py        -- JSON 読み書き (排他ロック + atomic write)
spec_review.py        -- spec レビューパース・統合
spec_revise.py        -- spec 改訂依頼・セルフレビュー
spec_issue.py         -- Issue 分割・起票・キュー生成
task_queue.py         -- タスクキュー管理 (qrun/qadd/qdel/qedit)
messages/             -- テンプレートメッセージ (render() 経由)
  __init__.py          -- render() エントリポイント
  ja/dev/              -- 通常モード (design_plan, code_review 等)
  ja/spec/             -- spec mode (review, revise, approved 等)
settings.py           -- ユーザー設定 (config override)
```

- **pipeline JSON**: `~/.openclaw/shared/pipelines/<project>.json`
- **watchdog**: `watchdog-loop.sh` で 5 秒おきにポーリング (後述 7 章)
- **Discord 通知先**: #gokrax (kaneko-discord アカウントで投稿)

## 3. ロール定義

### 3.1 実装担当 (Implementer) = kaneko, neumann

- DESIGN_PLAN フェーズで Issue 本文を確認・修正し、`plan-done` を実行する
- CODE_REVISE フェーズで P0 指摘に基づきコードを手動修正し、`code-revise` を実行する (commit 記録 + revise 完了を一発で)
- IMPLEMENTATION フェーズでは CC が自動起動される (実装担当が手動でやるのではない)

エージェント一覧:

| エージェント | address | モデル | 備考 |
|---|---|---|---|
| kaneko | `agent:kaneko:main` | Opus | Implementer |
| pascal | `agent:pascal:main` | Gemini 3 Pro | |
| leibniz | `agent:leibniz:main` | GPT-4.1 64k-ctx (GitHub) | short-context tier |
| hanfei | `agent:hanfei:main` | GPT-4.1 64k-ctx (GitHub) | short-context tier |
| dijkstra | `agent:dijkstra:main` | Opus | |
| neumann | `agent:neumann:main` | Opus | Implementer |
| euler | `agent:euler:main` | ChatGPT-5.4 | |
| basho | `agent:basho:main` | Local, Qwen3.5-27B | short-context tier |

### 3.2 レビュアー (Reviewers)

レビュアーは3つの tier に分類される。

| Tier | メンバー |
|---|---|
| regular | dijkstra, euler, pascal |
| free | (空) |
| short-context | basho, hanfei, leibniz |

- DESIGN_REVIEW または CODE_REVIEW でレビュー依頼を受け取る
- `gokrax review` コマンドで verdict (APPROVE / P0 / P1 / P2) を投稿する
- **自分が設計・実装したものを自分でレビューしてはならない**
- レビュアーは実装担当ではない。レビュアーが `plan-done`, `commit`, `design-revise`, `code-revise` を実行することはない

### 3.3 承認者 = M (人間)

- MERGE_SUMMARY_SENT で #gokrax にサマリーが投稿される。M が「OK」とリプライすると DONE -> マージ
- `gokrax start` や `gokrax transition --force` 等の制御コマンドを実行する

### 3.4 CC (Claude Code)

- IMPLEMENTATION フェーズで watchdog が自動起動する
- Plan (model: sonnet) -> Impl (model: sonnet) の 2 段階
- CC 完了後、自動で `gokrax commit` を実行する
- **CC は IMPLEMENTATION でのみ使用。他のフェーズでは使わない**
- neumann も CC 経由での実装に対応

## 4. 状態マシン

### 有効状態 (VALID_STATES)

```
IDLE, INITIALIZE, TRIAGE,
DESIGN_PLAN, DESIGN_REVIEW, DESIGN_REVISE, DESIGN_APPROVED,
IMPLEMENTATION,
CODE_TEST, CODE_TEST_FIX,
CODE_REVIEW, CODE_REVISE, CODE_APPROVED,
MERGE_SUMMARY_SENT, DONE, BLOCKED
```

> **注意**: spec mode が有効な場合、VALID_STATES は `sorted(set(VALID_STATES + SPEC_STATES))` で
> アルファベット順にマージされる。

### 遷移テーブル (VALID_TRANSITIONS)

```
TRIAGE       -> [IDLE]
IDLE         -> [INITIALIZE]
INITIALIZE   -> [DESIGN_PLAN]
DESIGN_PLAN  -> [DESIGN_REVIEW]
DESIGN_REVIEW -> [DESIGN_APPROVED, DESIGN_REVISE, BLOCKED]
DESIGN_REVISE -> [DESIGN_REVIEW]
DESIGN_APPROVED -> [IMPLEMENTATION]
IMPLEMENTATION  -> [CODE_TEST, CODE_REVIEW]
CODE_TEST       -> [CODE_REVIEW, CODE_TEST_FIX, BLOCKED]
CODE_TEST_FIX   -> [CODE_TEST, BLOCKED]
CODE_REVIEW  -> [CODE_APPROVED, CODE_REVISE, BLOCKED]
CODE_REVISE  -> [CODE_TEST, CODE_REVIEW]
CODE_APPROVED -> [MERGE_SUMMARY_SENT]
MERGE_SUMMARY_SENT -> [DONE]
DONE         -> [IDLE]
BLOCKED      -> [IDLE]
```

フロー図:

```
TRIAGE -> IDLE -> INITIALIZE -> DESIGN_PLAN -> DESIGN_REVIEW -> DESIGN_APPROVED -> IMPLEMENTATION
                                    ^              |                                   |
                                    |              v                                   v
                                DESIGN_REVISE <----+                              CODE_TEST
                                                                                  |       |
                                                                                  v       v
                                                                          CODE_REVIEW  CODE_TEST_FIX
                                                                          |       |       |
                                                                          v       v       v
                                                                    CODE_REVISE CODE_APPROVED
                                                                                  |
                                                                                  v
                                                                        MERGE_SUMMARY_SENT
                                                                                  |
                                                                                  v
                                                                                DONE -> IDLE

  ※ DESIGN_REVIEW, CODE_TEST, CODE_TEST_FIX, CODE_REVIEW から BLOCKED に遷移可能
  ※ BLOCKED からは IDLE にのみ戻れる
  ※ CODE_REVISE からは CODE_TEST (再テスト) または CODE_REVIEW (テスト不要時) に遷移
```

### 4.1 各状態の詳細

| 状態 | 責任者 | やること | 次の状態への条件 |
|------|--------|----------|-----------------|
| TRIAGE | - | 振り分け処理 | IDLE へ遷移 |
| IDLE | - | 何もない | `gokrax start` で INITIALIZE へ |
| INITIALIZE | (自動) | プロジェクト初期化 | DESIGN_PLAN へ遷移 |
| DESIGN_PLAN | 実装担当 | Issue 本文を確認・修正し `plan-done` | 全 Issue に `design_ready` フラグ |
| DESIGN_REVIEW | レビュアー | 設計レビュー、`gokrax review` で投稿 | `min_reviews` 件集まる |
| DESIGN_REVISE | 実装担当 | P0 指摘に基づき Issue 本文を修正、`design-revise` | 全対象 Issue に `design_revised` フラグ |
| DESIGN_APPROVED | (自動通過) | 即座に IMPLEMENTATION に遷移 | - |
| IMPLEMENTATION | CC (自動) | CC 自動起動 -> Plan + Impl -> `commit` | 全 Issue に `commit` ハッシュ |
| CODE_TEST | (自動) | テスト自動実行 (`_start_code_test`) | テスト結果に応じて CODE_REVIEW / CODE_TEST_FIX / BLOCKED |
| CODE_TEST_FIX | CC (自動) | テスト失敗の修正 | 修正完了後 CODE_TEST へ再遷移 |
| CODE_REVIEW | レビュアー | コードレビュー、`gokrax review` で投稿 | `min_reviews` 件集まる |
| CODE_REVISE | 実装担当 | P0 指摘に基づきコード修正 -> `code-revise --hash` | 全対象 Issue に `code_revised` フラグ |
| CODE_APPROVED | (自動通過) | 即座に MERGE_SUMMARY_SENT に遷移 | - |
| MERGE_SUMMARY_SENT | M (人間) | #gokrax のサマリーに「OK」リプライ | M の OK リプライ検出 |
| DONE | (自動) | git push + issue close -> IDLE | 自動遷移 |
| BLOCKED | M (人間) | 手動復旧が必要 | `transition --force --to IDLE` |

### 4.2 自動通過状態

- **DESIGN_APPROVED**: watchdog が検出次第、即座に IMPLEMENTATION に遷移。CC 自動起動。
- **CODE_APPROVED**: watchdog が検出次第、即座に MERGE_SUMMARY_SENT に遷移。サマリー自動投稿。
- **DONE**: git push + issue close -> IDLE

### 4.3 REVISE ループ

- P0/REJECT が含まれる場合、REVIEW -> REVISE に遷移
- REVISE 完了後、APPROVE 以外のレビュー (P0/P1/P2/REJECT) はクリアされる (APPROVE のみ保持)
- 再レビュー時、既に APPROVE 済みの Issue x レビュアーの組はスキップ
- **最大 4 サイクル** (`MAX_REVISE_CYCLES = 4`)。超過すると BLOCKED

## 5. レビューモード

プロジェクトごとに設定。使用するレビュアーの構成と最低レビュー数を制御。

| モード | メンバー | min_reviews | grace_period_sec |
|--------|----------|-------------|-----------------|
| full | pascal, dijkstra, euler, basho | 4 | 0 |
| standard | pascal, euler, dijkstra | 3 | 0 |
| lite3_woOpus | pascal, euler, basho | 3 | 0 |
| lite3_woGoogle | euler, dijkstra, basho | 3 | 0 |
| lite3_woOpenAI | pascal, dijkstra, basho | 3 | 0 |
| lite | basho, pascal | 2 | 0 |
| cheap | basho, leibniz, hanfei | 3 | 0 |
| min | pascal | 1 | 0 |
| skip | (なし) | 0 | 0 (自動承認) |

> **既知の制限**: spec mode の `should_continue_review()` が参照する `MIN_VALID_REVIEWS_BY_MODE` に
> `cheap`, `lite3_woOpus`, `lite3_woOpenAI` のエントリが存在しない。これらのモードを spec mode で
> 使用すると `ValueError` が発生する。通常使用では `gokrax spec start --review-mode` の argparse が
> 選択肢を `full/standard/lite/min` に制限しているため問題にならないが、コード上の不整合として注意。

## 6. タイムアウト

### BLOCK_TIMERS

| 状態 | 制限時間 | 延長可能 (EXTENDABLE_STATES) |
|------|---------|------------------------------|
| DESIGN_PLAN | 1800 秒 (30 分) | yes |
| DESIGN_REVIEW | 3600 秒 (60 分) | no |
| DESIGN_REVISE | 1800 秒 (30 分) | yes |
| IMPLEMENTATION | 7200 秒 (120 分) | yes |
| CODE_TEST | 600 秒 (10 分) | no |
| CODE_TEST_FIX | 3600 秒 (60 分) | yes |
| CODE_REVIEW | 3600 秒 (60 分) | no |
| CODE_REVISE | 1800 秒 (30 分) | yes |

### タイムアウト関連定数

| 定数 | 値 | 説明 |
|------|---|------|
| NUDGE_GRACE_SEC | 300 秒 | 遷移直後はこの期間催促しない |
| EXTEND_NOTICE_THRESHOLD | 300 秒 | 残り時間がこの値未満で延長案内を催促に付加 |
| INACTIVE_THRESHOLD_SEC | 303 秒 | この秒数更新がなければ非アクティブ扱い |

### timeout_extension

- pipeline JSON の `timeout_extension` フィールド (int, 単位: 秒)
- engine/fsm.py で `block_sec += data.get("timeout_extension", 0)` として加算される
- 延長は `gokrax extend --pj <PJ> --by 600` (デフォルト 600 秒)
- 延長回数は `extend_count` で管理。フェーズごとに DONE 時にリセット

## 7. watchdog 動作

### 7.0 watchdog-loop.sh

- 実行方法: `watchdog-loop.sh` で 5 秒おきにポーリング
- PID ファイル: `/tmp/gokrax-watchdog-loop.pid`
- ロックファイル: `/tmp/gokrax-watchdog-loop.lock`

### 7.1 メインループ

1. `PIPELINES_DIR` の全 `*.json` をスキャン
2. `enabled=false` ならスキップ
3. `check_transition()` で次のアクションを判定 (純粋関数、副作用なし)
4. Double-Checked Locking: ロック内で再判定 + 遷移
5. ロック外で通知 (Discord, エージェント送信)

### 7.2 エージェント送信方法

`send_to_agent()` と `send_to_agent_queued()` は同一関数 (後者はエイリアス)。
二層アーキテクチャで Gateway に `chat.send` を送信する。

| パス | 条件 | 実装 | device identity | auth mode |
|---|---|---|---|---|
| CLI (primary) | params < OS 閾値 | `openclaw gateway call` | ランタイム処理 | 全 mode |
| WS direct (fallback) | params >= OS 閾値 | `websocket-client` | 省略 (loopback) | token のみ |

OS ごとの CLI 引数サイズ上限 (`_get_max_cli_arg_bytes`):

| OS | 閾値 |
|---|---|
| Linux | 120,000 bytes |
| macOS | 900,000 bytes |
| Windows | 30,000 bytes |

- collect キューに積まれ、run 完了後に followup turn として処理される
  - 即時性より abort 回避を優先する設計。/new やレビュー依頼も followup turn として処理される
- 改行を保持する
- `dist/` 内部ファイルへの依存なし
- auth token 取得 (WS パス用): 環境変数 `OPENCLAW_GATEWAY_TOKEN` -> `~/.openclaw/openclaw.json` の `gateway.auth.token`

### 7.3 催促

- **実装担当**: 非アクティブ (INACTIVE_THRESHOLD_SEC=303 秒以上更新なし) の場合のみ `"continue"` を `send_to_agent_queued()` で送信
- **レビュアー**: 未完了レビュアーに `"continue"` を `send_to_agent_queued()` で送信。送信失敗時は 10 分後にリトライ
- CC 実行中 (`/proc/<pid>` 存在) はアクティブ扱い

### 7.4 CC 自動起動 (IMPLEMENTATION のみ)

- DESIGN_APPROVED -> IMPLEMENTATION 遷移時に `run_cc=True` -> `_start_cc()` で非同期起動
- CC が死んだ場合 (`_is_cc_running()=False`): watchdog の次サイクルで再起動
- **DESIGN_PLAN では CC 自動起動しない。** 実装担当が手動で Issue 確認 -> `plan-done` する

### 7.5 /new 送信タイミング

- **DESIGN_PLAN 遷移時**: レビュアー全員にセッションリセット (`/new`) を送信
- **IMPLEMENTATION 遷移時**: 同上 + 実装担当もリセット (PJ 変更時のみ)
- **REVISE -> REVIEW 遷移時**: `/new` は送信しない (コンテキスト維持)

### 7.6 Discord 通知

- 全状態遷移を `#gokrax` に投稿 (形式: `[PJ] OLD -> NEW (timestamp)`)
- DESIGN_PLAN 開始時のみ Issue 一覧を別メッセージで投稿
- CC 進捗: Plan 開始 -> Plan 完了 -> Impl 開始 -> Impl 完了
- マージサマリー: 全 Issue x 全レビュアーの判定を一覧投稿

## 8. pipeline JSON 構造

pipeline JSON のフィールドは 3 カテゴリに分類される。

### 8.1 初期化フィールド (cmd_init / cmd_start で設定)

| フィールド | 型 | 説明 |
|---|---|---|
| project | str | プロジェクト名 |
| gitlab | str | GitLab リポジトリパス |
| repo_path | str | ローカルリポジトリパス |
| state | str | 現在の状態 |
| enabled | bool | watchdog 監視対象か |
| batch | list[dict] | Issue バッチ (最大 MAX_BATCH=5) |
| review_mode | str | レビューモード名 |
| implementer | str | 実装担当エージェント名 |
| automerge | bool | 自動マージ有効か |
| history | list[dict] | 遷移履歴 (最大 MAX_HISTORY=100) |
| created_at | str | 作成日時 |
| updated_at | str | 更新日時 |

### 8.2 動的フィールド (実行中に追加・更新)

| フィールド | 型 | 説明 |
|---|---|---|
| cc_pid | int \| null | CC プロセス ID |
| cc_session_id | str \| null | CC セッション ID |
| design_revise_count | int | 設計 REVISE サイクル回数 |
| code_revise_count | int | コード REVISE サイクル回数 |
| summary_message_id | str \| null | マージサマリーの Discord メッセージ ID |
| skip_cc_plan | bool | CC Plan フェーズをスキップ |
| skip_test | bool | CODE_TEST をスキップ |
| keep_ctx_batch | bool | バッチ間コンテキスト保持 |
| keep_ctx_intra | bool | バッチ内コンテキスト保持 |
| base_commit | str \| null | ベースコミットハッシュ |
| p2_fix | bool | P2 修正モード |
| comment | str \| null | コメント (CC に渡す指示等) |
| timeout_extension | int | タイムアウト延長秒数 |
| extend_count | int | 延長回数 |
| excluded_reviewers | list[str] | 除外レビュアーリスト |
| min_reviews_override | int \| null | min_reviews の上書き値 |
| test_result | str \| null | テスト結果 |
| test_output | str \| null | テスト出力 |
| test_retry_count | int | テストリトライ回数 |
| test_baseline | dict \| null | pytest ベースラインデータ |

### 8.3 spec mode フィールド

| フィールド | 型 | 説明 |
|---|---|---|
| spec_mode | bool | spec mode 有効フラグ (commands/spec.py cmd_spec_start で設定) |
| spec_config | dict | spec mode 設定 (pipeline_io.py default_spec_config で初期化) |

spec_config の詳細は `docs/spec_mode_spec.md` を参照。

### 8.4 サンプル

```json
{
  "project": "BeamShifter",
  "gitlab": "atakalive/BeamShifter",
  "repo_path": "/mnt/s/wsl/work/project/BeamShifter",
  "state": "IDLE",
  "enabled": false,
  "implementer": "kaneko",
  "review_mode": "standard",
  "automerge": false,
  "batch": [
    {
      "issue": 17,
      "title": "Issue title",
      "commit": null,
      "cc_session_id": null,
      "design_ready": false,
      "design_reviews": {},
      "code_reviews": {},
      "design_revised": false,
      "code_revised": false,
      "added_at": "..."
    }
  ],
  "history": [{"from": "IDLE", "to": "DESIGN_PLAN", "at": "...", "actor": "cli"}],
  "cc_pid": null,
  "cc_session_id": null,
  "timeout_extension": 0,
  "extend_count": 0,
  "design_revise_count": 0,
  "code_revise_count": 0,
  "summary_message_id": null,
  "created_at": "...",
  "updated_at": "..."
}
```

## 9. verdict 定義

| Verdict | 意味 | 効果 |
|---------|------|------|
| APPROVE | 承認 | カウント対象。再レビュー時スキップ |
| P0 | 必須修正 (ブロッカー) | REVISE 遷移トリガー。revise 後クリアされる |
| P1 | 軽微な指摘 (ブロックしない) | カウント対象。revise 後クリアされる |
| P2 | 微細な指摘 (改善提案) | カウント対象。revise 後クリアされる |
| REJECT | 却下 | P0 と同等 |

有効な verdict: `VALID_VERDICTS = ["APPROVE", "P0", "P1", "P2", "REJECT"]`
フラグ付き verdict: `VALID_FLAG_VERDICTS = ["P0", "P1", "P2"]`

## 10. テスト原則

### テストは絶対に本番環境に影響を与えてはならない

これは最優先ルール。テストが本番の watchdog、crontab、pipeline JSON、Discord 通知、エージェントセッションに触れることは許されない。

#### 必須の隔離措置 (conftest.py `_block_external_calls`)

| 対象 | mock 方法 | 理由 |
|---|---|---|
| `notify.post_discord` | `return_value="mock-msg-id"` | Discord API を叩かない |
| `notify.send_to_agent` | `return_value=True` | Gateway CLI/WS を叩かない |
| `notify.send_to_agent_queued` | `return_value=True` | 同上 (エイリアス) |
| `notify.ping_agent` | `return_value=True` | ping を叩かない |
| `watchdog.send_to_agent` | `return_value=True` | 同上 |
| `watchdog.send_to_agent_queued` | `return_value=True` | 同上 |
| `watchdog.ping_agent` | `return_value=True` | 同上 |
| `engine.reviewer._reset_reviewers` | `return_value=[]` | レビュアーリセットを実行しない |
| `engine.reviewer._reset_short_context_reviewers` | mock 化 | 同上 |
| `time.sleep` | mock 化 | テスト累積タイムアウト防止 |
| `config.PIPELINES_DIR` | `tmp_path` に monkeypatch | 本番の pipeline JSON に触れない |
| `config.LOG_FILE` / `watchdog.LOG_FILE` | `tmp_path` にリダイレクト | 本番ログを汚さない |

#### 新しい外部副作用を追加するとき

1. 本番環境に影響する関数 (ファイル書き込み、プロセス操作、API 呼び出し) を追加したら、**conftest.py に mock を追加すること**
2. テストを走らせた後、本番の watchdog が生きてることを確認すること: `cat /tmp/gokrax-watchdog-loop.pid && ps -p $(cat /tmp/gokrax-watchdog-loop.pid)`

#### テストの禁止事項

- `time.sleep()` をテストコードで直接呼ぶな。conftest で `time.sleep` はグローバルにモック済み
- sleep の動作を検証したい場合は `patch("time.sleep") as mock_sleep` で呼び出し回数・引数を assert する
- 外部通信 (Discord, agent 送信) はテストで実行するな。conftest の `_block_external_calls` でモック済み
- `_reset_reviewers` / `_reset_short_context_reviewers` はテストで実行するな。conftest でモック済み

#### 事故記録

- **2026-02-25**: `_stop_loop_if_idle()` がテスト中に本番の watchdog-loop.sh を殺した。テスト用 tmp_path には disabled PJ しかないため「全 PJ disabled」と誤判定。IMPLEMENTATION 中の gokrax パイプラインが停止した

## 11. 禁止事項

1. **pipeline JSON の直接編集禁止。** 必ず gokrax CLI または `pipeline_io.update_pipeline()` 経由で操作する
2. **実装担当が自分の設計/実装をレビュー (APPROVE) してはならない**
3. **レビュアーが `plan-done`, `commit`, `design-revise`, `code-revise` を実行してはならない** (ロール違反)
4. **DESIGN_PLAN で CC を手動起動してはならない。** Issue 確認は実装担当の責務
5. **watchdog 無効時に手動で状態遷移する場合は `--force` フラグが必要**
6. **CODE_TEST / CODE_TEST_FIX の結果を手動で改竄してはならない。** テストはパイプラインが自動実行する

## 12. タスクキュー

### 概要

`task_queue.py` がタスクキューを管理する。キューファイルは `gokrax-queue.txt`。

### コマンド

| コマンド | 説明 |
|---|---|
| qrun | キューの先頭タスクを実行 |
| qadd | キューにタスクを追加 |
| qdel | キューからタスクを削除 |
| qedit | キュー内タスクを編集 |
| qstatus | キューの状態を表示 |

### キュー行フォーマット

`parse_queue_line()` のパースフォーマット:

```
PROJECT ISSUES [MODE] [OPTIONS...]
```

| オプション | 説明 |
|---|---|
| automerge | 自動マージ有効 |
| plan=MODEL | Plan フェーズのモデル指定 |
| impl=MODEL | Impl フェーズのモデル指定 |
| comment=TEXT | CC への指示コメント |
| keep-ctx-batch | バッチ間コンテキスト保持 |
| keep-ctx-intra | バッチ内コンテキスト保持 |
| p2-fix | P2 修正モード |
| skip-cc-plan | CC Plan フェーズスキップ |
| skip-test | CODE_TEST スキップ |

- キュー操作は `fcntl` ロックでアトミックに実行される

## 13. CODE_TEST ゲート

### 概要

IMPLEMENTATION 完了後、CODE_REVIEW の前にテストを自動実行するゲート。`engine/cc.py` で実装。

### 動作

- `_start_code_test()`: テストコマンドをバックグラウンドで実行
- `_poll_code_test()`: テスト結果をポーリングで確認
- テスト成功: CODE_REVIEW へ遷移
- テスト失敗: CODE_TEST_FIX へ遷移 (CC が自動修正を試行)
- 最大リトライ回数: `MAX_TEST_RETRY = 4`

### テスト設定

- `TEST_CONFIG` でプロジェクトごとのテスト構成を定義
- `test_baseline` フィールドに pytest ベースラインデータを保持
- `skip_test=True` でテストゲートをスキップ可能

## 14. settings.py override

### 仕組み

`config/__init__.py` の末尾で `settings.py` を動的にロードし、大文字の変数を config のグローバルに上書きする。

```python
_settings_path = Path(os.environ["GOKRAX_SETTINGS"]) if "GOKRAX_SETTINGS" in os.environ \
    else Path(__file__).resolve().parent.parent / "settings.py"
if _settings_path.exists():
    _spec = _importlib_util.spec_from_file_location("_gokrax_settings", _settings_path)
    _settings_mod = _importlib_util.module_from_spec(_spec)
    _spec.loader.exec_module(_settings_mod)
    for _attr in dir(_settings_mod):
        if _attr.isupper() and not _attr.startswith("_"):
            globals()[_attr] = getattr(_settings_mod, _attr)
```

### 使い方

- プロジェクトルートに `settings.py` を置き、上書きしたい定数を大文字で定義する
- 環境変数 `GOKRAX_SETTINGS` でパスを指定することもできる
- 上書き対象: AGENTS, REVIEWER_TIERS, REVIEW_MODES, BLOCK_TIMERS, MAX_REVISE_CYCLES 等、config で定義されている大文字定数全て
- 本仕様書の全定数値は config のデフォルト値。settings.py で上書きされている場合は実際の値が異なる

## 15. messages テンプレート

### 概要

`messages/` ディレクトリにプロンプト・通知テンプレートを外部化。

### 構造

```
messages/
  __init__.py    -- render() エントリポイント
  ja/dev/        -- 通常モード (design_plan, code_review, code_revise, implementation, code_test_fix, blocked 等)
  ja/spec/       -- spec mode (review, revise, approved 等)
```

### 使い方

- `messages.render()` を呼び出してテンプレートを取得する
- テンプレートカテゴリ: design_plan, design_review, code_review, code_revise, implementation, code_test_fix, blocked 等

## 16. spec mode 概要

spec mode は通常の開発パイプラインとは別系統の状態遷移を持つ。仕様書のレビュー・改訂・Issue 起票を自動化する。

- 状態: `SPEC_STATES` (config/states.py で定義)
- 遷移: `SPEC_TRANSITIONS` (config/states.py で定義)
- 遷移判定: `engine/fsm_spec.py` の `check_transition_spec()`
- CLI: `commands/spec.py` の `cmd_spec_start` 等
- spec_config の初期化: `pipeline_io.default_spec_config()`

詳細は `docs/spec_mode_spec.md` を参照。
