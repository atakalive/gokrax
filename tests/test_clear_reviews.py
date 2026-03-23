"""tests/test_clear_reviews.py — clear_reviews の粒度テスト"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.reviewer import clear_reviews


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

    def test_npass_intermediate_approve_cleared(self):
        """NPASS 中間パス（pass=1, target_pass=2）の APPROVE はクリアされる"""
        batch = [
            {
                "issue": 1,
                "design_reviews": {
                    "pascal": {"verdict": "APPROVE", "at": "t", "pass": 1, "target_pass": 2},
                },
                "design_revised": True,
            },
        ]
        clear_reviews(batch, "design_reviews", "design_revised")
        assert "pascal" not in batch[0]["design_reviews"]

    def test_npass_final_pass_approve_kept(self):
        """NPASS 最終パス（pass=2, target_pass=2）の APPROVE は保持される"""
        batch = [
            {
                "issue": 1,
                "design_reviews": {
                    "pascal": {"verdict": "APPROVE", "at": "t", "pass": 2, "target_pass": 2},
                },
                "design_revised": True,
            },
        ]
        clear_reviews(batch, "design_reviews", "design_revised")
        assert "pascal" in batch[0]["design_reviews"]

    def test_single_pass_approve_kept(self):
        """1-pass（pass/target_pass キーなし）の APPROVE は保持される（既存動作不変）"""
        batch = [
            {
                "issue": 1,
                "code_reviews": {
                    "leibniz": {"verdict": "APPROVE", "at": "t"},
                },
                "code_revised": True,
            },
        ]
        clear_reviews(batch, "code_reviews", "code_revised")
        assert "leibniz" in batch[0]["code_reviews"]

    def test_npass_mixed_reviewers(self):
        """NPASS 中間パス APPROVE はクリア、同一 Issue の P1 もクリア、1-pass APPROVE は保持"""
        batch = [
            {
                "issue": 1,
                "design_reviews": {
                    "pascal": {"verdict": "APPROVE", "at": "t", "pass": 1, "target_pass": 2},
                    "euler": {"verdict": "P1", "at": "t"},
                    "leibniz": {"verdict": "APPROVE", "at": "t"},
                },
                "design_revised": True,
            },
        ]
        clear_reviews(batch, "design_reviews", "design_revised")
        assert "pascal" not in batch[0]["design_reviews"]  # NPASS 中間 → クリア
        assert "euler" not in batch[0]["design_reviews"]    # P1 → クリア
        assert "leibniz" in batch[0]["design_reviews"]      # 1-pass APPROVE → 保持

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
