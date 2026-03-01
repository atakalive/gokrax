"""tests/test_spec_review.py — spec_review.py のテスト"""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spec_review import (
    VERDICT_ALIASES,
    SEVERITY_ALIASES,
    SEVERITY_ORDER,
    SpecReviewItem,
    SpecReviewResult,
    MergedReviewReport,
    parse_review_yaml,
    validate_received_entry,
    should_continue_review,
    _reset_review_requests,
    merge_reviews,
    format_merged_report,
    build_review_history_entry,
)

JST = timezone(timedelta(hours=9))


# --- VERDICT_ALIASES ---

class TestVerdictAliases:
    @pytest.mark.parametrize("raw,expected", [
        ("approve", "APPROVE"), ("APPROVE", "APPROVE"),
        ("Approve", "APPROVE"), ("  approve  ", "APPROVE"),
        ("p0", "P0"), ("P0", "P0"),
        ("reject", "P0"), ("REJECT", "P0"), ("Reject", "P0"),
        ("p1", "P1"), ("P1", "P1"),
    ])
    def test_mapping(self, raw, expected):
        """strip().lower() 正規化でルックアップ。"""
        assert VERDICT_ALIASES.get(raw.strip().lower()) == expected

    def test_unknown_not_in_aliases(self):
        assert "lgtm" not in VERDICT_ALIASES


# --- SEVERITY_ALIASES ---

class TestSeverityAliases:
    @pytest.mark.parametrize("raw,expected", [
        ("critical", "critical"), ("Critical", "critical"), ("CRITICAL", "critical"),
        ("major", "major"), ("Major", "major"), ("MAJOR", "major"),
        ("minor", "minor"), ("Minor", "minor"), ("MINOR", "minor"),
        ("suggestion", "suggestion"), ("Suggestion", "suggestion"), ("SUGGESTION", "suggestion"),
        ("  Critical  ", "critical"),
    ])
    def test_mapping(self, raw, expected):
        assert SEVERITY_ALIASES.get(raw.strip().lower()) == expected


# --- parse_review_yaml ---

class TestParseReviewYaml:
    def _make_yaml(self, verdict="APPROVE", items=None):
        yaml_str = f"verdict: {verdict}\n"
        if items is not None:
            yaml_str += "items:\n"
            for item in items:
                yaml_str += f"  - id: {item['id']}\n"
                yaml_str += f"    severity: {item['severity']}\n"
                yaml_str += f"    section: \"{item.get('section', '§1')}\"\n"
                yaml_str += f"    title: \"{item.get('title', 'title')}\"\n"
                yaml_str += f"    description: \"{item.get('description', 'desc')}\"\n"
        return f"```yaml\n{yaml_str}```"

    def test_approve_no_items(self):
        text = self._make_yaml("APPROVE")
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is True
        assert result.verdict == "APPROVE"
        assert result.items == []
        assert result.reviewer == "pascal"

    def test_p0_with_items(self):
        text = self._make_yaml("P0", [
            {"id": "C-1", "severity": "critical", "section": "§6.2",
             "title": "Bug", "description": "Bad"},
        ])
        result = parse_review_yaml(text, "leibniz")
        assert result.parse_success is True
        assert result.verdict == "P0"
        assert len(result.items) == 1
        assert result.items[0].normalized_id == "leibniz:C-1"
        assert result.items[0].severity == "critical"

    def test_alias_reject_becomes_p0(self):
        text = self._make_yaml("reject")
        result = parse_review_yaml(text, "pascal")
        assert result.verdict == "P0"

    def test_alias_case_insensitive(self):
        """Approve, P1 等の大文字混在も strip().lower() で正規化される。"""
        text = "```yaml\nverdict: Approve\n```"
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is True
        assert result.verdict == "APPROVE"

    def test_verdict_with_whitespace(self):
        text = "```yaml\nverdict: '  P1  '\n```"
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is True
        assert result.verdict == "P1"

    def test_no_yaml_block(self):
        result = parse_review_yaml("No yaml here", "pascal")
        assert result.parse_success is False

    def test_invalid_verdict(self):
        text = "```yaml\nverdict: LGTM\n```"
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is False

    def test_invalid_severity(self):
        text = self._make_yaml("P1", [
            {"id": "X-1", "severity": "blocker"},
        ])
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is False

    def test_items_null(self):
        """items: null → 空リストとして扱い parse_success=True（APPROVE時の正常動作）"""
        text = "```yaml\nverdict: APPROVE\nitems: null\n```"
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is True
        assert result.items == []

    def test_items_not_list(self):
        """items が string → parse_success=False（Leibniz P0-1）"""
        text = "```yaml\nverdict: APPROVE\nitems: not-a-list\n```"
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is False

    def test_item_missing_id(self):
        """item に id キーなし → parse_success=False（Leibniz P0-2）"""
        text = "```yaml\nverdict: P1\nitems:\n  - severity: major\n    title: t\n```"
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is False

    def test_item_missing_severity(self):
        """item に severity キーなし → parse_success=False（Leibniz P0-2）"""
        text = "```yaml\nverdict: P1\nitems:\n  - id: C-1\n    title: t\n```"
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is False

    def test_item_optional_keys_fallback(self):
        """section/title/description/suggestion 省略 → 空文字/None"""
        text = "```yaml\nverdict: P1\nitems:\n  - id: C-1\n    severity: critical\n```"
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is True
        assert result.items[0].section == ""
        assert result.items[0].title == ""
        assert result.items[0].suggestion is None

    def test_multiple_yaml_blocks_uses_first(self):
        text = "```yaml\nverdict: APPROVE\n```\n\n```yaml\nverdict: P0\n```"
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is True
        assert result.verdict == "APPROVE"

    def test_malformed_yaml(self):
        text = "```yaml\n: : :\n```"
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is False

    def test_yml_fence(self):
        text = "```yml\nverdict: APPROVE\n```"
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is True

    def test_raw_text_preserved_on_failure(self):
        text = "no yaml"
        result = parse_review_yaml(text, "pascal")
        assert result.raw_text == text

    def test_item_not_dict(self):
        """items 内に非dict → parse_success=False"""
        text = "```yaml\nverdict: P1\nitems:\n  - just a string\n```"
        result = parse_review_yaml(text, "pascal")
        assert result.parse_success is False


# --- validate_received_entry ---

class TestValidateReceivedEntry:
    def test_valid(self):
        entry = {"verdict": "P0", "items": [], "parse_success": True}
        assert validate_received_entry(entry) is True

    def test_verdict_null(self):
        entry = {"verdict": None, "items": [], "parse_success": True}
        assert validate_received_entry(entry) is False

    def test_verdict_invalid(self):
        entry = {"verdict": "LGTM", "items": [], "parse_success": True}
        assert validate_received_entry(entry) is False

    def test_items_not_list(self):
        entry = {"verdict": "APPROVE", "items": "none", "parse_success": True}
        assert validate_received_entry(entry) is False

    def test_parse_success_false(self):
        entry = {"verdict": "APPROVE", "items": [], "parse_success": False}
        assert validate_received_entry(entry) is False


# --- should_continue_review ---

class TestShouldContinueReview:
    def _config(self, entries, revise_count=0, max_cycles=5):
        return {
            "current_reviews": {"entries": entries},
            "revise_count": revise_count,
            "max_revise_cycles": max_cycles,
        }

    def test_all_approve(self):
        entries = {
            "pascal": {"status": "received", "verdict": "APPROVE", "items": [], "parse_success": True},
            "leibniz": {"status": "received", "verdict": "APPROVE", "items": [], "parse_success": True},
        }
        assert should_continue_review(self._config(entries), "full") == "approved"

    def test_one_p0(self):
        entries = {
            "pascal": {"status": "received", "verdict": "P0", "items": [], "parse_success": True},
            "leibniz": {"status": "received", "verdict": "APPROVE", "items": [], "parse_success": True},
        }
        assert should_continue_review(self._config(entries), "full") == "revise"

    def test_max_reached_stalled(self):
        entries = {
            "pascal": {"status": "received", "verdict": "P1", "items": [], "parse_success": True},
            "leibniz": {"status": "received", "verdict": "APPROVE", "items": [], "parse_success": True},
        }
        cfg = self._config(entries, revise_count=5, max_cycles=5)
        assert should_continue_review(cfg, "full") == "stalled"

    def test_all_timeout_failed(self):
        entries = {
            "pascal": {"status": "timeout"},
            "leibniz": {"status": "timeout"},
        }
        assert should_continue_review(self._config(entries), "full") == "failed"

    def test_parse_fail_plus_insufficient(self):
        entries = {
            "pascal": {"status": "parse_failed"},
            "leibniz": {"status": "timeout"},
        }
        assert should_continue_review(self._config(entries), "full") == "paused"

    def test_one_received_insufficient_for_full(self):
        entries = {
            "pascal": {"status": "received", "verdict": "APPROVE", "items": [], "parse_success": True},
            "leibniz": {"status": "timeout"},
        }
        # full requires 2, only 1 received, no parse_fail → failed
        assert should_continue_review(self._config(entries), "full") == "failed"

    def test_lite_mode_one_approve(self):
        entries = {
            "pascal": {"status": "received", "verdict": "APPROVE", "items": [], "parse_success": True},
        }
        assert should_continue_review(self._config(entries), "lite") == "approved"

    def test_empty_entries(self):
        assert should_continue_review(self._config({}), "full") == "failed"

    def test_received_invalid_entry_demoted(self):
        """received だが不変条件違反 → parse_failed に降格（Leibniz P0-4）"""
        entries = {
            "pascal": {"status": "received", "verdict": None, "items": [], "parse_success": True},
            "leibniz": {"status": "received", "verdict": "APPROVE", "items": [], "parse_success": True},
        }
        # pascal は降格 → received=1 (leibniz only), parsed_fail=1 (pascal)
        # full requires 2, received < min_valid, parsed_fail > 0 → paused
        assert should_continue_review(self._config(entries), "full") == "paused"

    def test_received_sufficient_with_parse_fail(self):
        """received >= min_valid かつ parsed_fail > 0 → 通常判定続行（Leibniz P0-5）"""
        entries = {
            "pascal": {"status": "received", "verdict": "APPROVE", "items": [], "parse_success": True},
            "leibniz": {"status": "received", "verdict": "APPROVE", "items": [], "parse_success": True},
            "hanfei": {"status": "parse_failed"},
        }
        assert should_continue_review(self._config(entries), "full") == "approved"

    def test_missing_revise_count_raises(self):
        """revise_count 欠落 → KeyError（Dijkstra P1-2）"""
        entries = {
            "pascal": {"status": "received", "verdict": "P1", "items": [], "parse_success": True},
            "leibniz": {"status": "received", "verdict": "P1", "items": [], "parse_success": True},
        }
        cfg = {"current_reviews": {"entries": entries}}
        with pytest.raises(KeyError):
            should_continue_review(cfg, "full")


# --- _reset_review_requests ---

class TestResetReviewRequests:
    def _now(self):
        return datetime(2026, 3, 1, 12, 0, 0, tzinfo=JST)

    def test_resets_all_fields(self):
        spec_config = {
            "review_requests": {
                "pascal": {
                    "status": "received",
                    "sent_at": "2026-01-01T00:00:00",
                    "timeout_at": "2026-01-01T00:30:00",
                    "last_nudge_at": "2026-01-01T00:15:00",
                    "response": "some response",
                },
            },
        }
        _reset_review_requests(spec_config, self._now())
        entry = spec_config["review_requests"]["pascal"]
        assert entry["status"] == "pending"
        assert entry["sent_at"] is None
        assert entry["timeout_at"] is None
        assert entry["last_nudge_at"] is None
        assert entry["response"] is None

    def test_multiple_reviewers(self):
        spec_config = {
            "review_requests": {
                "pascal": {"status": "received", "sent_at": "x", "timeout_at": "x", "last_nudge_at": "x", "response": "x"},
                "leibniz": {"status": "timeout", "sent_at": "y", "timeout_at": "y", "last_nudge_at": None, "response": None},
            },
        }
        _reset_review_requests(spec_config, self._now())
        for entry in spec_config["review_requests"].values():
            assert entry["status"] == "pending"

    def test_empty_requests(self):
        spec_config = {"review_requests": {}}
        _reset_review_requests(spec_config, self._now())  # should not raise


# --- merge_reviews ---

class TestMergeReviews:
    def test_severity_order(self):
        r1 = SpecReviewResult(
            reviewer="pascal", verdict="P0",
            items=[
                SpecReviewItem("M-1", "minor", "§1", "t", "d", None, "pascal", "pascal:M-1"),
                SpecReviewItem("C-1", "critical", "§2", "t", "d", None, "pascal", "pascal:C-1"),
            ],
            raw_text="", parse_success=True,
        )
        report = merge_reviews([r1])
        assert report.all_items[0].severity == "critical"
        assert report.all_items[1].severity == "minor"

    def test_summary_counts(self):
        r1 = SpecReviewResult(
            reviewer="pascal", verdict="P0",
            items=[
                SpecReviewItem("C-1", "critical", "§1", "t", "d", None, "pascal", "pascal:C-1"),
            ],
            raw_text="", parse_success=True,
        )
        r2 = SpecReviewResult(
            reviewer="leibniz", verdict="P1",
            items=[
                SpecReviewItem("M-1", "major", "§2", "t", "d", None, "leibniz", "leibniz:M-1"),
            ],
            raw_text="", parse_success=True,
        )
        report = merge_reviews([r1, r2])
        assert report.summary == {"critical": 1, "major": 1, "minor": 0, "suggestion": 0}
        assert report.highest_verdict == "P0"

    def test_highest_verdict_approve(self):
        r1 = SpecReviewResult("a", "APPROVE", [], "", True)
        r2 = SpecReviewResult("b", "APPROVE", [], "", True)
        report = merge_reviews([r1, r2])
        assert report.highest_verdict == "APPROVE"

    def test_empty_reviews(self):
        report = merge_reviews([])
        assert report.all_items == []
        assert report.highest_verdict == "APPROVE"

    def test_parse_failed_ignored_in_verdict(self):
        """parse_success=False (verdict="") は highest_verdict に影響しない（Leibniz P0-6）"""
        r1 = SpecReviewResult("a", "APPROVE", [], "", True)
        r2 = SpecReviewResult("b", "", [], "", False)  # parse失敗
        report = merge_reviews([r1, r2])
        assert report.highest_verdict == "APPROVE"


# --- format_merged_report ---

class TestFormatMergedReport:
    def test_contains_header(self):
        report = MergedReviewReport([], [], {"critical": 0, "major": 0, "minor": 0, "suggestion": 0}, "APPROVE")
        text = format_merged_report(report, "1")
        assert "# Rev1 レビュー統合レポート" in text

    def test_items_listed(self):
        items = [SpecReviewItem("C-1", "critical", "§1", "Bug", "desc", "fix it", "pascal", "pascal:C-1")]
        report = MergedReviewReport(
            [SpecReviewResult("pascal", "P0", items, "", True)],
            items,
            {"critical": 1, "major": 0, "minor": 0, "suggestion": 0},
            "P0",
        )
        text = format_merged_report(report, "2")
        assert "pascal:C-1" in text
        assert "fix it" in text


# --- build_review_history_entry ---

class TestBuildReviewHistoryEntry:
    def test_basic(self):
        now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=JST)
        spec_config = {
            "current_rev": "2",
            "rev_index": 2,
            "last_commit": "abc1234",
            "current_reviews": {
                "reviewed_rev": "2",
                "entries": {
                    "pascal": {
                        "status": "received",
                        "verdict": "P0",
                        "items": [
                            {"severity": "critical"},
                            {"severity": "major"},
                        ],
                    },
                    "leibniz": {
                        "status": "timeout",
                        "verdict": None,
                        "items": [],
                    },
                },
            },
        }
        entry = build_review_history_entry(spec_config, now)
        assert entry["rev"] == "2"
        assert entry["rev_index"] == 2
        assert entry["commit"] == "abc1234"
        assert "pascal" in entry["reviews"]
        assert "leibniz" not in entry["reviews"]  # timeout excluded
        assert entry["merged_counts"]["critical"] == 1
        assert entry["merged_counts"]["major"] == 1

    def test_empty_entries(self):
        now = datetime(2026, 3, 1, tzinfo=JST)
        spec_config = {
            "current_rev": "1", "rev_index": 1, "last_commit": None,
            "current_reviews": {"entries": {}},
        }
        entry = build_review_history_entry(spec_config, now)
        assert entry["reviews"] == {}
        assert entry["merged_counts"] == {"critical": 0, "major": 0, "minor": 0, "suggestion": 0}

    def test_non_dict_item_skipped(self):
        """items 内の非dict要素はスキップ（Dijkstra P1-5）"""
        now = datetime(2026, 3, 1, tzinfo=JST)
        spec_config = {
            "current_rev": "1", "rev_index": 1, "last_commit": None,
            "current_reviews": {
                "entries": {
                    "pascal": {
                        "status": "received",
                        "verdict": "P0",
                        "items": ["not-a-dict", {"severity": "critical"}],
                    },
                },
            },
        }
        entry = build_review_history_entry(spec_config, now)
        assert entry["merged_counts"]["critical"] == 1
