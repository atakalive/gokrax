"""gokrax settings — user configuration
Copy this file to settings.py and edit values.
"""
import os
from pathlib import Path, PurePosixPath
from datetime import timezone, timedelta  # noqa: F401 — used by commented-out settings

# ===========================================================================
# Required — fill in your values
# ===========================================================================
DISCORD_CHANNEL = ""       # Discord channel ID for posting updates
DISCORD_BOT_TOKEN = ""     # Discord bot token with permissions to receive/post in the above channel
ANNOUNCE_BOT_USER_ID = ""  # Discord bot user ID of the bot (can be obtained by right click menu on the bot name)
MERGE_APPROVER_DISCORD_ID = ""  # Your Discord user ID for approving merges
COMMAND_BOT_USER_ID = ""        # If you send commands via 3rd-party Discord tool (WatcherB etc.), include its bot user ID here

GLAB_BIN = "/usr/bin/glab"
GITLAB_NAMESPACE: str = "YOUR_NAMESPACE"  # i.e., gitlab.com/YOUR_NAMESPACE/ProjectName/
GOKRAX_CLI = PurePosixPath("/path/to/gokrax")  # may be symbolic link


GATEWAY_PORT = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789"))  # openclaw gateway port (localhost)
PIPELINES_DIR = Path.home() / ".openclaw/shared/pipelines"

REVIEWERS = ["reviewer1", "reviewer2"]
IMPLEMENTERS = ["impl1"]
# AGENTS dictionary is auto-generated from REVIEWERS + IMPLEMENTERS in config.

# Reviewer tiers means that their infrastructure capability
# Regular: Stable connection, enough context length
# Free: Limited daily token usage, may be disconnected in workflow. (Author did not test them well)
# Short-context: Shorter context length. Local LLM etc. (64k-ctx model was tested in single issue)
REVIEWER_TIERS: dict = {
    "regular": ["reviewer1", "reviewer2"],
    "short-context": [],
    "free": [],
}


# ===========================================================================
# Recommended — adjust to your setup
# ===========================================================================
OWNER_NAME: str = "User"
PROMPT_LANG: str = "en"  # Available: en, ja
LOCAL_TZ = timezone(timedelta(hours=0))  # UTC = 0
CC_MODEL_PLAN = "sonnet"
CC_MODEL_IMPL = "sonnet"
MASK_AGENT_NAMES = True  # if False, your agent names are shown in GitLab notes. (Default: True)

DEFAULT_QUEUE_OPTIONS: dict[str, bool | str] = {
    "skip_cc_plan": True,       # Claude Code Plan mode is skipped in IMPLEMENTATION. (Default: True)
    "no-cc": False,             # Claude Code is not used in IMPLEMENTATION. (Default: False)
    "keep_ctx_intra": True,     # Context is kept between DESIGN and CODE phases. (Default: True)
    "skip_test": True,          # CODE_TEST is skipped. (Default: True)
    "skip_assess": True,        # ASSESSMENT is skipped. (Default: True)
}

# Per-project overrides for DEFAULT_QUEUE_OPTIONS.
# Keys present here override DEFAULT_QUEUE_OPTIONS for the specified project.
# Keys not present fall through to DEFAULT_QUEUE_OPTIONS.
# If a project is not listed, DEFAULT_QUEUE_OPTIONS is used as-is.
# Supports the same key space as DEFAULT_QUEUE_OPTIONS:
#   bool keys (e.g. "skip_test": False), alias keys (e.g. "impl": "opus"),
#   and alias=value keys (e.g. "impl=opus": True).
PROJECT_QUEUE_OPTIONS: dict[str, dict[str, bool | str]] = {
    # "MyProject": {"skip_assess": False},      # override skip_assess for this project
    # "SimpleProject": {"skip_assess": True},   # explicitly keep default
}

REVIEW_MODES = {
    "full": {
        "members": ["reviewer1", "reviewer2", "reviewer3", "reviewer4"],
        "min_reviews": 4,
        "grace_period_sec": 0,
    },
    "standard": {
        "members": ["reviewer1", "reviewer2", "reviewer3"],
        "min_reviews": 3,
        "grace_period_sec": 0,
    },
    "lite": {
        "members": ["reviewer1", "reviewer2"],
        "min_reviews": 2,
        "grace_period_sec": 0,
    },
    "skip": {
        "members": [],
        "min_reviews": 0,
        "grace_period_sec": 0,
    },
    "lite3": {
        "members": ["reviewer1", "reviewer2", "reviewer3"],
        "min_reviews": 2,
        "grace_period_sec": 300,
    },
    "standard-x2": {
        "members": ["reviewer1", "reviewer2", "reviewer3"],
        "min_reviews": 3,
        "grace_period_sec": 0,
        # n_pass: per-reviewer multi-pass count (positive int). Reviewers not listed default to 1.
        "n_pass": {"reviewer1": 2, "reviewer3": 2},
    },
    # --- Phase override example ---
    # You can override per-phase (design/code) settings within any mode.
    # Fields not overridden inherit from the mode's top-level defaults.
    # "full-custom": {
    #     "members": ["reviewer1", "reviewer2", "reviewer3", "reviewer4"],
    #     "min_reviews": 4,
    #     "grace_period_sec": 0,
    #     # Code review uses fewer reviewers + multi-pass for one reviewer
    #     "code": {
    #         "members": ["reviewer1", "reviewer2", "reviewer3"],
    #         "n_pass": {"reviewer1": 2},
    #     },
    #     # Design review uses all members (inherits top-level, no override needed)
    # },
}

# ===========================================================================
# Advanced — uncomment and edit if needed
#            Other settings in config directory can be overridden in settings.py
# ===========================================================================
# AGENT_BACKEND = "openclaw"    # "openclaw" (default) or "pi"
# PI_BIN = "pi"                 # path to pi CLI binary
# PI_START_GRACE_SEC = 30       # seconds to treat a just-spawned pi agent as active
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
# ALLOWED_COMMAND_USER_IDS: tuple[str, ...] = (MERGE_APPROVER_DISCORD_ID, COMMAND_BOT_USER_ID,)
# SKILLS: dict[str, str] = {"example-skill": str(Path.home() / ".openclaw/skills/example-skill/SKILL.md"),}
# AGENT_SKILLS: dict[str, dict[str, list[str]]] = {
#     "reviewer1": {
#         "design": [],
#         "code": ["example-skill"],
#     },
# }
# PROJECT_SKILLS: dict[str, dict[str, list[str]]] = {
#     "myproject": {
#         "design": ["example-skill"],
#         "code": ["example-skill"],
#     },
# }
# PROJECT_RISK_FILES: dict[str, str] = {
#    "MyProject": "/home/user/custom/domain_risk.md",
# }
# MAX_SKILL_CHARS: int = 30_000
# TEST_CONFIG: dict = {
#     "myproject": {
#         "test_command": "cd /path/to/project && python3 -m pytest -x --tb=short",
#         "test_timeout": 300,
#     },
# }
