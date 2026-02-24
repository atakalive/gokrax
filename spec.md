# DevBar — 開発パイプライン仕様書

> 現行コード（2026-02-24時点）に基づく正式仕様。金子さん含むエージェント全員はこの文書に従うこと。

## 1. 概要

DevBarは **Issue → 設計 → 実装 → レビュー → マージ** のパイプラインを管理するCLI + watchdogシステム。LLMを使わない純粋なオーケストレーターで、pipeline JSONを状態マシンとして駆動する。

## 2. アーキテクチャ

```
devbar.py    — CLI。pipeline JSONの唯一の操作インターフェース
watchdog.py  — cronで1分間隔実行。条件判定→状態遷移→通知
notify.py    — エージェント通知 + Discord投稿
config.py    — 定数の一元管理
pipeline_io.py — JSON読み書き（排他ロック + atomic write）
```

- **pipeline JSON**: `~/.openclaw/shared/pipelines/<project>.json`
- **watchdog cron**: `*/1 * * * *` でflock排他実行
- **Discord通知先**: #dev-bar (kaneko-discord アカウントで投稿)

## 3. ロール定義

### 3.1 実装担当 (Implementer) = 金子 (kaneko)

- DESIGN_PLANフェーズでIssue本文を確認・修正し、`plan-done` を実行する
- CODE_REVISEフェーズでP0指摘に基づきコードを手動修正し、`commit` + `revise` を実行する
- IMPLEMENTATIONフェーズではCCが自動起動される（金子が手動でやるのではない）

### 3.2 レビュアー (Reviewers) = pascal, leibniz, hanfei, dijkstra

- DESIGN_REVIEWまたはCODE_REVIEWでレビュー依頼を受け取る
- `devbar review` コマンドでverdict（APPROVE / P0 / P1）を投稿する
- **自分が設計・実装したものを自分でレビューしてはならない**
- レビュアーは実装担当ではない。レビュアーが `plan-done`, `commit`, `revise` を実行することはない

### 3.3 承認者 = M (人間)

- MERGE_SUMMARY_SENT で #dev-bar にサマリーが投稿される。Mが「OK」とリプライするとDONE→マージ
- `devbar start` や `devbar transition --force` 等の制御コマンドを実行する

### 3.4 CC (Claude Code)

- IMPLEMENTATIONフェーズでwatchdogが自動起動する
- Plan (model: sonnet) → Impl (model: sonnet) の2段階
- CC完了後、自動で `devbar commit` を実行する
- **CCはIMPLEMENTATIONでのみ使用。他のフェーズでは使わない**

## 4. 状態マシン

```
IDLE → DESIGN_PLAN → DESIGN_REVIEW → DESIGN_APPROVED → IMPLEMENTATION
                  ↑        ↓                                    ↓
            DESIGN_REVISE ←┘                              CODE_REVIEW
                                                       ↗        ↓
                                                CODE_REVISE  CODE_APPROVED
                                                                 ↓
                                                       MERGE_SUMMARY_SENT
                                                                 ↓
                                                               DONE → IDLE

※ どの状態からもBLOCKEDに遷移可能（タイムアウト / REVISEサイクル上限）
※ BLOCKEDからはIDLEにのみ戻れる
```

### 4.1 各状態の詳細

| 状態 | 責任者 | やること | 次の状態への条件 |
|------|--------|----------|-----------------|
| IDLE | - | 何もない | `devbar start` で DESIGN_PLAN へ |
| DESIGN_PLAN | 実装担当 | Issue本文を確認・修正し `plan-done` | 全Issueに `design_ready` フラグ |
| DESIGN_REVIEW | レビュアー | 設計レビュー、`devbar review` で投稿 | `min_reviews` 件集まる |
| DESIGN_REVISE | 実装担当 | P0指摘に基づきIssue本文を修正、`revise` | 全対象Issueに `design_revised` フラグ |
| DESIGN_APPROVED | (自動通過) | 即座にIMPLEMENTATIONに遷移 | - |
| IMPLEMENTATION | CC (自動) | CC自動起動 → Plan + Impl → `commit` | 全Issueに `commit` ハッシュ |
| CODE_REVIEW | レビュアー | コードレビュー、`devbar review` で投稿 | `min_reviews` 件集まる |
| CODE_REVISE | 実装担当 | P0指摘に基づきコード修正 → `commit` + `revise` | 全対象Issueに `code_revised` フラグ |
| CODE_APPROVED | (自動通過) | 即座にMERGE_SUMMARY_SENTに遷移 | - |
| MERGE_SUMMARY_SENT | M (人間) | #dev-barのサマリーに「OK」リプライ | MのOKリプライ検出 |
| DONE | (自動) | git push + issue close → IDLE | 自動遷移 |
| BLOCKED | M (人間) | 手動復旧が必要 | `transition --force --to IDLE` |

### 4.2 自動通過状態

- **DESIGN_APPROVED**: watchdogが検出次第、即座にIMPLEMENTATIONに遷移。CC自動起動。
- **CODE_APPROVED**: watchdogが検出次第、即座にMERGE_SUMMARY_SENTに遷移。サマリー自動投稿。
- **DONE**: git push + issue close → IDLE

### 4.3 REVISEループ

- P0/REJECTが含まれる場合、REVIEW → REVISEに遷移
- REVISE完了後、P0/REJECTを出したレビュアーのレビューのみクリアされる（APPROVE/P1は保持）
- 再レビュー時、既にAPPROVE/P1済みのIssue×レビュアーの組はスキップ
- **最大2サイクル** (`MAX_REVISE_CYCLES=2`)。超過するとBLOCKED

## 5. レビューモード

プロジェクトごとに設定。使用するレビュアーの数を制御。

| モード | レビュアー | 最低レビュー数 |
|--------|-----------|---------------|
| full | pascal, leibniz, hanfei, dijkstra | 3 |
| standard | pascal, leibniz, hanfei | 2 |
| lite | pascal, leibniz | 2 |
| skip | (なし) | 0 (自動承認) |

## 6. タイムアウト

| 状態 | 制限時間 | 延長可能 |
|------|---------|---------|
| DESIGN_PLAN | 15分 | ✅ (最大2回) |
| DESIGN_REVIEW | 30分 | ❌ |
| DESIGN_REVISE | 20分 | ✅ (最大2回) |
| IMPLEMENTATION | 30分 | ✅ (最大2回) |
| CODE_REVIEW | 30分 | ❌ |
| CODE_REVISE | 20分 | ✅ (最大2回) |

- 遷移直後 **180秒** は催促しない (NUDGE_GRACE_SEC)
- 残り **300秒** 未満で延長案内を催促に付加
- 延長は `devbar extend --pj <PJ> --by 600` (デフォルト600秒)
- 延長回数はフェーズごと、DONE時にリセット

## 7. watchdog動作

1. `PIPELINES_DIR` の全 `*.json` をスキャン
2. `enabled=false` ならスキップ
3. `check_transition()` で次のアクションを判定（純粋関数、副作用なし）
4. Double-Checked Locking: ロック内で再判定 + 遷移
5. ロック外で通知（Discord, エージェント送信）

### 7.1 催促

- **実装担当**: 非アクティブ (181秒以上更新なし) の場合のみ `"continue"` を送信
- **レビュアー**: 未完了レビュアーに `"continue"` を送信。送信失敗時は10分後にリトライ
- CC実行中 (`/proc/<pid>` 存在) はアクティブ扱い

### 7.2 CC自動起動 (IMPLEMENTATIONのみ)

- DESIGN_APPROVED → IMPLEMENTATION遷移時に `run_cc=True` → `_start_cc()` で非同期起動
- CCが死んだ場合 (`_is_cc_running()=False`): watchdogの次サイクルで再起動
- **DESIGN_PLANではCC自動起動しない。** 実装担当が手動でIssue確認→`plan-done`する

### 7.3 /new 送信タイミング

- **DESIGN_PLAN遷移時**: レビュアー全員にセッションリセット (`/new`) を送信
- **IMPLEMENTATION遷移時**: 同上 + 実装担当もリセット（PJ変更時のみ）
- **REVISE→REVIEW遷移時**: `/new`は送信しない（コンテキスト維持）

### 7.4 Discord通知

- 全状態遷移を `#dev-bar` に投稿 (形式: `[PJ] OLD → NEW (timestamp)`)
- DESIGN_PLAN開始時のみIssue一覧を別メッセージで投稿
- CC進捗: 📋 Plan開始 → ✅ Plan完了 → 🔨 Impl開始 → ✅ Impl完了
- マージサマリー: 全Issue×全レビュアーの判定を一覧投稿

## 8. pipeline JSON構造

```json
{
  "project": "BeamShifter",
  "gitlab": "atakalive/BeamShifter",
  "repo_path": "/mnt/s/wsl/work/project/BeamShifter",
  "state": "IDLE",
  "enabled": false,
  "implementer": "kaneko",
  "review_mode": "standard",
  "batch": [
    {
      "issue": 17,
      "title": "Issue title",
      "commit": null,
      "cc_session_id": null,
      "design_ready": false,
      "design_reviews": {
        "pascal": {"verdict": "APPROVE", "at": "...", "summary": "..."},
        "leibniz": {"verdict": "P0", "at": "...", "summary": "..."}
      },
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
  "_last_impl_project": "BeamShifter"
}
```

## 9. verdict定義

| Verdict | 意味 | 効果 |
|---------|------|------|
| APPROVE | 承認 | カウント対象。再レビュー時スキップ |
| P0 | 必須修正（ブロッカー） | REVISE遷移トリガー。クリア対象 |
| P1 | 軽微な指摘（ブロックしない） | APPROVE同等扱い。再レビュー時スキップ |
| REJECT | 却下 | P0と同等 |

## 10. 禁止事項

1. **pipeline JSONの直接編集禁止。** 必ずdevbar CLIを使う
2. **実装担当が自分の設計/実装をレビュー(APPROVE)してはならない**
3. **レビュアーが `plan-done`, `commit`, `revise` を実行してはならない**（ロール違反）
4. **DESIGN_PLANでCCを手動起動してはならない。** Issue確認は実装担当の責務
5. **watchdog無効時に手動で状態遷移する場合は `--force` フラグが必要**
