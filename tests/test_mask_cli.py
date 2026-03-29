"""Tests for reviewer name masking in CLI output (#196)."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from notify import resolve_reviewer_arg
from commands.dev import _masked_reviewer


# ---------------------------------------------------------------------------
# 5a. resolve_reviewer_arg 単体テスト
# ---------------------------------------------------------------------------
class TestResolveReviewerArg:
    def test_number_resolves_to_name(self) -> None:
        rnm = {"reviewer5": 3, "reviewer6": 1}
        assert resolve_reviewer_arg("3", rnm) == "reviewer5"

    def test_invalid_number_raises(self) -> None:
        rnm = {"reviewer5": 3, "reviewer6": 1}
        with pytest.raises(SystemExit, match="Reviewer 99"):
            resolve_reviewer_arg("99", rnm)

    def test_name_passthrough(self) -> None:
        rnm = {"reviewer5": 3}
        assert resolve_reviewer_arg("reviewer5", rnm) == "reviewer5"

    def test_number_without_map_raises(self) -> None:
        with pytest.raises(SystemExit, match="batch start"):
            resolve_reviewer_arg("3", None)


# ---------------------------------------------------------------------------
# 5b. _masked_reviewer ヘルパーのテスト
# ---------------------------------------------------------------------------
class TestMaskedReviewer:
    def test_mask_with_map(self) -> None:
        assert _masked_reviewer("reviewer5", {"reviewer5": 3}) == "Reviewer 3"

    def test_mask_without_map(self) -> None:
        # reviewer_number_map=None → フォールバック動作（mask_agent_name に委譲）
        result = _masked_reviewer("reviewer5", None)
        # MASK_AGENT_NAMES=True のデフォルト設定では REVIEWERS index ベースの
        # フォールバックが使われる。具体的な番号は設定依存なので "Reviewer" を含むことだけ確認。
        assert "Reviewer" in result


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _write_pipeline(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# 5c. cmd_review の print マスク検証
# ---------------------------------------------------------------------------
class TestCmdReviewMask:
    def test_print_uses_masked_name(self, tmp_pipelines, capsys, monkeypatch) -> None:
        monkeypatch.setattr("commands.dev.REVIEWERS", ["reviewer5", "reviewer6"])
        pipeline = tmp_pipelines / "test-pj.json"
        _write_pipeline(pipeline, {
            "project": "test-pj",
            "gitlab": "testns/test-pj",
            "state": "DESIGN_REVIEW",
            "enabled": True,
            "implementer": "implementer1",
            "review_mode": "standard",
            "reviewer_number_map": {"reviewer5": 3, "reviewer6": 1},
            "batch": [
                {
                    "issue": 1,
                    "title": "Test",
                    "commit": None,
                    "cc_session_id": None,
                    "design_reviews": {},
                    "code_reviews": {},
                    "added_at": "2025-01-01T00:00:00+09:00",
                },
            ],
            "history": [],
            "created_at": "2025-01-01T00:00:00+09:00",
            "updated_at": "2025-01-01T00:00:00+09:00",
        })
        args = argparse.Namespace(
            project="test-pj", issue=1, reviewer="3",
            verdict="APPROVE", summary="ok", force=False, round=None,
        )
        with patch("commands.dev._post_gitlab_note", return_value=True), \
             patch("pipeline_io.append_metric"):
            from commands.dev import cmd_review
            cmd_review(args)

        out = capsys.readouterr().out
        assert "reviewer5" not in out
        assert "Reviewer 3" in out


# ---------------------------------------------------------------------------
# 5d. cmd_dispute の print マスク検証
# ---------------------------------------------------------------------------
class TestCmdDisputeMask:
    def test_print_uses_masked_name(self, tmp_pipelines, capsys, monkeypatch) -> None:
        monkeypatch.setattr("commands.dev.REVIEWERS", ["reviewer1", "reviewer6"])
        pipeline = tmp_pipelines / "test-pj.json"
        _write_pipeline(pipeline, {
            "project": "test-pj",
            "gitlab": "testns/test-pj",
            "state": "DESIGN_REVISE",
            "enabled": True,
            "implementer": "implementer1",
            "review_mode": "standard",
            "reviewer_number_map": {"reviewer1": 2, "reviewer6": 1},
            "batch": [
                {
                    "issue": 1,
                    "title": "Test",
                    "commit": None,
                    "cc_session_id": None,
                    "design_reviews": {
                        "reviewer1": {
                            "verdict": "P0",
                            "at": "2025-01-01T00:00:00+09:00",
                            "summary": "bad",
                        },
                    },
                    "code_reviews": {},
                    "added_at": "2025-01-01T00:00:00+09:00",
                },
            ],
            "history": [],
            "created_at": "2025-01-01T00:00:00+09:00",
            "updated_at": "2025-01-01T00:00:00+09:00",
        })
        args = argparse.Namespace(
            project="test-pj", issue=1, reviewer="2",
            reason="理由テスト",
        )
        with patch("commands.dev._post_gitlab_note", return_value=True), \
             patch("commands.dev.send_to_agent_queued", return_value=True):
            from commands.dev import cmd_dispute
            cmd_dispute(args)

        out = capsys.readouterr().out
        assert "reviewer1" not in out


# ---------------------------------------------------------------------------
# 5e. cmd_exclude の番号解決検証
# ---------------------------------------------------------------------------
class TestCmdExcludeMask:
    def test_number_resolves_for_add(self, tmp_pipelines, monkeypatch) -> None:
        monkeypatch.setattr("commands.dev.REVIEWERS", ["reviewer5"])
        pipeline = tmp_pipelines / "test-pj.json"
        _write_pipeline(pipeline, {
            "project": "test-pj",
            "gitlab": "testns/test-pj",
            "state": "IDLE",
            "enabled": True,
            "review_mode": "standard",
            "excluded_reviewers": [],
            "reviewer_number_map": {"reviewer5": 3},
        })
        args = argparse.Namespace(
            project="test-pj", add=["3"], remove=None, list=False,
        )
        from commands.dev import cmd_exclude
        cmd_exclude(args)

        data = _load(pipeline)
        assert "reviewer5" in data["excluded_reviewers"]
