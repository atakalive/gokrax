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
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
import uuid
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
            cmd.extend(["--effort", effort_val])
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

    # Persist session_id and pid
    sid_path = _session_id_path(agent_id)
    pid_p = _pid_path(agent_id)
    try:
        sid_path.write_text(session_id, encoding="utf-8")
        pid_p.write_text(str(proc.pid), encoding="utf-8")
    except OSError as e:
        logger.warning("cc state persist failed for %s: %s", agent_id, e)
        # Roll back partial writes
        try:
            sid_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            pid_p.unlink(missing_ok=True)
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
# reset_session
# ---------------------------------------------------------------------------

def reset_session(agent_id: str) -> None:
    """Best-effort session reset: delete session files and clear the starting marker.

    Contract:
    - Calls _rebuild_claude_md(agent_id)
    - Clears the process-local starting marker unconditionally.
    - Deletes session_id and pid files if present. Absent files are not an error.
    - Does NOT terminate in-flight claude processes.
    - Unexpected OS errors are logged as warnings and swallowed.
    """
    _rebuild_claude_md(agent_id)
    _starting_markers.pop(agent_id, None)
    try:
        _session_id_path(agent_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "reset_session: failed to delete session_id file for %s: %s",
            agent_id, exc,
        )
    try:
        _pid_path(agent_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "reset_session: failed to delete pid file for %s: %s",
            agent_id, exc,
        )
