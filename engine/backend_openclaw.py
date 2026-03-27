"""engine/backend_openclaw.py - openclaw backend for agent communication.

Provides send/ping for agents running via the ``openclaw`` CLI.
"""

from __future__ import annotations

import json
import logging
import subprocess
import uuid

import config
from config import AGENT_SEND_TIMEOUT

logger = logging.getLogger(__name__)


def _gateway_chat_send_cli(params_json: str, timeout: int) -> bool:
    """Send chat.send via openclaw gateway call CLI.

    CLI handles device identity and all auth modes internally.
    For params_json under MAX_CLI_ARG_BYTES (128KB) only.

    Args:
        params_json: JSON string (sessionKey, message, idempotencyKey)
        timeout: timeout in seconds

    Returns:
        True on success, False on failure.
    """
    try:
        result = subprocess.run(
            [
                "openclaw", "gateway", "call", "chat.send",
                "--params", params_json,
                "--json",
                "--timeout", str(timeout * 1000),
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,  # CLI timeout + margin
        )
        if result.returncode != 0:
            logger.warning("openclaw gateway call failed (rc=%d): %s",
                          result.returncode, result.stderr.strip()[:200])
            return False
        resp = json.loads(result.stdout)
        return resp.get("ok", False) or resp.get("status") == "started"
    except FileNotFoundError:
        logger.error("openclaw not found in PATH")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("openclaw gateway call timed out (%ds)", timeout)
        return False
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("openclaw gateway call error: %s", e)
        return False


def _gateway_chat_send(session_key: str, message: str, timeout: int) -> bool:
    """Send chat.send via gateway (CLI only).

    For params_json under MAX_CLI_ARG_BYTES only.
    Larger messages must be externalized by the caller.
    """
    params_json = json.dumps({
        "sessionKey": session_key,
        "message": message,
        "idempotencyKey": str(uuid.uuid4()),
    })
    return _gateway_chat_send_cli(params_json, timeout)


def send(agent_id: str, message: str, timeout: int = AGENT_SEND_TIMEOUT) -> bool:
    """Send message to an openclaw agent via gateway chat.send."""
    if config.DRY_RUN:
        logger.info("[dry-run] send_to_agent skipped (agent=%s)", agent_id)
        return True
    session_key = f"agent:{agent_id}:main"
    return _gateway_chat_send(session_key, message, timeout)


def ping(agent_id: str, timeout: int = 20) -> bool:
    """Ping an openclaw agent via ``openclaw agent`` CLI."""
    if config.DRY_RUN:
        logger.info("[dry-run] ping_agent skipped (agent=%s)", agent_id)
        return True

    try:
        result = subprocess.run(
            [
                "openclaw", "agent",
                "--agent", agent_id,
                "--message", "ping",
                "--json",
                "--timeout", str(timeout)
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 10,  # subprocess timeout > CLI timeout
        )
        alive = result.returncode == 0
        logger.info(
            "ping_agent %s: %s (rc=%d)",
            agent_id, "alive" if alive else "dead", result.returncode
        )
        return alive
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("ping_agent %s: dead (%s)", agent_id, e)
        return False
