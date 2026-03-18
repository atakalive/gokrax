"""tests/test_reviewer_choices.py — gokrax review --reviewer の choices 制限テスト"""

import sys
import argparse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _build_review_parser():
    """gokrax の review サブコマンド用 argparse パーサーを単独で構築。"""
    import gokrax
    import config
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("review")
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--reviewer", required=True, choices=config.ALLOWED_REVIEWERS)
    p.add_argument("--verdict", required=True, choices=config.VALID_VERDICTS)
    p.add_argument("--summary", default="")
    return parser


class TestReviewerChoices:

    def test_valid_reviewer_pascal_accepted(self):
        """--reviewer pascal は受け付けられること。"""
        parser = _build_review_parser()
        args = parser.parse_args([
            "review", "--project", "proj", "--issue", "1",
            "--reviewer", "pascal", "--verdict", "APPROVE",
        ])
        assert args.reviewer == "pascal"

    def test_valid_reviewer_kaneko_accepted(self):
        """--reviewer kaneko（実装者）は受け付けられること。"""
        parser = _build_review_parser()
        args = parser.parse_args([
            "review", "--project", "proj", "--issue", "1",
            "--reviewer", "kaneko", "--verdict", "APPROVE",
        ])
        assert args.reviewer == "kaneko"

    def test_invalid_reviewer_rejected(self):
        """--reviewer hoge は SystemExit（argparse エラー）になること。"""
        parser = _build_review_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "review", "--project", "proj", "--issue", "1",
                "--reviewer", "hoge", "--verdict", "APPROVE",
            ])

    def test_allowed_reviewers_contains_all_agents(self):
        """ALLOWED_REVIEWERS が AGENTS の全キーを含むこと。"""
        import config
        for key in config.AGENTS:
            assert key in config.ALLOWED_REVIEWERS, \
                f"AGENTS のキー '{key}' が ALLOWED_REVIEWERS に含まれていない"

    def test_allowed_reviewers_in_config(self):
        """config.ALLOWED_REVIEWERS が存在し空でないこと。"""
        import config
        assert hasattr(config, "ALLOWED_REVIEWERS")
        assert len(config.ALLOWED_REVIEWERS) > 0
