"""Tests for get_qstatus_text() [*] running entry options display."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from commands.dev import get_qstatus_text  # noqa: E402


def test_running_shows_all_options():
    running = {
        "project": "TestPJ",
        "issues": "#1,#2",
        "state": "CODE_REVIEW",
        "review_mode": "lite",
        "automerge": False,
        "p2_fix": True,
        "cc_plan_model": "opus",
        "cc_impl_model": "sonnet",
        "keep_ctx_batch": True,
        "keep_ctx_intra": True,
        "skip_cc_plan": True,
        "skip_test": True,
        "skip_assess": True,
        "skip_design": True,
        "no_cc": True,
        "exclude_high_risk": True,
        "exclude_any_risk": True,
        "allow_closed": True,
    }
    result = get_qstatus_text([], running=running)
    assert "[*]" in result
    assert "no-automerge" in result
    assert "p2-fix" in result
    assert "plan=opus" in result
    assert "impl=sonnet" in result
    assert "keep-ctx-all" in result
    assert "skip-cc-plan" in result
    assert "skip-test" in result
    assert "skip-assess" in result
    assert "skip-design" in result
    assert "no-cc" in result
    assert "exclude-high-risk" in result
    assert "exclude-any-risk" in result
    assert "allow-closed" in result


def test_running_automerge_true_hides_flag():
    running = {
        "project": "TestPJ",
        "issues": "#1",
        "state": "DESIGN_PLAN",
        "review_mode": "",
        "automerge": True,
    }
    result = get_qstatus_text([], running=running)
    assert "no-automerge" not in result


def test_running_default_automerge_absent():
    running = {
        "project": "TestPJ",
        "issues": "#1",
        "state": "IMPLEMENTATION",
        "review_mode": "full",
    }
    result = get_qstatus_text([], running=running)
    assert "no-automerge" in result


def test_running_keep_ctx_batch_only():
    running = {
        "project": "TestPJ",
        "issues": "#1",
        "state": "IMPLEMENTATION",
        "review_mode": "",
        "keep_ctx_batch": True,
        "keep_ctx_intra": False,
    }
    result = get_qstatus_text([], running=running)
    assert "keep-ctx-batch" in result
    assert "keep-ctx-all" not in result
