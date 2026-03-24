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


def _make_assessed_batch(n=1, complex_level=3, domain_risk="n/a"):
    """全 Issue に assessment 済みのバッチを生成する。"""
    batch = _make_batch(n)
    for issue in batch:
        issue["assessment"] = {
            "complex_level": complex_level,
            "domain_risk": domain_risk,
        }
    return batch


class TestAssessmentTransitions:

    def test_design_approved_transitions_to_assessment(self):
        from engine.fsm import check_transition
        action = check_transition("DESIGN_APPROVED", _make_batch())
        assert action.new_state == "ASSESSMENT"
        assert action.run_cc is False
        assert action.reset_reviewers is False

    def test_assessment_transitions_to_implementation(self):
        from engine.fsm import check_transition
        batch = _make_assessed_batch(1)
        action = check_transition("ASSESSMENT", batch)
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
        batch = _make_assessed_batch(1)
        data = {"skip_assess": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"

    def test_partial_assessment_does_not_transition(self):
        """バッチ内の一部 Issue のみ assessed の場合、遷移しない"""
        from engine.fsm import check_transition
        batch = _make_batch(3)
        batch[0]["assessment"] = {"complex_level": 2, "domain_risk": "none"}
        # batch[1] and batch[2] have no assessment
        action = check_transition("ASSESSMENT", batch)
        assert action.new_state is None

    def test_all_assessed_transitions(self):
        """バッチ内全 Issue が assessed なら IMPLEMENTATION へ遷移"""
        from engine.fsm import check_transition
        batch = _make_assessed_batch(3)
        action = check_transition("ASSESSMENT", batch)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True


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
        batch = _make_assessed_batch(1, domain_risk="high")
        data = {"exclude_high_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IDLE"
        assert action.run_cc is False
        assert action.reset_reviewers is False

    def test_exclude_high_risk_with_low(self):
        """exclude_high_risk=True + domain_risk=low → IMPLEMENTATION（スキップされない）"""
        from engine.fsm import check_transition
        batch = _make_assessed_batch(1, domain_risk="low")
        data = {"exclude_high_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.reset_reviewers is True

    def test_exclude_high_risk_with_none(self):
        """exclude_high_risk=True + domain_risk=none → IMPLEMENTATION"""
        from engine.fsm import check_transition
        batch = _make_assessed_batch(1, domain_risk="none")
        data = {"exclude_high_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"

    def test_exclude_any_risk_with_low(self):
        """exclude_any_risk=True + domain_risk=low → IDLE"""
        from engine.fsm import check_transition
        batch = _make_assessed_batch(1, domain_risk="low")
        data = {"exclude_any_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IDLE"

    def test_exclude_any_risk_with_high(self):
        """exclude_any_risk=True + domain_risk=high → IDLE"""
        from engine.fsm import check_transition
        batch = _make_assessed_batch(1, domain_risk="high")
        data = {"exclude_any_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IDLE"

    def test_exclude_any_risk_with_none(self):
        """exclude_any_risk=True + domain_risk=none → IMPLEMENTATION"""
        from engine.fsm import check_transition
        batch = _make_assessed_batch(1, domain_risk="none")
        data = {"exclude_any_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"

    def test_no_exclude_flags_with_high(self):
        """除外フラグなし + domain_risk=high → IMPLEMENTATION（既存動作不変）"""
        from engine.fsm import check_transition
        batch = _make_assessed_batch(1, domain_risk="high")
        action = check_transition("ASSESSMENT", batch)
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
        batch = _make_batch(1)
        batch[0]["assessment"] = {"complex_level": 3, "domain_risk": "critical"}
        data = {"exclude_any_risk": True}
        with patch("engine.fsm.log") as mock_log:
            action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"
        mock_log.assert_called_once()
        assert "unknown domain_risk" in mock_log.call_args[0][0]

    def test_worst_risk_aggregation(self):
        """exclude_high_risk + mixed batch → High のみ除外、Low/None は IMPLEMENTATION へ (Issue #200)"""
        from engine.fsm import check_transition
        batch = _make_batch(3)
        batch[0]["assessment"] = {"complex_level": 2, "domain_risk": "none"}
        batch[1]["assessment"] = {"complex_level": 3, "domain_risk": "high"}
        batch[2]["assessment"] = {"complex_level": 1, "domain_risk": "low"}
        data = {"exclude_high_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.skipped_issues is not None
        assert len(action.skipped_issues) == 1
        assert action.skipped_issues[0]["issue"] == 2
        assert action.remaining_issues is not None
        assert len(action.remaining_issues) == 2


class TestDomainRiskNa:
    """domain_risk "n/a" と "none" の区別テスト (Issue #199)"""

    def test_default_risk_is_na(self):
        """_make_assessed_batch で domain_risk 未指定時に "n/a" がデフォルト"""
        batch = _make_assessed_batch(1)
        assert batch[0]["assessment"]["domain_risk"] == "n/a"

    def test_worst_risk_na_only(self):
        """全 Issue が "n/a" → _worst_risk は "n/a" を返す"""
        from engine.fsm import _worst_risk
        batch = _make_assessed_batch(3, domain_risk="n/a")
        assert _worst_risk(batch) == "n/a"

    def test_worst_risk_none_and_na(self):
        """\"none\" と \"n/a\" 混在 → _worst_risk は \"none\" を返す"""
        from engine.fsm import _worst_risk
        batch = _make_batch(3)
        batch[0]["assessment"] = {"complex_level": 2, "domain_risk": "none"}
        batch[1]["assessment"] = {"complex_level": 3, "domain_risk": "n/a"}
        batch[2]["assessment"] = {"complex_level": 1, "domain_risk": "n/a"}
        assert _worst_risk(batch) == "none"

    def test_worst_risk_na_and_low(self):
        """\"n/a\" と \"low\" 混在 → _worst_risk は \"low\" を返す"""
        from engine.fsm import _worst_risk
        batch = _make_batch(2)
        batch[0]["assessment"] = {"complex_level": 2, "domain_risk": "n/a"}
        batch[1]["assessment"] = {"complex_level": 3, "domain_risk": "low"}
        assert _worst_risk(batch) == "low"

    def test_worst_risk_empty_batch(self):
        """空バッチ → _worst_risk は "n/a" を返す"""
        from engine.fsm import _worst_risk
        assert _worst_risk([]) == "n/a"

    def test_exclude_any_risk_with_na(self):
        """exclude_any_risk=True + domain_risk="n/a" → IMPLEMENTATION（スキップされない）"""
        from engine.fsm import check_transition
        batch = _make_assessed_batch(1, domain_risk="n/a")
        data = {"exclude_any_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.reset_reviewers is True

    def test_exclude_any_risk_na_and_low_mixed(self):
        """exclude_any_risk=True + "n/a" と "low" 混在 → IMPLEMENTATION（low のみ除外、n/a は残留）(Issue #200)"""
        from engine.fsm import check_transition
        batch = _make_batch(2)
        batch[0]["assessment"] = {"complex_level": 2, "domain_risk": "n/a"}
        batch[1]["assessment"] = {"complex_level": 3, "domain_risk": "low"}
        data = {"exclude_any_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.skipped_issues is not None
        assert len(action.skipped_issues) == 1
        assert action.skipped_issues[0]["issue"] == 2
        assert action.remaining_issues is not None
        assert len(action.remaining_issues) == 1
        assert action.remaining_issues[0]["issue"] == 1

    def test_risk_display_na_is_empty(self):
        """RISK_DISPLAY の "n/a" エントリは空文字"""
        from commands.dev import RISK_DISPLAY
        assert RISK_DISPLAY["n/a"] == ""

    def test_risk_display_none_is_no_risk(self):
        """RISK_DISPLAY の "none" エントリは "No Risk"（後方互換）"""
        from commands.dev import RISK_DISPLAY
        assert RISK_DISPLAY["none"] == "No Risk"


class TestPerIssueRiskExclusion:
    """Issue #200: Issue ごとのリスク除外判定テスト"""

    def test_exclude_high_risk_mixed_batch(self):
        """exclude-high-risk + mixed batch: High:2, Low:1, None:2 → High のみ除外"""
        from engine.fsm import check_transition
        batch = _make_batch(5)
        batch[0]["assessment"] = {"complex_level": 3, "domain_risk": "high"}
        batch[1]["assessment"] = {"complex_level": 2, "domain_risk": "high"}
        batch[2]["assessment"] = {"complex_level": 1, "domain_risk": "low"}
        batch[3]["assessment"] = {"complex_level": 2, "domain_risk": "none"}
        batch[4]["assessment"] = {"complex_level": 1, "domain_risk": "none"}
        data = {"exclude_high_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.skipped_issues is not None
        assert len(action.skipped_issues) == 2
        skipped_nums = {i["issue"] for i in action.skipped_issues}
        assert skipped_nums == {1, 2}
        assert action.remaining_issues is not None
        assert len(action.remaining_issues) == 3
        remaining_nums = {i["issue"] for i in action.remaining_issues}
        assert remaining_nums == {3, 4, 5}

    def test_exclude_any_risk_mixed_batch(self):
        """exclude-any-risk + mixed batch: High:1, Low:1, None:2 → High/Low 除外、None のみ残留"""
        from engine.fsm import check_transition
        batch = _make_batch(4)
        batch[0]["assessment"] = {"complex_level": 3, "domain_risk": "high"}
        batch[1]["assessment"] = {"complex_level": 2, "domain_risk": "low"}
        batch[2]["assessment"] = {"complex_level": 1, "domain_risk": "none"}
        batch[3]["assessment"] = {"complex_level": 2, "domain_risk": "none"}
        data = {"exclude_any_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.skipped_issues is not None
        assert len(action.skipped_issues) == 2
        skipped_nums = {i["issue"] for i in action.skipped_issues}
        assert skipped_nums == {1, 2}
        assert action.remaining_issues is not None
        assert len(action.remaining_issues) == 2
        remaining_nums = {i["issue"] for i in action.remaining_issues}
        assert remaining_nums == {3, 4}

    def test_exclude_high_risk_all_high(self):
        """exclude-high-risk + all high → IDLE（全除外）"""
        from engine.fsm import check_transition
        batch = _make_assessed_batch(3, domain_risk="high")
        data = {"exclude_high_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IDLE"
        assert action.skipped_issues is None

    def test_exclude_any_risk_all_none(self):
        """exclude-any-risk + all none → IMPLEMENTATION（除外なし）"""
        from engine.fsm import check_transition
        batch = _make_assessed_batch(3, domain_risk="none")
        data = {"exclude_any_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.skipped_issues is None

    def test_no_exclude_flags(self):
        """exclude なし → IMPLEMENTATION、skipped_issues is None"""
        from engine.fsm import check_transition
        batch = _make_assessed_batch(3, domain_risk="high")
        action = check_transition("ASSESSMENT", batch)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.skipped_issues is None

    def test_exclude_high_risk_no_high_issues(self):
        """exclude-high-risk + no high issues → IMPLEMENTATION、skipped_issues is None"""
        from engine.fsm import check_transition
        batch = _make_batch(3)
        batch[0]["assessment"] = {"complex_level": 1, "domain_risk": "low"}
        batch[1]["assessment"] = {"complex_level": 2, "domain_risk": "none"}
        batch[2]["assessment"] = {"complex_level": 1, "domain_risk": "none"}
        data = {"exclude_high_risk": True}
        action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.skipped_issues is None

    def test_unknown_domain_risk_normalized_to_na(self):
        """unknown domain_risk は n/a に正規化 → exclude_any_risk でも除外されない"""
        from engine.fsm import check_transition
        from unittest.mock import patch
        batch = _make_batch(2)
        batch[0]["assessment"] = {"complex_level": 3, "domain_risk": "unknown"}
        batch[1]["assessment"] = {"complex_level": 2, "domain_risk": "low"}
        data = {"exclude_any_risk": True}
        with patch("engine.fsm.log"):
            action = check_transition("ASSESSMENT", batch, data)
        assert action.new_state == "IMPLEMENTATION"
        assert action.run_cc is True
        assert action.skipped_issues is not None
        assert len(action.skipped_issues) == 1
        assert action.skipped_issues[0]["issue"] == 2  # low が除外
        assert action.remaining_issues is not None
        assert len(action.remaining_issues) == 1
        assert action.remaining_issues[0]["issue"] == 1  # unknown→n/a が残留
        # 正規化が実動作に反映されていることを検証
        assert batch[0]["assessment"]["domain_risk"] == "n/a"
