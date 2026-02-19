#!/usr/bin/env python3
"""devbar-watchdog.py — LLM不要のパイプラインオーケストレーター

cronで1分間隔実行。pipeline JSONを読んで条件満たしてたら状態遷移+アクター通知。
冪等。何回実行しても同じ結果。
"""

import json
import fcntl
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
PIPELINES_DIR = Path.home() / ".openclaw/shared/pipelines"
LOG_FILE = Path("/tmp/devbar-watchdog.log")

IMPLEMENTER = "agent:reviewer00:main"
MIN_REVIEWS = 3


def log(msg: str):
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_pipeline(path: Path) -> dict:
    with open(path) as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        data = json.load(f)
        fcntl.flock(f, fcntl.LOCK_UN)
    return data


def save_pipeline(path: Path, data: dict):
    data["updated_at"] = datetime.now(JST).isoformat()
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)


def add_history(data: dict, from_state: str, to_state: str, actor: str = "watchdog"):
    data.setdefault("history", []).append({
        "from": from_state, "to": to_state,
        "at": datetime.now(JST).isoformat(), "actor": actor,
    })


def notify(session_key: str, message: str):
    """openclaw CLIでsessions_send。"""
    try:
        subprocess.run(
            ["openclaw", "session", "send", "--to", session_key, "--message", message],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        log(f"  notify failed: {e}")


def transition(path: Path, data: dict, old: str, new: str, msg: str, notify_to: str = None):
    """状態遷移の共通処理。"""
    log(f"[{data.get('project', path.stem)}] {old} → {new}")
    add_history(data, old, new)
    data["state"] = new
    save_pipeline(path, data)
    if notify_to and msg:
        notify(notify_to, f"[devbar] {data.get('project', path.stem)}: {msg}")


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


def process(path: Path):
    data = load_pipeline(path)
    state = data.get("state", "IDLE")
    batch = data.get("batch", [])
    pj = data.get("project", path.stem)

    if state == "IDLE" or state == "MERGE_SUMMARY_SENT":
        return

    if state in ("DESIGN_REVIEW", "CODE_REVIEW"):
        key = "design_reviews" if "DESIGN" in state else "code_reviews"
        count, has_p0 = count_reviews(batch, key)
        if count >= MIN_REVIEWS:
            if has_p0:
                rev = "DESIGN_REVISE" if "DESIGN" in state else "CODE_REVISE"
                transition(path, data, state, rev, "レビューにP0あり。修正してください。", IMPLEMENTER)
            else:
                appr = "DESIGN_APPROVED" if "DESIGN" in state else "CODE_APPROVED"
                msg = "設計レビュー通過。実装に進んでください。" if "DESIGN" in state else "コードレビュー通過。Mにサマリーを送ってください。"
                transition(path, data, state, appr, msg, IMPLEMENTER)

    elif state in ("DESIGN_REVISE", "CODE_REVISE"):
        revised_key = "design_revised" if "DESIGN" in state else "code_revised"
        if all(i.get(revised_key) for i in batch):
            review_state = "DESIGN_REVIEW" if "DESIGN" in state else "CODE_REVIEW"
            key = "design_reviews" if "DESIGN" in state else "code_reviews"
            clear_reviews(batch, key, revised_key)
            transition(path, data, state, review_state, "修正完了。再レビューに出します。", None)

    elif state == "IMPLEMENTATION":
        if all(i.get("commit") for i in batch):
            transition(path, data, state, "CODE_REVIEW", None, None)

    elif state == "DONE":
        add_history(data, "DONE", "IDLE")
        data["state"] = "IDLE"
        data["batch"] = []
        save_pipeline(path, data)
        log(f"[{pj}] DONE → IDLE")


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
