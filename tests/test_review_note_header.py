"""tests/test_review_note_header.py — format_review_note_header テスト (Issue #276)"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notify import format_review_note_header  # noqa: E402


class TestFormatReviewNoteHeader:
    """format_review_note_header の出力テスト"""

    def test_round1_pass1(self):
        """Round 1, target_pass 1 → 'Round 1' のみ"""
        result = format_review_note_header("Reviewer 1", "P1", "design", 1, 1)
        assert result == "[Reviewer 1] P1 (design review) Round 1"

    def test_round2_pass1(self):
        """Round 2, target_pass 1 → 'Round 2' のみ"""
        result = format_review_note_header("Reviewer 1", "P1", "design", 2, 1)
        assert result == "[Reviewer 1] P1 (design review) Round 2"

    def test_round1_2pass(self):
        """Round 1, target_pass 2 → 'Round 1, 2-Pass'"""
        result = format_review_note_header("Reviewer 1", "APPROVE", "code", 1, 2)
        assert result == "[Reviewer 1] APPROVE (code review) Round 1, 2-Pass"

    def test_round2_2pass(self):
        """Round 2, target_pass 2 → 'Round 2, 2-Pass'"""
        result = format_review_note_header("Reviewer 1", "P1", "design", 2, 2)
        assert result == "[Reviewer 1] P1 (design review) Round 2, 2-Pass"

    def test_round0_no_round(self):
        """round_num=0 → Round 付与しない"""
        result = format_review_note_header("Reviewer 1", "APPROVE", "code", 0, 1)
        assert result == "[Reviewer 1] APPROVE (code review)"

    def test_code_review_phase(self):
        """phase='code' が正しく表示される"""
        result = format_review_note_header("Reviewer 2", "P2", "code", 3, 1)
        assert result == "[Reviewer 2] P2 (code review) Round 3"

    def test_3pass(self):
        """3-Pass"""
        result = format_review_note_header("Reviewer 1", "P0", "design", 1, 3)
        assert result == "[Reviewer 1] P0 (design review) Round 1, 3-Pass"
