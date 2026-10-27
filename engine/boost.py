"""Boost mode: per-batch temporary upgrade of reviewer models (Issue #400).

Boost applies only when all three are set:
1. the batch flag ``data["boost"]`` (queue option ``boost`` / CLI ``--boost``)
2. the phase flag ``boost`` in REVIEW_MODES (resolved into ``review_config``)
3. the per-agent ``boost`` sub-dict in ``agents/config_<backend>.json``

This module is imported by backend_* modules, so keep module-level imports to
``config`` only. ``engine.fsm`` imports ``notify`` and must be imported lazily.
"""

from __future__ import annotations

import importlib

import config

BOOST_KEYS: dict[str, frozenset[str]] = {
    "pi": frozenset({"model", "thinking"}),
    "cc": frozenset({"model", "thinking", "effort"}),
    "cci": frozenset({"model", "thinking", "effort"}),
    "codex": frozenset({"model", "effort"}),
    "gemini": frozenset({"model"}),
    "kimi": frozenset({"model"}),
    "agy": frozenset({"model"}),
}
_BOOST_PHASES: tuple[str, ...] = ("design", "code")


def boost_applies(profile: dict, backend: str) -> bool:
    """True when profile["boost"] is a dict containing at least one allowed key for backend."""
    boost = profile.get("boost")
    if not isinstance(boost, dict):
        return False
    allowed = BOOST_KEYS.get(backend, frozenset())
    return any(k in allowed for k in boost)


def overlay_profile(profile: dict, backend: str) -> dict:
    """Return a new dict with the allowed boost keys shallowly overlaid on profile.

    - profile.get("boost") is not a dict → return profile itself (same object).
    - otherwise → copy profile without "boost" and overlay only the keys in
      BOOST_KEYS.get(backend). Disallowed keys are silently dropped (warnings are
      emitted by validate_boost_config).
    - The input profile (a _load_config() cache entry) is never mutated.
    """
    boost = profile.get("boost")
    if not isinstance(boost, dict):
        return profile
    allowed = BOOST_KEYS.get(backend, frozenset())
    result = {k: v for k, v in profile.items() if k != "boost"}
    for k, v in boost.items():
        if k in allowed:
            result[k] = v
    return result


def is_boost_active(data: dict, state: str | None) -> bool:
    """Whether reviewer sends for this batch in this state should be boosted."""
    if not state:
        return False
    if not data.get("boost"):
        return False
    phase = config.STATE_PHASE_MAP.get(state)
    if phase not in _BOOST_PHASES:
        return False
    try:
        from engine.fsm import get_phase_config

        return get_phase_config(data, phase).get("boost", False) is True
    except Exception:
        return False


def boost_phases_from_data(data: dict) -> list[str]:
    """Phases (in _BOOST_PHASES order) whose resolved phase config has boost=True.

    Does not look at data["boost"] (callers check it). Returns [] on error.
    """
    try:
        from engine.fsm import get_phase_config

        return [
            p for p in _BOOST_PHASES
            if get_phase_config(data, p).get("boost", False) is True
        ]
    except Exception:
        return []


def boost_phases_from_mode(mode_config: dict) -> list[str]:
    """Phases (in _BOOST_PHASES order) for which REVIEW_MODES mode_config enables boost."""
    from engine.fsm import _build_phase_config

    return [p for p in _BOOST_PHASES if _build_phase_config(mode_config, p)["boost"] is True]


def format_boost_label(phases: list[str] | None) -> str:
    """None → "boost[?]", [] → "boost[-]", otherwise "boost[design,code]"."""
    if phases is None:
        return "boost[?]"
    if not phases:
        return "boost[-]"
    return "boost[" + ",".join(phases) + "]"


def validate_boost_config() -> list[str]:
    """Validate the ``boost`` entries of each backend config. Return warning strings."""
    warnings: list[str] = []
    for backend, allowed in BOOST_KEYS.items():
        try:
            mod = importlib.import_module(f"engine.backend_{backend}")
            cfg = mod._load_config()
        except Exception as e:
            warnings.append(
                f"WARNING: boost: failed to load config for backend '{backend}': {e}"
            )
            continue
        if not isinstance(cfg, dict):
            continue
        for agent_id, entry in cfg.items():
            if agent_id.startswith("_"):
                continue
            if not isinstance(entry, dict):
                continue
            if "boost" not in entry:
                continue
            boost = entry["boost"]
            if not isinstance(boost, dict):
                warnings.append(
                    f"WARNING: boost: agent '{agent_id}' in config_{backend}.json has "
                    f"non-dict boost ({type(boost).__name__}); ignored"
                )
                continue
            unknown = sorted(k for k in boost if k not in allowed)
            if unknown:
                warnings.append(
                    f"WARNING: boost: agent '{agent_id}' in config_{backend}.json has "
                    f"unsupported boost keys {unknown}; ignored "
                    f"(allowed: {sorted(allowed)})"
                )
    return warnings
