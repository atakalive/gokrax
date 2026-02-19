# DevBar 初回全文レビュー — Kaneko AI

**日付:** 2026-02-20
**レビュアー:** Kaneko AI (kaneko)
**対象:** spec.md (v0.6), config.py, devbar.py, watchdog.py, notify.py
**Verdict: P0**（致命的バグ2件。修正後に再レビューしたい）

---

## 全体所感

設計の方向性はいい。PJ単位の状態管理 + JSON永続化 + LLMゼロのwatchdog、これはImageRestorationNNの時に起きた問題を正確に解決してる。「セッション切れても状態が残る」「品質ゲートを飛ばせない」という2大問題をシンプルに潰してるのは好印象。

CLIのサブコマンド設計も、実装リードとして使うイメージで違和感ない。`devbar triage → transition → plan-done → commit → review` の流れは自然。ただ、いくつかマズいのがある。

---

## 🔴 致命的 (P0)

### P0-1: flock と open("w") の競合（devbar.py, watchdog.py）

`save()` / `save_pipeline()` の両方で:

```python
with open(path, "w") as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    json.dump(data, f, ...)
```

`open(path, "w")` の時点でファイルが **truncate（0バイト）** される。flock はその後。つまり truncate → flock の間に別プロセスが read すると空ファイルを読む。watchdog が cron で1分間隔、CLI が手動実行、両方が同じJSONを触るから実際に起きうる。

**修正案:**

```python
def save(path: Path, data: dict):
    data["updated_at"] = now_iso()
    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.truncate(0)
        f.seek(0)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)
```

もしくは atomic write（tmpfile + rename）がベスト。rename は POSIX で atomic なのでロック不要になる:

```python
import tempfile, os

def save(path: Path, data: dict):
    data["updated_at"] = now_iso()
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except:
        os.unlink(tmp)
        raise
```

### P0-2: notify.py の `openclaw agent` コマンドが存在しない可能性

```python
subprocess.run(
    ["openclaw", "agent", "--agent", agent_id, "--message", message, ...],
```

OpenClaw CLI のサブコマンドに `agent` があるか確認した？ドキュメント上は `openclaw gateway`, `openclaw status` 等は見たことあるけど、`openclaw agent --message` は見たことがない。これが存在しないと **全通知が死ぬ**。watchdog の心臓部なのに通知が全滅するのは致命的。

**確認方法:** `openclaw --help` か `openclaw agent --help` で存在確認。なければ代替手段を考える必要がある。候補:
- `openclaw session send --session-key agent:kaneko:main --message "..."` （あるかも？）
- HTTP API 直叩き（gateway の REST endpoint）
- notify.py 内で Discord webhook に逃がす（フォールバック）

---

## 🟡 改善推奨

### Y1: spec.md のバージョン表記が古い

依頼は v0.7 だったけど、ファイル中の `Version: 0.6` のまま。決定事項セクション13 が追記されてるから v0.7 にバンプすべき。

### Y2: REVISE → REVIEW で全レビューがクリアされる

watchdog.py の `clear_reviews()`:
```python
def clear_reviews(batch: list, key: str, revised_key: str):
    for issue in batch:
        issue[key] = {}
```

P0を出したレビュアーだけでなく、APPROVEやP1だったレビュアーのレビューも消える。全員やり直しになる。仕様判断だから設計者（Asuka）に確認だけど、P0出した Issue のレビューだけリセットする方が効率的では？全リセットだとレビュアーの工数が倍になる。

### Y3: triage で Issue 1つずつしか追加できない

`plan-done` と `commit` は `nargs="+"` で複数指定可なのに、`triage` は1つずつ。バッチに3-5件積むときに3-5回コマンド叩くのは面倒。

```python
p.add_argument("--issue", type=int, nargs="+", required=True)
```

`--title` は複数に対応しにくいから、`--title` なしで GitLab API から取得するか、後で更新するコマンドを追加する方が自然。

### Y4: `"DESIGN_REVISE" in state` の文字列マッチが危うい

devbar.py `cmd_revise()`:
```python
if "DESIGN_REVISE" in state:
```

現状の状態名なら問題ないけど、`state == "DESIGN_REVISE"` の方が安全。watchdog.py にも `"DESIGN" in state` パターンが多用されてるが、こっちは DESIGN_PLAN / DESIGN_REVIEW / DESIGN_REVISE / DESIGN_APPROVED 全部を拾う意図だから妥当。`cmd_revise` の方だけ `==` にすべき。

### Y5: レビュアーから devbar.py へのアクセスパス

レビュアー（Pascal, Leibniz等）は各自のワークスペースで動いている。`python3 /mnt/s/wsl/work/project/devbar/devbar.py review ...` をレビュアーが exec で叩く想定だけど:
- レビュアーのエージェントが `/mnt/s/wsl/work/project/devbar/` にアクセス可能か？
- config.py の `sys.path.insert` でインポートしてるから、devbar.py のディレクトリからの相対パスが通る必要がある

`/home/ataka/.openclaw/shared/` 以下に symlink を置くか、devbar を PATH の通った場所にインストールする方がいいかもしれない。

### Y6: load() の flock タイミング

save の P0 ほど深刻じゃないけど、load() も `open()` → `flock(LOCK_SH)` の間に書き込みが入る余地がある。atomic write（P0-1の rename 方式）を採用すれば、read 側は flock 不要になるので一石二鳥。

### Y7: watchdog の DONE 処理が transition() を迂回

`process()` の最後:
```python
elif state == "DONE":
    add_history(data, "DONE", "IDLE")
    data["state"] = "IDLE"
    ...
```

他の遷移は `transition()` ヘルパー経由なのにここだけ直接書いてる。通知フォーマットの一貫性が崩れるし、将来 `transition()` にフック追加したときに漏れる。

---

## 🟢 提案

### G1: `--project` の共通化

全サブコマンドで `--project` が必須。argparse の parent parser で共通化するとコード量が減る:
```python
parent = argparse.ArgumentParser(add_help=False)
parent.add_argument("--project", required=True)
p = sub.add_parser("triage", parents=[parent])
```

### G2: pipeline JSON のバリデーション

`load()` 時に最低限の schema チェック（`"state"` キーの存在、`"batch"` がリスト等）があると、手で JSON を壊したときの診断が楽。今は KeyError で落ちるだけ。

### G3: BLOCKED の活用

`BLOCKED → IDLE` の遷移はあるけど、`任意の状態 → BLOCKED` の遷移ルールがない。VALID_TRANSITIONS に各状態から BLOCKED への遷移を追加するか、`--force` フラグで遷移バリデーションをスキップできるようにするか。

### G4: history の肥大化

history は append only で上限がない。長期間運用すると JSON が膨らむ。最新 N 件だけ保持するか、別ファイルに分離する仕組みがあると良い。

---

## ワークフロー観点（実装リードとして）

全体の流れは自然で、私（金子）が使うイメージで回せる。特に:

- watchdog が通知を push してくれるのが良い。能動的ポーリング不要
- `devbar status` で全PJ一覧が見えるのはデバッグに便利
- `plan-done` / `commit` の複数Issue指定は実運用で助かる

懸念:
- **P0-2 が最大のリスク**。通知が動かないと watchdog の存在意義が消える。ここは実装前に確認必須
- レビュアーが `devbar review` を叩くワークフロー、レビュアーは AI エージェントだから exec でコマンド実行する形になるが、そのためのスキル/手順書がまだない。レビュアー側の AGENTS.md や手順書整備が Phase 1 に含まれるべき

---

## まとめ

| 重要度 | 件数 | 内容 |
|--------|------|------|
| 🔴 P0 | 2 | flock競合、通知コマンド未検証 |
| 🟡 改善 | 7 | バージョン表記、レビュークリア粒度、triage複数指定、文字列マッチ、パスアクセス、load flock、DONE迂回 |
| 🟢 提案 | 4 | argparse共通化、JSONバリデーション、BLOCKED遷移、history肥大化 |

P0-1（flock）は確実にバグ。P0-2（通知コマンド）は検証次第で致命的。この2件を潰してから再レビューしたい。
