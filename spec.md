# DevPilot: マルチエージェント開発パイプライン仕様書

**Version:** 0.1 (draft)
**Author:** Asuka (second)
**Date:** 2026-02-19

## 1. 背景と動機

### 1.1 現状の問題

2026-02-19のImageRestorationNN開発（Issue #30-#32）で以下の問題が顕在化した：

1. **状態がエージェントの頭の中にしかない** — セッション障害（OAuth 401）やcompactionで文脈が消えると、作業状態が不明になる
2. **直列実行のボトルネック** — 金子AI（reviewer00）が1 Issueずつ CC CLI → レビュー依頼 → レビュー待ち → Issueコメント → サマリーを直列処理。3 Issue で数時間
3. **レビュー工程のスキップ** — 自動化の速度にプロセスが追いつかず、品質ゲートが飛ばされた
4. **障害復旧に人手が必要** — OAuth失効で1時間以上停止。Mの介入なしに復旧できなかった
5. **並列化不可** — 1エージェント＝1セッションのため、プロジェクト横断の並列作業ができない

### 1.2 目標

- Issue単位の開発状態を永続ファイルで管理し、セッション障害に耐える
- 状態遷移ルールにより品質ゲート（レビュー必須等）を強制する
- 複数プロジェクト・複数Issueの並列処理を可能にする
- Mへの可視性を確保する（Discord通知、状態確認コマンド）

## 2. 用語

| 用語 | 定義 |
|------|------|
| Pipeline | 1つのIssueが OPEN → DONE に至るまでの状態遷移の流れ |
| State | Issueの現在の開発段階 |
| Actor | 状態遷移を実行するエージェント（金子AI、Pascal等） |
| Transition | ある状態から次の状態への遷移。条件と実行者を持つ |
| Project | GitLabリポジトリに対応する開発対象 |

## 3. 状態遷移モデル

### 3.1 状態一覧

```
OPEN                    Issueが作成された初期状態
DESIGN_PLAN             CC CLI Planモードで設計中
DESIGN_REVIEW           レビュアーに設計を送付、レビュー待ち
DESIGN_APPROVED         設計レビュー通過
IMPLEMENTATION          CC CLI bypassPermissionsで実装中
CODE_REVIEW             レビュアーにコードを送付、レビュー待ち
MERGE_READY             レビュー全員完了
MERGE_SUMMARY_SENT      Mにマージサマリー送信済み
DONE                    MがOK → マージ完了
BLOCKED                 外部要因で進行不可
```

### 3.2 状態遷移図

```
OPEN
  │ [implementer] CC Plan開始
  ▼
DESIGN_PLAN
  │ [implementer] Plan完了、レビュアーに送付
  ▼
DESIGN_REVIEW
  │ [reviewers] 必要数のAPPROVE取得
  ▼
DESIGN_APPROVED
  │ [implementer] CC実装開始
  ▼
IMPLEMENTATION
  │ [implementer] 実装+テスト完了、レビュアーに送付
  ▼
CODE_REVIEW
  │ [reviewers] 必要数のAPPROVE取得
  ▼
MERGE_READY
  │ [implementer] Mにサマリー送信
  ▼
MERGE_SUMMARY_SENT
  │ [M] OK指示
  ▼
DONE
```

任意の状態から `BLOCKED` に遷移可能（理由を記録）。
`CODE_REVIEW` で REJECT が出た場合は `IMPLEMENTATION` に差し戻し。
`DESIGN_REVIEW` で REJECT が出た場合は `DESIGN_PLAN` に差し戻し。

### 3.3 レビュー完了条件

- **設計レビュー**: 2名以上のAPPROVE（REJECT 0）
- **コードレビュー**: 2名以上のAPPROVE（REJECT 0）
- P1（改善提案）はAPPROVEと共存可。P0（必須修正）はREJECT扱い

## 4. データモデル

### 4.1 パイプライン状態ファイル

各プロジェクトに `dev-pipeline.json` を配置。

```json
{
  "project": "ImageRestorationNN",
  "gitlab": "atakalive/ImageRestorationNN",
  "issues": {
    "32": {
      "title": "チェックポイント選択時の自動描画廃止",
      "state": "CODE_REVIEW",
      "implementer": "reviewer00",
      "created_at": "2026-02-19T18:00:00+09:00",
      "updated_at": "2026-02-19T19:15:00+09:00",
      "cc_session_id": "050b780f-4a4d-4876-a8a8-a3d59941b91e",
      "commit": "1dbbade",
      "design_reviews": {
        "g-reviewer": {"verdict": "APPROVE", "at": "2026-02-19T18:30:00+09:00"},
        "c-reviewer": {"verdict": "APPROVE", "at": "2026-02-19T18:32:00+09:00"}
      },
      "code_reviews": {
        "g-reviewer": {"verdict": "APPROVE", "at": "2026-02-19T19:31:00+09:00"},
        "c-reviewer": {"verdict": "PASS_P1", "summary": "stale overlay提案", "at": "2026-02-19T19:31:00+09:00"}
      },
      "history": [
        {"from": "OPEN", "to": "DESIGN_PLAN", "at": "2026-02-19T18:00:00+09:00", "actor": "reviewer00"},
        {"from": "DESIGN_PLAN", "to": "DESIGN_REVIEW", "at": "2026-02-19T18:20:00+09:00", "actor": "reviewer00"},
        {"from": "DESIGN_REVIEW", "to": "DESIGN_APPROVED", "at": "2026-02-19T18:35:00+09:00", "actor": "system"},
        {"from": "DESIGN_APPROVED", "to": "IMPLEMENTATION", "at": "2026-02-19T18:36:00+09:00", "actor": "reviewer00"},
        {"from": "IMPLEMENTATION", "to": "CODE_REVIEW", "at": "2026-02-19T19:15:00+09:00", "actor": "reviewer00"}
      ]
    }
  }
}
```

### 4.2 ファイル配置

```
/home/ataka/.openclaw/shared/pipelines/
  ImageRestorationNN.json
  PaperScreening.json
  TrajOpt.json
```

shared/ 以下に置くことで全エージェントから memory_search / 直接読み書き可能。

## 5. アクターの役割

### 5.1 Implementer（金子AI / reviewer00）

- OPEN → DESIGN_PLAN: CC CLI Planモードで設計
- DESIGN_PLAN → DESIGN_REVIEW: 設計をレビュアーにsessions_send
- DESIGN_APPROVED → IMPLEMENTATION: CC CLI bypassPermissionsで実装
- IMPLEMENTATION → CODE_REVIEW: コードをレビュアーにsessions_send
- MERGE_READY → MERGE_SUMMARY_SENT: MにDiscordでサマリー送信

### 5.2 Reviewers（Pascal / Leibniz / Han Fei）

- DESIGN_REVIEW中: 設計を受け取り、verdict（APPROVE/REJECT）をpipeline JSONに書き込み
- CODE_REVIEW中: コードを受け取り、verdictをpipeline JSONに書き込み
- glab issue note でIssueコメントも記録

### 5.3 System（自動遷移）

- DESIGN_REVIEW → DESIGN_APPROVED: レビュー完了条件を満たしたら自動遷移
- CODE_REVIEW → MERGE_READY: 同上
- CODE_REVIEW → IMPLEMENTATION（差し戻し）: P0/REJECTがあれば

### 5.4 M（人間）

- MERGE_SUMMARY_SENT → DONE: OKを出す
- 任意の状態介入（BLOCKED設定、Issue追加、優先度変更）

## 6. ポーリングモデル

### 6.1 基本方針

イベント駆動ではなくポーリング方式を採用する。理由：
- sessions_sendはセッションが寝ていると届かない
- cronベースのwatchdogが既に実証済み
- ファイルベースの状態なら障害復旧が容易

### 6.2 Implementerのポーリング

金子AIのwatchdog cron（1分間隔）で `dev-pipeline.json` を読み、以下を判断：

1. 自分がアサインされたIssueで、自分の番の状態があるか？
2. あればそのIssueの作業を開始/継続
3. なければ次のプロジェクトのpipeline JSONを確認
4. 全プロジェクトで作業なし → NO_REPLY

### 6.3 Reviewerのポーリング

レビュアーは現状sessions_sendで受動的に受け取る方式。
将来的にはcronでpipeline JSONを巡回し、自分がレビューすべきIssueを自発的に取る方式に移行可能。

### 6.4 System遷移の実行タイミング

Implementerのポーリング時に判定する。
- pipeline JSONを読む → レビュー結果が揃っているか確認 → 揃っていれば自動遷移

## 7. 障害耐性

### 7.1 セッション障害

- 状態がファイルに永続化されているため、セッション再起動後にpipeline JSONを読めば復旧可能
- CC CLIのsession_idもpipeline JSONに記録されているため、`--resume`で継続可能

### 7.2 Compaction

- pipeline JSONが外部ファイルのため、compactionの影響を受けない
- エージェントはcompaction後もpipeline JSONを読み直すだけで文脈復元

### 7.3 OAuth失効

- 復旧後、pipeline JSONの状態を読んで作業を再開するだけ
- 「何をやっていたか」を思い出す必要がない

## 8. Discord通知

状態遷移時にDiscordの専用チャンネル（例: `#dev-pipeline`）に通知する。

```
[ImageRestorationNN #32] IMPLEMENTATION → CODE_REVIEW
  commit: 1dbbade
  reviewers: Pascal, Leibniz
```

```
[ImageRestorationNN #32] CODE_REVIEW → MERGE_READY
  Pascal: APPROVE
  Leibniz: PASS (P1: stale overlay)
```

## 9. 実装方針

### 9.1 Phase 1（MVP）

- `dev-pipeline.json` の読み書きライブラリ（Python）
- 状態遷移バリデーション（不正な遷移を拒否）
- flock による排他制御（複数エージェントの同時書き込み防止）
- 金子AIのwatchdog cronからの呼び出し
- CLIコマンド: `status`（現在状態表示）、`transition`（状態遷移実行）

### 9.2 Phase 2

- レビュアーの自発的ポーリング（cron巡回）
- Discord通知チャンネル
- 複数Implementer対応（プロジェクトごとに異なるImplementer）
- ダッシュボード（簡易Webビュー）

### 9.3 Phase 3

- 自動Issue取得（GitLab APIからopen Issueを読んでpipelineに追加）
- 優先度キュー
- SLA監視（特定状態に長時間滞留したらアラート）

## 10. 制約と前提

- **flock必須**: pipeline JSONへの書き込みは `fcntl.flock(LOCK_EX)` で排他
- **GitLab Issueが正**: pipeline JSONはキャッシュ/ワークフロー状態。Issue本体はGitLabに残る
- **glab issue note 必須**: レビュー結果はpipeline JSONだけでなくGitLab Issueにも記録
- **Mの承認なしにマージしない**: MERGE_SUMMARY_SENT → DONE はM専用
- **CC Plan必須**: DESIGN_PLAN をスキップして直接 IMPLEMENTATION に行くことは禁止

## 11. 未決事項

- [ ] プロジェクト名（DevPilot? Kōjō? 別案?）
- [ ] レビュアーの選出ルール（全員? ランダム2名? プロジェクトごとに固定?）
- [ ] 軽微なIssue（1行修正等）の簡略パイプライン
- [ ] pipeline JSONのバージョニング（Git管理? shared/直置き?）
- [ ] Implementer不在時のフォールバック（別エージェントが代行?）
- [ ] レビュー応答のタイムアウト（何分待ってリマインド?）
