"""Tests for get_status_text() — gokrax status command."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from commands.dev import get_status_text


def test_status_idle_no_review_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """IDLE pipeline with no review_mode/review_config does not crash."""
    import commands.dev
    monkeypatch.setattr(commands.dev, "PIPELINES_DIR", tmp_path)

    pipeline = {"project": "testpj", "state": "IDLE", "enabled": True, "batch": []}
    (tmp_path / "testpj.json").write_text(json.dumps(pipeline))

    result = get_status_text()

    assert "testpj" in result
    assert "IDLE" in result
    assert "ReviewerSize=" in result
    assert "Reviewers=[]" in result


def test_status_with_review_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pipeline with review_mode shows reviewer info correctly."""
    import commands.dev
    monkeypatch.setattr(commands.dev, "PIPELINES_DIR", tmp_path)

    from config import REVIEW_MODES

    pipeline = {
        "project": "testpj2",
        "state": "DESIGN_REVIEW",
        "enabled": True,
        "batch": [{"issue": 1, "design_reviews": {}}],
        "review_mode": "standard",
    }
    (tmp_path / "testpj2.json").write_text(json.dumps(pipeline))

    result = get_status_text()

    assert "testpj2" in result
    assert "DESIGN_REVIEW" in result
    assert "ReviewerSize=standard" in result
    # Should have at least one reviewer from REVIEW_MODES["standard"]
    members = REVIEW_MODES["standard"]["members"]
    for member in members:
        assert f'"{member}"' in result


def _per_issue_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("  #")]


def _write(tmp_path: Path, pipeline: dict) -> None:
    (tmp_path / f"{pipeline['project']}.json").write_text(json.dumps(pipeline))


def _base_pipeline(reviews: dict, *, excluded: list[str] | None = None,
                   min_reviews: int = 3) -> dict:
    pipeline = {
        "project": "pj",
        "state": "DESIGN_REVIEW",
        "enabled": True,
        "batch": [{"issue": 57, "design_reviews": reviews}],
        "review_mode": "custom",
        "review_config": {
            "design": {
                "members": ["alice", "bob", "carol"],
                "min_reviews": min_reviews,
                "n_pass": {},
                "grace_period_sec": 0,
            }
        },
    }
    if excluded is not None:
        pipeline["excluded_reviewers"] = excluded
        pipeline["min_reviews_override"] = min_reviews
    return pipeline


def test_status_per_issue_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import commands.dev
    monkeypatch.setattr(commands.dev, "PIPELINES_DIR", tmp_path)
    _write(tmp_path, _base_pipeline({"alice": {"verdict": "APPROVE"}}))
    lines = _per_issue_lines(get_status_text())
    assert len(lines) == 1
    line = lines[0]
    assert "APPROVE: alice" in line
    assert "WAITING: bob carol" in line
    assert "1/3 reviews" in line


def test_status_per_issue_all_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import commands.dev
    monkeypatch.setattr(commands.dev, "PIPELINES_DIR", tmp_path)
    _write(tmp_path, _base_pipeline({
        "alice": {"verdict": "APPROVE"},
        "bob": {"verdict": "APPROVE"},
        "carol": {"verdict": "APPROVE"},
    }))
    line = _per_issue_lines(get_status_text())[0]
    assert "APPROVE: alice bob carol" in line
    assert "WAITING" not in line


def test_status_per_issue_mixed_verdicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import commands.dev
    monkeypatch.setattr(commands.dev, "PIPELINES_DIR", tmp_path)
    _write(tmp_path, _base_pipeline({
        "alice": {"verdict": "APPROVE"},
        "bob": {"verdict": "P1"},
    }))
    line = _per_issue_lines(get_status_text())[0]
    assert "APPROVE: alice" in line
    assert "P1: bob" in line
    assert "WAITING: carol" in line


def test_status_per_issue_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import commands.dev
    monkeypatch.setattr(commands.dev, "PIPELINES_DIR", tmp_path)
    _write(tmp_path, _base_pipeline(
        {"alice": {"verdict": "APPROVE"}, "bob": {"verdict": "P1"}},
        excluded=["carol"],
        min_reviews=2,
    ))
    line = _per_issue_lines(get_status_text())[0]
    assert "carol" not in line
    assert "APPROVE: alice" in line
    assert "P1: bob" in line
    assert "2/2 reviews" in line


def _boost_rc(design: bool, code: bool) -> dict:
    return {
        "design": {"members": ["alice"], "min_reviews": 1, "n_pass": {},
                   "grace_period_sec": 0, "boost": design},
        "code": {"members": ["alice"], "min_reviews": 1, "n_pass": {},
                 "grace_period_sec": 0, "boost": code},
    }


def _status_line(text: str, pj: str) -> str:
    return next(ln for ln in text.splitlines() if f"] {pj}:" in ln)


def test_status_boost_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #400: boost batch shows boost[...] label at end of line."""
    import commands.dev
    monkeypatch.setattr(commands.dev, "PIPELINES_DIR", tmp_path)
    _write(tmp_path, {"project": "bp", "state": "DESIGN_PLAN", "enabled": True,
                      "batch": [{"issue": 400}], "review_mode": "custom",
                      "review_config": _boost_rc(True, False), "boost": True})
    line = _status_line(get_status_text(), "bp")
    assert line.endswith('Reviewers=["alice"]  boost[design]')


def test_status_no_boost_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import commands.dev
    monkeypatch.setattr(commands.dev, "PIPELINES_DIR", tmp_path)
    _write(tmp_path, {"project": "np", "state": "DESIGN_PLAN", "enabled": True,
                      "batch": [{"issue": 1}], "review_mode": "custom",
                      "review_config": _boost_rc(True, True)})
    line = _status_line(get_status_text(), "np")
    assert line == '[ON] np: DESIGN_PLAN  issues=[#1]  ReviewerSize=custom  Reviewers=["alice"]'
