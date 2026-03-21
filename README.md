# gokrax

GitLab Issue からマージまでを逐次実行する自動開発パイプライン。

LLMエージェントによる設計・実装・レビューを状態機械で管理し、Issue を入力として受け取りレビュー済みのコードを出力する。

**リポジトリ:**
- **GitHub（公開用）:** <https://github.com/atakalive/gokrax> — GitLab から不定期に同期
- **GitLab（開発用）:** <https://gitlab.com/atakalive/gokrax> — 開発状況、gokraxによるgokrax開発のデモ（日本語ページ）

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

gokraxは、自動開発パイプラインの高精度化を目的とする。動作はするが間違っているコードが生成される頻度を下げるためのツールである。

使用者が主にやることは、機能追加等の問題提起と、その問題解決の難易度・重要性に対する力加減の調整（投入モデル選択など）の2点である。

gokraxは以下のパイプラインを自動で実行する：

```
Issue → 設計計画 → 設計レビュー → 実装 → コードレビュー → マージ
```

各段階は LLM エージェントが実行し、作業完了報告が集まって遷移条件が満たされると次の段階へ自動で進む。

レビュー段階で重大な指摘（P0/P1）が出た場合は修正ループに入り、必要数（基本は全員分）の承認を得るまで修正する。修正ループの反復回数が規定数に達した場合にはパイプラインを停止させる。

## 動作環境

- **OS**: Linux（WSL2 含む）、macOS。（Windowsは未検証）
- **Python**: 3.10 以上。外部依存: `requests`, `PyYAML`
- **[OpenClaw](https://github.com/openclaw/openclaw)**: エージェント基盤。設計・修正・レビューを実行する LLM エージェントの認証・プロンプト送出のために使用
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)**: 実装作業エージェントとして内部で呼び出し
- **[glab CLI](https://gitlab.com/gitlab-org/cli)**: GitLab 操作（Issue 取得、コメント取得/投稿、Issue close）に使用
- **GitLab**: Issue トラッカーおよびコードホスティング。管理下プロジェクトへの git push 権限が必要（SSH 鍵 または HTTPS トークン）
- **Discord bot token**: 進捗通知用（推奨）。進捗監視用GUI（オプション）は [WatcherB](https://gitlab.com/atakalive/WatcherB) を参照

### LLM プロバイダ

gokrax は特定の LLM プロバイダに依存せず、OpenClaw が認証可能なプロバイダは使用可能：

- Anthropic（Claude）
- Google（Gemini）
- OpenAI（ChatGPT）
- ローカルモデル（llama.cpp, vLLM 等）

実装エージェントとレビュアーエージェントにそれぞれ異なるプロバイダ・モデル・視点を割り当てられる。

### ハードウェア要件

gokrax 自体の計算負荷はほぼ無し（状態管理とプロセス起動のみ）。

## セットアップ

gokrax のセットアップは openclaw エージェント等と対話的に実行するのが簡単である。本READMEをエージェントに読ませて初期設定すれば、以下を一通り実行できる：

- `settings.py`, `gokrax-queue.txt` の作成
- GitLab のプロジェクト作成とパス設定、ローカルリポジトリのパス設定
- gokraxへのプロジェクト追加
- Discord 通知先チャンネル（任意）
- レビュアー、実装者として使うエージェントの構成
- watchdog の設定

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

### openclaw の用意

openclaw: <https://github.com/openclaw/openclaw>

1. エージェントの認証を完了し、会話できるようにしておく。gokrax参加エージェントには read, exec 権限を付与する（gokrax review などのコマンドを使用するため）。
2. 現在gokraxはdiscord通知のみ対応しているので、openclawをdiscordと連携し、discordサーバーに #gokrax channelを作成する。以下に従って進める。  
openclaw docs Discord: <https://docs.openclaw.ai/channels/discord>

3. discord bot設定のため、`settings.py` を開く。この時点で無ければ上記の ``python3 update_settings.py`` が実行されていない。
4. gokrax通知用のdiscord botを作成（または流用）し、bot token を `settings.py` DISCORD_BOT_TOKEN に張り付ける。
5. botをサーバー招待後、discord bot名を右クリックし、user IDをコピーして `settings.py` ANNOUNCE_BOT_USER_ID に張り付ける。

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

最初からインストールする必要はない。gokraxを継続使用する場合のみ導入すればよい。

WatcherB: <https://gitlab.com/atakalive/WatcherB>

1. WatcherB の説明に従ってインストールし、通知用とは別のdiscord botを作成する。
2. WatcherBからgokraxコマンドを投稿する場合、bot user IDを`settings.py` COMMAND_BOT_USER_ID に張り付ける。  


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

0. `IDLE` → `INITIALIZE`（エージェント初期化等）
1. `DESIGN_PLAN` → `DESIGN_REVIEW`（レビュアーに自動通知）
2. レビュー結果に応じて `DESIGN_APPROVED` or `DESIGN_REVISE`
3. `DESIGN_APPROVED` → `IMPLEMENTATION`（実装エージェントが自動起動）
4. `IMPLEMENTATION` → `CODE_REVIEW`（レビュアーに自動通知）
5. レビュー結果に応じて `CODE_APPROVED` or `CODE_REVISE`
6. `CODE_APPROVED` → `MERGE_SUMMARY_SENT`（Discord にサマリー投稿）
7. 人間が「OK」とリプライ or 自動マージ → `DONE`（git push + Issue close）

各エージェントは ``gokrax plan-done ...`` とか ``gokrax review ...`` などのコマンドを実行することでシステムに作業完了報告する。完了報告により遷移条件が整えば状態遷移する。特に問題がなければ、1件あたり30分程度で完了する。

コンテキストのリセット判定は、INITIALIZE, IMPLEMENTATION で行われる。実行時の設定により、実行バッチ内で継続、バッチ間で継続を指定できる。


## パイプラインの状態遷移

```
IDLE → INITIALIZE → DESIGN_PLAN → DESIGN_REVIEW ⇄ DESIGN_REVISE
                                        ↓
                                  DESIGN_APPROVED → IMPLEMENTATION → CODE_TEST ⇄ CODE_FIX
                                                                          ↓
                                                                     CODE_REVIEW ⇄ CODE_REVISE
                                                                          ↓
                                                                     CODE_APPROVED → MERGE_SUMMARY_SENT → DONE → IDLE
```
[State Diagram (png)](docs/state-diagram.png)

設計の詳細は [docs/architecture.md](docs/architecture.md) を参照。

`INITIALIZE` はエージェントセッションの初期化（コンテキストリセット判定含む）等を行う状態。`CODE_TEST` は現在実験的で、`skip_test` 設定時（デフォルト）は `IMPLEMENTATION` から直接 `CODE_REVIEW` に遷移する。キュー実行時は自動的に次バッチへ進む。

各状態にはタイムアウトが設定されている（`settings.py` の `BLOCK_TIMERS`）：

| 状態 | タイムアウト初期値 |
|------|----------------------|
| `DESIGN_PLAN` | 30 分 |
| `DESIGN_REVIEW` | 60 分 |
| `DESIGN_REVISE` | 30 分 |
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

作者自身は、動作効率や利便性だけでなく、数理的な誤りや装置を破損させるような問題が混入しにくい自動開発システム構築を目指している。

### 小規模・ローカルモデルの活用

レビュアー枠では、大規模汎用モデルではなく、ドメイン知識偏重・外部知識を参照する小規模モデルであっても有用である可能性がある。gokraxは特定目的のレビューシステムへの組み込みという小規模モデルの応用先を提示する。

### LLMプロバイダ選定の実践的な注意

後述するアンサンブルレビュー体制のために、gokraxは複数のLLMプロバイダの併用を想定している。参考まで、作者の環境では Claude Max, Gemini Pro, ChatGPT Plus, Github Copilot, ローカルモデルを使用している。API従量課金プロバイダはテストしていない。作者の経験上、月額$20程度の定額契約においては、gokraxに毎回参加できるのは使用量的にレビュアー1人分になる（10 batch/day程度の場合）。

「レビュアー」モデルの選定はプロバイダのインフラ特性に大きく依存する。たとえば、Gemini Proプランはレートリミット到達時に減速するが完全に停止しにくいため、安価なインフラモデルとして扱いやすい。使用量の総量に制限があるプロバイダはプロジェクトの重要度・難易度に沿って投入する。ローカルモデルはコスト・レートリミットの問題がないため、コンテキスト長・ツール実行性能・レビュー性能が許すかぎり推奨される（Qwen3.5-27B-Q8_0, 128k contextで動作確認。間違いは多い）。

「実装者」モデルは、設計（3行Issueを詳細化）と、修正対応（レビュー指摘の反映・異議申し立て）を行う。動作確認は Claude Opusで行っている。


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
    "short-context": ["local"],                        # コンテキスト長に制約あり（頻繁に新セッション化する）
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

gokrax のアンサンブルレビューが最終的なコード品質をどの程度改善できるかは、現時点で定量的に評価できていない。

必要な評価は、**Claude Code Opusへの丸投げをベースラインとした end-to-end 比較**となる。同一の題材を「レビューなしの Claude Code 単体」と「gokrax パイプライン」で実装し、最終コードの品質を開発者の目的に沿った指標で第三者レビュアーが比較すればよい。ただ、一連の実験をすぐに用意することは作者には困難。

### コードレビューの限界

gokrax のレビューは静的なコードレビューであり、実機でしか発現しないバグ（特定入力での異常など）は検出できない。使用するレビュー手法については今後改善の余地がある。

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


## その他

gokraxは、増えすぎた並行開発プロジェクト推進のために構築された自動化ツールである。  
現在、装置制御、画像再構成、物理シミュレーション、最適化、家計管理などのプロジェクトで実稼働している。  
ドキュメントの重大な間違いや不親切な箇所を発見した場合や、意図通りに動作しない場合はご一報いただけると助かります。

## ライセンス

MIT License
