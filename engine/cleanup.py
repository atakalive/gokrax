"""engine/cleanup.py — バッチ状態クリーンアップの共通関数。

CLI (_reset_to_idle) と watchdog (DONE / ASSESSMENT→IDLE) の
3経路で同一のクリーンアップを保証する。
"""


def _cleanup_batch_state(data: dict, pj: str) -> None:
    """data からバッチ関連フィールドを全てクリアし、リソースを解放する。

    Args:
        data: pipeline dict（直接変更される）。
        pj: プロジェクト名。

    state, history, project, spec_mode, spec_config は変更しない。
    """
    # --- リソース解放（pop より先に実行）---
    from engine.cc import _kill_pytest_baseline, _kill_code_test
    from engine.reviewer import _cleanup_review_files
    from notify import cleanup_npass_files

    _kill_pytest_baseline(data, pj)
    _kill_code_test(data, pj)
    _cleanup_review_files(pj)
    cleanup_npass_files(pj)

    # --- 状態クリア ---
    data["batch"] = []
    data["enabled"] = False

    # REVISE counters
    data.pop("design_revise_count", None)
    data.pop("code_revise_count", None)
    data.pop("max_design_revise_cycles", None)
    data.pop("max_code_revise_cycles", None)
    # Queue options
    data.pop("automerge", None)
    data.pop("p2_fix", None)
    data.pop("cc_plan_model", None)
    data.pop("cc_impl_model", None)
    data.pop("keep_context", None)
    data.pop("keep_ctx_batch", None)
    data.pop("keep_ctx_intra", None)
    data.pop("comment", None)
    data.pop("skip_cc_plan", None)
    data.pop("skip_test", None)
    data.pop("skip_assess", None)
    data.pop("skip_design", None)
    data.pop("no_cc", None)
    data.pop("exclude_high_risk", None)
    data.pop("exclude_any_risk", None)
    data.pop("allow_closed", None)
    data.pop("assessment", None)
    # Timeout
    data.pop("timeout_extension", None)
    data.pop("extend_count", None)
    # Queue mode
    data.pop("queue_mode", None)
    # pytest baseline
    data.pop("test_baseline", None)
    data.pop("_pytest_baseline", None)
    # CODE_TEST
    data.pop("test_result", None)
    data.pop("test_output", None)
    data.pop("test_retry_count", None)
    # CC
    data.pop("cc_pid", None)
    data.pop("cc_session_id", None)
    # Base commit
    data.pop("base_commit", None)
    # Reviewer
    data.pop("excluded_reviewers", None)
    data.pop("min_reviews_override", None)
    data.pop("_transient_dispatch_warned", None)
    data.pop("review_config", None)
    data.pop("reviewer_number_map", None)
    # Merge summary
    data.pop("summary_message_id", None)
    data.pop("merge_approved", None)
    # Pending notifications
    data.pop("_pending_notifications", None)
    # State timer
    data.pop("_state_entered_at", None)
    # Previous reviews
    data.pop("_prev_design_reviews", None)
    data.pop("_prev_code_reviews", None)
    # NPASS
    data.pop("_npass_target_reviewers", None)
    # Nudge (static + dynamic keys)
    data.pop("_last_nudge_at", None)
    for k in [k for k in data if k.startswith(("_nudge_failed_", "_last_nudge_"))]:
        del data[k]
    for k in [k for k in data if k.endswith("_notify_count")]:
        del data[k]
