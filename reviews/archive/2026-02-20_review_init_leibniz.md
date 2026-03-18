# gokrax 全文レビュー（Leibniz）

対象: spec.md / config.py / gokrax.py / watchdog.py / notify.py

## Verdict: **P0**
主因は **pipeline JSON の書き込みが原子的でなく、同時読取/書込で破損し得る** 点（gokrax.save / watchdog.save_pipeline）。これは状態機械の基盤を壊すのでP0です。加えて、状態遷移の前提条件（空バッチ等）の検証不足がP1相当で複数あります。

---

## P0（必須修正）

### P0-1: JSON書き込みが非原子的（truncate→lock の順）で破損し得る
- gokrax.py: `save()`
- watchdog.py: `save_pipeline()`

現状:
```py
with open(path, "w") as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    json.dump(...)
```
`open(...,"w")` の時点で **ファイルは即truncate** されます。ここで同時に別プロセスが `load()` すると、空/途中のJSONを読んで `JSONDecodeError`、あるいは空状態を「正」と誤認する危険がある。

要求仕様（spec 7.3, 11: flock排他・冪等）と整合しません。排他は「truncate前」に成立していなければならない。

**修正案（いずれか）**
1) **temp file + fsync + rename**（推奨）
   - `path.with_suffix('.json.tmp')` に書き込み→`os.replace(tmp, path)`。
   - 併せて lock を「本体ファイル」ではなく **別の lockfile**（例: `path.with_suffix('.lock')`）で取ると設計が安定。
2) 本体を `open(path, "r+")` で開き、lock取得後に `truncate()`→書込→`flush()`→`os.fsync()`。

どちらでも良いが、(1) が「読取は常に完全な旧版 or 新版」にでき、状態機械に向く。

### P0-2: watchdogの例外ハンドリングが「ログのみ」で通知失敗が観測不能
watchdog.py:
```py
except Exception as e:
    log(f"[{path.stem}] ERROR: {e}")
```
通知（Discord/agent）失敗、JSONDecodeError 等の **致命障害** が起きても、/tmpログに沈むだけ。
cron運用では事実上ブラックホールです。

最低限:
- `process()` 例外を Discord にも投げる（best-effort）。
- `JSONDecodeError` は「破損検知」なので別扱い（復旧手順/バックアップ導線が必要）。

---

## P1（改善推奨：境界条件・形式性）

### P1-1: spec.md の版数不一致
冒頭に **Version: 0.6** とある一方、依頼文では「仕様書 v0.7」。レビュー/運用の参照点が揺れるので、specのヘッダを更新するか、変更履歴を明記すべき。

### P1-2: 状態遷移の前提条件（不変条件）をCLIで検証していない
gokrax.py `cmd_transition()` は「許可遷移」だけを検証し、**状態に付随する不変条件**を検証しない。
例:
- `IDLE → DESIGN_PLAN` を **空バッチでも許す**（watchdogは `batch empty` でWARNINGして停止）。
- `DESIGN_PLAN → DESIGN_REVIEW` もCLIで可能だが、`design_ready` の充足を検証しない（watchdogは自動遷移のみだが、CLIを唯一インターフェースとするならCLI側が守るべき）。

提案:
- `transition` に状態ごとの precondition を実装（例: DESIGN_PLANは batch非空、CODE_REVIEWは全issueにcommit等）。

### P1-3: 重複レビュー（上書き）を黙って許す
`cmd_review()`:
```py
issue[key][args.reviewer] = review_entry
```
同一 reviewer の再投稿で **履歴なく上書き**されます。
- 良い面: 冪等（同一入力なら同一状態）。
- 悪い面: 「いつ誰がverdictを変えたか」が消える（監査性がない）。

提案:
- 既存がある場合は拒否し `--force` でのみ上書き。
- あるいは `history` に「review_updated」イベントを残す。

### P1-4: reviewer/agent_id の整合性チェックがない（データモデルが緩すぎる）
- `cmd_review` は `--reviewer` を任意文字列で受け、`issue[key]` のキーにする。
- `notify.notify_implementer()` は引数名が `agent_id` だが、watchdog側からは `implementer="kaneko"` のように **人名キー**が入っている（たまたまAGENTS定義と一致している前提）。

提案:
- `--reviewer` は `choices=config.REVIEWERS` に制限。
- pipeline JSON の `implementer` は `AGENTS` のキー型に固定し、バリデーション。

### P1-5: load側も lock のタイミングが最適ではない
`load()` / `load_pipeline()` は `open()` 後にLOCK_SH。これは通常問題になりにくいが、P0-1 のような truncate race があると致命になる。
P0修正（原子書込）を入れれば実害は大きく減る。

---

## 仕様・実装整合の所見（形式的厳密性）

### 状態機械としての整合性
- `VALID_STATES` と `VALID_TRANSITIONS` は明示列挙で良い（Ars combinatoria 的に有限状態を明確化している）。
- しかし「状態×データ」の不変条件がコード上で形式化されていないため、**到達可能だが意味をなさない状態**（例: DESIGN_PLAN かつ batch空）が存在する。

### Watchdogの冪等性
- 条件成立時のみ遷移、成立しなければ何もしない、という意味では冪等。
- ただしP0-1の非原子書込があると「同じ入力→同じ出力」が崩れる（破損で分岐する）。

---

## 具体的エッジケース評価（質問への直接回答）

- **空バッチ**: 
  - CLIが不正遷移を許し得る（P1-2）。watchdogは検知してWARNINGで停止するが、回復操作は別途必要。
- **重複レビュー**:
  - 同一reviewerの再レビューは上書き。多重投稿は辞書サイズが増えず、`MIN_REVIEWS` の判定にも影響しにくいが、監査性がない（P1-3）。
- **同時書き込み**:
  - flockを使っているが、現実装は truncate→lock のため **排他の意味が壊れている**（P0-1）。

---

## 付記（軽微）
- notify.py は `requests` 依存だがrequirements管理が見当たらない（運用で落ちやすい）。
- README.md がGitLabテンプレのまま（仕様/運用手順はspecにあるので致命ではない）。

Calculemus! —— 状態機械は「原子性」と「不変条件」の2本柱で初めて計算可能になります。