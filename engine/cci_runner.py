"""engine/cci_runner.py - CCI driver (one-shot, blocking).

Drives the interactive ``claude`` TUI via a PTY (pexpect) for exactly one turn,
then exits. This is the non ``-p`` drop-in replacement for
``claude -p < promptfile``: launch the TUI, drive a single message, block until
the turn completes (detected via the transcript jsonl), send ``/exit``, and
return exit 0. Handshake failure / completion timeout / pexpect exception cause
a non-zero exit.

All synchronous (no asyncio). Adapted from the reference CCIBackend
implementation, stripped of chat/streaming/display logic.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import json
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path

import pexpect

import config

log = logging.getLogger("engine.cci_runner")


class StartupTimeoutError(Exception):
    """TUI の PROMPT_READY を制限時間内に検出できなかった。"""


class EofDuringStartupError(Exception):
    """起動 handshake 中に claude プロセスが終了した。"""

PROMPT_READY = re.compile(r"cycle|shortcuts", re.IGNORECASE)
TRUST_RE = re.compile(r"Yes,?\s+I\s+trust\s+this\s+folder", re.IGNORECASE)

POST_READY_DELAY = 12.0
_FILE_WAIT_FALLBACK = 10.0
_POLL_INTERVAL = 0.25

BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"

_THINKING_MODES = ("enabled", "adaptive", "disabled")
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultracode")

# ---------------------------------------------------------------------------
# Module-level state (referenced by the signal handler / finally cleanup)
# ---------------------------------------------------------------------------
_child: pexpect.spawn | None = None
_prompt_file_path: str | None = None  # None when "--prompt-file -" (stdin)


# ---------------------------------------------------------------------------
# Environment / path helpers
# ---------------------------------------------------------------------------

def _clean_env_for_cci() -> dict[str, str]:
    """Build a clean env for the nested claude TUI (strip API keys / CLAUDECODE)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    }
    env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"] = "1"
    env["TERM"] = "xterm-256color"
    return env


def _claude_project_dir(cwd: str) -> Path:
    """Return the Claude Code session JSONL storage directory for a given cwd."""
    project_key = str(Path(cwd).resolve()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / project_key


def _claude_session_jsonl_path(cwd: str, session_id: str) -> Path:
    """Return the predicted path to a Claude Code session JSONL file."""
    return _claude_project_dir(cwd) / f"{session_id}.jsonl"


def _glob_fallback_path(session_uuid: str) -> Path | None:
    """Fallback when the predicted path is wrong. Returns the newest match by mtime."""
    pattern = str(Path.home() / ".claude" / "projects" / "*" / f"{session_uuid}.jsonl")
    candidates = glob.glob(pattern)
    if not candidates:
        return None
    if len(candidates) == 1:
        return Path(candidates[0])
    return Path(max(candidates, key=os.path.getmtime))


# ---------------------------------------------------------------------------
# Trust dialog pre-accept (lockfile + double-check + atomic write)
# ---------------------------------------------------------------------------

def _ensure_trust_accepted(cwd: str) -> None:
    """Pre-set ~/.claude.json projects[cwd].hasTrustDialogAccepted = True.

    Uses a dedicated lock file (~/.claude.json.lock) with fcntl.LOCK_EX plus an
    optimistic pre-lock read and a post-lock re-read (double-check) so concurrent
    runners do not lose updates or corrupt the file.
    """
    path = os.path.expanduser("~/.claude.json")
    cwd_abs = str(Path(cwd).resolve())

    # 1. Optimistic pre-lock read — avoid the lock entirely if already accepted.
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            projects = data.get("projects", {})
            if isinstance(projects, dict):
                entry = projects.get(cwd_abs, {})
                if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
                    return
    except FileNotFoundError:
        # File auto-created by claude on first launch; nothing to do here.
        return
    except (OSError, ValueError):
        pass  # fall through to lock path

    # 2. Acquire the dedicated lock file (NOT the body file — os.replace swaps
    #    the inode and would let a body-file lock slip through).
    lock_path = path + ".lock"
    try:
        lockfd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        log.warning("cci_runner: could not open trust lock file %s", lock_path)
        return
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX)

        # 3. Post-lock re-read (another runner may have updated it first).
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("cci_runner: cannot read/parse %s under lock; skipping trust update", path)
            return
        if not isinstance(data, dict):
            log.warning("cci_runner: %s root is not a dict; skipping trust update", path)
            return

        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            projects = {}
            data["projects"] = projects
        entry = projects.setdefault(cwd_abs, {})
        if not isinstance(entry, dict):
            entry = {}
            projects[cwd_abs] = entry
        if entry.get("hasTrustDialogAccepted") is True:
            return

        entry["hasTrustDialogAccepted"] = True

        # 4. Atomic write.
        tmp = f"{path}.tmp-{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    finally:
        # 5. Releasing the lock by closing the fd.
        os.close(lockfd)


# ---------------------------------------------------------------------------
# Completion detection (transcript jsonl tail)
# ---------------------------------------------------------------------------

class _CompletionDetector:
    """Stateful NDJSON entry processor; returns True once the turn is complete."""

    def __init__(self, prompt_text: str, *, skip_queue_watermark: bool = False):
        self._prompt_text = prompt_text
        self._watermark_found = False
        self._saw_assistant = False
        self._skip_queue_watermark = skip_queue_watermark

    @staticmethod
    def _content_matches_prompt(prompt_text: str, content: object) -> bool:
        target = prompt_text.strip()
        if isinstance(content, str):
            return content.strip() == target
        if isinstance(content, list):
            texts: list[str] = []
            has_non_text = False
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    texts.append(str(b.get("text", "")))
                else:
                    has_non_text = True
            if not texts and has_non_text:
                # tool_result etc. only → not a watermark candidate
                return False
            return "".join(texts).strip() == target
        return False

    @staticmethod
    def _is_tool_result_only(content: object) -> bool:
        if not isinstance(content, list) or not content:
            return False
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                return False
        return True

    def process_entry(self, entry: dict) -> bool:
        etype = entry.get("type")

        if etype == "user":
            message = entry.get("message") or {}
            if message.get("role") != "user":
                return False
            content = message.get("content")
            if self._is_tool_result_only(content):
                return False
            if not self._watermark_found and self._content_matches_prompt(
                self._prompt_text, content
            ):
                self._watermark_found = True
            return False

        if etype == "queue-operation":
            if entry.get("operation") == "enqueue" and not self._skip_queue_watermark:
                content = entry.get("content")
                if not self._watermark_found and self._content_matches_prompt(
                    self._prompt_text, content
                ):
                    self._watermark_found = True
            return False

        if etype == "system" and entry.get("subtype") == "turn_duration":
            return self._watermark_found and self._saw_assistant

        if etype != "assistant":
            return False

        # Ignore assistant entries until the watermark (current user prompt) is
        # seen — guards against stale assistant entries from a timed-out prior turn.
        if not self._watermark_found:
            return False

        self._saw_assistant = True

        message = entry.get("message") or {}
        content = message.get("content") or []
        if not isinstance(content, list):
            return False

        has_text = False
        has_tool = False
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                has_text = True
            elif btype == "tool_use":
                has_tool = True

        stop_reason = message.get("stop_reason")
        if stop_reason in ("end_turn", "max_tokens", "stop_sequence") and (
            has_text or has_tool
        ):
            return True
        return False


def _drain_pty(child: pexpect.spawn) -> None:
    """Drain the PTY buffer until empty (prevents write() deadlock in the TUI)."""
    while True:
        try:
            child.read_nonblocking(size=4096, timeout=0)
        except (pexpect.TIMEOUT, pexpect.EOF):
            break
        except OSError:
            break


def _wait_for_completion(
    child: pexpect.spawn | None,
    path: Path,
    prompt_text: str,
    start_offset: int,
    deadline: float,
    fallback_resolver,
    *,
    skip_queue_watermark: bool = False,
) -> bool:
    """Poll the transcript jsonl from start_offset and detect turn completion."""
    detector = _CompletionDetector(prompt_text, skip_queue_watermark=skip_queue_watermark)
    offset = start_offset
    pending = b""

    # File appearance wait (new session) with glob fallback.
    file_wait_start = time.monotonic()
    while not path.exists():
        now = time.monotonic()
        if now >= deadline:
            return False
        if now - file_wait_start >= _FILE_WAIT_FALLBACK:
            fb = fallback_resolver(path.stem)
            if fb is not None:
                path = fb
                offset = 0
                break
        if child is not None:
            _drain_pty(child)
        time.sleep(_POLL_INTERVAL)

    # Main polling loop.
    while True:
        now = time.monotonic()
        if now >= deadline:
            return False

        if child is not None:
            _drain_pty(child)

        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
        except OSError:
            time.sleep(_POLL_INTERVAL)
            continue

        if chunk:
            offset += len(chunk)
            buf = pending + chunk
            if b"\n" in buf:
                complete, _, tail = buf.rpartition(b"\n")
                pending = tail
                lines = complete.split(b"\n")
            else:
                pending = buf
                lines = []

            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    log.warning("cci_runner: failed to decode transcript line: %r", raw[:200])
                    continue
                if detector.process_entry(entry):
                    return True

        time.sleep(_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# PTY drive helpers
# ---------------------------------------------------------------------------

def _startup_handshake(
    child: pexpect.spawn, timeout: float = config.CCI_STARTUP_TIMEOUT_SEC,
) -> None:
    """Wait for PROMPT_READY (handling TRUST_RE), then POST_READY_DELAY.

    Note: the ``timeout`` default is bound at import time, so monkeypatching
    ``config.CCI_STARTUP_TIMEOUT_SEC`` in tests does NOT change it — pass
    ``timeout`` explicitly when testing.
    """
    patterns = [PROMPT_READY, TRUST_RE, pexpect.TIMEOUT, pexpect.EOF]
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(1.0, deadline - time.monotonic())
        idx = child.expect(patterns, timeout=min(remaining, 10.0))
        if idx == 0:
            break
        elif idx == 1:
            child.send("1")
            time.sleep(0.5)
            child.send("\r")
        elif idx == 2:
            if time.monotonic() >= deadline:
                raise StartupTimeoutError("Startup timeout")
        else:
            raise EofDuringStartupError("claude exited during startup")
    time.sleep(POST_READY_DELAY)


def _send_prompt(child: pexpect.spawn, message: str) -> None:
    """Send the prompt via bracketed paste, then \\r after a short delay."""
    safe = f" {message}" if message.startswith("/") else message
    child.send(BRACKETED_PASTE_START + safe + BRACKETED_PASTE_END)
    time.sleep(0.5)
    child.send("\r")


def _send_exit(child: pexpect.spawn) -> None:
    """Send /exit and wait for EOF; force-terminate on timeout."""
    try:
        child.sendline("/exit")
    except (OSError, pexpect.EOF):
        pass
    try:
        child.expect(pexpect.EOF, timeout=10)
    except (pexpect.TIMEOUT, pexpect.EOF, OSError):
        if child.isalive():
            child.terminate(force=True)
    try:
        child.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def _write_last_error(agent_id: str | None, reason: str, session_id: str | None) -> None:
    """Write ``.cci-sessions/<agent_id>/last_error`` JSON for watchdog observability.

    Skipped when ``agent_id`` is None (``--agent-id`` not given) — the failure is
    still recorded in runner.log. Wrapped in try/except so disk/permission errors
    never change the runner's exit code. Re-creates the parent directory if it was
    removed (reset_session / manual deletion).
    """
    if agent_id is None:
        return
    try:
        error_path = config.CCI_SESSIONS_DIR / agent_id / "last_error"
        error_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"reason": reason, "ts": time.time(), "session_id": session_id}
        error_path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        log.warning("cci_runner: failed to write last_error for %s", agent_id, exc_info=True)


def _cleanup_handler(signum, frame) -> None:
    """Terminate the child (if any) and exit 128+signum. No file ops here.

    Signal-driven termination is an intentional external action (not a failure),
    so it exits 128+signum to stay distinguishable from the generic error exit 1
    in runner.log analysis. No last_error is written for signal exits.
    """
    if _child is not None and _child.isalive():
        try:
            _child.terminate(force=True)
        except Exception:
            pass
    sys.exit(128 + signum)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_claude_args(args: argparse.Namespace, session_id: str, is_resume: bool) -> list[str]:
    """Build CLI arg list for the claude subprocess."""
    cmd_args = ["--dangerously-skip-permissions"]
    if is_resume:
        cmd_args += ["--resume", session_id]
    else:
        cmd_args += ["--session-id", session_id]
    if args.model:
        cmd_args += ["--model", args.model]
    if args.thinking:
        cmd_args += ["--thinking", args.thinking]
    if args.effort:
        if args.effort == "ultracode":
            cmd_args += ["--effort", "xhigh", "--settings", '{"ultracode": true}']
        else:
            cmd_args += ["--effort", args.effort]
    if args.append_system_prompt:
        cmd_args += ["--append-system-prompt", args.append_system_prompt]
    if args.disallowed_tools:
        cmd_args += ["--disallowed-tools", args.disallowed_tools]
    return cmd_args


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="engine.cci_runner")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--session-id")
    group.add_argument("--resume")
    p.add_argument("--model")
    p.add_argument("--thinking", choices=list(_THINKING_MODES))
    p.add_argument("--effort", choices=list(_EFFORT_LEVELS))
    p.add_argument("--cwd")
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--delete-prompt-file", action="store_true")
    p.add_argument("--append-system-prompt")
    p.add_argument("--disallowed-tools")
    p.add_argument("--completion-timeout", type=int, default=config.CCI_COMPLETION_TIMEOUT_SEC)
    p.add_argument("--startup-timeout", type=int, default=config.CCI_STARTUP_TIMEOUT_SEC)
    p.add_argument("--agent-id", type=str, default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global _child, _prompt_file_path

    # Register signal handlers up front (before spawn / prompt_file assignment).
    try:
        signal.signal(signal.SIGTERM, _cleanup_handler)
        signal.signal(signal.SIGINT, _cleanup_handler)
    except ValueError:
        pass  # not in main thread (e.g. some test runners)

    args = _parse_args(argv)

    session_id: str | None = None
    try:
        # Read prompt (set _prompt_file_path early so signal/finally can clean up).
        if args.prompt_file == "-":
            _prompt_file_path = None
            prompt_text = sys.stdin.read()
        else:
            _prompt_file_path = args.prompt_file
            with open(args.prompt_file, encoding="utf-8") as f:
                prompt_text = f.read()

        cwd = args.cwd or os.getcwd()
        session_id = args.resume if args.resume else args.session_id
        is_resume = bool(args.resume)

        _ensure_trust_accepted(cwd)

        cmd_args = _build_claude_args(args, session_id, is_resume)

        env = _clean_env_for_cci()
        log.info("cci_runner: spawning claude (TUI): %s (cwd=%s)", " ".join(cmd_args), cwd)

        child = pexpect.spawn(
            config.CCI_BIN,
            args=cmd_args,
            cwd=cwd,
            env=env,
            encoding="utf-8",
            codec_errors="replace",
            timeout=args.completion_timeout,
            dimensions=(40, 120),
        )
        _child = child

        _startup_handshake(child, timeout=args.startup_timeout)

        # Resolve transcript path + initial offset (BEFORE sending the prompt).
        path = _claude_session_jsonl_path(cwd, session_id)
        if not path.exists():
            fb = _glob_fallback_path(session_id)
            if fb is not None:
                path = fb
        start_offset = path.stat().st_size if path.exists() else 0

        _send_prompt(child, prompt_text)

        deadline = time.monotonic() + args.completion_timeout
        done = _wait_for_completion(
            child, path, prompt_text, start_offset, deadline, _glob_fallback_path,
        )
        if not done:
            log.error("cci_runner: completion timeout after %ds", args.completion_timeout)
            _write_last_error(args.agent_id, "completion_timeout", session_id)
            return 3

        _send_exit(child)
        return 0
    except StartupTimeoutError:
        log.exception("cci_runner: startup timeout")
        _write_last_error(args.agent_id, "startup_timeout", session_id)
        return 2
    except EofDuringStartupError:
        log.exception("cci_runner: claude exited during startup")
        _write_last_error(args.agent_id, "eof_during_startup", session_id)
        return 4
    except Exception:
        log.exception("cci_runner: failed")
        _write_last_error(args.agent_id, "exception", session_id)
        return 1
    finally:
        # Child reaping: guarantees no orphan TUI on any exit path.
        if _child is not None and _child.isalive():
            try:
                _child.terminate(force=True)
            except Exception:
                pass
        # Temp file cleanup (opt-in via --delete-prompt-file).
        if args.delete_prompt_file and _prompt_file_path is not None:
            try:
                os.unlink(_prompt_file_path)
            except OSError:
                pass


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    sys.exit(main())
