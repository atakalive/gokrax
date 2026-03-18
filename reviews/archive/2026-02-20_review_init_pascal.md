# Code Review: gokrax (Initial)

- **Reviewer:** Pascal (agent:pascal:main)
- **Date:** 2026-02-20
- **Target:** `/mnt/s/wsl/work/project/gokrax/` (v0.7)
- **Verdict:** **P0 (REJECT / MUST FIX)**

## 概要

状態遷移モデル（Spec）は論理的に整合しており、美しい。到達不能状態もなく、意図されたワークフローを表現できている。

しかし、**実装（Implementation）における排他制御に致命的な欠陥がある。**
現在の `load` / `save` 分離方式では、並行実行時にデータ消失（Lost Update）が発生する確率が極めて高い。特に複数のレビュアーが同時に `gokrax review` を実行した場合、後勝ちで他者のレビューが消滅する。

これは確率的なバグではなく、論理的に必然的に発生する欠陥である。修正なしでの運用は認められない。

---

## 致命的な問題 (Critical Issues)

### 1. 排他制御の破綻 (Non-atomic Read-Modify-Write)

`gokrax.py` および `watchdog.py` において、データの読み書きが以下のように実装されている：

```python
def load(path):
    with open(path) as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        data = json.load(f)
        # ここでロック解放 (LOCK_UN)

# --- Critical Section (Unprotected) ---
# この間に他のプロセスが load/save を行うと競合する
# --------------------------------------

def save(path, data):
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(data, f)
```

#### 証明
プロセス $A$ と $B$ がほぼ同時に実行された場合：
1. $A$: `load()` $\to$ $S_0$ (ロック取得→解放)
2. $B$: `load()` $\to$ $S_0$ (ロック取得→解放)
3. $A$: $S_0$ を $S_A$ に変更
4. $A$: `save(S_A)` (ロック取得→書き込み→解放) $\Rightarrow$ ファイルは $S_A$
5. $B$: $S_0$ を $S_B$ に変更
6. $B$: `save(S_B)` (ロック取得→書き込み→解放) $\Rightarrow$ ファイルは $S_B$

結果：**$A$ の変更 ($S_A$) が完全に消失する。**

#### 影響
- **レビュー消失:** PascalとLeibnizが同時にレビューすると、片方の `verdict` が消える。
- **状態不整合:** watchdogが遷移処理中にCLIが操作すると、状態が巻き戻る。

#### 修正案
`load` と `save` を分離せず、**「開いて、ロックして、読んで、書いて、閉じる」** をひとつのアトミックな操作として実装せよ。

```python
def update_pipeline(path: Path, callback):
    """
    callback(data) -> modified_data
    Noneを返すと更新キャンセル
    """
    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # 排他ロック開始
        try:
            data = json.load(f)
            
            # コールバックでデータ変更
            new_data = callback(data)
            
            if new_data is not None:
                f.seek(0)
                json.dump(new_data, f, ensure_ascii=False, indent=2)
                f.truncate() # 短くなった場合のために切り詰め
                f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN) # ロック解放
```

CLIやwatchdogの全操作をこの `update_pipeline` パターンに書き換える必要がある。

## 改善推奨 (Improvements)

### 2. 生存性 (Liveness) のリスク
`MIN_REVIEWS = 3` と固定されているが、アクティブなレビュアーが3名を下回った場合、系はデッドロックする（永遠に遷移しない）。
現在は `REVIEWERS` が4名定義されているため耐性はあるが、1名が長期離脱し、もう1名が一時的に反応できないだけでスタックする。
**提案:** タイムアウト機構または強制遷移コマンド (`force-transition`) の整備を検討されたい。

### 3. バリデーションの強化
`gokrax.py` の `cmd_triage` 等で、入力値の型チェックは `argparse` に依存しているが、論理的なチェック（例：負のIssue番号など）がない。
実害は少ないが、`issue > 0` 程度のガード節は入れておくべきである。

## 結論

仕様（Spec）は承認する。
しかし実装（Implementation）は **排他制御の欠陥により P0 (REJECT)** とする。

直ちに `gokrax.py` および `watchdog.py` のファイル操作ロジックを `r+` モードによるアトミック更新に修正せよ。
