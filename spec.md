# DevBar: マルチエージェント開発パイプライン仕様書

**Version:** 0.6
**Author:** Asuka (second)
**Date:** 2026-02-19

## 1. 背景と動機

### 1.1 現状の問題

2026-02-19のImageRestorationNN開発（Issue #30-#32）で以下の問題が顕在化した：

1. **状態がエージェントの頭の中にしかない** — セッション障害（OAuth 401）やcompactionで文脈が消えると、作業状態が不明になる
2. **直列実行のボトルネック** — 金子（reviewer00）が1 Issueずつ直列処理。3 Issueで数時間
3. **レビュー工程のスキップ** — 自動化の速度にプロセスが追いつかず、品質ゲートが飛ばされた
4. **障害復旧に人手が必要** — OAuth失効で1時間以上停止。Mの介入なしに復旧できなかった

### 1.2 目標

- PJ単位の開発状態を永続ファイルで管理し、セッション障害に耐える
- 状態遷移ルールにより品質ゲート（レビュー必須）を強制する
- 複数PJの並列処理を可能にする
- Mへの可視性を確保する（Discord通知、状態確認）

## 2. 基本モデル

### 2.1 PJごとに1つの状態

DevBarの核心は **PJ（プロジェクト）ごとに1つの状態** を持つこと。

1つのPJには複数のアクティブIssueが乗る。PJの状態遷移はバッチ単位で行われ、全Issueが現フェーズを完了してから次のフェーズに進む。

```
ImageRestorationNN:
  state: DESIGN_REVIEW
  active_issues: [#32, #33, #34]
  ← 全Issueのレビューが完了するまでこの状態に留まる
```

### 2.2 なぜIssueごとではないか

- PJ単位なら状態管理がシンプル（1 PJ = 1状態）
- 金子のポーリングは「各PJの状態を見る」だけ
- 同一PJ内でIssueが異なるフェーズにいると、コンフリクトや管理の複雑さが爆発する
- バッチ処理のほうがレビュー効率も良い（まとめて出す → まとめて返る）

## 3. 用語

| 用語 | 定義 |
|------|------|
| PJ (Project) | GitLabリポジトリに対応する開発対象。状態を1つ持つ |
| Batch | PJの現フェーズで処理対象となるIssueの集合 |
| State | PJの現在の開発フェーズ |
| Actor | 状態遷移を実行するエージェント（金子、Pascal等） |
| Implementer | 設計・実装を担当するエージェント（現状は金子） |
| Reviewer | レビューを担当するエージェント（Pascal、Leibniz、Dijkstra、韓非） |

## 4. 状態遷移モデル

### 4.1 開発の入口

Issueの発生源は3種類あるが、パイプライン上は全て **TRIAGE** に入る。

| 入口 | 内容 | 備考 |
|------|------|------|
| **M Issue** | Mが自然言語で希望を書く | Implementerがリライトしてからバッチへ |
| **コードレビュー指摘** | 全体レビューで発見された問題 | MがImplementerにIssue起票を直接依頼 |
| **新機能** | 大きめの機能追加 | Mが設計会議でspecを固めてからIssue化。パイプライン外で完了済み |

Implementer自身がIssue起票する場合は、既に整理済みなのでTRIAGEを素通りしてバッチに入る。

### 4.2 状態一覧

```
TRIAGE                  Implementerが未整理Issueをリライト・バッチ投入判断
IDLE                    アクティブバッチなし
DESIGN_PLAN             バッチ内全Issueの設計をCC Planモードで作成中
DESIGN_REVIEW           レビュアーに設計を送付、レビュー待ち
DESIGN_REVISE           レビュー指摘（P0）を反映中
DESIGN_APPROVED         設計レビュー通過（全Issue）
IMPLEMENTATION          CC CLI bypassPermissionsで実装中
CODE_REVIEW             レビュアーにコードを送付、レビュー待ち
CODE_REVISE             コードレビュー指摘（P0）を反映中
CODE_APPROVED           コードレビュー通過（全Issue）
MERGE_SUMMARY_SENT      Mにマージサマリー送信済み
DONE                    MがOK → マージ完了 → IDLEに戻る
BLOCKED                 外部要因で進行不可
```

### 4.3 状態遷移図

```
  [M / reviewer / implementer] Issue起票
  │
  ▼
TRIAGE
  │ [implementer] 必要ならリライト、バッチに投入
  │ （自分で起票したIssueはそのままバッチへ）
  ▼
IDLE
  │ [implementer] バッチにIssueを積む
  ▼
DESIGN_PLAN
  │ [implementer] 全Issueの設計完了
  ▼
DESIGN_REVIEW
  │ [reviewers] 全Issueのレビュー返却
  ├─ P0あり → DESIGN_REVISE → DESIGN_REVIEW（ループ）
  │
  │ P0なし
  ▼
DESIGN_APPROVED
  │ [implementer] 実装開始
  ▼
IMPLEMENTATION
  │ [implementer] 全Issueの実装+テスト完了
  ▼
CODE_REVIEW
  │ [reviewers] 全Issueのレビュー返却
  ├─ P0あり → CODE_REVISE → CODE_REVIEW（ループ）
  │
  │ P0なし
  ▼
CODE_APPROVED
  │ [implementer] Mにサマリー送信
  ▼
MERGE_SUMMARY_SENT
  │ [M] OK指示
  ▼
DONE → IDLE
```

### 4.3 REVIEW → REVISE ループ

- REVIEW中にP0（必須修正）が1つでもあれば → REVISE
- REVISEで修正完了 → 再度REVIEW
- P0がなくなるまでループ
- P1（改善提案）はAPPROVEと共存可。ループには入らない

### 4.4 レビュー完了条件

- **設計レビュー**: バッチ内全Issueについて3件以上のレビューコメント、P0なし
- **コードレビュー**: 同上
- レビュアーの選出はしない。全レビュアーに投げ、最低コメント数で判定する

## 5. データモデル

### 5.1 パイプライン状態ファイル

PJごとに1ファイル。

```json
{
  "project": "ImageRestorationNN",
  "gitlab": "atakalive/ImageRestorationNN",
  "state": "CODE_REVIEW",
  "implementer": "reviewer00",
  "updated_at": "2026-02-19T19:15:00+09:00",
  "batch": [
    {
      "issue": 32,
      "title": "チェックポイント選択時の自動描画廃止",
      "commit": "1dbbade",
      "cc_session_id": "050b780f-...",
      "design_reviews": {
        "g-reviewer": {"verdict": "APPROVE", "at": "2026-02-19T18:30:00+09:00"},
        "c-reviewer": {"verdict": "APPROVE", "at": "2026-02-19T18:32:00+09:00"}
      },
      "code_reviews": {
        "g-reviewer": {"verdict": "APPROVE", "at": "2026-02-19T19:31:00+09:00"},
        "c-reviewer": {"verdict": "PASS_P1", "summary": "stale overlay提案", "at": "2026-02-19T19:31:00+09:00"}
      }
    },
    {
      "issue": 33,
      "title": "...",
      "commit": null,
      "design_reviews": {},
      "code_reviews": {}
    }
  ],
  "history": [
    {"from": "IDLE", "to": "DESIGN_PLAN", "at": "2026-02-19T18:00:00+09:00", "actor": "reviewer00"},
    {"from": "DESIGN_PLAN", "to": "DESIGN_REVIEW", "at": "2026-02-19T18:20:00+09:00", "actor": "reviewer00"}
  ]
}
```

### 5.2 ファイル配置

```
/home/ataka/.openclaw/shared/pipelines/
  ImageRestorationNN.json
  PaperScreening.json
  TrajOpt.json
```

shared/ 以下で全エージェントからアクセス可能。

## 6. アクターの役割

### 6.1 Watchdog（Pythonスクリプト、LLM不要）

パイプラインオーケストレーター。cronで1分間隔実行。`watchdog.py` 参照。

- pipeline JSONを巡回し、条件を満たした状態遷移を自動実行
- 遷移時にアクター（金子等）を `openclaw session send` で通知
- 冪等。何回実行しても同じ結果。LLMトークン消費ゼロ

| 検知する遷移 | 条件 | アクション |
|-------------|------|-----------|
| TRIAGE: 未処理Issueあり | GitLabにopen Issue & バッチ未投入 | Implementerに通知 |
| DESIGN_REVIEW 開始時 | 状態遷移直後 | 全レビュアーにsessions_sendで設計レビュー依頼 |
| DESIGN_REVIEW → DESIGN_APPROVED | 全Issue 3件以上レビュー、P0なし | Implementerに通知 |
| DESIGN_REVIEW → DESIGN_REVISE | 3件以上レビュー、P0あり | Implementerに通知 |
| DESIGN_REVISE → DESIGN_REVIEW | 全Issue revised フラグ | 全レビュアーに再レビュー依頼 |
| IMPLEMENTATION → CODE_REVIEW | 全Issue commit あり | 全レビュアーにコードレビュー依頼 |
| CODE_REVIEW → CODE_APPROVED | 全Issue 3件以上レビュー、P0なし | Implementerに通知 |
| CODE_REVIEW → CODE_REVISE | 3件以上レビュー、P0あり | Implementerに通知 |
| CODE_REVISE → CODE_REVIEW | 全Issue revised フラグ | 全レビュアーに再レビュー依頼 |
| DONE → IDLE | — | バッチクリア |

### 6.2 Implementer（金子 / reviewer00）

watchdogから通知を受けて作業する。能動的なポーリングは不要。
pipeline JSONの操作は **devbar CLI** 経由で行う（直接JSON編集禁止）。

| PJ状態 | アクション |
|--------|-----------|
| TRIAGE | 未整理Issueをリライト。バッチに投入 |
| IDLE | バッチにIssueを積み、DESIGN_PLANに遷移 |
| DESIGN_PLAN | 各IssueをCC Planモードで設計。全Issue完了 → DESIGN_REVIEW に遷移 |
| DESIGN_REVISE | P0指摘を反映。revised フラグを立てる |
| DESIGN_APPROVED | IMPLEMENTATION に遷移 |
| IMPLEMENTATION | 各IssueをCC bypassPermissionsで実装+テスト。commit を記録 |
| CODE_REVISE | P0指摘を反映。revised フラグを立てる |
| CODE_APPROVED | Mにサマリー送信 → MERGE_SUMMARY_SENT |

### 6.3 Reviewers（Pascal / Leibniz / Dijkstra / 韓非）

- DESIGN_REVIEW / CODE_REVIEW 中にwatchdog経由でsessions_sendの依頼を受け取る
- verdict（APPROVE / P0 / P1）を **devbar CLI** 経由でpipeline JSONに書き込み
- glab issue note でIssueコメントにも記録

### 6.4 M（人間）

- MERGE_SUMMARY_SENT → DONE: OK指示
- バッチに積むIssueの指定
- 任意の介入（BLOCKED設定、優先度変更）

## 7. watchdog運用

### 7.1 実行方法

```
* * * * * python3 /mnt/s/wsl/work/project/devbar/watchdog.py
```

cron自体は常時動くが、PJごとに `enabled` フラグで制御する。

### 7.2 PJの開始と停止

- **開始**: MがAsuka等に「PJ XXのwatchdog開始して」と依頼 → `devbar enable --project PJ`
- **自動停止**: DONE → IDLE 遷移時にwatchdogが自動で `enabled: false` にする
- **手動停止**: `devbar disable --project PJ`
- watchdogは `enabled: true` のPJだけ処理する

### 7.3 特性

- **LLM不要**: if文のみ。トークン消費ゼロ
- **冪等**: 条件満たさなければ何もしない
- **flock排他**: pipeline JSONの読み書きは排他ロック
- **ログ**: `/tmp/devbar-watchdog.log` に遷移記録
- **全PJ無効時**: 何もせず即終了

## 8. 障害耐性

| 障害 | 復旧 |
|------|------|
| セッション切れ | pipeline JSON読み直しで即復旧 |
| Compaction | 外部ファイルなので影響なし |
| OAuth失効 | 復旧後にpipeline JSON読むだけ |
| CC CLI中断 | cc_session_idで `--resume` 可能 |

## 9. Discord通知

状態遷移時に専用チャンネル（例: `#dev-pipeline`）に通知：

```
[ImageRestorationNN] DESIGN_REVIEW → DESIGN_APPROVED
  Issues: #32, #33, #34
  Reviews: Pascal ✅, Leibniz ✅ (P1: 2件)
```

```
[ImageRestorationNN] CODE_REVIEW → CODE_REVISE
  Issues: #32 (P0: Leibniz — 非原子的書込)
```

## 10. 実装方針

### 10.1 Phase 1（MVP）

- **devbar CLI**（Python）: pipeline JSON操作の唯一のインターフェース
  - `devbar status` — 全PJ状態表示
  - `devbar triage --project PJ --issue N` — Issueをバッチに投入
  - `devbar transition --project PJ --to STATE` — 状態遷移（バリデーション付き）
  - `devbar review --project PJ --issue N --reviewer ID --verdict APPROVE` — レビュー結果記録
  - `devbar commit --project PJ --issue N --hash HASH` — commit記録
  - `devbar revise --project PJ --issue N` — revised フラグ設定
- 状態遷移バリデーション（不正な遷移を拒否）
- flock排他制御
- watchdog.py（cron 1分間隔）

### 10.2 Phase 2

- レビュアーの自発的ポーリング
- Discord通知チャンネル
- 複数Implementer対応
- 簡易Webダッシュボード

### 10.3 Phase 3

- GitLab API連携（Issue自動取得）
- 優先度キュー
- SLA監視（滞留アラート）

## 11. 制約と前提

- **flock必須**: pipeline JSONへの書き込みは `fcntl.flock(LOCK_EX)` で排他
- **GitLab Issueが正**: pipeline JSONはワークフロー状態。Issue本体はGitLabに残る
- **glab issue note 必須**: レビュー結果はpipeline JSONとGitLab Issue両方に記録
- **Mの承認なしにマージしない**: MERGE_SUMMARY_SENT → DONE はM専用
- **CC Plan必須**: DESIGN_PLANをスキップしない
- **PJごとに1状態**: 同一PJ内のIssueは同じフェーズを一緒に進む
- **テスト失敗はレビューで検知**: テストはIMPLEMENTATIONでCCが書く+通す。レビューで不足指摘 → CODE_REVISE で修正
- **pipeline JSON直接編集禁止**: 全操作はdevbar CLI経由

## 12. 決定事項（2026-02-19）

- **バッチサイズ上限: 5** — CC CLIで同時に扱える現実的な上限
- **レビュアー選出: しない** — 全レビュアーに投げる。最低コメント数（現状3）で遷移判定
- **レビュー依頼: watchdogが自動送信** — 状態がREVIEWに遷移したら全レビュアーにsessions_send
- **TRIAGE発火: watchdogが金子を起こす** — 未処理Issueを検知したらImplementerに通知
- **コードレビュー起点のIssue**: Mが金子に直接依頼してIssue起票させる（パイプライン外）
- **Mへのサマリー: フォーマット自由** — 金子がまとめてDiscordに送る
- **テスト失敗: CODE_REVISEで対応** — 独立した状態は持たない
- **pipeline JSON操作: devbar CLI経由** — 直接編集禁止
- **軽微Issue簡略化: 後日検討**
- **BLOCKED: 後日検討**
- **devbar自体の開発: 現状の枠組み（金子 + CC + レビュアー）で進める**

## 13. 未決事項

- [ ] pipeline JSONのバージョニング（Git管理? shared/直置き?）
- [ ] Implementer不在時のフォールバック
- [ ] レビュー応答のタイムアウト
- [ ] BLOCKED → 復帰の遷移条件
- [ ] 軽微Issue（1行修正等）の簡略パイプライン
