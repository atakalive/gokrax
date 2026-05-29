"""engine/agy_quota.py - agy (antigravity-cli) usage-based fallback to alt backend.

Uses the Code Assist Internal API (cloudcode-pa.googleapis.com) — the same
endpoint the antigravity client uses internally — to detect when an agent's
quota is near exhaustion, and routes operations to a configured fallback
backend (pi/cc/kimi/openclaw) until reset time.

Proactive REST variant: quota is fetched via
``:fetchAvailableModels`` (which returns per-model ``quotaInfo``) using the
agy client's own OAuth token. The token is read read-only from
``antigravity-oauth-token``; expired tokens are refreshed in-memory only
(the auth file is never written).

Public surface:
    resolve_fallback(agent_id) -> str       # cache read only, no HTTP
    should_fallback(agent_id) -> tuple      # called from send() only
    get_agy_quota(model) -> tuple           # raw API call helper
    validate_fallback_config() -> list[str] # startup validation

Cache file format (~/.gokrax/quota-cache-agy/<agent_id>.json):
    {"active": true, "fallback_to": "pi", "until": "<ISO-8601>", "reason": "..."}
"""

from __future__ import annotations

import fcntl
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config.paths as _paths
from engine import fallback_cache
from engine.shared import log

_OAUTH_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep"
_OAUTH_CLIENT_SECRET = "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"
_API_BASE = "https://cloudcode-pa.googleapis.com/v1internal"
_TOKEN_REFRESH_URL = "https://oauth2.googleapis.com/token"
_HTTP_TIMEOUT = 10
_NEGATIVE_CACHE_SEC = 300
_VALID_FALLBACK_BACKENDS = frozenset({"pi", "cc", "kimi", "openclaw"})


def _antigravity_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Antigravity/1.18.3 Chrome/138.0.7204.235 "
            "Electron/37.3.1 Safari/537.36"
        ),
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        "Client-Metadata": '{"ideType":"ANTIGRAVITY","platform":"LINUX_AMD64","pluginType":"GEMINI"}',
    }


def _post_json(url: str, token: str, body: dict) -> dict | None:
    """POST JSON to Code Assist API with antigravity headers. Return parsed JSON or None."""
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        **_antigravity_headers(),
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"WARN agy_quota: POST {url} failed: {e!r}")
        return None


def _refresh_token_in_memory(token_data: dict) -> dict | None:
    """Refresh an expired token in-memory only. Never writes the auth file."""
    refresh_tok = token_data.get("refresh_token")
    if not isinstance(refresh_tok, str) or not refresh_tok:
        return None
    body = urllib.parse.urlencode({
        "client_id": _OAUTH_CLIENT_ID,
        "client_secret": _OAUTH_CLIENT_SECRET,
        "refresh_token": refresh_tok,
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
        log(f"WARN agy_quota: in-memory token refresh failed: {e!r}")
        return None
    new_access = payload.get("access_token")
    if not isinstance(new_access, str) or not new_access:
        return None
    return {**token_data, "access_token": new_access}


def _load_token() -> dict | None:
    """Load the inner ``token`` dict from AGY_OAUTH_TOKEN. Refresh in-memory if expired.

    Returns the (possibly refreshed) inner token dict, or None on any failure
    (fail-open).
    """
    try:
        with open(_paths.AGY_OAUTH_TOKEN, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    if not isinstance(token, dict):
        return None
    if not isinstance(token.get("access_token"), str) or not token["access_token"]:
        return None
    expiry = token.get("expiry")
    expired = False
    if isinstance(expiry, str) and expiry:
        try:
            exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt < datetime.now(timezone.utc):
                expired = True
        except ValueError:
            return None
    elif isinstance(expiry, (int, float)):
        if expiry < time.time():
            expired = True
    if expired:
        return _refresh_token_in_memory(token)
    return token


def _load_code_assist(token: str) -> str | None:
    """Call loadCodeAssist; return cloudaicompanionProject id or None."""
    body = {
        "cloudaicompanionProject": "",
        "metadata": {
            "ideType": "ANTIGRAVITY",
            "pluginType": "GEMINI",
            "platform": "LINUX_AMD64",
            "pluginVersion": "n/a",
        },
    }
    resp = _post_json(_API_BASE + ":loadCodeAssist", token, body)
    if not isinstance(resp, dict):
        return None
    project = resp.get("cloudaicompanionProject")
    return project if isinstance(project, str) and project else None


def _model_family(name: str) -> str:
    """Classify a model name into a quota family (case-insensitive substring)."""
    lower = name.lower()
    if "claude" in lower:
        return "claude"
    if "gpt" in lower:
        return "gpt"
    if "gemini" in lower:
        return "gemini"
    return "unknown"


def _fetch_available_models(token: str, project_id: str) -> dict:
    """Call fetchAvailableModels; return models dict (name -> dict). Empty on failure."""
    resp = _post_json(_API_BASE + ":fetchAvailableModels", token, {"project": project_id})
    if not isinstance(resp, dict):
        return {}
    models = resp.get("models")
    if not isinstance(models, dict):
        return {}
    return {k: v for k, v in models.items() if isinstance(v, dict)}


def get_agy_quota(model: str) -> tuple[bool, float, datetime | None]:
    """Fetch quota usage for the family that ``model`` belongs to.

    Returns (ok, usage_fraction, reset_time_utc).
    On any failure / no matching quota: (False, 0.0, None).
    """
    try:
        token_data = _load_token()
        if token_data is None:
            return (False, 0.0, None)
        access_token = token_data["access_token"]
        project_id = _load_code_assist(access_token)
        if not project_id:
            return (False, 0.0, None)
        models = _fetch_available_models(access_token, project_id)
        if not models:
            return (False, 0.0, None)

        target_family = _model_family(model)
        if target_family == "unknown":
            family_entries = [
                (name, entry) for name, entry in models.items()
                if isinstance(entry.get("quotaInfo"), dict)
            ]
        else:
            family_entries = [
                (name, entry) for name, entry in models.items()
                if _model_family(name) == target_family
                and isinstance(entry.get("quotaInfo"), dict)
            ]
            if not family_entries:
                family_entries = [
                    (name, entry) for name, entry in models.items()
                    if isinstance(entry.get("quotaInfo"), dict)
                ]
        if not family_entries:
            return (False, 0.0, None)

        chosen_remaining = None
        chosen_reset_str = None
        for name, entry in family_entries:
            qi = entry["quotaInfo"]
            try:
                rem = float(qi.get("remainingFraction", 0.0))
            except (TypeError, ValueError):
                continue
            rem = max(0.0, min(1.0, rem))
            if chosen_remaining is None or rem < chosen_remaining:
                chosen_remaining = rem
                chosen_reset_str = qi.get("resetTime")

        if chosen_remaining is None:
            return (False, 0.0, None)

        usage = 1.0 - chosen_remaining
        reset_dt: datetime | None = None
        if isinstance(chosen_reset_str, str) and chosen_reset_str:
            try:
                s = chosen_reset_str.replace("Z", "+00:00")
                reset_dt = datetime.fromisoformat(s)
                if reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                reset_dt = None
        return (True, usage, reset_dt)
    except Exception as e:
        log(f"WARN agy_quota: get_agy_quota exception: {e!r}")
        return (False, 0.0, None)


def _cache_path(agent_id: str):
    return _paths.AGY_QUOTA_CACHE_DIR / f"{agent_id}.json"


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


def _negative_cache_path(agent_id: str):
    return _paths.AGY_QUOTA_CACHE_DIR / f"{agent_id}.neg"


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
            current_mtime = _paths.AGY_OAUTH_TOKEN.stat().st_mtime
            if current_mtime != saved_mtime:
                return False
        except OSError:
            pass
    return True


def _write_negative_cache(agent_id: str) -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=_NEGATIVE_CACHE_SEC)
    payload: dict = {"until": until.isoformat()}
    try:
        payload["token_mtime"] = _paths.AGY_OAUTH_TOKEN.stat().st_mtime
    except OSError:
        pass
    try:
        _paths.AGY_QUOTA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
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
    """Load config_agy.json fresh each time. Return empty dict on failure."""
    try:
        with open(_paths.AGY_AGENT_CONFIG, "r") as f:
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
        elif fallback_to == "kimi":
            from engine.backend_kimi import reset_session as _reset
        elif fallback_to == "openclaw":
            return  # openclaw uses /new; reset_session is a no-op
        else:
            return
        _reset(agent_id)
    except Exception as e:
        log(f"WARN agy_quota: reset_session({agent_id}, {fallback_to}) failed: {e!r}")


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
        if not model.strip():
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

        ok, usage_fraction, reset_dt = get_agy_quota(model)
        if not ok:
            _write_negative_cache(agent_id)
            return (False, "", False)
        _clear_negative_cache(agent_id)
        if usage_fraction < threshold / 100.0:
            return (False, "", False)

        cache_dir = _paths.AGY_QUOTA_CACHE_DIR
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
                "reason": f"agy quota {pct}% (>={threshold})",
            }
            fallback_cache.atomic_write_cache(_cache_path(agent_id), payload)
            return (True, fallback_to, True)
    except Exception as e:
        log(f"WARN agy_quota: should_fallback({agent_id}) exception: {e!r}")
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
        if not model.strip():
            warnings.append(
                f"WARN agy_quota: agent '{agent_id}' has fallback=true but model "
                f"is empty/unset; fallback will not engage (family判定不能)"
            )
        elif _model_family(model) == "unknown":
            warnings.append(
                f"WARN agy_quota: agent '{agent_id}' model '{model}' does not match "
                f"any known quota family (claude/gpt/gemini); fallback will use "
                f"all-model minimum quota (conservative)"
            )
        fb = entry.get("fallback_backend", "")
        if fb == "":
            continue
        if not isinstance(fb, str) or fb not in _VALID_FALLBACK_BACKENDS:
            warnings.append(
                f"WARN agy_quota: agent '{agent_id}' has invalid fallback_backend "
                f"'{fb}'; expected one of {sorted(_VALID_FALLBACK_BACKENDS)}"
            )
        threshold = entry.get("usage_threshold", 95)
        try:
            t = int(threshold)
        except (TypeError, ValueError):
            warnings.append(
                f"WARN agy_quota: agent '{agent_id}' has non-integer "
                f"usage_threshold '{threshold}'; defaulting to 95"
            )
            continue
        if t < 0 or t > 100:
            warnings.append(
                f"WARN agy_quota: agent '{agent_id}' usage_threshold {t} "
                f"out of range [0,100]; defaulting to 95"
            )
    return warnings
