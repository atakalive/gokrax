"""engine/backend_cci.py - cci backend for agent communication.

Subscription-billed alternative to the cc backend: instead of ``claude -p``
(headless/print mode, which becomes paid-only for subscribers after 2026-06-15),
each ``send()`` spawns ``engine.cci_runner`` which drives the interactive
``claude`` TUI for one turn and exits.

This module mirrors ``backend_cc.py`` (send/ping/is_inactive/reset_session plus
path/ownership helpers). Differences:
- session/config paths use CCI_SESSIONS_DIR / CCI_AGENT_CONFIG
- send() launches ``engine.cci_runner`` (detached) instead of ``claude -p``
- ownership is determined by inspecting the runner process cmdline
- reset_session additionally SIGTERMs a live runner before deleting files

Liveness invariant mirrors cc: a recent successful send (CCI_START_GRACE_SEC),
PID + /proc/<pid>/cmdline validity, and session jsonl mtime freshness.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import config
from config import (
    CCI_SESSIONS_DIR,
    INACTIVE_THRESHOLD_SEC,
    AGENT_PROFILES_DIR,
    CCI_AGENT_CONFIG,
    PROJECT_ROOT,
)
from engine.backend_types import SendResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process-local starting-state marker
# ---------------------------------------------------------------------------
_starting_markers: dict[str, float] = {}

# ---------------------------------------------------------------------------
# Per-agent config (agents/config_cci.json)
# ---------------------------------------------------------------------------
_agent_config_cache: dict[str, dict[str, object]] | None = None


def _load_config() -> dict[str, dict[str, object]]:
    """Load and cache agents/config_cci.json. Called once per process lifetime.

    Returns an empty dict on missing/empty/invalid/non-dict-root files. Non-dict
    entries within the root are filtered out (with a warning). The returned dict
    is the cached reference; callers must not mutate it.
    """
    global _agent_config_cache
    if _agent_config_cache is not None:
        return _agent_config_cache

    try:
        text = CCI_AGENT_CONFIG.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        _agent_config_cache = {}
        return _agent_config_cache
    except OSError as exc:
        logger.warning("Failed to read %s: %s", CCI_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not text:
        _agent_config_cache = {}
        return _agent_config_cache

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in %s: %s", CCI_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not isinstance(parsed, dict):
        logger.warning(
            "Expected JSON object in %s, got %s",
            CCI_AGENT_CONFIG, type(parsed).__name__,
        )
        _agent_config_cache = {}
        return _agent_config_cache

    _agent_config_cache = {
        k: v for k, v in parsed.items() if isinstance(v, dict)
    }
    if len(_agent_config_cache) < len(parsed):
        skipped = [k for k, v in parsed.items() if not isinstance(v, dict)]
        logger.warning(
            "Skipped non-dict entries in %s: %s", CCI_AGENT_CONFIG, skipped,
        )
    return _agent_config_cache


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _session_dir(agent_id: str) -> Path:
    """Return the per-agent session directory."""
    return CCI_SESSIONS_DIR / agent_id


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
    """Read and validate the session_id file for an agent (None if missing/invalid)."""
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
    """Read session_id and pid files atomically into a single snapshot."""
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

    try:
        pid_text_raw = _pid_path(agent_id).read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        pid_text_raw = ""
    pid_text: str | None = pid_text_raw if pid_text_raw else None

    return PersistedCcState(session_id=session_id, pid_text=pid_text)


def _check_session_ownership(state: PersistedCcState) -> SessionOwnership:
    """Determine whether a live runner process owns the recorded session.

    The owner is the ``engine.cci_runner`` process whose cmdline contains the
    ``engine.cci_runner`` token and a ``--resume``/``--session-id`` flag whose
    next token exactly equals the recorded session_id.
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

    has_runner = any(t == "engine.cci_runner" for t in str_tokens)
    if not has_runner:
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
    """Fire-and-forget launch of ``engine.cci_runner`` (interactive claude TUI).

    Args:
        agent_id: Internal gokrax agent name.
        message: Message body; written to a temp prompt file passed to the runner.
        timeout: Communication timeout (interface parity); NOT the turn-completion
            timeout. The runner receives ``config.CCI_COMPLETION_TIMEOUT_SEC``.

    Returns:
        SendResult.OK on successful spawn, SendResult.BUSY if a live runner already
        owns the session, SendResult.FAIL on other errors.
    """
    if config.DRY_RUN:
        logger.info("[dry-run] cci send skipped (agent=%s)", agent_id)
        return SendResult.OK

    CCI_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    agent_dir = _session_dir(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    _rebuild_claude_md(agent_id)

    state = _read_persisted_state(agent_id)
    ownership = _check_session_ownership(state)

    if ownership.has_live_owner:
        marker_ts = _starting_markers.get(agent_id)
        if marker_ts is not None and (time.time() - marker_ts) < config.CCI_START_GRACE_SEC:
            logger.warning(
                "cci send refused for %s session %s: live owner within starting grace; "
                "deferring spawn (busy)",
                agent_id, state.session_id,
            )
            return SendResult.BUSY

        logger.info(
            "cci send refused for %s session %s: live owner pid present (busy)",
            agent_id, state.session_id,
        )
        return SendResult.BUSY

    is_resume = ownership.has_valid_session
    assert state.session_id is not None or not is_resume
    session_id = state.session_id if is_resume else str(uuid.uuid4())

    config_data = _load_config()
    profile = config_data.get(agent_id, {})

    if not profile:
        logger.debug("No cci profile for agent %s; using claude defaults", agent_id)

    profile_dir = AGENT_PROFILES_DIR / agent_id
    cwd = profile_dir if profile_dir.is_dir() else PROJECT_ROOT

    # Write the prompt to a private temp file (0o600).
    try:
        fd, prompt_path = tempfile.mkstemp(suffix=".txt", prefix=f"cci-{agent_id}-", dir="/tmp")
    except OSError as e:
        logger.warning("cci mkstemp failed for %s: %s", agent_id, e)
        return SendResult.FAIL
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(message)
    except OSError as e:
        logger.warning("cci prompt file write failed for %s: %s", agent_id, e)
        try:
            os.unlink(prompt_path)
        except OSError:
            pass
        return SendResult.FAIL

    # Build the runner command.
    cmd: list[str] = [sys.executable, "-m", "engine.cci_runner"]
    if is_resume:
        cmd += ["--resume", session_id]
    else:
        cmd += ["--session-id", session_id]
    cmd += ["--cwd", str(cwd), "--prompt-file", prompt_path, "--delete-prompt-file"]
    cmd += ["--completion-timeout", str(config.CCI_COMPLETION_TIMEOUT_SEC)]

    # --model
    model_val = profile.get("model")
    if model_val and isinstance(model_val, str) and model_val.strip():
        cmd.extend(["--model", model_val])

    # --thinking <mode>  (enabled | adaptive | disabled); bool fallback
    _THINKING_MODES = {"enabled", "adaptive", "disabled"}
    thinking_val = profile.get("thinking")
    if thinking_val is not None:
        if isinstance(thinking_val, bool):
            thinking_val = "enabled" if thinking_val else "disabled"
        if isinstance(thinking_val, str) and thinking_val in _THINKING_MODES:
            cmd.extend(["--thinking", thinking_val])
        else:
            logger.warning(
                "Agent %s: 'thinking' has invalid value %r in config_cci.json; "
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
                "Agent %s: 'effort' has invalid value %r in config_cci.json; ignoring",
                agent_id, effort_val,
            )

    # Spawn the detached runner. stderr → runner.log ("w": fresh each turn).
    proc = None
    try:
        log_path = _session_dir(agent_id) / "runner.log"
        log_fd = open(log_path, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log_fd,
                cwd=str(PROJECT_ROOT),
                start_new_session=True,
            )
        finally:
            log_fd.close()
    except (OSError, FileNotFoundError) as e:
        logger.warning("cci runner spawn failed for %s: %s", agent_id, e)
        try:
            os.unlink(prompt_path)
        except OSError:
            pass
        return SendResult.FAIL

    # Persist session_id and pid.
    sid_path = _session_id_path(agent_id)
    pid_p = _pid_path(agent_id)
    try:
        sid_path.write_text(session_id, encoding="utf-8")
        pid_p.write_text(str(proc.pid), encoding="utf-8")
    except OSError as e:
        logger.warning("cci state persist failed for %s: %s", agent_id, e)
        try:
            sid_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            pid_p.unlink(missing_ok=True)
        except OSError:
            pass
        _cleanup_proc(proc)
        try:
            os.unlink(prompt_path)
        except OSError:
            pass
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
                logger.warning("cci proc cleanup failed: %s", e)
                return
            try:
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError) as e:
                logger.warning("cci proc cleanup failed: %s", e)
    except OSError as e:
        logger.warning("cci proc cleanup failed: %s", e)


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------

def ping(agent_id: str, timeout: int) -> bool:
    """Always returns True (cci agents are on-demand processes; no health check)."""
    return True


# ---------------------------------------------------------------------------
# is_inactive
# ---------------------------------------------------------------------------

def is_inactive(agent_id: str, pipeline_data: dict | None = None,
                *, cc_running: bool = False) -> bool:
    """Return whether the agent should be considered inactive.

    Judgment order mirrors cc: cc_running override, starting grace
    (CCI_START_GRACE_SEC), session_id validity, PID + /proc cmdline, jsonl mtime.
    """
    if cc_running:
        return False

    started_at = _starting_markers.get(agent_id)
    if started_at is not None:
        elapsed_since_start = time.time() - started_at
        if elapsed_since_start < config.CCI_START_GRACE_SEC:
            grace_state = _read_persisted_state(agent_id)
            grace_ownership = _check_session_ownership(grace_state)
            if not grace_ownership.has_valid_session:
                return False

            assert grace_state.session_id is not None
            profile_dir = AGENT_PROFILES_DIR / agent_id
            cwd = profile_dir if profile_dir.is_dir() else PROJECT_ROOT
            jsonl_path = _claude_session_jsonl_path(cwd, grace_state.session_id)
            try:
                mtime = jsonl_path.stat().st_mtime
            except (OSError, FileNotFoundError):
                return False

            if mtime >= started_at:
                del _starting_markers[agent_id]
            else:
                return False
        else:
            del _starting_markers[agent_id]

    state = _read_persisted_state(agent_id)
    ownership = _check_session_ownership(state)

    if not ownership.has_valid_session:
        return True

    if ownership.has_live_owner:
        return False

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
    """Best-effort session reset: SIGTERM a live runner, then delete session files.

    Contract:
    - Calls _rebuild_claude_md(agent_id)
    - Sends SIGTERM to a live runner owning this agent (its signal handler reaps
      the child claude TUI). Errors are swallowed.
    - Clears the process-local starting marker unconditionally.
    - Deletes session_id and pid files if present.
    """
    _rebuild_claude_md(agent_id)

    # SIGTERM a live runner before deleting files.
    pid_text: str | None = None
    try:
        pid_text = _pid_path(agent_id).read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        pid_text = None
    if pid_text:
        try:
            pid = int(pid_text)
        except ValueError:
            pid = None
        if pid is not None:
            cmdline_path = Path(f"/proc/{pid}") / "cmdline"
            try:
                cmdline_bytes = cmdline_path.read_bytes()
            except (OSError, FileNotFoundError):
                cmdline_bytes = b""
            tokens = [
                t.decode("utf-8", "replace") for t in cmdline_bytes.split(b"\0")
            ]
            if any(t == "engine.cci_runner" for t in tokens):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass

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
