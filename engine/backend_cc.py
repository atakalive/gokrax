"""engine/backend_cc.py - cc backend for agent communication.

Provides send/ping/is_inactive/reset_session for agents running via the ``claude`` CLI.

Liveness invariant:
    cc activity is determined by three observable states:
    1. **starting grace**: a recent successful ``send()`` within CC_START_GRACE_SEC
    2. **PID validity**: /proc/<pid> exists and cmdline matches the expected claude process
    3. **session persistence**: the last filesystem modification time of the session jsonl file

    The agent is considered active if starting grace says active, or if PID is valid
    and session jsonl is fresh.

    The starting marker is process-local to the current gokrax Python process.
    Cross-process consistency relies on PID + session-file mtime as the shared
    liveness signal.  The starting marker is only a local anti-race aid for the
    process that executed ``send()``.

    ``soft_reap()`` may terminate the live owner, but only when it was spawned
    (``spawned_at``: WSL-clock epoch seconds written by ``send()``) strictly
    before the agent's last review submission in the calling project.  The
    session_id is kept so the next ``send()`` resumes the same context.  If the
    owner cannot be terminated, the pid file is kept so the BUSY guard holds.

    ``send()``, ``soft_reap()`` and ``reset_session()`` serialize their reads and
    writes of the pid/spawned_at files with the per-agent ``_agent_lock()``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import config
from config import (
    CC_SESSIONS_DIR,
    INACTIVE_THRESHOLD_SEC,
    AGENT_PROFILES_DIR,
    CC_AGENT_CONFIG,
    PROJECT_ROOT,
)
from engine.backend_types import SendResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported backend values (centralized domain)
# ---------------------------------------------------------------------------
SUPPORTED_BACKENDS: frozenset[str] = frozenset({"openclaw", "pi", "cc"})

# ---------------------------------------------------------------------------
# Process-local starting-state marker
# ---------------------------------------------------------------------------
_starting_markers: dict[str, float] = {}

# ---------------------------------------------------------------------------
# Per-agent config (agents/config_cc.json)
# ---------------------------------------------------------------------------
_agent_config_cache: dict[str, dict[str, object]] | None = None


def _load_config() -> dict[str, dict[str, object]]:
    """Load and cache agents/config_cc.json. Called once per process lifetime.

    Returns:
        Dict mapping agent_id -> {model?, thinking?, effort?, compile-startup-md?}.
        Returns empty dict if:
        - File does not exist
        - File is empty or contains only whitespace
        - JSON decode fails (log warning)
        - JSON root is not a dict (log warning)

        Non-dict entries within the root object are silently filtered out
        (with a warning listing skipped keys).

    The returned dict is the cached reference. Callers must not mutate it.
    """
    global _agent_config_cache
    if _agent_config_cache is not None:
        return _agent_config_cache

    try:
        text = CC_AGENT_CONFIG.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        _agent_config_cache = {}
        return _agent_config_cache
    except OSError as exc:
        logger.warning("Failed to read %s: %s", CC_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not text:
        _agent_config_cache = {}
        return _agent_config_cache

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in %s: %s", CC_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not isinstance(parsed, dict):
        logger.warning(
            "Expected JSON object in %s, got %s",
            CC_AGENT_CONFIG, type(parsed).__name__,
        )
        _agent_config_cache = {}
        return _agent_config_cache

    # Filter out non-dict entries
    _agent_config_cache = {
        k: v for k, v in parsed.items() if isinstance(v, dict)
    }
    if len(_agent_config_cache) < len(parsed):
        skipped = [k for k, v in parsed.items() if not isinstance(v, dict)]
        logger.warning(
            "Skipped non-dict entries in %s: %s", CC_AGENT_CONFIG, skipped,
        )
    return _agent_config_cache


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _session_dir(agent_id: str) -> Path:
    """Return the per-agent session directory."""
    return CC_SESSIONS_DIR / agent_id


def _session_id_path(agent_id: str) -> Path:
    """Return the path to the session_id file for an agent."""
    return _session_dir(agent_id) / "session_id"


def _pid_path(agent_id: str) -> Path:
    """Return the path to the pid file for an agent."""
    return _session_dir(agent_id) / "pid"


def _spawned_at_path(agent_id: str) -> Path:
    """Return the path to the spawned_at file (epoch seconds of the last spawn, WSL clock)."""
    return _session_dir(agent_id) / "spawned_at"


def _lock_path(agent_id: str) -> Path:
    """Return the path to the per-agent lock file (see _agent_lock)."""
    return _session_dir(agent_id) / "lock"


@contextmanager
def _agent_lock(agent_id: str) -> Iterator[None]:
    """Hold the agent's exclusive inter-process lock on its pid/spawned_at files.

    Blocking ``flock(LOCK_EX)`` (not LOCK_NB) on ``<session_dir>/lock``.  send()
    holds it from the ownership check through the state persist, soft_reap()
    from the ownership check through the state cleanup, and reset_session()
    while deleting the files, so none of them can read or delete the files in
    the middle of another's update.  Not reentrant: never call one of these
    three while holding the lock.
    """
    _session_dir(agent_id).mkdir(parents=True, exist_ok=True)
    with open(_lock_path(agent_id), "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _claude_project_dir(cwd: Path) -> Path:
    """Return the Claude Code session JSONL storage directory for a given cwd."""
    project_key = str(cwd.resolve()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / project_key


def _claude_session_jsonl_path(cwd: Path, session_id: str) -> Path:
    """Return the path to a Claude Code session JSONL file."""
    return _claude_project_dir(cwd) / f"{session_id}.jsonl"


def _read_session_id(agent_id: str) -> str | None:
    """Read and validate the session_id file for an agent.

    Returns None if:
    - File does not exist
    - File is empty
    - UUID is invalid
    - Read error
    """
    try:
        text = _session_id_path(agent_id).read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return None
    if not text:
        return None
    try:
        uuid.UUID(text)
    except ValueError:
        return None
    return text


# ---------------------------------------------------------------------------
# Persisted-state snapshot & session ownership
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PersistedCcState:
    session_id: str | None
    pid_text: str | None


@dataclass(frozen=True)
class SessionOwnership:
    state: PersistedCcState
    has_valid_session: bool
    has_live_owner: bool


def _read_persisted_state(agent_id: str) -> PersistedCcState:
    """Read session_id and pid files atomically into a single snapshot.

    Returns:
        PersistedCcState with validated session_id (UUID string or None)
        and raw pid_text (stripped string or None).
    """
    # Read session_id
    try:
        sid_text = _session_id_path(agent_id).read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        sid_text = ""
    session_id: str | None = None
    if sid_text:
        try:
            uuid.UUID(sid_text)
            session_id = sid_text
        except ValueError:
            pass

    # Read pid
    try:
        pid_text_raw = _pid_path(agent_id).read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        pid_text_raw = ""
    pid_text: str | None = pid_text_raw if pid_text_raw else None

    return PersistedCcState(session_id=session_id, pid_text=pid_text)


def _check_session_ownership(state: PersistedCcState) -> SessionOwnership:
    """Determine whether a live process owns the recorded session.

    This helper never re-reads persisted files from disk.
    """
    if state.session_id is None:
        return SessionOwnership(state=state, has_valid_session=False, has_live_owner=False)

    if state.pid_text is None:
        return SessionOwnership(state=state, has_valid_session=True, has_live_owner=False)

    try:
        pid = int(state.pid_text)
    except ValueError:
        return SessionOwnership(state=state, has_valid_session=True, has_live_owner=False)

    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        return SessionOwnership(state=state, has_valid_session=True, has_live_owner=False)

    cmdline_path = proc_dir / "cmdline"
    try:
        cmdline_bytes = cmdline_path.read_bytes()
    except (OSError, FileNotFoundError):
        return SessionOwnership(state=state, has_valid_session=True, has_live_owner=False)

    tokens = cmdline_bytes.split(b"\0")
    str_tokens: list[str] = []
    for t in tokens:
        try:
            str_tokens.append(t.decode("utf-8"))
        except UnicodeDecodeError:
            str_tokens.append("")

    has_claude = any(
        t == "claude" or t.endswith("/claude")
        for t in str_tokens
    )
    if not has_claude:
        return SessionOwnership(state=state, has_valid_session=True, has_live_owner=False)

    has_session_match = False
    for i in range(len(str_tokens) - 1):
        if str_tokens[i] in ("--resume", "--session-id") and str_tokens[i + 1] == state.session_id:
            has_session_match = True
            break

    return SessionOwnership(
        state=state, has_valid_session=True, has_live_owner=has_session_match,
    )


# ---------------------------------------------------------------------------
# _rebuild_claude_md
# ---------------------------------------------------------------------------

def _rebuild_claude_md(agent_id: str) -> None:
    """Rebuild CLAUDE.md from IDENTITY.md + INSTRUCTION.md + MEMORY.md (on source change only)."""
    try:
        config_data = _load_config()
        agent_profile = config_data.get(agent_id, {})
        compile_flag = agent_profile.get("compile-startup-md", False)
        if not isinstance(compile_flag, bool):
            logger.warning(
                "_rebuild_claude_md: compile-startup-md for %s has non-bool value %r; "
                "treating as False",
                agent_id, compile_flag,
            )
            compile_flag = False

        profile_dir = AGENT_PROFILES_DIR / agent_id

        if not compile_flag:
            # When disabled, clean up auto-generated files only if .claude_hash exists
            if profile_dir.is_dir():
                hash_path = profile_dir / ".claude_hash"
                if hash_path.exists():
                    try:
                        (profile_dir / "CLAUDE.md").unlink(missing_ok=True)
                    except OSError:
                        pass
                    try:
                        hash_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            return

        if not profile_dir.is_dir():
            return

        identity_path = profile_dir / "IDENTITY.md"
        instruction_path = profile_dir / "INSTRUCTION.md"
        memory_path = profile_dir / "MEMORY.md"

        try:
            identity_bytes = identity_path.read_bytes()
        except FileNotFoundError:
            identity_bytes = b""
        try:
            instruction_bytes = instruction_path.read_bytes()
        except FileNotFoundError:
            instruction_bytes = b""
        try:
            memory_bytes = memory_path.read_bytes()
        except FileNotFoundError:
            memory_bytes = b""

        claude_md_path = profile_dir / "CLAUDE.md"
        hash_path = profile_dir / ".claude_hash"

        if identity_bytes == b"" and instruction_bytes == b"" and memory_bytes == b"":
            claude_md_path.unlink(missing_ok=True)
            hash_path.unlink(missing_ok=True)
            return

        # Hash algorithm: PI-compatible
        # identity/instruction get length prefix, memory does not
        new_hash = hashlib.sha256(
            len(identity_bytes).to_bytes(8, "big")
            + identity_bytes
            + len(instruction_bytes).to_bytes(8, "big")
            + instruction_bytes
            + memory_bytes,
        ).hexdigest()

        try:
            old_hash = hash_path.read_text(encoding="utf-8").strip()
        except OSError:
            old_hash = ""

        if old_hash == new_hash and claude_md_path.exists():
            return

        identity_text = identity_bytes.decode("utf-8").rstrip()
        instruction_text = instruction_bytes.decode("utf-8").rstrip()
        memory_text = memory_bytes.decode("utf-8").rstrip()

        parts = [t for t in (identity_text, instruction_text, memory_text) if t]

        if not parts:
            claude_md_path.unlink(missing_ok=True)
            hash_path.unlink(missing_ok=True)
            return

        output = "\n\n---\n\n".join(parts) + "\n"

        claude_md_path.write_text(output, encoding="utf-8")
        hash_path.write_text(new_hash + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("_rebuild_claude_md: failed for %s: %s", agent_id, exc)


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

def send(agent_id: str, message: str, timeout: int) -> SendResult:
    """Fire-and-forget subprocess launch of ``claude -p``.

    Args:
        agent_id: Internal gokrax agent name.
        message: Message to write to claude's stdin.
        timeout: Kept for interface parity; unused by cc fire-and-forget spawn.

    Returns:
        SendResult.OK on successful spawn, SendResult.BUSY if another live
        claude owner is writing the session, SendResult.FAIL on other errors.
    """
    if config.DRY_RUN:
        logger.info("[dry-run] cc send skipped (agent=%s)", agent_id)
        return SendResult.OK

    CC_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Create per-agent session dir
    agent_dir = _session_dir(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    _rebuild_claude_md(agent_id)

    with _agent_lock(agent_id):
        return _send_locked(agent_id, message)


def _send_locked(agent_id: str, message: str) -> SendResult:
    """Spawn part of send(), from the ownership check through the state persist.

    The caller holds _agent_lock(agent_id), so soft_reap() and reset_session()
    cannot read or delete the pid/spawned_at files in between.
    """
    # Read one persisted-state snapshot for this call
    state = _read_persisted_state(agent_id)
    ownership = _check_session_ownership(state)

    if ownership.has_live_owner:
        marker_ts = _starting_markers.get(agent_id)
        if marker_ts is not None and (time.time() - marker_ts) < config.CC_START_GRACE_SEC:
            logger.warning(
                "cc send refused for %s session %s: live owner within starting grace; "
                "deferring spawn (busy)",
                agent_id, state.session_id,
            )
            return SendResult.BUSY

        logger.info(
            "cc send refused for %s session %s: live owner pid present (busy)",
            agent_id, state.session_id,
        )
        return SendResult.BUSY

    # Determine session_id: resume existing or create new
    is_resume = ownership.has_valid_session
    assert state.session_id is not None or not is_resume
    session_id = state.session_id if is_resume else str(uuid.uuid4())

    config_data = _load_config()
    profile = config_data.get(agent_id, {})

    if not profile:
        logger.debug("No cc profile for agent %s; using claude defaults", agent_id)

    profile_dir = AGENT_PROFILES_DIR / agent_id
    cwd = profile_dir if profile_dir.is_dir() else PROJECT_ROOT

    # Build command
    cmd: list[str] = [config.CC_BIN, "-p"]
    if is_resume:
        cmd += ["--resume", session_id]
    else:
        cmd += ["--session-id", session_id]

    # --model
    model_val = profile.get("model")
    if model_val and isinstance(model_val, str) and model_val.strip():
        cmd.extend(["--model", model_val])

    # --thinking <mode>  (enabled | adaptive | disabled)
    # bool fallback: True → "enabled", False → "disabled"
    _THINKING_MODES = {"enabled", "adaptive", "disabled"}
    thinking_val = profile.get("thinking")
    if thinking_val is not None:
        if isinstance(thinking_val, bool):
            thinking_val = "enabled" if thinking_val else "disabled"
        if isinstance(thinking_val, str) and thinking_val in _THINKING_MODES:
            cmd.extend(["--thinking", thinking_val])
        else:
            logger.warning(
                "Agent %s: 'thinking' has invalid value %r in config_cc.json; "
                "expected one of %s; ignoring",
                agent_id, thinking_val, sorted(_THINKING_MODES),
            )

    # --effort
    effort_val = profile.get("effort")
    if effort_val is not None:
        if isinstance(effort_val, str) and effort_val.strip():
            ev = effort_val.strip()
            if ev == "ultracode":
                cmd.extend(["--effort", "xhigh", "--settings", '{"ultracode": true}'])
            else:
                cmd.extend(["--effort", ev])
        else:
            logger.warning(
                "Agent %s: 'effort' has invalid value %r in config_cc.json; ignoring",
                agent_id, effort_val,
            )

    # Always add --dangerously-skip-permissions
    cmd.append("--dangerously-skip-permissions")

    # Spawn
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(cwd),
            start_new_session=True,
        )
    except (OSError, FileNotFoundError) as e:
        logger.warning("cc spawn failed for %s: %s", agent_id, e)
        return SendResult.FAIL
    spawned_at = time.time()

    # Write message to stdin
    try:
        proc.stdin.write(message.encode("utf-8"))
        proc.stdin.close()
    except (BrokenPipeError, OSError) as e:
        logger.warning("cc stdin write failed for %s: %s", agent_id, e)
        try:
            proc.stdin.close()
        except OSError:
            pass
        _cleanup_proc(proc)
        return SendResult.FAIL

    # Persist session_id, spawned_at and pid.  spawned_at is written before pid
    # so that a reader never pairs a new pid with the previous spawn's time.
    sid_path = _session_id_path(agent_id)
    spawned_p = _spawned_at_path(agent_id)
    pid_p = _pid_path(agent_id)
    try:
        sid_path.write_text(session_id, encoding="utf-8")
        spawned_p.write_text(repr(spawned_at), encoding="utf-8")
        pid_p.write_text(str(proc.pid), encoding="utf-8")
    except OSError as e:
        logger.warning("cc state persist failed for %s: %s", agent_id, e)
        # Roll back partial writes
        for path in (sid_path, spawned_p, pid_p):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        _cleanup_proc(proc)
        return SendResult.FAIL

    _starting_markers[agent_id] = time.time()
    return SendResult.OK


def _cleanup_proc(proc: subprocess.Popen) -> None:
    """Best-effort cleanup of a spawned process."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError as e:
                logger.warning("cc proc cleanup failed: %s", e)
                return
            try:
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError) as e:
                logger.warning("cc proc cleanup failed: %s", e)
    except OSError as e:
        logger.warning("cc proc cleanup failed: %s", e)


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------

def ping(agent_id: str, timeout: int) -> bool:
    """Always returns True.

    cc agents are on-demand processes; no active health check is performed.
    Signature kept for dispatch parity.
    """
    return True


# ---------------------------------------------------------------------------
# is_inactive
# ---------------------------------------------------------------------------

def is_inactive(agent_id: str, pipeline_data: dict | None = None,
                *, cc_running: bool = False) -> bool:
    """Return whether the agent should be considered inactive.

    Judgment order:
    1. cc_running override
    2. Starting grace period
    3. session_id validity
    4. PID file validity
    5. /proc/<pid> existence + cmdline verification
    6. Session JSONL mtime freshness
    """
    if cc_running:
        return False

    # Check process-local starting marker
    started_at = _starting_markers.get(agent_id)
    if started_at is not None:
        elapsed_since_start = time.time() - started_at
        if elapsed_since_start < config.CC_START_GRACE_SEC:
            # During grace, stay fail-safe active until the jsonl mtime catches up.
            # Also evaluate ownership from the same persisted snapshot so the
            # grace-path decision does not bypass the shared ownership helper.
            grace_state = _read_persisted_state(agent_id)
            grace_ownership = _check_session_ownership(grace_state)
            if not grace_ownership.has_valid_session:
                return False  # grace period active, no valid session yet

            assert grace_state.session_id is not None
            profile_dir = AGENT_PROFILES_DIR / agent_id
            cwd = profile_dir if profile_dir.is_dir() else PROJECT_ROOT
            jsonl_path = _claude_session_jsonl_path(cwd, grace_state.session_id)
            try:
                mtime = jsonl_path.stat().st_mtime
            except (OSError, FileNotFoundError):
                return False  # grace period active, fail-safe to active

            if mtime >= started_at:
                del _starting_markers[agent_id]
                # Fall through to normal judgment
            else:
                return False  # still within grace period
        else:
            # Grace window expired; clear marker
            del _starting_markers[agent_id]

    # Read one persisted-state snapshot for this call
    state = _read_persisted_state(agent_id)
    ownership = _check_session_ownership(state)

    if not ownership.has_valid_session:
        return True

    if ownership.has_live_owner:
        return False

    # No live owner — fall back to jsonl mtime freshness
    assert state.session_id is not None
    profile_dir = AGENT_PROFILES_DIR / agent_id
    cwd = profile_dir if profile_dir.is_dir() else PROJECT_ROOT
    jsonl_path = _claude_session_jsonl_path(cwd, state.session_id)
    try:
        mtime = jsonl_path.stat().st_mtime
    except (OSError, FileNotFoundError):
        return True

    elapsed = time.time() - mtime
    return elapsed >= INACTIVE_THRESHOLD_SEC


# ---------------------------------------------------------------------------
# soft_reap
# ---------------------------------------------------------------------------

_PROC_ROOT = Path("/proc")
_TERMINATE_POLLS = 10
_TERMINATE_POLL_SEC = 0.5


def _read_spawned_at(agent_id: str) -> float | None:
    """Read the spawned_at file. Returns None if missing, unparsable or non-finite."""
    try:
        text = _spawned_at_path(agent_id).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _proc_identity(pid: int) -> tuple[int, int] | None:
    """Return (starttime in clock ticks, pgid) from <_PROC_ROOT>/<pid>/stat.

    Returns None if the process is gone, is a zombie (it has exited and only
    its parent's wait() is pending), or the stat line cannot be parsed.
    Modeled on backend_cci._proc_starttime_ticks / _is_zombie; kept local
    because backends do not import each other's private helpers.
    """
    try:
        stat_line = (_PROC_ROOT / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    close_paren = stat_line.rfind(")")
    if close_paren == -1:
        return None
    try:
        fields = stat_line[close_paren + 2 :].split()
        if fields[0] == "Z":
            return None
        return int(fields[19]), int(fields[2])
    except (IndexError, ValueError):
        return None


def _snapshot_children(pid: int) -> list[tuple[int, int, int]]:
    """Return (child pid, starttime ticks, pgid) for each direct child of *pid*.

    Reads <_PROC_ROOT>/<pid>/task/*/children.  Unreadable entries are skipped.
    """
    children: list[tuple[int, int, int]] = []
    try:
        task_dirs = sorted((_PROC_ROOT / str(pid) / "task").iterdir())
    except OSError:
        return children
    for task_dir in task_dirs:
        try:
            tokens = (task_dir / "children").read_text(encoding="utf-8").split()
        except OSError:
            continue
        for token in tokens:
            try:
                child_pid = int(token)
            except ValueError:
                continue
            identity = _proc_identity(child_pid)
            if identity is not None:
                children.append((child_pid, identity[0], identity[1]))
    return children


def _signalable_pgid(pgid: int | None) -> bool:
    """True if killpg may target *pgid*: a real group (> 1) that is not our own.

    0 and negative values mean "our own group" / "every process" to
    kill(2)/killpg(3), and 1 is init's group.
    """
    return pgid is not None and pgid > 1 and pgid != os.getpgrp()


def _terminate_owner(
    pid: int, pgid: int | None, state: PersistedCcState, agent_id: str,
) -> bool:
    """SIGTERM -> poll -> SIGKILL -> poll the live owner. Returns True once it is gone.

    Signals the owner's process group (send() spawns with start_new_session=True,
    so the group holds claude and its non-detached descendants), or the pid
    alone when the pgid is unknown or not signalable.  Liveness is re-evaluated
    with _check_session_ownership(state) on every poll; a zombie has an empty
    cmdline and counts as gone.  Same sequence as backend_agy._terminate_pid_tree.
    """
    target_pgid = pgid if _signalable_pgid(pgid) else None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if target_pgid is not None:
                os.killpg(target_pgid, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except OSError as e:
            logger.warning(
                "cc soft_reap: %s to pid %d (pgid %s) failed for %s: %s",
                sig.name, pid, target_pgid, agent_id, e,
            )
        for _ in range(_TERMINATE_POLLS):
            if not _check_session_ownership(state).has_live_owner:
                return True
            time.sleep(_TERMINATE_POLL_SEC)
    return not _check_session_ownership(state).has_live_owner


def _reap_detached_children(
    children: list[tuple[int, int, int]], owner_pgid: int | None, agent_id: str,
) -> None:
    """Best-effort SIGTERM -> SIGKILL to children that left the owner's process group.

    A child that started its own group (e.g. a Bash tool shell started with
    setsid) survives killpg on the owner's group.  The child's group is
    signalled only while the recorded child itself is alive with the same
    (starttime, pgid), and never when the group is not signalable or is the
    owner's group.  Once the recorded child is gone, the rest of its group
    (e.g. a grandchild that ignored SIGTERM) is not signalled: a bare pgid
    cannot be told apart from a reused one.
    """
    for child_pid, starttime, pgid in children:
        if not _signalable_pgid(pgid) or pgid == owner_pgid:
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if _proc_identity(child_pid) != (starttime, pgid):
                break
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            except OSError as e:
                logger.warning(
                    "cc soft_reap: %s to child pgid %d failed for %s: %s",
                    sig.name, pgid, agent_id, e,
                )
                break
            logger.info(
                "cc soft_reap: sent %s to child pgid %d (pid %d) for %s",
                sig.name, pgid, child_pid, agent_id,
            )
            for _ in range(_TERMINATE_POLLS):
                if _proc_identity(child_pid) != (starttime, pgid):
                    break
                time.sleep(_TERMINATE_POLL_SEC)


def _soft_reap_locked(
    agent_id: str, submitted_at: float,
) -> tuple[list[tuple[int, int, int]], int | None] | None:
    """Owner part of soft_reap(); the caller holds _agent_lock(agent_id).

    Returns (children snapshot, owner pgid) once the owner is gone, or None if
    nothing was terminated.
    """
    state = _read_persisted_state(agent_id)
    if not _check_session_ownership(state).has_live_owner:
        return None
    assert state.pid_text is not None
    pid = int(state.pid_text)
    spawned_at = _read_spawned_at(agent_id)
    if spawned_at is None or spawned_at >= submitted_at:
        logger.info(
            "cc soft_reap skipped for %s: live owner pid %d spawned_at=%r "
            "is not before submitted_at=%r",
            agent_id, pid, spawned_at, submitted_at,
        )
        return None

    children = _snapshot_children(pid)
    try:
        owner_pgid: int | None = os.getpgid(pid)
    except OSError:
        owner_pgid = None
    logger.info(
        "cc soft_reap: terminating live owner pid %d for %s "
        "(spawned_at=%r < submitted_at=%r)",
        pid, agent_id, spawned_at, submitted_at,
    )
    if not _terminate_owner(pid, owner_pgid, state, agent_id):
        logger.warning(
            "cc soft_reap: failed to terminate live owner pid %d for %s; "
            "keeping pid file to preserve BUSY guard",
            pid, agent_id,
        )
        return None

    _starting_markers.pop(agent_id, None)
    for path in (_pid_path(agent_id), _spawned_at_path(agent_id)):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "cc soft_reap: failed to delete %s file for %s: %s",
                path.name, agent_id, exc,
            )
    return children, owner_pgid


def soft_reap(agent_id: str, *, submitted_at: float | None = None) -> None:
    """Context-preserving reap of a claude process still running after a submission.

    Terminates the live owner only if it was spawned strictly before
    ``submitted_at`` (epoch seconds of the agent's last review submission in
    the calling project).  The session/pid files are per agent and shared
    across projects, and the BUSY guard keeps at most one live owner, so an
    owner spawned before the submission and still alive is the process that
    submitted and is doing post-submission work.  An owner spawned at or after
    ``submitted_at`` (e.g. for another project) is left alone, and so is an
    owner whose spawned_at is unknown.

    On success the pid and spawned_at files are removed and the session_id is
    kept, so the next send() resumes the same context.  If the owner cannot be
    terminated, the files are kept to preserve the BUSY guard.

    Holds _agent_lock(agent_id) from the ownership check through the state
    cleanup, so no send()/reset_session() can write or delete the files in
    between.  This relies on every writer of those files taking the lock (code
    from before this change does not).  Detached children are reaped after the
    lock is released, so a slow child does not hold up send().
    """
    if config.DRY_RUN or submitted_at is None:
        return
    with _agent_lock(agent_id):
        reaped = _soft_reap_locked(agent_id, submitted_at)
    if reaped is not None:
        children, owner_pgid = reaped
        _reap_detached_children(children, owner_pgid, agent_id)


# ---------------------------------------------------------------------------
# reset_session
# ---------------------------------------------------------------------------

def reset_session(agent_id: str) -> None:
    """Best-effort session reset: delete session files and clear the starting marker.

    Contract:
    - Calls _rebuild_claude_md(agent_id)
    - Clears the process-local starting marker unconditionally.
    - Deletes session_id, pid and spawned_at files if present, holding
      _agent_lock(agent_id). Absent files are not an error.
    - Does NOT terminate in-flight claude processes.
    - Unexpected OS errors are logged as warnings and swallowed.
    """
    _rebuild_claude_md(agent_id)
    with _agent_lock(agent_id):
        _starting_markers.pop(agent_id, None)
        for name, path in (
            ("session_id", _session_id_path(agent_id)),
            ("pid", _pid_path(agent_id)),
            ("spawned_at", _spawned_at_path(agent_id)),
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "reset_session: failed to delete %s file for %s: %s",
                    name, agent_id, exc,
                )
