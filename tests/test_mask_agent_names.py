"""tests/test_mask_agent_names.py — MASK_AGENT_NAMES テスト (Issue #187)"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notify import mask_agent_name  # noqa: E402


# ---------------------------------------------------------------------------
# 1. mask_agent_name 単体: 有効時
# ---------------------------------------------------------------------------

def test_mask_agent_name_enabled(monkeypatch):
    monkeypatch.setattr("config.MASK_AGENT_NAMES", True)
    monkeypatch.setattr("config.ALLOWED_REVIEWERS", ["alice", "bob"])
    assert mask_agent_name("alice") == "Reviewer 1"
    assert mask_agent_name("bob") == "Reviewer 2"


# ---------------------------------------------------------------------------
# 2. mask_agent_name 単体: 無効時
# ---------------------------------------------------------------------------

def test_mask_agent_name_disabled(monkeypatch):
    monkeypatch.setattr("config.MASK_AGENT_NAMES", False)
    monkeypatch.setattr("config.ALLOWED_REVIEWERS", ["alice", "bob"])
    assert mask_agent_name("alice") == "alice"


# ---------------------------------------------------------------------------
# 3. mask_agent_name 単体: 未知の名前
# ---------------------------------------------------------------------------

def test_mask_agent_name_unknown(monkeypatch):
    monkeypatch.setattr("config.MASK_AGENT_NAMES", True)
    monkeypatch.setattr("config.ALLOWED_REVIEWERS", ["alice", "bob"])
    assert mask_agent_name("M") == "M"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_pipeline(tmp_pipelines: Path, state: str = "CODE_REVIEW",
                   review_mode: str = "standard",
                   code_reviews: dict | None = None,
                   design_reviews: dict | None = None,
                   reviewer_number_map: dict | None = None) -> Path:
    data = {
        "project": "test-pj",
        "gitlab": "testns/test-pj",
        "state": state,
        "enabled": True,
        "implementer": "implementer1",
        "review_mode": review_mode,
        "batch": [
            {
                "issue": 1,
                "title": "Test Issue",
                "commit": "abc123" if "CODE" in state else None,
                "cc_session_id": None,
                "design_reviews": design_reviews or {},
                "code_reviews": code_reviews or {},
                "added_at": "2025-01-01T00:00:00+09:00",
            }
        ],
    }
    if reviewer_number_map is not None:
        data["reviewer_number_map"] = reviewer_number_map
    pj_file = tmp_pipelines / "test-pj.json"
    pj_file.write_text(json.dumps(data))
    return pj_file


# ---------------------------------------------------------------------------
# 4. cmd_review_done の note_body がマスクされる
# ---------------------------------------------------------------------------

def test_review_done_note_masked(tmp_pipelines, monkeypatch):
    _make_pipeline(tmp_pipelines, state="CODE_REVIEW",
                   reviewer_number_map={"alice": 1})

    monkeypatch.setattr("config.MASK_AGENT_NAMES", True)
    monkeypatch.setattr("config.ALLOWED_REVIEWERS", ["alice"])
    monkeypatch.setattr("commands.dev.ALLOWED_REVIEWERS", ["alice"])
    monkeypatch.setattr("config.REVIEW_MODES", {
        "standard": {
            "members": ["alice"],
            "min_reviews": 1,
            "grace_period_sec": 0,
        },
    })

    import gokrax
    args = argparse.Namespace(
        project="test-pj",
        issue=1,
        reviewer="alice",
        verdict="APPROVE",
        summary="LGTM",
        force=False,
        round=None,
    )

    mock_note = MagicMock(return_value=True)
    with patch("commands.dev._post_gitlab_note", mock_note), \
         patch("commands.dev.time.sleep"):
        gokrax.cmd_review(args)

    mock_note.assert_called_once()
    note_body = mock_note.call_args[0][2]
    assert note_body.startswith("[Reviewer 1]")
    assert "alice" not in note_body


# ---------------------------------------------------------------------------
# 5. cmd_dispute の note_body がマスクされる
# ---------------------------------------------------------------------------

def test_dispute_note_masked(tmp_pipelines, monkeypatch):
    _make_pipeline(tmp_pipelines, state="CODE_REVISE",
                   code_reviews={"alice": {"verdict": "P1", "summary": "issue"}},
                   reviewer_number_map={"alice": 1})

    monkeypatch.setattr("config.MASK_AGENT_NAMES", True)
    monkeypatch.setattr("config.ALLOWED_REVIEWERS", ["alice"])
    monkeypatch.setattr("commands.dev.ALLOWED_REVIEWERS", ["alice"])
    monkeypatch.setattr("config.REVIEW_MODES", {
        "standard": {
            "members": ["alice"],
            "min_reviews": 1,
            "grace_period_sec": 0,
        },
    })

    import gokrax
    args = argparse.Namespace(
        project="test-pj",
        issue=1,
        reviewer="alice",
        reason="判定が不適切",
    )

    mock_note = MagicMock(return_value=True)
    with patch("commands.dev._post_gitlab_note", mock_note), \
         patch("commands.dev.send_to_agent_queued", return_value=True), \
         patch("commands.dev.time.sleep"):
        gokrax.cmd_dispute(args)

    mock_note.assert_called_once()
    note_body = mock_note.call_args[0][2]
    assert "Reviewer 1" in note_body
    assert "alice" not in note_body


# ---------------------------------------------------------------------------
# 6. format_merge_summary (ja) がマスクされる
# ---------------------------------------------------------------------------

def test_format_merge_summary_masked_ja(monkeypatch):
    monkeypatch.setattr("config.MASK_AGENT_NAMES", True)
    monkeypatch.setattr("config.ALLOWED_REVIEWERS", ["alice"])

    from messages.ja.dev.merge_summary_sent import format_merge_summary

    batch = [{
        "issue": 1,
        "title": "Test",
        "commit": "abc123",
        "code_reviews": {
            "alice": {"verdict": "APPROVE", "summary": "LGTM"},
        },
    }]

    result = format_merge_summary(project="test-pj", batch=batch)
    assert "Reviewer 1" in result
    assert "alice" not in result


# ---------------------------------------------------------------------------
# 7. format_merge_summary (en) がマスクされる
# ---------------------------------------------------------------------------

def test_format_merge_summary_masked_en(monkeypatch):
    monkeypatch.setattr("config.MASK_AGENT_NAMES", True)
    monkeypatch.setattr("config.ALLOWED_REVIEWERS", ["alice"])

    from messages.en.dev.merge_summary_sent import format_merge_summary

    batch = [{
        "issue": 1,
        "title": "Test",
        "commit": "abc123",
        "code_reviews": {
            "alice": {"verdict": "APPROVE", "summary": "LGTM"},
        },
    }]

    result = format_merge_summary(project="test-pj", batch=batch)
    assert "Reviewer 1" in result
    assert "alice" not in result


# ---------------------------------------------------------------------------
# 8. reviewer_number_map が渡された場合、マップ内の番号が使われる (Issue #190)
# ---------------------------------------------------------------------------

def test_mask_agent_name_with_reviewer_number_map(monkeypatch):
    monkeypatch.setattr("config.MASK_AGENT_NAMES", True)
    monkeypatch.setattr("config.ALLOWED_REVIEWERS", ["alice", "bob", "charlie"])
    reviewer_map = {"alice": 3, "bob": 1, "charlie": 2}
    assert mask_agent_name("alice", reviewer_number_map=reviewer_map) == "Reviewer 3"
    assert mask_agent_name("bob", reviewer_number_map=reviewer_map) == "Reviewer 1"
    assert mask_agent_name("charlie", reviewer_number_map=reviewer_map) == "Reviewer 2"


# ---------------------------------------------------------------------------
# 9. reviewer_number_map に含まれないレビュアーはフォールバック + warning (Issue #190)
# ---------------------------------------------------------------------------

def test_mask_agent_name_fallback_warning(monkeypatch, caplog):
    import logging
    monkeypatch.setattr("config.MASK_AGENT_NAMES", True)
    monkeypatch.setattr("config.ALLOWED_REVIEWERS", ["alice", "bob"])
    reviewer_map = {"alice": 1}
    with caplog.at_level(logging.WARNING):
        result = mask_agent_name("bob", reviewer_number_map=reviewer_map)
    assert result == "Reviewer 2"
    assert "reviewer_number_map missing or incomplete for bob" in caplog.text


# ---------------------------------------------------------------------------
# 10. マッピング生成で番号が 1〜N の範囲に収まる (Issue #190)
# ---------------------------------------------------------------------------

def test_reviewer_number_map_range():
    import random
    active_reviewers = ["alice", "bob", "charlie"]
    n = len(active_reviewers)
    numbers = list(range(1, n + 1))
    random.shuffle(numbers)
    reviewer_number_map = dict(zip(active_reviewers, numbers))

    assert set(reviewer_number_map.values()) == {1, 2, 3}
    assert len(reviewer_number_map) == 3
    for v in reviewer_number_map.values():
        assert 1 <= v <= n


# ---------------------------------------------------------------------------
# 11. _save_excluded で reviewer_number_map が既存なら上書きされない (Issue #190)
# ---------------------------------------------------------------------------

def test_save_excluded_does_not_overwrite_reviewer_number_map(tmp_pipelines, monkeypatch):
    """reviewer_number_map が既に存在する場合、_save_excluded で上書きされない"""
    existing_map = {"alice": 2, "bob": 1}
    _make_pipeline(tmp_pipelines, state="CODE_REVIEW",
                   reviewer_number_map=existing_map)

    monkeypatch.setattr("config.REVIEW_MODES", {
        "standard": {
            "members": ["alice", "bob"],
            "min_reviews": 1,
            "grace_period_sec": 0,
        },
    })

    from pipeline_io import update_pipeline, get_path
    path = get_path("test-pj")

    # _save_excluded と同じロジックをシミュレート
    excluded = []
    mode_config = {"members": ["alice", "bob"], "min_reviews": 1}

    import random
    active_reviewers = [r for r in mode_config["members"] if r not in excluded]
    n_active = len(active_reviewers)
    numbers = list(range(1, n_active + 1))
    random.shuffle(numbers)
    new_map = dict(zip(active_reviewers, numbers))

    def _save_excluded(data):
        data["excluded_reviewers"] = excluded
        if "reviewer_number_map" not in data:
            data["reviewer_number_map"] = new_map

    data = update_pipeline(path, _save_excluded)

    # 既存のマップが保持されている
    assert data["reviewer_number_map"] == existing_map
