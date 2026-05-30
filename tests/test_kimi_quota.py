"""Tests for engine/kimi_quota.py — kimi proactive REST usage-based fallback."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import config.paths as _paths
from engine import kimi_quota as kq


@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    """Redirect all kimi paths to tmp_path."""
    creds = tmp_path / "kimi-code.json"
    cache_dir = tmp_path / "quota-cache-kimi"
    agent_cfg = tmp_path / "config_kimi.json"
    monkeypatch.setattr(_paths, "KIMI_OAUTH_CREDS", creds)
    monkeypatch.setattr(_paths, "KIMI_QUOTA_CACHE_DIR", cache_dir)
    monkeypatch.setattr(_paths, "KIMI_AGENT_CONFIG", agent_cfg)
    return {
        "creds": creds,
        "cache_dir": cache_dir,
        "agent_cfg": agent_cfg,
        "tmp_path": tmp_path,
    }


def _write_creds(path, *, access="eyJhbG.test", expires_at=None):
    if expires_at is None:
        expires_at = time.time() + 3600  # 1h from now
    path.write_text(json.dumps({
        "access_token": access,
        "refresh_token": "refresh-test",
        "expires_at": expires_at,
        "scope": "test",
        "token_type": "Bearer",
    }))


def _mk_response(data):
    """Build a fake urlopen context manager."""
    body = json.dumps(data).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def _usages_payload(*, weekly_limit="100", weekly_used="76",
                    rolling_limit="100", rolling_used="1",
                    reset_iso="2099-01-01T00:00:00Z",
                    include_weekly=True, include_rolling=True,
                    extra_limits=None):
    payload: dict = {}
    if include_weekly:
        payload["usage"] = {
            "limit": weekly_limit,
            "used": weekly_used,
            "remaining": str(int(weekly_limit) - int(weekly_used)),
            "resetTime": reset_iso,
        }
    limits = []
    if include_rolling:
        limits.append({
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {
                "limit": rolling_limit,
                "used": rolling_used,
                "remaining": str(int(rolling_limit) - int(rolling_used)),
                "resetTime": reset_iso,
            },
        })
    if extra_limits:
        limits.extend(extra_limits)
    payload["limits"] = limits
    return payload


# ---------------------------------------------------------------------------
# _load_token
# ---------------------------------------------------------------------------

class TestLoadToken:
    def test_normal(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        assert kq._load_token() == "eyJhbG.test"

    def test_missing_file(self, tmp_paths):
        assert kq._load_token() is None

    def test_invalid_json(self, tmp_paths):
        tmp_paths["creds"].write_text("not json {{{")
        assert kq._load_token() is None

    def test_not_dict(self, tmp_paths):
        tmp_paths["creds"].write_text(json.dumps(["a", "b"]))
        assert kq._load_token() is None

    def test_missing_access_token(self, tmp_paths):
        tmp_paths["creds"].write_text(json.dumps({"refresh_token": "r"}))
        assert kq._load_token() is None

    def test_empty_access_token(self, tmp_paths):
        tmp_paths["creds"].write_text(json.dumps({"access_token": ""}))
        assert kq._load_token() is None

    def test_expired_fail_open(self, tmp_paths):
        _write_creds(tmp_paths["creds"], expires_at=time.time() - 100)
        assert kq._load_token() is None

    def test_future_expiry_ok(self, tmp_paths):
        _write_creds(tmp_paths["creds"], expires_at=time.time() + 3600)
        assert kq._load_token() == "eyJhbG.test"

    def test_no_expires_at_ok(self, tmp_paths):
        tmp_paths["creds"].write_text(json.dumps({"access_token": "tok"}))
        assert kq._load_token() == "tok"


# ---------------------------------------------------------------------------
# _parse_window
# ---------------------------------------------------------------------------

class TestParseWindow:
    def test_used_path(self):
        assert kq._parse_window({"limit": "100", "used": "76"}) == pytest.approx(0.76)

    def test_remaining_fallthrough(self):
        # used absent -> use remaining
        assert kq._parse_window({"limit": "100", "remaining": "24"}) == pytest.approx(0.76)

    def test_used_empty_fallthrough(self):
        # used empty string -> fall through to remaining
        assert kq._parse_window({"limit": "100", "used": "", "remaining": "24"}) == pytest.approx(0.76)

    def test_used_over_limit_clamped(self):
        assert kq._parse_window({"limit": "100", "used": "150"}) == pytest.approx(1.0)

    def test_remaining_over_limit_clamped(self):
        # remaining > limit -> used negative -> clamp to 0.0
        assert kq._parse_window({"limit": "100", "remaining": "150"}) == pytest.approx(0.0)

    def test_zero_limit_none(self):
        assert kq._parse_window({"limit": "0", "used": "1"}) is None

    def test_missing_limit_none(self):
        assert kq._parse_window({"used": "1"}) is None

    def test_both_invalid_none(self):
        assert kq._parse_window({"limit": "100", "used": "bad", "remaining": "bad"}) is None

    def test_no_used_no_remaining_none(self):
        assert kq._parse_window({"limit": "100"}) is None


# ---------------------------------------------------------------------------
# get_kimi_quota
# ---------------------------------------------------------------------------

class TestGetKimiQuota:
    def test_normal_both_windows(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="76", rolling_used="1")
        )):
            ok, frac, dt = kq.get_kimi_quota()
        assert ok is True and frac == pytest.approx(0.76)  # weekly more exhausted
        assert dt is not None and dt.tzinfo is not None

    def test_rolling_more_exhausted(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="10", rolling_used="99")
        )):
            ok, frac, _ = kq.get_kimi_quota()
        assert ok is True and frac == pytest.approx(0.99)

    def test_weekly_only(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="80", include_rolling=False)
        )):
            ok, frac, _ = kq.get_kimi_quota()
        assert ok is True and frac == pytest.approx(0.80)

    def test_rolling_only(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(rolling_used="42", include_weekly=False)
        )):
            ok, frac, _ = kq.get_kimi_quota()
        assert ok is True and frac == pytest.approx(0.42)

    def test_zero_limit_skipped(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        payload = {
            "usage": {"limit": "0", "used": "0"},
            "limits": [{
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {"limit": "100", "used": "30"},
            }],
        }
        with patch("urllib.request.urlopen", return_value=_mk_response(payload)):
            ok, frac, _ = kq.get_kimi_quota()
        assert ok is True and frac == pytest.approx(0.30)  # weekly skipped

    def test_used_missing_uses_remaining(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        payload = {
            "usage": {"limit": "100", "remaining": "24", "resetTime": "2099-01-01T00:00:00Z"},
            "limits": [],
        }
        with patch("urllib.request.urlopen", return_value=_mk_response(payload)):
            ok, frac, _ = kq.get_kimi_quota()
        assert ok is True and frac == pytest.approx(0.76)

    def test_used_over_limit_clamped(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        payload = {"usage": {"limit": "100", "used": "150"}, "limits": []}
        with patch("urllib.request.urlopen", return_value=_mk_response(payload)):
            ok, frac, _ = kq.get_kimi_quota()
        assert ok is True and frac == pytest.approx(1.0)

    def test_unknown_window_filtered(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        # 300-min (known, 5h) used=10 ; 60-min (unknown) used=99 -> 60 ignored
        extra = [{
            "window": {"duration": 60, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {"limit": "100", "used": "99"},
        }]
        payload = _usages_payload(
            weekly_used="5", rolling_used="10",
            extra_limits=extra,
        )
        with patch("urllib.request.urlopen", return_value=_mk_response(payload)):
            ok, frac, _ = kq.get_kimi_quota()
        # weekly=0.05, rolling 5h=0.10; unknown 60-min=0.99 ignored -> max is 0.10
        assert ok is True and frac == pytest.approx(0.10)

    def test_reset_time_parsed(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="76", reset_iso="2099-06-01T12:00:00Z")
        )):
            ok, _, dt = kq.get_kimi_quota()
        assert ok is True and dt.year == 2099 and dt.tzinfo is not None

    def test_no_windows(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        with patch("urllib.request.urlopen", return_value=_mk_response({"limits": []})):
            assert kq.get_kimi_quota() == (False, 0.0, None)

    def test_token_expired_no_http(self, tmp_paths):
        _write_creds(tmp_paths["creds"], expires_at=time.time() - 100)
        with patch("urllib.request.urlopen", side_effect=Exception("boom")) as mock_urlopen:
            assert kq.get_kimi_quota() == (False, 0.0, None)
        mock_urlopen.assert_not_called()

    def test_http_error(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert kq.get_kimi_quota() == (False, 0.0, None)

    def test_non_dict_response(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        with patch("urllib.request.urlopen", return_value=_mk_response(["a", "b"])):
            assert kq.get_kimi_quota() == (False, 0.0, None)

    def test_base_url_override(self, tmp_paths, monkeypatch):
        _write_creds(tmp_paths["creds"])
        monkeypatch.setenv("KIMI_CODE_BASE_URL", "https://example.test/coding/v1/")
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="76", include_rolling=False)
        )) as mock_urlopen:
            ok, _, _ = kq.get_kimi_quota()
            req = mock_urlopen.call_args[0][0]
        assert ok is True
        assert req.full_url == "https://example.test/coding/v1/usages"


# ---------------------------------------------------------------------------
# Negative cache
# ---------------------------------------------------------------------------

class TestNegativeCache:
    def _write_cfg(self, tmp_paths, **fields):
        base = {
            "fallback": True,
            "fallback_backend": "pi",
            "usage_threshold": 95,
        }
        base.update(fields)
        tmp_paths["agent_cfg"].write_text(json.dumps({"a1": base}))

    def test_written_on_api_failure(self, tmp_paths):
        self._write_cfg(tmp_paths)
        _write_creds(tmp_paths["creds"])
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            kq.should_fallback("a1")
        neg = kq._negative_cache_path("a1")
        assert neg.exists()
        data = json.loads(neg.read_text())
        assert "token_mtime" in data

    def test_active_skips_http(self, tmp_paths):
        self._write_cfg(tmp_paths)
        _write_creds(tmp_paths["creds"])
        kq._write_negative_cache("a1")
        with patch("urllib.request.urlopen", side_effect=Exception("should not be called")) as mock_urlopen:
            assert kq.should_fallback("a1") == (False, "", False)
        mock_urlopen.assert_not_called()

    def test_expired_inactive(self, tmp_paths):
        cd = tmp_paths["cache_dir"]
        cd.mkdir(parents=True, exist_ok=True)
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        kq._negative_cache_path("a1").write_text(json.dumps({"until": past}))
        assert kq._negative_cache_active("a1") is False

    def test_corrupt_inactive(self, tmp_paths):
        cd = tmp_paths["cache_dir"]
        cd.mkdir(parents=True, exist_ok=True)
        kq._negative_cache_path("a1").write_text("not json")
        assert kq._negative_cache_active("a1") is False

    def test_token_mtime_change_invalidates(self, tmp_paths):
        _write_creds(tmp_paths["creds"])
        kq._write_negative_cache("a1")
        assert kq._negative_cache_active("a1") is True
        _write_creds(tmp_paths["creds"], access="eyJhbG.refreshed")
        import os
        st = tmp_paths["creds"].stat()
        os.utime(tmp_paths["creds"], (st.st_atime + 5, st.st_mtime + 5))
        assert kq._negative_cache_active("a1") is False

    def test_token_missing_no_mtime(self, tmp_paths):
        kq._write_negative_cache("a1")
        data = json.loads(kq._negative_cache_path("a1").read_text())
        assert "token_mtime" not in data
        assert kq._negative_cache_active("a1") is True


# ---------------------------------------------------------------------------
# resolve_fallback
# ---------------------------------------------------------------------------

class TestResolveFallback:
    def _write_cache(self, tmp_paths, agent_id, payload):
        cd = tmp_paths["cache_dir"]
        cd.mkdir(parents=True, exist_ok=True)
        (cd / f"{agent_id}.json").write_text(json.dumps(payload))

    def test_active_cache(self, tmp_paths):
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self._write_cache(tmp_paths, "a1", {"active": True, "fallback_to": "pi", "until": until})
        assert kq.resolve_fallback("a1") == "pi"

    def test_expired_cache(self, tmp_paths):
        until = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self._write_cache(tmp_paths, "a1", {"active": True, "fallback_to": "pi", "until": until})
        assert kq.resolve_fallback("a1") == ""

    def test_missing_cache(self, tmp_paths):
        assert kq.resolve_fallback("ghost") == ""

    def test_corrupt_cache(self, tmp_paths):
        cd = tmp_paths["cache_dir"]
        cd.mkdir(parents=True)
        (cd / "a1.json").write_text("not json {{{")
        assert kq.resolve_fallback("a1") == ""

    def test_invalid_fallback_to_miss(self, tmp_paths):
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self._write_cache(tmp_paths, "a1", {"active": True, "fallback_to": "bogus", "until": until})
        assert kq.resolve_fallback("a1") == ""


# ---------------------------------------------------------------------------
# should_fallback
# ---------------------------------------------------------------------------

class TestShouldFallback:
    def _write_cfg(self, tmp_paths, **fields):
        base = {
            "fallback": True,
            "fallback_backend": "pi",
            "usage_threshold": 95,
        }
        base.update(fields)
        tmp_paths["agent_cfg"].write_text(json.dumps({"a1": base}))
        _write_creds(tmp_paths["creds"])

    def test_fallback_disabled(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback=False)
        assert kq.should_fallback("a1") == (False, "", False)

    def test_empty_fallback_backend(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback_backend="")
        assert kq.should_fallback("a1") == (False, "", False)

    def test_invalid_fallback_backend(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback_backend="bogus")
        assert kq.should_fallback("a1") == (False, "", False)

    def test_gemini_fallback_backend_rejected(self, tmp_paths):
        # quota-aware backends excluded from _VALID_FALLBACK_BACKENDS
        self._write_cfg(tmp_paths, fallback_backend="gemini")
        assert kq.should_fallback("a1") == (False, "", False)

    def test_agy_fallback_backend_rejected(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback_backend="agy")
        assert kq.should_fallback("a1") == (False, "", False)

    def test_threshold_exceeded_creates_cache(self, tmp_paths):
        self._write_cfg(tmp_paths)
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="96", include_rolling=False)
        )), patch("engine.backend_pi.reset_session") as mock_reset:
            active, fb, new = kq.should_fallback("a1")
        assert active is True and fb == "pi" and new is True
        mock_reset.assert_called_once_with("a1")
        cache = json.loads((tmp_paths["cache_dir"] / "a1.json").read_text())
        assert cache["active"] is True and cache["fallback_to"] == "pi"
        assert "kimi quota" in cache["reason"]

    def test_below_threshold(self, tmp_paths):
        self._write_cfg(tmp_paths)
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="50", include_rolling=False)
        )):
            assert kq.should_fallback("a1") == (False, "", False)

    def test_boundary_at_threshold(self, tmp_paths):
        self._write_cfg(tmp_paths, usage_threshold=95)
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="95", include_rolling=False)
        )), patch("engine.backend_pi.reset_session"):
            active, fb, _ = kq.should_fallback("a1")
        assert active is True and fb == "pi"

    def test_boundary_under_threshold(self, tmp_paths):
        self._write_cfg(tmp_paths, usage_threshold=95)
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="94", include_rolling=False)
        )):
            active, _, _ = kq.should_fallback("a1")
        assert active is False

    def test_token_expired_no_urlopen(self, tmp_paths):
        self._write_cfg(tmp_paths)
        _write_creds(tmp_paths["creds"], expires_at=time.time() - 100)
        with patch("urllib.request.urlopen", side_effect=Exception("boom")) as mock_urlopen:
            assert kq.should_fallback("a1") == (False, "", False)
        mock_urlopen.assert_not_called()
        assert kq._negative_cache_path("a1").exists()

    def test_cc_fallback_backend(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback_backend="cc")
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="96", include_rolling=False)
        )), patch("engine.backend_cc.reset_session") as mock_reset:
            active, fb, _ = kq.should_fallback("a1")
        assert active is True and fb == "cc"
        mock_reset.assert_called_once_with("a1")

    def test_openclaw_fallback_backend_no_reset(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback_backend="openclaw")
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="96", include_rolling=False)
        )):
            active, fb, _ = kq.should_fallback("a1")
        assert active is True and fb == "openclaw"

    def test_dcl_existing_cache_skips_reset(self, tmp_paths):
        self._write_cfg(tmp_paths)
        cd = tmp_paths["cache_dir"]
        cd.mkdir(parents=True)
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        (cd / "a1.json").write_text(json.dumps({"active": True, "fallback_to": "pi", "until": until}))
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="96", include_rolling=False)
        )), patch("engine.backend_pi.reset_session") as mock_reset:
            active, fb, new = kq.should_fallback("a1")
        assert active is True and fb == "pi" and new is False
        mock_reset.assert_not_called()

    def test_reset_session_failure_warns_continues(self, tmp_paths):
        self._write_cfg(tmp_paths)
        with patch("urllib.request.urlopen", return_value=_mk_response(
            _usages_payload(weekly_used="96", include_rolling=False)
        )), patch("engine.backend_pi.reset_session", side_effect=RuntimeError("x")):
            active, fb, new = kq.should_fallback("a1")
        assert active is True and fb == "pi" and new is True
        assert (tmp_paths["cache_dir"] / "a1.json").exists()


# ---------------------------------------------------------------------------
# backend.py dispatch wiring
# ---------------------------------------------------------------------------

class TestBackendWiring:
    def test_resolve_backend_uses_fallback(self, monkeypatch):
        import config
        import engine.backend as backend
        monkeypatch.setattr(config, "DEFAULT_AGENT_BACKEND", "kimi")
        monkeypatch.setattr(config, "AGENT_BACKEND_OVERRIDE", {}, raising=False)
        with patch("engine.kimi_quota.resolve_fallback", return_value="pi"):
            assert backend.resolve_backend("a1") == "pi"

    def test_resolve_backend_ignore_fallback(self, monkeypatch):
        import config
        import engine.backend as backend
        monkeypatch.setattr(config, "DEFAULT_AGENT_BACKEND", "kimi")
        monkeypatch.setattr(config, "AGENT_BACKEND_OVERRIDE", {}, raising=False)
        with patch("engine.kimi_quota.resolve_fallback", return_value="pi") as mock_rf:
            assert backend.resolve_backend("a1", ignore_fallback=True) == "kimi"
        mock_rf.assert_not_called()

    def test_send_dispatches_to_fallback(self, monkeypatch):
        import config
        import engine.backend as backend
        monkeypatch.setattr(config, "DEFAULT_AGENT_BACKEND", "kimi")
        monkeypatch.setattr(config, "AGENT_BACKEND_OVERRIDE", {}, raising=False)
        with patch("engine.kimi_quota.resolve_fallback", return_value=""), \
             patch("engine.kimi_quota.should_fallback", return_value=(True, "pi", True)) as mock_sf, \
             patch("engine.backend_pi.send", return_value="OK") as mock_pi_send:
            result = backend.send("a1", "msg", 10)
        assert result == "OK"
        mock_sf.assert_called_once_with("a1")
        mock_pi_send.assert_called_once()

    def test_send_skips_when_kimi_is_fallback_target(self, monkeypatch):
        # configured backend is agy; agy falls back to kimi. send() must NOT
        # evaluate kimi_should_fallback because kimi is not the configured backend.
        import config
        import engine.backend as backend
        monkeypatch.setattr(config, "DEFAULT_AGENT_BACKEND", "agy")
        monkeypatch.setattr(config, "AGENT_BACKEND_OVERRIDE", {}, raising=False)
        with patch("engine.agy_quota.resolve_fallback", return_value="kimi"), \
             patch("engine.agy_quota.should_fallback", return_value=(False, "", False)), \
             patch("engine.kimi_quota.should_fallback", return_value=(True, "pi", True)) as mock_kimi_sf, \
             patch("engine.backend_kimi.send", return_value="OK") as mock_kimi_send:
            result = backend.send("a1", "msg", 10)
        assert result == "OK"
        mock_kimi_sf.assert_not_called()
        mock_kimi_send.assert_called_once()

    def test_reset_session_resets_kimi_and_fallback(self, monkeypatch):
        import config
        import engine.backend as backend
        monkeypatch.setattr(config, "DEFAULT_AGENT_BACKEND", "kimi")
        monkeypatch.setattr(config, "AGENT_BACKEND_OVERRIDE", {}, raising=False)
        with patch("engine.kimi_quota.resolve_fallback", return_value="pi"), \
             patch("engine.backend_kimi.reset_session") as mock_km_reset, \
             patch("engine.backend_pi.reset_session") as mock_pi_reset:
            backend.reset_session("a1")
        mock_km_reset.assert_called_once_with("a1")
        mock_pi_reset.assert_called_once_with("a1")


# ---------------------------------------------------------------------------
# validate_fallback_config
# ---------------------------------------------------------------------------

class TestValidate:
    def test_fallback_false_skipped(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"fallback": False, "fallback_backend": "bogus"}
        }))
        assert kq.validate_fallback_config() == []

    def test_empty_fallback_backend_skipped(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"fallback": True, "fallback_backend": ""}
        }))
        assert kq.validate_fallback_config() == []

    def test_invalid_fallback_backend_warns(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"fallback": True, "fallback_backend": "bogus"}
        }))
        warns = kq.validate_fallback_config()
        assert any("fallback_backend" in w for w in warns)

    def test_quota_aware_backend_warns(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"fallback": True, "fallback_backend": "agy"}
        }))
        warns = kq.validate_fallback_config()
        assert any("fallback_backend" in w for w in warns)

    def test_threshold_out_of_range_warns(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"fallback": True, "fallback_backend": "pi", "usage_threshold": 150}
        }))
        warns = kq.validate_fallback_config()
        assert any("usage_threshold" in w for w in warns)

    def test_non_integer_threshold_warns(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"fallback": True, "fallback_backend": "pi", "usage_threshold": "abc"}
        }))
        warns = kq.validate_fallback_config()
        assert any("non-integer" in w for w in warns)

    def test_valid_config_no_warn(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"fallback": True, "fallback_backend": "pi", "usage_threshold": 90}
        }))
        assert kq.validate_fallback_config() == []
