"""engine/filter — GitLab author filtering for prompt injection prevention."""

import config


def _allowed_authors() -> frozenset[str]:
    return frozenset({config.GITLAB_NAMESPACE} | set(config.ALLOWED_GITLAB_AUTHORS))


class UnauthorizedAuthorError(Exception):
    """Raised when a GitLab entity is authored by a non-allowed user."""

    pass


def validate_issue_author(data: dict) -> bool:
    """Check whether the issue author is in the allowed list.

    Args:
        data: glab issue show --output json dict.
              Reads data["author"]["username"].

    Returns:
        True if the author is allowed, False otherwise.
    """
    author = data.get("author")
    if not isinstance(author, dict):
        return False
    return author.get("username") in _allowed_authors()


def require_issue_author(data: dict) -> None:
    """Strict version of validate_issue_author. Raises on unauthorized author.

    Args:
        data: glab issue show --output json dict.

    Raises:
        UnauthorizedAuthorError: if the author is not in the allowed list.
    """
    author = data.get("author")
    username = author.get("username", "<unknown>") if isinstance(author, dict) else "<unknown>"
    if not validate_issue_author(data):
        raise UnauthorizedAuthorError(
            f"Unauthorized issue author: {username} "
            f"(allowed: {_allowed_authors()})"
        )
