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

import logging
import subprocess
import time
from pathlib import Path

import config
from config import (
    PI_SESSIONS_DIR,
    INACTIVE_THRESHOLD_SEC,
    AGENT_PROFILES_DIR,
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
    if config.PI_MODEL:
        cmd.extend(["--model", config.PI_MODEL])

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
    """Delete the deterministic session file and clear the starting marker.

    No error if the file is absent.  Does not delete parent directories.
    """
    _starting_markers.pop(agent_id, None)
    _session_path(agent_id).unlink(missing_ok=True)
