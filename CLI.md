# DevBar CLI マニュアル

> `python3 devbar.py <command> [options]`
> 全コマンドで `--pj` は `--project` のエイリアス。

---

## バッチ開始〜完了の基本フロー

```bash
# 1. バッチ開始（triage + DESIGN_PLAN遷移 + watchdog有効化を一括実行）
python3 devbar.py start --pj BeamShifter --issue 17 18 19 --mode full

# 2. [実装担当] Issue本文を確認・修正後、設計完了を報告
python3 devbar.py plan-done --pj BeamShifter --issue 17 18 19

# --- ここから自動 ---
# watchdog: DESIGN_PLAN → DESIGN_REVIEW（レビュアーに自動通知）
# watchdog: レビュー完了検出 → DESIGN_APPROVED → IMPLEMENTATION（CC自動起動）
# CC: Plan → Impl → devbar commit を自動実行
# watchdog: IMPLEMENTATION → CODE_REVIEW（レビュアーに自動通知）
# watchdog: レビュー完了検出 → CODE_APPROVED → MERGE_SUMMARY_SENT

# 3. [M] #dev-barのサマリーに「OK」リプライ → DONE → git push + issue close → IDLE
```

---

## コマンド一覧

### `status` — 全プロジェクトの状態表示

```bash
python3 devbar.py status
```

出力例:
```
[ON] BeamShifter: CODE_REVIEW  issues=[#17, #18]  ReviewerSize=full  Reviewers=["pascal", "leibniz", "hanfei", "dijkstra"]
  #17: 2/3 reviews (1 APPROVE, 1 P0)
  #18: 3/3 reviews (3 APPROVE)
[OFF] devbar: IDLE  issues=[none]  ReviewerSize=lite  Reviewers=["pascal", "leibniz"]
```

### `start` — バッチ開始（一括操作）

```bash
# Issue番号指定
python3 devbar.py start --pj BeamShifter --issue 17 18 19 --mode full

# GitLabのopen issue全件を自動取得
python3 devbar.py start --pj BeamShifter --mode standard
```

| オプション | 必須 | 説明 |
|-----------|------|------|
| `--pj` | ✅ | プロジェクト名 |
| `--issue N [N...]` | - | Issue番号。省略時はGitLab APIで全open issue取得 |
| `--mode` | - | レビューモード: full/standard/lite/skip |

前提: IDLE状態であること。

### `init` — 新プロジェクト作成

```bash
python3 devbar.py init --pj NewProject --gitlab atakalive/NewProject --repo-path /path/to/repo
```

### `triage` — Issueをバッチに投入

```bash
python3 devbar.py triage --pj BeamShifter --issue 20 21
```

通常は `start` が内部で呼ぶので直接使うことは少ない。

### `plan-done` — 設計完了報告

```bash
python3 devbar.py plan-done --pj BeamShifter --issue 17 18 19
```

**DESIGN_PLAN状態でのみ実行可能。** 実装担当がIssue本文を確認・修正した後に実行する。

### `review` — レビュー結果投稿

```bash
python3 devbar.py review \
  --pj BeamShifter \
  --issue 17 \
  --reviewer pascal \
  --verdict APPROVE \
  --summary $'設計は妥当。\n境界条件の処理も適切。'
```

| オプション | 必須 | 説明 |
|-----------|------|------|
| `--pj` | ✅ | プロジェクト名 |
| `--issue` | ✅ | Issue番号 |
| `--reviewer` | ✅ | レビュアー名 (pascal/leibniz/hanfei/dijkstra/kaneko) |
| `--verdict` | ✅ | APPROVE / P0 / P1 / REJECT |
| `--summary` | - | レビューコメント |

**冪等:** 同一レビュアーによる二重投稿はスキップされる。
**GitLab連携:** verdict + summaryがIssue noteとして自動投稿される。

### `commit` — 実装完了報告

```bash
python3 devbar.py commit --pj BeamShifter --issue 17 18 19 --hash abc1234
```

複数Issueを一つのコミットで解決した場合、全Issue番号を列挙する。

### `design-revise` — 設計修正完了報告

```bash
python3 devbar.py design-revise --pj BeamShifter --issue 17
python3 devbar.py design-revise --pj BeamShifter --issue 17 18
python3 devbar.py design-revise --pj BeamShifter --issue 17 --comment "P0指摘のIssue本文修正完了"
```

**DESIGN_REVISE状態でのみ実行可能。複数Issue番号を並べて一括報告可能。**

### `code-revise` — コード修正完了報告

```bash
python3 devbar.py code-revise --pj BeamShifter --issue 17 --hash f8f7c30
python3 devbar.py code-revise --pj BeamShifter --issue 17 18 19 --hash f8f7c30
python3 devbar.py code-revise --pj BeamShifter --issue 17 --hash f8f7c30 --comment "P0指摘のゼロ除算ガードを追加"
```

**CODE_REVISE状態でのみ実行可能。`--hash` は必須。複数Issue番号を並べて一括登録可能。commit記録 + revise完了フラグを一発で設定する。**

### `extend` — タイムアウト延長

```bash
python3 devbar.py extend --pj BeamShifter --by 600
```

- デフォルト: +600秒
- 対象状態: DESIGN_PLAN, DESIGN_REVISE, IMPLEMENTATION, CODE_REVISE
- **最大2回まで。** 超過するとエラー

### `transition` — 手動状態遷移

```bash
# 通常遷移（バリデーションあり）
python3 devbar.py transition --pj BeamShifter --to CODE_REVIEW

# 強制遷移（バリデーションスキップ）
python3 devbar.py transition --pj BeamShifter --to IDLE --force

# 再開（通知に「（再開）」プレフィックス付与）
python3 devbar.py transition --pj BeamShifter --to DESIGN_PLAN --resume
```

### `review-mode` — レビューモード変更

```bash
python3 devbar.py review-mode --pj BeamShifter --mode full
```

| モード | レビュアー | （レビュアーリストは変更されている可能性が高い）
|--------|-----------|
| full | pascal, leibniz, hanfei, dijkstra |
| standard | pascal, leibniz, hanfei |
| lite | pascal, leibniz |
| skip | (なし、自動承認) |

### `enable` / `disable` — watchdog制御

```bash
python3 devbar.py enable --pj BeamShifter
python3 devbar.py disable --pj BeamShifter
```

### `cc-start` — CC PID記録

```bash
python3 devbar.py cc-start --pj BeamShifter --pid 12345
```

通常はwatchdogが自動で記録するので直接使うことは少ない。

### `merge-summary` — マージサマリー手動投稿

```bash
python3 devbar.py merge-summary --pj BeamShifter
```

CODE_APPROVED状態でのみ実行可能。通常はwatchdogが自動投稿する。

---

## verdict の使い分け

| Verdict | いつ使う |
|---------|---------|
| **APPROVE** | 問題なし。承認 |
| **P0** | Critical。このIssueはREVISEに戻る |
| **P1** | Major。ブロックするように変更 |
| **P2 / Suggestion** | ブロックしない。p2-fix指定時、MAX_TURNまでREVISEを試みる |
| **REJECT** | 根本的に問題あり。P0と同等（使用頻度は低い） |

---

## ログ確認

```bash
# watchdogログ
tail -f /tmp/devbar-watchdog.log

# pipeline JSON確認
cat ~/.openclaw/shared/pipelines/BeamShifter.json | python3 -m json.tool
```

---

## Spec Mode（仕様書レビューパイプライン）

specレビュー → 改訂ループ → Issue起票 → キュー生成を自動化する。

### 基本フロー

```bash
# 1. spec-modeパイプライン開始
python3 devbar.py spec start --pj TrajOpt \
  --spec /mnt/s/wsl/work/project/TrajOpt/docs/SPEC.md --implementer kaneko

# --- ここから自動 ---
# watchdog: SPEC_REVIEW（レビュアーに自動通知）
# watchdog: P0あり → SPEC_REVISE（implementerに改訂指示）
# watchdog: 改訂完了 → SPEC_REVIEW（再レビュー）
# ... REVIEW⇔REVISEループ（max-cycles回まで）
# watchdog: 全員APPROVE → SPEC_APPROVED

# 2. [M] 確認後、Issue化フェーズに進む
python3 devbar.py spec continue --pj TrajOpt

# --- 自動 ---
# ISSUE_SUGGESTION → ISSUE_PLAN → QUEUE_PLAN → SPEC_DONE

# 3. 完了 → IDLEに戻す
python3 devbar.py spec done --pj TrajOpt
```

### コマンド一覧

#### `spec start` — spec-modeパイプライン開始

```bash
python3 devbar.py spec start --pj <PROJECT> --spec <PATH> --implementer <AGENT>
```

| オプション | 説明 |
|---|---|
| `--pj` / `--project` | プロジェクト名（必須） |
| `--spec` | specファイルのパス（cwd相対、必須） |
| `--implementer` | 改訂担当エージェントID（必須） |
| `--review-mode` | full / standard / lite / min |
| `--max-cycles` | REVIEW⇔REVISEの最大ループ数 |
| `--model` | CC使用モデル指定 |
| `--skip-review` | レビュースキップ（即APPROVED） |
| `--review-only` | レビューだけ行いIssue化しない |
| `--no-queue` | キュー生成をスキップ |
| `--auto-continue` | APPROVED後にM確認スキップで自動的にISSUE_SUGGESTIONへ |

#### `spec approve` — 手動でSPEC_APPROVEDに遷移

```bash
python3 devbar.py spec approve --pj <PROJECT> [--force]
```

レビューが膠着した場合等に手動でAPPROVE。`--force` でmin_reviews未到達でも強制遷移。

#### `spec continue` — APPROVED → ISSUE_SUGGESTION

```bash
python3 devbar.py spec continue --pj <PROJECT>
```

SPEC_APPROVED状態でのみ有効。Issue起票フェーズに進む。

#### `spec extend` — STALLED → REVISE（サイクル上限追加）

```bash
python3 devbar.py spec extend --pj <PROJECT> [--cycles 2]
```

max-cyclesに到達してSTALLEDになった場合、サイクル数を追加して続行。

#### `spec retry` — FAILED → REVIEW（やり直し）
#### `spec resume` — PAUSED → 中断前の状態に復帰
#### `spec stop` — spec modeを強制停止

```bash
python3 devbar.py spec stop --pj <PROJECT>
```

任意の状態からspec-modeを中断してIDLEに戻す。watchdogも無効化される。

#### `spec done` — SPEC_DONE → IDLE
#### `spec status` — 現在のspec-mode状態表示

#### `spec review-submit` — レビュー結果の投入

```bash
python3 devbar.py spec review-submit --pj <PROJECT> --reviewer <REVIEWER> --file <FILE>
```

前提: SPEC_REVIEW状態。ファイルはレビュアーのレビュー結果YAML（フェンスあり/なし両対応）。
素YAMLが入力された場合は自動でフェンスを付与してパースする。

#### `spec revise-submit` — SPEC_REVISE完了報告の投入

```bash
python3 devbar.py spec revise-submit --pj <PROJECT> --file <FILE>
```

前提: SPEC_REVISE状態。ファイルはimplementerの改訂完了YAML（フェンスあり/なし両対応）。
パーサー `parse_revise_response` で検証後、`spec_config._revise_response` にフェンス化して格納。
素YAMLが入力された場合は自動でフェンスを付与して保存する（watchdog再パースの整合性確保）。

#### `spec issue-submit` — ISSUE_PLAN完了報告の投入

```bash
python3 devbar.py spec issue-submit --pj <PROJECT> --file <FILE>
```

前提: ISSUE_PLAN状態。ファイルはimplementerのIssue起票完了YAML（フェンスあり/なし両対応）。
パーサー `parse_issue_plan_response` で検証後、`spec_config._issue_plan_response` にフェンス化して格納。

#### `spec queue-submit` — QUEUE_PLAN完了報告の投入

```bash
python3 devbar.py spec queue-submit --pj <PROJECT> --file <FILE>
```

前提: QUEUE_PLAN状態。ファイルはimplementerのキュー生成完了YAML（フェンスあり/なし両対応）。
パーサー `parse_queue_plan_response` で検証後、`spec_config._queue_plan_response` にフェンス化して格納。

#### `spec suggestion-submit` — ISSUE_SUGGESTIONレビュアー提案の投入

```bash
python3 devbar.py spec suggestion-submit --pj <PROJECT> --reviewer <REVIEWER> --file <FILE>
```

前提: ISSUE_SUGGESTION状態かつプロンプト送信済み(sent\_at!=None)。
ファイルはレビュアーのIssue分割提案YAML（フェンスあり/なし両対応）。
パーサー `parse_issue_suggestion_response` で検証後、`current_reviews.entries[REVIEWER]` にフェンス化して格納。
review\_requestsのstatusはwatchdogが更新するためCLI側では触らない。
素YAMLが入力された場合は自動でフェンスを付与して保存する。

### Spec Mode 状態遷移

```
SPEC_REVIEW ⇔ SPEC_REVISE（max-cyclesまでループ）
    ↓ 全員APPROVE
SPEC_APPROVED
    ↓ spec continue
ISSUE_SUGGESTION → ISSUE_PLAN → QUEUE_PLAN → SPEC_DONE
    ↓ spec done
IDLE
```

特殊状態:
- **STALLED**: max-cycles到達。`spec extend` で続行
- **FAILED**: エラー発生。`spec retry` でREVIEWに戻す
- **PAUSED**: 手動中断。`spec resume` で復帰
