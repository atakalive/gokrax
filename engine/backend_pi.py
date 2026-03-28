"""engine/backend_pi.py - pi backend for agent communication.

Provides send/ping/is_inactive/reset_session for agents running via the ``pi`` CLI.

Liveness invariant:
    pi activity is determined by two observable states:
    1. **starting grace**: a recent successful ``send()`` within PI_START_GRACE_SEC
    2. **session persistence**: the last filesystem modification time of the session jsonl file

    The agent is considered active if either state says active.

    The starting marker is process-local to the current gokrax Python process.
    Cross-process consistency relies solely on session-file mtime as the shared
    liveness signal.  The starting marker is only a local anti-race aid for the
    process that executed ``send()``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

import config
from config import (
    PI_SESSIONS_DIR,
    INACTIVE_THRESHOLD_SEC,
    AGENT_PROFILES_DIR,
    PI_AGENT_CONFIG,
    PROJECT_ROOT,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported backend values (centralized domain)
# ---------------------------------------------------------------------------
SUPPORTED_BACKENDS: frozenset[str] = frozenset({"openclaw", "pi"})

# ---------------------------------------------------------------------------
# Process-local starting-state marker
# ---------------------------------------------------------------------------
_starting_markers: dict[str, float] = {}

# ---------------------------------------------------------------------------
# Per-agent config (agents/config_pi.json)
# ---------------------------------------------------------------------------
_agent_config_cache: dict[str, dict[str, str]] | None = None


def _load_config() -> dict[str, dict[str, str]]:
    """Load and cache agents/config_pi.json. Called once per process lifetime.

    Returns:
        Dict mapping agent_id -> {provider?, model?, thinking?, tools?}.
        Returns empty dict if:
        - File does not exist
        - File is empty or contains only whitespace
        - JSON decode fails (log warning)
        - JSON root is not a dict (log warning)

        Non-dict entries within the root object are silently filtered out
        (with a warning listing skipped keys). This prevents AttributeError
        when send() calls profile.get() on a string or other non-dict value.

    The returned dict is the cached reference. Callers must not mutate it.
    """
    global _agent_config_cache
    if _agent_config_cache is not None:
        return _agent_config_cache

    try:
        text = PI_AGENT_CONFIG.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        _agent_config_cache = {}
        return _agent_config_cache
    except OSError as exc:
        logger.warning("Failed to read %s: %s", PI_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not text:
        _agent_config_cache = {}
        return _agent_config_cache

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in %s: %s", PI_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not isinstance(parsed, dict):
        logger.warning(
            "Expected JSON object in %s, got %s",
            PI_AGENT_CONFIG, type(parsed).__name__,
        )
        _agent_config_cache = {}
        return _agent_config_cache

    # Filter out non-dict entries (e.g. shorthand "agent": "model-name" typos)
    _agent_config_cache = {
        k: v for k, v in parsed.items() if isinstance(v, dict)
    }
    if len(_agent_config_cache) < len(parsed):
        skipped = [k for k, v in parsed.items() if not isinstance(v, dict)]
        logger.warning(
            "Skipped non-dict entries in %s: %s", PI_AGENT_CONFIG, skipped,
        )
    return _agent_config_cache


def _session_path(agent_id: str) -> Path:
    """Return the deterministic per-agent session file path.

    Returns an absolute path: ``PI_SESSIONS_DIR / "{agent_id}.jsonl"``.
    """
    return PI_SESSIONS_DIR / f"{agent_id}.jsonl"


def send(agent_id: str, message: str, timeout: int) -> bool:
    """Fire-and-forget subprocess launch of ``pi``.

    Args:
        agent_id: Internal gokrax agent name.
        message: Message to write to pi's stdin.
        timeout: Kept for interface parity with the openclaw backend; unused
            by pi fire-and-forget spawn.

    Returns:
        True if process spawn and stdin handoff succeeded, False otherwise.
    """
    if config.DRY_RUN:
        logger.info("[dry-run] pi send skipped (agent=%s)", agent_id)
        return True

    PI_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [config.PI_BIN, "--session", str(_session_path(agent_id))]

    config_data = _load_config()
    profile = config_data.get(agent_id, {})

    if not profile:
        logger.debug("No pi profile for agent %s; using pi defaults", agent_id)

    # --model
    if profile.get("provider") and not profile.get("model"):
        logger.warning(
            "Agent %s: 'provider' set without 'model' in config_pi.json; "
            "ignoring provider", agent_id,
        )

    model_arg = ""
    if profile.get("provider") and profile.get("model"):
        model_arg = f"{profile['provider']}/{profile['model']}"
    elif profile.get("model"):
        model_arg = str(profile["model"])
    if model_arg:
        cmd.extend(["--model", model_arg])

    # --thinking
    if profile.get("thinking"):
        cmd.extend(["--thinking", str(profile["thinking"])])

    # --tools
    if profile.get("tools"):
        cmd.extend(["--tools", str(profile["tools"])])

    profile_dir = AGENT_PROFILES_DIR / agent_id
    cwd = profile_dir if profile_dir.is_dir() else PROJECT_ROOT

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(cwd),
        )
    except (OSError, FileNotFoundError) as e:
        logger.warning("pi spawn failed for %s: %s", agent_id, e)
        return False

    try:
        proc.stdin.write(message.encode())
        proc.stdin.close()
    except (BrokenPipeError, OSError) as e:
        logger.warning("pi stdin write failed for %s: %s", agent_id, e)
        try:
            proc.stdin.close()
        except OSError:
            pass
        return False

    _starting_markers[agent_id] = time.time()
    return True


def ping(agent_id: str, timeout: int) -> bool:
    """Always returns True.

    pi agents are on-demand processes; no active health check is performed.
    Signature kept for dispatch parity.
    """
    return True


def is_inactive(agent_id: str, pipeline_data: dict | None = None,
                *, cc_running: bool = False) -> bool:
    """Return whether the agent should be considered inactive.

    Args:
        agent_id: Internal gokrax agent name.
        pipeline_data: Not used directly by pi backend. The ``cc_running``
            override is computed by the dispatch layer from pipeline_data and
            passed in to avoid importing engine.shared.
        cc_running: If True, CC is currently running and the agent is
            considered active regardless of session state.

    The starting marker is process-local and not a cross-process
    synchronization primitive.  See module docstring for details.
    """
    if cc_running:
        return False

    # Check process-local starting marker
    started_at = _starting_markers.get(agent_id)
    if started_at is not None:
        elapsed_since_start = time.time() - started_at
        if elapsed_since_start < config.PI_START_GRACE_SEC:
            # If the session file mtime has caught up to started_at, the
            # filesystem now reflects activity and we can clear the marker.
            sp = _session_path(agent_id)
            try:
                mtime = sp.stat().st_mtime
            except (OSError, FileNotFoundError):
                return False  # grace period active, fail-safe to active

            if mtime >= started_at:
                del _starting_markers[agent_id]
            else:
                return False  # still within grace period
        else:
            # Grace window expired; clear marker
            del _starting_markers[agent_id]

    # Normal mtime-based check
    sp = _session_path(agent_id)
    try:
        mtime = sp.stat().st_mtime
    except (OSError, FileNotFoundError):
        return True

    elapsed = time.time() - mtime
    return elapsed >= INACTIVE_THRESHOLD_SEC


def reset_session(agent_id: str) -> None:
    """Best-effort session reset: delete the session file and clear the starting marker.

    Contract:
    - Clears the process-local starting marker unconditionally.
    - Deletes the session file if present.  Absent files are not an error.
    - Does NOT terminate in-flight pi processes (fire-and-forget, no PID
      tracking).  Under POSIX, unlink removes the directory entry; any old
      process with the file still open writes to the old inode.  In observed
      pi behavior, processes do not reopen the session file by path after
      the original fd is closed.
    - Does NOT wait for quiescence.  If an old process recreates the file
      after unlink, ``is_inactive()`` will report the agent as active for
      up to ``INACTIVE_THRESHOLD_SEC`` (bounded false-active window).
      This delay is accepted as part of the pi backend contract.
    - Unexpected OS errors are logged as warnings and swallowed.

    See #246 for full design rationale.
    """
    _starting_markers.pop(agent_id, None)
    try:
        _session_path(agent_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "reset_session: failed to delete session file for %s: %s",
            agent_id, exc,
        )
