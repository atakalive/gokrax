"""engine/reviewer.py - レビュー関連ロジック（watchdog.pyから分離）"""

import re
import time
from datetime import datetime

import config
from config import (
    AGENTS, REVIEW_MODES, POST_NEW_COMMAND_WAIT_SEC,
)
from engine.shared import log
from notify import send_to_agent_queued, ping_agent


def _reset_reviewers(review_mode: str = "standard", implementer: str = "") -> list[str]:
    """レビュアー（+実装担当）に /new を先行送信（collectキュー経由）。free tier をping確認。

    Args:
        review_mode: Review mode to determine member list
        implementer: Implementer agent ID (if DESIGN_PLAN state)

    Returns:
        List of excluded reviewer names (those who failed ping check)
    """
    mode_config = REVIEW_MODES.get(review_mode, REVIEW_MODES["standard"])
    targets = set(mode_config["members"])
    if implementer:
        targets.add(implementer)

    log(f"[/new] reset_reviewers: mode={review_mode}, impl='{implementer}', targets={sorted(targets)}")

    # Send /new to all targets
    sent_impl = False
    for r in targets:
        if r in AGENTS:
            log(f"[/new] sending /new to {r}")
            if not send_to_agent_queued(r, "/new"):
                log(f"[/new] WARNING: failed to send /new to {r}")
            if r == implementer:
                sent_impl = True
        else:
            log(f"[/new] SKIP {r} (not in AGENTS)")

    # Wait for session reset completion
    if sent_impl or targets:
        log(f"[/new] waiting {POST_NEW_COMMAND_WAIT_SEC} sec for session reset")
        if not config.DRY_RUN:
            time.sleep(POST_NEW_COMMAND_WAIT_SEC)

    # Ping free tier reviewers
    excluded = []

    # Check if any free tier members exist in this mode
    free_members = [m for m in mode_config["members"] if config.get_tier(m) == "free"]
    if not free_members:
        log("[/new] no free tier members in mode, skipping ping")
        return excluded

    for reviewer in free_members:
        if not ping_agent(reviewer):
            log(f"[/new] WARNING: free tier reviewer {reviewer} failed ping, excluding")
            excluded.append(reviewer)

    if excluded:
        log(f"[/new] excluded {len(excluded)} reviewers: {excluded}")

    return excluded


def _reset_short_context_reviewers(review_mode: str) -> None:
    """keep_ctx スキップ時でも short-context tier のレビュアーだけ /new を送信。"""
    mode_config = REVIEW_MODES.get(review_mode)
    if mode_config is None:
        log(f"[/new] WARNING: unknown review_mode '{review_mode}', skipping short-context reset")
        return
    short_ctx = [m for m in mode_config["members"]
                 if config.get_tier(m) == "short-context" and m in AGENTS]
    if not short_ctx:
        return
    for r in short_ctx:
        log(f"[/new] sending /new to {r} (short-context, forced)")
        if not send_to_agent_queued(r, "/new"):
            log(f"[/new] WARNING: failed to send /new to {r}")
    if not config.DRY_RUN:
        log(f"[/new] waiting {POST_NEW_COMMAND_WAIT_SEC} sec for short-context reset")
        time.sleep(POST_NEW_COMMAND_WAIT_SEC)


def count_reviews(batch: list, key: str) -> tuple:
    """(最小レビュー数, P0有無, P1有無, P2有無)"""
    min_n = min((len(i.get(key, {})) for i in batch), default=0)
    has_p0 = any(
        r.get("verdict", "").upper() in ("REJECT", "P0")
        for i in batch for r in i.get(key, {}).values()
    )
    has_p1 = any(
        r.get("verdict", "").upper() == "P1"
        for i in batch for r in i.get(key, {}).values()
    )
    has_p2 = any(
        r.get("verdict", "").upper() == "P2"
        for i in batch for r in i.get(key, {}).values()
    )
    return min_n, has_p0, has_p1, has_p2


def _awaiting_dispute_re_review(
    batch: list, review_key: str, excluded: list[str] | None = None,
) -> list[str]:
    """dispute 後に再レビューを出していないレビュアーのリストを返す。

    対象:
    - status == "pending": 常に awaiting（未解決）
    - status == "accepted": review が無い or review.at < dispute.resolved_at → awaiting
    - status == "rejected": 対象外（元の判定維持）

    excluded に含まれるレビュアーは対象外（レート制限等で除外済み）。
    """
    excluded_set = set(excluded or [])
    awaiting = set()
    for issue in batch:
        for d in issue.get("disputes", []):
            status = d.get("status", "")
            if status not in ("pending", "accepted"):
                continue
            reviewer = d.get("reviewer", "")
            if reviewer in excluded_set:
                continue
            phase = d.get("phase", "")
            expected_key = "design_reviews" if phase == "design" else "code_reviews"
            if expected_key != review_key:
                continue
            if status == "pending":
                awaiting.add(reviewer)
                continue
            # accepted: review の有無と時刻で判定
            resolved_at = d.get("resolved_at", "")
            review = issue.get(review_key, {}).get(reviewer)
            if review is None:
                awaiting.add(reviewer)
            else:
                review_at = review.get("at", "")
                try:
                    r_dt = datetime.fromisoformat(review_at)
                    d_dt = datetime.fromisoformat(resolved_at)
                    if r_dt < d_dt:
                        awaiting.add(reviewer)
                except (ValueError, TypeError):
                    if review_at < resolved_at:
                        awaiting.add(reviewer)
    return sorted(awaiting)


def _revise_target_issues(batch: list, review_key: str, revised_key: str, p2_fix: bool = False) -> str:
    """REVISE対象Issueを文字列化。P0/REJECT/P1が付いた未修正Issueを明示。p2_fix時はP2も含む。

    対象が特定できない場合（flag差し込み等）は全Issueをフォールバックで返す。
    """
    targets = []
    for i in batch:
        if i.get(revised_key):
            continue
        reviews = i.get(review_key, {})
        has_p0 = any(
            r.get("verdict", "").upper() in ("REJECT", "P0")
            for r in reviews.values()
        )
        has_p1 = any(
            r.get("verdict", "").upper() == "P1"
            for r in reviews.values()
        )
        has_p2 = any(
            r.get("verdict", "").upper() == "P2"
            for r in reviews.values()
        )
        if has_p0 or has_p1 or (p2_fix and has_p2):
            targets.append(f"#{i['issue']}")
    return ", ".join(targets) if targets else ", ".join(f"#{i['issue']}" for i in batch)


def clear_reviews(batch: list, key: str, revised_key: str):
    """P0/REJECT/P1を出したレビュアーのレビューのみクリアする。
    APPROVEのレビューは保持。
    revised_key は全Issueから削除（次のREVISEサイクル用）。
    """
    for issue in batch:
        reviews = issue.get(key, {})
        to_clear = [
            reviewer for reviewer, r in reviews.items()
            if r.get("verdict", "").upper() in ("REJECT", "P0", "P1", "P2")
        ]
        for reviewer in to_clear:
            del reviews[reviewer]
        issue.pop(revised_key, None)


def _get_pending_reviewers(batch: list, review_key: str, reviewers: list, excluded=None) -> list:
    """全Issueのレビューを完了していないレビュアーのリストを返す。

    Args:
        excluded: 除外するレビュアーのリスト（例: レート制限で応答不能になった者）
    """
    excluded_set = set(excluded or [])
    pending = []
    for reviewer in reviewers:
        if reviewer in excluded_set:
            continue
        # 全Issueにこのレビュアーのレビューがあるか
        if not all(reviewer in i.get(review_key, {}) for i in batch):
            pending.append(reviewer)
    return pending


def _cleanup_review_files(project: str) -> None:
    """REVIEW_FILE_DIR 内の当該プロジェクトのファイルのみ削除する。

    ファイル名のダブルハイフン ``--`` セパレータより前が sanitized project 名。
    sanitized は notify._write_review_file() と同じ正規化ルール:
        sanitized = re.sub(r'[/\\\\s]', '-', project)
    判定: f.name.startswith(f"{sanitized}--")

    ダブルハイフンにより、プロジェクト "foo" のクリーンアップが
    "foo-bar" のファイルを誤削除しない（"foo--" vs "foo-bar--"）。

    呼び出しタイミング:
    - IDLE→DESIGN_PLAN 遷移時（バッチ開始時）
    - DONE遷移時（二重保険）
    ディレクトリが存在しない場合は何もしない。
    """
    review_dir = config.REVIEW_FILE_DIR
    if not review_dir.exists():
        return
    sanitized = re.sub(r'[/\\\s]', '-', project)
    prefix = f"{sanitized}--"
    for f in review_dir.iterdir():
        if f.name.startswith(prefix):
            try:
                f.unlink(missing_ok=True)
            except OSError as e:
                log(f"Warning: failed to delete review file {f}: {e}")
