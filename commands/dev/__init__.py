import subprocess  # noqa: F401 — tests patch commands.dev.subprocess
import time  # noqa: F401 — tests patch commands.dev.time

from config import (  # noqa: F401
    PIPELINES_DIR, GLAB_BIN,
    VALID_STATES, VALID_TRANSITIONS, MAX_BATCH,
    GLAB_TIMEOUT, REVIEWERS, REVIEW_MODES, LOCAL_TZ,
    WATCHDOG_LOOP_PIDFILE, WATCHDOG_LOOP_LOCKFILE,
    STATE_PHASE_MAP,
    GITLAB_NAMESPACE, IMPLEMENTERS,
    GOKRAX_CLI, OWNER_NAME,
)
from pipeline_io import (  # noqa: F401
    load_pipeline, save_pipeline, update_pipeline,
    add_history, now_iso, get_path, find_issue,
    clear_pending_notification, merge_pending_notifications,
)
from engine.filter import require_issue_author, UnauthorizedAuthorError  # noqa: F401
from engine.fsm import get_notification_for_state  # noqa: F401
from notify import (  # noqa: F401
    notify_implementer, notify_reviewers, notify_discord,
    resolve_reviewer_arg,
    send_to_agent_queued,
    post_gitlab_note as _post_gitlab_note,
    mask_agent_name, format_review_note_header,
)

from commands.dev.helpers import (    # noqa: F401
    VERDICT_SEVERITY, RISK_DISPLAY,
    parse_issue_args, _log, _masked_reviewer, _reset_to_idle,
)
from commands.dev.lifecycle import (  # noqa: F401
    get_status_text, cmd_status, cmd_init, cmd_enable, cmd_disable, cmd_extend,
    _fetch_open_issues, _fetch_issue_info, cmd_triage,
    cmd_start, cmd_transition, cmd_reset,
    cmd_review_mode, cmd_exclude, cmd_merge_summary, cmd_ok,
    cmd_get_comments, cmd_blocked_report,
)
from commands.dev.review import (     # noqa: F401
    _update_issue_title_with_assessment,
    cmd_review, cmd_dispute, cmd_flag,
    cmd_commit, cmd_cc_start, cmd_plan_done, cmd_assess_done,
    cmd_design_revise, cmd_code_revise,
)
from commands.dev.queue import (      # noqa: F401
    cmd_qrun, _get_running_info, get_qstatus_text,
    cmd_qstatus, cmd_qadd, cmd_qdel, cmd_qedit,
)


# ---------------------------------------------------------------------------
# Patchability bridge: make patch("commands.dev.X") intercept submodule calls
# ---------------------------------------------------------------------------
# When commands/dev.py was a monolithic module, tests could patch any name on
# it (e.g. patch("commands.dev.notify_implementer")) and the patch would
# intercept calls from within the module because all code shared one __dict__.
#
# Now that commands/dev/ is a package, each submodule has its own __dict__.
# To preserve the old patching semantics without changing any test files,
# we replace the submodule bindings for patchable names with _PatchableRef
# proxy objects.  Each proxy looks up the current value from this package's
# namespace (globals()) at call time, so patch("commands.dev.X") is honored.
# ---------------------------------------------------------------------------

class _PatchableRef:
    """Callable proxy: always resolves through the package namespace."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __call__(self, *args, **kwargs):
        return globals()[self._name](*args, **kwargs)


import commands.dev.lifecycle as _lc  # noqa: E402
import commands.dev.review as _rv  # noqa: E402
import commands.dev.queue as _q  # noqa: E402

# lifecycle.py — imported callables
for _n in (
    "notify_implementer", "notify_reviewers", "notify_discord",
    "resolve_reviewer_arg",
    "load_pipeline", "save_pipeline", "update_pipeline",
    "add_history", "now_iso", "get_path", "find_issue",
    "clear_pending_notification", "merge_pending_notifications",
    "get_notification_for_state", "require_issue_author",
    "parse_issue_args", "_masked_reviewer", "_reset_to_idle",
    # defined functions that tests patch through commands.dev
    "cmd_start", "cmd_transition",
    "cmd_triage", "_fetch_issue_info", "_fetch_open_issues",
):
    if _n in _lc.__dict__:
        _lc.__dict__[_n] = _PatchableRef(_n)

# review.py — imported callables
for _n in (
    "_post_gitlab_note", "send_to_agent_queued",
    "mask_agent_name", "resolve_reviewer_arg", "format_review_note_header",
    "load_pipeline", "update_pipeline", "now_iso", "get_path", "find_issue",
    "parse_issue_args", "_log", "_masked_reviewer",
):
    if _n in _rv.__dict__:
        _rv.__dict__[_n] = _PatchableRef(_n)

# queue.py — imported callables
for _n in ("update_pipeline", "add_history", "get_path", "_reset_to_idle"):
    if _n in _q.__dict__:
        _q.__dict__[_n] = _PatchableRef(_n)

# Cleanup temp names from package namespace
del _lc, _rv, _q, _n
