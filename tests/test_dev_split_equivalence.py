"""Equivalence tests: commands._dev must expose the same 41 names as commands.dev.

This is a migration safety net for phase 1 of the dev.py split.
Deleted in phase 2 when commands/dev.py is removed.
"""

import inspect

import commands.dev as old_mod
import commands._dev as new_mod


# 41 names that commands._dev must re-export
EXPECTED_NAMES = {
    # helpers (6)
    "VERDICT_SEVERITY", "RISK_DISPLAY",
    "parse_issue_args", "_log", "_masked_reviewer", "_reset_to_idle",
    # lifecycle (18)
    "get_status_text", "cmd_status", "cmd_init", "cmd_enable", "cmd_disable", "cmd_extend",
    "_fetch_open_issues", "_fetch_issue_info", "cmd_triage",
    "cmd_start", "cmd_transition", "cmd_reset",
    "cmd_review_mode", "cmd_exclude", "cmd_merge_summary", "cmd_ok",
    "cmd_get_comments", "cmd_blocked_report",
    # review (10)
    "_update_issue_title_with_assessment",
    "cmd_review", "cmd_dispute", "cmd_flag",
    "cmd_commit", "cmd_cc_start", "cmd_plan_done", "cmd_assess_done",
    "cmd_design_revise", "cmd_code_revise",
    # queue (7)
    "cmd_qrun", "_get_running_info", "get_qstatus_text",
    "cmd_qstatus", "cmd_qadd", "cmd_qdel", "cmd_qedit",
}

# 33 names imported by gokrax.py (the public API contract)
GOKRAX_PUBLIC_NAMES = {
    "cmd_status", "cmd_init", "cmd_enable", "cmd_disable", "cmd_extend",
    "cmd_triage", "cmd_start", "cmd_transition", "cmd_reset",
    "cmd_review", "cmd_dispute", "cmd_flag", "cmd_commit",
    "cmd_cc_start", "cmd_plan_done", "cmd_assess_done", "cmd_design_revise", "cmd_code_revise",
    "cmd_review_mode", "cmd_exclude", "cmd_merge_summary", "cmd_ok",
    "cmd_qrun", "cmd_qstatus", "cmd_qadd", "cmd_qdel", "cmd_qedit",
    "cmd_get_comments", "cmd_blocked_report",
    "get_status_text", "get_qstatus_text", "_get_running_info",
    "_reset_to_idle",
}


def test_name_set_matches():
    """commands._dev exports exactly the expected 41 names (no more, no less)."""
    actual = {
        n for n in dir(new_mod)
        if not n.startswith("__") and not inspect.ismodule(getattr(new_mod, n))
    }
    assert actual == EXPECTED_NAMES


def test_public_api_subset():
    """Every name in gokrax.py's 33-name import list exists in commands._dev."""
    actual = {
        n for n in dir(new_mod)
        if not n.startswith("__") and not inspect.ismodule(getattr(new_mod, n))
    }
    missing = GOKRAX_PUBLIC_NAMES - actual
    assert not missing, f"Missing from commands._dev: {missing}"


def test_functions_have_matching_signatures():
    """All callable names in the expected set have identical signatures."""
    for name in sorted(EXPECTED_NAMES):
        old_obj = getattr(old_mod, name)
        new_obj = getattr(new_mod, name)
        if callable(old_obj):
            old_sig = inspect.signature(old_obj)
            new_sig = inspect.signature(new_obj)
            assert old_sig == new_sig, (
                f"{name}: signature mismatch\n  old: {old_sig}\n  new: {new_sig}"
            )


def test_constants_match():
    """Non-callable names (constants) have identical values."""
    for name in sorted(EXPECTED_NAMES):
        old_obj = getattr(old_mod, name)
        new_obj = getattr(new_mod, name)
        if not callable(old_obj):
            assert old_obj == new_obj, (
                f"{name}: value mismatch\n  old: {old_obj!r}\n  new: {new_obj!r}"
            )
