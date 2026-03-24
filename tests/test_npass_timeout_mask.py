"""tests/test_npass_timeout_mask.py — NPASS timeout note のレビュアー名マスクテスト (Issue #207)"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JST = timezone(timedelta(hours=9))


def _write_pipeline(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _make_npass_pipeline(
    *,
    reviewer: str = "basho",
    reviewer_number_map: dict | None = None,
    review_mode: str = "npass-test",
) -> dict:
    """CODE_REVIEW_NPASS で timeout するパイプラインを構築。

    reviewer の pass=1, target_pass=2 で、state 進入時刻を2時間前に設定して
    timeout を確実に発火させる。
    """
    old_at = (datetime.now(JST) - timedelta(hours=2)).isoformat()
    data: dict = {
        "project": "test-pj",
        "gitlab": "testns/test-pj",
        "state": "CODE_REVIEW_NPASS",
        "enabled": True,
        "implementer": "implementer1",
        "review_mode": review_mode,
        "batch": [
            {
                "issue": 42,
                "title": "Test Issue",
                "commit": "abc123",
                "cc_session_id": None,
                "design_reviews": {},
                "code_reviews": {
                    reviewer: {
                        "verdict": "APPROVE",
                        "summary": "LGTM",
                        "pass": 1,
                        "target_pass": 2,
                        "at": old_at,
                    },
                },
                "added_at": "2025-01-01T00:00:00+09:00",
            }
        ],
        "_npass_target_reviewers": [reviewer],
        "history": [
            {"from": "CODE_REVIEW", "to": "CODE_REVIEW_NPASS", "at": old_at, "actor": "watchdog"},
        ],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    if reviewer_number_map is not None:
        data["reviewer_number_map"] = reviewer_number_map
    return data


# ---------------------------------------------------------------------------
# 1. MASK_AGENT_NAMES=True + reviewer_number_map で Reviewer N に変換される
# ---------------------------------------------------------------------------

def test_npass_timeout_note_masks_reviewer_name(tmp_pipelines, monkeypatch):
    """NPASS timeout note でレビュアー名が Reviewer N にマスクされる。"""
    from watchdog import process

    pipeline_data = _make_npass_pipeline(
        reviewer="basho",
        reviewer_number_map={"basho": 3},
    )
    pipeline_path = tmp_pipelines / "test-pj.json"
    _write_pipeline(pipeline_path, pipeline_data)

    monkeypatch.setattr("config.MASK_AGENT_NAMES", True)
    monkeypatch.setattr("config.REVIEWERS", ["basho"])
    monkeypatch.setattr("config.REVIEW_MODES", {
        "npass-test": {
            "members": ["basho"],
            "min_reviews": 1,
            "grace_period_sec": 0,
            "n_pass": {"basho": 2},
        },
        "standard": {
            "members": ["basho"],
            "min_reviews": 1,
            "grace_period_sec": 0,
        },
    })

    mock_note = MagicMock(return_value=True)
    with patch("notify.post_gitlab_note", mock_note):
        process(pipeline_path)

    assert mock_note.call_count >= 1
    # NPASS timeout note の body を検証
    bodies = [call[0][2] for call in mock_note.call_args_list
              if "NPASS timeout" in call[0][2]]
    assert len(bodies) == 1
    assert "Reviewer 3" in bodies[0]
    assert "basho" not in bodies[0]


# ---------------------------------------------------------------------------
# 2. MASK_AGENT_NAMES=False ではレビュアー実名が表示される
# ---------------------------------------------------------------------------

def test_npass_timeout_note_no_mask(tmp_pipelines, monkeypatch):
    """MASK_AGENT_NAMES=False の場合、レビュアー実名がそのまま表示される。"""
    from watchdog import process

    pipeline_data = _make_npass_pipeline(
        reviewer="basho",
        reviewer_number_map={"basho": 3},
    )
    pipeline_path = tmp_pipelines / "test-pj.json"
    _write_pipeline(pipeline_path, pipeline_data)

    monkeypatch.setattr("config.MASK_AGENT_NAMES", False)
    monkeypatch.setattr("config.REVIEWERS", ["basho"])
    monkeypatch.setattr("config.REVIEW_MODES", {
        "npass-test": {
            "members": ["basho"],
            "min_reviews": 1,
            "grace_period_sec": 0,
            "n_pass": {"basho": 2},
        },
        "standard": {
            "members": ["basho"],
            "min_reviews": 1,
            "grace_period_sec": 0,
        },
    })

    mock_note = MagicMock(return_value=True)
    with patch("notify.post_gitlab_note", mock_note):
        process(pipeline_path)

    assert mock_note.call_count >= 1
    bodies = [call[0][2] for call in mock_note.call_args_list
              if "NPASS timeout" in call[0][2]]
    assert len(bodies) == 1
    assert "basho" in bodies[0]


# ---------------------------------------------------------------------------
# 3. reviewer_number_map にキーが存在しない場合のフォールバック
# ---------------------------------------------------------------------------

def test_npass_timeout_note_reviewer_not_in_map(tmp_pipelines, monkeypatch):
    """reviewer_number_map={} でもクラッシュせず Reviewer N 形式で出力される。"""
    from watchdog import process

    pipeline_data = _make_npass_pipeline(
        reviewer="basho",
        reviewer_number_map={},
    )
    pipeline_path = tmp_pipelines / "test-pj.json"
    _write_pipeline(pipeline_path, pipeline_data)

    monkeypatch.setattr("config.MASK_AGENT_NAMES", True)
    monkeypatch.setattr("config.REVIEWERS", ["basho"])
    monkeypatch.setattr("config.REVIEW_MODES", {
        "npass-test": {
            "members": ["basho"],
            "min_reviews": 1,
            "grace_period_sec": 0,
            "n_pass": {"basho": 2},
        },
        "standard": {
            "members": ["basho"],
            "min_reviews": 1,
            "grace_period_sec": 0,
        },
    })

    mock_note = MagicMock(return_value=True)
    with patch("notify.post_gitlab_note", mock_note):
        process(pipeline_path)

    assert mock_note.call_count >= 1
    bodies = [call[0][2] for call in mock_note.call_args_list
              if "NPASS timeout" in call[0][2]]
    assert len(bodies) == 1
    # フォールバック: REVIEWERS リストのインデックス+1 → Reviewer 1
    assert "Reviewer 1" in bodies[0]
    assert "basho" not in bodies[0]
