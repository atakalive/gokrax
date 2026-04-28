# gokrax -- 開発パイプライン仕様書

> 現行コード (2026-04-27 時点) に基づく正式仕様。全エージェントはこの文書に従うこと。
> 定数値は config のデフォルト値。settings.py で上書き可能 (14 章参照)。

## 1. 概要

gokrax は **Issue -> 設計 -> 実装 -> テスト -> レビュー -> マージ** のパイプラインを管理する CLI + watchdog システム。LLM を使わない純粋なオーケストレーターで、pipeline JSON を状態マシンとして駆動する。

## 2. アーキテクチャ

```
gokrax.py            -- CLI エントリポイント + watchdog loop 管理 + main()
commands/dev/       -- 通常モード CLI コマンド (cmd_start, cmd_review, etc.)
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
  backend.py           -- バックエンドディスパッチ (openclaw/pi/cc/gemini/kimi 振り分け)
  backend_openclaw.py  -- openclaw バックエンド (Gateway CLI 経由)
  backend_pi.py        -- pi バックエンド (pi CLI 経由)
  backend_cc.py        -- cc バックエンド (claude CLI 経由)
  backend_gemini.py    -- gemini バックエンド (gemini CLI 経由, oneshot)
  backend_kimi.py    -- kimi バックエンド (kimi CLI 経由)
  backend_types.py     -- バックエンド戻り値型 (SendResult: OK/BUSY/FAIL)
  gemini_quota.py      -- Gemini Pro クォータ検出 & fallback キャッシュ
  cleanup.py           -- バッチ状態クリーンアップ共通関数
  filter.py            -- プロジェクト/著者フィルタリング (許可著者、Issue/コメント検証)
watchdog.py           -- watchdog ループ + Discord コマンド処理
notify.py             -- エージェント通知 + Discord 投稿 (CLI 経由)
pipeline_io.py        -- JSON 読み書き (排他ロック + atomic write)
spec_review.py        -- spec レビューパース・統合
spec_revise.py        -- spec 改訂依頼・セルフレビュー
spec_issue.py         -- Issue 分割・起票・キュー生成
task_queue.py         -- タスクキュー管理 (qrun/qadd/qdel/qedit)
messages/             -- テンプレートメッセージ (render() 経由)
  __init__.py          -- render() エントリポイント
  ja/dev/              -- 通常モード (日本語)
  ja/spec/             -- spec mode (日本語)
  en/dev/              -- 通常モード (英語)
  en/spec/             -- spec mode (英語)
settings.py           -- ユーザー設定 (config override)
```

- **エージェント通信**: `engine/backend.py` がルーターとして機能し、エージェントごとに `openclaw`、`pi`、`cc`、`gemini`、`kimi` バックエンドに振り分ける。`settings.py` の `DEFAULT_AGENT_BACKEND` と `AGENT_BACKEND_OVERRIDE` で制御。
- **pipeline JSON**: `~/.gokrax/pipelines/<project>.json`
- **watchdog**: `watchdog-loop.sh` で 20 秒おきにポーリング (後述 7 章)
- **Discord 通知先**: Discord 通知チャンネル（`settings.py` の `DISCORD_CHANNEL` で設定）

## 3. ロール定義

### 3.1 実装担当 (Implementer)

- DESIGN_PLAN フェーズで Issue 本文を確認・修正し、`plan-done` を実行する
- CODE_REVISE フェーズで P0 指摘に基づきコードを手動修正し、`code-revise` を実行する (commit 記録 + revise 完了を一発で)
- IMPLEMENTATION フェーズでは CC が自動起動される (実装担当が手動でやるのではない)

エージェント一覧は `settings.py` (`AGENTS` 辞書) で定義する。デフォルト値は `settings.example.py` を参照。address フォーマットは `agent:<name>:main`。

### 3.2 レビュアー (Reviewers)

レビュアーは3つの tier に分類される。tier のメンバーは `settings.py` の `REVIEWER_TIERS` で定義する。デフォルト構造は `settings.example.py` を参照。

| Tier | メンバー |
|---|---|
| regular | [] |
| free | [] |
| short-context | [] |

- DESIGN_REVIEW または CODE_REVIEW でレビュー依頼を受け取る
- `gokrax review` コマンドで verdict (APPROVE / P0 / P1 / P2) を投稿する
- **自分が設計・実装したものを自分でレビューしてはならない**
- レビュアーは実装担当ではない。レビュアーが `plan-done`, `commit`, `design-revise`, `code-revise` を実行することはない

### 3.3 承認者 (Owner)

- MERGE_SUMMARY_SENT で Discord 通知チャンネルにサマリーが投稿される。以下のいずれかで DONE に遷移:
  - Discord サマリーに「OK」リプライ
  - `gokrax ok --pj <project>` CLI コマンド (`commands/dev/` `cmd_ok`)
  - `automerge` フラグ有効時は自動遷移
- `gokrax start` や `gokrax transition --force` 等の制御コマンドを実行する

### 3.4 CC (Claude Code)

- IMPLEMENTATION フェーズで watchdog が自動起動する
- Plan (model: sonnet) -> Impl (model: sonnet) の 2 段階
- CC 完了後、自動で `gokrax commit` を実行する
- **CC は IMPLEMENTATION でのみ使用。他のフェーズでは使わない**
- `AGENTS` に複数の implementer を定義すれば、`implementer` フィールドで切り替え可能

## 4. 状態マシン

### 有効状態 (VALID_STATES)

```
IDLE, INITIALIZE,
DESIGN_PLAN, DESIGN_REVIEW, DESIGN_REVIEW_NPASS, DESIGN_REVISE, DESIGN_APPROVED,
ASSESSMENT, IMPLEMENTATION,
CODE_TEST, CODE_TEST_FIX,
CODE_REVIEW, CODE_REVIEW_NPASS, CODE_REVISE, CODE_APPROVED,
MERGE_SUMMARY_SENT, DONE, BLOCKED
```

> **注意**: spec mode が有効な場合、VALID_STATES は `sorted(set(VALID_STATES + SPEC_STATES))` で
> アルファベット順にマージされる。

### 遷移テーブル (VALID_TRANSITIONS)

```
IDLE         -> [INITIALIZE]
INITIALIZE   -> [DESIGN_PLAN, DESIGN_APPROVED]
DESIGN_PLAN  -> [DESIGN_REVIEW]
DESIGN_REVIEW -> [DESIGN_APPROVED, DESIGN_REVISE, BLOCKED, DESIGN_REVIEW_NPASS]
DESIGN_REVIEW_NPASS -> [DESIGN_APPROVED, DESIGN_REVISE, DESIGN_REVIEW_NPASS]
DESIGN_REVISE -> [DESIGN_REVIEW]
DESIGN_APPROVED -> [ASSESSMENT, IMPLEMENTATION]
ASSESSMENT   -> [IMPLEMENTATION, IDLE]
IMPLEMENTATION  -> [CODE_TEST, CODE_REVIEW]
CODE_TEST       -> [CODE_REVIEW, CODE_TEST_FIX, BLOCKED]
CODE_TEST_FIX   -> [CODE_TEST, BLOCKED]
CODE_REVIEW  -> [CODE_APPROVED, CODE_REVISE, BLOCKED, CODE_REVIEW_NPASS]
CODE_REVIEW_NPASS -> [CODE_APPROVED, CODE_REVISE, CODE_REVIEW_NPASS]
CODE_REVISE  -> [CODE_TEST, CODE_REVIEW]
CODE_APPROVED -> [MERGE_SUMMARY_SENT]
MERGE_SUMMARY_SENT -> [DONE]
DONE         -> [IDLE]
BLOCKED      -> [IDLE]
```

フロー図:

```
IDLE -> INITIALIZE -> DESIGN_PLAN -> DESIGN_REVIEW -> DESIGN_APPROVED -> ASSESSMENT -> IMPLEMENTATION
                                    ^              |                                                  |
                                    |              v                                                  v
                                DESIGN_REVISE <----+                                             CODE_TEST
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
  ※ skip_design 有効時: INITIALIZE -> DESIGN_APPROVED (DESIGN_PLAN/REVIEW をスキップ)
  ※ skip_assess 有効時: DESIGN_APPROVED -> IMPLEMENTATION (ASSESSMENT をスキップ)
```

### 4.1 各状態の詳細

| 状態 | 責任者 | やること | 次の状態への条件 |
|------|--------|----------|-----------------|
| IDLE | - | 何もない | `gokrax start` で INITIALIZE へ |
| INITIALIZE | (自動) | プロジェクト初期化 | DESIGN_PLAN へ遷移 |
| DESIGN_PLAN | 実装担当 | Issue 本文を確認・修正し `plan-done` | 全 Issue に `design_ready` フラグ |
| DESIGN_REVIEW | レビュアー | 設計レビュー、`gokrax review` で投稿 | `min_reviews` 件集まる |
| DESIGN_REVISE | 実装担当 | P0 指摘に基づき Issue 本文を修正、`design-revise` | 全対象 Issue に `design_revised` フラグ |
| DESIGN_APPROVED | (自動通過) | skip_assess 有効時は即座に IMPLEMENTATION へ。無効時は ASSESSMENT へ | - |
| ASSESSMENT | CC (自動) | Issue ごとの難易度・ドメインリスクを判定 | 全 Issue 判定完了で IMPLEMENTATION へ。domain_risk 除外時は IDLE へ |
| IMPLEMENTATION | CC (自動) | CC 自動起動 -> Plan + Impl -> `commit` | 全 Issue に `commit` ハッシュ |
| CODE_TEST | (自動) | テスト自動実行 (`_start_code_test`) | テスト結果に応じて CODE_REVIEW / CODE_TEST_FIX / BLOCKED |
| CODE_TEST_FIX | CC (自動) | テスト失敗の修正 | 修正完了後 CODE_TEST へ再遷移 |
| CODE_REVIEW | レビュアー | コードレビュー、`gokrax review` で投稿 | `min_reviews` 件集まる |
| CODE_REVISE | 実装担当 | P0 指摘に基づきコード修正 -> `code-revise --hash` | 全対象 Issue に `code_revised` フラグ |
| CODE_APPROVED | (自動通過) | 即座に MERGE_SUMMARY_SENT に遷移 | - |
| MERGE_SUMMARY_SENT | Owner | Discord「OK」リプライ / gokrax ok CLI / automerge で DONE に遷移 | Owner の OK リプライ検出 |
| DONE | (自動) | git push + issue close -> IDLE | 自動遷移 |
| BLOCKED | Owner | 手動復旧が必要 | `transition --force --to IDLE` |

### 4.2 自動通過状態

- **DESIGN_APPROVED**: watchdog が検出次第、ASSESSMENT に遷移（skip_assess 有効時は直接 IMPLEMENTATION に遷移）。
- **ASSESSMENT**: CC が Issue ごとに複雑度レベルと domain risk を判定。判定完了で IMPLEMENTATION に遷移。domain risk 除外対象のみの場合は IDLE に遷移。
- **CODE_APPROVED**: watchdog が検出次第、即座に MERGE_SUMMARY_SENT に遷移。サマリー自動投稿。
- **DONE**: git push + issue close -> IDLE

### 4.3 REVISE ループ

- P0/REJECT が含まれる場合、REVIEW -> REVISE に遷移
- REVISE 完了後、APPROVE 以外のレビュー (P0/P1/P2/REJECT) はクリアされる (APPROVE のみ保持)
- 再レビュー時、既に APPROVE 済みの Issue x レビュアーの組はスキップ
- **最大 4 サイクル** (`MAX_REVISE_CYCLES = 4`)。超過すると BLOCKED

## 5. レビューモード

プロジェクトごとにデフォルト値を設定。実行バッチ毎の設定も可能。使用するレビュアーの構成と最低レビュー数を制御。

レビューモードは `settings.py` の `REVIEW_MODES` で定義する。デフォルト構造は `settings.example.py` を参照。

| モード | メンバー | min_reviews | grace_period_sec | n_pass |
|--------|----------|-------------|-----------------|--------|
| full | `settings.py` の `REVIEW_MODES` で定義 | 4 | 0 | — |
| standard | `settings.py` の `REVIEW_MODES` で定義 | 3 | 0 | — |
| lite | `settings.py` の `REVIEW_MODES` で定義 | 2 | 0 | — |
| min | `settings.py` の `REVIEW_MODES` で定義 | 1 | 0 | — |
| skip | (なし) | 0 | 0 (自動承認) | — |
| standard-x2 | `settings.py` の `REVIEW_MODES` で定義 | 3 | 0 | {reviewer1: 2, reviewer3: 2} |

### n_pass (Nパスレビュー設定)

`n_pass` はレビューモードに追加できるオプション設定。指定したレビュアーに複数パスのレビューを実行させる。

```python
"standard-x2": {
    "members": [],
    "min_reviews": 3,
    "n_pass": {"reviewer1": 2, "reviewer3": 2},
}
```

- `n_pass` に含まれないレビュアーはデフォルト 1 パス
- パス 1 は通常の DESIGN_REVIEW / CODE_REVIEW で実行される
- パス 2 以降は *_REVIEW_NPASS ステートで実行される

### Nパスレビュー

#### フロー

1. パス 1 は通常の DESIGN_REVIEW / CODE_REVIEW で完了する
2. `n_pass > 1` のレビュアーが存在する場合、*_REVIEW_NPASS に遷移する
3. NPASS レビュアーには軽量プロンプトが送信される（Issue 本文/diff の再送なし）
4. 全 NPASS パス完了後、`count_reviews()` で最終 verdict を集計する — 各レビュアーの最新 verdict を 1 票としてカウント（n_pass=1 のレビュアーも含む）
5. いずれかの提出済みレビュアーから P0/P1 → 即座に REVISE（タイムアウト待ち不要）
6. REVISE → REVIEW 後、パスカウンターはリセットされる。パス 1 から再開（NPASS に直接再突入しない）

#### 中間パスの GitLab Note 動作

- 中間パス（pass < target_pass）の APPROVE: GitLab note は **スキップ**
- 中間パスの P0/P1/P2: GitLab note は **投稿される**（開発者がフィードバックを確認できるようにするため）

#### タイムアウト

- NPASS は基底の REVIEW ステートと同じタイムアウトを使用する
- タイムアウト時: `count_reviews()` で全 verdict を収集（未完了 NPASS レビュアーはパス 1 の verdict を保持）し、`_resolve_review_outcome` で遷移先を決定する。P0/P1 → タイムアウト時も REVISE
- NPASS は BLOCKED に遷移 **しない**

#### 強制外部化ファイル

- CODE_REVIEW ステート突入時（`notify_reviewers` 内）でトリガーされる。キュー投入時ではない
- `n_pass > 1` のレビュアーがレビューモードに存在する場合、メッセージサイズに関係なく常にレビューデータをファイルに外部化する
- これにより NPASS プロンプトがファイルパスを参照できる
- 既存のキュー済みバッチは CODE_REVIEW に突入するまで影響を受けない

## 6. タイムアウト

### BLOCK_TIMERS

| 状態 | 制限時間 | 延長可能 (EXTENDABLE_STATES) |
|------|---------|------------------------------|
| DESIGN_PLAN | 1800 秒 (30 分) | yes |
| DESIGN_REVIEW | 3600 秒 (60 分) | no |
| DESIGN_REVISE | 1800 秒 (30 分) | yes |
| ASSESSMENT | 1200 秒 (20 分) | yes |
| IMPLEMENTATION | 7200 秒 (120 分) | yes |
| CODE_TEST | 600 秒 (10 分) | no |
| CODE_TEST_FIX | 3600 秒 (60 分) | yes |
| CODE_REVIEW | 3600 秒 (60 分) | no |
| CODE_REVISE | 1800 秒 (30 分) | yes |

### タイムアウト関連定数

| 定数 | 値 | 説明 |
|------|---|------|
| NUDGE_GRACE_SEC | 600 秒 | 遷移直後はこの期間催促しない |
| EXTEND_NOTICE_THRESHOLD | 300 秒 | 残り時間がこの値未満で延長案内を催促に付加 |
| INACTIVE_THRESHOLD_SEC | 603 秒 | 催促閾値: この秒数更新がなければ非アクティブ扱い。送信可否判定には使われない — 送信可否は live PID 所有権のみで判定する (#327) |
| INACTIVE_THRESHOLD_PLAN_SEC | 900 秒 | DESIGN_PLAN での実装者催促間隔 |
| BUSY_ESCALATION_SEC | 1800 秒 | spec mode: 30 分継続した `SendResult.BUSY` を 1 回の機械的失敗としてカウント (retry counter を 1 つ消費) |

### timeout_extension

- pipeline JSON の `timeout_extension` フィールド (int, 単位: 秒)
- engine/fsm.py で `block_sec += data.get("timeout_extension", 0)` として加算される
- 延長は `gokrax extend --pj <PJ> --by 600` (デフォルト 600 秒)
- 延長回数は `extend_count` で管理。フェーズごとに DONE 時にリセット

## 7. watchdog 動作

### 7.0 watchdog-loop.sh

- 実行方法: `watchdog-loop.sh` で 10 秒おきにポーリング
- PID ファイル: `/tmp/gokrax-watchdog-loop.pid`
- ロックファイル: `/tmp/gokrax-watchdog-loop.lock`
- 子 PGID ファイル: `/tmp/gokrax-watchdog-loop-child.pgid` — 現在の iteration の子 PGID。outer bash が trap 完了前に殺された場合に `_stop_loop` が iteration プロセスグループを fallback で SIGKILL するために使用する。
- SIGTERM 伝搬: `set -m` で job control を有効化し、各 iteration の子 (`flock(1)` + `python3 watchdog.py`) を独立 PGID で起動する。`_shutdown` トラップが PGID に SIGTERM を送り、`wait` で直接子を reap、残存する孫を SIGKILL することで、disable 後に orphan の `flock(1)` / `python3` が残らないようにする。
- PI backend: `engine/backend_pi.py` の Popen は `start_new_session=True` を使い、PI 送信 subprocess を iteration PGID から切り離して watchdog SIGTERM の巻き添えを免れさせる (CC/Gemini と同じ挙動)。

### 7.1 メインループ

1. `PIPELINES_DIR` の全 `*.json` をスキャン
2. `enabled=false` ならスキップ
3. `check_transition()` で次のアクションを判定 (純粋関数、副作用なし)
4. Double-Checked Locking: ロック内で再判定 + 遷移
5. ロック外で通知 (Discord, エージェント送信)

### 7.2 エージェント送信方法

エージェントへの送信は `engine/backend.py` の `send()` / `ping()` を経由する。

**Backend 解決 (`resolve_backend(agent_id)` — キャッシュ読み取りのみ、HTTP なし):**
1. `AGENT_BACKEND_OVERRIDE[agent_id]` があればそれ、なければ `DEFAULT_AGENT_BACKEND`。
2. 解決された backend が `gemini` の場合のみ `engine/gemini_quota.py:resolve_fallback()` を呼ぶ。これはキャッシュ `~/.gokrax/quota-cache/<agent_id>.json` を **読み取るだけ** で、active (`active=true`、`fallback_to ∈ {"pi","cc"}`、`until` が未来) なら fallback backend を返す。schema 違反 (例: `fallback_to` が valid set 外) のキャッシュは miss 扱いとなり、通常通り `gemini` が使われる。

**Backend ごとの挙動:**
- **openclaw**: `engine/backend_openclaw.py` — `openclaw gateway call` CLI 経由で Gateway に送信。
- **pi**: `engine/backend_pi.py` — `pi` CLI 経由で送信。アクティビティはセッションファイルの mtime で判定。
- **cc**: `engine/backend_cc.py` — `claude -p` CLI 経由で送信。**送信可否** はセッションの live PID 所有権 (`/proc/<pid>` + cmdline チェック) のみで判定する。live owner が居る場合 `send()` は **即座に** `SendResult.BUSY` を返す (待機も SIGTERM も行わない)。セッション JSONL mtime は催促/非アクティブ判定 (§7.3) には引き続き使うが、送信可否には使われなくなった (#327)。
- **gemini**: `engine/backend_gemini.py` — `gemini` CLI を oneshot プロセスとして起動して送信する（1 プロンプト = 1 プロセス）。`send()` は `subprocess.Popen(cwd=<agent profile dir>)` で `gemini` を起動する（Gemini CLI はセッションを cwd でスコープする）。アクティビティは pid ファイルに加え `/proc/<pid>` の存在と cmdline に `"gemini"` を含むことで判定する。セッション継続は `-r latest` を使用する。セッションは cwd 単位のため、エージェント間のセッション混在を避けるためにエージェントごとに独立した profile dir（`agents/<agent_id>/`）が必要。

バックエンドは `settings.py` の `DEFAULT_AGENT_BACKEND`（config デフォルト: `"openclaw"`、`settings.example.py` 推奨値: `"pi"`、4 種類: openclaw, pi, cc, gemini）で設定し、`AGENT_BACKEND_OVERRIDE` でエージェント単位の上書きが可能。

**送信時の Gemini Pro クォータ fallback (`should_fallback()`):**

`send()` が `gemini` に解決された場合、追加で `engine/gemini_quota.py:should_fallback(agent_id)` が呼ばれる。これは Code Assist Internal API (`cloudcode-pa.googleapis.com`) に HTTP リクエストを発行して Pro 使用率を更新し、閾値超過なら今回の send だけを fallback backend に振り直す。fallback が発動するのは `agents/config_gemini.json` の該当 agent エントリで以下を **全て** 満たすときのみ:
- `fallback: true`
- `fallback_backend ∈ {"pi", "cc"}`
- `model` に `"pro"` を含む (大文字小文字無視)
- Pro 使用率 ≥ `usage_threshold` (0–100、既定 95)

キャッシュファイル: `~/.gokrax/quota-cache/<agent_id>.json` の形は `{"active": true, "fallback_to": "pi"|"cc", "until": "<ISO-8601>", "reason": "..."}`。schema 違反のエントリは miss 扱い。

前提: Gemini OAuth クレデンシャル (`GEMINI_OAUTH_CREDS`) と Gemini `settings.json` (`security.auth.selectedType` 含む) が読める必要がある。watchdog 起動時に `engine/backend.py:validate_overrides()` と `engine/gemini_quota.py:validate_fallback_config()` が config を検証して、未知 agent や不正 fallback 設定があれば警告ログを出す。

**`SendResult` (3 値、`engine/backend_types.py`):** `engine/backend.py:send()` は `SendResult.{OK, BUSY, FAIL}` を返す。`notify.send_to_agent()` は互換のため bool ラッパーとして残る (`OK` のときのみ `True`)。新 API `notify.send_to_agent_with_status()` は `SendResult` をそのまま返し、BUSY と FAIL を区別したい呼び出し元 (spec mode、`docs/spec_mode_spec_ja.md` §10.1 参照) で使われる。

`send_to_agent()` と `send_to_agent_queued()` は同一関数 (後者はエイリアス)。
`openclaw gateway call` CLI 経由で Gateway に `chat.send` を送信する。

- CLI が device identity と全 auth mode を内部で処理する
- `MAX_CLI_ARG_BYTES` 未満の `params_json` 専用。それ以上のメッセージは呼び出し元でファイル外部化する

OS ごとの CLI 引数サイズ上限 (`_get_max_cli_arg_bytes`):

| OS | 閾値 | 根拠 |
|---|---|---|
| Linux | 120,000 bytes | MAX_ARG_STRLEN=131,072 (単一引数上限) |
| macOS | 900,000 bytes | ARG_MAX=1,048,576 (argv+envp 合計上限) |
| Windows | 30,000 bytes | CreateProcess=32,767 文字 |

- collect キューに積まれ、run 完了後に followup turn として処理される
  - 即時性より abort 回避を優先する設計。/new やレビュー依頼も followup turn として処理される
- 改行を保持する
- `dist/` 内部ファイルへの依存なし

### 7.3 催促

- **実装担当**: 非アクティブ (INACTIVE_THRESHOLD_SEC=603 秒以上更新なし) の場合のみ `"continue"` を `send_to_agent_queued()` で送信
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

- 全状態遷移を Discord 通知チャンネルに投稿 (形式: `[PJ] OLD -> NEW (timestamp)`)
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
| max_design_revise_cycles | int \| null | パイプライン固有の設計 revise サイクル上限 (--resume で加算) |
| max_code_revise_cycles | int \| null | パイプライン固有のコード revise サイクル上限 (--resume で加算) |

### 8.3 spec mode フィールド

| フィールド | 型 | 説明 |
|---|---|---|
| spec_mode | bool | spec mode 有効フラグ (commands/spec.py cmd_spec_start で設定) |
| spec_config | dict | spec mode 設定 (pipeline_io.py default_spec_config で初期化) |

spec_config の詳細は `docs/spec_mode_spec.md` を参照。

### 8.4 サンプル

```json
{
  "project": "MyProject",
  "gitlab": "username/MyProject",
  "repo_path": "/path/to/MyProject",
  "state": "IDLE",
  "enabled": false,
  "implementer": "implementer1",
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
| `notify.send_to_agent` | `return_value=True` | Gateway CLI を叩かない |
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
| keep-ctx-all | バッチ間・バッチ内コンテキスト保持 |
| p2-fix | P2 修正モード |
| skip-cc-plan | CC Plan フェーズスキップ |
| skip-test | CODE_TEST スキップ |
| skip-assess | ASSESSMENT スキップ |
| skip-design | DESIGN_PLAN/REVIEW スキップ |
| no-cc | CC を使わず実装者が直接実装 |
| exclude-high-risk | domain_risk=high の Issue を除外 |
| exclude-any-risk | domain_risk が none 以外の Issue を除外 |

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
  ja/dev/        -- 通常モード (日本語)
  ja/spec/       -- spec mode (日本語)
  en/dev/        -- 通常モード (英語)
  en/spec/       -- spec mode (英語)
```

### 使い方

- `messages.render()` を呼び出してテンプレートを取得する
- `settings.py` の `PROMPT_LANG`（デフォルト: `"en"`）でテンプレート言語を切り替える
- テンプレートカテゴリ: design_plan, design_review, code_review, code_revise, implementation, code_test_fix, blocked 等

## 16. spec mode 概要

spec mode は通常の開発パイプラインとは別系統の状態遷移を持つ。仕様書のレビュー・改訂・Issue 起票を自動化する。

- 状態: `SPEC_STATES` (config/states.py で定義)
- 遷移: `SPEC_TRANSITIONS` (config/states.py で定義)
- 遷移判定: `engine/fsm_spec.py` の `check_transition_spec()`
- CLI: `commands/spec.py` の `cmd_spec_start` 等
- spec_config の初期化: `pipeline_io.default_spec_config()`

詳細は `docs/spec_mode_spec.md` を参照。
