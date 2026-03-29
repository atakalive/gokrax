# Quick Start

gokrax を最短で動かすためのガイド。所要時間: 約15分。

## 1. 前提

- Linux または macOS（WSL2 含む）
- Python 3.11+
- [GitLab](https://gitlab.com/) アカウント（無料で private repository を利用可能）
- いずれかの LLM プロバイダのアカウント（Anthropic, Google, OpenAI, GitHub Copilot 等）

## 2. 必要なツールのインストール

```bash
# gokrax
git clone https://gitlab.com/atakalive/gokrax.git
cd gokrax
pip install -r requirements.txt

# glab（GitLab CLI — https://gitlab.com/gitlab-org/cli）
brew install glab          # macOS / Linux (Homebrew)
# apt install glab         # Debian / Ubuntu
# dnf install glab         # Fedora
glab auth login

# pi（エージェント基盤 https://github.com/badlogic/pi-mono/tree/main/packages/agent）
npm install -g @mariozechner/pi-agent-core
pi    # 起動後、/login でプロバイダを選択してブラウザでログイン（URLへのスペース混入注意）
```

## 3. 設定

```bash
python3 update_settings.py    # settings.example.py → settings.py を生成
```

`settings.py` を編集:

```python
# --- 必須 ---
GLAB_BIN = "/usr/bin/glab"              # which glab で確認
GITLAB_NAMESPACE = "your-username"      # gitlab.com/YOUR_NAMESPACE/...

DEFAULT_AGENT_BACKEND = "pi"

DEFAULT_QUEUE_OPTIONS = {
    "no-cc": True,              # <- Claude Code CLI 無しで動かす
    "automerge": True,          # no-cc 以外はデフォルトでOK
    "skip_cc_plan": True,
    "keep_ctx_intra": True,
    "skip_test": True,
    "skip_assess": True,
}

# --- エージェント ---
REVIEWERS = ["reviewer1"]
IMPLEMENTERS = ["impl1"]

REVIEWER_TIERS = {
    "regular": ["reviewer1"],
    "short-context": [],
    "free": [],
}

REVIEW_MODES = {
    "min": {"members": ["reviewer1"]},
}
```

最小構成: レビュアー1体 + 実装者1体。

## 4. エージェントの準備

```bash
# テンプレートからコピー
mkdir -p agents/reviewer1 agents/impl1

cp agents/example/INSTRUCTION.md.reviewer   agents/reviewer1/INSTRUCTION.md
cp agents/example/INSTRUCTION.md.implementer agents/impl1/INSTRUCTION.md
cp agents/example/MEMORY.md.example          agents/reviewer1/MEMORY.md
cp agents/example/MEMORY.md.example          agents/impl1/MEMORY.md
```

必要に応じて各ファイルを編集する。`agents/example/` にテンプレートの説明がある。

モデル設定（`agents/config_pi.json`）。`provider` と `model` は `pi --list-models` で表示される名前を使う:

```json
{
  "reviewer1": {
    "provider": "google-gemini-cli",
    "model": "gemini-3.1-pro-preview",
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

## 5. プロジェクト登録とサンプル Issue

GitLab にプロジェクトがまだない場合:

```bash
mkdir myproject && cd myproject
git init
glab repo create myproject --private
git commit --allow-empty -m "init" && git push --set-upstream origin main
```

```bash
# gokrax にプロジェクトを登録（GitLab リポジトリとローカルパスを指定して実行）
gokrax init \
  --pj myproject \
  --gitlab your-username/myproject \
  --repo-path /path/to/your/project \
  --implementer impl1

# サンプル Issue を作成
cd /path/to/your/project
glab issue create \
  --title "Add hello.py" \
  --description "Create hello.py that prints 'Hello, gokrax!' to stdout."
```

GitLab の Issue ページをブラウザで開いておく。設計・レビューのコメントがリアルタイムで追記されていく。

## 6. 実行

```bash
gokrax start --project myproject --issue 1 --mode min

# 進捗をリアルタイムで確認
tail -f /tmp/gokrax-watchdog.log
# DESIGN_PLAN → DESIGN_REVIEW → ... → DONE まで自動で進む

# 完了したら成果物を確認
cat /path/to/your/project/hello.py
```

## 次のステップ

- **Discord 通知を追加** — bot 作成手順は [README: Discord 通知の設定](../README.md#discord-通知の設定) を参照
- **レビュアーを増やす** — アンサンブルレビューで品質向上（[README: アンサンブルレビュー](../README.md#アンサンブルレビュー)）
- **バッチ実行** — キューファイルで複数 Issue を連続処理（`gokrax qrun`）
- **Spec Mode** — 仕様書から Issue 自動分割（[README: Spec Mode](../README.md#spec-mode仕様書パイプライン)）
- **ドメインリスク判定** — `DOMAIN_RISK.md` でプロジェクト固有のリスクを定義

## クリーンアップ

テスト用プロジェクトが不要になった場合:

```bash
gokrax reset --pj myproject                      # パイプライン状態をリセット
glab repo delete your-username/myproject --yes   # GitLab リポジトリを削除
rm -rf /path/to/myproject                        # ローカルディレクトリを削除
```
