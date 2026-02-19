#!/usr/bin/env python3
"""devbar-watchdog.py — LLM不要のパイプラインオーケストレーター

cronで1分間隔実行。pipeline JSONを読んで条件満たしてたら状態遷移+アクター通知。
冪等。何回実行しても同じ結果。
"""

import json
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from config import PIPELINES_DIR, JST, LOG_FILE, MIN_REVIEWS
from notify import notify_implementer, notify_reviewers, notify_discord


def log(msg: str):
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_pipeline(path: Path) -> dict:
    with open(path) as f:
        data = json.load(f)
    return data


def save_pipeline(path: Path, data: dict):
    """atomic write: tmpfile + rename で競合を回避。"""
    import tempfile, os
    data["updated_at"] = datetime.now(JST).isoformat()
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, str(path))
    except BaseException:
        os.unlink(tmp)
        raise


def add_history(data: dict, from_state: str, to_state: str, actor: str = "watchdog"):
    data.setdefault("history", []).append({
        "from": from_state, "to": to_state,
        "at": datetime.now(JST).isoformat(), "actor": actor,
    })


def count_reviews(batch: list, key: str) -> tuple:
    """(最小レビュー数, P0有無)"""
    min_n = min((len(i.get(key, {})) for i in batch), default=0)
    has_p0 = any(
        r.get("verdict", "").upper() in ("REJECT", "P0")
        for i in batch for r in i.get(key, {}).values()
    )
    return min_n, has_p0


def clear_reviews(batch: list, key: str, revised_key: str):
    for issue in batch:
        issue[key] = {}
        issue.pop(revised_key, None)


def transition(path, data, old, new, impl_msg=None, send_review=False):
    """状態遷移 + 通知"""
    pj = data.get("project", path.stem)
    gitlab = data.get("gitlab", f"atakalive/{pj}")
    implementer = data.get("implementer", "kaneko")

    log(f"[{pj}] {old} → {new}")
    add_history(data, old, new)
    data["state"] = new
    save_pipeline(path, data)

    # Discord通知
    notify_discord(f"[{pj}] {old} → {new}")

    # Implementer通知
    if impl_msg:
        notify_implementer(implementer, f"[devbar] {pj}: {impl_msg}")

    # レビュアーへの依頼（レビュアーごとに個別コマンド付き）
    if send_review:
        notify_reviewers(pj, new, data.get("batch", []), gitlab)


def process(path: Path):
    data = load_pipeline(path)
    if not data.get("enabled", False):
        return

    state = data.get("state", "IDLE")
    batch = data.get("batch", [])
    pj = data.get("project", path.stem)

    if state in ("IDLE", "TRIAGE", "MERGE_SUMMARY_SENT",
                 "DESIGN_APPROVED", "BLOCKED"):
        return

    if not batch:
        log(f"[{pj}] WARNING: state={state} but batch is empty")
        return

    if state == "DESIGN_PLAN":
        if all(i.get("design_ready") for i in batch):
            transition(path, data, state, "DESIGN_REVIEW", send_review=True)
        return

    if state in ("DESIGN_REVIEW", "CODE_REVIEW"):
        key = "design_reviews" if "DESIGN" in state else "code_reviews"
        count, has_p0 = count_reviews(batch, key)
        if count >= MIN_REVIEWS:
            if has_p0:
                rev = "DESIGN_REVISE" if "DESIGN" in state else "CODE_REVISE"
                transition(path, data, state, rev,
                           impl_msg="レビューにP0あり。修正してください。")
            else:
                appr = "DESIGN_APPROVED" if "DESIGN" in state else "CODE_APPROVED"
                msg = ("設計レビュー通過。実装に進んでください。"
                       if "DESIGN" in state
                       else "コードレビュー通過。Mにサマリーを送ってください。")
                transition(path, data, state, appr, impl_msg=msg)

    elif state in ("DESIGN_REVISE", "CODE_REVISE"):
        revised_key = "design_revised" if "DESIGN" in state else "code_revised"
        if all(i.get(revised_key) for i in batch):
            review_state = "DESIGN_REVIEW" if "DESIGN" in state else "CODE_REVIEW"
            key = "design_reviews" if "DESIGN" in state else "code_reviews"
            clear_reviews(batch, key, revised_key)
            transition(path, data, state, review_state, send_review=True)

    elif state == "IMPLEMENTATION":
        if all(i.get("commit") for i in batch):
            transition(path, data, state, "CODE_REVIEW", send_review=True)

    elif state == "DONE":
        data["batch"] = []
        data["enabled"] = False
        transition(path, data, "DONE", "IDLE",
                   impl_msg="バッチ完了。watchdog無効化しました。")


def main():
    if not PIPELINES_DIR.exists():
        return
    for path in sorted(PIPELINES_DIR.glob("*.json")):
        try:
            process(path)
        except Exception as e:
            log(f"[{path.stem}] ERROR: {e}")


if __name__ == "__main__":
    main()
