"""Tests for parse_issue_args helper."""

import pytest
from commands.dev import parse_issue_args


def test_space_separated():
    assert parse_issue_args(["33", "34"]) == [33, 34]


def test_comma_separated():
    assert parse_issue_args(["33,34"]) == [33, 34]


def test_mixed():
    assert parse_issue_args(["33,34", "35"]) == [33, 34, 35]


def test_single():
    assert parse_issue_args(["33"]) == [33]


def test_comma_with_spaces():
    """Comma with surrounding spaces (argparse won't normally produce this, but handle defensively)"""
    assert parse_issue_args(["33, 34"]) == [33, 34]


def test_invalid_non_numeric():
    with pytest.raises(SystemExit, match="Invalid issue number"):
        parse_issue_args(["abc"])


def test_invalid_mixed_with_non_numeric():
    with pytest.raises(SystemExit, match="Invalid issue number"):
        parse_issue_args(["33,abc"])


def test_empty_list():
    with pytest.raises(SystemExit, match="No issue numbers"):
        parse_issue_args([])


def test_trailing_comma():
    """Trailing comma ignores empty element and works correctly"""
    assert parse_issue_args(["33,34,"]) == [33, 34]
