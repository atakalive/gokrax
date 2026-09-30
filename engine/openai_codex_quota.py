"""engine/openai_codex_quota.py - openai-codex usage-based fallback for PI backend.

Reuses pi (or codex CLI) OAuth credentials to query
``https://chatgpt.com/backend-api/wham/usage`` and routes a PI agent's
``--model`` argument from openai-codex to a configured fallback
provider/model when the weekly window reaches ``usage_threshold`` or the
5-hour window is exhausted (100% used, or ``allowed=false`` /
``limit_reached=true``). The 5-hour window is never compared against the
threshold, so it does not flip back and forth near the threshold.

Public surface:
    CodexUsage (frozen dataclass)
    get_codex_usage() -> CodexUsage
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config.paths as _paths
from engine import fallback_cache
from engine.shared import log

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_HTTP_TIMEOUT = 10
_WEEKLY_KEYS = ("secondary_window", "weekly", "secondary")
_PRIMARY_KEYS = ("primary_window", "five_hour", "primary")
# Valid Mode B (backend redirect) targets. Quota-aware backends (gemini/agy/kimi/codex)
# are intentionally excluded to prevent transitive loops (e.g. pi→agy→pi).
# Defined locally (not imported from backend_pi) to avoid circular import,
# consistent with gemini/agy/kimi quota modules.
_VALID_FALLBACK_BACKENDS = frozenset({"pi", "cc", "openclaw"})


@dataclass(frozen=True)
class CodexUsage:
    """Parsed /wham/usage snapshot.

    ``weekly_*`` / ``primary_*`` (5-hour window) are None when the window is
    absent or unparseable. ``limit_reached`` is True iff the API reports
    ``allowed is False`` or ``limit_reached is True`` (strict identity).
    ``ok`` is False only for fail-open (no usable window and no flag).
    """

    ok: bool
    weekly_used: float | None = None
    weekly_reset: datetime | None = None
    primary_used: float | None = None
    primary_reset: datetime | None = None
    limit_reached: bool = False


_FAIL_OPEN = CodexUsage(ok=False)


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


def _load_pi_codex_auth() -> tuple[str, str] | None:
    """Read access_token + account_id from pi auth only.

    ``~/.pi/agent/auth.json`` -> openai-codex entry (expires in ms).
    Returns (access_token, account_id) or None on any failure / expired token.
    """
    try:
        with open(_paths.PI_AUTH_FILE, "r") as f:
            pi_auth = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(pi_auth, dict):
        return None
    entry = pi_auth.get("openai-codex")
    if not isinstance(entry, dict):
        return None
    access = entry.get("access")
    account_id = entry.get("accountId")
    expires = entry.get("expires")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(account_id, str) or not account_id:
        return None
    if isinstance(expires, (int, float)) and expires / 1000.0 < time.time():
        return None
    return (access, account_id)


def _load_codex_cli_auth() -> tuple[str, str] | None:
    """Read access_token + account_id from the Codex CLI auth only.

    ``~/.codex/auth.json`` -> tokens.{access_token, account_id} (expires_at
    in sec, checked only when present). Never falls back to pi auth.
    Returns (access_token, account_id) or None on any failure / expired token.
    """
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


def _load_codex_auth() -> tuple[str, str] | None:
    """Read access_token + account_id with 2-stage fallback.

    1. ~/.pi/agent/auth.json -> openai-codex entry (``_load_pi_codex_auth``)
    2. ~/.codex/auth.json -> tokens (``_load_codex_cli_auth``)

    Returns (access_token, account_id) or None on any failure / expired token.
    """
    creds = _load_pi_codex_auth()
    if creds is not None:
        return creds
    return _load_codex_cli_auth()


def _parse_reset(wk: dict) -> datetime | None:
    """Parse a usage window's reset time from the window dict.

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


def _to_percent(raw: object) -> float | None:
    """float(raw) clamped to [0, 100]; None for bool, non-numeric or non-finite."""
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    return max(0.0, min(100.0, value))


def _parse_window(wk: object, label: str) -> tuple[float, datetime | None] | None:
    """Parse one usage window into (used_percent, reset_dt).

    ``used_percent`` wins over ``percent_left`` / ``remaining_percent``; the
    value is clamped to [0, 100]. A positive remaining percent always maps
    to a used percent strictly below 100.0. Returns None (after a WARN
    naming ``label``) when the window is not a dict or has no usable percent.
    """
    if not isinstance(wk, dict):
        log(f"WARN openai_codex_quota: {label} window: not a dict; type={type(wk).__name__}")
        return None
    used_raw = wk.get("used_percent")
    if used_raw is not None:
        used_percent = _to_percent(used_raw)
        if used_percent is None:
            log(
                f"WARN openai_codex_quota: {label} window: unusable used_percent={used_raw!r}; "
                f"window_keys={sorted(wk.keys())[:20]}"
            )
            return None
    else:
        left_raw = wk.get("percent_left", wk.get("remaining_percent"))
        left = _to_percent(left_raw)
        if left is None:
            log(
                f"WARN openai_codex_quota: {label} window: no usable percent; "
                f"window_keys={sorted(wk.keys())[:20]} percent_left={left_raw!r}"
            )
            return None
        used_percent = 100.0 - left
        if left > 0.0 and used_percent >= 100.0:
            # 100.0 - 1e-15 rounds to 100.0 in double precision; some quota
            # left must never read as exhausted.
            used_percent = math.nextafter(100.0, 0.0)
    return (used_percent, _parse_reset(wk))


def get_codex_usage() -> CodexUsage:
    """Fetch openai-codex weekly and 5-hour window usage.

    Credentials come from ``_load_codex_auth`` (pi first, then Codex CLI).
    Returns ``_FAIL_OPEN`` on any error; see ``_fetch_codex_usage``.
    """
    try:
        creds = _load_codex_auth()
        if creds is None:
            return _FAIL_OPEN
        access, account_id = creds
        return _fetch_codex_usage(access, account_id)
    except Exception as e:
        log(f"WARN openai_codex_quota: get_codex_usage exception: {e!r}")
        return _FAIL_OPEN


def _fetch_codex_usage(access: str, account_id: str) -> CodexUsage:
    """GET /wham/usage with the given credentials and parse both windows.

    Each window is parsed independently: an unparseable window becomes None
    (with a WARN) and the other window and the limit flag are still
    evaluated. Returns ``_FAIL_OPEN`` on HTTP failure or when no usable
    window and no limit flag is present. Other unexpected exceptions
    propagate; callers wrap this in ``try/except Exception``.
    """
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
        return _FAIL_OPEN
    if not isinstance(payload, dict):
        log(f"WARN openai_codex_quota: payload not dict; type={type(payload).__name__}")
        return _FAIL_OPEN
    rl = payload.get("rate_limit") or payload.get("rate_limits") or {}
    if not isinstance(rl, dict):
        log(f"WARN openai_codex_quota: rate_limit invalid; type={type(rl).__name__}")
        return _FAIL_OPEN
    weekly_raw = next((rl[k] for k in _WEEKLY_KEYS if rl.get(k) is not None), None)
    primary_raw = next((rl[k] for k in _PRIMARY_KEYS if rl.get(k) is not None), None)
    weekly = _parse_window(weekly_raw, "weekly") if weekly_raw is not None else None
    primary = _parse_window(primary_raw, "5h") if primary_raw is not None else None
    limit_reached = rl.get("allowed") is False or rl.get("limit_reached") is True

    if weekly is None and primary is None and not limit_reached:
        if weekly_raw is None and primary_raw is None:
            log(
                f"WARN openai_codex_quota: no usage window found; "
                f"rate_limit_keys={sorted(rl.keys())[:20]}"
            )
        return _FAIL_OPEN

    return CodexUsage(
        ok=True,
        weekly_used=weekly[0] if weekly is not None else None,
        weekly_reset=weekly[1] if weekly is not None else None,
        primary_used=primary[0] if primary is not None else None,
        primary_reset=primary[1] if primary is not None else None,
        limit_reached=limit_reached,
    )


def _evaluate(usage: CodexUsage, threshold: int) -> tuple[list[str], list[datetime | None]]:
    """Pure fallback decision. Returns (reasons, reset candidates).

    - weekly fires at ``weekly_used >= threshold``.
    - 5h fires only on exhaustion (``primary_used >= 100.0``); it is never
      compared against ``threshold``.
    - the limit flag fires on its own. It is a global value that does not
      say which window refused, so it adds ``primary_reset`` (None allowed)
      as a short re-probe period, and only when no window fired, so a None
      candidate (now+6h after clamping) cannot outvote a real window reset.
      A re-fire after that period keeps the session (``_is_continuation``).

    Invariant: reasons non-empty => candidates non-empty.
    """
    reasons: list[str] = []
    candidates: list[datetime | None] = []
    weekly_hit = usage.weekly_used is not None and usage.weekly_used >= threshold
    primary_hit = usage.primary_used is not None and usage.primary_used >= 100.0
    if weekly_hit:
        reasons.append(f"Codex usage {int(round(usage.weekly_used))}% (>={threshold})")
        candidates.append(usage.weekly_reset)
    if primary_hit:
        reasons.append(f"Codex 5h window exhausted ({int(round(usage.primary_used))}%)")
        candidates.append(usage.primary_reset)
    if usage.limit_reached:
        reasons.append("Codex limit reached (allowed/limit_reached flag)")
        if not (weekly_hit or primary_hit):
            candidates.append(usage.primary_reset)
    return reasons, candidates


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


def _clear_stale_cache(agent_id: str, existing: dict | None) -> None:
    """Delete an inactive cache once a no-fallback decision is made.

    Called on every no-fallback decision taken after the cache check, i.e.
    right before the send goes to the original provider. Guarantee (one
    direction only): a ``clear_on_revert`` cache still on disk was never seen
    as inactive by a later no-fallback decision. ``_is_continuation`` reads
    that as "the original provider has not been used since". Accepted
    exceptions, each able to skip at most one session reset: the delete
    fails (logged; the decision stays "no fallback"), or a decision races
    with a concurrent activation (the cache is re-checked under the
    per-agent lock and an active one is never deleted, but this send still
    goes to the original provider).
    """
    if existing is None:
        return
    try:
        lock_path = _paths.OPENAI_CODEX_QUOTA_CACHE_DIR / f"{agent_id}.lock"
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            current = fallback_cache.read_cache(_cache_path(agent_id))
            if current is None or _cache_active(current) or _cache_active_mode_b(current):
                return
            _cache_path(agent_id).unlink(missing_ok=True)
    except Exception as e:
        log(f"WARN openai_codex_quota: clear stale cache({agent_id}) failed: {e!r}")


def _is_continuation(existing: dict | None, target: dict[str, str]) -> bool:
    """True if ``existing`` is an inactive period of this module for the same target.

    ``clear_on_revert`` marks caches written under the ``_clear_stale_cache``
    guarantee (caches from older versions lack it and never count). Such a
    cache still on disk is taken to mean the fallback session is still the
    live one, so a re-fire does not reset it again (best effort; see the
    exceptions in ``_clear_stale_cache``).
    """
    return (
        isinstance(existing, dict)
        and existing.get("active") is True
        and existing.get("clear_on_revert") is True
        and all(existing.get(k) == v for k, v in target.items())
    )


def should_fallback(agent_id: str) -> tuple[bool, str, str, bool]:
    """Decide whether to fall back.

    Mode A: called from backend_pi.send() — swaps provider/model within pi.
    Mode B: called from engine.backend.send() — redirects to another backend.

    Fires when the weekly window reaches ``usage_threshold`` or the 5-hour
    window is exhausted (see ``_evaluate``). The cache ``until`` is the
    latest clamped reset among the windows that fired. A re-fire after the
    previous period expired, with no no-fallback decision in between, keeps
    the session instead of resetting it again (``new_period`` is False).

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

        usage = get_codex_usage()
        if not usage.ok:
            _clear_stale_cache(agent_id, existing)
            return (False, "", "", False)
        reasons, candidates = _evaluate(usage, threshold)
        if not reasons:
            _clear_stale_cache(agent_id, existing)
            return (False, "", "", False)

        cache_dir = _paths.OPENAI_CODEX_QUOTA_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock_path = cache_dir / f"{agent_id}.lock"
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            existing = fallback_cache.read_cache(_cache_path(agent_id))

            until = max(
                fallback_cache.clamp_reset_time(c, default_hrs=6, max_hrs=168)
                for c in (candidates or [None])
            )
            reason = "; ".join(reasons)

            if mode_b:
                if _cache_active_mode_b(existing):
                    return (True, existing["fallback_backend"], "", False)
                renewed = _is_continuation(existing, {"fallback_backend": fb_backend})
                if not renewed:
                    # Reset the backend that will take over (the redirect target).
                    _reset_target_backend(agent_id, fb_backend)
                payload = {
                    "active": True,
                    "fallback_backend": fb_backend,
                    "until": until.isoformat(),
                    "reason": reason,
                    "clear_on_revert": True,
                }
                fallback_cache.atomic_write_cache(_cache_path(agent_id), payload)
                verb = "renewed" if renewed else "activated"
                log(
                    f"openai_codex_quota: fallback {verb} agent={agent_id} mode=B "
                    f"reason={reason!r} until={until.isoformat()}"
                )
                return (True, fb_backend, "", not renewed)

            if _cache_active(existing):
                return (True, existing["fallback_provider"], existing["fallback_model"], False)
            renewed = _is_continuation(
                existing, {"fallback_provider": fb_provider, "fallback_model": fb_model},
            )
            if not renewed:
                # Mode A: reset pi itself (which keeps handling the work).
                _reset_target_backend(agent_id, "pi")
            payload = {
                "active": True,
                "fallback_provider": fb_provider,
                "fallback_model": fb_model,
                "until": until.isoformat(),
                "reason": reason,
                "clear_on_revert": True,
            }
            fallback_cache.atomic_write_cache(_cache_path(agent_id), payload)
            verb = "renewed" if renewed else "activated"
            log(
                f"openai_codex_quota: fallback {verb} agent={agent_id} mode=A "
                f"reason={reason!r} until={until.isoformat()}"
            )
            return (True, fb_provider, fb_model, not renewed)
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
