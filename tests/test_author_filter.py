"""Tests for engine/filter.py — GitLab author filtering (Issue #278)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from engine.filter import (
    UnauthorizedAuthorError,
    require_issue_author,
    validate_comment_author,
    validate_issue_author,
)


# ---------------------------------------------------------------------------
# validate_issue_author
# ---------------------------------------------------------------------------


def test_validate_issue_author_allowed(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
    assert validate_issue_author({"author": {"username": "owner"}}) is True


def test_validate_issue_author_allowed_extra(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ("collab",))
    assert validate_issue_author({"author": {"username": "collab"}}) is True


def test_validate_issue_author_rejected(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
    assert validate_issue_author({"author": {"username": "attacker"}}) is False


def test_validate_issue_author_missing_author(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
    assert validate_issue_author({}) is False
    assert validate_issue_author({"author": {}}) is False
    assert validate_issue_author({"author": None}) is False
    assert validate_issue_author({"author": "string"}) is False


# ---------------------------------------------------------------------------
# require_issue_author
# ---------------------------------------------------------------------------


def test_require_issue_author_raises(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
    with pytest.raises(UnauthorizedAuthorError, match="attacker"):
        require_issue_author({"author": {"username": "attacker"}})


# ---------------------------------------------------------------------------
# Integration: _fetch_issue_info
# ---------------------------------------------------------------------------


def test_fetch_issue_info_rejects_unauthorized(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
    import commands.dev
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(
        {"title": "evil", "state": "opened", "author": {"username": "attacker"}}
    )
    with patch("commands.dev.subprocess.run", return_value=mock_result):
        with pytest.raises(UnauthorizedAuthorError):
            commands.dev._fetch_issue_info(1, "ns/proj")


def test_fetch_issue_info_allows_authorized(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
    import commands.dev
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(
        {"title": "legit", "state": "opened", "author": {"username": "owner"}}
    )
    with patch("commands.dev.subprocess.run", return_value=mock_result):
        result = commands.dev._fetch_issue_info(1, "ns/proj")
    assert result == ("legit", "opened")


# ---------------------------------------------------------------------------
# Integration: fetch_issue_body
# ---------------------------------------------------------------------------


def test_fetch_issue_body_rejects_unauthorized(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
    import notify
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(
        {"description": "evil body", "author": {"username": "attacker"}}
    )
    with patch("notify.subprocess.run", return_value=mock_result):
        with pytest.raises(UnauthorizedAuthorError):
            notify.fetch_issue_body(1, "ns/proj")


def test_fetch_issue_body_allows_authorized(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
    import notify
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(
        {"description": "legit body", "author": {"username": "owner"}}
    )
    with patch("notify.subprocess.run", return_value=mock_result):
        result = notify.fetch_issue_body(1, "ns/proj")
    assert result == "legit body"


# ---------------------------------------------------------------------------
# validate_comment_author
# ---------------------------------------------------------------------------


def test_validate_comment_author_allowed(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
    assert validate_comment_author({"author": {"username": "owner"}}) is True


def test_validate_comment_author_rejected(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
    assert validate_comment_author({"author": {"username": "stranger"}}) is False


def test_validate_comment_author_none_author(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
    assert validate_comment_author({"author": None}) is False


def test_validate_comment_author_missing_author(monkeypatch):
    monkeypatch.setattr("config.GITLAB_NAMESPACE", "owner")
    monkeypatch.setattr("config.ALLOWED_GITLAB_AUTHORS", ())
    assert validate_comment_author({}) is False
