# gokrax

GitLab Issue からマージまでを逐次実行する自動開発パイプライン。

LLMエージェントによる設計・実装・レビューを状態機械で管理し、Issue を入力として受け取りレビュー済みのコードを出力する。

**リポジトリ:**
- **GitHub（公開用）:** <https://github.com/atakalive/gokrax> — GitLab から不定期に同期
- **GitLab（開発用）:** <https://gitlab.com/atakalive/gokrax> — 開発状況、[gokraxによるgokrax開発のデモ（日本語）](https://gitlab.com/atakalive/gokrax/-/work_items?sort=created_date&state=all&first_page_size=100)

---

## 目次

- [概要](#概要)
- [前提条件](#前提条件)
- [セットアップ](#セットアップ)
- [基本的な使い方](#基本的な使い方)
- [パイプラインの状態遷移](#パイプラインの状態遷移)
- [アンサンブルレビュー](#アンサンブルレビュー)
- [設定](#設定)
- [Spec Mode（仕様書パイプライン）](#spec-mode仕様書パイプライン)
- [ディレクトリ構成](#ディレクトリ構成)
- [アンインストール](#アンインストール)
- [Limitations](#limitations)
- [今後の課題](#今後の課題)
- [ライセンス](#ライセンス)

---

## 概要

gokraxは、自動開発パイプラインによる生成コードの高精度化を目的とする。同じバグであっても、プロジェクトごとにその重大さは異なる。gokraxはドメイン固有のリスクに応じて、許容できない誤りを含みながら動作するコードが導入される頻度を低減することを目指す。

使用者が主にやることは、機能追加等の提起と、その実現の難易度・重要性に対する力加減の調整（投入モデル選択など）の2点である。

gokraxは以下のパイプラインを自動で実行する：

```
Issue → 設計計画 → 設計レビュー → 実装 → コードレビュー → マージ
```

各段階は LLM エージェントが実行し、作業完了報告が集まって遷移条件が満たされると次の段階へ自動で進む。

レビュー段階で重大な指摘（P0/P1）が出た場合は修正ループに入り、必要数（基本は全員分）の承認を得るまで修正する。修正ループの反復回数が規定数に達した場合にはパイプラインを停止させる。

## 動作環境

- **OS**: Linux（WSL2 含む）、macOS
- **操作**: Discord 経由で OS を問わず可能
- **Python**: 3.11 以上。外部依存: `requests`, `PyYAML`
- **[OpenClaw](https://github.com/openclaw/openclaw)**: エージェント基盤。設計・修正・レビューを実行する LLM エージェントの認証・プロンプト送出のために使用
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)**: 実装作業エージェントとして内部で呼び出し
- **[glab CLI](https://gitlab.com/gitlab-org/cli)**: GitLab 操作（Issue 取得、コメント取得/投稿、Issue close）に使用
- **GitLab**: Issue トラッカーおよびコードホスティング。管理下プロジェクトへの git push 権限が必要（SSH 鍵 または HTTPS トークン）
- **Discord bot token**: 進捗通知用（推奨）。進捗監視用GUIは [WatcherB](https://gitlab.com/atakalive/WatcherB) を参照

### LLM プロバイダ

gokrax は特定の LLM プロバイダに依存せず、OpenClaw が認証可能なプロバイダは使用可能：

- Anthropic（Claude）
- Google（Gemini）
- OpenAI（ChatGPT）
- ローカルモデル（llama.cpp, vLLM 等）

実装エージェントとレビュアーエージェントにそれぞれ異なるプロバイダ・モデル・レビュー視点を割り当てられる。

### ハードウェア要件

gokrax 自体の計算負荷はほぼ無し（状態管理とプロセス起動のみ）。

## セットアップ

gokrax のセットアップは openclaw エージェント等と対話的に実行するのが簡単である。本READMEをエージェントに読ませれば、本項の内容を一通り設定できる。

- GitLab のプロジェクト作成とパス設定、ローカルリポジトリのパス設定
- gokraxへの既存プロジェクト追加
- Discord 通知先チャンネル設定
- レビュアー、実装者として使うエージェントの構成
- watchdog の設定（crontab）

gokrax 自体はシンプルな Python スクリプト群であり、インストール作業は `git clone` と依存パッケージのインストールのみである。アンインストール時は、状態遷移条件監視用のcrontabを削除する（後述）。

### gokrax のインストール

```bash
git clone https://gitlab.com/atakalive/gokrax.git
cd gokrax
pip install -r requirements.txt
python3 update_settings.py   # settings.example.py から settings.py を生成
# settings.py を編集（AGENTS, DISCORD設定等）

# PATH の通ったディレクトリにシンボリックリンクを作成（任意）
ln -s "$(pwd)/gokrax.py" ~/.local/bin/gokrax
```

`setup.py` は初回実行時に `settings.example.py` を `settings.py` にコピーする。アップデート後（`git pull`）に再実行すると、新しいバージョンで追加された設定項目だけを `settings.py` の末尾に追記する（既存の設定は変更しない）。

設定ファイル（`settings.py`）の主要項目については[設定](#設定)セクションを参照。

### Discord 通知の設定

gokrax は進捗通知を Discord チャンネルに投稿する（Discord API を直接使用）。

1. Discord サーバーに通知用チャンネルを作成する。  
   サーバーがまだない場合: [Discord](https://discord.com/) を開き、左サイドバーの「+」→「オリジナルを作成」→「自分と友達のため」→ サーバー名を入力して作成。  
   サーバー内にテキストチャンネル（例: `#gokrax`）を作成する。チャンネル一覧の「+」または右クリック →「チャンネルを作成」→ テキストチャンネルを選択。
2. 通知用の Discord bot を作成する（既存の bot を流用してもよい）。  
   新規作成の場合: [Discord Developer Portal](https://discord.com/developers/applications) を開き、「New Application」→ 任意の名前（例: `gokrax-notify`）で作成。  
   - 左メニュー「Bot」→「Privileged Gateway Intents」セクションで **Message Content Intent** をオンにする（マージ承認時にユーザーの返信内容を読み取るために必要）。  
   - 同じ「Bot」ページで「Reset Token」→ bot token を取得 → `settings.py` の `DISCORD_BOT_TOKEN` に設定する。
   - 左メニュー「OAuth2」→「OAuth2 URL Generator」→ SCOPES で `bot` にチェック → BOT PERMISSIONS で `Send Messages`、`Read Message History` にチェック → 生成された URL をブラウザで開き、bot をサーバーに招待する。
3. bot のユーザー ID を取得する。
   - ID コピーには Discord の開発者モードが必要なので、画面左下のユーザー設定 →「詳細設定」→「開発者モード」をオンにする。
   - サーバーのメンバー一覧から bot 名を右クリック →「ユーザー ID をコピー」→ `settings.py` の `ANNOUNCE_BOT_USER_ID` に設定する。
4. 通知先チャンネルの ID をチャンネル名の右クリックからコピーして、`settings.py` の `DISCORD_CHANNEL` に設定する。

### openclaw の準備

参考: openclaw: <https://github.com/openclaw/openclaw>

openclaw エージェントの認証を完了し、会話できるようにしておく。gokrax 参加エージェントには read, exec 権限を付与する（`gokrax review` などのコマンドを使用するため）。

### glab CLI のインストール

GitLab の Issue 操作に使用する。

```bash
# Linux
brew install glab        # Homebrew
# または https://gitlab.com/gitlab-org/cli/-/releases からバイナリ取得

glab auth login          # GitLab アカウントで認証
```

詳細: <https://gitlab.com/gitlab-org/cli>

### Claude Code CLI のインストール

実装フェーズで Claude Code CLI を使用する。

```bash
npm install -g @anthropic-ai/claude-code
claude /login   # Anthropic アカウントで認証
```

詳細: <https://docs.anthropic.com/en/docs/claude-code>

### 進捗監視用のdiscord通信ソフトのインストール（オプション）

discord #gokrax channelを読み書きするための簡素な常駐ツール。gokraxのためにdiscord画面を切り替える必要が無くなる。

WatcherB: <https://gitlab.com/atakalive/WatcherB>

1. WatcherB の説明に従ってインストールし、通知用とは別のdiscord botを作成する。
2. WatcherBからdiscordチャンネルへgokraxコマンドを投稿する場合、bot user IDを`settings.py` COMMAND_BOT_USER_ID に張り付ける。  


## 基本的な使い方

### 1. GitLab Issue を作成

Issue 本文に実装したい内容を記述する。担当エージェントが Issue 本文を読み、設計計画を詳細化する。

### 2. バッチ開始

```bash
# Issue 番号を指定して開始
python3 gokrax.py start --project MyProject --issue 1 2 3 --mode standard

# GitLab の open Issue 全件を自動取得して開始
python3 gokrax.py start --project MyProject --mode standard
```

`start` は以下を一括実行する：
- 指定 Issue のトリアージ（バッチへの投入）
- `DESIGN_PLAN` 状態への遷移
- watchdog の有効化
- 実際には、毎回コマンド実行は煩雑なのでキューファイルを作成してキュー実行させるのが簡単（後述）。

### 3. 以降は自動

watchdog が以下を自動で駆動する：

1. `IDLE` → `INITIALIZE`（エージェント初期化等）
2. `DESIGN_PLAN` → `DESIGN_REVIEW`（レビュアーに自動通知）
3. レビュー結果に応じて `DESIGN_APPROVED` or `DESIGN_REVISE`
4. `DESIGN_APPROVED` → `IMPLEMENTATION`（実装エージェントが自動起動）
5. `IMPLEMENTATION` → `CODE_REVIEW`（レビュアーに自動通知）
6. レビュー結果に応じて `CODE_APPROVED` or `CODE_REVISE`
7. `CODE_APPROVED` → `MERGE_SUMMARY_SENT`（Discord にサマリー投稿）
8. 人間が「OK」とリプライ or 自動マージ → `DONE`（git push + Issue close）
9. キュー実行中は (1) に戻る。

各エージェントは ``gokrax plan-done ...`` とか ``gokrax review ...`` などのコマンドを実行することでシステムに作業完了報告する。完了報告により遷移条件が整えば状態遷移する。特に問題がなければ、1件あたり30分程度で完了する。

コンテキストのリセット判定は、INITIALIZE, IMPLEMENTATION で行われる。実行時の設定により、実行バッチ内で継続、バッチ間で継続を指定できる。


## パイプラインの状態遷移

```
IDLE → INITIALIZE → DESIGN_PLAN → DESIGN_REVIEW ⇄ DESIGN_REVISE
                                        ↓
                                  DESIGN_APPROVED → ASSESSMENT → IMPLEMENTATION → CODE_TEST ⇄ CODE_FIX
                                                                          ↓
                                                                     CODE_REVIEW ⇄ CODE_REVISE
                                                                          ↓
                                                                     CODE_APPROVED → MERGE_SUMMARY_SENT → DONE → IDLE
```
[State Diagram (png)](docs/state-diagram.png)

設計の詳細は [docs/architecture.md](docs/architecture.md) を参照。

`INITIALIZE` はエージェントセッションの初期化（コンテキストリセット判定含む）等を行う状態。`ASSESSMENT` は設計承認後の判定ステート（現在はスケルトンで即通過）。`--skip-assess` 指定時は `DESIGN_APPROVED` から直接 `IMPLEMENTATION` に遷移する。`CODE_TEST` は現在実験的で、`skip_test` 設定時（デフォルト）は `IMPLEMENTATION` から直接 `CODE_REVIEW` に遷移する。キュー実行時は自動的に次バッチへ進む。

各状態にはタイムアウトが設定されている（`settings.py` の `BLOCK_TIMERS`）：

| 状態 | タイムアウト初期値 |
|------|----------------------|
| `DESIGN_PLAN` | 30 分 |
| `DESIGN_REVIEW` | 60 分 |
| `DESIGN_REVISE` | 30 分 |
| `ASSESSMENT` | 20 分 |
| `IMPLEMENTATION` | 120 分 |
| `CODE_TEST` | 10 分 |
| `CODE_TEST_FIX` | 60 分 |
| `CODE_REVIEW` | 60 分 |
| `CODE_REVISE` | 30 分 |

初期値でほぼタイムアウトしないが、`extend` コマンドで期限の延長も可能（最大2回）。

修正ループ（REVISE → REVIEW）の最大回数は `MAX_REVISE_CYCLES`（初期値: 4）で制限される。

### BLOCKED 状態への遷移と復帰

以下の場合、パイプラインは `BLOCKED` 状態に遷移して停止する：

- **タイムアウト超過**: 各状態の `BLOCK_TIMERS` を超えても完了報告が集まりきらない場合。
- **修正ループ上限到達**: レビューで P0 または P1 指摘が出て修正を繰り返し、`MAX_REVISE_CYCLES`（初期値: 4）に達した場合。設計レビュー・コードレビューの両方に適用。
- **テスト修正上限到達**: `CODE_TEST_FIX` が `MAX_TEST_RETRY`（初期値: 4）に達した場合。

`BLOCKED` からの復帰手順（DESIGN_REVIEW -> BLOCKED想定）：

```bash
# 1. 状態を戻す (問題なければ DESIGN_APPROVED 遷移も可)
python3 gokrax.py transition --to DESIGN_REVIEW --pj MyProject --force

# 2. Watchdog再起動
python3 gokrax.py enable --pj MyProject
```

全プロジェクトの状態を `IDLE` にする場合は、
```bash
python3 gokrax.py reset
```


## アンサンブルレビュー

gokrax のレビューは、複数の LLM レビュアーを並走させるアンサンブル方式を採用している。

### レビュー戦略

使用者の開発目的に沿ってレビューの網羅性を高める戦略として、シンプルな3つの観点を考えた。

1. 異なるモデル使用により、各モデルの癖や盲点を補完する
2. プロンプト調整によりレビュー観点を直交させる（使用者が設定）
3. 反復レビューにより見落としを回避する

gokraxは主に戦略 (1)、(2)に基づき生成コードの品質向上を目指している。使用モデル・レビュー観点が重複すれば現状でも(3)は部分的に達成されると考えられる。


### 小規模・ローカルモデルの活用

レビュアー枠では、大規模汎用モデルではなく、ドメイン知識偏重・外部知識を参照する小規模モデルであっても有用である可能性がある。gokraxは特定目的のレビューシステムへの組み込みという小規模モデルの応用先を提示する。


## Spec Mode（仕様書パイプライン）

新規プロジェクトの開始や、大きな機能の実装にあたって仕様書をレビュー・改訂するモード。仕様書の品質を担保してから Issue への分割・タスクキュー作成（力加減の調整）に進む。

```
仕様書投入 → SPEC_REVIEW ⇄ SPEC_REVISE → SPEC_APPROVED
  → ISSUE_SUGGESTION → ISSUE_PLAN → QUEUE_PLAN → SPEC_DONE
```

```bash
python3 gokrax.py spec start \
  --project MyProject \
  --spec docs/feature-spec.md \
  --implementer agent-name \
  --review-mode standard
```

仕様書はレビュアーのフィードバックを受けて改訂を繰り返し、全員が承認すると Issue 分割フェーズに入る。Issue 分割案の生成、実装順序の決定、キュー生成までを自動で行う。

`--auto-continue` を指定すると、承認後の人間確認ステップをスキップできる。

`--auto-qrun` を指定すると、キュー生成後に開発パイプラインへ自動進行する。


詳細は [docs/spec_mode_spec_ja.md](docs/spec_ja.md) を参照。

## ディレクトリ構成

デフォルトのパイプラインディレクトリ（`settings.py` の `PIPELINES_DIR` で変更可能）：

```
~/.openclaw/shared/
├── pipelines/
│   ├── MyProject.json       # プロジェクトごとのパイプライン状態
│   ├── MyProject.lock       # ファイルロック
│   └── gokrax-state.json    # グローバル状態（PJ間セッション管理）
└── gokrax-metrics.jsonl     # メトリクス（レビュー時間等の記録）
```

```
/tmp/
├── gokrax-watchdog.log          # watchdog ログ
├── gokrax-watchdog-loop.pid     # watchdog PID
└── gokrax-review/               # レビューデータ外部化ディレクトリ
    └── MyProject_reviewer1.md   # 大規模レビュー依頼のファイル
```

## CLI コマンド

詳細は [CLI.md](CLI.md) を参照。  

| コマンド | 説明 |
|---------|------|
| `init` | 新規プロジェクト作成（初回必須） |
| `status` | 全プロジェクトの状態表示 |
| `start` | バッチ開始（triage + 設計計画遷移 + watchdog 有効化） |
| `enable` / `disable` | watchdog 有効化・無効化（主にBLOCKEDからの復帰） |
| `transition` | 手動状態遷移（`--force` で強制） |
| `review-mode` | レビューモード変更（full, lite, etc.） |

| Queueコマンド（動作確認後はこちらがおすすめ） | 説明 |
|---------|------|
| `qstatus` | キュー内容の表示。キュー番号[0...N]の表示 |
| `qrun` | キューモードでバッチ開始（合図が検知され次第開始） |
| `qadd ...` | キューファイルにアイテムを追加 |
| `qdel N` | キューファイルのN番目アイテムを削除（qstatus表示番号に対応） |


## 設定

主要な設定項目（`settings.py`）：

### エージェント定義

openclaw agentsの設定を使用する。以降は、ここで設定されたエージェント名を使用する。
```python
AGENTS = {
    "implementer": "agent:implementer:main",  # 実装担当
    "gemini":      "agent:gemini:main",       # レビュアー（例: Gemini）
    "claude":      "agent:claude:main",       # レビュアー（例: Claude）
    "chatgpt":     "agent:chatgpt:main",      # レビュアー（例: ChatGPT）
    "local":       "agent:local:main",        # レビュアー（ローカルモデル）
}
```

### レビュアーティア

レビュアーはインフラの安定性に応じてティアに分類される：

```python
REVIEWER_TIERS = {
    "regular":       ["claude", "chatgpt", "gemini"],  # 安定接続、十分なコンテキスト長
    "short-context": ["local"],                        # コンテキスト長に制約あり（頻繁に新セッション化して対応）
    "free":          ["qwen-portal"],                  # 日次トークン上限あり、不安定
}
```

### レビューモード

```python
REVIEW_MODES = {
    "full":     {"members": ["gemini", "claude", "chatgpt", "local"], "min_reviews": 4},
    "standard": {"members": ["gemini", "chatgpt", "claude"],          "min_reviews": 3},
    "lite":     {"members": ["gemini", "local"],                      "min_reviews": 2},
    "lite3":    {"members": ["gemini", "local", "chatgpt"],           "min_reviews": 2, "grace_period_sec": 300},
    "min":      {"members": ["gemini"],                               "min_reviews": 1},
    "skip":     {"members": [],                                       "min_reviews": 0},
}
```

`min_reviews` 件の承認が集まった時点で次の状態に遷移する。

`min_reviews` の数が `members` より少ない場合（例: `lite3` は3人中2人）、`min_reviews` 到達後に `grace_period_sec` だけ追加レビューを待つ。猶予時間内に残りのレビュアーが応答すればそれも反映され、猶予を過ぎれば集まった分で遷移する。応答の遅いレビュアーや不安定なレビュアーを含めつつ、パイプラインを止めない運用ができる。

## プロンプトのカスタマイズ

gokrax がエージェントに送るプロンプトは `messages/{lang}/` 以下のテンプレートで定義されている。変更したいテンプレートだけを `messages_custom/` にコピーすると、デフォルトを上書きできる：

```bash
# 例: 設計レビューのプロンプトを変更
cp messages/ja/dev/design_review.py messages_custom/ja/dev/
# messages_custom/ja/dev/design_review.py を編集
```

`messages_custom/` は `.gitignore` に含まれているため、`git pull` で上書きされない。コピーしていないテンプレートはデフォルト（`messages/`）が使われる。

## アンインストール

gokrax を停止・削除するには以下を実行する：

```bash
# 1. 全プロジェクトを IDLE にリセット
python3 gokrax.py reset

# 2. crontab エントリを手動で削除（gokrax は常駐用の crontab エントリを登録する）
crontab -e
# gokrax-watchdog-loop と書かれた行を削除

# 3. 残存プロセスとファイルの削除
rm -f /tmp/gokrax-watchdog-loop.pid /tmp/gokrax-watchdog-loop.lock /tmp/gokrax-cron-spawn.lock /tmp/gokrax-watchdog.log
rm -rf /tmp/gokrax-review/

# 4. パイプライン状態ファイルの削除（必要に応じて）
rm -rf ~/.openclaw/shared/pipelines/

# 5. gokraxリポジトリの削除
rm -rf /path/to/gokrax
```

**注意:** `gokrax enable` を実行すると、watchdog の自動復旧のために crontab にエントリが追加される。`reset` はパイプライン状態のみリセットし、crontab エントリは削除しない。完全に停止するには手順 2 の crontab 手動削除が必要。


## Limitations

### アンサンブルレビューの有効性が未定量

gokrax のアンサンブルレビューが最終的なコード品質をどのような指標に基づき、どの程度改善できるかは、現時点で定量的に評価できていない。

必要な評価は、現在広く用いられている**Claude Code Opusへの丸投げをベースラインとした end-to-end 比較**となる。同一の題材を「レビューなしの Claude Code 単体」と「gokrax パイプライン」で実装し、最終コードの品質を開発者の目的に沿った指標で第三者レビュアーが比較すればよいと考えられる。

### コードレビューの限界

gokrax のレビューは静的なコードレビューであり、実機でしか発現しないバグなどは検出できない。

### 対応プラットフォーム

現時点で GitLab のみ対応（無料でprivate repositoryを利用できるため）。GitHub 対応は未実装。


## 今後の課題

### 操作・監視用のGUI

エージェントを介在させることでCLIの操作が簡単に行える一方で、パイプライン開始や状況に応じたレビューモードの設定は使用者が手動で行う必要がある。この部分の操作を簡略化するため、discord監視GUIツールの拡張を検討している（マウス操作でIssueとパラメータを選択して「キューに追加」「実行」ボタンを押すような形）。

### タスクキュー自動生成時のパラメータ調整

仕様書から一気通貫に実装する手順において、分割された各作業バッチに対するモデル選択などの自動調整は難しく、使用者の意図通りになりにくい。キュー生成提案を依頼する際のプロンプト調整により改善の可能性がある。

### テスト (CODE_TEST state)

IMPLEMENTATION -> CODE_TEST 遷移および CODE_REVISE -> CODE_TEST -> CODE_REVIEW 遷移とすることで、テストパス後にのみレビューする流れにできる。CODE_TEST状態は実装済みだが動作検証が不十分であるため、現状では実験的機能の扱いである（--skip-test: True を初期値としている）。

### SKILL

現在は新セッション開始時に一律でSKILL注入しており、DESIGN_REVIEW、CODE_REVIEW用途でスキルを区別していない。たとえば diff-reading スキルやバグ検出特化のスキルは CODE_REVIEW でのみ使いたい、などに対応できると良い。

### openclaw依存性 / 導入の敷居が高い

エージェントの認証とプロンプト送出、discord通知といった基本機能のためにgokraxはopenclawの枠組みを利用しているが、gokraxのもつ機能に対して導入の敷居が高い。今後、(1) 最低限のパッケージを使用、(2) gokraxを拡張して使用、といったバックエンド切り替えオプションの追加が考えられる。


## ライセンス

MIT License
