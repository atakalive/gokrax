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
from config import PIPELINES_DIR, JST, LOG_FILE, MIN_REVIEWS, DESIGN_MIN_REVIEWS, CODE_MIN_REVIEWS, CC_MODEL_PLAN, CC_MODEL_IMPL, DEVBAR_CLI, INACTIVE_THRESHOLD_SEC, SESSIONS_BASE, DESIGN_REVIEWERS, CODE_REVIEWERS
from datetime import datetime as _datetime
import json as _json
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


# BLOCKEDまでの時間 (秒)
BLOCK_TIMERS = {
    "DESIGN_PLAN":    360,   # 6分
    "DESIGN_REVISE":  600,   # 10分
    "CODE_REVISE":    600,   # 10分
    "IMPLEMENTATION": 1200,  # 20分
}

# 状態遷移直後の催促猶予期間（秒）。遷移からこの時間が経つまで催促しない
NUDGE_GRACE_SEC = 180  # 3分


@dataclass
class TransitionAction:
    """check_transition() の返り値。new_state が None なら遷移不要。"""
    new_state: str | None = None
    impl_msg: str | None = None
    send_review: bool = False
    nudge: str | None = None   # 催促通知が必要な状態名
    nudge_reviewers: list | None = None  # 催促が必要なレビュアーのリスト


def _get_state_entered_at(data: dict, state: str) -> _datetime | None:
    """historyから指定stateに遷移した最新時刻を取得。"""
    for entry in reversed(data.get("history", [])):
        if entry.get("to") == state:
            try:
                return _datetime.fromisoformat(entry["at"])
            except (KeyError, ValueError):
                return None
    return None


def _nudge_key(state: str) -> str:
    """状態ごとの催促カウンタキー名。"""
    return f"{state.lower()}_notify_count"


def _check_nudge(state: str, data: dict) -> TransitionAction | None:
    """催促/BLOCKED判定。該当しなければNone。"""
    block_sec = BLOCK_TIMERS.get(state)
    if block_sec is None or data is None:
        return None
    entered_at = _get_state_entered_at(data, state)
    elapsed = 0.0
    if entered_at is not None:
        elapsed = (_datetime.now(JST) - entered_at).total_seconds()
    # BLOCKED判定（時間超過）
    if elapsed >= block_sec:
        return TransitionAction(
            new_state="BLOCKED",
            impl_msg=f"{state} タイムアウト。応答がありませんでした。",
        )
    # 猶予期間内は催促しない
    if elapsed < NUDGE_GRACE_SEC:
        return None
    # 催促（非アクティブチェックはロック内で行う）
    return TransitionAction(nudge=state)


def get_notification_for_state(
    state: str,
    project: str = "",
    batch: list | None = None,
    gitlab: str = "",
    implementer: str = "",
) -> TransitionAction:
    """全状態の通知メッセージを一元管理。

    遷移通知（初回）にも催促（nudge）にも使う。
    check_transition(), cmd_transition(), 催促処理 から呼ばれる共通ロジック。
    通知不要な状態は TransitionAction() を返す。
    """
    batch = batch or []

    if state in ("DESIGN_REVIEW", "CODE_REVIEW"):
        return TransitionAction(send_review=True)

    if state == "DESIGN_PLAN":
        issues_str = ", ".join(
            f"#{i['issue']}" for i in batch if not i.get("design_ready")
        ) or "（全Issue）"
        msg = (
            f"[devbar] {project}: 設計確認フェーズ\n"
            f"対象Issue: {issues_str}\n"
            f"対象Issueを確認して、Claude Codeが楽に実装できるように内容確認/修正してください。"
            f"その後、問題がなければ plan-done してください。\n"
            f"python3 {DEVBAR_CLI} plan-done --project {project} --issue N"
        )
        return TransitionAction(impl_msg=msg)

    if state in ("DESIGN_REVISE", "CODE_REVISE"):
        phase = "設計" if "DESIGN" in state else "コード"
        revised_key = "design_revised" if "DESIGN" in state else "code_revised"
        issues_str = ", ".join(
            f"#{i['issue']}" for i in batch if not i.get(revised_key)
        ) or "（全Issue）"
        msg = (
            f"[devbar] {project}: {phase}修正フェーズ\n"
            f"対象Issue: {issues_str}\n"
            f"P0の指摘を修正して、revise コマンドで完了報告してください。\n"
            f"python3 {DEVBAR_CLI} revise --project {project} --issue N"
        )
        return TransitionAction(impl_msg=msg)

    if state == "DESIGN_APPROVED":
        msg = (
            f"設計レビュー通過。あなた（実装担当）がClaude Codeを使用して、バッチ単位で Plan => Impl してください。\n"
            f"CC Plan: `claude --model {CC_MODEL_PLAN}` でまとめて設計確認\n"
            f"CC Impl: `claude --model {CC_MODEL_IMPL}` でまとめて実装\n"
            f"実装完了後: `python3 {DEVBAR_CLI} commit --project {project} --issue N [N...] --hash <commit>`"
        )
        return TransitionAction(impl_msg=msg)

    if state == "IMPLEMENTATION":
        issues_str = ", ".join(
            f"#{i['issue']}" for i in batch if not i.get("commit")
        ) or "（全Issue）"
        msg = (
            f"[devbar] {project}: 実装フェーズ — あなた（実装担当）がClaude Codeで実装してください\n"
            f"対象Issue: {issues_str}\n"
            f"手順:\n"
            f"1. `claude --model {CC_MODEL_PLAN}` でIssueの設計を確認（Plan）\n"
            f"2. `claude --model {CC_MODEL_IMPL}` で実装（Impl）\n"
            f"3. 完了後: `python3 {DEVBAR_CLI} commit --project {project} --issue N [N...] --hash <commit>`"
        )
        return TransitionAction(impl_msg=msg)

    if state == "CODE_APPROVED":
        return TransitionAction(impl_msg="コードレビュー通過。Mにサマリーを送ってください。")

    if state == "IDLE":
        return TransitionAction(impl_msg="バッチ完了。watchdog無効化しました。")

    return TransitionAction()


def check_transition(state: str, batch: list, data: dict | None = None) -> TransitionAction:
    """現在の状態とバッチから次の遷移アクションを決定する純粋関数。副作用なし。"""
    if state in ("IDLE", "TRIAGE", "BLOCKED"):
        return TransitionAction()

    if state == "MERGE_SUMMARY_SENT":
        if data is None:
            return TransitionAction()
        from notify import fetch_discord_replies
        from config import M_DISCORD_USER_ID, DISCORD_CHANNEL
        summary_id = data.get("summary_message_id")
        if not summary_id:
            return TransitionAction()
        messages = fetch_discord_replies(DISCORD_CHANNEL, summary_id)
        for msg in messages:
            ref = msg.get("message_reference", {})
            if (ref.get("message_id") == summary_id
                    and msg.get("author", {}).get("id") == M_DISCORD_USER_ID
                    and msg.get("content", "").strip().lower().startswith("ok")):
                return TransitionAction(
                    new_state="DONE",
                    impl_msg="Mが承認しました。push + issue close してください。",
                )
        return TransitionAction()

    if state == "DONE":
        return TransitionAction(
            new_state="IDLE",
            impl_msg=get_notification_for_state("IDLE").impl_msg,
        )

    if not batch:
        return TransitionAction()

    if state == "DESIGN_PLAN":
        if all(i.get("design_ready") for i in batch):
            return TransitionAction(new_state="DESIGN_REVIEW", send_review=True)
        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    if state in ("DESIGN_REVIEW", "CODE_REVIEW"):
        key = "design_reviews" if "DESIGN" in state else "code_reviews"
        count, has_p0 = count_reviews(batch, key)
        min_rev = DESIGN_MIN_REVIEWS if "DESIGN" in state else CODE_MIN_REVIEWS
        if count >= min_rev:
            if has_p0:
                rev = "DESIGN_REVISE" if "DESIGN" in state else "CODE_REVISE"
                return TransitionAction(
                    new_state=rev,
                    impl_msg=get_notification_for_state(rev).impl_msg,
                )
            else:
                appr = "DESIGN_APPROVED" if "DESIGN" in state else "CODE_APPROVED"
                return TransitionAction(
                    new_state=appr,
                    impl_msg=get_notification_for_state(appr).impl_msg,
                )
        # 未完了レビュアーの催促（猶予期間内はスキップ）
        entered_at = _get_state_entered_at(data, state) if data is not None else None
        if entered_at is not None:
            elapsed = (_datetime.now(JST) - entered_at).total_seconds()
            if elapsed < NUDGE_GRACE_SEC:
                return TransitionAction()
        pending = _get_pending_reviewers(batch, key)
        if pending:
            return TransitionAction(nudge_reviewers=pending)
        return TransitionAction()

    if state == "DESIGN_APPROVED":
        return TransitionAction(
            new_state="IMPLEMENTATION",
            impl_msg=get_notification_for_state("DESIGN_APPROVED").impl_msg,
        )

    if state == "CODE_APPROVED":
        return TransitionAction(
            new_state="MERGE_SUMMARY_SENT",
            impl_msg=get_notification_for_state("CODE_APPROVED").impl_msg,
        )

    if state in ("DESIGN_REVISE", "CODE_REVISE"):
        revised_key = "design_revised" if "DESIGN" in state else "code_revised"
        if all(i.get(revised_key) for i in batch):
            review_state = "DESIGN_REVIEW" if "DESIGN" in state else "CODE_REVIEW"
            return TransitionAction(new_state=review_state, send_review=True)
        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    if state == "IMPLEMENTATION":
        if all(i.get("commit") for i in batch):
            return TransitionAction(new_state="CODE_REVIEW", send_review=True)
        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    return TransitionAction()


def _is_agent_inactive(agent_id: str) -> bool:
    """エージェントが非アクティブ(81秒以上更新なし)かどうか判定。"""
    try:
        path = SESSIONS_BASE / agent_id / "sessions" / "sessions.json"
        data = _json.loads(path.read_text())
        session = data.get(f"agent:{agent_id}:main")
        if not session or "updatedAt" not in session:
            return True  # 情報なし = 非アクティブ扱い
        last_active = _datetime.fromtimestamp(session["updatedAt"] / 1000, JST)
        elapsed = (_datetime.now(JST) - last_active).total_seconds()
        return elapsed >= INACTIVE_THRESHOLD_SEC
    except (FileNotFoundError, _json.JSONDecodeError, KeyError):
        return True  # 読めない = 非アクティブ扱い


def _get_pending_reviewers(batch: list, review_key: str) -> list:
    """全Issueのレビューを完了していないレビュアーのリストを返す。"""
    reviewers = DESIGN_REVIEWERS if review_key == "design_reviews" else CODE_REVIEWERS
    pending = []
    for reviewer in reviewers:
        # 全Issueにこのレビュアーのレビューがあるか
        if not all(reviewer in i.get(review_key, {}) for i in batch):
            pending.append(reviewer)
    return pending


def _format_nudge_message(state: str, project: str, batch: list) -> str:
    """催促メッセージ生成。get_notification_for_state() に委譲。"""
    notif = get_notification_for_state(state, project=project, batch=batch)
    return notif.impl_msg or f"[devbar] {project}: {state} — 対応してください。"


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

    pre_action = check_transition(state, batch, data)
    if pre_action.new_state is None and not pre_action.nudge and not pre_action.nudge_reviewers:
        return

    # === ロック内で第2チェック + 遷移 (Double-Checked Locking) ===
    notification: dict = {}

    def do_transition(data):
        state = data.get("state", "IDLE")
        batch = data.get("batch", [])
        action = check_transition(state, batch, data)

        # レビュアー催促（書き込み不要、情報保存のみ）
        if action.nudge_reviewers:
            pj = data.get("project", path.stem)
            notification.update({
                "pj": pj,
                "action": action,
                "nudge_reviewers": list(action.nudge_reviewers),
            })
            return

        # 実装担当催促（遷移なし、カウンタ書き込みのみ）
        if action.nudge:
            implementer = data.get("implementer", "kaneko")
            if not _is_agent_inactive(implementer):
                # アクティブなら催促しない（カウンタも上げない）
                return
            key = _nudge_key(action.nudge)
            data[key] = data.get(key, 0) + 1
            pj = data.get("project", path.stem)
            log(f"[{pj}] {action.nudge}: 催促通知送信 (count={data[key]})")
            notification.update({
                "pj": pj,
                "action": action,
                "implementer": data.get("implementer", "kaneko"),
                "batch": list(batch),
                "gitlab": data.get("gitlab", f"atakalive/{pj}"),
                "nudge_count": data[key],
            })
            return

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

        # 催促カウンタリセット（状態から出るとき）
        if state in BLOCK_TIMERS:
            data.pop(_nudge_key(state), None)

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
            "repo_path": data.get("repo_path", ""),
        })

    update_pipeline(path, do_transition)

    # === ロック外で通知 ===
    if notification:
        action = notification["action"]
        pj = notification["pj"]

        if action.nudge_reviewers:
            # 非アクティブなレビュアーにのみ「continue」送信
            woken = []
            for reviewer in notification["nudge_reviewers"]:
                if _is_agent_inactive(reviewer):
                    notify_implementer(reviewer, "continue")
                    woken.append(reviewer)
            if woken:
                ts = _datetime.now(JST).strftime("%m/%d %H:%M")
                log(f"[{pj}] レビュアー催促: {', '.join(woken)} ({ts})")
            return

        if action.nudge:
            # 初回(count=1)は詳細メッセージ、2回目以降は起床用"continue"
            nudge_count = notification.get("nudge_count", 1)
            if nudge_count <= 1:
                nudge_msg = _format_nudge_message(action.nudge, pj, notification["batch"])
            else:
                nudge_msg = "continue"
            notify_implementer(notification["implementer"], nudge_msg)
            ts = _datetime.now(JST).strftime("%m/%d %H:%M")
            notify_discord(f"[{pj}] {action.nudge}: 実装担当に通知送信 ({ts})")
            return

        ts = _datetime.now(JST).strftime("%m/%d %H:%M")
        notify_discord(f"[{pj}] {notification['old_state']} → {action.new_state} ({ts})")
        if action.impl_msg:
            notify_implementer(
                notification["implementer"],
                f"[devbar] {pj}: {action.impl_msg}",
            )
        if action.send_review:
            notify_reviewers(
                pj, action.new_state, notification["batch"], notification["gitlab"],
                repo_path=notification.get("repo_path", ""),
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
