"""engine/reviewer.py - レビュー関連ロジック（watchdog.pyから分離）"""

import logging
import re
import time
from datetime import datetime

import config
from config import (
    AGENTS, REVIEW_MODES, POST_NEW_COMMAND_WAIT_SEC,
)
from engine.shared import log
from notify import send_to_agent_queued, ping_agent

_logger = logging.getLogger(__name__)


def _reset_reviewers(review_mode: str, implementer: str = "") -> list[str]:
    """Reset reviewer/implementer sessions before a review cycle.

    For openclaw backend: sends /new to each target, waits, then pings free tier.
    For pi backend: calls reset_session() for each target (no /new, no wait).

    Args:
        review_mode: Review mode to determine member list
        implementer: Implementer agent ID (if DESIGN_PLAN state)

    Returns:
        List of excluded reviewer names (those who failed ping check)
    """
    from engine.backend import reset_session as _dispatch_reset
    from engine.backend import resolve_backend

    mode_config = REVIEW_MODES[review_mode]
    targets = set(mode_config["members"])
    if implementer:
        targets.add(implementer)

    log(f"[/new] reset_reviewers: mode={review_mode}, impl='{implementer}', targets={sorted(targets)}")

    excluded = []
    oc_targets = []  # openclaw agents that received /new
    for r in targets:
        if r not in AGENTS:
            log(f"[/new] SKIP {r} (not in AGENTS)")
            continue
        try:
            agent_backend = resolve_backend(r)
        except ValueError:
            log(f"[/new] ERROR: invalid backend for {r}, skipping")
            excluded.append(r)
            continue
        if agent_backend == "pi":
            log(f"[/new] reset_session for {r} (pi backend)")
            _dispatch_reset(r)
        else:
            log(f"[/new] sending /new to {r}")
            if not send_to_agent_queued(r, "/new"):
                log(f"[/new] WARNING: failed to send /new to {r}")
            oc_targets.append(r)

    # sleep は openclaw 経路のセッションリセット完了を待つため。
    # /new 送信後、エージェント側で新セッションが立ち上がるまでの猶予。
    # openclaw エージェントが1人以上いた場合のみ待機する。
    if oc_targets and not config.DRY_RUN:
        log(f"[/new] waiting {POST_NEW_COMMAND_WAIT_SEC} sec for openclaw session reset")
        time.sleep(POST_NEW_COMMAND_WAIT_SEC)

    # Ping free tier reviewers (openclaw only — pi agents have no ping check)
    free_members = [m for m in mode_config["members"]
                    if get_tier(m) == "free" and m in oc_targets]
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
    """Reset short-context tier reviewers before a review cycle.

    For openclaw backend: sends /new and waits POST_NEW_COMMAND_WAIT_SEC.
    For pi backend: calls reset_session() (no /new, no wait).
    """
    from engine.backend import reset_session as _dispatch_reset
    from engine.backend import resolve_backend

    mode_config = REVIEW_MODES[review_mode]
    short_ctx = [m for m in mode_config["members"]
                 if get_tier(m) == "short-context" and m in AGENTS]
    if not short_ctx:
        return

    oc_short = []
    for r in short_ctx:
        try:
            agent_backend = resolve_backend(r)
        except ValueError:
            log(f"[/new] ERROR: invalid backend for {r} (short-context), skipping")
            continue
        if agent_backend == "pi":
            log(f"[/new] reset_session for {r} (short-context, pi backend)")
            _dispatch_reset(r)
        else:
            log(f"[/new] sending /new to {r} (short-context, forced)")
            if not send_to_agent_queued(r, "/new"):
                log(f"[/new] WARNING: failed to send /new to {r}")
            oc_short.append(r)
    # sleep は openclaw の /new 処理完了待ち（POST_NEW_COMMAND_WAIT_SEC 秒）
    if oc_short and not config.DRY_RUN:
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
    - status == "accepted" + resolved_verdict == "APPROVE": 対象外（レビュー完了扱い。
      dispute 解決自体が final verdict として機能し、reviewer の再投稿は不要）
    - status == "accepted" + それ以外の resolved_verdict: review が無い or
      review.at < dispute.resolved_at → awaiting
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
            # accepted + resolved_verdict=APPROVE: dispute resolution が
            # final verdict として機能する。レビュー完了扱い（再レビュー不要）。
            # reviewer の再投稿は不要であり、dispute 解決自体が APPROVE を確定する。
            resolved_verdict = d.get("resolved_verdict", "")
            if resolved_verdict.upper() == "APPROVE":
                continue
            # accepted (verdict降格: P0→P2 等): review の有無と時刻で判定
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
            or (r.get("verdict", "").upper() == "APPROVE"
                and r.get("pass", 1) < r.get("target_pass", 1))
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


def get_tier(agent_name: str) -> str:
    """Return tier for agent. Unknown agents are conservatively marked as 'free'."""
    for tier, members in config.REVIEWER_TIERS.items():
        if agent_name in members:
            return tier
    return "free"


def _validate_reviewer_tiers() -> None:
    """Warn if REVIEW_MODES contains reviewers not in REVIEWER_TIERS,
    or if a reviewer appears in multiple tiers."""
    all_tier_members = set()
    member_to_tiers: dict[str, list[str]] = {}
    for tier, members in config.REVIEWER_TIERS.items():
        for m in members:
            member_to_tiers.setdefault(m, []).append(tier)
        all_tier_members.update(members)

    # 一意性チェック
    for member, tiers in member_to_tiers.items():
        if len(tiers) > 1:
            _logger.warning(
                "[config] Reviewer '%s' appears in multiple tiers: %s. "
                "get_tier() will return the first match (dict order).",
                member, tiers
            )

    for mode_name, cfg in config.REVIEW_MODES.items():
        for reviewer in cfg["members"]:
            if reviewer not in all_tier_members:
                _logger.warning(
                    "[config] Reviewer '%s' in mode '%s' not found in REVIEWER_TIERS, will be treated as 'free'",
                    reviewer, mode_name
                )
        # フェーズ上書き内の members もチェック
        for phase in ("design", "code"):
            phase_cfg = cfg.get(phase, {})
            if "members" in phase_cfg:
                for reviewer in phase_cfg["members"]:
                    if reviewer not in all_tier_members:
                        _logger.warning(
                            "[config] Reviewer '%s' in mode '%s'.%s not found in REVIEWER_TIERS, "
                            "will be treated as 'free'",
                            reviewer, mode_name, phase
                        )


_validate_reviewer_tiers()
