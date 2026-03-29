# Quick Start

gokrax を最短で動かすためのガイド。所要時間: 約30分。

## 1. 前提

- Linux または macOS（WSL2 含む）
- Python 3.11+
- [GitLab](https://gitlab.com/) アカウント（無料で private repository を利用可能）
- GitLab に SSH 鍵を登録済みであること（下記参照）
- いずれかの LLM プロバイダのアカウント（[pi が対応するプロバイダ](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/providers.md): Anthropic, GitHub Copilot, Google Gemini CLI, OpenAI Codex, Antigravity 等）

### SSH 鍵の登録（GitLab）

gokrax のパイプラインは自動で git push するため、SSH 鍵が必要。

```bash
# 既存の鍵を確認
ls ~/.ssh/id_ed25519.pub
# "No such file or directory" と出たら鍵がないので作成する:
ssh-keygen -t ed25519 -C "you@example.com"
# Enter で全てデフォルト（パスフレーズは空でも可）

# 公開鍵を表示してコピー
cat ~/.ssh/id_ed25519.pub
```

表示された内容を [GitLab SSH Keys 設定ページ](https://gitlab.com/-/user_settings/ssh_keys) に貼り付けて登録する。

```bash
# 接続確認
ssh -T git@gitlab.com
# "Welcome to GitLab, @your-username!" と出れば成功
```

## 2. 必要なツールのインストール

```bash
# gokrax
git clone https://gitlab.com/atakalive/gokrax.git
cd gokrax
pip install -r requirements.txt

# Homebrew（未インストールの場合 — https://brew.sh）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# glab（GitLab CLI — https://gitlab.com/gitlab-org/cli）
brew install glab
glab auth login

# pi（エージェント基盤 https://github.com/badlogic/pi-mono/tree/main/packages/agent）
npm install -g @mariozechner/pi-coding-agent
pi    # 起動後、/login でプロバイダを選択してブラウザでログイン

# pi にパスが通っているか確認
which pi
# 見つからない場合: echo 'export PATH="$(npm -g prefix)/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

## 3. gokrax コマンドの設定（必須）

エージェントが内部で `gokrax` コマンドを呼び出すため、PATH の通った場所にシンボリックリンクが必要:

```bash
mkdir -p ~/.local/bin
ln -s /path/to/gokrax/gokrax.py ~/.local/bin/gokrax

# ~/.local/bin が PATH にない場合:
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc

# 確認
which gokrax
```

## 4. 設定

```bash
python3 update_settings.py    # settings.example.py → settings.py を生成
```

`settings.py` を編集:

```bash
# 設定 (Save: Ctrl+O, Exit: Ctrl+X)
nano settings.py
```

```python
# --- 必須 ---
GLAB_BIN = "/usr/bin/glab"              # which glab で確認
PI_BIN = "/usr/bin/pi"                  # which pi で確認
GOKRAX_CLI = "/home/you/.local/bin/gokrax"  # which gokrax で確認（手順 3 で作成したリンク）
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

## 5. エージェントの準備

```bash
# プロンプトのテンプレートをコピー
mkdir -p agents/reviewer1
cp agents/example/INSTRUCTION.md.reviewer agents/reviewer1/INSTRUCTION.md

mkdir -p agents/impl1
cp agents/example/INSTRUCTION.md.implementer agents/impl1/INSTRUCTION.md
```

モデル設定（`agents/config_pi.json`）。`provider` と `model` は `pi --list-models` で表示される名前を使う:

```bash
# 有効な provider, model を表示
pi --list-models

# モデル設定 (Save: Ctrl+O, Exit: Ctrl+X)
nano agents/config_pi.json
```

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

## 6. プロジェクト登録とサンプル Issue

GitLab にリポジトリがまだない場合:

```bash
# GitLab にリポジトリを作成
glab repo create myproject --private

# gokrax ディレクトリの外に移動してからプロジェクトを作成
cd ~
mkdir myproject && cd myproject
git init
git config user.email "you@example.com"
git config user.name "Your Name"
git remote add origin git@gitlab.com:your-username/myproject.git
# 修正したい場合: git remote set-url origin git@gitlab.com:correct-username/myproject.git

# 初回コミットとプッシュ
echo "# myproject" > README.md
git add README.md
git commit -m "init"
git push -u origin HEAD
```

GitLab リポジトリ作成後:

```bash
# gokrax ディレクトリに戻って実行
cd /path/to/gokrax

# gokrax にプロジェクトを登録（GitLab リポジトリとローカルパスを指定して実行）
python3 gokrax.py init --pj myproject --gitlab your-username/myproject --repo-path /path/to/your/project --implementer impl1

# myproject ディレクトリに戻る
cd /path/to/your/project

# サンプル Issue を作成
glab issue create \
  --title "Add hello.py" \
  --description "Create hello.py that prints 'Hello, gokrax.' to stdout."
```

GitLab の Issue #1 ページをブラウザで開いておく。設計・レビューのコメントがリアルタイムで追記されていく。

## 7. 実行

```bash
cd /path/to/gokrax
python3 gokrax.py start --project myproject --issue 1 --mode min

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
python3 gokrax.py reset --pj myproject                      # パイプライン状態をリセット
glab repo delete your-username/myproject --yes   # GitLab リポジトリを削除
rm -rf /path/to/myproject                        # ローカルディレクトリを削除
```
