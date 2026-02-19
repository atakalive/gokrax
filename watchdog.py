#!/usr/bin/env python3
"""devbar-watchdog.py — LLM不要のパイプラインオーケストレーター

cronで1分間隔実行。pipeline JSONを読んで条件満たしてたら状態遷移+アクター通知。
冪等。何回実行しても同じ結果。

Double-Checked Locking パターン:
  1. ロックなしで事前チェック（不要なら早期リターン）
  2. update_pipeline のロック内で再チェック + 遷移
  3. ロック外で通知
"""

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PIPELINES_DIR, JST, LOG_FILE, MIN_REVIEWS
from pipeline_io import (
    load_pipeline, update_pipeline,
    add_history, now_iso, find_issue,
)
from notify import notify_implementer, notify_reviewers, notify_discord


def log(msg: str):
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def count_reviews(batch: list, key: str) -> tuple:
    """(最小レビュー数, P0有無)"""
    min_n = min((len(i.get(key, {})) for i in batch), default=0)
    has_p0 = any(
        r.get("verdict", "").upper() in ("REJECT", "P0")
        for i in batch for r in i.get(key, {}).values()
    )
    return min_n, has_p0


def clear_reviews(batch: list, key: str, revised_key: str):
    """P0/REJECTが付いたIssueのレビューのみクリアする。
    APPROVE/P1のIssueはレビュー結果を保持。
    revised_key は全Issueから削除（次のREVISEサイクル用）。
    """
    for issue in batch:
        reviews = issue.get(key, {})
        has_p0 = any(
            r.get("verdict", "").upper() in ("REJECT", "P0")
            for r in reviews.values()
        )
        if has_p0:
            issue[key] = {}
        issue.pop(revised_key, None)


@dataclass
class TransitionAction:
    """check_transition() の返り値。new_state が None なら遷移不要。"""
    new_state: str | None = None
    impl_msg: str | None = None
    send_review: bool = False


def check_transition(state: str, batch: list) -> TransitionAction:
    """現在の状態とバッチから次の遷移アクションを決定する純粋関数。副作用なし。"""
    if state in ("IDLE", "TRIAGE", "MERGE_SUMMARY_SENT", "DESIGN_APPROVED", "BLOCKED"):
        return TransitionAction()

    if state == "DONE":
        return TransitionAction(
            new_state="IDLE",
            impl_msg="バッチ完了。watchdog無効化しました。",
        )

    if not batch:
        return TransitionAction()

    if state == "DESIGN_PLAN":
        if all(i.get("design_ready") for i in batch):
            return TransitionAction(new_state="DESIGN_REVIEW", send_review=True)
        return TransitionAction()

    if state in ("DESIGN_REVIEW", "CODE_REVIEW"):
        key = "design_reviews" if "DESIGN" in state else "code_reviews"
        count, has_p0 = count_reviews(batch, key)
        if count >= MIN_REVIEWS:
            if has_p0:
                rev = "DESIGN_REVISE" if "DESIGN" in state else "CODE_REVISE"
                return TransitionAction(
                    new_state=rev,
                    impl_msg="レビューにP0あり。修正してください。",
                )
            else:
                appr = "DESIGN_APPROVED" if "DESIGN" in state else "CODE_APPROVED"
                msg = (
                    "設計レビュー通過。実装に進んでください。"
                    if "DESIGN" in state
                    else "コードレビュー通過。Mにサマリーを送ってください。"
                )
                return TransitionAction(new_state=appr, impl_msg=msg)
        return TransitionAction()

    if state in ("DESIGN_REVISE", "CODE_REVISE"):
        revised_key = "design_revised" if "DESIGN" in state else "code_revised"
        if all(i.get(revised_key) for i in batch):
            review_state = "DESIGN_REVIEW" if "DESIGN" in state else "CODE_REVIEW"
            return TransitionAction(new_state=review_state, send_review=True)
        return TransitionAction()

    if state == "IMPLEMENTATION":
        if all(i.get("commit") for i in batch):
            return TransitionAction(new_state="CODE_REVIEW", send_review=True)
        return TransitionAction()

    return TransitionAction()


def process(path: Path):
    # === 第1チェック (ロックなし) ===
    data = load_pipeline(path)
    if not data.get("enabled", False):
        return

    state = data.get("state", "IDLE")
    batch = data.get("batch", [])
    pj = data.get("project", path.stem)

    if state != "DONE" and not batch:
        log(f"[{pj}] WARNING: state={state} but batch is empty")
        return

    if check_transition(state, batch).new_state is None:
        return

    # === ロック内で第2チェック + 遷移 (Double-Checked Locking) ===
    notification: dict = {}

    def do_transition(data):
        state = data.get("state", "IDLE")
        batch = data.get("batch", [])
        action = check_transition(state, batch)
        if action.new_state is None:
            # ロック待ち中に他プロセスが状態を変えた → スキップ
            return

        pj = data.get("project", path.stem)

        # DONE状態: バッチクリア + watchdog無効化
        if state == "DONE":
            data["batch"] = []
            data["enabled"] = False

        # REVISE → REVIEW: ロック内でレビュークリア
        if state in ("DESIGN_REVISE", "CODE_REVISE"):
            revised_key = "design_revised" if "DESIGN" in state else "code_revised"
            key = "design_reviews" if "DESIGN" in state else "code_reviews"
            clear_reviews(batch, key, revised_key)

        log(f"[{pj}] {state} → {action.new_state}")
        add_history(data, state, action.new_state, actor="watchdog")
        data["state"] = action.new_state

        # ロック外通知用に情報を保存
        notification.update({
            "pj": pj,
            "old_state": state,
            "action": action,
            "gitlab": data.get("gitlab", f"atakalive/{pj}"),
            "implementer": data.get("implementer", "kaneko"),
            "batch": list(data.get("batch", [])),
        })

    update_pipeline(path, do_transition)

    # === ロック外で通知 ===
    if notification:
        action = notification["action"]
        pj = notification["pj"]
        notify_discord(f"[{pj}] {notification['old_state']} → {action.new_state}")
        if action.impl_msg:
            notify_implementer(
                notification["implementer"],
                f"[devbar] {pj}: {action.impl_msg}",
            )
        if action.send_review:
            notify_reviewers(
                pj, action.new_state, notification["batch"], notification["gitlab"]
            )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
    )
    if not PIPELINES_DIR.exists():
        return
    for path in sorted(PIPELINES_DIR.glob("*.json")):
        try:
            process(path)
        except Exception as e:
            log(f"[{path.stem}] ERROR: {e}")


if __name__ == "__main__":
    main()
