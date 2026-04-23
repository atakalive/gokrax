"""engine/backend_gemini.py - gemini backend for agent communication.

Provides send/ping/is_inactive/reset_session for agents running via the ``gemini`` CLI.

Gemini characteristics:
    - Oneshot process (one prompt = one process, completes and exits)
    - Sessions are managed server-side, scoped per-cwd (project scope); the
      client cannot specify a session_id.
    - Continuation: ``-r latest`` resumes the most recent session for the cwd.
    - List: ``--list-sessions`` (header ``Available sessions for this project (N):``
      or ``No sessions found for this project.``).
    - Delete: ``--delete-session <index>``.

Liveness invariant:
    pid file exists, /proc/<pid> exists, and cmdline contains "gemini".
    No starting-grace mechanism is needed because pid is written immediately
    after ``Popen`` (no session-file appearance delay to bridge).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import config
from config import (
    AGENT_PROFILES_DIR,
    GEMINI_AGENT_CONFIG,
    GEMINI_PIDS_DIR,
)
from engine.backend_types import SendResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-agent config (agents/config_gemini.json)
# ---------------------------------------------------------------------------
_agent_config_cache: dict[str, dict[str, object]] | None = None


def _load_config() -> dict[str, dict[str, object]]:
    """Load and cache agents/config_gemini.json. Called once per process lifetime."""
    global _agent_config_cache
    if _agent_config_cache is not None:
        return _agent_config_cache

    try:
        text = GEMINI_AGENT_CONFIG.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        _agent_config_cache = {}
        return _agent_config_cache
    except OSError as exc:
        logger.warning("Failed to read %s: %s", GEMINI_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not text:
        _agent_config_cache = {}
        return _agent_config_cache

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in %s: %s", GEMINI_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not isinstance(parsed, dict):
        logger.warning(
            "Expected JSON object in %s, got %s",
            GEMINI_AGENT_CONFIG, type(parsed).__name__,
        )
        _agent_config_cache = {}
        return _agent_config_cache

    _agent_config_cache = {
        k: v for k, v in parsed.items() if isinstance(v, dict)
    }
    if len(_agent_config_cache) < len(parsed):
        skipped = [k for k, v in parsed.items() if not isinstance(v, dict)]
        logger.warning(
            "Skipped non-dict entries in %s: %s", GEMINI_AGENT_CONFIG, skipped,
        )
    return _agent_config_cache


def _pid_path(agent_id: str) -> Path:
    return GEMINI_PIDS_DIR / f"{agent_id}.pid"


def _is_gemini_pid_alive(pid: int) -> bool:
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
        if s == "gemini" or s.endswith("/gemini"):
            return True
    return False


def _count_sessions(cwd: Path) -> int | None:
    """Return session count for the cwd-scoped Gemini project.

    Returns:
        int >= 0: parsed count (explicit "No sessions found" → 0).
        None: subprocess failure, non-zero exit, or unrecognized stdout
            (treated as drift — caller decides).
    """
    try:
        r = subprocess.run(
            [config.GEMINI_BIN, "--list-sessions"],
            cwd=str(cwd), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"Available sessions for this project\s*\((\d+)\)", r.stdout)
    if m:
        return int(m.group(1))
    if "No sessions found for this project" in r.stdout:
        return 0
    # Output drift: neither known header matched — surface it so operators can
    # diagnose format changes in the Gemini CLI.
    logger.warning(
        "gemini _count_sessions: unrecognized --list-sessions output (drift); "
        "stdout head=%r",
        r.stdout[:200],
    )
    return None


def _terminate_pid_tree(
    pid: int,
    agent_id: str,
    proc: subprocess.Popen | None = None,
) -> bool:
    """Best-effort: PGID 単位で SIGTERM→wait→SIGKILL。

    Args:
        pid: 停止対象プロセスの pid。
        agent_id: ログ文言用。
        proc: Popen オブジェクトがあれば wait() に使う。無ければ wait はスキップし、
              ``_is_gemini_pid_alive`` のポーリングで終了確認する。

    Returns:
        True when the process is confirmed terminated (or was already gone);
        False when the process may still be alive after SIGKILL + wait.
    """
    try:
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError) as e:
        logger.warning(
            "gemini getpgid(%d) failed for %s: %s; falling back to pid-only kill",
            pid, agent_id, e,
        )
        pgid = pid

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        # already terminated
        return True
    except OSError as e:
        logger.warning("gemini SIGTERM to pgid %d failed for %s: %s", pgid, agent_id, e)

    if proc is not None:
        try:
            proc.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            pass
    else:
        for _ in range(10):
            if not _is_gemini_pid_alive(pid):
                return True
            time.sleep(0.5)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        # already terminated
        return True
    except OSError as e:
        logger.warning("gemini SIGKILL to pgid %d failed for %s: %s", pgid, agent_id, e)

    if proc is not None:
        try:
            proc.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            logger.warning("gemini proc %d did not exit after SIGKILL", pid)
            return False
    for _ in range(10):
        if not _is_gemini_pid_alive(pid):
            return True
        time.sleep(0.5)
    logger.warning("gemini proc %d still alive after SIGKILL for %s", pid, agent_id)
    return False


def _rebuild_gemini_md(agent_id: str) -> None:
    """Rebuild GEMINI.md from IDENTITY.md + INSTRUCTION.md + MEMORY.md.

    IMPORTANT: This function is a near-exact copy of
    backend_pi.py:_rebuild_agents_md. If you modify the logic here, mirror the
    change to pi (and vice versa), or extract a shared helper. The two functions
    MUST stay in sync beyond filename differences.
    """
    try:
        config_data = _load_config()
        agent_profile = config_data.get(agent_id, {})
        compile_flag = agent_profile.get("compile-startup-md", False)
        if not isinstance(compile_flag, bool):
            logger.warning(
                "_rebuild_gemini_md: compile-startup-md for %s has non-bool value %r; "
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

        gemini_md_path = profile_dir / "GEMINI.md"
        hash_path = profile_dir / ".gemini_hash"

        if identity_bytes == b"" and instruction_bytes == b"" and memory_bytes == b"":
            gemini_md_path.unlink(missing_ok=True)
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

        if old_hash == new_hash and gemini_md_path.exists():
            return

        identity_text = identity_bytes.decode("utf-8").rstrip()
        instruction_text = instruction_bytes.decode("utf-8").rstrip()
        memory_text = memory_bytes.decode("utf-8").rstrip()

        parts = [t for t in (identity_text, instruction_text, memory_text) if t]

        if not parts:
            gemini_md_path.unlink(missing_ok=True)
            hash_path.unlink(missing_ok=True)
            return

        output = "\n\n---\n\n".join(parts) + "\n"

        gemini_md_path.write_text(output, encoding="utf-8")
        hash_path.write_text(new_hash + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("_rebuild_gemini_md: failed for %s: %s", agent_id, exc)


def send(agent_id: str, message: str, timeout: int) -> SendResult:
    """Fire-and-forget subprocess launch of ``gemini``.

    Returns SendResult.OK on spawn + pid persistence success, SendResult.FAIL
    otherwise. Profile dir is mandatory (per-agent cwd for session scoping);
    absence logs a warning and returns SendResult.FAIL.
    """
    if config.DRY_RUN:
        logger.info("[dry-run] gemini send skipped (agent=%s)", agent_id)
        return SendResult.OK

    GEMINI_PIDS_DIR.mkdir(parents=True, exist_ok=True)
    _rebuild_gemini_md(agent_id)

    config_data = _load_config()
    profile = config_data.get(agent_id, {})

    profile_dir = AGENT_PROFILES_DIR / agent_id
    if not profile_dir.is_dir():
        logger.warning(
            "gemini send refused for %s: profile dir %s does not exist. "
            "gemini backend requires a dedicated cwd per agent to avoid cross-agent "
            "session contamination (session is cwd-scoped).",
            agent_id, profile_dir,
        )
        return SendResult.FAIL
    cwd = profile_dir

    has_prev = (_count_sessions(cwd) or 0) > 0

    cmd: list[str] = [
        config.GEMINI_BIN,
        "-p", message,
        "--approval-mode", "yolo",
    ]
    model = profile.get("model")
    if isinstance(model, str) and model.strip():
        cmd.extend(["-m", model.strip()])
    if has_prev:
        cmd.extend(["-r", "latest"])

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
        logger.warning("gemini spawn failed for %s: %s", agent_id, e)
        return SendResult.FAIL

    try:
        _pid_path(agent_id).write_text(str(proc.pid))
    except OSError as exc:
        logger.warning(
            "gemini pid write failed for %s: %s; terminating spawned process group",
            agent_id, exc,
        )
        if not _terminate_pid_tree(proc.pid, agent_id, proc=proc):
            # Stray Gemini process could still create/update a cwd-scoped session
            # that the next send() would resume via -r latest, replaying stale
            # context. We cannot write a pid file (that is why we are here), so
            # escalate visibility and require manual intervention.
            logger.error(
                "gemini send for %s: spawned pid %d could not be terminated "
                "after pid-write failure; session for cwd %s may be contaminated. "
                "Manual intervention required: kill pid %d and run reset_session "
                "(or clear --list-sessions) for this agent.",
                agent_id, proc.pid, cwd, proc.pid,
            )
        return SendResult.FAIL

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

    return not _is_gemini_pid_alive(pid)


def reset_session(agent_id: str) -> None:
    """Best-effort session reset.

    Terminates the recorded live process (if any) BEFORE session deletion so an
    in-flight oneshot process cannot re-create the session after we delete it.
    Then deletes the pid file and iterates ``--delete-session <count>`` until
    ``_count_sessions`` returns 0 (safety-capped at 100 iterations).
    """
    _rebuild_gemini_md(agent_id)

    try:
        pid_text = _pid_path(agent_id).read_text().strip()
        pid = int(pid_text)
    except (OSError, FileNotFoundError, ValueError):
        pid = None

    if pid is not None and _is_gemini_pid_alive(pid):
        logger.info(
            "gemini reset_session: terminating live process %d for %s before session deletion",
            pid, agent_id,
        )
        if not _terminate_pid_tree(pid, agent_id, proc=None):
            logger.warning(
                "gemini reset_session for %s: failed to terminate live process %d; "
                "aborting session deletion to avoid re-creation by in-flight process",
                agent_id, pid,
            )
            return

    try:
        _pid_path(agent_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "gemini reset_session: failed to delete pid file for %s: %s",
            agent_id, exc,
        )

    profile_dir = AGENT_PROFILES_DIR / agent_id
    if not profile_dir.is_dir():
        logger.warning(
            "gemini reset_session: profile dir %s does not exist for %s; "
            "skipping session deletion (no per-agent cwd to scope to)",
            profile_dir, agent_id,
        )
        return
    cwd = profile_dir

    for _ in range(100):
        count = _count_sessions(cwd)
        if count is None:
            logger.warning(
                "gemini reset_session for %s: failed to list sessions; "
                "aborting deletion to avoid leaving unreset sessions",
                agent_id,
            )
            return
        if count == 0:
            return
        try:
            subprocess.run(
                [config.GEMINI_BIN, "--delete-session", str(count)],
                cwd=str(cwd), capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("gemini --delete-session failed for %s: %s", agent_id, exc)
            return
    remaining = _count_sessions(cwd)
    remaining_str = str(remaining) if remaining is not None else "unknown"
    logger.warning(
        "gemini reset_session for %s hit safety cap (100 iterations); "
        "remaining sessions: %s",
        agent_id, remaining_str,
    )
