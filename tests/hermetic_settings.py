"""Hermetic test settings — committed config the test suite runs against.

``config/__init__.py`` normally execs the developer's local, ``.gitignore``'d
``settings.py``. That made the whole suite depend on machine-specific values
(``PROMPT_LANG``, ``MASK_AGENT_NAMES``, ``REVIEW_MODES``, ``REVIEWERS`` ...),
so the same tests passed or failed depending on whose machine ran them.

``tests/conftest.py`` points the ``GOKRAX_SETTINGS`` env var at this file
(before ``config`` is first imported) so that config loads a fixed,
version-controlled contract instead of the developer's ``settings.py``.

This module is the single source of truth for the test-only agent/namespace
constants; ``tests/conftest.py`` imports them as the ``TEST_*`` constants.
"""

# Test-only agent / namespace contract -------------------------------------
REVIEWERS = ["reviewer1", "reviewer2", "reviewer3", "reviewer4", "reviewer5", "reviewer6"]
IMPLEMENTERS = ["implementer1", "implementer2"]
GITLAB_NAMESPACE = "testns"

REVIEWER_TIERS = {
    "regular": ["reviewer1", "reviewer3", "reviewer6"],
    "free": [],
    "short-context": ["reviewer2", "reviewer4", "reviewer5"],
}

REVIEW_MODES = {
    "full": {
        "members": ["reviewer1", "reviewer3", "reviewer5", "reviewer6"],
        "min_reviews": 4,
        "grace_period_sec": 0,
        "code": {
            "members": ["reviewer1", "reviewer3", "reviewer6"],
        },
    },
    "standard": {"members": ["reviewer1", "reviewer3", "reviewer6"], "min_reviews": 3, "grace_period_sec": 0},
    "lite": {"members": ["reviewer1", "reviewer3"], "min_reviews": 2, "grace_period_sec": 0},
    "min": {"members": ["reviewer1"], "min_reviews": 1, "grace_period_sec": 0},
    "skip": {"members": [], "min_reviews": 0, "grace_period_sec": 0},
    "no_minrev": {"members": ["reviewer1", "reviewer3"], "grace_period_sec": 0},
}

# Previously-leaking knobs, pinned explicitly so the test contract no longer
# depends on the developer's settings.py.
from pathlib import PurePosixPath  # noqa: E402

PROMPT_LANG = "en"
MASK_AGENT_NAMES = True
OWNER_NAME = "M"
GOKRAX_CLI = PurePosixPath("/usr/local/bin/gokrax")

# Discord — pinned to fixed, fake test values. The suite's baseline assumed
# Discord was configured (the developer's settings.py sets these), so keep it
# configured here; actual posts are mocked in conftest. MERGE_APPROVER_DISCORD_ID
# must match tests/test_merge_summary.py::TestWatchdogMergeSummary.M_ID.
DISCORD_CHANNEL = "test-discord-channel"
MERGE_APPROVER_DISCORD_ID = "100000000000000007"
