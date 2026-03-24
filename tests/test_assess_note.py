"""tests/test_assess_note.py — ASSESSMENT結果 GitLab note 投稿テスト (Issue #186)"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _write_pipeline(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _make_issue(issue_num: int, assessment: dict | None = None) -> dict:
    item: dict = {
        "issue": issue_num,
        "title": f"Issue {issue_num}",
        "commit": None,
        "cc_session_id": None,
        "design_reviews": {},
        "code_reviews": {},
        "added_at": "2025-01-01T00:00:00+09:00",
    }
    if assessment is not None:
        item["assessment"] = assessment
    return item


def _base_pipeline(batch: list, **overrides) -> dict:
    data = {
        "project": "test-pj",
        "gitlab": "testns/test-pj",
        "state": "ASSESSMENT",
        "enabled": True,
        "implementer": "implementer1",
        "batch": batch,
        "history": [{"from": "DESIGN_APPROVED", "to": "ASSESSMENT",
                      "at": "2025-01-01T00:00:00+09:00", "actor": "watchdog"}],
    }
    data.update(overrides)
    return data


class TestAssessmentNoteOnImplementation:
    """ASSESSMENT → IMPLEMENTATION 遷移時の note 投稿テスト"""

    def test_notes_posted_for_all_issues(self, tmp_pipelines, monkeypatch):
        """batch に2件の Issue（assessment 付き）で遷移。各 Issue に note が投稿される"""
        from watchdog import process

        batch = [
            _make_issue(10, {"complex_level": 3, "domain_risk": "none", "summary": "Simple change"}),
            _make_issue(20, {"complex_level": 5, "domain_risk": "none", "summary": "Complex refactor"}),
        ]
        pipeline_path = tmp_pipelines / "test-pj.json"
        _write_pipeline(pipeline_path, _base_pipeline(batch))

        mock_note = MagicMock(return_value=True)
        monkeypatch.setattr("watchdog.notify_discord", MagicMock())
        monkeypatch.setattr("watchdog._start_cc", MagicMock())

        with patch("notify.post_gitlab_note", mock_note):
            process(pipeline_path)

        assert mock_note.call_count == 2
        # Issue #10
        call_10 = mock_note.call_args_list[0]
        assert call_10[0][1] == 10
        assert "Lvl 3" in call_10[0][2]
        assert "No Risk" in call_10[0][2]
        assert "Simple change" in call_10[0][2]
        # Issue #20
        call_20 = mock_note.call_args_list[1]
        assert call_20[0][1] == 20
        assert "Lvl 5" in call_20[0][2]
        assert "Complex refactor" in call_20[0][2]

    def test_risk_reason_included_for_low_risk(self, tmp_pipelines, monkeypatch):
        """domain_risk が low/high のとき risk_reason が note に含まれる"""
        from watchdog import process

        batch = [
            _make_issue(1, {
                "complex_level": 4,
                "domain_risk": "low",
                "risk_reason": "理由テスト",
                "summary": "概要",
            }),
        ]
        pipeline_path = tmp_pipelines / "test-pj.json"
        _write_pipeline(pipeline_path, _base_pipeline(batch))

        mock_note = MagicMock(return_value=True)
        monkeypatch.setattr("watchdog.notify_discord", MagicMock())
        monkeypatch.setattr("watchdog._start_cc", MagicMock())

        with patch("notify.post_gitlab_note", mock_note):
            process(pipeline_path)

        assert mock_note.call_count == 1
        body = mock_note.call_args[0][2]
        assert "Low Risk" in body
        assert "Risk reason: 理由テスト" in body
        assert "概要" in body

    def test_summary_empty_omits_second_line(self, tmp_pipelines, monkeypatch):
        """summary が空文字列のとき2行目が省略される"""
        from watchdog import process

        batch = [
            _make_issue(1, {"complex_level": 2, "domain_risk": "none", "summary": ""}),
        ]
        pipeline_path = tmp_pipelines / "test-pj.json"
        _write_pipeline(pipeline_path, _base_pipeline(batch))

        mock_note = MagicMock(return_value=True)
        monkeypatch.setattr("watchdog.notify_discord", MagicMock())
        monkeypatch.setattr("watchdog._start_cc", MagicMock())

        with patch("notify.post_gitlab_note", mock_note):
            process(pipeline_path)

        assert mock_note.call_count == 1
        body = mock_note.call_args[0][2]
        assert body == "[gokrax] Assessment: Lvl 2 / No Risk"

    def test_post_failure_does_not_block_transition(self, tmp_pipelines, monkeypatch):
        """post_gitlab_note 失敗時に遷移がブロックされない"""
        from watchdog import process

        batch = [
            _make_issue(1, {"complex_level": 3, "domain_risk": "none", "summary": "test"}),
        ]
        pipeline_path = tmp_pipelines / "test-pj.json"
        _write_pipeline(pipeline_path, _base_pipeline(batch))

        mock_note = MagicMock(return_value=False)
        monkeypatch.setattr("watchdog.notify_discord", MagicMock())
        monkeypatch.setattr("watchdog._start_cc", MagicMock())

        with patch("notify.post_gitlab_note", mock_note):
            process(pipeline_path)

        data = json.loads(pipeline_path.read_text())
        assert data["state"] == "IMPLEMENTATION"

    def test_missing_assessment_skipped(self, tmp_pipelines, monkeypatch):
        """assessment dict が欠落している Issue はスキップされる"""
        from watchdog import process

        batch = [
            _make_issue(1, {"complex_level": 3, "domain_risk": "none", "summary": "has assessment"}),
            _make_issue(2),  # assessment なし
        ]
        pipeline_path = tmp_pipelines / "test-pj.json"
        _write_pipeline(pipeline_path, _base_pipeline(batch))

        # assessment なし Issue があると fsm が遷移しないため check_transition をモック
        from engine.fsm import TransitionAction
        mock_action = TransitionAction(
            new_state="IMPLEMENTATION",
            run_cc=True,
            reset_reviewers=True,
        )
        mock_note = MagicMock(return_value=True)
        monkeypatch.setattr("watchdog.notify_discord", MagicMock())
        monkeypatch.setattr("watchdog._start_cc", MagicMock())
        monkeypatch.setattr("watchdog.check_transition", lambda *a, **kw: mock_action)

        with patch("notify.post_gitlab_note", mock_note):
            process(pipeline_path)

        # assessment 付き Issue #1 のみ note が投稿される
        assert mock_note.call_count == 1
        assert mock_note.call_args[0][1] == 1

    def test_unknown_domain_risk_fallback(self, tmp_pipelines, monkeypatch):
        """未知の domain_risk 値で "Unknown Risk" がフォールバックされる"""
        from watchdog import process

        batch = [
            _make_issue(1, {"complex_level": 3, "domain_risk": "critical", "summary": "test"}),
        ]
        pipeline_path = tmp_pipelines / "test-pj.json"
        _write_pipeline(pipeline_path, _base_pipeline(batch))

        from engine.fsm import TransitionAction
        mock_action = TransitionAction(
            new_state="IMPLEMENTATION",
            run_cc=True,
            reset_reviewers=True,
        )
        mock_note = MagicMock(return_value=True)
        monkeypatch.setattr("watchdog.notify_discord", MagicMock())
        monkeypatch.setattr("watchdog._start_cc", MagicMock())
        monkeypatch.setattr("watchdog.check_transition", lambda *a, **kw: mock_action)

        with patch("notify.post_gitlab_note", mock_note):
            process(pipeline_path)

        body = mock_note.call_args[0][2]
        assert "Unknown Risk" in body


class TestAssessmentNoteOnIdleSkip:
    """ASSESSMENT → IDLE（リスクスキップ）時の note 投稿テスト"""

    def test_skip_note_posted(self, tmp_path, monkeypatch):
        """リスクスキップ時に「実装スキップ」付きの note が投稿される"""
        from watchdog import process

        batch = [
            _make_issue(1, {"complex_level": 4, "domain_risk": "low",
                            "risk_reason": "認証関連", "summary": "Auth change"}),
        ]
        pipeline_path = tmp_path / "test.json"
        _write_pipeline(pipeline_path, _base_pipeline(batch, exclude_any_risk=True))

        mock_note = MagicMock(return_value=True)
        monkeypatch.setattr("watchdog.notify_discord", MagicMock())
        monkeypatch.setattr("notify.post_discord", MagicMock())

        with patch("notify.post_gitlab_note", mock_note):
            process(pipeline_path)

        assert mock_note.call_count == 1
        body = mock_note.call_args[0][2]
        assert "実装スキップ（除外条件に合致）" in body
        assert "Lvl 4" in body
        assert "Low Risk" in body
        assert "Risk reason: 認証関連" in body
        assert "Auth change" in body
