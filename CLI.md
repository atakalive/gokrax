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
python3 devbar.py design-revise --pj BeamShifter --issue 17 --comment "P0指摘のIssue本文修正完了"
```

**DESIGN_REVISE状態でのみ実行可能。**

### `code-revise` — コード修正完了報告

```bash
python3 devbar.py code-revise --pj BeamShifter --issue 17 --hash f8f7c30
python3 devbar.py code-revise --pj BeamShifter --issue 17 --hash f8f7c30 --comment "P0指摘のゼロ除算ガードを追加"
```

**CODE_REVISE状態でのみ実行可能。`--hash` は必須。commit記録 + revise完了フラグを一発で設定する。**

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

| モード | レビュアー | 最低レビュー数 |
|--------|-----------|---------------|
| full | pascal, leibniz, hanfei, dijkstra | 3 |
| standard | pascal, leibniz, hanfei | 2 |
| lite | pascal, leibniz | 2 |
| skip | (なし、自動承認) | 0 |

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
| **P0** | 必須修正。このIssueはREVISEに戻る |
| **P1** | 軽微な指摘。ブロックしない（APPROVEと同等扱い） |
| **REJECT** | 根本的に問題あり。P0と同等 |

---

## ログ確認

```bash
# watchdogログ
tail -f /tmp/devbar-watchdog.log

# pipeline JSON確認
cat ~/.openclaw/shared/pipelines/BeamShifter.json | python3 -m json.tool
```
