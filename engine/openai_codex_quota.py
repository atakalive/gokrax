"""engine/openai_codex_quota.py - openai-codex usage-based fallback for PI backend.

Reuses pi (or codex CLI) OAuth credentials to query
``https://chatgpt.com/backend-api/wham/usage`` and routes a PI agent's
``--model`` argument from openai-codex to a configured fallback
provider/model when weekly quota is near exhaustion.

Public surface:
    get_codex_usage() -> tuple[bool, float, datetime | None]
    should_fallback(agent_id) -> tuple[bool, str, str, bool]
    resolve_fallback_backend(agent_id) -> str
    is_mode_b_backend(fb_backend) -> bool
    validate_fallback_config() -> list[str]
"""

from __future__ import annotations

import fcntl
import json
import math
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import config.paths as _paths
from engine import fallback_cache
from engine.shared import log

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_HTTP_TIMEOUT = 10
# Valid Mode B (backend redirect) targets. Quota-aware backends (gemini/agy/kimi)
# are intentionally excluded to prevent transitive loops (e.g. pi→agy→pi).
# Defined locally (not imported from backend_pi) to avoid circular import,
# consistent with gemini/agy/kimi quota modules.
_VALID_FALLBACK_BACKENDS = frozenset({"pi", "cc", "openclaw"})


def is_mode_b_backend(fb_backend: str) -> bool:
    """Return True if fb_backend is a valid Mode B target (non-pi, non-quota-aware).

    Used by backend.py and backend_pi.py to determine Mode B activation.
    The single source of truth for Mode B validity.
    """
    return (isinstance(fb_backend, str)
            and fb_backend in _VALID_FALLBACK_BACKENDS
            and fb_backend != "pi")


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
                expired = isinstance(expires, (int, float)) and expires / 1000.0 < time.time()
                if not expired:
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


def _parse_reset(wk: dict) -> datetime | None:
    """Parse weekly reset time from the window dict.

    Supports (in order): reset_at as Unix seconds (int/float, new schema),
    reset_after_seconds (int/float, new schema, relative), reset_time_ms
    (int/float, legacy, milliseconds), reset_at as ISO-8601 string (legacy).
    """
    reset_at = wk.get("reset_at")
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        try:
            return datetime.fromtimestamp(float(reset_at), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    reset_after = wk.get("reset_after_seconds")
    if isinstance(reset_after, (int, float)) and not isinstance(reset_after, bool):
        try:
            return datetime.now(timezone.utc) + timedelta(seconds=float(reset_after))
        except (OverflowError, OSError, ValueError):
            return None
    reset_ms = wk.get("reset_time_ms")
    if isinstance(reset_ms, (int, float)) and not isinstance(reset_ms, bool):
        try:
            return datetime.fromtimestamp(reset_ms / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(reset_at, str):
        try:
            s = reset_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
    return None


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
            log(f"WARN openai_codex_quota: payload not dict; type={type(payload).__name__}")
            return (False, 0.0, None)
        rl = payload.get("rate_limit") or payload.get("rate_limits") or {}
        if not isinstance(rl, dict):
            log(f"WARN openai_codex_quota: rate_limit invalid; type={type(rl).__name__}")
            return (False, 0.0, None)
        wk = rl.get("secondary_window") or rl.get("weekly") or rl.get("secondary") or {}
        if not isinstance(wk, dict) or not wk:
            log(
                f"WARN openai_codex_quota: weekly window key not found; "
                f"rate_limit_keys={sorted(rl.keys())[:20]}"
            )
            return (False, 0.0, None)

        used_raw = wk.get("used_percent")
        if used_raw is not None:
            try:
                used_percent = float(used_raw)
            except (TypeError, ValueError):
                log(
                    f"WARN openai_codex_quota: non-numeric used_percent={used_raw!r}; "
                    f"window_keys={sorted(wk.keys())[:20]}"
                )
                return (False, 0.0, None)
            if not math.isfinite(used_percent):
                log(f"WARN openai_codex_quota: non-finite used_percent={used_raw!r}")
                return (False, 0.0, None)
            used_percent = max(0.0, min(100.0, used_percent))
        else:
            left_raw = wk.get("percent_left", wk.get("remaining_percent"))
            try:
                left = float(left_raw)
            except (TypeError, ValueError):
                log(
                    f"WARN openai_codex_quota: no usable percent in window; "
                    f"window_keys={sorted(wk.keys())[:20]} percent_left={left_raw!r}"
                )
                return (False, 0.0, None)
            if not math.isfinite(left):
                log(f"WARN openai_codex_quota: non-finite percent_left={left_raw!r}")
                return (False, 0.0, None)
            left = max(0.0, min(100.0, left))
            used_percent = 100.0 - left

        return (True, used_percent, _parse_reset(wk))
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


def _cache_active_mode_b(cache: dict | None) -> bool:
    return fallback_cache.cache_active(
        cache,
        validators={
            "fallback_backend": lambda v: (isinstance(v, str)
                                           and v in _VALID_FALLBACK_BACKENDS
                                           and v != "pi"),
        },
    )


def resolve_fallback_backend(agent_id: str) -> str:
    """Cache-only resolution for Mode B. Returns fallback backend name or "".

    No HTTP, no config read. Called from resolve_backend() hot path.
    Consistent with gemini/agy/kimi resolve_fallback() pattern.
    """
    try:
        cache = fallback_cache.read_cache(_cache_path(agent_id))
        if not _cache_active_mode_b(cache):
            return ""
        return cache.get("fallback_backend", "")
    except Exception:
        return ""


def _reset_target_backend(agent_id: str, target: str) -> None:
    """Best-effort reset of target backend for fallback activation."""
    try:
        if target == "pi":
            from engine.backend_pi import reset_session as _reset
        elif target == "cc":
            from engine.backend_cc import reset_session as _reset
        elif target == "openclaw":
            return  # no-op — openclaw has no reset_session
        else:
            return
        _reset(agent_id)
    except Exception as e:
        log(f"WARN openai_codex_quota: reset_session({agent_id}, {target}) failed: {e!r}")


def should_fallback(agent_id: str) -> tuple[bool, str, str, bool]:
    """Decide whether to fall back.

    Mode A: called from backend_pi.send() — swaps provider/model within pi.
    Mode B: called from engine.backend.send() — redirects to another backend.

    Returns (active, value, model, new_period). ``value`` is the fallback
    provider in Mode A (e.g. "github-copilot") and the fallback backend name
    in Mode B (e.g. "cc"); ``model`` is "" in Mode B.
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
        fb_backend = cfg.get("fallback_backend", "")
        mode_b = is_mode_b_backend(fb_backend)

        # Mode A requires provider/model. Mode B does not.
        if not mode_b:
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
        if mode_b:
            if _cache_active_mode_b(existing):
                return (True, existing["fallback_backend"], "", False)
        else:
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

            until = fallback_cache.clamp_reset_time(
                reset_dt, default_hrs=6, max_hrs=168,
            )
            pct_disp = int(round(used_percent))

            if mode_b:
                if _cache_active_mode_b(existing):
                    return (True, existing["fallback_backend"], "", False)
                # Reset the backend that will take over (the redirect target).
                _reset_target_backend(agent_id, fb_backend)
                payload = {
                    "active": True,
                    "fallback_backend": fb_backend,
                    "until": until.isoformat(),
                    "reason": f"Codex usage {pct_disp}% (>={threshold})",
                }
                fallback_cache.atomic_write_cache(_cache_path(agent_id), payload)
                return (True, fb_backend, "", True)

            if _cache_active(existing):
                return (True, existing["fallback_provider"], existing["fallback_model"], False)
            # Mode A: reset pi itself (which keeps handling the work).
            _reset_target_backend(agent_id, "pi")
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
        fb_backend = entry.get("fallback_backend", "")
        mode_b = is_mode_b_backend(fb_backend)
        if not entry.get("fallback"):
            # Mode B (backend redirect) also requires fallback=true; otherwise
            # should_fallback() returns early and never engages.
            if mode_b:
                warnings.append(
                    f"WARN openai_codex_quota: agent '{agent_id}' has fallback_backend "
                    f"'{fb_backend}' but fallback is not true; fallback will not engage"
                )
            continue
        provider = entry.get("provider", "")
        if provider != "openai-codex":
            warnings.append(
                f"WARN openai_codex_quota: agent '{agent_id}' has fallback=true but "
                f"provider '{provider}' is not 'openai-codex'; fallback will not engage"
            )
        fb_provider = entry.get("fallback_provider", "")
        fb_model = entry.get("fallback_model", "")
        if fb_backend not in ("", "pi") and not mode_b:
            # Unknown fallback_backend value: stays in Mode A, never dispatched.
            warnings.append(
                f"WARN openai_codex_quota: agent '{agent_id}' has invalid fallback_backend "
                f"'{fb_backend}'; expected one of {sorted(_VALID_FALLBACK_BACKENDS)}; "
                f"treated as Mode A (provider/model swap)"
            )
        if not mode_b:
            # Mode A (provider/model swap) requires both fields.
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
