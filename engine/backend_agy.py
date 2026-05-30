"""engine/backend_agy.py - agy (antigravity-cli) backend for agent communication.

Provides send/ping/is_inactive/reset_session for agents running via the ``agy``
CLI (Google antigravity-cli, successor to subscriber gemini-cli).

agy characteristics:
    - Oneshot process (one prompt = one process, completes and exits)
    - Sessions are scoped per-cwd; no ``--list-sessions`` / ``--delete-session``
    - Continuation: ``-c`` / ``--continue`` resumes the most recent cwd session
    - Model is selected via ``~/.gemini/antigravity-cli/settings.json``
      (no CLI flag). We set ``HOME`` per-agent so each agent has its own
      settings.json without polluting the real HOME.
    - ``--print-timeout 24h`` is fixed to avoid premature default 5m termination
    - ``AGY_CLI_DISABLE_AUTO_UPDATE=1`` suppresses auto-update writes to HOME
    - Reads both ``AGENTS.md`` and ``GEMINI.md``: we generate ``AGENTS.md`` and
      strip any stale ``GEMINI.md`` before launch.

Liveness invariant:
    pid file exists, /proc/<pid> exists, and cmdline contains "agy".

Note (quota): agy quota is fetched via proactive REST
(cloudcode-pa.googleapis.com :fetchAvailableModels). See engine/agy_quota.py.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Generator
from pathlib import Path

import config
from config import (
    AGENT_PROFILES_DIR,
    AGY_AGENT_CONFIG,
    AGY_PIDS_DIR,
    INACTIVE_THRESHOLD_SEC,
)
from engine.backend_types import SendResult

logger = logging.getLogger(__name__)

# Symlinks from per-agent HOME (.gemini/) into the real HOME. Limited to
# read-only auth/identity files so mutable per-agent state (conversations,
# cache, log, etc.) stays per-agent and cannot leak into the real HOME.
#
# Maintenance: if agy changes its auth filenames (e.g. ``credentials.json``),
# update this list. Authentication failures after an agy upgrade are the
# canonical sign that this list is stale.
_SHARED_SYMLINKS: list[str] = [
    ".gemini/oauth_creds.json",
    ".gemini/google_accounts.json",
    ".gemini/installation_id",
    ".gemini/antigravity-cli/installation_id",
    ".gemini/antigravity-cli/antigravity-oauth-token",
]

# ---------------------------------------------------------------------------
# Per-agent config (agents/config_agy.json)
# ---------------------------------------------------------------------------
_agent_config_cache: dict[str, dict[str, object]] | None = None


def _load_config() -> dict[str, dict[str, object]]:
    """Load and cache agents/config_agy.json. Called once per process lifetime."""
    global _agent_config_cache
    if _agent_config_cache is not None:
        return _agent_config_cache

    try:
        text = AGY_AGENT_CONFIG.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        _agent_config_cache = {}
        return _agent_config_cache
    except OSError as exc:
        logger.warning("Failed to read %s: %s", AGY_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not text:
        _agent_config_cache = {}
        return _agent_config_cache

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in %s: %s", AGY_AGENT_CONFIG, exc)
        _agent_config_cache = {}
        return _agent_config_cache

    if not isinstance(parsed, dict):
        logger.warning(
            "Expected JSON object in %s, got %s",
            AGY_AGENT_CONFIG, type(parsed).__name__,
        )
        _agent_config_cache = {}
        return _agent_config_cache

    _agent_config_cache = {
        k: v for k, v in parsed.items() if isinstance(v, dict)
    }
    if len(_agent_config_cache) < len(parsed):
        skipped = [k for k, v in parsed.items() if not isinstance(v, dict)]
        logger.warning(
            "Skipped non-dict entries in %s: %s", AGY_AGENT_CONFIG, skipped,
        )
    return _agent_config_cache


def _pid_path(agent_id: str) -> Path:
    return AGY_PIDS_DIR / f"{agent_id}.pid"


def _session_marker_path(agent_id: str) -> Path:
    """Per-agent marker recording that a previous send() Popen succeeded.

    Existence means "previous send() spawned agy successfully". agy has no
    ``--list-sessions`` equivalent, so the marker is a best-effort substitute
    used to decide whether to pass ``-c``.
    """
    return AGY_PIDS_DIR / f"{agent_id}.has_session"


def _agent_lock_path(agent_id: str) -> Path:
    return AGY_PIDS_DIR / f"{agent_id}.lock"


@contextlib.contextmanager
def _per_agent_lock(agent_id: str) -> Generator[None, None, None]:
    AGY_PIDS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _agent_lock_path(agent_id)
    with open(lock_path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _is_agy_pid_alive(pid: int) -> bool:
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
        if s == "agy" or s.endswith("/agy"):
            return True
    return False


def _agy_last_activity(agent_id: str) -> float | None:
    """Return the most recent mtime among agy activity files, or None.

    Considers only files updated by the agent's own working session:
      - newest ``conversations/*.pb`` mtime (updated on conversation turns)
      - the pid file mtime (most recent spawn time; doubles as the
        launch-grace floor so a freshly spawned process is never reaped
        before its first ``.pb`` is written)
      - ``cli.log`` mtime (fallback right after spawn before any ``.pb``)

    Files updated by unrelated activity (last_check.timestamp, updater/,
    cache/, brain/, settings.json) are intentionally excluded.
    """
    cli_dir = AGENT_PROFILES_DIR / agent_id / ".gemini" / "antigravity-cli"
    candidates: list[Path] = []
    conv_dir = cli_dir / "conversations"
    if conv_dir.is_dir():
        candidates.extend(conv_dir.glob("*.pb"))
    candidates.append(_pid_path(agent_id))
    candidates.append(cli_dir / "cli.log")

    mtimes: list[float] = []
    for path in candidates:
        try:
            mtimes.append(path.stat().st_mtime)
        except (OSError, FileNotFoundError):
            continue
    return max(mtimes) if mtimes else None


def _terminate_pid_tree(
    pid: int,
    agent_id: str,
    proc: subprocess.Popen | None = None,
) -> bool:
    """Best-effort: PGID-wide SIGTERM → wait → SIGKILL."""
    try:
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError) as e:
        logger.warning(
            "agy getpgid(%d) failed for %s: %s; falling back to pid-only kill",
            pid, agent_id, e,
        )
        pgid = pid

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as e:
        logger.warning("agy SIGTERM to pgid %d failed for %s: %s", pgid, agent_id, e)

    if proc is not None:
        try:
            proc.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            pass
    else:
        for _ in range(10):
            if not _is_agy_pid_alive(pid):
                return True
            time.sleep(0.5)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError as e:
        logger.warning("agy SIGKILL to pgid %d failed for %s: %s", pgid, agent_id, e)

    if proc is not None:
        try:
            proc.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            logger.warning("agy proc %d did not exit after SIGKILL", pid)
            return False
    for _ in range(10):
        if not _is_agy_pid_alive(pid):
            return True
        time.sleep(0.5)
    logger.warning("agy proc %d still alive after SIGKILL for %s", pid, agent_id)
    return False


def _rebuild_agy_md(agent_id: str) -> None:
    """Rebuild AGENTS.md from IDENTITY.md + INSTRUCTION.md + MEMORY.md.

    IMPORTANT: This function is a near-exact copy of
    backend_pi.py:_rebuild_agents_md, backend_gemini.py:_rebuild_gemini_md, and
    backend_kimi.py:_rebuild_kimi_md. The four functions MUST stay in sync
    beyond filename differences. If you modify the logic here, mirror the
    change to pi, gemini and kimi (and vice versa), or extract a shared helper.
    """
    try:
        config_data = _load_config()
        agent_profile = config_data.get(agent_id, {})
        compile_flag = agent_profile.get("compile-startup-md", False)
        if not isinstance(compile_flag, bool):
            logger.warning(
                "_rebuild_agy_md: compile-startup-md for %s has non-bool value %r; "
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

        agy_md_path = profile_dir / "AGENTS.md"
        hash_path = profile_dir / ".agy_hash"

        if identity_bytes == b"" and instruction_bytes == b"" and memory_bytes == b"":
            agy_md_path.unlink(missing_ok=True)
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

        if old_hash == new_hash and agy_md_path.exists():
            return

        identity_text = identity_bytes.decode("utf-8").rstrip()
        instruction_text = instruction_bytes.decode("utf-8").rstrip()
        memory_text = memory_bytes.decode("utf-8").rstrip()

        parts = [t for t in (identity_text, instruction_text, memory_text) if t]

        if not parts:
            agy_md_path.unlink(missing_ok=True)
            hash_path.unlink(missing_ok=True)
            return

        output = "\n\n---\n\n".join(parts) + "\n"

        agy_md_path.write_text(output, encoding="utf-8")
        hash_path.write_text(new_hash + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("_rebuild_agy_md: failed for %s: %s", agent_id, exc)


def _remove_stale_gemini_md(agent_id: str) -> bool:
    """Remove stale ``GEMINI.md`` / ``.gemini_hash`` before launching agy.

    agy reads both ``AGENTS.md`` and ``GEMINI.md``. After gemini→agy migration
    the cwd may still contain a stale ``GEMINI.md`` (possibly user-edited),
    which would contaminate agy's context. We always preserve ``GEMINI.md``
    by atomically hard-linking it to a timestamped backup before deletion.

    Returns True on success (caller may proceed), False on failure (caller
    must abort send to avoid contamination).
    """
    profile_dir = AGENT_PROFILES_DIR / agent_id
    gemini_md = profile_dir / "GEMINI.md"
    gemini_hash = profile_dir / ".gemini_hash"

    # Remove internal hash metadata. missing_ok avoids TOCTOU with exists().
    try:
        gemini_hash.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "agy: failed to delete %s for %s: %s",
            gemini_hash, agent_id, exc,
        )
        return False

    if not gemini_md.exists():
        return True

    for old_bak in profile_dir.glob("GEMINI.md.bak.*"):
        with contextlib.suppress(OSError):
            old_bak.unlink()
    backup = profile_dir / f"GEMINI.md.bak.{time.time_ns()}"
    try:
        os.link(gemini_md, backup)
    except FileExistsError:
        logger.warning(
            "agy: backup target %s already exists for %s; refusing to overwrite",
            backup, agent_id,
        )
        return False
    except OSError as exc:
        logger.warning(
            "agy: failed to hard-link %s -> %s for %s: %s",
            gemini_md, backup, agent_id, exc,
        )
        return False

    try:
        gemini_md.unlink()
    except OSError as exc:
        logger.warning(
            "agy: failed to unlink %s after backup for %s: %s",
            gemini_md, agent_id, exc,
        )
        return False

    logger.warning(
        "agy: preserved stale GEMINI.md for %s as %s",
        agent_id, backup,
    )
    return True


def _purge_session_data_on_reset(agent_id: str) -> None:
    agent_home = AGENT_PROFILES_DIR / agent_id
    if agent_home.is_symlink():
        logger.warning(
            "reset_session: skipping session data purge for %s "
            "(agent_home is symlink)", agent_id,
        )
        return
    gemini_dir = agent_home / ".gemini"
    cli_dir = gemini_dir / "antigravity-cli"
    conv_dir = cli_dir / "conversations"
    brain_dir = cli_dir / "brain"
    for path, label in [
        (gemini_dir, ".gemini"),
        (cli_dir, "antigravity-cli"),
        (conv_dir, "conversations"),
        (brain_dir, "brain"),
    ]:
        if path.is_symlink():
            logger.warning(
                "reset_session: skipping session data purge for %s "
                "(symlink at %s)", agent_id, label,
            )
            return
    if conv_dir.is_dir():
        try:
            shutil.rmtree(conv_dir)
        except OSError as e:
            logger.warning("Failed to purge conversations: %s", e)
    if brain_dir.is_dir():
        try:
            shutil.rmtree(brain_dir)
        except OSError as e:
            logger.warning("Failed to purge brain: %s", e)


def _ensure_symlink(link_path: Path, target: Path) -> None:
    """Ensure link_path is a symlink pointing to target (idempotent).

    agents/<agent_id>/.gemini/ is owned entirely by backend_agy, so we
    prioritize repair: regular files or wrong-target links found there
    are replaced with the correct symlink whenever target exists.
    """
    if not target.exists() and not target.is_symlink():
        logger.warning(
            "agy: shared symlink target missing: %s (skipping %s)",
            target, link_path,
        )
        return

    link_path.parent.mkdir(parents=True, exist_ok=True)

    if link_path.is_symlink():
        try:
            current = os.readlink(link_path)
        except OSError:
            current = None
        if current == str(target):
            if target.exists():
                return
            # broken symlink to right target — repair below
        try:
            link_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "agy: failed to remove existing symlink %s: %s", link_path, exc,
            )
            return
    elif link_path.exists():
        try:
            if link_path.is_dir():
                shutil.rmtree(link_path)
            else:
                link_path.unlink()
        except OSError as exc:
            logger.warning(
                "agy: failed to remove %s for symlink repair: %s",
                link_path, exc,
            )
            return

    try:
        link_path.symlink_to(target)
    except FileExistsError:
        # Concurrent send race: verify final state is correct.
        try:
            if link_path.is_symlink() and os.readlink(link_path) == str(target):
                return
        except OSError:
            pass
        logger.warning(
            "agy: symlink %s exists after race but does not match %s",
            link_path, target,
        )
    except OSError as exc:
        logger.warning(
            "agy: failed to create symlink %s -> %s: %s",
            link_path, target, exc,
        )


def _ensure_agy_home(agent_id: str, model: str | None) -> Path | None:
    """Idempotently prepare ``agents/<agent_id>/`` as the per-agent HOME.

    - Creates ``.gemini/antigravity-cli/cache/`` if missing
    - Ensures shared auth/identity symlinks into the real HOME
    - Writes per-agent ``antigravity-cli/settings.json`` (base merged from
      real HOME, with model/trustedWorkspaces overridden)

    Returns the agent profile dir (to be used as ``HOME`` and ``cwd``).
    """
    agent_home = AGENT_PROFILES_DIR / agent_id

    if agent_home.is_symlink():
        logger.warning(
            "agy: agent_home is a symlink (potential HOME escape): %s",
            agent_home,
        )
        return None

    real_home = Path.home()

    # Guard: if .gemini, intermediate components, mutable subdirectories,
    # or mutable files are symlinks, mkdir/write would follow them into the
    # real HOME — destroying HOME isolation.  Remove stale symlinks so mkdir
    # creates real directories and writes stay per-agent.
    _agy_cli = agent_home / ".gemini" / "antigravity-cli"
    _gemini_config = agent_home / ".gemini" / "config"
    # Ordering constraint: parents MUST precede children. The guard iterates
    # this tuple and unlinks symlinks. If a child were checked first while its
    # parent is still a symlink, child.unlink() would resolve the parent
    # symlink and delete a file inside the real HOME — a destructive leak.
    # Maintenance: this whitelist tracks paths observed in agy 1.0.2.
    # After an agy upgrade, run the agent once and diff agent_home to detect
    # new write targets. Auth failures or unexpected symlinks after upgrade
    # are the canonical sign that this list is stale.
    _MUTABLE_PATHS = (
        agent_home / ".gemini",
        agent_home / ".antigravitycli",
        _gemini_config,
        _gemini_config / "mcp_config.json",
        _gemini_config / ".migrated",
        _gemini_config / "projects",
        _agy_cli,
        _agy_cli / "cache",
        _agy_cli / "conversations",
        _agy_cli / "log",
        _agy_cli / "bin",
        _agy_cli / "knowledge",
        _agy_cli / "implicit",
        _agy_cli / "updater",
        _agy_cli / "brain",
        _agy_cli / "settings.json",
        _agy_cli / "last_check.timestamp",
        _agy_cli / "cli.log",
        _agy_cli / "keybindings.json",
    )
    for component in _MUTABLE_PATHS:
        if component.is_symlink():
            try:
                component.unlink()
            except OSError as exc:
                logger.warning(
                    "agy: failed to remove symlink %s blocking HOME isolation: %s",
                    component, exc,
                )
                return None

    (agent_home / ".gemini" / "antigravity-cli" / "cache").mkdir(
        parents=True, exist_ok=True,
    )

    for rel_path in _SHARED_SYMLINKS:
        _ensure_symlink(agent_home / rel_path, real_home / rel_path)

    settings_path = agent_home / ".gemini" / "antigravity-cli" / "settings.json"
    real_settings_path = real_home / ".gemini" / "antigravity-cli" / "settings.json"

    try:
        base_settings = json.loads(real_settings_path.read_text(encoding="utf-8"))
        if not isinstance(base_settings, dict):
            base_settings = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        base_settings = {}

    settings: dict = {**base_settings}
    settings["trustedWorkspaces"] = [str(agent_home)]

    if model and model.strip():
        settings["model"] = model.strip()
    # else: keep whatever model came from base_settings (if any). If base
    # also had no model, the key is absent and agy uses its built-in default.

    new_text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    try:
        old_text = settings_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        old_text = ""
    if old_text != new_text:
        settings_path.write_text(new_text, encoding="utf-8")

    _gemini_config.mkdir(parents=True, exist_ok=True)
    mcp_cfg = _gemini_config / "mcp_config.json"
    try:
        needs_seed = not mcp_cfg.exists() or mcp_cfg.stat().st_size == 0
    except OSError:
        needs_seed = True
    if needs_seed:
        try:
            mcp_cfg.write_text("{}\n", encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "agy: failed to seed mcp_config.json for %s: %s",
                agent_id, exc,
            )

    log_dir = agent_home / ".gemini" / "antigravity-cli" / "log"
    if log_dir.is_dir():
        def _safe_mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        cli_logs = sorted(
            log_dir.glob("cli-*.log"),
            key=_safe_mtime,
            reverse=True,
        )
        for stale in cli_logs[10:]:
            with contextlib.suppress(OSError):
                stale.unlink()

    return agent_home


def send(agent_id: str, message: str, timeout: int) -> SendResult:
    """Fire-and-forget subprocess launch of ``agy``.

    At most one live agy process per agent is allowed. If a previous
    agy process is still running, the send is refused (returns BUSY).
    The caller (watchdog/fsm) retries on the next poll cycle after
    is_inactive() confirms the process has exited.
    """
    if config.DRY_RUN:
        logger.info("[dry-run] agy send skipped (agent=%s)", agent_id)
        return SendResult.OK
    with _per_agent_lock(agent_id):
        agent_home = AGENT_PROFILES_DIR / agent_id
        if agent_home.is_symlink():
            logger.warning(
                "agy: agent_home is a symlink (potential HOME escape): %s",
                agent_home,
            )
            return SendResult.FAIL

        try:
            existing_pid_text = _pid_path(agent_id).read_text().strip()
            existing_pid = int(existing_pid_text)
        except (OSError, FileNotFoundError, ValueError):
            existing_pid = None
        if existing_pid is not None and _is_agy_pid_alive(existing_pid):
            la = _agy_last_activity(agent_id)
            if la is not None and (time.time() - la) < INACTIVE_THRESHOLD_SEC:
                logger.info(
                    "agy send: live process %d still running for %s; "
                    "refusing to spawn",
                    existing_pid, agent_id,
                )
                return SendResult.BUSY          # active → keep #327 dual-spawn guard
            # stale = hang: inline reap (do NOT call soft_reap → avoids flock
            # self-deadlock from re-acquiring the held _per_agent_lock)
            logger.warning(
                "agy send: live process %d stale for %s; reaping",
                existing_pid, agent_id,
            )
            if not _terminate_pid_tree(existing_pid, agent_id, proc=None):
                logger.warning(
                    "agy send: failed to terminate stale process %d for %s; "
                    "keeping BUSY guard",
                    existing_pid, agent_id,
                )
                return SendResult.BUSY          # terminate failed → keep pid (no dual spawn)
            _pid_path(agent_id).unlink(missing_ok=True)
            # has_session preserved → has_prev=True below keeps -c continuation
            # Warn if no .pb (lost context observability)
            conv_dir = (
                AGENT_PROFILES_DIR / agent_id / ".gemini"
                / "antigravity-cli" / "conversations"
            )
            if not (conv_dir.is_dir() and any(conv_dir.glob("*.pb"))):
                logger.warning(
                    "agy send: no .pb found after reap for %s; "
                    "review context may be lost — nudge refresher commands "
                    "will provide recovery",
                    agent_id,
                )

        _rebuild_agy_md(agent_id)
        if not _remove_stale_gemini_md(agent_id):
            return SendResult.FAIL

        config_data = _load_config()
        profile = config_data.get(agent_id, {})

        if not agent_home.is_dir():
            logger.warning(
                "agy send refused for %s: profile dir %s does not exist. "
                "agy backend requires a dedicated cwd per agent to avoid "
                "cross-agent session contamination (session is cwd-scoped).",
                agent_id, agent_home,
            )
            return SendResult.FAIL

        raw_model = profile.get("model")
        model = raw_model if isinstance(raw_model, str) else None
        if _ensure_agy_home(agent_id, model) is None:
            return SendResult.FAIL

        has_prev = _session_marker_path(agent_id).exists()

        cmd: list[str] = [
            config.AGY_BIN,
            "--add-dir", str(agent_home),
            "--print-timeout", "24h",
            "-p", message,
            "--dangerously-skip-permissions",
        ]
        if has_prev:
            cmd.append("-c")

        env = os.environ.copy()
        env["HOME"] = str(agent_home)
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "1"

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(agent_home),
                start_new_session=True,
                env=env,
            )
        except (OSError, FileNotFoundError) as e:
            logger.warning("agy spawn failed for %s: %s", agent_id, e)
            return SendResult.FAIL

        try:
            _pid_path(agent_id).write_text(str(proc.pid))
        except OSError as exc:
            logger.warning(
                "agy pid write failed for %s: %s; terminating spawned process group",
                agent_id, exc,
            )
            if not _terminate_pid_tree(proc.pid, agent_id, proc=proc):
                logger.error(
                    "agy send for %s: spawned pid %d could not be terminated "
                    "after pid-write failure; session for cwd %s may be "
                    "contaminated. Manual intervention required: kill pid %d "
                    "and run reset_session for this agent.",
                    agent_id, proc.pid, agent_home, proc.pid,
                )
            return SendResult.FAIL

        try:
            _session_marker_path(agent_id).touch()
        except OSError as exc:
            logger.warning(
                "agy session-marker touch failed for %s: %s; next send will not use -c",
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

    if not _is_agy_pid_alive(pid):
        return True                             # dead → inactive (legacy agy behavior)

    # Alive: treat as inactive only if activity files are stale (hang detection)
    la = _agy_last_activity(agent_id)
    return la is None or (time.time() - la) >= INACTIVE_THRESHOLD_SEC


def soft_reap(agent_id: str) -> None:
    """Terminate a lingering live agy process while PRESERVING session context.

    Unlike reset_session(), does NOT purge conversations/brain and does NOT
    remove the session marker — only the finished/hung process is killed and
    the pid file cleared, so the next send() resumes the same conversation via -c.

    Safety: agy pid files are per agent_id (global, not per project). The
    caller must ensure the live process belongs to its own project to avoid
    killing another project's active review. The canonical usage filters
    targets via prev_reviews (project-scoped P0/P1/P2/REJECT submitter set
    built before clear_reviews in do_transition).
    """
    if config.DRY_RUN:
        return
    with _per_agent_lock(agent_id):
        try:
            pid = int(_pid_path(agent_id).read_text().strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None and _is_agy_pid_alive(pid):
            if not _terminate_pid_tree(pid, agent_id, proc=None):
                logger.warning(
                    "agy soft_reap: failed to terminate live process %d for %s; "
                    "keeping pid file to preserve BUSY guard", pid, agent_id,
                )
                return
        try:
            _pid_path(agent_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "agy soft_reap: failed to delete pid file for %s: %s",
                agent_id, exc,
            )


def reset_session(agent_id: str) -> None:
    """Best-effort session reset.

    Terminates the recorded live process (if any), removes the pid file and
    local session marker, and purges the per-agent conversations directory.
    Per-agent settings.json and shared symlinks are NOT touched (next send
    re-establishes them).
    """
    with _per_agent_lock(agent_id):
        agent_home = AGENT_PROFILES_DIR / agent_id
        if agent_home.is_symlink():
            logger.warning(
                "agy reset_session: agent_home is a symlink "
                "(potential HOME escape): %s",
                agent_home,
            )
            return

        _rebuild_agy_md(agent_id)

        try:
            pid_text = _pid_path(agent_id).read_text().strip()
            pid = int(pid_text)
        except (OSError, FileNotFoundError, ValueError):
            pid = None

        if pid is not None and _is_agy_pid_alive(pid):
            logger.info(
                "agy reset_session: terminating live process %d for %s",
                pid, agent_id,
            )
            if not _terminate_pid_tree(pid, agent_id, proc=None):
                logger.warning(
                    "agy reset_session for %s: failed to terminate live process %d",
                    agent_id, pid,
                )
                return

        try:
            _pid_path(agent_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "agy reset_session: failed to delete pid file for %s: %s",
                agent_id, exc,
            )

        try:
            _session_marker_path(agent_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "agy reset_session: failed to delete session marker for %s: %s",
                agent_id, exc,
            )

        _purge_session_data_on_reset(agent_id)
