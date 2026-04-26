"""Tests for engine/fallback_cache.py — generic fallback cache helpers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from engine import fallback_cache as fc


def test_read_cache_missing(tmp_path):
    assert fc.read_cache(tmp_path / "nope.json") is None


def test_read_cache_corrupt(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    assert fc.read_cache(p) is None


def test_read_cache_non_dict(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[1,2,3]")
    assert fc.read_cache(p) is None


def test_atomic_write_and_read_roundtrip(tmp_path):
    p = tmp_path / "sub" / "c.json"
    payload = {"active": True, "until": "2099-01-01T00:00:00+00:00", "x": 1}
    fc.atomic_write_cache(p, payload)
    assert p.exists()
    assert fc.read_cache(p) == payload


def test_atomic_write_no_tmp_leftover(tmp_path):
    p = tmp_path / "c.json"
    fc.atomic_write_cache(p, {"a": 1})
    files = sorted([f.name for f in tmp_path.iterdir()])
    assert files == ["c.json"]


def test_cache_active_truthy_with_validators():
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    cache = {"active": True, "until": until, "fallback_to": "pi"}
    assert fc.cache_active(
        cache,
        validators={"fallback_to": lambda v: isinstance(v, str) and v in {"pi", "cc"}},
    ) is True


def test_cache_active_validator_rejects():
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    cache = {"active": True, "until": until, "fallback_to": "bogus"}
    assert fc.cache_active(
        cache,
        validators={"fallback_to": lambda v: isinstance(v, str) and v in {"pi", "cc"}},
    ) is False


def test_cache_active_none():
    assert fc.cache_active(None, validators={}) is False


def test_cache_active_inactive_flag():
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert fc.cache_active({"active": False, "until": until}, validators={}) is False


def test_cache_active_expired():
    until = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert fc.cache_active({"active": True, "until": until}, validators={}) is False


def test_cache_active_invalid_until():
    assert fc.cache_active({"active": True, "until": "not-a-date"}, validators={}) is False


def test_cache_active_until_z_suffix():
    """Trailing Z (Zulu) should be accepted."""
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    until = future.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert fc.cache_active({"active": True, "until": until}, validators={}) is True


def test_clamp_reset_time_none_uses_default():
    out = fc.clamp_reset_time(None, default_hrs=6)
    delta = out - datetime.now(timezone.utc)
    assert timedelta(hours=5, minutes=59) < delta < timedelta(hours=6, minutes=1)


def test_clamp_reset_time_far_future_uses_default():
    far = datetime.now(timezone.utc) + timedelta(days=30)
    out = fc.clamp_reset_time(far, default_hrs=6, max_hrs=168)
    delta = out - datetime.now(timezone.utc)
    # 30 days exceeds 168h max → falls back to default_hrs (6h)
    assert delta < timedelta(hours=7)


def test_clamp_reset_time_within_max_passes_through():
    """Weekly reset (e.g. 6 days) within max_hrs=168 should pass through."""
    six_days = datetime.now(timezone.utc) + timedelta(days=6)
    out = fc.clamp_reset_time(six_days, default_hrs=6, max_hrs=168)
    assert out == six_days


def test_clamp_reset_time_past_uses_min_mins():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    out = fc.clamp_reset_time(past, min_mins=5)
    delta = out - datetime.now(timezone.utc)
    assert timedelta(minutes=4) < delta < timedelta(minutes=6)


def test_atomic_write_payload_is_json_loadable(tmp_path):
    p = tmp_path / "c.json"
    fc.atomic_write_cache(p, {"a": [1, 2, 3], "b": "x"})
    assert json.loads(p.read_text()) == {"a": [1, 2, 3], "b": "x"}
