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


def test_running_autopull_false_shows_no_autopull():
    """running セクションで autopull=False のとき no-autopull が表示される (Issue #365)"""
    running = {
        "project": "TestPJ",
        "issues": "#1",
        "state": "DESIGN_PLAN",
        "review_mode": "",
        "automerge": True,
        "autopull": False,
    }
    result = get_qstatus_text([], running=running)
    assert "no-autopull" in result


def test_running_autopull_true_shows_autopull():
    """running セクションで autopull=True のとき autopull が表示される (Issue #365)"""
    running = {
        "project": "TestPJ",
        "issues": "#1",
        "state": "DESIGN_PLAN",
        "review_mode": "",
        "automerge": True,
        "autopull": True,
    }
    result = get_qstatus_text([], running=running)
    assert "autopull" in result
    assert "no-autopull" not in result


def test_running_autopull_unset_hidden():
    """running セクションで autopull 未設定のとき表示されない (Issue #365)"""
    running = {
        "project": "TestPJ",
        "issues": "#1",
        "state": "DESIGN_PLAN",
        "review_mode": "",
        "automerge": True,
    }
    result = get_qstatus_text([], running=running)
    assert "autopull" not in result


def test_entries_no_autopull_shown_when_explicit():
    """entries セクションで明示 no-autopull が表示される (Issue #365)"""
    entries = [{
        "project": "TestPJ",
        "issues": "1",
        "index": 1,
        "autopull": False,
        "_explicit_keys": {"autopull"},
    }]
    result = get_qstatus_text(entries)
    assert "no-autopull" in result


def test_entries_autopull_shown_when_explicit():
    """entries セクションで明示 autopull が表示される (Issue #365)"""
    entries = [{
        "project": "TestPJ",
        "issues": "1",
        "index": 1,
        "autopull": True,
        "_explicit_keys": {"autopull"},
    }]
    result = get_qstatus_text(entries)
    assert "autopull" in result
    assert "no-autopull" not in result


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


# ---------------------------------------------------------------------------
# Issue #400: boost label
# ---------------------------------------------------------------------------

def _running(**kw) -> dict:
    return {"project": "TestPJ", "issues": "#1", "state": "DESIGN_REVIEW",
            "review_mode": "lite", "automerge": True, **kw}


def test_running_boost_design_label():
    result = get_qstatus_text([], running=_running(boost=True, boost_phases=["design"]))
    assert result.endswith(" boost[design]")


def test_running_boost_no_phases_label():
    result = get_qstatus_text([], running=_running(boost=True, boost_phases=[]))
    assert result.endswith(" boost[-]")


def test_running_no_boost_unchanged():
    base = get_qstatus_text([], running=_running())
    assert get_qstatus_text([], running=_running(boost=False, boost_phases=[])) == base
    assert "boost" not in base


def test_entries_boost_label_from_mode(monkeypatch):
    import config
    monkeypatch.setattr(config, "REVIEW_MODES",
                        {"bm": {"members": ["reviewer1"], "design": {"boost": True}}})
    entries = [{"project": "TestPJ", "issues": "1", "index": 1, "mode": "bm", "boost": True}]
    assert get_qstatus_text(entries) == "[1] TestPJ 1 bm boost[design]"


def test_entries_boost_label_unknown_mode(monkeypatch):
    monkeypatch.setattr("pipeline_io.load_pipeline", lambda path: {})
    monkeypatch.setattr("commands.dev.queue.get_path", lambda pj: Path("/nonexistent.json"))
    entries = [{"project": "TestPJ", "issues": "1", "index": 1, "boost": True}]
    assert get_qstatus_text(entries) == "[1] TestPJ 1 boost[?]"


def test_entries_no_boost_unchanged():
    entries = [{"project": "TestPJ", "issues": "1", "index": 1, "boost": False}]
    assert get_qstatus_text(entries) == "[1] TestPJ 1"


def test_get_running_info_boost(tmp_path, monkeypatch):
    import json
    import config
    from commands.dev.queue import _get_running_info
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
    rc = {p: {"members": ["reviewer1"], "min_reviews": 1, "n_pass": {},
              "grace_period_sec": 0, "boost": p == "code"} for p in ("design", "code")}
    (tmp_path / "pj.json").write_text(json.dumps({
        "project": "pj", "state": "CODE_REVIEW", "batch": [{"issue": 1}],
        "review_config": rc, "boost": True,
    }))
    info = _get_running_info()
    assert info["boost"] is True
    assert info["boost_phases"] == ["code"]
