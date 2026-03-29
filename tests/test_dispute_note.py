"""Test dispute GitLab note formatting."""

from unittest.mock import patch, MagicMock
import pytest


def test_dispute_note_format(tmp_path):
    """dispute note の Reason が太字ラベル + 段落分離されている。"""
    from commands.dev import cmd_dispute

    # pipeline JSON を用意
    import json
    pipeline = {
        "state": "DESIGN_REVISE",
        "batch": [
            {
                "issue": 99,
                "design_reviews": {
                    "reviewer_a": {"verdict": "P0", "summary": "bad", "at": "2026-01-01T00:00:00+09:00"}
                },
                "disputes": [],
            }
        ],
        "gitlab": "test/repo",
    }
    pj_path = tmp_path / "pipeline.json"
    pj_path.write_text(json.dumps(pipeline))

    args = MagicMock()
    args.project = "test"
    args.issue = 99
    args.reviewer = "reviewer_a"
    args.reason = "This is wrong because X.\nAlso Y is incorrect."

    posted_notes: list[str] = []

    with (
        patch("commands.dev.get_path", return_value=pj_path),
        patch("commands.dev.update_pipeline") as mock_update,
        patch("commands.dev._post_gitlab_note", side_effect=lambda g, i, body: (posted_notes.append(body), True)[-1]) as mock_note,
        patch("commands.dev.send_to_agent_queued", return_value=True),
        patch("commands.dev.find_issue", return_value=pipeline["batch"][0]),
        patch("commands.dev.mask_agent_name", return_value="Reviewer 1"),
    ):
        mock_update.return_value = pipeline
        cmd_dispute(args)

    assert len(posted_notes) == 1
    note = posted_notes[0]
    # ヘッダと Reason ラベルが段落分離されている
    assert "**Reason:**" in note
    # Reason ラベルと本文が別段落（\n\n で区切り）
    assert "**Reason:**\n\n" in note
    # 理由テキストが含まれている
    assert "This is wrong because X." in note
