"""engine/gemini_quota.py - Gemini Pro usage-based fallback to alt backend.

Uses the Code Assist Internal API (cloudcode-pa.googleapis.com) — the same
endpoint the official Gemini CLI uses internally — to detect when an agent's
Pro quota is near exhaustion, and routes operations to a configured fallback
backend (pi/cc) until reset time.

Public surface:
    resolve_fallback(agent_id) -> str       # cache read only, no HTTP
    should_fallback(agent_id) -> tuple      # called from send() only
    get_pro_quota() -> tuple                # raw API call helper
    validate_fallback_config() -> list[str] # startup validation

Cache file format (~/.gokrax/quota-cache/<agent_id>.json):
    {"active": true, "fallback_to": "pi", "until": "<ISO-8601>", "reason": "..."}
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config.paths as _paths
from engine.shared import log

_OAUTH_CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
_OAUTH_CLIENT_SECRET = "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"
_API_BASE = "https://cloudcode-pa.googleapis.com/v1internal"
_TOKEN_REFRESH_URL = "https://oauth2.googleapis.com/token"

_HTTP_TIMEOUT = 10
_VALID_FALLBACK_BACKENDS = frozenset({"pi", "cc"})


def _load_oauth_creds() -> dict | None:
    """Load OAuth creds from GEMINI_OAUTH_CREDS. Return None on any failure."""
    try:
        with open(_paths.GEMINI_OAUTH_CREDS, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("access_token"), str):
        return None
    if not isinstance(data.get("refresh_token"), str):
        return None
    return data


def _load_settings_auth_type() -> str:
    """Read security.auth.selectedType from settings.json. Return '' on failure."""
    try:
        with open(_paths.GEMINI_SETTINGS, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    sec = data.get("security")
    if not isinstance(sec, dict):
        return ""
    auth = sec.get("auth")
    if not isinstance(auth, dict):
        return ""
    sel = auth.get("selectedType")
    return sel if isinstance(sel, str) else ""


def _refresh_token_if_expired(creds: dict) -> dict:
    """In-place update + DCL flock. Refreshes access_token if expired.

    Returns the (possibly updated) creds dict. On refresh failure, returns
    creds unchanged (caller may still attempt API; will likely fail).
    """
    path = _paths.GEMINI_OAUTH_CREDS
    if time.time() < float(creds.get("expires_at", 0)):
        return creds
    try:
        f = open(path, "r+")
    except OSError:
        return creds
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        try:
            on_disk = json.load(f)
        except json.JSONDecodeError:
            on_disk = creds
        # DCL: another process may have refreshed while we waited
        if time.time() < float(on_disk.get("expires_at", 0)):
            return on_disk
        body = urllib.parse.urlencode({
            "client_id": _OAUTH_CLIENT_ID,
            "client_secret": _OAUTH_CLIENT_SECRET,
            "refresh_token": on_disk.get("refresh_token", creds.get("refresh_token", "")),
            "grant_type": "refresh_token",
        }).encode("utf-8")
        req = urllib.request.Request(
            _TOKEN_REFRESH_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            log(f"WARN gemini_quota: token refresh failed: {e!r}")
            return on_disk
        new_access = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(new_access, str) or not isinstance(expires_in, (int, float)):
            return on_disk
        on_disk["access_token"] = new_access
        on_disk["expires_at"] = time.time() + float(expires_in)
        f.seek(0)
        f.write(json.dumps(on_disk))
        f.truncate()
        f.flush()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return on_disk
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


def _post_json(url: str, token: str, body: dict) -> dict | None:
    """POST JSON to Code Assist API. Return parsed JSON or None on failure."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"WARN gemini_quota: POST {url} failed: {e!r}")
        return None


def _load_code_assist(token: str) -> str | None:
    """Call loadCodeAssist; return cloudaicompanionProject id or None."""
    body = {
        "cloudaicompanionProject": "",
        "metadata": {
            "ideType": "GEMINI_CLI",
            "pluginType": "GEMINI",
            "platform": "linux",
            "pluginVersion": "n/a",
        },
    }
    resp = _post_json(_API_BASE + ":loadCodeAssist", token, body)
    if not isinstance(resp, dict):
        return None
    project = resp.get("cloudaicompanionProject")
    return project if isinstance(project, str) and project else None


def _retrieve_user_quota(token: str, project_id: str) -> list[dict]:
    """Call retrieveUserQuota; return buckets list (empty on failure)."""
    resp = _post_json(_API_BASE + ":retrieveUserQuota", token, {"project": project_id})
    if not isinstance(resp, dict):
        return []
    buckets = resp.get("buckets")
    if not isinstance(buckets, list):
        return []
    return [b for b in buckets if isinstance(b, dict)]


def get_pro_quota() -> tuple[bool, float, datetime | None]:
    """Fetch Pro quota usage.

    Returns (ok, usage_fraction, reset_time_utc).
    On any failure / non oauth-personal / no Pro bucket: (False, 0.0, None).
    """
    try:
        if _load_settings_auth_type() != "oauth-personal":
            return (False, 0.0, None)
        creds = _load_oauth_creds()
        if creds is None:
            return (False, 0.0, None)
        creds = _refresh_token_if_expired(creds)
        token = creds.get("access_token", "")
        if not isinstance(token, str) or not token:
            return (False, 0.0, None)
        project_id = _load_code_assist(token)
        if not project_id:
            return (False, 0.0, None)
        buckets = _retrieve_user_quota(token, project_id)
        if not buckets:
            return (False, 0.0, None)
        pro_buckets = [b for b in buckets if "pro" in str(b.get("modelId", "")).lower()]
        if not pro_buckets:
            return (False, 0.0, None)
        # Choose the most-depleted Pro bucket (smallest remainingFraction)
        chosen = None
        chosen_remaining = None
        for b in pro_buckets:
            try:
                rem = float(b.get("remainingFraction", 0.0))
            except (TypeError, ValueError):
                continue
            rem = max(0.0, min(1.0, rem))
            if chosen_remaining is None or rem < chosen_remaining:
                chosen = b
                chosen_remaining = rem
        if chosen is None:
            return (False, 0.0, None)
        usage = 1.0 - chosen_remaining
        reset_str = chosen.get("resetTime")
        reset_dt: datetime | None = None
        if isinstance(reset_str, str) and reset_str:
            try:
                # Python's fromisoformat supports trailing Z only on 3.11+
                s = reset_str.replace("Z", "+00:00")
                reset_dt = datetime.fromisoformat(s)
                if reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                reset_dt = None
        return (True, usage, reset_dt)
    except Exception as e:
        log(f"WARN gemini_quota: get_pro_quota exception: {e!r}")
        return (False, 0.0, None)


def _read_cache(agent_id: str) -> dict | None:
    """Read agent quota cache. Return dict or None if missing/corrupt."""
    path = _paths.GEMINI_QUOTA_CACHE_DIR / f"{agent_id}.json"
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _cache_active(cache: dict | None) -> bool:
    """Check if a cache dict represents an active fallback period."""
    if not cache or not cache.get("active"):
        return False
    until = cache.get("until")
    if not isinstance(until, str):
        return False
    try:
        until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if until_dt.tzinfo is None:
            until_dt = until_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return until_dt > datetime.now(timezone.utc)


def resolve_fallback(agent_id: str) -> str:
    """Cache-only resolution. Returns fallback backend name or "".

    No HTTP, no config read. Called from resolve_backend() hot path.
    """
    cache = _read_cache(agent_id)
    if not _cache_active(cache):
        return ""
    fb = cache.get("fallback_to") if cache else ""
    return fb if isinstance(fb, str) else ""


def _load_agent_config() -> dict:
    """Load config_gemini.json. Return empty dict on failure."""
    try:
        with open(_paths.GEMINI_AGENT_CONFIG, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_cache(agent_id: str, payload: dict) -> None:
    cache_dir = _paths.GEMINI_QUOTA_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{agent_id}.json"
    tmp = cache_dir / f"{agent_id}.json.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def _reset_fallback_backend(agent_id: str, fallback_to: str) -> None:
    """Best-effort reset_session on fallback backend. WARN on exception."""
    try:
        if fallback_to == "pi":
            from engine.backend_pi import reset_session as _reset
        elif fallback_to == "cc":
            from engine.backend_cc import reset_session as _reset
        else:
            return
        _reset(agent_id)
    except Exception as e:
        log(f"WARN gemini_quota: reset_session({agent_id}, {fallback_to}) failed: {e!r}")


def _clamp_reset_time(reset_dt: datetime | None) -> datetime:
    now = datetime.now(timezone.utc)
    if reset_dt is None:
        return now + timedelta(hours=1)
    if reset_dt > now + timedelta(hours=48):
        return now + timedelta(hours=1)
    if reset_dt < now:
        return now + timedelta(minutes=5)
    return reset_dt


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
        model = str(cfg.get("model", ""))
        if "pro" not in model.lower():
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

        ok, usage_fraction, reset_dt = get_pro_quota()
        if not ok:
            return (False, "", False)
        if usage_fraction < threshold / 100.0:
            return (False, "", False)

        cache_dir = _paths.GEMINI_QUOTA_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock_path = cache_dir / f"{agent_id}.lock"
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            existing = _read_cache(agent_id)
            if _cache_active(existing):
                fb_existing = existing.get("fallback_to") if existing else ""
                if isinstance(fb_existing, str) and fb_existing:
                    return (True, fb_existing, False)
                return (True, fallback_to, False)

            _reset_fallback_backend(agent_id, fallback_to)

            until = _clamp_reset_time(reset_dt)
            pct = int(round(usage_fraction * 100))
            payload = {
                "active": True,
                "fallback_to": fallback_to,
                "until": until.isoformat(),
                "reason": f"Pro quota {pct}% (>={threshold})",
            }
            _atomic_write_cache(agent_id, payload)
            return (True, fallback_to, True)
    except Exception as e:
        log(f"WARN gemini_quota: should_fallback({agent_id}) exception: {e!r}")
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
        model = str(entry.get("model", ""))
        if "pro" not in model.lower():
            warnings.append(
                f"WARN gemini_quota: agent '{agent_id}' has fallback=true but model "
                f"'{model}' does not contain 'pro'; fallback will not engage"
            )
        fb = entry.get("fallback_backend", "")
        if fb == "":
            continue
        if not isinstance(fb, str) or fb not in _VALID_FALLBACK_BACKENDS:
            warnings.append(
                f"WARN gemini_quota: agent '{agent_id}' has invalid fallback_backend "
                f"'{fb}'; expected one of {sorted(_VALID_FALLBACK_BACKENDS)}"
            )
        threshold = entry.get("usage_threshold", 95)
        try:
            t = int(threshold)
        except (TypeError, ValueError):
            warnings.append(
                f"WARN gemini_quota: agent '{agent_id}' has non-integer "
                f"usage_threshold '{threshold}'; defaulting to 95"
            )
            continue
        if t < 0 or t > 100:
            warnings.append(
                f"WARN gemini_quota: agent '{agent_id}' usage_threshold {t} "
                f"out of range [0,100]; defaulting to 95"
            )
    return warnings
