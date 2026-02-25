"""tests/test_clear_reviews.py — clear_reviews の粒度テスト"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from watchdog import clear_reviews


class TestClearReviews:

    def test_p0_reviewer_cleared_approve_reviewer_kept(self):
        """P0を出したレビュアーだけクリア、APPROVEレビュアーは保持"""
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

        # P0を出したpascalだけクリア、leibnizのAPPROVEは残る
        assert "pascal" not in batch[0]["code_reviews"]
        assert "leibniz" in batch[0]["code_reviews"]
        # 全APPROVE Issueはそのまま
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

    def test_p1_verdict_cleared(self):
        """P1 verdict → レビュークリア（Issue #36修正後）"""
        batch = [
            {
                "issue": 1,
                "code_reviews": {
                    "pascal": {"verdict": "P1", "at": "t", "summary": "minor issue"},
                    "leibniz": {"verdict": "APPROVE", "at": "t"},
                },
                "code_revised": True,
            },
        ]
        clear_reviews(batch, "code_reviews", "code_revised")

        # P1クリア、APPROVEは保持
        assert "pascal" not in batch[0]["code_reviews"]
        assert "leibniz" in batch[0]["code_reviews"]
        assert "code_revised" not in batch[0]
