"""gokrax settings — user configuration
Copy this file to settings.py and edit values.
"""
from pathlib import Path, PurePosixPath
from datetime import timezone, timedelta  # noqa: F401 — used by commented-out settings

# ===========================================================================
# Required — fill in your values
# ===========================================================================
DISCORD_CHANNEL = ""
DISCORD_BOT_TOKEN = ""
MERGE_APPROVER_DISCORD_ID = ""
BOT_USER_ID = ""
GLAB_BIN = "/usr/bin/glab"
GOKRAX_CLI = PurePosixPath("/path/to/gokrax")
PIPELINES_DIR = Path.home() / ".openclaw/shared/pipelines"

AGENTS = {
    "reviewer1": "agent:reviewer1:main",
    "reviewer2": "agent:reviewer2:main",
}

# ===========================================================================
# Recommended — adjust to your setup
# ===========================================================================
OWNER_NAME: str = "User"
PROMPT_LANG: str = "en"
CC_MODEL_PLAN = "sonnet"
CC_MODEL_IMPL = "sonnet"
MIN_REVIEWS = 3

REVIEWER_TIERS: dict = {
    "regular": [],
    "free": [],
    "short-context": [],
}

REVIEW_MODES = {
    "full": {
        "members": [],
        "min_reviews": 4,
        "grace_period_sec": 0,
    },
    "standard": {
        "members": [],
        "min_reviews": 3,
        "grace_period_sec": 0,
    },
    "lite": {
        "members": [],
        "min_reviews": 2,
        "grace_period_sec": 0,
    },
    "skip": {
        "members": [],
        "min_reviews": 0,
        "grace_period_sec": 0,
    },
}

TEST_CONFIG: dict = {
    "myproject": {
        "test_command": "cd /path/to/project && python3 -m pytest -x --tb=short",
        "test_timeout": 300,
    },
}

# ===========================================================================
# Advanced — uncomment and edit if needed
# ===========================================================================
# AGENT_SEND_TIMEOUT = 30
# DISCORD_POST_TIMEOUT = 10
# GLAB_TIMEOUT = 15
# INACTIVE_THRESHOLD_SEC = 303
# POST_NEW_COMMAND_WAIT_SEC = 30
# MAX_TEST_RETRY: int = 4
# MAX_DIFF_CHARS: int = 5_000_000
# REVIEW_FILE_WRITE_RETRIES: int = 3
# REVIEW_FILE_WRITE_RETRY_DELAY: float = 2.0
# MERGE_SUMMARY_FOOTER = "\n---\n✅ Reply \"OK\" to this message to execute the merge."
# ALLOWED_COMMAND_USER_IDS: tuple = (MERGE_APPROVER_DISCORD_ID,)
# SKILLS: dict[str, str] = {"example-skill": str(Path.home() / ".openclaw/skills/example-skill/SKILL.md"),}
# AGENT_SKILLS: dict[str, list[str]] = {"reviewer1": ["example-skill"],}
# MAX_SKILL_CHARS: int = 30_000
