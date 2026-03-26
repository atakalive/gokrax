from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import datetime as _datetime

from config import (
    BLOCK_TIMERS,
    GOKRAX_CLI,
    EXTENDABLE_STATES,
    EXTEND_NOTICE_THRESHOLD,
    LOCAL_TZ,
    MAX_TIMEOUT_EXTENSION,
    NUDGE_GRACE_SEC,
    REVIEW_MODES,
    STATE_PHASE_MAP,
)
from engine.reviewer import (
    _awaiting_dispute_re_review,
    _get_pending_reviewers,
    _revise_target_issues,
    count_reviews,
)
from engine.shared import _is_cc_running, _is_ok_reply, log
from messages import render
from notify import notify_discord, notify_implementer, notify_reviewers
from pipeline_io import clear_pending_notification, get_path, load_pipeline


def get_min_reviews(mode_config: dict) -> int:
    """mode_config から min_reviews を取得する。未定義または None の場合は len(members) を返す。"""
    val = mode_config.get("min_reviews")
    if val is not None:
        return val
    return len(mode_config.get("members", []))


def _build_phase_config(mode_config: dict, phase: str) -> dict:
    """mode_config から指定フェーズの review_config エントリを生成する。

    min_reviews の計算順序:
    1. フェーズ override に明示的な min_reviews がある → それを使う
    2. ベース mode_config に明示的な min_reviews がある → それを使う
    3. どちらにもない → override 適用後の最終的な members の len を使う

    この順序により、override で members だけ変更した場合に
    ベースの min_reviews が残ってデッドロックになる問題を防ぐ。
    """
    # Step 1: ベース構成を組み立てる（min_reviews 以外）
    base: dict = {
        "members": list(mode_config.get("members", [])),
        "n_pass": dict(mode_config.get("n_pass", {})),
        "grace_period_sec": mode_config.get("grace_period_sec", 0),
    }

    # Step 2: フェーズ override を適用（min_reviews 以外）
    override = mode_config.get(phase, {})
    if override:
        if "members" in override:
            base["members"] = list(override["members"])
        if "n_pass" in override:
            base["n_pass"] = dict(override["n_pass"])
        if "grace_period_sec" in override:
            base["grace_period_sec"] = override["grace_period_sec"]

    # Step 3: min_reviews を解決（override 適用後の members を考慮）
    if override and "min_reviews" in override:
        base["min_reviews"] = override["min_reviews"]
    elif mode_config.get("min_reviews") is not None:
        base["min_reviews"] = mode_config["min_reviews"]
    else:
        base["min_reviews"] = len(base["members"])

    # Step 4: min_reviews を members 数でキャップ（デッドロック防止）
    # 例: ベース min_reviews=3 + override members=[r1] → min_reviews=min(3,1)=1
    if base["min_reviews"] > len(base["members"]):
        base["min_reviews"] = len(base["members"])

    return base


def build_review_config(mode_config: dict) -> dict:
    """mode_config から design/code 両フェーズの review_config を生成する。

    公開 API。watchdog.py の INITIALIZE 処理から呼ぶ。

    Args:
        mode_config: REVIEW_MODES[review_mode] の値

    Returns:
        {"design": {...}, "code": {...}}
    """
    return {
        "design": _build_phase_config(mode_config, "design"),
        "code": _build_phase_config(mode_config, "code"),
    }


def get_phase_config(data: dict | None, phase: str) -> dict:
    """pipeline data から指定フェーズ（"design" or "code"）の review config を取得する。

    review_config が存在すれば使い、なければ review_mode から REVIEW_MODES を引いてフォールバックする。
    フォールバック時も _build_phase_config を経由するため、返り値の構造は常に同一
    （members, min_reviews, n_pass, grace_period_sec の4キーが保証される）。

    Args:
        data: pipeline.json の data dict。None の場合は REVIEW_MODES["standard"] にフォールバック。
        phase: "design" or "code"

    Returns:
        {"members": list[str], "min_reviews": int, "n_pass": dict, "grace_period_sec": int}
    """
    if data is not None:
        rc = data.get("review_config", {}).get(phase)
        if rc is not None:
            return rc
    # フォールバック: review_config が存在しない旧パイプライン、または data=None
    review_mode = data.get("review_mode", "standard") if data else "standard"
    mode_config = REVIEW_MODES.get(review_mode, REVIEW_MODES["standard"])
    return _build_phase_config(mode_config, phase)


@dataclass
class TransitionAction:
    """check_transition() の返り値。new_state が None なら遷移不要。"""
    new_state: str | None = None
    impl_msg: str | None = None
    send_review: bool = False
    reset_reviewers: bool = False  # レビュアーに /new を先行送信
    send_merge_summary: bool = False  # #gokrax にマージサマリーを投稿
    run_cc: bool = False  # CC CLI を直接起動
    run_test: bool = False  # テスト実行トリガー（CODE_TEST進入時）
    nudge: str | None = None   # 催促通知が必要な状態名
    nudge_reviewers: list | None = None  # 催促が必要なレビュアーのリスト
    dispute_nudge_reviewers: list | None = None  # dispute pending で催促が必要なレビュアー
    extend_notice: str | None = None  # タイムアウト延長案内メッセージ
    save_grace_met_at: str | None = None  # grace met_atをpipelineに保存する必要がある場合のキー名
    clear_grace_met_at: str | None = None  # 遷移確定時にクリアする grace met_at のキー名
    npass_target_reviewers: list | None = None  # NPASS遷移時のターゲットレビュアー一覧
    skipped_issues: list[dict] | None = None   # ASSESSMENT で除外された Issue の dict リスト
    remaining_issues: list[dict] | None = None  # ASSESSMENT で残留した Issue の dict リスト
    grace_skipped_reviewers: list[str] | None = None  # grace period 切れでスキップされたレビュアー


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


def _get_timeout_extension(data: dict) -> int:
    """pipeline data から timeout_extension を取得し、[0, MAX_TIMEOUT_EXTENSION] にクランプする。

    - 非数値型（str 等）: 0 を返す
    - nan / inf: 0 を返す
    - 負値: 0 にクランプ
    - MAX_TIMEOUT_EXTENSION 超: MAX_TIMEOUT_EXTENSION にクランプ
    """
    import math

    raw = data.get("timeout_extension", 0)
    if not isinstance(raw, (int, float)):
        return 0
    if not math.isfinite(raw):
        return 0
    return max(0, min(int(raw), MAX_TIMEOUT_EXTENSION))


def _check_nudge(state: str, data: dict) -> TransitionAction | None:
    """催促/BLOCKED判定。該当しなければNone。"""
    block_sec = BLOCK_TIMERS.get(state)
    if not block_sec or data is None:
        return None

    # 延長分を加算
    block_sec += _get_timeout_extension(data)

    entered_at = _get_state_entered_at(data, state)
    elapsed = 0.0
    if entered_at is not None:
        elapsed = (_datetime.now(LOCAL_TZ) - entered_at).total_seconds()

    # BLOCKED判定（時間超過）
    if elapsed >= block_sec:
        return TransitionAction(
            new_state="BLOCKED",
            impl_msg=f"{state} タイムアウト。応答がありませんでした。",
        )

    # 猶予期間内は催促しない
    if elapsed < NUDGE_GRACE_SEC:
        return None

    # 催促メッセージ作成
    nudge = TransitionAction(nudge=state)

    # 延長案内を付加（残り5分未満 + 対象フェーズ）
    remaining = block_sec - elapsed
    if remaining < EXTEND_NOTICE_THRESHOLD and state in EXTENDABLE_STATES:
        project = data.get("project", "")
        extend_count = data.get("extend_count", 0)
        max_extends = 2
        if extend_count < max_extends:
            nudge.extend_notice = (
                f"\n\n⏰ タイムアウトまで残り{int(remaining)}秒（延長残り{max_extends - extend_count}回）。延長が必要なら:\n"
                f"{GOKRAX_CLI} extend --project {project} --by 600"
            )
        else:
            nudge.extend_notice = (
                f"\n\n⏰ タイムアウトまで残り{int(remaining)}秒。延長上限に達しています。"
            )

    return nudge



def _get_reviewer_entry(batch: list, key: str, reviewer: str) -> dict | None:
    """バッチの最初のIssueから指定レビュアーのレビューエントリを取得。"""
    for issue in batch:
        entry = issue.get(key, {}).get(reviewer)
        if entry is not None:
            return entry
    return None


def _worst_risk(batch: list) -> str:
    """バッチ内の全 Issue の domain_risk から最悪リスクを返す。

    順序: n/a(-1) < none(0) < low(1) < high(2)
    "n/a" は未判定、"none" は判定済みリスクなし。
    空バッチの場合は "n/a"（未判定）を返す。
    """
    _RISK_ORDER = {"n/a": -1, "none": 0, "low": 1, "high": 2}
    worst = "n/a"
    for issue in batch:
        a = issue.get("assessment", {})
        ir = a.get("domain_risk", "n/a")
        if _RISK_ORDER.get(ir, -1) > _RISK_ORDER.get(worst, -1):
            worst = ir
    return worst


def _read_domain_risk(project: str, repo_path: str) -> str:
    """DOMAIN_RISK.md を読み込んで内容を返す。ファイルなし/エラー時は空文字。"""
    import logging
    from pathlib import Path

    from config import PROJECT_RISK_FILES

    _logger = logging.getLogger(__name__)

    # 1. カスタムパス優先
    custom = PROJECT_RISK_FILES.get(project)
    if custom:
        if not Path(custom).is_absolute():
            _logger.warning("PROJECT_RISK_FILES[%s] is a relative path: %s", project, custom)
        risk_path = Path(custom)
    elif repo_path:
        risk_path = Path(repo_path) / "DOMAIN_RISK.md"
    else:
        return ""

    if not risk_path.exists():
        if custom:
            _logger.warning("DOMAIN_RISK.md not found at custom path: %s", risk_path)
        return ""

    try:
        content = risk_path.read_text(encoding="utf-8")
    except Exception as e:
        _logger.warning("Failed to read DOMAIN_RISK.md (%s): %s", risk_path, e)
        return ""

    if len(content) > 10_000:
        _logger.warning("DOMAIN_RISK.md exceeds 10,000 chars (%d), truncating", len(content))
        content = content[:10_000]

    return content


def get_notification_for_state(
    state: str,
    project: str = "",
    batch: list | None = None,
    gitlab: str = "",
    implementer: str = "",
    p2_fix: bool = False,
    comment: str = "",
    repo_path: str = "",
) -> TransitionAction:
    """全状態の通知メッセージを一元管理。

    遷移通知（初回）にも催促（nudge）にも使う。ただし現状の催促は "continue" のみ。
    check_transition(), cmd_transition(), 催促処理 から呼ばれる共通ロジック。
    通知不要な状態は TransitionAction() を返す。
    """
    batch = batch or []

    if state in ("DESIGN_REVIEW", "CODE_REVIEW"):
        return TransitionAction(send_review=True)

    if state in ("DESIGN_REVIEW_NPASS", "CODE_REVIEW_NPASS"):
        return TransitionAction(send_review=True)

    from config import OWNER_NAME
    comment_line = f"{OWNER_NAME}からの要望: {comment}\n" if comment else ""

    if state == "DESIGN_PLAN":
        issues_str = ", ".join(
            f"#{i['issue']}" for i in batch if not i.get("design_ready")
        ) or "（全Issue）"
        msg = render("dev.design_plan", "transition",
            project=project, issues_str=issues_str,
            comment_line=comment_line, GOKRAX_CLI=GOKRAX_CLI,
        )
        return TransitionAction(impl_msg=msg, reset_reviewers=True)

    if state == "DESIGN_REVISE":
        issues_str = _revise_target_issues(batch, "design_reviews", "design_revised", p2_fix=p2_fix)
        p2_note = ""
        if p2_fix:
            p2_note = "\n⚠️ --p2-fix モード: P2 指摘も全件修正が必要です。P0/P1 がなくても P2 が残っていれば再度 REVISE に差し戻されます。\n"
        fix_label = "P0/P1/P2指摘" if p2_fix else "P0/P1指摘"
        msg = render("dev.design_revise", "transition",
            project=project, issues_str=issues_str, comment_line=comment_line,
            fix_label=fix_label, p2_note=p2_note, GOKRAX_CLI=GOKRAX_CLI,
        )
        return TransitionAction(impl_msg=msg)

    if state == "CODE_REVISE":
        issues_str = _revise_target_issues(batch, "code_reviews", "code_revised", p2_fix=p2_fix)
        p2_note = ""
        if p2_fix:
            p2_note = "\n⚠️ --p2-fix モード: P2 指摘も全件修正が必要です。P0/P1 がなくても P2 が残っていれば再度 REVISE に差し戻されます。\n"
        fix_label = "P0/P1/P2指摘" if p2_fix else "P0/P1指摘"
        msg = render("dev.code_revise", "transition",
            project=project, issues_str=issues_str, comment_line=comment_line,
            fix_label=fix_label, p2_note=p2_note, GOKRAX_CLI=GOKRAX_CLI,
        )
        return TransitionAction(impl_msg=msg)

    if state == "ASSESSMENT":
        issues_str = ", ".join(f"#{i['issue']}" for i in batch) or "（全Issue）"
        domain_risk_content = _read_domain_risk(project, repo_path)
        msg = render("dev.assessment", "transition",
            project=project, issues_str=issues_str,
            comment_line=comment_line, GOKRAX_CLI=GOKRAX_CLI,
            domain_risk_content=domain_risk_content,
            batch=batch,
        )
        return TransitionAction(impl_msg=msg)

    # 現在はシステム側でCCを動かしているため使っていないが、残しておく
    if state == "IMPLEMENTATION":
        return TransitionAction(run_cc=True, reset_reviewers=True)

    return TransitionAction()


def _resolve_review_outcome(
    state: str, data: dict | None, batch: list,
    has_p0: bool, has_p1: bool, has_p2: bool,
    comment: str = "",
) -> TransitionAction:
    """min_reviews 到達後の遷移先を決定する。APPROVED or REVISE or BLOCKED.

    義務レベル:
    - P0: 常に義務。max cycles → BLOCKED。
    - P1: 常に義務。max cycles → BLOCKED。
    - P2: p2_fix 有効時のみ義務。max cycles → APPROVE フォールバック（免除）。
    """
    appr = "DESIGN_APPROVED" if "DESIGN" in state else "CODE_APPROVED"
    revise_state = "DESIGN_REVISE" if "DESIGN" in state else "CODE_REVISE"
    pj = data.get("project", "") if data else ""
    p2_fix = data.get("p2_fix", False) if data else False

    # P0 or P1 あり → REVISE or BLOCKED/フォールバック
    if has_p0 or has_p1:
        from config import MAX_REVISE_CYCLES, OWNER_NAME
        counter_key = "design_revise_count" if "DESIGN" in state else "code_revise_count"
        current_count = data.get(counter_key, 0) if data else 0

        if current_count >= MAX_REVISE_CYCLES:
            severity = "P0" if has_p0 else "P1"
            # P0/P1 あり → BLOCKED（いずれも免除しない）
            return TransitionAction(
                new_state="BLOCKED",
                impl_msg=render("dev.blocked", "blocked_max_cycles",
                    state=state, MAX_REVISE_CYCLES=MAX_REVISE_CYCLES,
                    OWNER_NAME=OWNER_NAME, severity=severity,
                ),
            )

        return TransitionAction(
            new_state=revise_state,
            impl_msg=get_notification_for_state(revise_state, project=pj, batch=batch, p2_fix=p2_fix, comment=comment).impl_msg,
        )

    # P0/P1 なし + p2_fix 有効 + P2 あり → REVISE（max_revise_cycles フォールバック付き）
    if p2_fix and has_p2:
        from config import MAX_REVISE_CYCLES
        counter_key = "design_revise_count" if "DESIGN" in state else "code_revise_count"
        current_count = data.get(counter_key, 0) if data else 0

        if current_count >= MAX_REVISE_CYCLES:
            # フォールバック: P0/P1 がないので APPROVE する
            return TransitionAction(
                new_state=appr,
                impl_msg=get_notification_for_state(appr, project=pj, batch=batch, comment=comment).impl_msg,
            )

        return TransitionAction(
            new_state=revise_state,
            impl_msg=get_notification_for_state(revise_state, project=pj, batch=batch, p2_fix=True, comment=comment).impl_msg,
        )

    # P0/P1/P2 なし or (P2 ありだが p2_fix 無効) → APPROVE
    return TransitionAction(
        new_state=appr,
        impl_msg=get_notification_for_state(appr, project=pj, batch=batch, comment=comment).impl_msg,
    )


def check_transition(state: str, batch: list, data: dict | None = None) -> TransitionAction:
    """現在の状態とバッチから次の遷移アクションを決定する。

    data を読み取るが直接変更しない。data への書き込みが必要な場合は
    TransitionAction のフィールド（save_grace_met_at, clear_grace_met_at 等）
    で呼び出し側に委譲する。
    """
    if state in ("IDLE", "BLOCKED"):
        return TransitionAction()

    # INITIALIZE → DESIGN_PLAN: 自動遷移（初期化処理は watchdog の do_transition 内で実行）
    if state == "INITIALIZE":
        skip_design = data.get("skip_design", False) if data else False
        if skip_design:
            return TransitionAction(
                new_state="DESIGN_APPROVED",
            )
        pj = data.get("project", "") if data else ""
        comment = data.get("comment", "") if data else ""
        notif = get_notification_for_state(
            "DESIGN_PLAN", project=pj, batch=batch, comment=comment,
        )
        return TransitionAction(
            new_state="DESIGN_PLAN",
            impl_msg=notif.impl_msg,
            reset_reviewers=True,
        )

    if state == "MERGE_SUMMARY_SENT":
        if data is None:
            return TransitionAction()

        # Automerge: skip approval wait (Issue #45)
        if data.get("automerge", False):
            return TransitionAction(new_state="DONE")

        # Manual merge: wait for M's OK
        from notify import fetch_discord_replies
        from config import MERGE_APPROVER_DISCORD_ID, DISCORD_CHANNEL
        summary_id = data.get("summary_message_id")
        if not summary_id:
            return TransitionAction()
        messages = fetch_discord_replies(DISCORD_CHANNEL, summary_id)
        for msg in messages:
            ref = msg.get("message_reference", {})
            if (ref.get("message_id") == summary_id
                    and msg.get("author", {}).get("id") == MERGE_APPROVER_DISCORD_ID
                    and _is_ok_reply(msg.get("content", ""))):
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
        appr = "DESIGN_APPROVED" if "DESIGN" in state else "CODE_APPROVED"
        revise_state = "DESIGN_REVISE" if "DESIGN" in state else "CODE_REVISE"
        review_mode = data.get("review_mode", "standard") if data else "standard"

        # "skip" mode: 即座に承認状態へ遷移
        if review_mode == "skip":
            return TransitionAction(
                new_state=appr,
                impl_msg=f"[review_mode=skip] 自動承認: {appr}",
            )

        phase = "design" if "DESIGN" in state else "code"
        phase_config = get_phase_config(data, phase)
        min_rev = data.get("min_reviews_override", phase_config["min_reviews"]) if data else phase_config["min_reviews"]
        excluded = data.get("excluded_reviewers", []) if data else []
        effective_count = len(phase_config["members"]) - len(excluded)
        grace_sec = phase_config["grace_period_sec"]

        count, has_p0, has_p1, has_p2 = count_reviews(batch, key)

        # Check for unresolved P0 flags (flag P0 → immediate REVISE)
        flag_phase = STATE_PHASE_MAP.get(state)
        if flag_phase is not None:
            has_flag_p0 = any(
                f.get("verdict") == "P0"
                and not f.get("resolved")
                and f.get("phase") == flag_phase
                for issue in batch
                for f in issue.get("flags", [])
            )
            if has_flag_p0:
                revise_state = "DESIGN_REVISE" if "DESIGN" in state else "CODE_REVISE"
                log(f"[FLAG] unresolved P0 flag(s) in {flag_phase} phase → {revise_state}")
                return TransitionAction(new_state=revise_state, send_review=False)
        else:
            # Defensive: should never happen (all REVIEW states are in STATE_PHASE_MAP)
            log(f"[FLAG] WARNING: unknown state {state} in REVIEW block, skipping flag check")

        # Check if min_reviews reached
        if count >= min_rev:
            met_key = f"{'design' if 'DESIGN' in state else 'code'}_min_reviews_met_at"
            met_at_exists = bool(data and data.get(met_key))
            if not met_at_exists:
                log(
                    f"[GRACE] min_reviews={min_rev} met (first detection), effective={effective_count}, grace={grace_sec} sec"
                )

            # Determine if we should transition now
            should_transition = False
            grace_expired = False

            # Case 1: All effective reviewers done → immediate
            if count >= effective_count:
                log(f"[GRACE] all {effective_count} effective reviewers done, transitioning")
                should_transition = True

            # Case 2: Grace period check
            elif min_rev < effective_count and grace_sec > 0 and met_at_exists:
                met_at = datetime.fromisoformat(data[met_key])
                elapsed = (datetime.now(LOCAL_TZ) - met_at).total_seconds()
                if elapsed >= grace_sec:
                    log(f"[GRACE] grace period expired ({elapsed:.1f}s >= {grace_sec}s), transitioning")
                    should_transition = True
                    grace_expired = True
                else:
                    log(f"[GRACE] waiting ({grace_sec - elapsed:.1f}s remaining)")
                    return TransitionAction(save_grace_met_at=met_key)

            # Case 3: No grace (min == effective or grace_sec == 0) → immediate
            else:
                if not met_at_exists and min_rev < effective_count and grace_sec > 0:
                    log("[GRACE] grace period started, save met_at and wait")
                    return TransitionAction(save_grace_met_at=met_key)
                log("[GRACE] no grace period, transitioning")
                should_transition = True

            if should_transition:
                awaiting = _awaiting_dispute_re_review(batch, key, excluded=excluded)
                if awaiting:
                    log(f"[DISPUTE] waiting for re-review from: {', '.join(awaiting)}")
                    should_transition = False

            if should_transition:
                comment = data.get("comment", "") if data else ""
                outcome = _resolve_review_outcome(state, data, batch, has_p0, has_p1, has_p2, comment=comment)

                # NPASS interception: Round 1（revise_count == 0）かつ
                # pass < target_pass のレビュアーがいる → verdict に関わらず NPASS へ遷移。
                # Round 2+（revise_count > 0）ではスキップ。
                counter_key = "design_revise_count" if "DESIGN" in state else "code_revise_count"
                revise_count = data.get(counter_key, 0) if data else 0
                if revise_count == 0:
                    npass_targets: list[str] = []
                    seen: set[str] = set()
                    for issue in batch:
                        for reviewer, entry in issue.get(key, {}).items():
                            if reviewer not in seen and entry.get("pass", 1) < entry.get("target_pass", 1):
                                npass_targets.append(reviewer)
                                seen.add(reviewer)
                    if npass_targets:
                        npass_state = "DESIGN_REVIEW_NPASS" if "DESIGN" in state else "CODE_REVIEW_NPASS"
                        return TransitionAction(
                            new_state=npass_state,
                            send_review=True,
                            npass_target_reviewers=npass_targets,
                            clear_grace_met_at=met_key if met_at_exists else None,
                        )

                # Grace met_at のクリアを呼び出し側に委譲（実際に met_at が存在する場合のみ）
                if met_at_exists:
                    outcome.clear_grace_met_at = met_key
                # Grace period expired 経路のみ: スキップされたレビュアーを記録
                if grace_expired:
                    pending = _get_pending_reviewers(batch, key, phase_config["members"], excluded=excluded)
                    if pending:
                        outcome.grace_skipped_reviewers = pending
                return outcome

        # Not enough reviews yet
        # 1. タイムアウト判定（BLOCKEDのみ早期リターン）
        nudge = _check_nudge(state, data) if data is not None else None
        if nudge and nudge.new_state == "BLOCKED":
            return nudge
        # 2. 未完了レビュアーの催促（猶予期間内はスキップ）
        entered_at = _get_state_entered_at(data, state) if data is not None else None
        if entered_at is not None:
            elapsed = (_datetime.now(LOCAL_TZ) - entered_at).total_seconds()
            if elapsed < NUDGE_GRACE_SEC:
                return TransitionAction()
        # 3. レビュアー催促（最低優先）
        excluded = data.get("excluded_reviewers", []) if data else []
        pending = _get_pending_reviewers(batch, key, phase_config["members"], excluded=excluded)
        dispute_awaiting = _awaiting_dispute_re_review(batch, key, excluded=excluded)
        if pending or dispute_awaiting:
            return TransitionAction(
                nudge="REVIEW",
                nudge_reviewers=sorted(pending) or None,
                dispute_nudge_reviewers=sorted(dispute_awaiting) or None,
            )
        return TransitionAction()

    if state in ("DESIGN_REVIEW_NPASS", "CODE_REVIEW_NPASS"):
        key = "design_reviews" if "DESIGN" in state else "code_reviews"
        npass_targets = data.get("_npass_target_reviewers", []) if data else []

        if not npass_targets:
            return TransitionAction()

        entered_at = _get_state_entered_at(data, state)
        if entered_at is None:
            return TransitionAction()

        # 各ターゲットレビュアーの提出状況を確認（state 進入後に at が更新されたか）
        all_submitted = True
        has_p0 = False
        has_p1 = False
        has_p2 = False

        for reviewer in npass_targets:
            # 全 Issue のレビュー完了を確認（バッチ内パス均一性を盲信しない）
            reviewer_submitted = True
            for issue in batch:
                entry = issue.get(key, {}).get(reviewer)
                if entry is None:
                    reviewer_submitted = False
                    break
                review_at_str = entry.get("at")
                if not review_at_str:
                    reviewer_submitted = False
                    break
                try:
                    review_at = _datetime.fromisoformat(review_at_str)
                    if review_at <= entered_at:
                        reviewer_submitted = False
                        break
                except (ValueError, TypeError):
                    reviewer_submitted = False
                    break

            if not reviewer_submitted:
                all_submitted = False
                # verdict は提出済み Issue の最初のエントリから取得
                entry = _get_reviewer_entry(batch, key, reviewer)
                if entry:
                    v = entry.get("verdict", "").upper()
                    if v in ("REJECT", "P0"):
                        has_p0 = True
                    elif v == "P1":
                        has_p1 = True
                    elif v == "P2":
                        has_p2 = True
                continue

            # 全 Issue 提出済み — verdict を収集（最初の Issue から取得、均一性前提）
            entry = _get_reviewer_entry(batch, key, reviewer)
            if entry:
                verdict = entry.get("verdict", "").upper()
                if verdict in ("REJECT", "P0"):
                    has_p0 = True
                elif verdict == "P1":
                    has_p1 = True
                elif verdict == "P2":
                    has_p2 = True

        if all_submitted:
            # まだパスが残っているレビュアー → 自己遷移
            still_npass: list[str] = []
            for reviewer in npass_targets:
                entry = _get_reviewer_entry(batch, key, reviewer)
                if entry and entry.get("pass", 0) < entry.get("target_pass", 1):
                    still_npass.append(reviewer)

            if still_npass:
                return TransitionAction(
                    new_state=state,
                    send_review=True,
                    npass_target_reviewers=still_npass,
                )

            # 全パス完了 → 全レビュアー（n_pass==1 含む）の verdict で遷移先を決定
            _, all_has_p0, all_has_p1, all_has_p2 = count_reviews(batch, key)
            comment = data.get("comment", "") if data else ""
            return _resolve_review_outcome(
                state, data, batch, all_has_p0, all_has_p1, all_has_p2, comment=comment,
            )

        # 未提出あり — 提出済み verdict に P0/P1 があれば即 REVISE（タイムアウト待ち不要）
        if has_p0 or has_p1:
            comment = data.get("comment", "") if data else ""
            return _resolve_review_outcome(
                state, data, batch, has_p0, has_p1, has_p2, comment=comment,
            )

        # タイムアウト判定（NPASS は BLOCKED にならない）
        base_state = state.replace("_NPASS", "")
        block_sec = BLOCK_TIMERS.get(base_state, 3600)
        if data:
            block_sec += _get_timeout_extension(data)

        elapsed = (_datetime.now(LOCAL_TZ) - entered_at).total_seconds()
        if elapsed >= block_sec:
            pj = data.get("project", "") if data else ""
            comment = data.get("comment", "") if data else ""
            log(f"[NPASS] timeout ({elapsed:.0f}s >= {block_sec}s), using current verdicts for {pj}")
            # タイムアウト: 全レビュアーの現在の verdict で判定
            # 未完了レビュアーは pass 1 verdict（上書きされていない）、
            # 完了済みは最終パス verdict、n_pass==1 は pass 1 verdict
            _, to_has_p0, to_has_p1, to_has_p2 = count_reviews(batch, key)
            return _resolve_review_outcome(
                state, data, batch, to_has_p0, to_has_p1, to_has_p2, comment=comment,
            )

        return TransitionAction()

    if state == "DESIGN_APPROVED":
        skip_assess = data.get("skip_assess", False) if data else False
        if skip_assess:
            no_cc = data.get("no_cc", False) if data else False
            return TransitionAction(
                new_state="IMPLEMENTATION",
                run_cc=not no_cc,
                reset_reviewers=True,
            )
        pj = data.get("project", "") if data else ""
        comment = data.get("comment", "") if data else ""
        notif = get_notification_for_state(
            "ASSESSMENT", project=pj, batch=batch, comment=comment,
            repo_path=data.get("repo_path", "") if data else "",
        )
        return TransitionAction(
            new_state="ASSESSMENT",
            impl_msg=notif.impl_msg,
        )

    if state == "ASSESSMENT":
        batch = batch or []
        # 全 Issue が assessment 済みかチェック
        all_assessed = batch and all(issue.get("assessment") for issue in batch)
        if all_assessed:
            # domain_risk の値域チェック + 未知値の正規化
            valid_risks = {"n/a", "none", "low", "high"}
            for issue in batch:
                a = issue.get("assessment", {})
                ir = a.get("domain_risk", "n/a")
                if ir not in valid_risks:
                    log(f"[ASSESSMENT] WARNING: unknown domain_risk={ir!r} in #{issue.get('issue')}, normalizing to n/a")
                    a["domain_risk"] = "n/a"

            exclude_any = data.get("exclude_any_risk", False) if data else False
            exclude_high = data.get("exclude_high_risk", False) if data else False
            no_cc = data.get("no_cc", False) if data else False

            # Issue ごとに除外判定
            remaining = []
            skipped = []
            for issue in batch:
                risk = issue.get("assessment", {}).get("domain_risk", "n/a")
                should_skip = False
                if exclude_any and risk not in ("none", "n/a"):
                    should_skip = True
                elif exclude_high and risk == "high":
                    should_skip = True

                if should_skip:
                    skipped.append(issue)
                else:
                    remaining.append(issue)

            if not remaining:
                # 全 Issue が除外 → IDLE に戻す（既存動作）
                return TransitionAction(new_state="IDLE")

            if skipped:
                # 一部除外: remaining と skipped を TransitionAction 経由で watchdog に伝える
                return TransitionAction(
                    new_state="IMPLEMENTATION",
                    run_cc=not no_cc,
                    reset_reviewers=True,
                    skipped_issues=skipped,
                    remaining_issues=remaining,
                )

            # 除外なし: 全 Issue で IMPLEMENTATION へ
            return TransitionAction(
                new_state="IMPLEMENTATION",
                run_cc=not no_cc,
                reset_reviewers=True,
            )
        # 未判定 Issue がある → ASSESSMENT 継続（遷移しない）
        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    if state == "CODE_APPROVED":
        return TransitionAction(
            new_state="MERGE_SUMMARY_SENT",
            send_merge_summary=True,
        )

    if state in ("DESIGN_REVISE", "CODE_REVISE"):
        review_key = "design_reviews" if "DESIGN" in state else "code_reviews"
        revised_key = "design_revised" if "DESIGN" in state else "code_revised"
        review_state = "DESIGN_REVIEW" if "DESIGN" in state else "CODE_REVIEW"
        p2_fix = data.get("p2_fix", False) if data else False

        flag_phase = STATE_PHASE_MAP.get(state)
        all_done = True
        for issue in batch:
            reviews = issue.get(review_key, {})

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

            has_unresolved_flag = False
            if flag_phase is not None:
                has_unresolved_flag = any(
                    f.get("verdict") == "P0"
                    and not f.get("resolved")
                    and f.get("phase") == flag_phase
                    for f in issue.get("flags", [])
                )

            needs_revision = has_p0 or has_p1 or (p2_fix and has_p2) or has_unresolved_flag
            if needs_revision and not issue.get(revised_key):
                all_done = False
                break

        if all_done:
            if "CODE" in state:
                from config import TEST_CONFIG
                project = data.get("project", "") if data else ""
                has_test = bool(TEST_CONFIG.get(project, {}).get("test_command"))
                skip_test = data.get("skip_test", False) if data else False
                if has_test and not skip_test:
                    log(
                        f"[REVISE] all issues with P0/P1{'/P2' if p2_fix else ''} revised, transitioning to CODE_TEST"
                    )
                    return TransitionAction(new_state="CODE_TEST", run_test=True)
            log(
                f"[REVISE] all issues with P0/P1{'/P2' if p2_fix else ''} revised, transitioning to {review_state}"
            )
            return TransitionAction(new_state=review_state, send_review=True)

        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    if state == "IMPLEMENTATION":
        # 1. 完了判定（最優先）
        if all(i.get("commit") for i in batch):
            from config import TEST_CONFIG
            project = data.get("project", "") if data else ""
            has_test = bool(TEST_CONFIG.get(project, {}).get("test_command"))
            skip_test = data.get("skip_test", False) if data else False
            if has_test and not skip_test:
                return TransitionAction(new_state="CODE_TEST", run_test=True)
            else:
                return TransitionAction(new_state="CODE_REVIEW", send_review=True)
        # 2. no_cc モード: CC を起動しない。実装者の手動 commit を待つ
        no_cc = data.get("no_cc", False) if data else False
        if no_cc:
            # no_cc ではタイムアウトを無効化。手動実装に BLOCK_TIMERS は適用しない
            return TransitionAction()
        # 3. CC未実行 → 起動指示
        if data is not None and not _is_cc_running(data):
            return TransitionAction(run_cc=True)
        # 4. CC実行中だが進捗なし → タイムアウト判定
        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    if state == "CODE_TEST":
        if data is None:
            return TransitionAction()
        test_result = data.get("test_result")
        if test_result is None:
            block_sec = BLOCK_TIMERS.get("CODE_TEST", 600)
            block_sec += _get_timeout_extension(data)
            entered_at = _get_state_entered_at(data, "CODE_TEST")
            if entered_at is not None:
                elapsed = (_datetime.now(LOCAL_TZ) - entered_at).total_seconds()
                if elapsed >= block_sec:
                    return TransitionAction(
                        new_state="BLOCKED",
                        impl_msg="CODE_TEST タイムアウト。テストプロセスが応答しませんでした。",
                    )
            return TransitionAction()
        if test_result == "pass":
            return TransitionAction(new_state="CODE_REVIEW", send_review=True)
        # test_result == "fail"
        from config import MAX_TEST_RETRY
        retry_count = data.get("test_retry_count", 0)
        if retry_count >= MAX_TEST_RETRY:
            return TransitionAction(
                new_state="BLOCKED",
                impl_msg=f"テスト {retry_count} 回連続失敗。自動修復不能。",
            )
        return TransitionAction(new_state="CODE_TEST_FIX")

    if state == "CODE_TEST_FIX":
        if data is None:
            return TransitionAction()
        cc_pid = data.get("cc_pid")
        if cc_pid is not None and not _is_cc_running(data):
            return TransitionAction(new_state="CODE_TEST", run_test=True)
        nudge = _check_nudge(state, data) if data is not None else None
        return nudge or TransitionAction()

    return TransitionAction()


def _format_nudge_message(state: str, project: str, batch: list) -> str:
    """催促メッセージ生成。get_notification_for_state() に委譲。"""
    notif = get_notification_for_state(state, project=project, batch=batch)
    return notif.impl_msg or f"[gokrax] {project}: {state} — 対応してください。"


def _recover_pending_notifications(pj: str, pending: dict) -> None:
    """未完了通知のリカバリ(Issue #59)。impl/review は再送、merge_summary/run_cc は Discord警告。

    At-least-once 保証: 通知成功時のみ pending をクリアする。
    失敗時は pending を維持し、次回 process() で再試行する。
    """
    if "impl" in pending:
        info = pending["impl"]
        try:
            notify_implementer(info["implementer"], info["msg"])
            clear_pending_notification(pj, "impl")
        except Exception as e:
            log(f"[{pj}] WARNING: impl recovery failed, will retry next cycle: {e}")

    if "review" in pending:
        info = pending["review"]
        try:
            fresh_data = load_pipeline(get_path(pj))
            excluded = fresh_data.get("excluded_reviewers", [])
            comment = fresh_data.get("comment", "")
            notify_reviewers(
                pj, info["new_state"], info["batch"], info["gitlab"],
                repo_path=info.get("repo_path", ""),
                review_mode=info.get("review_mode", "standard"),
                excluded=excluded,
                base_commit=info.get("base_commit"),
                comment=comment,
            )
            clear_pending_notification(pj, "review")
        except Exception as e:
            log(f"[{pj}] WARNING: review recovery failed, will retry next cycle: {e}")

    if "merge_summary" in pending:
        try:
            notify_discord(render("dev.blocked", "notify_recovery_merge_summary", project=pj))
            clear_pending_notification(pj, "merge_summary")
        except Exception as e:
            log(f"[{pj}] WARNING: merge_summary recovery warning failed, will retry: {e}")

    if "run_cc" in pending:
        try:
            notify_discord(render("dev.blocked", "notify_recovery_cc", project=pj))
            clear_pending_notification(pj, "run_cc")
        except Exception as e:
            log(f"[{pj}] WARNING: run_cc recovery warning failed, will retry: {e}")
