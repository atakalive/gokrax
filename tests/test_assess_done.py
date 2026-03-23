"""tests/test_assess_done.py — assess-done コマンドと難易度記録 (Issue #169)"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _write_pipeline(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _make_batch(n=1, **kwargs):
    items = []
    for i in range(1, n + 1):
        item = {
            "issue": i, "title": f"Issue {i}", "commit": None,
            "cc_session_id": None,
            "design_reviews": {}, "code_reviews": {},
            "added_at": "2025-01-01T00:00:00+09:00",
        }
        item.update(kwargs)
        items.append(item)
    return items


def _make_assessed_batch(n=1, complex_level=3, domain_risk="none"):
    """全 Issue に assessment 済みのバッチを生成する。"""
    batch = _make_batch(n)
    for issue in batch:
        issue["assessment"] = {
            "complex_level": complex_level,
            "domain_risk": domain_risk,
        }
    return batch


# --- 8-1: cmd_assess_done が assessment を記録 ---
class TestCmdAssessDone:

    def test_assess_done_records_assessment(self, tmp_pipelines):
        """8-1: assessment dict がバッチ内の Issue エントリに記録される"""

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, {
            "project": "test-pj",
            "gitlab": "atakalive/test-pj",
            "state": "ASSESSMENT",
            "enabled": True,
            "implementer": "kaneko",
            "batch": _make_batch(1),
            "history": [],
        })

        args = SimpleNamespace(
            project="test-pj", issue=1, complex_level=3, summary="medium difficulty",
            risk="none", risk_reason="",
        )
        with patch("commands.dev._update_issue_title_with_assessment", return_value=True):
            from commands.dev import cmd_assess_done
            cmd_assess_done(args)

        data = json.loads(path.read_text())
        assert data["batch"][0]["assessment"]["complex_level"] == 3
        assert data["batch"][0]["assessment"]["summary"] == "medium difficulty"
        assert data["batch"][0]["assessment"]["assessed_by"] == "kaneko"
        assert "timestamp" in data["batch"][0]["assessment"]

    # --- 8-2: ASSESSMENT 以外ではエラー ---
    def test_assess_done_wrong_state(self, tmp_pipelines):
        """8-2: ASSESSMENT 以外で SystemExit"""

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, {
            "project": "test-pj",
            "state": "IMPLEMENTATION",
            "enabled": True,
            "implementer": "kaneko",
            "batch": [],
            "history": [],
        })

        args = SimpleNamespace(project="test-pj", issue=1, complex_level=2, summary="", risk="none", risk_reason="")
        with pytest.raises(SystemExit, match="Not in ASSESSMENT state"):
            from commands.dev import cmd_assess_done
            cmd_assess_done(args)

    # --- 8-3: level は 1-5 のみ（argparse が弾く。テストは境界確認）---
    def test_assess_done_complex_level_range(self, tmp_pipelines):
        """8-3: complex_level 1 と 5 が正常に記録される"""

        from commands.dev import cmd_assess_done

        for complex_level in (1, 5):
            path = tmp_pipelines / "test-pj.json"
            _write_pipeline(path, {
                "project": "test-pj",
                "state": "ASSESSMENT",
                "enabled": True,
                "implementer": "kaneko",
                "batch": _make_batch(1),
                "history": [],
            })
            args = SimpleNamespace(project="test-pj", issue=1, complex_level=complex_level, summary="", risk="none", risk_reason="")
            with patch("commands.dev._update_issue_title_with_assessment", return_value=True):
                cmd_assess_done(args)
            data = json.loads(path.read_text())
            assert data["batch"][0]["assessment"]["complex_level"] == complex_level

    def test_assess_done_issue_not_in_batch(self, tmp_pipelines):
        """args.issue がバッチに存在しない場合の SystemExit"""
        from commands.dev import cmd_assess_done

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, {
            "project": "test-pj",
            "state": "ASSESSMENT",
            "enabled": True,
            "implementer": "kaneko",
            "batch": _make_batch(1),
            "history": [],
        })
        args = SimpleNamespace(project="test-pj", issue=999, complex_level=3, summary="", risk="none", risk_reason="")
        with pytest.raises(SystemExit, match="Issue #999 not in batch"):
            cmd_assess_done(args)


# --- 8-4, 8-5: FSM 遷移 ---
class TestAssessmentFSMWait:

    def test_assessment_waits_for_assess_done(self):
        """8-4: assessment 未設定なら遷移しない"""
        from engine.fsm import check_transition
        action = check_transition("ASSESSMENT", _make_batch())
        assert action.new_state is None

    def test_assessment_transitions_after_assess_done(self):
        """8-5: 全 Issue assessment 設定済みなら IMPLEMENTATION へ遷移"""
        from engine.fsm import check_transition
        batch = _make_assessed_batch(1)
        action = check_transition("ASSESSMENT", batch)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.reset_reviewers is True


# --- 8-6, 8-7: _update_issue_title_with_assessment ---
class TestUpdateIssueTitleWithComplexLevel:

    def test_update_issue_title_with_assessment(self):
        """8-6: 正常系 — [Lvl N / No Risk] が末尾に付与される"""
        from commands.dev import _update_issue_title_with_assessment

        view_result = MagicMock(returncode=0, stdout=json.dumps({"title": "feat: do something"}))
        update_result = MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=[view_result, update_result]) as mock_run:
            ok = _update_issue_title_with_assessment("atakalive/test-pj", 42, 3)

        assert ok is True
        # update コマンドのタイトル引数を確認
        update_call = mock_run.call_args_list[1]
        assert "feat: do something [Lvl 3 / No Risk]" in update_call[0][0]

    def test_update_issue_title_replaces_existing_complex_level(self):
        """8-7: 既存の [Lvl N] を置換（後方互換: 先頭タグの除去確認）"""
        from commands.dev import _update_issue_title_with_assessment

        view_result = MagicMock(returncode=0, stdout=json.dumps({"title": "[Lvl 2 / No Risk] feat: do something"}))
        update_result = MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=[view_result, update_result]) as mock_run:
            ok = _update_issue_title_with_assessment("atakalive/test-pj", 42, 4)

        assert ok is True
        update_call = mock_run.call_args_list[1]
        assert "feat: do something [Lvl 4 / No Risk]" in update_call[0][0]

    def test_update_issue_title_replaces_existing_complex_level_at_end(self):
        """8-7b: 末尾の既存 [Lvl N] を置換"""
        from commands.dev import _update_issue_title_with_assessment

        view_result = MagicMock(returncode=0, stdout=json.dumps({"title": "feat: do something [Lvl 2 / Low Risk]"}))
        update_result = MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=[view_result, update_result]) as mock_run:
            ok = _update_issue_title_with_assessment("atakalive/test-pj", 42, 4)

        assert ok is True
        update_call = mock_run.call_args_list[1]
        assert "feat: do something [Lvl 4 / No Risk]" in update_call[0][0]

    def test_update_issue_title_removes_both_front_and_end_tags(self):
        """8-7c: 先頭・末尾両方にタグがある異常状態 — 両方除去して末尾に1つだけ付与"""
        from commands.dev import _update_issue_title_with_assessment

        view_result = MagicMock(returncode=0, stdout=json.dumps({"title": "[Lvl 2 / Low Risk] feat: do something [Lvl 3 / High Risk]"}))
        update_result = MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=[view_result, update_result]) as mock_run:
            ok = _update_issue_title_with_assessment("atakalive/test-pj", 42, 5)

        assert ok is True
        update_call = mock_run.call_args_list[1]
        assert "feat: do something [Lvl 5 / No Risk]" in update_call[0][0]


# --- 8-8: ASSESSMENT タイムアウト ---
class TestAssessmentTimeout:

    def test_assessment_timeout_blocked(self):
        """8-8: BLOCK_TIMERS 超過で BLOCKED 遷移"""
        from engine.fsm import check_transition
        from datetime import datetime, timedelta
        from config import LOCAL_TZ, BLOCK_TIMERS

        entered = datetime.now(LOCAL_TZ) - timedelta(seconds=BLOCK_TIMERS["ASSESSMENT"] + 10)
        data = {
            "history": [{"to": "ASSESSMENT", "at": entered.isoformat()}],
        }
        action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "BLOCKED"


# --- 8-9: title 更新失敗は warning ---
class TestAssessDoneTitleFailure:

    def test_assess_done_title_failure_is_warning(self, tmp_pipelines, capsys):
        """8-9: title 更新失敗でも遷移はブロックされない"""

        from commands.dev import cmd_assess_done

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, {
            "project": "test-pj",
            "gitlab": "atakalive/test-pj",
            "state": "ASSESSMENT",
            "enabled": True,
            "implementer": "kaneko",
            "batch": _make_batch(2),
            "history": [],
        })

        args = SimpleNamespace(project="test-pj", issue=1, complex_level=3, summary="", risk="none", risk_reason="")
        with patch("commands.dev._update_issue_title_with_assessment", return_value=False):
            cmd_assess_done(args)

        # assessment は記録されている
        data = json.loads(path.read_text())
        assert data["batch"][0]["assessment"]["complex_level"] == 3

        # stderr に warning が出る
        captured = capsys.readouterr()
        assert "title update failed" in captured.err


# --- 8-10: summary 500文字超切り詰め ---
class TestAssessDoneSummaryTruncation:

    def test_assess_done_summary_truncation(self, tmp_pipelines):
        """8-10: summary が 500 文字を超えたら切り詰められる"""

        from commands.dev import cmd_assess_done

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, {
            "project": "test-pj",
            "gitlab": "atakalive/test-pj",
            "state": "ASSESSMENT",
            "enabled": True,
            "implementer": "kaneko",
            "batch": _make_batch(1),
            "history": [],
        })

        long_summary = "x" * 600
        args = SimpleNamespace(project="test-pj", issue=1, complex_level=2, summary=long_summary, risk="none", risk_reason="")
        with patch("commands.dev._update_issue_title_with_assessment", return_value=True):
            cmd_assess_done(args)

        data = json.loads(path.read_text())
        assert len(data["batch"][0]["assessment"]["summary"]) == 500


# --- domain_risk テスト ---
class TestCmdAssessDoneDomainRisk:

    def test_assess_done_records_domain_risk(self, tmp_pipelines):
        """--risk high --risk-reason "reason" → pipeline に記録される"""
        from commands.dev import cmd_assess_done

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, {
            "project": "test-pj", "gitlab": "atakalive/test-pj",
            "state": "ASSESSMENT", "enabled": True,
            "implementer": "kaneko", "batch": _make_batch(1), "history": [],
        })
        args = SimpleNamespace(
            project="test-pj", issue=1, complex_level=3, summary="test",
            risk="high", risk_reason="credential handling change",
        )
        with patch("commands.dev._update_issue_title_with_assessment", return_value=True):
            cmd_assess_done(args)

        data = json.loads(path.read_text())
        assert data["batch"][0]["assessment"]["domain_risk"] == "high"
        assert data["batch"][0]["assessment"]["risk_reason"] == "credential handling change"

    def test_assess_done_default_risk_none(self, tmp_pipelines):
        """--risk 未指定 → domain_risk: "none", risk_reason: "" が記録される"""
        from commands.dev import cmd_assess_done

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, {
            "project": "test-pj", "gitlab": "atakalive/test-pj",
            "state": "ASSESSMENT", "enabled": True,
            "implementer": "kaneko", "batch": _make_batch(1), "history": [],
        })
        args = SimpleNamespace(
            project="test-pj", issue=1, complex_level=2, summary="",
            risk="none", risk_reason="",
        )
        with patch("commands.dev._update_issue_title_with_assessment", return_value=True):
            cmd_assess_done(args)

        data = json.loads(path.read_text())
        assert data["batch"][0]["assessment"]["domain_risk"] == "none"
        assert data["batch"][0]["assessment"]["risk_reason"] == ""

    def test_assess_done_risk_reason_required_for_non_none(self, tmp_pipelines):
        """--risk high --risk-reason "" → SystemExit"""
        from commands.dev import cmd_assess_done

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, {
            "project": "test-pj", "state": "ASSESSMENT", "enabled": True,
            "implementer": "kaneko", "batch": _make_batch(1), "history": [],
        })
        args = SimpleNamespace(
            project="test-pj", issue=1, complex_level=3, summary="",
            risk="high", risk_reason="",
        )
        with pytest.raises(SystemExit, match="--risk-reason is required"):
            cmd_assess_done(args)

    def test_assess_done_risk_reason_normalized_for_none(self, tmp_pipelines):
        """--risk none --risk-reason "something" → risk_reason: "" に正規化"""
        from commands.dev import cmd_assess_done

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, {
            "project": "test-pj", "gitlab": "atakalive/test-pj",
            "state": "ASSESSMENT", "enabled": True,
            "implementer": "kaneko", "batch": _make_batch(1), "history": [],
        })
        args = SimpleNamespace(
            project="test-pj", issue=1, complex_level=2, summary="",
            risk="none", risk_reason="something",
        )
        with patch("commands.dev._update_issue_title_with_assessment", return_value=True):
            cmd_assess_done(args)

        data = json.loads(path.read_text())
        assert data["batch"][0]["assessment"]["domain_risk"] == "none"
        assert data["batch"][0]["assessment"]["risk_reason"] == ""


class TestUpdateIssueTitleWithRisk:

    def test_update_title_with_risk(self):
        """domain_risk="high" → [Lvl 3 / High Risk] が末尾に付与"""
        from commands.dev import _update_issue_title_with_assessment

        view_result = MagicMock(returncode=0, stdout=json.dumps({"title": "feat: do something"}))
        update_result = MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=[view_result, update_result]) as mock_run:
            ok = _update_issue_title_with_assessment("atakalive/test-pj", 42, 3, "high")

        assert ok is True
        update_call = mock_run.call_args_list[1]
        assert "feat: do something [Lvl 3 / High Risk]" in update_call[0][0]

    def test_update_title_risk_none_shows_no_risk(self):
        """domain_risk="none" → [Lvl 3 / No Risk]"""
        from commands.dev import _update_issue_title_with_assessment

        view_result = MagicMock(returncode=0, stdout=json.dumps({"title": "feat: do something"}))
        update_result = MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=[view_result, update_result]) as mock_run:
            ok = _update_issue_title_with_assessment("atakalive/test-pj", 42, 3, "none")

        assert ok is True
        update_call = mock_run.call_args_list[1]
        assert "feat: do something [Lvl 3 / No Risk]" in update_call[0][0]

    def test_update_title_replaces_existing_risk_tag(self):
        """既存 [Lvl 2 / Low Risk] → [Lvl 3 / High Risk] に置換"""
        from commands.dev import _update_issue_title_with_assessment

        view_result = MagicMock(returncode=0, stdout=json.dumps({"title": "feat: do something [Lvl 2 / Low Risk]"}))
        update_result = MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=[view_result, update_result]) as mock_run:
            ok = _update_issue_title_with_assessment("atakalive/test-pj", 42, 3, "high")

        assert ok is True
        update_call = mock_run.call_args_list[1]
        assert "feat: do something [Lvl 3 / High Risk]" in update_call[0][0]

    def test_update_title_replaces_no_risk_with_high_risk(self):
        """既存 [Lvl 2 / No Risk] → [Lvl 3 / High Risk] に置換"""
        from commands.dev import _update_issue_title_with_assessment

        view_result = MagicMock(returncode=0, stdout=json.dumps({"title": "feat: do something [Lvl 2 / No Risk]"}))
        update_result = MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=[view_result, update_result]) as mock_run:
            ok = _update_issue_title_with_assessment("atakalive/test-pj", 42, 3, "high")

        assert ok is True
        update_call = mock_run.call_args_list[1]
        assert "feat: do something [Lvl 3 / High Risk]" in update_call[0][0]
