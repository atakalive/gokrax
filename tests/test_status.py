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
