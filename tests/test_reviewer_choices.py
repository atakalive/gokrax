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
    p.add_argument("--reviewer", required=True, choices=config.REVIEWERS)
    p.add_argument("--verdict", required=True, choices=config.VALID_VERDICTS)
    p.add_argument("--summary", default="")
    return parser


class TestReviewerChoices:

    def test_valid_reviewer_reviewer1_accepted(self):
        """--reviewer reviewer1 は受け付けられること。"""
        parser = _build_review_parser()
        args = parser.parse_args([
            "review", "--project", "proj", "--issue", "1",
            "--reviewer", "reviewer1", "--verdict", "APPROVE",
        ])
        assert args.reviewer == "reviewer1"

    def test_valid_reviewer_reviewer3_accepted(self):
        """--reviewer reviewer3 は受け付けられること。"""
        parser = _build_review_parser()
        args = parser.parse_args([
            "review", "--project", "proj", "--issue", "1",
            "--reviewer", "reviewer3", "--verdict", "APPROVE",
        ])
        assert args.reviewer == "reviewer3"

    def test_invalid_reviewer_rejected(self):
        """--reviewer hoge は SystemExit（argparse エラー）になること。"""
        parser = _build_review_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "review", "--project", "proj", "--issue", "1",
                "--reviewer", "hoge", "--verdict", "APPROVE",
            ])

    def test_agents_keys_in_reviewers_and_implementers(self):
        """AGENTS のキーが REVIEWERS + IMPLEMENTERS に含まれること。"""
        import config
        all_names = set(config.REVIEWERS + config.IMPLEMENTERS)
        for key in config.AGENTS:
            assert key in all_names, \
                f"AGENTS のキー '{key}' が REVIEWERS + IMPLEMENTERS に含まれていない"

    def test_reviewers_in_config(self):
        """config.REVIEWERS が存在し空でないこと。"""
        import config
        assert hasattr(config, "REVIEWERS")
        assert len(config.REVIEWERS) > 0
