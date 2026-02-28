# DevBar プロジェクトレビュー

**レビュアー:** 韓非（hanfei）  
**日時:** 2026-02-20  
**対象:** spec.md v0.6, config.py, devbar.py, watchdog.py, notify.py  
**観点:** 堅牢性・防御的プログラミング・性悪説

---

## 総評

**verdict: P0（必須修正あり）**

設計思想は賞賛に値する。状態を外部ファイルで永続化し、セッション障害に耐えるアーキテクチャは「仕組みで防ぐ」法家思想に合致する。

しかし、**実装には致命的な欠陥がある**。特にファイルロックの扱いが甘すぎる。例外発生時にロックが解放されないコードは、デッドロックという形でシステムを停止させる。これは「法（ルール）が明文化されていない」状態に他ならない。

---

## P0: 必須修正

### 1. ファイルロックの例外安全性（devbar.py, watchdog.py）

**問題:** `fcntl.flock()` の解放が `try/finally` で保護されていない。例外発生時にロックが永続的に保持され、デッドロックを引き起こす。

```python
# ❌ 現在のコード（devbar.py load 関数）
def load(path: Path) -> dict:
    with open(path) as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        data = json.load(f)
        fcntl.flock(f, fcntl.LOCK_UN)  # ← json.load で例外が発生すると実行されない
    return data
```

**修正:**
```python
# ✅ 修正後
def load(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return json.load(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
```

**影響範囲:**
- `devbar.py`: `load()`, `save()` 関数
- `watchdog.py`: `load_pipeline()`, `save_pipeline()` 関数

**根拠:** 外部入力を信用するな。`json.load()` は不正な JSON で例外を発生させる。ネットワークファイルシステム（WSL2 越しの `/mnt/s/`）では I/O エラーも発生しうる。例外経路こそが、ロック解放の漏れを生む。

---

### 2. アトミック書き込みの欠如（devbar.py, watchdog.py）

**問題:** `save()` 関数が直接ファイルに書き込んでいる。書き込み中の停止でファイルが破損する。

```python
# ❌ 現在のコード
def save(path: Path, data: dict):
    data["updated_at"] = now_iso()
    with open(path, "w") as f:  # ← ここで既存内容が消える
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)
```

**修正:**
```python
# ✅ 修正後
import tempfile
import os

def save(path: Path, data: dict):
    data["updated_at"] = now_iso()
    path = path.resolve()
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp_path, path)  # アトミックな置き換え
    except:
        os.unlink(tmp_path)  # 失敗時にテンポラリを削除
        raise
```

**根拠:** 「矛盾」の故事を引くまでもなく、書き込み中の停止は必ず発生しうる。電源断、kill -9、WSL2 のシャットダウン——これらを防ぐのは「気をつける」ではなく、アトミック操作という仕組みである。

---

### 3. パストラバーサルの脆弱性（devbar.py）

**問題:** `--project` 引数にパストラバーサル文字列が含まれる可能性がある。

```python
def get_path(project: str) -> Path:
    return PIPELINES_DIR / f"{project}.json"
```

**攻撃例:**
```bash
devbar triage --project ../../etc/passwd --issue 1
```

**修正:**
```python
def get_path(project: str) -> Path:
    # プロジェクト名は英数字とハイフンのみに制限
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', project):
        raise ValueError(f"Invalid project name: {project}")
    path = PIPELINES_DIR / f"{project}.json"
    # PIPELINES_DIR からはみ出していないことを確認
    path = path.resolve()
    if not str(path).startswith(str(PIPELINES_DIR.resolve())):
        raise ValueError(f"Invalid project path: {path}")
    return path
```

**根拠:** 入力を信用するな。CLI 引数は外部入力に他ならない。

---

### 4. Discord トークンの取り扱い（notify.py）

**問題:** `openclaw.json` から直接トークンを読み込み、例外を握りつぶしている。

```python
def get_bot_token() -> str | None:
    import re
    try:
        text = GATEWAY_TOKEN_PATH.read_text()
        text = re.sub(r',\s*([}\]])', r'\1', text)
        data = json.loads(text)
        return data["channels"]["discord"]["accounts"][DISCORD_BOT_ACCOUNT]["token"]
    except Exception:  # ← 全ての例外を黙殺
        return None
```

**修正:**
```python
def get_bot_token() -> str | None:
    import logging
    try:
        data = json.loads(GATEWAY_TOKEN_PATH.read_text(encoding="utf-8"))
        token = data["channels"]["discord"]["accounts"][DISCORD_BOT_ACCOUNT]["token"]
        if not isinstance(token, str) or not token:
            logging.error("Discord token is missing or invalid")
            return None
        return token
    except (KeyError, json.JSONDecodeError, FileNotFoundError) as e:
        logging.error(f"Failed to load Discord token: {type(e).__name__}: {e}")
        return None
```

**根拠:** 認証情報は最も保護すべきデータである。例外の種類を区別せず握りつぶすのは、問題の発見を遅らせるだけだ。

---

## P1: 強く推奨

### 5. 型ヒントの欠如（全ファイル）

**問題:** PEP 585/604 に準拠した型ヒントがない。

**修正例:**
```python
from pathlib import Path
from typing import Any

def load(path: Path) -> dict[str, Any]:
    ...

def find_issue(batch: list[dict[str, Any]], issue_num: int) -> dict[str, Any] | None:
    ...
```

**根拠:** 型は「法」の一種である。型システムが防ぐのは、単なるタイプミスではない。暗黙の前提の侵害だ。

---

### 6. エンコーディング指定の欠如（全ファイル）

**問題:** `open()` でエンコーディング指定がない。

```python
# ❌ 現在のコード
with open(path) as f:

# ✅ 修正後
with open(path, "r", encoding="utf-8") as f:
```

**根拠:** Windows（WSL2 越しの `/mnt/s/`）ではデフォルトエンコーディングが CP932 の可能性がある。UTF-8 を明示せよ。

---

### 7. GLAB_BIN の存在チェック（devbar.py）

**問題:** `GLAB_BIN` がハードコードされているが、存在しない場合の処理がない。

```python
# config.py
GLAB_BIN = "/home/ataka/bin/glab"
```

**修正:**
```python
# devbar.py cmd_review() 内
import shutil
glab = shutil.which("glab") or GLAB_BIN
if not Path(glab).exists():
    print(f"⚠ glab not found at {glab}", file=sys.stderr)
    # GitLab note はスキップするが、pipeline JSON への記録は継続
```

---

### 8. ログファイルのローテーション（watchdog.py）

**問題:** `/tmp/devbar-watchdog.log` が無限に大きくなる。

**修正:**
```python
import logging
from logging.handlers import RotatingFileHandler

def setup_logging():
    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )
```

---

### 9. spec.md のロック仕様の詳細化

**問題:** 仕様では「flock 必須」とあるが、例外安全性についての言及がない。

**修正:** spec.md の「11. 制約と前提」に以下を追加：
```markdown
- **flock の例外安全性**: ロック解放は `try/finally` ブロック内で行うこと。例外発生時もロックが解放されることを保証する。
- **アトミック書き込み**: pipeline JSON の書き込みは、テンポラリファイル→`os.replace()` のアトミック操作で行うこと。
```

---

## P2: 改善の余地

### 10. subprocess の timeout 値の定数化（notify.py）

**問題:** `timeout=10` などがハードコードされている。

```python
# config.py に追加
SUBPROCESS_TIMEOUT = 30
DISCORD_TIMEOUT = 10
GLAB_TIMEOUT = 15
```

---

### 11. エラーメッセージの改善（devbar.py）

**問題:** エラーメッセージが技術的すぎて、エンドユーザー（金子）に不親切。

```python
# ❌ 現在のコード
print(f"Invalid transition: {current} → {target} (allowed: {allowed})", file=sys.stderr)

# ✅ 改善案
print(f"エラー: 状態遷移 '{current} → {target}' は許可されていません。", file=sys.stderr)
print(f"  可能な遷移: {' → '.join(allowed)}", file=sys.stderr)
```

---

## 決定事項のレビュー

spec.md「13. 決定・解決済み」の項目について：

- [x] **pipeline JSON: shared/直置き** — 妥当。ただしバックアップ戦略の検討を推奨
- [x] **BLOCKED → 復帰** — 実装済み、問題なし
- [x] **devbar review 一本化** — 良い設計。ただし GitLab note 失敗時の挙動を明文化すべき
- [x] **設計完了検知** — 実装済み、問題なし
- [x] **定数管理** — config.py への一元化、問題なし
- [x] **Discord 投稿** — 実装済み。ただしトークン取り扱いに P0 問題あり（上記参照）
- [x] **レビュー依頼フォーマット** — 実装済み、問題なし
- [x] **複数 Issue 一括操作** — 実装済み、問題なし

---

## 結論

**APPROVE の条件:**

1. P0 問題（ファイルロックの例外安全性、アトミック書き込み、パストラバーサル、トークン取り扱い）を修正
2. P1 問題（型ヒント、エンコーディング指定）を少なくとも一部修正
3. spec.md にロックの例外安全性とアトミック書き込みの仕様を追記

**韓非の所見:**

> 「法に照らして誤りなきか」

現在のコードは、例外経路という「法の死角」を放置している。これは怠慢ではない。人間は例外経路を忘れがちである。だからこそ、`try/finally` という仕組みで強制せねばならない。

修正後は再レビューを要する。特にファイルロックとアトミック書き込みは、システムの根幹をなす。ここを誤れば、パイプライン全体がデッドロックで停止する。

---

**次のアクション:**

1. P0 問題の修正を優先
2. 修正後、watchdog の動作テスト（並列実行、例外注入）
3. 再レビュー依頼
