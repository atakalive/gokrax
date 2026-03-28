"""tests/test_repo_path_in_messages.py — repo_path in agent messages (Issue #249)"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from engine.fsm import check_transition, get_notification_for_state


@pytest.fixture(autouse=True)
def _force_english(monkeypatch) -> None:
    """Force English templates for this test module."""
    monkeypatch.setattr("messages.PROMPT_LANG", "en")


class TestRepoPathInImplMessages:
    """get_notification_for_state() に repo_path を渡すと impl_msg に含まれる。"""

    def test_design_plan(self) -> None:
        action = get_notification_for_state(
            "DESIGN_PLAN", project="testpj",
            batch=[{"issue": 1}], repo_path="/mnt/test/repo",
        )
        assert action.impl_msg is not None
        assert "Repository: /mnt/test/repo" in action.impl_msg

    def test_design_revise(self) -> None:
        batch = [{"issue": 1, "title": "test",
                  "design_reviews": {"r1": {"verdict": "P1", "summary": "fix"}}}]
        action = get_notification_for_state(
            "DESIGN_REVISE", project="testpj",
            batch=batch, repo_path="/mnt/test/repo",
        )
        assert action.impl_msg is not None
        assert "Repository: /mnt/test/repo" in action.impl_msg

    def test_code_revise(self) -> None:
        batch = [{"issue": 1, "title": "test",
                  "code_reviews": {"r1": {"verdict": "P1", "summary": "fix"}}}]
        action = get_notification_for_state(
            "CODE_REVISE", project="testpj",
            batch=batch, repo_path="/mnt/test/repo",
        )
        assert action.impl_msg is not None
        assert "Repository: /mnt/test/repo" in action.impl_msg

    def test_assessment(self) -> None:
        action = get_notification_for_state(
            "ASSESSMENT", project="testpj",
            batch=[{"issue": 1}], repo_path="/mnt/test/repo",
        )
        assert action.impl_msg is not None
        assert "Repository: /mnt/test/repo" in action.impl_msg


class TestRepoPathInReviewRequest:
    """format_review_request() に repo_path を渡すとメッセージに含まれる。"""

    def test_design_review(self, monkeypatch) -> None:
        monkeypatch.setattr("notify.fetch_issue_body", lambda num, gitlab: "test body")
        from notify import format_review_request
        msg = format_review_request(
            "testpj", "DESIGN_REVIEW",
            [{"issue": 1, "title": "test"}],
            "gitlab/testpj",
            reviewer="r1", repo_path="/mnt/test/repo",
        )
        assert "Repository: /mnt/test/repo" in msg


class TestRepoPathEmpty:
    """repo_path が空の場合、Repository: 行は出力されない。"""

    def test_design_plan_empty(self) -> None:
        action = get_notification_for_state(
            "DESIGN_PLAN", project="testpj",
            batch=[{"issue": 1}], repo_path="",
        )
        assert action.impl_msg is not None
        assert "Repository:" not in action.impl_msg

    def test_design_revise_empty(self) -> None:
        batch = [{"issue": 1, "title": "test",
                  "design_reviews": {"r1": {"verdict": "P1", "summary": "fix"}}}]
        action = get_notification_for_state(
            "DESIGN_REVISE", project="testpj",
            batch=batch, repo_path="",
        )
        assert action.impl_msg is not None
        assert "Repository:" not in action.impl_msg

    def test_code_revise_empty(self) -> None:
        batch = [{"issue": 1, "title": "test",
                  "code_reviews": {"r1": {"verdict": "P1", "summary": "fix"}}}]
        action = get_notification_for_state(
            "CODE_REVISE", project="testpj",
            batch=batch, repo_path="",
        )
        assert action.impl_msg is not None
        assert "Repository:" not in action.impl_msg

    def test_assessment_empty(self) -> None:
        action = get_notification_for_state(
            "ASSESSMENT", project="testpj",
            batch=[{"issue": 1}], repo_path="",
        )
        assert action.impl_msg is not None
        assert "Repository:" not in action.impl_msg

    def test_review_request_empty(self, monkeypatch) -> None:
        monkeypatch.setattr("notify.fetch_issue_body", lambda num, gitlab: "test body")
        from notify import format_review_request
        msg = format_review_request(
            "testpj", "DESIGN_REVIEW",
            [{"issue": 1, "title": "test"}],
            "gitlab/testpj",
            reviewer="r1", repo_path="",
        )
        assert "Repository:" not in msg


class TestCheckTransitionRepoPath:
    """check_transition() が data の repo_path を impl_msg に伝播する。"""

    def test_initialize_to_design_plan(self) -> None:
        action = check_transition(
            "INITIALIZE",
            batch=[{"issue": 1}],
            data={"project": "testpj", "repo_path": "/mnt/test/repo"},
        )
        assert action.impl_msg is not None
        assert "Repository: /mnt/test/repo" in action.impl_msg
