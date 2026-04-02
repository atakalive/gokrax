# gokrax

GitLab Issue からマージまでを逐次実行する自動開発パイプライン。

LLMエージェントによる設計・実装・レビューを状態機械で管理し、Issue を入力として受け取りレビュー済みのコードを出力する。

現在フィードバック収集中。

**リポジトリ:**
- **GitHub（安定版）:** <https://github.com/atakalive/gokrax> — GitLab から不定期に同期
- **GitLab（開発版）:** <https://gitlab.com/atakalive/gokrax> — 開発状況、[gokraxによるgokrax開発のデモ（日本語）](https://gitlab.com/atakalive/gokrax/-/work_items?sort=created_date&state=all&first_page_size=100)

---

## Features

- **全自動パイプライン** — Issue → 設計/レビュー → 実装 → レビュー → マージを状態機械で自動実行。複数 Issue の連続自動処理が可能
- **アンサンブルレビュー** — 複数の LLM レビュアーを並走させ、異なるプロバイダ・モデル・レビュー視点でコード品質を担保
- **リスク判定と保留** — プロジェクト固有のリスク定義に沿って、高リスク変更を自動で保留可能
- **Spec Mode** — 仕様書のレビュー・改訂から Issue への分割・キュー自動生成までを一気通貫で実行
- **Discord 通知・操作** — 進捗通知の受信と基本コマンド操作が場所を選ばず可能
- **作業履歴の自動蓄積** — 設計議論・レビュー指摘・修正履歴が Issue・コメントとして残る。判断経緯の確認にも、参考資料としても使える

**→ [Quick Start](docs/quick_start.md)**

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

gokrax は、自動開発パイプラインによる生成コードの品質にアプローチする。たとえ同じようなバグであっても、その重大さはプロジェクトごとに異なる。そうしたドメイン固有のリスクに応じて、gokrax は許容できない問題を含むコードが導入される頻度を減らすことを目指す。具体的には、複数の専門領域にまたがるプロジェクトを同時に推進する開発者が、すべてのコードを常に自分で把握し続けなくても、致命的なバグの混入を防げるようにするためのツールである。

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
- **エージェント基盤**: [openclaw](https://github.com/openclaw/openclaw) または [pi-coding-agent](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent)。設計・修正・レビューを実行する LLM エージェントの認証・プロンプト送出のために使用
- **GitLab**: Issue トラッカーおよびコードホスティング。管理下プロジェクトへの git push 権限が必要（SSH 鍵 または HTTPS トークン）
- **[glab CLI](https://gitlab.com/gitlab-org/cli)**: GitLab 操作（Issue 取得/編集、コメント取得/投稿、Issue close）に使用
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)**: 実装作業エージェントとして内部で呼び出し（推奨）
- **Discord bot token**: 進捗通知用（推奨）。進捗監視用GUIは [WatcherB](https://gitlab.com/atakalive/WatcherB) を参照

### LLM プロバイダ

gokrax は特定の LLM プロバイダに依存せず、openclaw、pi が認証可能なプロバイダは使用可能：

- Anthropic（Claude）
- Google（Gemini）
- OpenAI（ChatGPT）
- GitHub（GitHub-Copilot） etc.
- ローカルモデル（llama.cpp, vLLM 等、設定が必要）

実装エージェントとレビュアーエージェントにそれぞれ異なるプロバイダ・モデル・レビュー視点を割り当てられる。

### ハードウェア要件

gokrax 自体の計算負荷はほぼ無し（状態管理とプロセス起動のみ）。

## セットアップ

最小構成で素早く試したい場合は **[Quick Start](docs/quick_start.md)** を参照。

前提:
- GitLab アカウントと SSH 鍵の登録（パイプラインが自動で git push するため。手順は [Quick Start](docs/quick_start.md#ssh-鍵の登録gitlab) を参照）
- いずれかの LLM プロバイダのアカウント

セットアップの各セクションで必要な設定を完了した後、以下を実施することで gokrax が利用可能となる。

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
# "externally managed" エラーが出る場合: pip install -r requirements.txt --break-system-packages
python3 update_settings.py   # settings.example.py から settings.py を生成
# settings.py を編集（agent設定, DISCORD設定等）

# PATH の通った場所にシンボリックリンクを作成（必須: エージェントが内部で gokrax コマンドを呼び出す）
chmod +x gokrax.py
mkdir -p ~/.local/bin
ln -s "$(realpath gokrax.py)" ~/.local/bin/gokrax
# ~/.local/bin が PATH にない場合: echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

`update_settings.py` は初回実行時に `settings.example.py` を `settings.py` にコピーする。アップデート後（`git pull`）に再実行すると、新しいバージョンで追加された設定項目だけを `settings.py` の末尾に追記する（既存の設定は変更しない）。

設定ファイル（`settings.py`）の主要項目については[設定](#設定)セクションを参照。

### エージェント基盤のインストール

gokrax は、エージェントのプロバイダ認証・プロンプト送出のためのバックエンドを必要とする。下記いずれか、または併用も可能。

- openclaw
- pi-coding-agent

ユーザーの環境で既に openclaw が動作しているなら、そのまま openclaw を使用するのが簡単である。そうでなければ pi のほうがセットアップが簡単である。

なお、gokrax に参加するエージェントは最低でも 2体は必要となる（実装者、レビュアー）。

### openclaw の準備

公式ページを参考に、openclaw エージェントの認証を完了する。gokrax 参加エージェントには read, exec 権限を付与する（`gokrax review` などのコマンドを使用するため）。

openclaw: <https://github.com/openclaw/openclaw>

### pi の準備

pi は認証回りが非常に簡単。軽量であり、gokrax は最小構成となる。ただし現状では作業中のエージェントに直接話しかけて介入できない。

使用可能なプロバイダは、Anthropic, GitHub, Google, OpenAI である。

pi-coding-agent: <https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent>

```bash
# WSL の場合は先に nvm で Node.js をインストールする（Windows 側の npm が使われるのを防ぐ）
# curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash && source ~/.bashrc && nvm install --lts

npm install -g @mariozechner/pi-coding-agent
```

#### LLM プロバイダの認証

pi を起動してプロバイダを認証する。
```bash
pi
```

使用するプロバイダを認証する。
```
/login
```

#### エージェントプロファイルの作成

各エージェントのプロファイルを `agents/{name}/` に作成し、`agents/example/` からファイルをコピーする。エージェントの役割や指針はここに記述する：

```
agents/
├── reviewer1/
│   ├── IDENTITY.md       # 名前など
│   ├── INSTRUCTION.md    # 役割・ルール・レビュー指針
│   ├── MEMORY.md         # 教訓・既知の問題
│   ├── AGENTS.md         # IDENTITY + INSTRUCTION + MEMORY で自動生成
│   └── .agents_hash      # ファイル内容更新の検出用（自動生成）
├── reviewer2/
│   └── ...
└── impl1/
    └── ...
```

`IDENTITY.md`, `INSTRUCTION.md`, `MEMORY.md` から `AGENTS.md` が自動生成される（内容更新時のみ）。`AGENTS.md` を直接編集する必要はない。

#### エージェントごとのモデル設定

`agents/config_pi.json` でエージェントごとにプロバイダ・モデル・thinking レベル・使用ツールを設定する。レビュアーも gokrax に完了報告を行うため、`bash` が必要（`INSTRUCTION.md` で書き込み禁止の指示はしてある）。実装者の使用ツール指定は不要（=> 全て許可）。

`pi --list-models` で現在有効なプロバイダ・モデルの一覧を出せる。

設定例:
```json
{
  "reviewer1": {
    "provider": "google-gemini-cli",
    "model": "gemini-3.1-pro-preview",
    "thinking": "low",
    "tools": "read,bash,grep,find,ls"
  },
  "reviewer2": {
    "provider": "openai-codex",
    "model": "gpt-5.4",
    "thinking": "low",
    "tools": "read,bash,grep,find,ls"
  },
  "impl1": {
    "provider": "anthropic",
    "model": "claude-opus-4-6",
    "thinking": "low"
  }
}
```

### glab CLI のインストール

GitLab の Issue 操作に使用する。

```bash
# Homebrew（未インストールの場合 — https://brew.sh）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# インストール後、表示される Next steps に従って PATH に追加すること

brew install glab        # apt install の glab はバージョンが古く動作しない
glab auth login          # GitLab アカウントで認証
```

詳細: <https://gitlab.com/gitlab-org/cli>

### Claude Code CLI のインストール（推奨）

実装フェーズで Claude Code CLI を使用する。バッチ実行時のオプションで ``--no-cc`` 指定すれば実装者が直接実装するので Claude Code 不使用での動作も可能。

```bash
npm install -g @anthropic-ai/claude-code
claude /login   # Anthropic アカウントで認証
```

詳細: <https://docs.anthropic.com/en/docs/claude-code>

### Discord 通知の設定（推奨）

gokrax は進捗通知を Discord チャンネルに投稿する（Discord API を直接使用）。Discord を使用しない最小構成では、ログファイルの表示で進捗確認する（`tail -f /tmp/gokrax-watchdog.log`）。

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

### 進捗監視用のdiscord通信ソフトのインストール（オプション）

discord #gokrax channel を読み書きするための簡素な常駐ツール。gokrax 状況確認・操作のために discord 画面を切り替える必要が無くなる。

WatcherB: <https://gitlab.com/atakalive/WatcherB>

1. WatcherB の説明に従ってインストールし、上記の通知用とは別の discord bot を作成する。
2. WatcherB から discord チャンネルへ gokrax コマンドを投稿する場合、bot user IDを`settings.py` COMMAND_BOT_USER_ID に張り付ける。  


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
                                  DESIGN_APPROVED → ASSESSMENT → IMPLEMENTATION
                                                                          ↓
                                                                     CODE_TEST ⇄ CODE_FIX
                                                                          ↓
                                                                     CODE_REVIEW ⇄ CODE_REVISE
                                                                          ↓
                                                                     CODE_APPROVED → MERGE_SUMMARY_SENT → DONE → IDLE
```
[State Diagram (png)](docs/state-diagram.png)

設計の詳細は [docs/architecture.md](docs/architecture.md) を参照。

- `ASSESSMENT` は設計承認後の判定ステートで、5段階のコード複雑性判定と、3段階のドメインリスク判定を行う。`--exclude-high-risk` / `--exclude-any-risk` 指定時はリスク判定結果に従って Issue をスキップする。（デフォルト: `skip-assess: True`）
- `CODE_TEST` は現在実験段階。テストをパスするように修正してから `CODE_REVIEW` に遷移する。`CODE_REVISE` 後も、テストをパスしてから再レビューに遷移する。（デフォルト: `skip_test: True`）
- `DONE` → `IDLE` 遷移後、キュー実行時は自動的に次バッチへ進む。

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

`BLOCKED` からの復帰手順（`DESIGN_REVIEW` -> `BLOCKED` を想定）：

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

### レビュアーが応答不能となった場合の対応

レートリミット等でレビュアーが作業継続できなくなった場合は、``exclude`` コマンドで除外することができる。

```bash
# 実行中バッチから reviewer1 を除外
python3 gokrax.py exclude --pj MyProject --add reviewer1
```


## アンサンブルレビュー

gokrax のレビューは、複数の LLM レビュアーを並走させるアンサンブル方式を採用している。

### レビュー戦略

開発目的に沿ってレビューの網羅性を高めるため、3つの方法を使用できる。

1. 異なるモデル使用により、各モデルの癖や盲点を補完する  
   → 複数プロバイダの LLM を併用

2. レビュー観点を直交させる（使用者が設定）  
   → エージェントごと、プロジェクトごとの注入スキル切り替え。LLM エージェントのメモリ調整（バックエンド側）

3. 反復レビューにより見落としを減らす  
   → N-pass レビュー機能


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
└── gokrax-metrics.jsonl     # メトリクス（レビュアー評価に向けたレビュー記録。ローカル記録のみ）
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
| `init` | 新規プロジェクト作成（**初回必須**、下記例を参照） |
| `status` | 全プロジェクトの状態表示 |
| `start` | バッチ開始（triage + 設計計画遷移 + watchdog 有効化） |
| `enable` / `disable` | watchdog 有効化・無効化（主にBLOCKEDからの復帰） |
| `transition` | 手動状態遷移（`--force` で強制） |
| `review-mode` | レビューモード変更（full, lite, etc.） |

### プロジェクトの初期化

```bash
# 基本（GitLabパスとローカルリポジトリを指定）
gokrax init --pj myproject --gitlab user/myproject --repo-path /path/to/repo --implementer my_agent
```

`init` はプロジェクトごとに1回だけ実行する。パイプライン管理ファイル（`pipeline.json`）が生成され、以降の全コマンドはこのファイルを参照する。

| Queueコマンド（動作確認後はこちらがおすすめ） | 説明 |
|---------|------|
| `qstatus` | キュー内容の表示。キュー番号[0...N]の表示 |
| `qrun` | キューモードでバッチ開始（合図が検知され次第開始） |
| `qadd ...` | キューファイルにアイテムを追加 |
| `qdel N` | キューファイルのN番目アイテムを削除（qstatus表示番号に対応） |


## 設定

主要な設定項目（`settings.py`）：

### エージェント定義

openclaw agentsのIDを登録する。以降は、ここで設定されたエージェント名を使用する。

```python
# レビュアー名
REVIEWERS = ["rev1", "rev2", "rev3", "rev4"]
# 実装者名
IMPLEMENTERS = ["impl1"]
```

### パス設定

```python
GLAB_BIN = "/usr/bin/glab"                      # which glab で確認
PI_BIN = "/home/you/.nvm/.../bin/pi"            # which pi で確認（nvm 環境ではパスに注意）
GOKRAX_CLI = "/home/you/.local/bin/gokrax"      # which gokrax で確認（シンボリックリンク先）
GITLAB_NAMESPACE = "your-username"              # gitlab.com/YOUR_NAMESPACE/...
```

### バックエンド設定

```python
# 全エージェントを openclaw で動かす場合
DEFAULT_AGENT_BACKEND = "openclaw"

# エージェントごとに混在させる場合
DEFAULT_AGENT_BACKEND = "pi"
AGENT_BACKEND_OVERRIDE = {"impl1": "openclaw"}
```

### レビュアーティア

レビュアーはインフラの安定性に応じてティアに分類される：

```python
REVIEWER_TIERS = {
    "regular":       ["rev1", "rev2"],  # 安定接続、十分なコンテキスト長
    "short-context": ["rev3"],          # コンテキスト長に制約あり（頻繁に新セッション化して対応）
    "free":          ["rev4"],          # 日次トークン上限あり、不安定、扱いが難しい
}
```

### レビューモード

解きたい問題に応じてレビューコストを調整するために、モード切替できるようにしておく。

```python
REVIEW_MODES = {
    "full":     {"members": ["rev1", "rev2", "rev3"],},
                 "min_reviews": 3, "grace_period_sec": 0},  # 省略可: min_reviews, grace_period_sec
    "lite":     {"members": ["rev1", "rev2"],},
    "min":      {"members": ["rev1"],},
    "skip":     {"members": [],},

    "lite3":    {"members": ["rev1", "rev2", "rev3"],
                 "min_reviews": 2, "grace_period_sec": 300},
    "lite_x2":  {"members": ["rev1", "rev2"],
                 "n_pass":  {"rev1": 2, "rev2": 2}, },
}
```

- `min_reviews` 件の承認が集まった時点で次の状態に遷移する（デフォルト: 全員）。`min_reviews` の数が `members` より少ない場合（例: `lite3` は3人中2人）、`min_reviews` 到達後に `grace_period_sec` だけ追加レビューを待つ。猶予時間内に残りのレビュアーが応答すればそれも反映され、猶予を過ぎれば集まった分で遷移する。応答の遅いレビュアーや不安定なレビュアーを含めつつ、パイプラインを止めない運用ができる。

- `n_pass` の設定により、指定レビュアーが N回の見直しを行う。（指定なし = 1）

- 存在しないレビュアー名が設定されていると警告が出るので、予め削除しておくかコメントアウトで対処する。

- **フェーズ上書き**: モード定義内に `"design"` / `"code"` キーでフェーズ固有の設定を追加できる。上書き可能なフィールドは `members`, `min_reviews`, `n_pass`, `grace_period_sec`。上書きされないフィールドはモードのデフォルト値を継承する。`min_reviews` は自動的に `len(members)` でキャップされ、メンバー数を超えることはない。

```python
"full_x2": {
    "members": ["rev1", "rev2", "rev3", "rev4"],
    "n_pass": {"rev1": 2, "rev2": 2, "rev3": 2, "rev4": 2},
    "code": {
        "members": ["rev1", "rev2", "rev3"],  # excluded rev4 in CODE_REVIEW
        "n_pass": {"rev1": 2, "rev2": 2},     # rev3: 1 (default value)
    },
},
```

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

gokrax のアンサンブルレビューが最終的なコード品質を、どの程度、どのような指標に基づいて改善できるかは、現時点で定量的に評価できていない。

必要な評価は、現在広く用いられている**Claude Code Opusへの丸投げをベースラインとした end-to-end 比較**となる。同一の題材を「レビューなしの Claude Code 単体」と「gokrax パイプライン」で実装し、最終コードの品質を開発者の目的に沿った指標で第三者レビュアーが比較すればよいと考えられる。

### コードレビューの限界

gokrax のレビューは静的なコードレビューであり、実機でしか発現しないバグなどは検出できない。

### 並列化

現在、パイプラインは1プロジェクトずつ逐次実行される。パイプライン状態管理は並列化を想定した設計だが、主にエラーハンドリングの複雑化を考慮し、現在、初期段階では逐次実行に限定している。

### 対応プラットフォーム

現時点で GitLab のみ対応（無料でprivate repositoryを利用できるため）。GitHub 対応は未実装。


## 今後の課題

### 操作・監視用のGUI

エージェントを介在させることでCLIの操作が簡単に行える一方で、パイプライン開始や状況に応じたレビューモードの設定は使用者が手動で行う必要がある。この部分の操作を簡略化するため、discord監視GUIツールの拡張を検討している（マウス操作でIssueとパラメータを選択して「キューに追加」「実行」ボタンを押すような形）。

### タスクキュー自動生成時のパラメータ調整

仕様書から一気通貫に実装する spec mode 手順において、分割済みの各作業バッチに対するモデル選択などの自動調整は難しく、使用者の意図通りになりにくい。キュー生成提案を依頼する際のプロンプト調整と、モデル使用量の測定（未実装）を行うことにより改善の可能性がある。

### テスト (CODE_TEST state)

CODE_TEST は実装済みだが動作検証が不十分であるため、現状では実験的機能の扱いである（デフォルト: --skip-test: True）。


## ライセンス

MIT License
