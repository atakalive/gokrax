"""engine/backend_codex.py - codex (OpenAI Codex CLI) backend for agent communication.

Provides send/ping/is_inactive/soft_reap/reset_session for agents running via
the ``codex`` CLI (``codex exec``).

Codex characteristics:
    - Oneshot process (one prompt = one process, completes and exits)
    - Sessions are NOT cwd-scoped: they are stored under ``~/.codex/sessions/``
      and addressed by thread id
    - Continuation: the thread id is harvested from the first
      ``thread.started`` event of the ``--json`` stdout of the previous run and
      the next send continues it with ``codex exec resume <id> -``
      (``resume --last`` is deliberately not used: it could pick up manual runs
      or multi_agent sub-threads)
    - The prompt is written to ``<agent>.prompt`` and fed via stdin (``-``)
    - cwd is ``AGENT_PROFILES_DIR/<agent>``; Codex reads ``AGENTS.md`` from it
      (also on resume)

Liveness invariant:
    A run is in progress iff ``<agent>.out.jsonl`` is flock-held. send() takes
    ``LOCK_EX`` on the freshly opened out file right before spawn and hands the
    fd to the child as stdout, so the lock is inherited by the whole codex
    process group (node wrapper and native binary) and released only when all
    of them have exited or been killed. The pid file only names the kill
    target; it is trusted only when its pid, the /proc start time and the
    cmdline (a codex token plus ``exec``) all still match.

Limitation (hang detection):
    ``codex exec --json`` writes nothing while a shell command runs, so a
    single command longer than INACTIVE_THRESHOLD_SEC is false-reaped on the
    next send. Session id and output are kept, and the next send resumes the
    thread, so context is not lost (same class of limitation as agy).

Manual recovery:
    If a run is in progress but its pid record is missing or no longer owned,
    nothing is killed (fail-closed): send() keeps returning BUSY (escalated to
    FAIL after BUSY_ESCALATION_SEC) and soft_reap/reset_session return without
    deleting anything. Identify the lock holder with
    ``fuser -v <CODEX_PIDS_DIR>/<agent>.out.jsonl`` and kill it; the next send
    harvests the output and resumes.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import NamedTuple

import config
from config import (
    AGENT_PROFILES_DIR,
    CODEX_AGENT_CONFIG,
    CODEX_PIDS_DIR,
    INACTIVE_THRESHOLD_SEC,
)
from engine.backend_types import SendResult
from engine.boost import overlay_profile

logger = logging.getLogger(__name__)

# All regexes below are used with fullmatch (re.match + "$" accepts a trailing "\n").
_SESSION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EFFORT_RE = re.compile(r"[a-z]+")
_PARSE_MAX_LINES = 200
_PROJECT_DOC_MAX_BYTES = 32 * 1024
_ERR_TAIL_BYTES = 800

# ---------------------------------------------------------------------------
# Per-agent config (agents/config_codex.json)
# ---------------------------------------------------------------------------
_agent_config_cache: dict[str, dict[str, object]] | None = None


def _load_config() -> dict[str, dict[str, object]]:
    """Load and cache agents/config_codex.json. Called once per process lifetime."""
    global _agent_config_cache
    if _agent_config_cache is not None:
        return _agent_config_cache

    try:
        text = CODEX_AGENT_CONFIG.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        _agent_config_cache = {}
        return _agent_config_cache
    except OSError as exc:
        logger.warning("Failed to read %s: %s", CODEX_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not text:
        _agent_config_cache = {}
        return _agent_config_cache

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in %s: %s", CODEX_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not isinstance(parsed, dict):
        logger.warning(
            "Expected JSON object in %s, got %s",
            CODEX_AGENT_CONFIG, type(parsed).__name__,
        )
        _agent_config_cache = {}
        return _agent_config_cache

    _agent_config_cache = {
        k: v for k, v in parsed.items() if isinstance(v, dict)
    }
    if len(_agent_config_cache) < len(parsed):
        skipped = [k for k, v in parsed.items() if not isinstance(v, dict)]
        logger.warning(
            "Skipped non-dict entries in %s: %s", CODEX_AGENT_CONFIG, skipped,
        )
    return _agent_config_cache


# ---------------------------------------------------------------------------
# State files (all under CODEX_PIDS_DIR)
# ---------------------------------------------------------------------------

def _lock_path(agent_id: str) -> Path:
    return CODEX_PIDS_DIR / f"{agent_id}.lock"


def _pid_path(agent_id: str) -> Path:
    return CODEX_PIDS_DIR / f"{agent_id}.pid"


def _session_id_path(agent_id: str) -> Path:
    return CODEX_PIDS_DIR / f"{agent_id}.session_id"


def _out_path(agent_id: str) -> Path:
    return CODEX_PIDS_DIR / f"{agent_id}.out.jsonl"


def _err_path(agent_id: str) -> Path:
    return CODEX_PIDS_DIR / f"{agent_id}.err.log"


def _prompt_path(agent_id: str) -> Path:
    return CODEX_PIDS_DIR / f"{agent_id}.prompt"


@contextlib.contextmanager
def _per_agent_lock(agent_id: str) -> Generator[None, None, None]:
    CODEX_PIDS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(agent_id)
    with open(lock_path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------

def _valid_model(profile: dict[str, object]) -> str | None:
    """Return the stripped model slug if it is safe to pass as ``-m``, else None."""
    model = profile.get("model")
    if not isinstance(model, str):
        return None
    model = model.strip()
    if not model or model.startswith("-") or "\x00" in model:
        return None
    return model


def _valid_effort(profile: dict[str, object]) -> str | None:
    """Return the effort if it is lowercase letters only, else None."""
    effort = profile.get("effort")
    if isinstance(effort, str) and _EFFORT_RE.fullmatch(effort):
        return effort
    return None


def _build_cmd(
    agent_id: str,
    profile: dict[str, object],
    session_id: str | None,
) -> list[str]:
    flags = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
    model = _valid_model(profile)
    raw_model = profile.get("model")
    if model is None and isinstance(raw_model, str) and raw_model.strip():
        logger.warning(
            "codex: invalid model %r for %s (starts with '-' or contains NUL); "
            "omitting -m", raw_model, agent_id,
        )
    if model is not None:
        flags += ["-m", model]
    effort = _valid_effort(profile)
    raw_effort = profile.get("effort")
    if effort is None and raw_effort is not None and raw_effort != "":
        logger.warning(
            "codex: invalid effort %r for %s (lowercase letters only); "
            "omitting model_reasoning_effort", raw_effort, agent_id,
        )
    if effort is not None:
        flags += ["-c", f'model_reasoning_effort="{effort}"']
    if session_id is None:
        return [config.CODEX_BIN, "exec", *flags, "-"]
    return [config.CODEX_BIN, "exec", "resume", *flags, session_id, "-"]


# ---------------------------------------------------------------------------
# Liveness / kill
# ---------------------------------------------------------------------------

def _run_in_progress(agent_id: str) -> bool:
    """Return True iff the out file is flock-held (a codex run is alive).

    Probes with ``LOCK_SH | LOCK_NB`` so concurrent probes do not block each
    other; the running child holds ``LOCK_EX``. Fail-closed on unexpected errors.
    """
    out_path = _out_path(agent_id)
    try:
        f = open(out_path, "rb")
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning(
            "codex: cannot open %s for %s: %s; treating run as in progress",
            out_path, agent_id, exc,
        )
        return True
    with f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as exc:
            logger.warning(
                "codex: flock probe on %s failed for %s: %s; treating run as in progress",
                out_path, agent_id, exc,
            )
            return True
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return False


def _proc_starttime(pid: int) -> int | None:
    """Return field 22 (starttime) of /proc/<pid>/stat, or None.

    Splits after the LAST ")" so a comm containing spaces or ")" is handled.
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return int(text[text.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _read_pid_record(agent_id: str) -> tuple[int, int] | None:
    try:
        parts = _pid_path(agent_id).read_text(encoding="utf-8").split()
        if len(parts) != 2:
            return None
        pid, starttime = int(parts[0]), int(parts[1])
    except (OSError, ValueError):
        return None
    if pid <= 0 or starttime <= 0:
        return None
    return pid, starttime


def _write_pid_record(agent_id: str, pid: int, starttime: int) -> bool:
    try:
        _pid_path(agent_id).write_text(f"{pid} {starttime}\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("codex: pid write failed for %s: %s", agent_id, exc)
        return False
    return True


def _is_owned_codex_pid(pid: int, starttime: int) -> bool:
    """True iff pid is still the recorded ``codex exec`` process (start time + cmdline)."""
    if _proc_starttime(pid) != starttime:
        return False
    proc_dir = Path(f"/proc/{pid}")
    try:
        if not proc_dir.exists():
            return False
        cmdline_bytes = (proc_dir / "cmdline").read_bytes()
    except OSError:
        return False
    has_codex = False
    has_exec = False
    for t in cmdline_bytes.split(b"\0"):
        try:
            s = t.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if s == "codex" or s.endswith("/codex") or s.endswith("/codex.js"):
            has_codex = True
        if s == "exec":
            has_exec = True
    return has_codex and has_exec


def _codex_last_activity(agent_id: str) -> float | None:
    """Return the most recent mtime among out/err/pid files, or None.

    The pid file mtime (spawn time) doubles as the launch-grace floor.
    """
    mtimes: list[float] = []
    for path in (_out_path(agent_id), _err_path(agent_id), _pid_path(agent_id)):
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else None


def _terminate_run(
    agent_id: str,
    pid: int,
    proc: subprocess.Popen | None = None,
) -> bool:
    """PGID-wide SIGTERM → wait → SIGKILL. Success means the out lock was released.

    Only call with an ownership-verified pid (via _kill_running) or the pid of
    a process this caller just spawned.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)  # start_new_session=True → pgid == pid
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.warning(
                "codex: killpg(%d, %s) failed for %s: %s", pid, sig.name, agent_id, exc,
            )
        if proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        for _ in range(10):
            if not _run_in_progress(agent_id):
                return True
            time.sleep(0.5)
    logger.warning(
        "codex: run of pgid %d for %s still holds %s after SIGKILL",
        pid, agent_id, _out_path(agent_id),
    )
    return False


def _kill_running(agent_id: str, where: str) -> bool:
    """Ensure no codex run is in progress. Returns True if none remains."""
    if not _run_in_progress(agent_id):
        return True
    rec = _read_pid_record(agent_id)
    if rec is None or not _is_owned_codex_pid(*rec):
        logger.warning(
            "codex %s: codex run for %s is in progress but its pid record %r is "
            "missing or not owned; refusing to kill. Manual recovery: "
            "fuser -v %s, kill the holder; the next send harvests and resumes.",
            where, agent_id, rec, _out_path(agent_id),
        )
        return False
    if not _terminate_run(agent_id, rec[0]):
        logger.warning(
            "codex %s: failed to terminate codex run %r for %s. Manual recovery: "
            "fuser -v %s, kill the holder; the next send harvests and resumes.",
            where, rec, agent_id, _out_path(agent_id),
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Session harvest
# ---------------------------------------------------------------------------

def _read_session_id(agent_id: str) -> str | None:
    try:
        sid = _session_id_path(agent_id).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return sid if _SESSION_ID_RE.fullmatch(sid) else None


def _write_session_id(agent_id: str, session_id: str) -> bool:
    path = _session_id_path(agent_id)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(session_id + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("codex: session_id write failed for %s: %s", agent_id, exc)
        return False
    return True


def _parse_event_line(raw: bytes) -> dict[str, object] | None:
    try:
        ev = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return ev if isinstance(ev, dict) else None


def _launch_header(session_id: str | None) -> bytes:
    return (
        json.dumps({"type": "gokrax.launch", "session_id": session_id}, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


class _RunOutput(NamedTuple):
    header_ok: bool
    launched_sid: str | None
    thread_id: str | None
    saw_thread_started: bool
    saw_turn_event: bool


def _parse_run_output(path: Path) -> _RunOutput:
    """Parse the launch header and the first events of a run's stdout.

    Only the first ``thread.started`` is considered (later ones are
    multi_agent sub-threads). Stops as soon as both a ``thread.started`` and a
    turn/item event were seen, to avoid reading huge ``item.completed`` lines.
    Raises OSError.
    """
    header_ok = False
    launched_sid: str | None = None
    thread_id: str | None = None
    saw_thread_started = False
    saw_turn_event = False
    with open(path, "rb") as f:
        for i, raw in enumerate(f):
            if i >= _PARSE_MAX_LINES:
                break
            ev = _parse_event_line(raw)
            if i == 0 and ev is not None and ev.get("type") == "gokrax.launch":
                sid = ev.get("session_id")
                if sid is None:
                    header_ok = True
                    continue
                if isinstance(sid, str) and _SESSION_ID_RE.fullmatch(sid):
                    header_ok = True
                    launched_sid = sid
                    continue
            if ev is None:
                continue
            etype = ev.get("type")
            if etype == "thread.started" and not saw_thread_started:
                tid = ev.get("thread_id")
                if isinstance(tid, str) and _SESSION_ID_RE.fullmatch(tid):
                    thread_id = tid
                saw_thread_started = True
            if isinstance(etype, str) and (etype.startswith("turn.") or etype.startswith("item.")):
                saw_turn_event = True
            if saw_thread_started and saw_turn_event:
                break
    return _RunOutput(header_ok, launched_sid, thread_id, saw_thread_started, saw_turn_event)


def _stderr_tail(agent_id: str) -> str:
    try:
        with open(_err_path(agent_id), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _ERR_TAIL_BYTES))
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()


def _harvest_session(agent_id: str) -> bool:
    """Harvest the previous run's thread id into the session_id file.

    Call only after confirming ``_run_in_progress`` is False. Returns True when
    harvested (or nothing to harvest), False when the state update failed (the
    caller must not spawn; the out file is kept so the next send re-reads it).

    The outcome depends only on the launch header and the out/err contents, so
    re-reading the same out (e.g. after a failed unlink) is idempotent.

    A resume run without any turn event (but with evidence that codex ran:
    thread.started, a turn/item event or stderr output) discards the session
    id — with a compare-and-delete against the launched id — so a broken
    thread is not resumed forever. ``thread.started`` and ``turn.started`` are
    written almost together right after launch, so a legitimate session is
    only discarded if the process dies in between; the next send then starts a
    fresh session and the nudge refresher restores context.
    """
    out_path = _out_path(agent_id)
    if not out_path.exists():
        return True
    try:
        r = _parse_run_output(out_path)
    except OSError as exc:
        logger.warning("codex harvest: failed to read %s for %s: %s", out_path, agent_id, exc)
        return False

    cur_sid = _read_session_id(agent_id)
    tail = _stderr_tail(agent_id)
    ran = r.saw_thread_started or r.saw_turn_event or tail != ""

    ok = True
    if not r.header_ok:
        logger.warning(
            "codex harvest: %s for %s has no valid launch header; leaving session id untouched",
            out_path, agent_id,
        )
    elif r.launched_sid is not None:
        if not r.saw_turn_event:
            if ran:
                logger.warning(
                    "codex harvest: resume of %s for %s produced no turn; discarding "
                    "session id. stderr tail: %s",
                    r.launched_sid, agent_id, tail,
                )
                if cur_sid == r.launched_sid:
                    try:
                        _session_id_path(agent_id).unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "codex harvest: failed to delete session id for %s: %s",
                            agent_id, exc,
                        )
                        ok = False
            else:
                logger.warning(
                    "codex harvest: resume of %s for %s left no output and no stderr "
                    "(not launched or killed before output); keeping session id",
                    r.launched_sid, agent_id,
                )
        elif r.thread_id is not None:
            ok = _write_session_id(agent_id, r.thread_id)
        else:
            logger.warning(
                "codex harvest: resume of %s for %s ran a turn but emitted no valid "
                "thread.started; leaving session id untouched",
                r.launched_sid, agent_id,
            )
    else:
        if r.thread_id is not None:
            ok = _write_session_id(agent_id, r.thread_id)
            if not r.saw_turn_event:
                logger.warning(
                    "codex harvest: new thread %s for %s ended before any turn. "
                    "stderr tail: %s",
                    r.thread_id, agent_id, tail,
                )
        elif r.saw_turn_event:
            logger.warning(
                "codex harvest: new run for %s ran a turn but no valid thread id was "
                "found. stderr tail: %s",
                agent_id, tail,
            )
        else:
            logger.warning(
                "codex harvest: new run for %s produced no thread and no turn. "
                "stderr tail: %s",
                agent_id, tail,
            )

    if not ok:
        return False
    try:
        out_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "codex harvest: failed to delete %s for %s: %s (harmless; harvest is idempotent)",
            out_path, agent_id, exc,
        )
    return True


# ---------------------------------------------------------------------------
# AGENTS.md
# ---------------------------------------------------------------------------

def _rebuild_codex_md(agent_id: str) -> None:
    """Rebuild AGENTS.md from IDENTITY.md + INSTRUCTION.md + MEMORY.md.

    IMPORTANT: This function is a near-exact copy of
    backend_pi.py:_rebuild_agents_md, backend_gemini.py:_rebuild_gemini_md,
    backend_kimi.py:_rebuild_kimi_md, and backend_agy.py:_rebuild_agy_md.
    The five functions MUST stay in sync beyond filename differences. If you
    modify the logic here, mirror the change to the other four (and vice
    versa), or extract a shared helper.
    The only intentional logic difference is the WARNING emitted when the
    compiled AGENTS.md exceeds Codex's project_doc_max_bytes (32 KiB).
    """
    try:
        config_data = _load_config()
        agent_profile = config_data.get(agent_id, {})
        compile_flag = agent_profile.get("compile-startup-md", False)
        if not isinstance(compile_flag, bool):
            logger.warning(
                "_rebuild_codex_md: compile-startup-md for %s has non-bool value %r; "
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

        codex_md_path = profile_dir / "AGENTS.md"
        hash_path = profile_dir / ".codex_hash"

        if identity_bytes == b"" and instruction_bytes == b"" and memory_bytes == b"":
            codex_md_path.unlink(missing_ok=True)
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

        if old_hash == new_hash and codex_md_path.exists():
            return

        identity_text = identity_bytes.decode("utf-8").rstrip()
        instruction_text = instruction_bytes.decode("utf-8").rstrip()
        memory_text = memory_bytes.decode("utf-8").rstrip()

        parts = [t for t in (identity_text, instruction_text, memory_text) if t]

        if not parts:
            codex_md_path.unlink(missing_ok=True)
            hash_path.unlink(missing_ok=True)
            return

        output = "\n\n---\n\n".join(parts) + "\n"

        if len(output.encode("utf-8")) > _PROJECT_DOC_MAX_BYTES:
            logger.warning(
                "_rebuild_codex_md: AGENTS.md for %s is %d bytes, exceeding Codex's "
                "project_doc_max_bytes (%d); Codex will truncate it",
                agent_id, len(output.encode("utf-8")), _PROJECT_DOC_MAX_BYTES,
            )
        codex_md_path.write_text(output, encoding="utf-8")
        hash_path.write_text(new_hash + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("_rebuild_codex_md: failed for %s: %s", agent_id, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send(agent_id: str, message: str, timeout: int, *, boost: bool = False) -> SendResult:
    """Fire-and-forget launch of ``codex exec`` (``timeout`` is unused)."""
    if config.DRY_RUN:
        logger.info("[dry-run] codex send skipped (agent=%s)", agent_id)
        return SendResult.OK

    with _per_agent_lock(agent_id):
        profile_dir = AGENT_PROFILES_DIR / agent_id
        if not profile_dir.is_dir():
            logger.warning(
                "codex send refused for %s: profile dir %s does not exist. "
                "codex backend requires a dedicated cwd per agent.",
                agent_id, profile_dir,
            )
            return SendResult.FAIL

        if _run_in_progress(agent_id):
            la = _codex_last_activity(agent_id)
            if la is not None and time.time() - la < INACTIVE_THRESHOLD_SEC:
                logger.info("codex send: run for %s still in progress; BUSY", agent_id)
                return SendResult.BUSY
            logger.warning(
                "codex send: run for %s looks hung (last activity %s); reaping",
                agent_id, la,
            )
            if not _kill_running(agent_id, "send"):
                return SendResult.BUSY

        # No run in progress: any leftover pid file belongs to a finished run.
        try:
            _pid_path(agent_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("codex send: failed to delete stale pid file for %s: %s", agent_id, exc)

        if not _harvest_session(agent_id):
            return SendResult.FAIL

        _rebuild_codex_md(agent_id)

        profile = _load_config().get(agent_id, {})
        if boost:
            profile = overlay_profile(profile, "codex")
        session_id = _read_session_id(agent_id)
        cmd = _build_cmd(agent_id, profile, session_id)

        prompt_path = _prompt_path(agent_id)
        out_path = _out_path(agent_id)
        err_path = _err_path(agent_id)
        try:
            prompt_path.write_text(message, encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("codex send: prompt write failed for %s: %s", agent_id, exc)
            return SendResult.FAIL

        try:
            with open(prompt_path, "rb") as fin, open(out_path, "wb") as fout, \
                    open(err_path, "wb") as ferr:
                fcntl.flock(fout.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fout.write(_launch_header(session_id))
                fout.flush()
                proc = subprocess.Popen(
                    cmd, stdin=fin, stdout=fout, stderr=ferr,
                    cwd=str(profile_dir), start_new_session=True,
                )
        except BlockingIOError:
            # 別のプロセスが _run_in_progress で一瞬だけ LOCK_SH を持っていた。次の tick で再試行する
            with contextlib.suppress(OSError):
                out_path.unlink(missing_ok=True)
            return SendResult.BUSY
        except (OSError, ValueError) as e:
            logger.warning("codex spawn failed for %s: %s", agent_id, e)
            with contextlib.suppress(OSError):
                out_path.unlink(missing_ok=True)
            return SendResult.FAIL

        starttime = _proc_starttime(proc.pid)
        if starttime is None or not _write_pid_record(agent_id, proc.pid, starttime):
            logger.warning(
                "codex send: could not record pid %d (starttime=%s) for %s; "
                "terminating spawned process group",
                proc.pid, starttime, agent_id,
            )
            if _terminate_run(agent_id, proc.pid, proc=proc):
                with contextlib.suppress(OSError):
                    out_path.unlink(missing_ok=True)
                return SendResult.FAIL
            logger.error(
                "codex send for %s: spawned pid %d could not be terminated after "
                "pid-record failure; it still holds %s so further sends stay BUSY. "
                "Manual recovery: kill -- -%d (or fuser -v %s and kill the holder).",
                agent_id, proc.pid, out_path, proc.pid, out_path,
            )
            return SendResult.FAIL

        return SendResult.OK


def ping(agent_id: str, timeout: int) -> bool:
    """Always returns True. Signature parity only."""
    return True


def is_inactive(agent_id: str, pipeline_data: dict | None = None,
                *, cc_running: bool = False) -> bool:
    """Return whether the agent should be considered inactive (no lock taken)."""
    if cc_running:
        return False
    if not _run_in_progress(agent_id):
        return True
    la = _codex_last_activity(agent_id)
    return la is None or (time.time() - la) >= INACTIVE_THRESHOLD_SEC


def soft_reap(agent_id: str) -> None:
    """Terminate a lingering codex run while PRESERVING session context.

    Keeps the session_id and out files so the next send() harvests the output
    and resumes the same thread; only the run is killed and the pid file cleared.

    Safety: codex state files are per agent_id (global, not per project). The
    caller must ensure the run belongs to its own project to avoid killing
    another project's active review (the canonical caller filters targets via
    the project-scoped prev_reviews set).
    """
    if config.DRY_RUN:
        return
    with _per_agent_lock(agent_id):
        if not _kill_running(agent_id, "soft_reap"):
            return
        try:
            _pid_path(agent_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "codex soft_reap: failed to delete pid file for %s: %s", agent_id, exc,
            )


def reset_session(agent_id: str) -> None:
    """Best-effort session reset.

    Terminates the running codex (if owned) and removes all local state files
    so the next send() starts a new thread. ``~/.codex`` is NOT touched. If a
    run is in progress but cannot be terminated, nothing is deleted.
    """
    with _per_agent_lock(agent_id):
        _rebuild_codex_md(agent_id)
        if not _kill_running(agent_id, "reset_session"):
            return
        session_path = _session_id_path(agent_id)
        for path in (
            _pid_path(agent_id),
            session_path,
            session_path.with_name(session_path.name + ".tmp"),
            _out_path(agent_id),
            _err_path(agent_id),
            _prompt_path(agent_id),
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "codex reset_session: failed to delete %s for %s: %s",
                    path, agent_id, exc,
                )
