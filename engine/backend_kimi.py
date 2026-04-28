"""engine/backend_kimi.py - kimi backend for agent communication.

Provides send/ping/is_inactive/reset_session for agents running via the ``kimi``
CLI (Moonshot AI's kimi-cli).

Kimi characteristics:
    - Oneshot process (one prompt = one process, completes and exits)
    - Sessions are scoped per-cwd; the client cannot specify a session_id
    - Continuation: ``-C`` resumes the most recent session for the cwd
      (best-effort; creates a new session if none exists)
    - No ``--list-sessions`` / ``--delete-session`` CLI: presence of a previous
      session is tracked by a local marker file (``<agent>.has_session``)

Liveness invariant:
    pid file exists, /proc/<pid> exists, and cmdline contains "kimi".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import config
from config import (
    AGENT_PROFILES_DIR,
    KIMI_AGENT_CONFIG,
    KIMI_PIDS_DIR,
)
from engine.backend_types import SendResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-agent config (agents/config_kimi.json)
# ---------------------------------------------------------------------------
_agent_config_cache: dict[str, dict[str, object]] | None = None


def _load_config() -> dict[str, dict[str, object]]:
    """Load and cache agents/config_kimi.json. Called once per process lifetime."""
    global _agent_config_cache
    if _agent_config_cache is not None:
        return _agent_config_cache

    try:
        text = KIMI_AGENT_CONFIG.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        _agent_config_cache = {}
        return _agent_config_cache
    except OSError as exc:
        logger.warning("Failed to read %s: %s", KIMI_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not text:
        _agent_config_cache = {}
        return _agent_config_cache

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in %s: %s", KIMI_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not isinstance(parsed, dict):
        logger.warning(
            "Expected JSON object in %s, got %s",
            KIMI_AGENT_CONFIG, type(parsed).__name__,
        )
        _agent_config_cache = {}
        return _agent_config_cache

    _agent_config_cache = {
        k: v for k, v in parsed.items() if isinstance(v, dict)
    }
    if len(_agent_config_cache) < len(parsed):
        skipped = [k for k, v in parsed.items() if not isinstance(v, dict)]
        logger.warning(
            "Skipped non-dict entries in %s: %s", KIMI_AGENT_CONFIG, skipped,
        )
    return _agent_config_cache


def _pid_path(agent_id: str) -> Path:
    return KIMI_PIDS_DIR / f"{agent_id}.pid"


def _session_marker_path(agent_id: str) -> Path:
    """Per-agent marker recording that a previous send() Popen succeeded.

    The marker's existence means "the previous send() spawned a kimi process
    successfully". It does NOT guarantee that a Kimi server-side session for
    this agent's cwd is currently alive — Kimi has no ``--list-sessions``
    equivalent, so the marker is a best-effort substitute. See module-level
    notes and Issue #338 for the rationale and stale-marker behavior.
    """
    return KIMI_PIDS_DIR / f"{agent_id}.has_session"


def _is_kimi_pid_alive(pid: int) -> bool:
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        return False
    try:
        cmdline_bytes = (proc_dir / "cmdline").read_bytes()
    except (OSError, FileNotFoundError):
        return False
    tokens = cmdline_bytes.split(b"\0")
    for t in tokens:
        try:
            s = t.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if s == "kimi" or s.endswith("/kimi"):
            return True
    return False


def _terminate_pid_tree(
    pid: int,
    agent_id: str,
    proc: subprocess.Popen | None = None,
) -> bool:
    """Best-effort: PGID 単位で SIGTERM→wait→SIGKILL。"""
    try:
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError) as e:
        logger.warning(
            "kimi getpgid(%d) failed for %s: %s; falling back to pid-only kill",
            pid, agent_id, e,
        )
        pgid = pid

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as e:
        logger.warning("kimi SIGTERM to pgid %d failed for %s: %s", pgid, agent_id, e)

    if proc is not None:
        try:
            proc.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            pass
    else:
        for _ in range(10):
            if not _is_kimi_pid_alive(pid):
                return True
            time.sleep(0.5)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError as e:
        logger.warning("kimi SIGKILL to pgid %d failed for %s: %s", pgid, agent_id, e)

    if proc is not None:
        try:
            proc.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            logger.warning("kimi proc %d did not exit after SIGKILL", pid)
            return False
    for _ in range(10):
        if not _is_kimi_pid_alive(pid):
            return True
        time.sleep(0.5)
    logger.warning("kimi proc %d still alive after SIGKILL for %s", pid, agent_id)
    return False


def _rebuild_kimi_md(agent_id: str) -> None:
    """Rebuild KIMI.md from IDENTITY.md + INSTRUCTION.md + MEMORY.md.

    IMPORTANT: This function is a near-exact copy of
    backend_pi.py:_rebuild_agents_md and backend_gemini.py:_rebuild_gemini_md.
    The three functions MUST stay in sync beyond filename differences. If you
    modify the logic here, mirror the change to pi and gemini (and vice versa),
    or extract a shared helper.
    """
    try:
        config_data = _load_config()
        agent_profile = config_data.get(agent_id, {})
        compile_flag = agent_profile.get("compile-startup-md", False)
        if not isinstance(compile_flag, bool):
            logger.warning(
                "_rebuild_kimi_md: compile-startup-md for %s has non-bool value %r; "
                "treating as False",
                agent_id, compile_flag,
            )
            compile_flag = False
        if not compile_flag:
            return

        profile_dir = AGENT_PROFILES_DIR / agent_id
        if not profile_dir.is_dir():
            return

        instruction_path = profile_dir / "INSTRUCTION.md"
        memory_path = profile_dir / "MEMORY.md"
        identity_path = profile_dir / "IDENTITY.md"

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

        kimi_md_path = profile_dir / "KIMI.md"
        hash_path = profile_dir / ".kimi_hash"

        if identity_bytes == b"" and instruction_bytes == b"" and memory_bytes == b"":
            kimi_md_path.unlink(missing_ok=True)
            hash_path.unlink(missing_ok=True)
            return

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

        if old_hash == new_hash and kimi_md_path.exists():
            return

        identity_text = identity_bytes.decode("utf-8").rstrip()
        instruction_text = instruction_bytes.decode("utf-8").rstrip()
        memory_text = memory_bytes.decode("utf-8").rstrip()

        parts = [t for t in (identity_text, instruction_text, memory_text) if t]

        if not parts:
            kimi_md_path.unlink(missing_ok=True)
            hash_path.unlink(missing_ok=True)
            return

        output = "\n\n---\n\n".join(parts) + "\n"

        kimi_md_path.write_text(output, encoding="utf-8")
        hash_path.write_text(new_hash + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("_rebuild_kimi_md: failed for %s: %s", agent_id, exc)


def send(agent_id: str, message: str, timeout: int) -> SendResult:
    """Fire-and-forget subprocess launch of ``kimi``."""
    if config.DRY_RUN:
        logger.info("[dry-run] kimi send skipped (agent=%s)", agent_id)
        return SendResult.OK

    KIMI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
    _rebuild_kimi_md(agent_id)

    config_data = _load_config()
    profile = config_data.get(agent_id, {})

    profile_dir = AGENT_PROFILES_DIR / agent_id
    if not profile_dir.is_dir():
        logger.warning(
            "kimi send refused for %s: profile dir %s does not exist. "
            "kimi backend requires a dedicated cwd per agent to avoid cross-agent "
            "session contamination (session is cwd-scoped).",
            agent_id, profile_dir,
        )
        return SendResult.FAIL
    cwd = profile_dir

    has_prev = _session_marker_path(agent_id).exists()

    # --quiet expands to "--print --output-format text --final-message-only"
    # which currently implies --yolo, but we pass -y explicitly to remain
    # robust if that implicit behavior changes in a future kimi release.
    cmd: list[str] = [config.KIMI_BIN, "--quiet", "-y", "-p", message]
    model = profile.get("model")
    if isinstance(model, str) and model.strip():
        cmd.extend(["-m", model.strip()])
    if has_prev:
        cmd.append("-C")

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(cwd),
            start_new_session=True,
        )
    except (OSError, FileNotFoundError) as e:
        logger.warning("kimi spawn failed for %s: %s", agent_id, e)
        return SendResult.FAIL

    try:
        _pid_path(agent_id).write_text(str(proc.pid))
    except OSError as exc:
        logger.warning(
            "kimi pid write failed for %s: %s; terminating spawned process group",
            agent_id, exc,
        )
        if not _terminate_pid_tree(proc.pid, agent_id, proc=proc):
            logger.error(
                "kimi send for %s: spawned pid %d could not be terminated "
                "after pid-write failure; session for cwd %s may be contaminated. "
                "Manual intervention required: kill pid %d and run reset_session "
                "for this agent.",
                agent_id, proc.pid, cwd, proc.pid,
            )
        return SendResult.FAIL

    try:
        _session_marker_path(agent_id).touch()
    except OSError as exc:
        logger.warning(
            "kimi session-marker touch failed for %s: %s; next send will not use -C",
            agent_id, exc,
        )

    return SendResult.OK


def ping(agent_id: str, timeout: int) -> bool:
    """Always returns True. Signature parity only."""
    return True


def is_inactive(agent_id: str, pipeline_data: dict | None = None,
                *, cc_running: bool = False) -> bool:
    """Return whether the agent should be considered inactive."""
    if cc_running:
        return False

    try:
        pid_text = _pid_path(agent_id).read_text().strip()
        pid = int(pid_text)
    except (OSError, FileNotFoundError, ValueError):
        return True

    return not _is_kimi_pid_alive(pid)


def reset_session(agent_id: str) -> None:
    """Best-effort session reset.

    Terminates the recorded live process (if any), removes the pid file, and
    removes the local session marker so the next send() will not pass ``-C``.
    Server/local kimi session storage (``~/.kimi/...``) is NOT touched: kimi
    has no ``--delete-session`` equivalent and the on-disk session layout is
    not officially specified, so we avoid reaching into it. Once the marker
    is gone, the watchdog treats the agent as having no continuable session,
    which is sufficient in practice.
    """
    _rebuild_kimi_md(agent_id)

    try:
        pid_text = _pid_path(agent_id).read_text().strip()
        pid = int(pid_text)
    except (OSError, FileNotFoundError, ValueError):
        pid = None

    if pid is not None and _is_kimi_pid_alive(pid):
        logger.info(
            "kimi reset_session: terminating live process %d for %s",
            pid, agent_id,
        )
        if not _terminate_pid_tree(pid, agent_id, proc=None):
            logger.warning(
                "kimi reset_session for %s: failed to terminate live process %d",
                agent_id, pid,
            )
            return

    try:
        _pid_path(agent_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "kimi reset_session: failed to delete pid file for %s: %s",
            agent_id, exc,
        )

    try:
        _session_marker_path(agent_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "kimi reset_session: failed to delete session marker for %s: %s",
            agent_id, exc,
        )
