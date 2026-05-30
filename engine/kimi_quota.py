"""engine/kimi_quota.py - kimi (Moonshot AI) usage-based fallback to alt backend.

Uses the kimi-code coding API (api.kimi.com/coding/v1) — the same endpoint the
kimi CLI ``/usage`` slash command uses internally — to detect when an agent's
quota is near exhaustion, and routes operations to a configured fallback
backend (pi/cc/openclaw) until reset time.

Proactive REST variant: quota is fetched via ``GET /coding/v1/usages`` using the
kimi CLI's own OAuth token. The token is read read-only from KIMI_OAUTH_CREDS;
expired tokens are NOT refreshed (kimi rotates refresh_token on refresh and
writing the shared creds could break a concurrently running kimi CLI). An
expired token is fail-open — the cycle simply does not detect quota.

Public surface:
    resolve_fallback(agent_id) -> str       # cache read only, no HTTP
    should_fallback(agent_id) -> tuple      # called from send() only
    get_kimi_quota() -> tuple               # raw API call helper
    validate_fallback_config() -> list[str] # startup validation

Cache file format (~/.gokrax/quota-cache-kimi/<agent_id>.json):
    {"active": true, "fallback_to": "pi", "until": "<ISO-8601>", "reason": "..."}
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config.paths as _paths
from engine import fallback_cache
from engine.shared import log

_DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"
_HTTP_TIMEOUT = 10
_NEGATIVE_CACHE_SEC = 300
_VALID_FALLBACK_BACKENDS = frozenset({"pi", "cc", "openclaw"})
_KNOWN_WINDOW_DURATION_MIN = 300  # 5h rolling window (300 minutes)


def _load_token() -> str | None:
    """Load access_token from kimi creds (read-only). None on failure or expiry (fail-open)."""
    try:
        data = json.loads(_paths.KIMI_OAUTH_CREDS.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    expires_at = data.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at <= time.time():
        return None  # expired — fail-open, no refresh
    return token


def _get_json(url: str, token: str) -> dict | None:
    """GET JSON with Bearer auth. Return parsed dict or None on failure."""
    headers = {"Authorization": f"Bearer {token}"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"WARN kimi_quota: GET {url} failed: {e!r}")
        return None


def _parse_window(w: dict) -> float | None:
    """Parse used/limit from a quota window dict. Return usage fraction [0.0, 1.0] or None."""
    try:
        limit = int(w["limit"])
    except (KeyError, TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    used: int | None = None
    used_raw = w.get("used")
    if used_raw is not None and used_raw != "":
        try:
            used = int(used_raw)
        except (TypeError, ValueError):
            pass  # fallthrough to remaining
    if used is None:
        remaining_raw = w.get("remaining")
        if remaining_raw is None or remaining_raw == "":
            return None
        try:
            used = limit - int(remaining_raw)
        except (TypeError, ValueError):
            return None
    return max(0.0, min(1.0, used / limit))


def _parse_reset_time(s: object) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def get_kimi_quota() -> tuple[bool, float, datetime | None]:
    """Fetch account-level kimi quota usage.

    Returns (ok, usage_fraction, reset_time_utc). Picks the most exhausted of
    the weekly window and any recognized rolling window.
    On any failure / no recognized quota: (False, 0.0, None).
    """
    try:
        token = _load_token()
        if token is None:
            return (False, 0.0, None)
        base_url = os.environ.get("KIMI_CODE_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
        data = _get_json(f"{base_url}/usages", token)
        if not isinstance(data, dict):
            return (False, 0.0, None)

        # Collect (usage_fraction, resetTime_str) from all recognized windows
        windows: list[tuple[float, str | None]] = []

        # Weekly: top-level "usage" (always included)
        weekly = data.get("usage")
        if isinstance(weekly, dict):
            frac = _parse_window(weekly)
            if frac is not None:
                windows.append((frac, weekly.get("resetTime")))

        # Rolling: only accept known window types from "limits[]"
        limits = data.get("limits")
        if isinstance(limits, list):
            for entry in limits:
                if not isinstance(entry, dict):
                    continue
                window = entry.get("window")
                if not isinstance(window, dict):
                    continue
                duration = window.get("duration")
                time_unit = window.get("timeUnit", "")
                if not (duration == _KNOWN_WINDOW_DURATION_MIN
                        and isinstance(time_unit, str)
                        and "MINUTE" in time_unit):
                    continue  # skip unknown window types
                detail = entry.get("detail")
                if isinstance(detail, dict):
                    frac = _parse_window(detail)
                    if frac is not None:
                        windows.append((frac, detail.get("resetTime")))

        if not windows:
            return (False, 0.0, None)

        # Pick the most exhausted window
        best_frac, best_reset_str = max(windows, key=lambda x: x[0])
        reset_dt = _parse_reset_time(best_reset_str)
        return (True, best_frac, reset_dt)
    except Exception as e:
        log(f"WARN kimi_quota: get_kimi_quota exception: {e!r}")
        return (False, 0.0, None)


def _cache_path(agent_id: str) -> Path:
    return _paths.KIMI_QUOTA_CACHE_DIR / f"{agent_id}.json"


def _read_cache(agent_id: str) -> dict | None:
    """Read agent quota cache. Return dict or None if missing/corrupt."""
    return fallback_cache.read_cache(_cache_path(agent_id))


def _cache_active(cache: dict | None) -> bool:
    """Check if a cache dict represents an active fallback period."""
    return fallback_cache.cache_active(
        cache,
        validators={
            "fallback_to": lambda v: isinstance(v, str) and v in _VALID_FALLBACK_BACKENDS,
        },
    )


def _negative_cache_path(agent_id: str) -> Path:
    return _paths.KIMI_QUOTA_CACHE_DIR / f"{agent_id}.neg"


def _negative_cache_active(agent_id: str) -> bool:
    """True if a non-expired negative cache exists and the token file is unchanged."""
    try:
        data = json.loads(_negative_cache_path(agent_id).read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    until_str = data.get("until")
    if not isinstance(until_str, str):
        return False
    try:
        until_dt = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
        if until_dt.tzinfo is None:
            until_dt = until_dt.replace(tzinfo=timezone.utc)
        if until_dt <= datetime.now(timezone.utc):
            return False
    except ValueError:
        return False
    saved_mtime = data.get("token_mtime")
    if saved_mtime is not None:
        try:
            current_mtime = _paths.KIMI_OAUTH_CREDS.stat().st_mtime
            if current_mtime != saved_mtime:
                return False
        except OSError:
            pass
    return True


def _write_negative_cache(agent_id: str) -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=_NEGATIVE_CACHE_SEC)
    payload: dict = {"until": until.isoformat()}
    try:
        payload["token_mtime"] = _paths.KIMI_OAUTH_CREDS.stat().st_mtime
    except OSError:
        pass
    try:
        _paths.KIMI_QUOTA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fallback_cache.atomic_write_cache(
            _negative_cache_path(agent_id), payload,
        )
    except Exception:
        pass


def _clear_negative_cache(agent_id: str) -> None:
    try:
        _negative_cache_path(agent_id).unlink(missing_ok=True)
    except OSError:
        pass


def resolve_fallback(agent_id: str) -> str:
    """Cache-only resolution. Returns fallback backend name or "".

    No HTTP, no config read. Called from resolve_backend() hot path.
    """
    cache = _read_cache(agent_id)
    if not _cache_active(cache):
        return ""
    return cache.get("fallback_to", "")


def _load_agent_config() -> dict:
    """Load config_kimi.json fresh each time. Return empty dict on failure."""
    try:
        with open(_paths.KIMI_AGENT_CONFIG, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _reset_fallback_backend(agent_id: str, fallback_to: str) -> None:
    """Best-effort reset_session on fallback backend. WARN on exception."""
    try:
        if fallback_to == "pi":
            from engine.backend_pi import reset_session as _reset
        elif fallback_to == "cc":
            from engine.backend_cc import reset_session as _reset
        elif fallback_to == "openclaw":
            return  # openclaw uses /new; reset_session is a no-op
        else:
            return
        _reset(agent_id)
    except Exception as e:
        log(f"WARN kimi_quota: reset_session({agent_id}, {fallback_to}) failed: {e!r}")


def should_fallback(agent_id: str) -> tuple[bool, str, bool]:
    """Decide whether to fall back. Called only from send().

    Returns (active, fallback_to, new_period).
    DCL critical section: cache recheck -> reset_session -> cache write.
    """
    try:
        cfg = _load_agent_config().get(agent_id) or {}
        if not isinstance(cfg, dict):
            return (False, "", False)
        if not cfg.get("fallback"):
            return (False, "", False)
        fallback_to = cfg.get("fallback_backend", "")
        if not isinstance(fallback_to, str) or not fallback_to:
            return (False, "", False)
        if fallback_to not in _VALID_FALLBACK_BACKENDS:
            return (False, "", False)
        try:
            threshold = int(cfg.get("usage_threshold", 95))
        except (TypeError, ValueError):
            threshold = 95
        if threshold < 0 or threshold > 100:
            threshold = 95

        if _negative_cache_active(agent_id):
            return (False, "", False)

        ok, usage_fraction, reset_dt = get_kimi_quota()
        if not ok:
            _write_negative_cache(agent_id)
            return (False, "", False)
        _clear_negative_cache(agent_id)
        if usage_fraction < threshold / 100.0:
            return (False, "", False)

        cache_dir = _paths.KIMI_QUOTA_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock_path = cache_dir / f"{agent_id}.lock"
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            existing = _read_cache(agent_id)
            if _cache_active(existing):
                return (True, existing["fallback_to"], False)

            _reset_fallback_backend(agent_id, fallback_to)

            until = fallback_cache.clamp_reset_time(reset_dt, default_hrs=5, max_hrs=168)
            pct = int(round(usage_fraction * 100))
            payload = {
                "active": True,
                "fallback_to": fallback_to,
                "until": until.isoformat(),
                "reason": f"kimi quota {pct}% (>={threshold})",
            }
            fallback_cache.atomic_write_cache(_cache_path(agent_id), payload)
            return (True, fallback_to, True)
    except Exception as e:
        log(f"WARN kimi_quota: should_fallback({agent_id}) exception: {e!r}")
        return (False, "", False)


def validate_fallback_config() -> list[str]:
    """Pure validation. Return list of WARN strings (caller logs)."""
    warnings: list[str] = []
    cfg = _load_agent_config()
    for agent_id, entry in cfg.items():
        if agent_id == "_comment":
            continue
        if not isinstance(entry, dict):
            continue
        if not entry.get("fallback"):
            continue
        fb = entry.get("fallback_backend", "")
        if fb == "":
            continue
        if not isinstance(fb, str) or fb not in _VALID_FALLBACK_BACKENDS:
            warnings.append(
                f"WARN kimi_quota: agent '{agent_id}' has invalid fallback_backend "
                f"'{fb}'; expected one of {sorted(_VALID_FALLBACK_BACKENDS)}"
            )
        threshold = entry.get("usage_threshold", 95)
        try:
            t = int(threshold)
        except (TypeError, ValueError):
            warnings.append(
                f"WARN kimi_quota: agent '{agent_id}' has non-integer "
                f"usage_threshold '{threshold}'; defaulting to 95"
            )
            continue
        if t < 0 or t > 100:
            warnings.append(
                f"WARN kimi_quota: agent '{agent_id}' usage_threshold {t} "
                f"out of range [0,100]; defaulting to 95"
            )
    return warnings
