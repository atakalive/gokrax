from __future__ import annotations

import copy
import json
from unittest.mock import patch

import pytest

from engine.fsm import TransitionAction, build_review_config, check_transition


def _mode_config(members: list[str], min_reviews: int, grace_period_sec: int) -> dict:
    return {
        "members": members,
        "min_reviews": min_reviews,
        "grace_period_sec": grace_period_sec,
        "n_pass": {},
    }


def test_check_transition_does_not_mutate_data_on_first_met(monkeypatch):
    monkeypatch.setattr("engine.fsm.REVIEW_MODES", {"standard": _mode_config(["r1", "r2", "r3"], 2, 300)})
    monkeypatch.setattr("engine.fsm.BLOCK_TIMERS", {})

    data = {
        "review_mode": "standard",
        "review_config": build_review_config(_mode_config(["r1", "r2", "r3"], 2, 300)),
    }
    batch = [{
        "issue": 1,
        "design_reviews": {
            "r1": {"verdict": "APPROVE", "at": "2026-03-26T00:00:00+09:00"},
            "r2": {"verdict": "APPROVE", "at": "2026-03-26T00:01:00+09:00"},
        },
    }]
    original = copy.deepcopy(data)

    with patch("engine.fsm.log"):
        action = check_transition("DESIGN_REVIEW", batch, data)

    assert data == original
    assert action.save_grace_met_at == "design_min_reviews_met_at"
    assert action.new_state is None


def test_check_transition_does_not_mutate_data_on_transition(monkeypatch):
    monkeypatch.setattr("engine.fsm.REVIEW_MODES", {"standard": _mode_config(["r1", "r2", "r3"], 2, 300)})
    monkeypatch.setattr("engine.fsm.BLOCK_TIMERS", {})

    data = {
        "review_mode": "standard",
        "review_config": build_review_config(_mode_config(["r1", "r2", "r3"], 2, 300)),
        "code_min_reviews_met_at": "2026-03-26T00:00:00+09:00",
    }
    batch = [{
        "issue": 1,
        "code_reviews": {
            "r1": {"verdict": "APPROVE", "at": "2026-03-26T00:00:00+09:00"},
            "r2": {"verdict": "APPROVE", "at": "2026-03-26T00:01:00+09:00"},
            "r3": {"verdict": "APPROVE", "at": "2026-03-26T00:02:00+09:00"},
        },
    }]
    original = copy.deepcopy(data)

    with patch("engine.fsm.log"):
        action = check_transition("CODE_REVIEW", batch, data)

    assert data == original
    assert action.clear_grace_met_at == "code_min_reviews_met_at"
    assert action.new_state is not None


def test_check_transition_returns_clear_grace_met_at_on_npass(monkeypatch):
    monkeypatch.setattr("engine.fsm.REVIEW_MODES", {"standard": _mode_config(["r1", "r2"], 2, 300)})
    monkeypatch.setattr("engine.fsm.BLOCK_TIMERS", {})

    data = {
        "review_mode": "standard",
        "review_config": build_review_config(_mode_config(["r1", "r2"], 2, 300)),
        "code_min_reviews_met_at": "2026-03-26T00:00:00+09:00",
        "code_revise_count": 0,
    }
    batch = [{
        "issue": 1,
        "code_reviews": {
            "r1": {"verdict": "APPROVE", "at": "2026-03-26T00:00:00+09:00", "pass": 1, "target_pass": 2},
            "r2": {"verdict": "APPROVE", "at": "2026-03-26T00:01:00+09:00", "pass": 1, "target_pass": 1},
        },
    }]
    original = copy.deepcopy(data)

    with patch("engine.fsm.log"):
        action = check_transition("CODE_REVIEW", batch, data)

    assert data == original
    assert action.clear_grace_met_at == "code_min_reviews_met_at"
    assert action.new_state == "CODE_REVIEW_NPASS"
    assert action.npass_target_reviewers


def test_no_clear_grace_met_at_when_grace_not_started(monkeypatch):
    monkeypatch.setattr("engine.fsm.REVIEW_MODES", {"standard": _mode_config(["r1", "r2"], 2, 0)})
    monkeypatch.setattr("engine.fsm.BLOCK_TIMERS", {})

    data = {
        "review_mode": "standard",
        "review_config": build_review_config(_mode_config(["r1", "r2"], 2, 0)),
    }
    batch = [{
        "issue": 1,
        "design_reviews": {
            "r1": {"verdict": "APPROVE", "at": "2026-03-26T00:00:00+09:00"},
            "r2": {"verdict": "APPROVE", "at": "2026-03-26T00:01:00+09:00"},
        },
    }]

    with patch("engine.fsm.log"):
        action = check_transition("DESIGN_REVIEW", batch, data)

    assert action.clear_grace_met_at is None
    assert action.new_state is not None


@pytest.mark.parametrize("case_id, state, data_extra, batch_extra, grace_sec, members, min_reviews", [
    (
        "grace_first_detection",
        "DESIGN_REVIEW",
        {},
        {"design_reviews": {
            "r1": {"verdict": "APPROVE", "at": "2026-03-26T00:00:00+09:00"},
            "r2": {"verdict": "APPROVE", "at": "2026-03-26T00:01:00+09:00"},
        }},
        300, ["r1", "r2", "r3"], 2,
    ),
    (
        "all_done_immediate",
        "DESIGN_REVIEW",
        {},
        {"design_reviews": {
            "r1": {"verdict": "APPROVE", "at": "2026-03-26T00:00:00+09:00"},
            "r2": {"verdict": "APPROVE", "at": "2026-03-26T00:01:00+09:00"},
        }},
        0, ["r1", "r2"], 2,
    ),
    (
        "grace_expired",
        "DESIGN_REVIEW",
        {"design_min_reviews_met_at": "2020-01-01T00:00:00+09:00"},
        {"design_reviews": {
            "r1": {"verdict": "APPROVE", "at": "2026-03-26T00:00:00+09:00"},
            "r2": {"verdict": "APPROVE", "at": "2026-03-26T00:01:00+09:00"},
        }},
        300, ["r1", "r2", "r3"], 2,
    ),
    (
        "npass_transition",
        "CODE_REVIEW",
        {"code_min_reviews_met_at": "2026-03-26T00:00:00+09:00", "code_revise_count": 0},
        {"code_reviews": {
            "r1": {"verdict": "APPROVE", "at": "2026-03-26T00:00:00+09:00", "pass": 1, "target_pass": 2},
            "r2": {"verdict": "APPROVE", "at": "2026-03-26T00:01:00+09:00", "pass": 1, "target_pass": 1},
        }},
        300, ["r1", "r2"], 2,
    ),
])
def test_save_and_new_state_mutually_exclusive(
    monkeypatch, case_id, state, data_extra, batch_extra, grace_sec, members, min_reviews,
):
    mode = _mode_config(members, min_reviews, grace_sec)
    monkeypatch.setattr("engine.fsm.REVIEW_MODES", {"standard": mode})
    monkeypatch.setattr("engine.fsm.BLOCK_TIMERS", {})

    data = {
        "review_mode": "standard",
        "review_config": build_review_config(mode),
    }
    data.update(data_extra)
    batch = [{"issue": 1, **batch_extra}]

    with patch("engine.fsm.log"):
        action = check_transition(state, batch, data)

    assert not (action.save_grace_met_at and action.new_state), (
        f"case={case_id}: save_grace_met_at={action.save_grace_met_at} and new_state={action.new_state} must not both be set"
    )


def _write_pipeline(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def test_do_transition_saves_grace_met_at(tmp_path, monkeypatch):
    import config
    import pipeline_io
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
    monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)

    path = tmp_path / "test-pj.json"
    data = {
        "project": "test-pj", "state": "DESIGN_REVIEW",
        "enabled": True, "batch": [{"issue": 1, "design_reviews": {}}],
        "history": [], "created_at": "", "updated_at": "",
    }
    _write_pipeline(path, data)

    mock_action = TransitionAction(save_grace_met_at="design_min_reviews_met_at")

    from watchdog import process

    def fake_update(p, cb):
        cb(data)
        return data

    with patch("watchdog.check_transition", return_value=mock_action), \
         patch("watchdog.update_pipeline", side_effect=fake_update):
        process(path)

    assert "design_min_reviews_met_at" in data
    val = data["design_min_reviews_met_at"]
    assert isinstance(val, str)
    # ISO 8601 形式であることを検証
    from datetime import datetime as _dt
    _dt.fromisoformat(val)  # パース失敗なら ValueError
    assert data["state"] == "DESIGN_REVIEW"


def test_do_transition_clears_grace_met_at_before_state_change(tmp_path, monkeypatch):
    import config
    import pipeline_io
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
    monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)

    path = tmp_path / "test-pj.json"
    mode = _mode_config(["r1", "r2"], 2, 300)
    data = {
        "project": "test-pj", "state": "DESIGN_REVIEW",
        "enabled": True,
        "batch": [{"issue": 1, "design_reviews": {
            "r1": {"verdict": "APPROVE", "at": "2026-03-26T00:00:00+09:00"},
            "r2": {"verdict": "APPROVE", "at": "2026-03-26T00:01:00+09:00"},
        }}],
        "review_config": build_review_config(mode),
        "design_min_reviews_met_at": "2026-03-26T00:00:00+09:00",
        "history": [], "created_at": "", "updated_at": "",
    }
    _write_pipeline(path, data)

    mock_action = TransitionAction(
        new_state="DESIGN_APPROVED",
        clear_grace_met_at="design_min_reviews_met_at",
    )

    from watchdog import process

    def fake_update(p, cb):
        cb(data)
        return data

    with patch("watchdog.check_transition", return_value=mock_action), \
         patch("watchdog.update_pipeline", side_effect=fake_update), \
         patch("watchdog.notify_discord"), \
         patch("watchdog.notify_reviewers"):
        process(path)

    assert "design_min_reviews_met_at" not in data
    assert data["state"] == "DESIGN_APPROVED"


def test_save_grace_met_at_rejects_unexpected_flags(tmp_path, monkeypatch):
    """save_grace_met_at と副作用フラグが同居した場合、ValueError で拒否されること。"""
    import config
    import pipeline_io
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
    monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)

    path = tmp_path / "test-pj.json"
    data = {
        "project": "test-pj", "state": "DESIGN_REVIEW",
        "enabled": True, "batch": [{"issue": 1, "design_reviews": {}}],
        "history": [], "created_at": "", "updated_at": "",
    }
    _write_pipeline(path, data)

    # save_grace_met_at と run_cc が同居する異常な TransitionAction
    mock_action = TransitionAction(save_grace_met_at="design_min_reviews_met_at", run_cc=True)

    from watchdog import process

    def fake_update(p, cb):
        cb(data)
        return data

    with pytest.raises(ValueError, match="save_grace_met_at conflicts with side-effect flags"):
        with patch("watchdog.check_transition", return_value=mock_action), \
             patch("watchdog.update_pipeline", side_effect=fake_update):
            process(path)
