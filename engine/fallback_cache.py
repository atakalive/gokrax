"""engine/fallback_cache.py - Generic fallback cache utilities.

Shared by gemini_quota.py and openai_codex_quota.py for atomic JSON cache
read/write, schema validation, and reset-time clamping.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


def read_cache(path: Path) -> dict | None:
    """Read JSON cache file. Return dict or None if missing/corrupt."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def atomic_write_cache(path: Path, payload: dict) -> None:
    """Atomically write JSON payload to ``path`` via tmp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def cache_active(
    cache: dict | None,
    *,
    validators: dict[str, Callable[[Any], bool]],
) -> bool:
    """Return True if cache represents an active (not yet expired) fallback.

    Always validates ``active`` (truthy) and ``until`` (ISO-8601 in future).
    Additional fields are validated via the ``validators`` mapping.
    """
    if not cache or not cache.get("active"):
        return False
    for key, check in validators.items():
        if not check(cache.get(key)):
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


def clamp_reset_time(
    reset_dt: datetime | None,
    *,
    default_hrs: int = 1,
    max_hrs: int = 48,
    min_mins: int = 5,
) -> datetime:
    """Clamp a reset datetime into a sane future window."""
    now = datetime.now(timezone.utc)
    if reset_dt is None:
        return now + timedelta(hours=default_hrs)
    if reset_dt > now + timedelta(hours=max_hrs):
        return now + timedelta(hours=default_hrs)
    if reset_dt < now:
        return now + timedelta(minutes=min_mins)
    return reset_dt
