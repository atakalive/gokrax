"""engine/openai_codex_quota.py - openai-codex usage-based fallback for PI backend.

Reuses pi (or codex CLI) OAuth credentials to query
``https://chatgpt.com/backend-api/wham/usage`` and routes a PI agent's
``--model`` argument from openai-codex to a configured fallback
provider/model when weekly quota is near exhaustion.

Public surface:
    get_codex_usage() -> tuple[bool, float, datetime | None]
    should_fallback(agent_id) -> tuple[bool, str, str, bool]
    validate_fallback_config() -> list[str]
"""

from __future__ import annotations

import fcntl
import json
import math
import time
import urllib.request
from datetime import datetime, timezone

import config.paths as _paths
from engine import fallback_cache
from engine.shared import log

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_HTTP_TIMEOUT = 10


def _load_pi_config() -> dict:
    """Load config_pi.json fresh (no cache). Return empty dict on failure."""
    try:
        with open(_paths.PI_AGENT_CONFIG, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_codex_auth() -> tuple[str, str] | None:
    """Read access_token + account_id with 2-stage fallback.

    1. ~/.pi/agent/auth.json -> openai-codex entry (expires in ms)
    2. ~/.codex/auth.json -> tokens.{access_token, account_id} (expires_at in sec)

    Returns (access_token, account_id) or None on any failure / expired token.
    """
    # pi auth (preferred)
    try:
        with open(_paths.PI_AUTH_FILE, "r") as f:
            pi_auth = json.load(f)
    except (OSError, json.JSONDecodeError):
        pi_auth = None
    if isinstance(pi_auth, dict):
        entry = pi_auth.get("openai-codex")
        if isinstance(entry, dict):
            access = entry.get("access")
            account_id = entry.get("accountId")
            expires = entry.get("expires")
            if isinstance(access, str) and access and isinstance(account_id, str) and account_id:
                if isinstance(expires, (int, float)):
                    if expires / 1000.0 < time.time():
                        return None
                return (access, account_id)

    # codex auth (fallback)
    try:
        with open(_paths.CODEX_AUTH_FILE, "r") as f:
            codex_auth = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(codex_auth, dict):
        return None
    tokens = codex_auth.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(account_id, str) or not account_id:
        return None
    expires_at = tokens.get("expires_at")
    if isinstance(expires_at, (int, float)):
        if expires_at < time.time():
            return None
    return (access, account_id)


def get_codex_usage() -> tuple[bool, float, datetime | None]:
    """Fetch openai-codex weekly quota usage.

    Returns (ok, used_percent, reset_dt). Fail-open on any error.
    """
    try:
        creds = _load_codex_auth()
        if creds is None:
            return (False, 0.0, None)
        access, account_id = creds
        req = urllib.request.Request(
            _USAGE_URL,
            headers={
                "Authorization": f"Bearer {access}",
                "ChatGPT-Account-Id": account_id,
                "Accept": "application/json",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "User-Agent": "Mozilla/5.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            log(f"WARN openai_codex_quota: GET {_USAGE_URL} failed: {e!r}")
            return (False, 0.0, None)
        if not isinstance(payload, dict):
            return (False, 0.0, None)
        rl = payload.get("rate_limit") or payload.get("rate_limits") or {}
        if not isinstance(rl, dict):
            return (False, 0.0, None)
        wk = rl.get("weekly") or rl.get("secondary") or {}
        if not isinstance(wk, dict):
            return (False, 0.0, None)

        raw = wk.get("percent_left", wk.get("remaining_percent"))
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return (False, 0.0, None)
        if not math.isfinite(val):
            return (False, 0.0, None)
        val = max(0.0, min(100.0, val))
        used_percent = 100.0 - val

        reset_dt: datetime | None = None
        reset_ms = wk.get("reset_time_ms")
        if isinstance(reset_ms, (int, float)):
            try:
                reset_dt = datetime.fromtimestamp(reset_ms / 1000.0, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                reset_dt = None
        elif isinstance(wk.get("reset_at"), str):
            try:
                s = wk["reset_at"].replace("Z", "+00:00")
                reset_dt = datetime.fromisoformat(s)
                if reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                reset_dt = None
        return (True, used_percent, reset_dt)
    except Exception as e:
        log(f"WARN openai_codex_quota: get_codex_usage exception: {e!r}")
        return (False, 0.0, None)


def _cache_path(agent_id: str):
    return _paths.OPENAI_CODEX_QUOTA_CACHE_DIR / f"{agent_id}.json"


def _cache_active(cache: dict | None) -> bool:
    return fallback_cache.cache_active(
        cache,
        validators={
            "fallback_provider": lambda v: isinstance(v, str) and bool(v),
            "fallback_model": lambda v: isinstance(v, str) and bool(v),
        },
    )


def _reset_pi_session(agent_id: str) -> None:
    """Best-effort reset of pi session for fallback. WARN on exception."""
    try:
        from engine.backend_pi import reset_session
        reset_session(agent_id)
    except Exception as e:
        log(f"WARN openai_codex_quota: reset_session({agent_id}) failed: {e!r}")


def should_fallback(agent_id: str) -> tuple[bool, str, str, bool]:
    """Decide whether to fall back. Called only from backend_pi.send().

    Returns (active, fallback_provider, fallback_model, new_period).
    """
    try:
        cfg = _load_pi_config().get(agent_id) or {}
        if not isinstance(cfg, dict):
            return (False, "", "", False)
        if cfg.get("provider") != "openai-codex":
            return (False, "", "", False)
        if not cfg.get("fallback"):
            return (False, "", "", False)
        fb_provider = cfg.get("fallback_provider", "")
        fb_model = cfg.get("fallback_model", "")
        if not isinstance(fb_provider, str) or not fb_provider:
            return (False, "", "", False)
        if not isinstance(fb_model, str) or not fb_model:
            return (False, "", "", False)
        try:
            threshold = int(cfg.get("usage_threshold", 95))
        except (TypeError, ValueError):
            threshold = 95
        if threshold < 0 or threshold > 100:
            threshold = 95

        existing = fallback_cache.read_cache(_cache_path(agent_id))
        if _cache_active(existing):
            return (True, existing["fallback_provider"], existing["fallback_model"], False)

        ok, used_percent, reset_dt = get_codex_usage()
        if not ok:
            return (False, "", "", False)
        if used_percent < threshold:
            return (False, "", "", False)

        cache_dir = _paths.OPENAI_CODEX_QUOTA_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock_path = cache_dir / f"{agent_id}.lock"
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            existing = fallback_cache.read_cache(_cache_path(agent_id))
            if _cache_active(existing):
                return (True, existing["fallback_provider"], existing["fallback_model"], False)

            _reset_pi_session(agent_id)

            until = fallback_cache.clamp_reset_time(
                reset_dt, default_hrs=6, max_hrs=168,
            )
            pct_disp = int(round(used_percent))
            payload = {
                "active": True,
                "fallback_provider": fb_provider,
                "fallback_model": fb_model,
                "until": until.isoformat(),
                "reason": f"Codex usage {pct_disp}% (>={threshold})",
            }
            fallback_cache.atomic_write_cache(_cache_path(agent_id), payload)
            return (True, fb_provider, fb_model, True)
    except Exception as e:
        log(f"WARN openai_codex_quota: should_fallback({agent_id}) exception: {e!r}")
        return (False, "", "", False)


def validate_fallback_config() -> list[str]:
    """Pure validation. Return list of WARN strings (caller logs)."""
    warnings: list[str] = []
    cfg = _load_pi_config()
    for agent_id, entry in cfg.items():
        if agent_id == "_comment":
            continue
        if not isinstance(entry, dict):
            continue
        if not entry.get("fallback"):
            continue
        provider = entry.get("provider", "")
        if provider != "openai-codex":
            warnings.append(
                f"WARN openai_codex_quota: agent '{agent_id}' has fallback=true but "
                f"provider '{provider}' is not 'openai-codex'; fallback will not engage"
            )
        fb_provider = entry.get("fallback_provider", "")
        fb_model = entry.get("fallback_model", "")
        if not isinstance(fb_provider, str) or not fb_provider:
            warnings.append(
                f"WARN openai_codex_quota: agent '{agent_id}' has fallback=true but "
                f"fallback_provider is empty/invalid"
            )
        if not isinstance(fb_model, str) or not fb_model:
            warnings.append(
                f"WARN openai_codex_quota: agent '{agent_id}' has fallback=true but "
                f"fallback_model is empty/invalid"
            )
        threshold = entry.get("usage_threshold", 95)
        try:
            t = int(threshold)
        except (TypeError, ValueError):
            warnings.append(
                f"WARN openai_codex_quota: agent '{agent_id}' has non-integer "
                f"usage_threshold '{threshold}'; defaulting to 95"
            )
            continue
        if t < 0 or t > 100:
            warnings.append(
                f"WARN openai_codex_quota: agent '{agent_id}' usage_threshold {t} "
                f"out of range [0,100]; defaulting to 95"
            )
    return warnings
