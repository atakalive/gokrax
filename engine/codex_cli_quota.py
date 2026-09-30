"""engine/codex_cli_quota.py - codex CLI (ChatGPT) usage-based fallback to alt backend.

Queries ChatGPT's ``/wham/usage`` with the Codex CLI's own credentials
(``~/.codex/auth.json``, read-only; pi's auth is never used) and routes a
codex agent's sends to a configured fallback backend (pi/cc/openclaw) until
reset time. The firing rules are those of ``openai_codex_quota._evaluate``:
the weekly window fires at ``usage_threshold``, the 5-hour window only on
exhaustion, and the ``allowed``/``limit_reached`` flag on its own. A re-fire
after the previous period expired, with no no-fallback decision and no
session reset in between, keeps the fallback session
(``openai_codex_quota._is_continuation``).

A cache is honored only while config_codex.json still enables fallback to
the same backend. An active cache that no longer matches a readable config
is deleted at the next resolve, so disabling fallback or changing
``fallback_backend`` cannot be undone by reverting the config. While the
config cannot be read, an active cache is kept as is and every failed read
logs a WARN. No negative cache: auth or API failures are fail-open (the send
stays on codex).

Public surface:
    get_codex_cli_usage() -> CodexUsage
    resolve_fallback(agent_id) -> str       # cache + config read, no HTTP
    should_fallback(agent_id) -> tuple      # called from send() only
    settle_for_reset(agent_id) -> str       # called from reset_session() only
    validate_fallback_config() -> list[str] # startup validation

Cache file format (~/.gokrax/quota-cache-codex-cli/<agent_id>.json):
    {"active": true, "fallback_to": "pi", "until": "<ISO-8601>",
     "reason": "...", "clear_on_revert": true}
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

import config.paths as _paths
import engine.openai_codex_quota as _ocq
from engine import fallback_cache
from engine.openai_codex_quota import CodexUsage
from engine.shared import log

# Quota-aware backends (gemini/agy/kimi/codex) are excluded to prevent
# transitive fallback chains. Defined locally, consistent with the other
# quota modules.
_VALID_FALLBACK_BACKENDS = frozenset({"pi", "cc", "openclaw"})
_DEFAULT_THRESHOLD = 95


def get_codex_cli_usage() -> CodexUsage:
    """Fetch weekly and 5-hour usage with the Codex CLI's credentials only.

    Returns ``CodexUsage(ok=False)`` (fail-open) when the auth is missing or
    expired, or on any error. The token is never refreshed or written here.
    """
    try:
        creds = _ocq._load_codex_cli_auth()
        if creds is None:
            return CodexUsage(ok=False)
        access, account_id = creds
        return _ocq._fetch_codex_usage(access, account_id)
    except Exception as e:
        log(f"WARN codex_cli_quota: get_codex_cli_usage exception: {e!r}")
        return CodexUsage(ok=False)


def _cache_path(agent_id: str) -> Path:
    return _paths.CODEX_CLI_QUOTA_CACHE_DIR / f"{agent_id}.json"


def _cache_active(cache: dict | None) -> bool:
    """Check if a cache dict represents an active fallback period."""
    return fallback_cache.cache_active(
        cache,
        validators={
            "fallback_to": lambda v: isinstance(v, str) and v in _VALID_FALLBACK_BACKENDS,
        },
    )


def _configured_target(entry: object) -> str:
    """Return the fallback backend this agent's config enables, or "".

    "" when the entry is missing or not a dict, ``fallback`` is not truthy,
    or ``fallback_backend`` is not one of ``_VALID_FALLBACK_BACKENDS``.
    """
    if not isinstance(entry, dict) or not entry.get("fallback"):
        return ""
    fallback_to = entry.get("fallback_backend", "")
    if not isinstance(fallback_to, str) or fallback_to not in _VALID_FALLBACK_BACKENDS:
        return ""
    return fallback_to


def _cache_matches(cache: dict | None, fallback_to: str) -> bool:
    """True if ``cache`` is active and points at ``fallback_to`` (non-empty)."""
    return (
        bool(fallback_to)
        and _cache_active(cache)
        and cache.get("fallback_to") == fallback_to
    )


def _keep_target(agent_id: str, cfg_all: dict | None, cache: dict | None) -> str:
    """Return the fallback backend a cache must point at to be kept, or "".

    With a readable config (``cfg_all`` is a dict) this is the configured
    target. While the config cannot be read (``cfg_all`` is None), an active
    cache is kept as is, so a half-written file never ends a fallback
    period, and any other cache is not kept.
    """
    if cfg_all is None:
        return cache.get("fallback_to", "") if _cache_active(cache) else ""
    return _configured_target(cfg_all.get(agent_id))


def _threshold(raw: object) -> int | None:
    """Return ``raw`` if it is an int (not bool) in [0, 100], else None.

    Floats, numeric strings and bools are rejected, so JSON values such as
    0.95, 94.9, "90" or true never turn into 0, 94, 90 or 1.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw < 0 or raw > 100:
        return None
    return raw


def _read_agent_config() -> tuple[dict | None, str]:
    """Read config_codex.json fresh each time. Returns (config, failure reason).

    ({}, "") when the file does not exist (no agent has fallback enabled),
    (dict, "") when it holds a JSON object, and (None, <reason>) when it
    exists but cannot be read or parsed, or is not a JSON object.
    """
    try:
        with open(_paths.CODEX_AGENT_CONFIG, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return ({}, "")
    except (OSError, ValueError) as e:
        return (None, repr(e))
    if not isinstance(data, dict):
        return (None, f"not a JSON object (type={type(data).__name__})")
    return (data, "")


def _load_agent_config() -> dict | None:
    """``_read_agent_config`` for the runtime paths; logs a WARN on every failed read.

    None means "cannot be read": no cache is deleted on the strength of it.
    The WARN is not rate-limited, so a config that stays unreadable keeps
    showing up in the log: at every resolve of an agent with an active
    cache, every send decision and every session reset.
    """
    cfg, reason = _read_agent_config()
    if cfg is None:
        log(
            f"WARN codex_cli_quota: {_paths.CODEX_AGENT_CONFIG} unreadable ({reason}); "
            f"active fallbacks are kept and no new fallback is decided"
        )
    return cfg


def resolve_fallback(agent_id: str) -> str:
    """Return the active fallback backend name, or "".

    No HTTP. Reads the cache, and config_codex.json only while the cache is
    active. The cache is honored only if the config still enables fallback
    to the same backend; otherwise it is deleted (under the per-agent lock)
    so that reverting the config cannot revive it. While the config cannot
    be read, the active cache is honored and kept. Called from
    resolve_backend() hot path.
    """
    try:
        cache = fallback_cache.read_cache(_cache_path(agent_id))
        if not _cache_active(cache):
            return ""
        fallback_to = _keep_target(agent_id, _load_agent_config(), cache)
        if _cache_matches(cache, fallback_to):
            return fallback_to
        _clear_stale_cache(agent_id, cache, fallback_to)
        return ""
    except Exception:
        return ""


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
        log(f"WARN codex_cli_quota: reset_session({agent_id}, {fallback_to}) failed: {e!r}")


def _clear_stale_cache(agent_id: str, existing: dict | None, fallback_to: str) -> None:
    """Delete the cache unless, re-read under the lock, it is active for ``fallback_to``.

    ``fallback_to`` "" deletes any cache. Callers: every no-fallback
    decision in ``should_fallback`` (right before the send goes to codex),
    ``resolve_fallback`` when an active cache no longer matches the config,
    and ``settle_for_reset``. Deleting never skips a session reset; at worst
    the next fire starts a new period and resets its fallback backend once
    more.

    Guarantee (one direction only): a ``clear_on_revert`` cache still on
    disk was not followed by a send to codex through ``should_fallback`` nor
    by ``settle_for_reset``; ``_is_continuation`` reads that as "the fallback
    session has only seen this period's sends". Accepted exceptions, each
    able to skip at most one session reset: the delete fails,
    ``should_fallback`` hits an unexpected exception, or config_codex.json
    cannot be read at the decision (all logged; the send stays on codex), or
    a decision races with a concurrent activation (the cache is re-checked
    under the per-agent lock and a matching active one is never deleted,
    but this send still goes to codex).
    """
    if existing is None:
        return
    try:
        lock_path = _paths.CODEX_CLI_QUOTA_CACHE_DIR / f"{agent_id}.lock"
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            current = fallback_cache.read_cache(_cache_path(agent_id))
            if current is None or _cache_matches(current, fallback_to):
                return
            _cache_path(agent_id).unlink(missing_ok=True)
    except Exception as e:
        log(f"WARN codex_cli_quota: clear stale cache({agent_id}) failed: {e!r}")


def settle_for_reset(agent_id: str) -> str:
    """Settle the fallback state for engine.backend.reset_session().

    Returns the fallback backend whose session reset_session() must also
    reset: the target of an active cache that is kept (see ``_keep_target``),
    else "". The period of such a cache stays in force with a freshly reset
    fallback session. Every other cache (expired, or no longer matching the
    config) is deleted, so a later fire starts a new period and resets its
    fallback backend instead of continuing a session that holds context
    from before this reset.
    """
    try:
        existing = fallback_cache.read_cache(_cache_path(agent_id))
        if existing is None:
            return ""
        fallback_to = _keep_target(agent_id, _load_agent_config(), existing)
        if _cache_matches(existing, fallback_to):
            return fallback_to
        _clear_stale_cache(agent_id, existing, fallback_to)
        return ""
    except Exception as e:
        log(f"WARN codex_cli_quota: settle_for_reset({agent_id}) exception: {e!r}")
        return ""


def should_fallback(agent_id: str) -> tuple[bool, str, bool]:
    """Decide whether to fall back. Called only from engine.backend.send().

    Returns (active, fallback_to, new_period). ``new_period`` is True only
    when this call activated a new period and reset the fallback backend.
    DCL critical section: cache recheck -> reset_session -> cache write.
    """
    try:
        cfg_all = _load_agent_config()
        existing = fallback_cache.read_cache(_cache_path(agent_id))
        fallback_to = _keep_target(agent_id, cfg_all, existing)
        if _cache_matches(existing, fallback_to):
            return (True, fallback_to, False)
        if cfg_all is None:
            # Unreadable config (already logged): no config-based decision, delete nothing.
            return (False, "", False)
        if not fallback_to:
            # Fallback disabled or misconfigured: this send goes to codex.
            _clear_stale_cache(agent_id, existing, "")
            return (False, "", False)
        threshold = _threshold(cfg_all[agent_id].get("usage_threshold", _DEFAULT_THRESHOLD))
        if threshold is None:
            threshold = _DEFAULT_THRESHOLD

        usage = get_codex_cli_usage()
        if not usage.ok:
            _clear_stale_cache(agent_id, existing, fallback_to)
            return (False, "", False)
        reasons, candidates = _ocq._evaluate(usage, threshold)
        if not reasons:
            _clear_stale_cache(agent_id, existing, fallback_to)
            return (False, "", False)

        cache_dir = _paths.CODEX_CLI_QUOTA_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock_path = cache_dir / f"{agent_id}.lock"
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            existing = fallback_cache.read_cache(_cache_path(agent_id))
            if _cache_matches(existing, fallback_to):
                return (True, fallback_to, False)

            until = max(
                fallback_cache.clamp_reset_time(c, default_hrs=6, max_hrs=168)
                for c in (candidates or [None])
            )
            reason = "; ".join(reasons)
            renewed = _ocq._is_continuation(existing, {"fallback_to": fallback_to})
            if not renewed:
                _reset_fallback_backend(agent_id, fallback_to)
            payload = {
                "active": True,
                "fallback_to": fallback_to,
                "until": until.isoformat(),
                "reason": reason,
                "clear_on_revert": True,
            }
            fallback_cache.atomic_write_cache(_cache_path(agent_id), payload)
            verb = "renewed" if renewed else "activated"
            log(
                f"codex_cli_quota: fallback {verb} agent={agent_id} "
                f"fallback_to={fallback_to} reason={reason!r} until={until.isoformat()}"
            )
            return (True, fallback_to, not renewed)
    except Exception as e:
        log(f"WARN codex_cli_quota: should_fallback({agent_id}) exception: {e!r}")
        return (False, "", False)


def validate_fallback_config() -> list[str]:
    """Pure validation. Return list of WARN strings (caller logs)."""
    cfg, reason = _read_agent_config()
    if cfg is None:
        return [
            f"WARN codex_cli_quota: {_paths.CODEX_AGENT_CONFIG} cannot be read or is not "
            f"a JSON object ({reason}); active fallbacks are kept and new codex quota "
            f"fallbacks are not evaluated until it can be read"
        ]
    warnings: list[str] = []
    pi_cfg = _ocq._load_pi_config()
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
                f"WARN codex_cli_quota: agent '{agent_id}' has invalid fallback_backend "
                f"'{fb}'; expected one of {sorted(_VALID_FALLBACK_BACKENDS)}"
            )
        elif fb == "pi":
            pi_entry = pi_cfg.get(agent_id)
            if isinstance(pi_entry, dict) and pi_entry.get("provider") == "openai-codex":
                warnings.append(
                    f"WARN codex_cli_quota: agent '{agent_id}' falls back to pi, but its "
                    f"config_pi.json provider is 'openai-codex', which draws on the same "
                    f"ChatGPT quota; the fallback relieves it only if pi's own "
                    f"openai-codex fallback (Mode A) switches the provider"
                )
        raw = entry.get("usage_threshold", _DEFAULT_THRESHOLD)
        if _threshold(raw) is None:
            warnings.append(
                f"WARN codex_cli_quota: agent '{agent_id}' has invalid usage_threshold "
                f"{raw!r}; expected an integer in [0,100]; defaulting to {_DEFAULT_THRESHOLD}"
            )
    return warnings
