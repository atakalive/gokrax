"""tests/test_clear_reviews.py — clear_reviews の粒度テスト"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from watchdog import clear_reviews


class TestClearReviews:

    def test_p0_issue_cleared_approve_issue_kept(self):
        """P0 Issue と APPROVE Issue → P0のみクリア"""
        batch = [
            {
                "issue": 1,
                "code_reviews": {
                    "pascal": {"verdict": "P0", "at": "t"},
                    "leibniz": {"verdict": "APPROVE", "at": "t"},
                },
                "code_revised": True,
            },
            {
                "issue": 2,
                "code_reviews": {
                    "pascal": {"verdict": "APPROVE", "at": "t"},
                    "leibniz": {"verdict": "APPROVE", "at": "t"},
                },
                "code_revised": True,
            },
        ]
        clear_reviews(batch, "code_reviews", "code_revised")

        # P0 Issue のレビューがクリアされている
        assert batch[0]["code_reviews"] == {}
        # APPROVE Issue のレビューは残っている
        assert "pascal" in batch[1]["code_reviews"]
        assert "leibniz" in batch[1]["code_reviews"]
        # revised_key は両方から削除
        assert "code_revised" not in batch[0]
        assert "code_revised" not in batch[1]

    def test_all_p0_cleared(self):
        """P0 Issue のみ → 全クリア"""
        batch = [
            {
                "issue": 1,
                "design_reviews": {"pascal": {"verdict": "P0", "at": "t"}},
                "design_revised": True,
            },
            {
                "issue": 2,
                "design_reviews": {"pascal": {"verdict": "REJECT", "at": "t"}},
                "design_revised": True,
            },
        ]
        clear_reviews(batch, "design_reviews", "design_revised")

        assert batch[0]["design_reviews"] == {}
        assert batch[1]["design_reviews"] == {}

    def test_all_approve_nothing_cleared(self):
        """APPROVE Issue のみ → 何もクリアされない"""
        batch = [
            {
                "issue": 1,
                "code_reviews": {
                    "pascal": {"verdict": "APPROVE", "at": "t"},
                    "leibniz": {"verdict": "APPROVE", "at": "t"},
                },
                "code_revised": True,
            },
        ]
        clear_reviews(batch, "code_reviews", "code_revised")

        assert len(batch[0]["code_reviews"]) == 2
        assert "code_revised" not in batch[0]

    def test_revised_key_removed_from_all(self):
        """revised_key は全 Issue から削除"""
        batch = [
            {"issue": 1, "code_reviews": {"pascal": {"verdict": "P0", "at": "t"}}, "code_revised": True},
            {"issue": 2, "code_reviews": {"pascal": {"verdict": "APPROVE", "at": "t"}}, "code_revised": True},
            {"issue": 3, "code_reviews": {}, "code_revised": True},
        ]
        clear_reviews(batch, "code_reviews", "code_revised")

        for issue in batch:
            assert "code_revised" not in issue

    def test_p1_verdict_kept(self):
        """P1 verdict → レビュー保持"""
        batch = [
            {
                "issue": 1,
                "code_reviews": {
                    "pascal": {"verdict": "P1", "at": "t"},
                    "leibniz": {"verdict": "APPROVE", "at": "t"},
                },
                "code_revised": True,
            },
        ]
        clear_reviews(batch, "code_reviews", "code_revised")

        # P1はクリア対象ではないのでレビュー保持
        assert len(batch[0]["code_reviews"]) == 2
        assert "code_revised" not in batch[0]
