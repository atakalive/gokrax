"""tests/test_assessment.py — ASSESSMENT 状態のスケルトンテスト (Issue #168)"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


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


class TestAssessmentTransitions:

    def test_design_approved_transitions_to_assessment(self):
        from engine.fsm import check_transition
        action = check_transition("DESIGN_APPROVED", _make_batch())
        assert action.new_state == "ASSESSMENT"
        assert action.run_cc is False
        assert action.reset_reviewers is False

    def test_assessment_transitions_to_implementation(self):
        from engine.fsm import check_transition
        data = {"assessment": {"complex_level": 3}}
        action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.reset_reviewers is True

    def test_skip_assess_skips_to_implementation(self):
        from engine.fsm import check_transition
        data = {"skip_assess": True}
        action = check_transition("DESIGN_APPROVED", _make_batch(), data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.reset_reviewers is True

    def test_skip_assess_false_explicit(self):
        from engine.fsm import check_transition
        data = {"skip_assess": False}
        action = check_transition("DESIGN_APPROVED", _make_batch(), data)
        assert action.new_state == "ASSESSMENT"

    def test_assessment_ignores_skip_assess(self):
        from engine.fsm import check_transition
        data = {"skip_assess": True, "assessment": {"complex_level": 3}}
        action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IMPLEMENTATION"


class TestAssessmentConfig:

    def test_assessment_in_valid_transitions(self):
        from config.states import VALID_TRANSITIONS
        assert "ASSESSMENT" in VALID_TRANSITIONS["DESIGN_APPROVED"]
        assert "IMPLEMENTATION" in VALID_TRANSITIONS["DESIGN_APPROVED"]
        assert VALID_TRANSITIONS["ASSESSMENT"] == ["IMPLEMENTATION", "IDLE"]

    def test_assessment_in_state_phase_map(self):
        from config.states import STATE_PHASE_MAP
        assert STATE_PHASE_MAP["ASSESSMENT"] == "design"

    def test_assessment_in_block_timers(self):
        from config.states import BLOCK_TIMERS
        assert BLOCK_TIMERS["ASSESSMENT"] == 1200

    def test_assessment_state_enum_exists(self):
        from config.states import State
        assert State.ASSESSMENT.value == "ASSESSMENT"


class TestReadDomainRisk:

    def test_read_domain_risk_file_exists(self, tmp_path):
        """DOMAIN_RISK.md が存在する場合に内容が返される（非ASCII含む）"""
        risk_file = tmp_path / "DOMAIN_RISK.md"
        risk_file.write_text("## High Risk\n- 認証情報の変更\n", encoding="utf-8")
        from engine.fsm import _read_domain_risk
        content = _read_domain_risk("test-pj", str(tmp_path))
        assert "認証情報の変更" in content
        assert "## High Risk" in content

    def test_read_domain_risk_file_missing(self, tmp_path):
        """ファイルなし → 空文字"""
        from engine.fsm import _read_domain_risk
        content = _read_domain_risk("test-pj", str(tmp_path))
        assert content == ""

    def test_read_domain_risk_custom_path(self, tmp_path):
        """PROJECT_RISK_FILES にカスタムパスを指定した場合にそのパスから読む"""
        custom_file = tmp_path / "custom_risk.md"
        custom_file.write_text("custom risk content", encoding="utf-8")
        from engine.fsm import _read_domain_risk
        from unittest.mock import patch
        with patch("config.PROJECT_RISK_FILES", {"test-pj": str(custom_file)}):
            content = _read_domain_risk("test-pj", "")
        assert content == "custom risk content"

    def test_read_domain_risk_custom_path_missing_file(self, tmp_path, caplog):
        """カスタムパスのファイルが存在しない → 空文字（warning ログ）"""
        import logging
        from engine.fsm import _read_domain_risk
        from unittest.mock import patch
        with patch("config.PROJECT_RISK_FILES", {"test-pj": str(tmp_path / "nonexistent.md")}):
            with caplog.at_level(logging.WARNING):
                content = _read_domain_risk("test-pj", "")
        assert content == ""
        assert "not found at custom path" in caplog.text

    def test_read_domain_risk_empty_repo_path_no_custom(self):
        """repo_path="" かつカスタムパスなし → 空文字"""
        from engine.fsm import _read_domain_risk
        from unittest.mock import patch
        with patch("config.PROJECT_RISK_FILES", {}):
            content = _read_domain_risk("test-pj", "")
        assert content == ""

    def test_read_domain_risk_truncation(self, tmp_path):
        """10,000 文字超のファイル → 先頭 10,000 文字に切り詰め"""
        risk_file = tmp_path / "DOMAIN_RISK.md"
        risk_file.write_text("x" * 15_000, encoding="utf-8")
        from engine.fsm import _read_domain_risk
        content = _read_domain_risk("test-pj", str(tmp_path))
        assert len(content) == 10_000

    def test_read_domain_risk_relative_path_warning(self, tmp_path, caplog):
        """カスタムパスに相対パスが指定された場合 → warning ログ"""
        import logging
        from engine.fsm import _read_domain_risk
        from unittest.mock import patch
        rel_path = "relative/path/risk.md"
        with patch("config.PROJECT_RISK_FILES", {"test-pj": rel_path}):
            with caplog.at_level(logging.WARNING):
                _read_domain_risk("test-pj", "")
        assert "relative path" in caplog.text


class TestParseQueueLineSkipAssess:

    def test_parse_queue_line_skip_assess(self):
        from task_queue import parse_queue_line
        result = parse_queue_line("gokrax 1 skip-assess")
        assert result["skip_assess"] is True

    def test_parse_queue_line_no_skip_assess(self):
        from task_queue import parse_queue_line
        result = parse_queue_line("gokrax 1 no-skip-assess")
        assert result["skip_assess"] is False


class TestParseQueueLineExcludeRisk:
    """parse_queue_line の exclude-high-risk / exclude-any-risk トークンテスト (Issue #181)"""

    def test_exclude_high_risk_token(self):
        from task_queue import parse_queue_line
        result = parse_queue_line("gokrax 1 exclude-high-risk")
        assert result["exclude_high_risk"] is True

    def test_exclude_any_risk_token(self):
        from task_queue import parse_queue_line
        result = parse_queue_line("gokrax 1 exclude-any-risk")
        assert result["exclude_any_risk"] is True

    def test_no_exclude_high_risk_token(self):
        from task_queue import parse_queue_line
        result = parse_queue_line("gokrax 1 no-exclude-high-risk")
        assert result["exclude_high_risk"] is False

    def test_no_exclude_any_risk_token(self):
        from task_queue import parse_queue_line
        result = parse_queue_line("gokrax 1 no-exclude-any-risk")
        assert result["exclude_any_risk"] is False

    def test_default_exclude_flags(self):
        from task_queue import parse_queue_line
        result = parse_queue_line("gokrax 1")
        assert result["exclude_high_risk"] is False
        assert result["exclude_any_risk"] is False


class TestDomainRiskSkip:
    """ASSESSMENT 完了時の domain_risk に基づくスキップ判定テスト (Issue #181)"""

    def test_exclude_high_risk_with_high(self):
        """exclude_high_risk=True + domain_risk=high → IDLE"""
        from engine.fsm import check_transition
        data = {
            "assessment": {"complex_level": 3, "domain_risk": "high"},
            "exclude_high_risk": True,
        }
        action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IDLE"
        assert action.run_cc is False
        assert action.reset_reviewers is False

    def test_exclude_high_risk_with_low(self):
        """exclude_high_risk=True + domain_risk=low → IMPLEMENTATION（スキップされない）"""
        from engine.fsm import check_transition
        data = {
            "assessment": {"complex_level": 3, "domain_risk": "low"},
            "exclude_high_risk": True,
        }
        action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.reset_reviewers is True

    def test_exclude_high_risk_with_none(self):
        """exclude_high_risk=True + domain_risk=none → IMPLEMENTATION"""
        from engine.fsm import check_transition
        data = {
            "assessment": {"complex_level": 3, "domain_risk": "none"},
            "exclude_high_risk": True,
        }
        action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IMPLEMENTATION"

    def test_exclude_any_risk_with_low(self):
        """exclude_any_risk=True + domain_risk=low → IDLE"""
        from engine.fsm import check_transition
        data = {
            "assessment": {"complex_level": 3, "domain_risk": "low"},
            "exclude_any_risk": True,
        }
        action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IDLE"

    def test_exclude_any_risk_with_high(self):
        """exclude_any_risk=True + domain_risk=high → IDLE"""
        from engine.fsm import check_transition
        data = {
            "assessment": {"complex_level": 3, "domain_risk": "high"},
            "exclude_any_risk": True,
        }
        action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IDLE"

    def test_exclude_any_risk_with_none(self):
        """exclude_any_risk=True + domain_risk=none → IMPLEMENTATION"""
        from engine.fsm import check_transition
        data = {
            "assessment": {"complex_level": 3, "domain_risk": "none"},
            "exclude_any_risk": True,
        }
        action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IMPLEMENTATION"

    def test_no_exclude_flags_with_high(self):
        """除外フラグなし + domain_risk=high → IMPLEMENTATION（既存動作不変）"""
        from engine.fsm import check_transition
        data = {
            "assessment": {"complex_level": 3, "domain_risk": "high"},
        }
        action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IMPLEMENTATION"

    def test_assessment_to_idle_in_valid_transitions(self):
        """ASSESSMENT → IDLE 遷移が VALID_TRANSITIONS で許可されていること"""
        from config.states import VALID_TRANSITIONS
        assert "IDLE" in VALID_TRANSITIONS["ASSESSMENT"]
        assert "IMPLEMENTATION" in VALID_TRANSITIONS["ASSESSMENT"]

    def test_unknown_domain_risk_no_skip(self):
        """domain_risk が未知の値 → IMPLEMENTATION（安全側）+ warning ログ"""
        from engine.fsm import check_transition
        from unittest.mock import patch
        data = {
            "assessment": {"complex_level": 3, "domain_risk": "critical"},
            "exclude_any_risk": True,
        }
        with patch("engine.fsm.log") as mock_log:
            action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IMPLEMENTATION"
        mock_log.assert_called_once()
        assert "unknown domain_risk" in mock_log.call_args[0][0]

    def test_domain_risk_key_missing(self):
        """domain_risk キー欠損 → IMPLEMENTATION（none 扱い）+ warning ログ"""
        from engine.fsm import check_transition
        from unittest.mock import patch
        data = {
            "assessment": {"complex_level": 3},
            "exclude_any_risk": True,
        }
        with patch("engine.fsm.log") as mock_log:
            action = check_transition("ASSESSMENT", _make_batch(), data)
        assert action.new_state == "IMPLEMENTATION"
        mock_log.assert_called_once()
        assert "domain_risk key missing" in mock_log.call_args[0][0]
