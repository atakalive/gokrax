# DevBar Standalone Mode — Design Spec (Draft)

**Status**: Draft  
**Date**: 2026-03-14  
**Goal**: OpenClaw依存を外部化し、DevBarを独立ツールとして動作可能にする

---

## 1. 現状の依存関係

DevBarは現在OpenClawの以下の機能に依存している：

| 機能 | OpenClaw側 | DevBar側の利用箇所 |
|------|-----------|-------------------|
| ペルソナ注入 | SOUL.md / AGENTS.md → システムプロンプト自動注入 | レビュアーの性格・判断基準の定義 |
| 認証管理 | auth-profiles.json（OAuth自動リフレッシュ含む） | 各LLMプロバイダーへのAPI認証 |
| レビュー実行 | sessions_spawn / sessions_send | レビュアーエージェントの起動・応答受信 |
| Discord通知 | messageツール（channel plugin） | #dev-bar への進捗通知 |
| 実装実行 | Claude Code CLI（exec経由） | IMPLEMENTATION状態での自動コーディング |

**注**: watchdog.py（状態監視）、pipeline.json（状態管理）、task_queue.py（キュー管理）は既にOpenClaw非依存。

---

## 2. 外部化すべきモジュール

### 2.1 LLM Backend Abstraction Layer

レビュアーが各社LLM APIを統一インターフェースで叩けるようにする。

```
┌─────────────────────────────────────────────┐
│            DevBar LLM Interface             │
│                                             │
│  review(prompt, system_prompt) → response   │
│  code(prompt, system_prompt) → diff         │
└──────────┬──────────────────────────────────┘
           │
     ┌─────┴─────┐
     │  Backend   │
     │  Router    │
     └─────┬─────┘
           │
  ┌────────┼────────┬────────────┬──────────────┐
  ▼        ▼        ▼            ▼              ▼
Anthropic  OpenAI  Google    GitHub Copilot   Ollama
(Claude)  (GPT)   (Gemini)  (GPT-4.1 etc.)  (Local)
```

**統一インターフェース**:

```python
class LLMBackend(Protocol):
    def chat(
        self,
        messages: list[dict],
        system_prompt: str,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse: ...

@dataclass
class LLMResponse:
    content: str
    usage: TokenUsage
    model: str
    finish_reason: str
```

**実装候補**:
- **litellm**: 100+プロバイダー対応。ただしGitHub Copilot device flowは未対応
- **自前薄いラッパー**: 対応プロバイダーを絞る代わりに依存を最小化
- **ハイブリッド**: litellmベース + GitHub Copilot/Ollama用のカスタムbackend

### 2.2 Persona Injection System

OpenClawのSOUL.md/AGENTS.mdに相当する、レビュアー定義の仕組み。

```yaml
# reviewers.yaml
reviewers:
  dijkstra:
    display_name: "Dijkstra"
    tier: regular
    backend: anthropic
    model: claude-opus-4-6
    system_prompt: |
      あなたはダイクストラです。構造化プログラミングの父として、
      コードの設計品質・エレガンス・構造化を重視してレビューします。
      ...
    focus_areas:
      - design_quality
      - elegance
      - structured_programming

  pascal:
    display_name: "Pascal"
    tier: semi
    backend: google
    model: gemini-3-pro-preview
    system_prompt: |
      あなたはパスカルです。数理的厳密性の観点からレビューします。
      ...

  hanfei:
    display_name: "Han Fei"
    tier: free
    backend: github-copilot
    model: gpt-4.1
    system_prompt: |
      あなたは韓非です。性悪説に基づき、堅牢性・防御的プログラミングを
      重視してレビューします。
      ...

  basho:
    display_name: "Basho"
    tier: free
    backend: ollama
    model: Qwen3.5-27B-Q8_0
    base_url: "http://localhost:11434"
    system_prompt: |
      ...
```

**ポイント**:
- 1ファイルでレビュアー定義を完結（OpenClawの複数ファイル散在を解消）
- tier（regular/semi/free）もここに含める
- backend + model + 認証情報の紐付け

### 2.3 Authentication Manager

各プロバイダーのAPI認証を統一管理する。

```yaml
# auth.yaml (or env vars)
backends:
  anthropic:
    type: oauth  # or api_key
    # OAuth: refresh tokenベースの自動更新
    refresh_token: ${ANTHROPIC_REFRESH_TOKEN}
    # API Key: シンプル
    # api_key: ${ANTHROPIC_API_KEY}

  openai:
    type: api_key
    api_key: ${OPENAI_API_KEY}

  google:
    type: oauth
    refresh_token: ${GOOGLE_REFRESH_TOKEN}
    project_id: ${GOOGLE_PROJECT_ID}

  github-copilot:
    type: device_flow
    # 初回: devbar auth login-github-copilot でdevice flow実行
    # 以後: tokenファイルに保存・自動リフレッシュ
    token_file: ~/.devbar/github-copilot-token.json

  ollama:
    type: none  # ローカル、認証不要
    base_url: "http://localhost:11434"
```

**OAuth自動リフレッシュ**:
- Anthropic: OpenClawのrefreshAnthropicToken相当の実装が必要
- GitHub Copilot: device flow + token refresh
- Google: 標準OAuth2フロー
- `devbar auth` サブコマンドで初期認証・トークンリフレッシュ

### 2.4 Notification Abstraction

Discord通知のOpenClaw依存を外す。

```python
class Notifier(Protocol):
    def send(self, channel: str, message: str, **kwargs) -> None: ...

# 実装
class DiscordWebhookNotifier:  # webhook URL直叩き（最もシンプル）
class DiscordBotNotifier:      # Bot token直叩き（スレッド操作等）
class SlackNotifier:           # Slack webhook
class StdoutNotifier:          # ログ出力のみ（通知不要時）
class OpenClawNotifier:        # 従来互換（OpenClaw経由）
```

---

## 3. 動作モード

### 3.1 Standalone Mode（新規）

```
devbar --mode standalone --config devbar-standalone.yaml
```

- OpenClaw不要
- LLM API直叩き
- Discord webhook通知（or 通知なし）
- Claude Code CLI / aider / Codex CLI で実装

### 3.2 OpenClaw Mode（従来互換）

```
# 現行通り、OpenClawエージェントとして動作
```

- OpenClawのsessions_spawn/sendでレビュアー起動
- ペルソナはSOUL.md/AGENTS.mdから注入
- 通知はOpenClawのmessageツール経由

### 3.3 設定ファイル構造

```
~/.devbar/
├── config.yaml          # メイン設定（mode, projects, etc.）
├── auth.yaml            # 認証情報（or 環境変数）
├── reviewers.yaml       # レビュアー定義
├── github-copilot-token.json  # device flow token
└── pipelines/           # 各プロジェクトのpipeline.json
    ├── DevBar.json
    ├── EMCalibrator.json
    └── ...
```

---

## 4. コーディングエージェント抽象化

```python
class CodingAgent(Protocol):
    def execute(
        self,
        task: str,           # Issue description / plan
        project_path: str,
        session_id: str | None = None,  # 継続セッション用
    ) -> CodingResult: ...

@dataclass
class CodingResult:
    success: bool
    changed_files: list[str]
    session_id: str | None  # REVISE時の継続用
    output: str

# 実装
class ClaudeCodeAgent:    # claude CLI
class CodexAgent:         # codex CLI
class AiderAgent:         # aider CLI
class OpenClawAgent:      # sessions_spawn経由（従来互換）
```

---

## 5. 移行ステップ（優先度順）

### Phase 1: LLM Backend + Persona（最小限で独立動作）
1. LLMBackend抽象化層を実装
2. reviewers.yaml でペルソナ定義
3. レビュー実行をOpenClaw sessions_send → LLM API直叩きに切替
4. auth.yaml / 環境変数ベースの認証

→ **これだけでレビュー部分が独立する**

### Phase 2: 通知の外部化
5. DiscordWebhookNotifier実装
6. notify.pyをNotifierインターフェース経由に変更

### Phase 3: コーディングエージェント抽象化
7. CodingAgent Protocol実装
8. Claude Code CLI直接呼び出し（exec経由、OpenClaw不要）

### Phase 4: CLI / パッケージ化
9. `devbar` CLIコマンド体系
10. pip install可能なパッケージ化
11. ドキュメント

---

## 6. 設計原則

- **OpenClawは"あると便利"だが"必須ではない"**: Standalone modeがデフォルト
- **設定はYAML 1ファイルに集約**: 分散しない
- **認証は環境変数でもファイルでも**: CI/CD環境でも動くように
- **プロバイダー追加は1クラス追加で完結**: LLMBackendとCodingAgentのProtocol準拠
- **既存のpipeline.json/watchdog.pyは変更最小限**: コアロジックは触らない

---

## 7. 未決事項

- [ ] litellm採用 vs 自前ラッパー（依存の軽さ vs 対応範囲）
- [ ] GitHub Copilot device flowの自前実装コスト見積り
- [ ] Anthropic OAuthリフレッシュの自前実装 vs API key運用に割り切るか
- [ ] `devbar` CLIのサブコマンド設計
- [ ] テスト戦略（モック vs 実API）
- [ ] ライセンス選定
