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
from config import PIPELINES_DIR, JST, LOG_FILE, REVIEW_MODES, CC_MODEL_PLAN, CC_MODEL_IMPL, DEVBAR_CLI, INACTIVE_THRESHOLD_SEC, SESSIONS_BASE
from datetime import datetime as _datetime
import json as _json
from pipeline_io import (
    load_pipeline, update_pipeline, get_path,
    add_history, now_iso, find_issue,
)
from notify import notify_implementer, notify_reviewers, notify_discord


def log(msg: str):
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    # crontabの >> リダイレクトと直接書き込みの二重出力を防止
    # ファイルのみに書き込み、stdoutには出さない
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
    "DESIGN_PLAN":    600,   # 10分
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
    send_merge_summary: bool = False  # #dev-bar にマージサマリーを投稿
    run_cc: bool = False  # CC CLI を直接起動
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


_VERDICT_EMOJI = {"APPROVE": "🟢", "P0": "🔴", "P1": "🟡"}


def _format_merge_summary(project: str, batch: list) -> str:
    """#dev-bar 投稿用マージサマリーを生成する。"""
    from config import MERGE_SUMMARY_FOOTER
    lines = [f"**[{project}] マージサマリー**\n"]
    for item in batch:
        num = item["issue"]
        title = item.get("title", "")
        commit = item.get("commit", "?")
        lines.append(f"**#{num}: {title}** (`{commit}`)")
        # コードレビュー結果を表示（なければ設計レビュー）
        reviews = item.get("code_reviews") or item.get("design_reviews") or {}
        for reviewer, rev in reviews.items():
            verdict = rev.get("verdict", "?")
            emoji = _VERDICT_EMOJI.get(verdict, "⚪")
            summary = rev.get("summary", "")
            # summary の1行目だけ使う（長いレビューは切る）
            first_line = summary.split("\n")[0][:120] if summary else ""
            if first_line:
                lines.append(f"  {emoji} **{reviewer}**: {verdict} — {first_line}")
            else:
                lines.append(f"  {emoji} **{reviewer}**: {verdict}")
        lines.append("")  # 空行で区切り
    lines.append(MERGE_SUMMARY_FOOTER)
    return "\n".join(lines)


def get_notification_for_state(
    state: str,
    project: str = "",
    batch: list | None = None,
    gitlab: str = "",
    implementer: str = "",
) -> TransitionAction:
    """全状態の通知メッセージを一元管理。

    遷移通知（初回）にも催促（nudge）にも使う。ただし現状の催促は "continue" のみ。
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
            f"対象Issueを確認して、Claude Codeが楽に実装できるように内容確認/修正して、\n"
            f"glab issue update コマンドを使用して、Issue本文を更新してください。\n"
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
            f"P0の指摘を修正して、Issue本文に反映してください。\n"
            f"glab issue update コマンドを使用。\n"
            f"その後、revise コマンドで完了報告してください。\n"
            f"python3 {DEVBAR_CLI} revise --project {project} --issue N"
        )
        return TransitionAction(impl_msg=msg)

    if state == "IMPLEMENTATION":
        issues_str = ", ".join(
            f"#{i['issue']}" for i in batch if not i.get("commit")
        ) or "（全Issue）"
        msg = (
            f"[devbar] {project}: 実装フェーズ\n"
            f"— あなた（実装担当）がClaude Codeを使用して全ての対象Issueを一括で Plan => Impl して、devbarに完了報告してください。\n"
            f"対象Issue: {issues_str}\n"
            f"手順:\n"
            f"1. `claude --model {CC_MODEL_PLAN}` で、全対象Issueをまとめて設計確認（Plan）\n"
            f"2. `claude --model {CC_MODEL_IMPL}` で、全対象Issueをまとめて実装（Impl）\n"
            f"3. 完了後: `python3 {DEVBAR_CLI} commit --project {project} --issue N [N...] --hash <commit>`"
        )
        return TransitionAction(impl_msg=msg)

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
                return TransitionAction(new_state="DONE")
        return TransitionAction()

    if state == "DONE":
        # DONE→IDLEは自動遷移。通知不要（push+closeは呼び出し元で処理済み）
        return TransitionAction(new_state="IDLE")

    if not batch:
        return TransitionAction()

    if state == "DESIGN_PLAN":
        if all(i.get("design_ready") for i in batch):
            return TransitionAction(new_state="DESIGN_REVIEW", send_review=True)
        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    if state in ("DESIGN_REVIEW", "CODE_REVIEW"):
        key = "design_reviews" if "DESIGN" in state else "code_reviews"
        review_mode = data.get("review_mode", "standard") if data else "standard"

        # "skip" mode: 即座に承認状態へ遷移
        if review_mode == "skip":
            appr = "DESIGN_APPROVED" if "DESIGN" in state else "CODE_APPROVED"
            return TransitionAction(
                new_state=appr,
                impl_msg=f"[review_mode=skip] 自動承認: {appr}",
            )

        mode_config = REVIEW_MODES.get(review_mode, REVIEW_MODES["standard"])
        min_rev = mode_config["min_reviews"]
        count, has_p0 = count_reviews(batch, key)

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
        pending = _get_pending_reviewers(batch, key, mode_config["members"])
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
            send_merge_summary=True,
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
        # CC未実行 → 起動指示
        if data is not None and not _is_cc_running(data):
            return TransitionAction(run_cc=True)
        # CC実行中 → 何もしない
        return TransitionAction()

    return TransitionAction()


def _is_cc_running(data: dict) -> bool:
    """パイプラインに記録されたCC PIDが生存中か判定。"""
    pid = data.get("cc_pid")
    if not pid:
        return False
    return Path(f"/proc/{pid}").exists()


def _is_agent_inactive(agent_id: str, pipeline_data: dict | None = None) -> bool:
    """エージェントが非アクティブ(81秒以上更新なし)かどうか判定。

    CC実行中（cc_pid が /proc に存在）はアクティブと判定する。
    """
    # CC実行中ならアクティブ
    if pipeline_data is not None and _is_cc_running(pipeline_data):
        return False
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


def _get_pending_reviewers(batch: list, review_key: str, reviewers: list) -> list:
    """全Issueのレビューを完了していないレビュアーのリストを返す。"""
    pending = []
    for reviewer in reviewers:
        # 全Issueにこのレビュアーのレビューがあるか
        if not all(reviewer in i.get(review_key, {}) for i in batch):
            pending.append(reviewer)
    return pending


def _auto_push_and_close(repo_path: str, gitlab: str, batch: list, project: str) -> None:
    """DONE遷移時に git push + issue close を自動実行。"""
    import subprocess as _sp
    from config import GLAB_BIN

    # git push
    if repo_path:
        try:
            result = _sp.run(
                ["git", "-C", repo_path, "push"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                log(f"[{project}] git push 成功")
            else:
                log(f"[{project}] git push 失敗: {result.stderr.strip()}")
        except Exception as e:
            log(f"[{project}] git push エラー: {e}")

    # issue close
    for item in batch:
        issue_num = item.get("issue")
        if not issue_num:
            continue
        try:
            result = _sp.run(
                [GLAB_BIN, "issue", "close", str(issue_num), "-R", gitlab],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                log(f"[{project}] Issue #{issue_num} closed")
            else:
                log(f"[{project}] Issue #{issue_num} close失敗: {result.stderr.strip()}")
        except Exception as e:
            log(f"[{project}] Issue #{issue_num} closeエラー: {e}")


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
            if not _is_agent_inactive(implementer, data):
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
            "review_mode": data.get("review_mode", "standard"),
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
            # 遷移時に既に詳細メッセージを送っているので、催促は常に "continue"
            nudge_msg = "continue"
            notify_implementer(notification["implementer"], nudge_msg)
            ts = _datetime.now(JST).strftime("%m/%d %H:%M")
            notify_discord(f"[{pj}] {action.nudge}: 実装担当に通知送信 ({ts})")
            return

        ts = _datetime.now(JST).strftime("%m/%d %H:%M")
        notify_discord(f"[{pj}] {notification['old_state']} → {action.new_state} ({ts})")

        # MERGE_SUMMARY_SENT遷移時: #dev-bar にサマリーを投稿
        if action.send_merge_summary:
            from config import MERGE_SUMMARY_FOOTER, DISCORD_CHANNEL
            from notify import post_discord
            batch = notification["batch"]
            content = _format_merge_summary(pj, batch)
            message_id = post_discord(DISCORD_CHANNEL, content)
            if message_id:
                # summary_message_id をパイプラインに保存
                path = get_path(pj)
                def _save_summary_id(data):
                    data["summary_message_id"] = message_id
                update_pipeline(path, _save_summary_id)
                log(f"[{pj}] merge summary posted (message_id={message_id})")
            else:
                log(f"[{pj}] WARNING: merge summary post failed")

        # DONE遷移時: git push + issue close を自動実行
        if action.new_state == "DONE":
            _auto_push_and_close(
                notification.get("repo_path", ""),
                notification["gitlab"],
                notification["batch"],
                pj,
            )

        if action.impl_msg:
            notify_implementer(
                notification["implementer"],
                f"[devbar] {pj}: {action.impl_msg}",
            )
        if action.send_review:
            review_mode = notification.get("review_mode", "standard")
            # REVISE→REVIEW の再レビューではレビュアーの /new 不要（コンテキスト維持）
            old_state = notification.get("old_state", "")
            skip_new = old_state in ("DESIGN_REVISE", "CODE_REVISE")
            notify_reviewers(
                pj, action.new_state, notification["batch"], notification["gitlab"],
                repo_path=notification.get("repo_path", ""),
                review_mode=review_mode,
                skip_new=skip_new,
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
