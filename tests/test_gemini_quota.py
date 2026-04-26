"""Tests for engine/gemini_quota.py — Pro usage-based fallback."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import config.paths as _paths
from engine import gemini_quota as gq


@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    """Redirect all gemini paths to tmp_path."""
    creds = tmp_path / "oauth_creds.json"
    settings = tmp_path / "settings.json"
    cache_dir = tmp_path / "quota-cache"
    agent_cfg = tmp_path / "config_gemini.json"
    monkeypatch.setattr(_paths, "GEMINI_OAUTH_CREDS", creds)
    monkeypatch.setattr(_paths, "GEMINI_SETTINGS", settings)
    monkeypatch.setattr(_paths, "GEMINI_QUOTA_CACHE_DIR", cache_dir)
    monkeypatch.setattr(_paths, "GEMINI_AGENT_CONFIG", agent_cfg)
    return {
        "creds": creds,
        "settings": settings,
        "cache_dir": cache_dir,
        "agent_cfg": agent_cfg,
        "tmp_path": tmp_path,
    }


def _write_settings(path, sel="oauth-personal"):
    path.write_text(json.dumps({"security": {"auth": {"selectedType": sel}}}))


def _write_creds(path, expires_at=None):
    if expires_at is None:
        expires_at = time.time() + 3600
    path.write_text(json.dumps({
        "access_token": "ya29.test",
        "refresh_token": "1//refresh",
        "expires_at": expires_at,
    }))


def _mk_response(data, status=200):
    """Build a fake urlopen context manager."""
    body = json.dumps(data).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def _mk_quota_responses(usage=0.96, reset_iso="2099-01-01T00:00:00+00:00"):
    """Side effect for two POST calls: loadCodeAssist, retrieveUserQuota."""
    load_resp = {"cloudaicompanionProject": "proj-123"}
    quota_resp = {
        "buckets": [
            {"modelId": "gemini-2.5-pro", "remainingFraction": 1.0 - usage, "resetTime": reset_iso},
            {"modelId": "gemini-2.5-flash", "remainingFraction": 0.85, "resetTime": reset_iso},
        ]
    }
    return [_mk_response(load_resp), _mk_response(quota_resp)]


# ---------------------------------------------------------------------------
# _refresh_token_if_expired
# ---------------------------------------------------------------------------

class TestRefreshToken:
    def test_no_op_when_not_expired(self, tmp_paths):
        _write_creds(tmp_paths["creds"], expires_at=time.time() + 3600)
        with patch("urllib.request.urlopen") as mock_urlopen:
            creds = gq._load_oauth_creds()
            result = gq._refresh_token_if_expired(creds)
        assert result["access_token"] == "ya29.test"
        mock_urlopen.assert_not_called()

    def test_refresh_when_expired(self, tmp_paths):
        _write_creds(tmp_paths["creds"], expires_at=time.time() - 100)
        with patch("urllib.request.urlopen", return_value=_mk_response({
            "access_token": "ya29.new",
            "expires_in": 3600,
            "token_type": "Bearer",
        })):
            creds = gq._load_oauth_creds()
            result = gq._refresh_token_if_expired(creds)
        assert result["access_token"] == "ya29.new"
        on_disk = json.loads(tmp_paths["creds"].read_text())
        assert on_disk["access_token"] == "ya29.new"
        assert on_disk["refresh_token"] == "1//refresh"

    def test_chmod_600_after_refresh(self, tmp_paths):
        _write_creds(tmp_paths["creds"], expires_at=0)
        with patch("urllib.request.urlopen", return_value=_mk_response({
            "access_token": "ya29.new",
            "expires_in": 3600,
        })):
            creds = gq._load_oauth_creds()
            gq._refresh_token_if_expired(creds)
        mode = os.stat(tmp_paths["creds"]).st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# _load_code_assist / _retrieve_user_quota
# ---------------------------------------------------------------------------

class TestApiCalls:
    def test_load_code_assist_returns_project(self, tmp_paths):
        with patch("urllib.request.urlopen", return_value=_mk_response(
            {"cloudaicompanionProject": "proj-X"}
        )) as mock_urlopen:
            assert gq._load_code_assist("tok") == "proj-X"
            req = mock_urlopen.call_args[0][0]
            assert req.full_url.endswith(":loadCodeAssist")
            assert req.headers["Authorization"] == "Bearer tok"
            assert req.headers["Content-type"] == "application/json"
            assert mock_urlopen.call_args.kwargs.get("timeout") == 10

    def test_retrieve_user_quota_body(self, tmp_paths):
        with patch("urllib.request.urlopen", return_value=_mk_response(
            {"buckets": [{"modelId": "gemini-2.5-pro", "remainingFraction": 0.5}]}
        )) as mock_urlopen:
            buckets = gq._retrieve_user_quota("tok", "proj-Y")
            assert len(buckets) == 1
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body == {"project": "proj-Y"}
            assert mock_urlopen.call_args.kwargs.get("timeout") == 10


# ---------------------------------------------------------------------------
# get_pro_quota
# ---------------------------------------------------------------------------

class TestGetProQuota:
    def _setup(self, tmp_paths, sel="oauth-personal"):
        _write_settings(tmp_paths["settings"], sel)
        _write_creds(tmp_paths["creds"])

    def test_normal(self, tmp_paths):
        self._setup(tmp_paths)
        with patch("urllib.request.urlopen", side_effect=_mk_quota_responses(usage=0.96)):
            ok, frac, dt = gq.get_pro_quota()
        assert ok is True
        assert frac == pytest.approx(0.96)
        assert dt is not None and dt.tzinfo is not None

    def test_zero_usage(self, tmp_paths):
        self._setup(tmp_paths)
        with patch("urllib.request.urlopen", side_effect=_mk_quota_responses(usage=0.0)):
            ok, frac, _ = gq.get_pro_quota()
        assert ok is True and frac == pytest.approx(0.0)

    def test_no_pro_bucket(self, tmp_paths):
        self._setup(tmp_paths)
        load = _mk_response({"cloudaicompanionProject": "p"})
        quota = _mk_response({"buckets": [{"modelId": "gemini-2.5-flash", "remainingFraction": 0.5}]})
        with patch("urllib.request.urlopen", side_effect=[load, quota]):
            ok, frac, dt = gq.get_pro_quota()
        assert ok is False and frac == 0.0 and dt is None

    def test_multiple_pro_buckets_picks_min(self, tmp_paths):
        self._setup(tmp_paths)
        load = _mk_response({"cloudaicompanionProject": "p"})
        quota = _mk_response({"buckets": [
            {"modelId": "gemini-2.5-pro", "remainingFraction": 0.5, "resetTime": "2099-01-01T00:00:00Z"},
            {"modelId": "gemini-2.5-pro-exp", "remainingFraction": 0.1, "resetTime": "2099-01-01T00:00:00Z"},
        ]})
        with patch("urllib.request.urlopen", side_effect=[load, quota]):
            ok, frac, _ = gq.get_pro_quota()
        # smallest remaining = 0.1 -> usage = 0.9
        assert ok is True and frac == pytest.approx(0.9)

    def test_api_error(self, tmp_paths):
        self._setup(tmp_paths)
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            ok, frac, dt = gq.get_pro_quota()
        assert ok is False and frac == 0.0 and dt is None

    def test_non_oauth_personal(self, tmp_paths):
        self._setup(tmp_paths, sel="api-key")
        ok, frac, dt = gq.get_pro_quota()
        assert ok is False

    def test_no_creds(self, tmp_paths):
        _write_settings(tmp_paths["settings"])
        ok, frac, dt = gq.get_pro_quota()
        assert ok is False

    def test_reset_time_none(self, tmp_paths):
        self._setup(tmp_paths)
        load = _mk_response({"cloudaicompanionProject": "p"})
        quota = _mk_response({"buckets": [{"modelId": "gemini-2.5-pro", "remainingFraction": 0.04}]})
        with patch("urllib.request.urlopen", side_effect=[load, quota]):
            ok, frac, dt = gq.get_pro_quota()
        assert ok is True and dt is None

    def test_remaining_fraction_negative_clamped(self, tmp_paths):
        self._setup(tmp_paths)
        load = _mk_response({"cloudaicompanionProject": "p"})
        quota = _mk_response({"buckets": [
            {"modelId": "gemini-2.5-pro", "remainingFraction": -0.5, "resetTime": "2099-01-01T00:00:00Z"}
        ]})
        with patch("urllib.request.urlopen", side_effect=[load, quota]):
            ok, frac, _ = gq.get_pro_quota()
        assert ok is True and frac == pytest.approx(1.0)

    def test_remaining_fraction_above_one_clamped(self, tmp_paths):
        self._setup(tmp_paths)
        load = _mk_response({"cloudaicompanionProject": "p"})
        quota = _mk_response({"buckets": [
            {"modelId": "gemini-2.5-pro", "remainingFraction": 1.5, "resetTime": "2099-01-01T00:00:00Z"}
        ]})
        with patch("urllib.request.urlopen", side_effect=[load, quota]):
            ok, frac, _ = gq.get_pro_quota()
        assert ok is True and frac == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# resolve_fallback
# ---------------------------------------------------------------------------

class TestResolveFallback:
    def _write_cache(self, tmp_paths, agent_id, payload):
        cd = tmp_paths["cache_dir"]
        cd.mkdir(parents=True, exist_ok=True)
        (cd / f"{agent_id}.json").write_text(json.dumps(payload))

    def test_active_cache_returns_fallback_to(self, tmp_paths):
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self._write_cache(tmp_paths, "a1", {"active": True, "fallback_to": "pi", "until": until})
        assert gq.resolve_fallback("a1") == "pi"

    def test_expired_cache_returns_empty(self, tmp_paths):
        until = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self._write_cache(tmp_paths, "a1", {"active": True, "fallback_to": "pi", "until": until})
        assert gq.resolve_fallback("a1") == ""

    def test_missing_cache(self, tmp_paths):
        assert gq.resolve_fallback("ghost") == ""

    def test_corrupt_cache(self, tmp_paths):
        cd = tmp_paths["cache_dir"]
        cd.mkdir(parents=True)
        (cd / "a1.json").write_text("not json {{{")
        assert gq.resolve_fallback("a1") == ""

    def test_config_fallback_false_does_not_affect(self, tmp_paths):
        """Active cache returns fallback_to even if config has fallback=false."""
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a1": {"model": "gemini-2.5-pro", "fallback": False, "fallback_backend": ""}
        }))
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self._write_cache(tmp_paths, "a1", {"active": True, "fallback_to": "pi", "until": until})
        assert gq.resolve_fallback("a1") == "pi"


# ---------------------------------------------------------------------------
# should_fallback
# ---------------------------------------------------------------------------

class TestShouldFallback:
    def _write_cfg(self, tmp_paths, **fields):
        base = {
            "model": "gemini-2.5-pro",
            "fallback": True,
            "fallback_backend": "pi",
            "usage_threshold": 95,
        }
        base.update(fields)
        tmp_paths["agent_cfg"].write_text(json.dumps({"a1": base}))
        _write_settings(tmp_paths["settings"])
        _write_creds(tmp_paths["creds"])

    def test_fallback_disabled(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback=False)
        assert gq.should_fallback("a1") == (False, "", False)

    def test_non_pro_model(self, tmp_paths):
        self._write_cfg(tmp_paths, model="gemini-2.5-flash")
        assert gq.should_fallback("a1") == (False, "", False)

    def test_empty_fallback_backend(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback_backend="")
        assert gq.should_fallback("a1") == (False, "", False)

    def test_threshold_exceeded_creates_cache(self, tmp_paths):
        self._write_cfg(tmp_paths)
        with patch("urllib.request.urlopen", side_effect=_mk_quota_responses(usage=0.96)), \
             patch("engine.backend_pi.reset_session") as mock_reset:
            active, fb, new = gq.should_fallback("a1")
        assert active is True and fb == "pi" and new is True
        mock_reset.assert_called_once_with("a1")
        cache_path = tmp_paths["cache_dir"] / "a1.json"
        cache = json.loads(cache_path.read_text())
        assert cache["active"] is True and cache["fallback_to"] == "pi"

    def test_below_threshold(self, tmp_paths):
        self._write_cfg(tmp_paths)
        with patch("urllib.request.urlopen", side_effect=_mk_quota_responses(usage=0.5)):
            assert gq.should_fallback("a1") == (False, "", False)

    def test_api_failure(self, tmp_paths):
        self._write_cfg(tmp_paths)
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert gq.should_fallback("a1") == (False, "", False)

    def test_boundary_threshold_under(self, tmp_paths):
        self._write_cfg(tmp_paths, usage_threshold=95)
        with patch("urllib.request.urlopen", side_effect=_mk_quota_responses(usage=0.949)):
            active, _, _ = gq.should_fallback("a1")
        assert active is False

    def test_boundary_threshold_at(self, tmp_paths):
        self._write_cfg(tmp_paths, usage_threshold=95)
        with patch("urllib.request.urlopen", side_effect=_mk_quota_responses(usage=0.950)), \
             patch("engine.backend_pi.reset_session"):
            active, fb, _ = gq.should_fallback("a1")
        assert active is True and fb == "pi"

    def test_threshold_unspecified_defaults_95(self, tmp_paths):
        cfg = {"model": "gemini-2.5-pro", "fallback": True, "fallback_backend": "pi"}
        tmp_paths["agent_cfg"].write_text(json.dumps({"a1": cfg}))
        _write_settings(tmp_paths["settings"])
        _write_creds(tmp_paths["creds"])
        with patch("urllib.request.urlopen", side_effect=_mk_quota_responses(usage=0.96)), \
             patch("engine.backend_pi.reset_session"):
            active, _, _ = gq.should_fallback("a1")
        assert active is True

    def test_reset_time_past_clamped(self, tmp_paths):
        self._write_cfg(tmp_paths)
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with patch("urllib.request.urlopen", side_effect=_mk_quota_responses(usage=0.96, reset_iso=past)), \
             patch("engine.backend_pi.reset_session"):
            gq.should_fallback("a1")
        cache = json.loads((tmp_paths["cache_dir"] / "a1.json").read_text())
        until = datetime.fromisoformat(cache["until"])
        delta = until - datetime.now(timezone.utc)
        assert timedelta(minutes=4) < delta < timedelta(minutes=6)

    def test_reset_time_far_future_clamped(self, tmp_paths):
        self._write_cfg(tmp_paths)
        far = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()
        with patch("urllib.request.urlopen", side_effect=_mk_quota_responses(usage=0.96, reset_iso=far)), \
             patch("engine.backend_pi.reset_session"):
            gq.should_fallback("a1")
        cache = json.loads((tmp_paths["cache_dir"] / "a1.json").read_text())
        until = datetime.fromisoformat(cache["until"])
        delta = until - datetime.now(timezone.utc)
        assert timedelta(minutes=55) < delta < timedelta(minutes=65)

    def test_reset_time_in_range_used(self, tmp_paths):
        self._write_cfg(tmp_paths)
        target = datetime.now(timezone.utc) + timedelta(hours=3)
        with patch("urllib.request.urlopen", side_effect=_mk_quota_responses(usage=0.96, reset_iso=target.isoformat())), \
             patch("engine.backend_pi.reset_session"):
            gq.should_fallback("a1")
        cache = json.loads((tmp_paths["cache_dir"] / "a1.json").read_text())
        until = datetime.fromisoformat(cache["until"])
        assert abs((until - target).total_seconds()) < 1

    def test_reset_time_none_default_1h(self, tmp_paths):
        self._write_cfg(tmp_paths)
        load = _mk_response({"cloudaicompanionProject": "p"})
        quota = _mk_response({"buckets": [{"modelId": "gemini-2.5-pro", "remainingFraction": 0.04}]})
        with patch("urllib.request.urlopen", side_effect=[load, quota]), \
             patch("engine.backend_pi.reset_session"):
            gq.should_fallback("a1")
        cache = json.loads((tmp_paths["cache_dir"] / "a1.json").read_text())
        until = datetime.fromisoformat(cache["until"])
        delta = until - datetime.now(timezone.utc)
        assert timedelta(minutes=55) < delta < timedelta(minutes=65)

    def test_dcl_existing_cache_skips_reset(self, tmp_paths):
        self._write_cfg(tmp_paths)
        # Pre-create active cache (simulate a competing process)
        cd = tmp_paths["cache_dir"]
        cd.mkdir(parents=True)
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        (cd / "a1.json").write_text(json.dumps({"active": True, "fallback_to": "pi", "until": until}))
        with patch("urllib.request.urlopen", side_effect=_mk_quota_responses(usage=0.96)), \
             patch("engine.backend_pi.reset_session") as mock_reset:
            active, fb, new = gq.should_fallback("a1")
        assert active is True and fb == "pi" and new is False
        mock_reset.assert_not_called()

    def test_reset_session_failure_warns_continues(self, tmp_paths):
        self._write_cfg(tmp_paths)
        with patch("urllib.request.urlopen", side_effect=_mk_quota_responses(usage=0.96)), \
             patch("engine.backend_pi.reset_session", side_effect=RuntimeError("x")):
            active, fb, new = gq.should_fallback("a1")
        assert active is True and fb == "pi" and new is True
        assert (tmp_paths["cache_dir"] / "a1.json").exists()


# ---------------------------------------------------------------------------
# validate_fallback_config
# ---------------------------------------------------------------------------

class TestValidate:
    def test_fallback_false_skipped(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"model": "gemini-2.5-pro", "fallback": False, "fallback_backend": "bogus"}
        }))
        assert gq.validate_fallback_config() == []

    def test_empty_fallback_backend_skipped(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"model": "gemini-2.5-pro", "fallback": True, "fallback_backend": ""}
        }))
        assert gq.validate_fallback_config() == []

    def test_invalid_fallback_backend_warns(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"model": "gemini-2.5-pro", "fallback": True, "fallback_backend": "openclaw"}
        }))
        warns = gq.validate_fallback_config()
        assert any("fallback_backend" in w for w in warns)

    def test_threshold_out_of_range_warns(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"model": "gemini-2.5-pro", "fallback": True, "fallback_backend": "pi", "usage_threshold": 150}
        }))
        warns = gq.validate_fallback_config()
        assert any("usage_threshold" in w for w in warns)

    def test_non_pro_model_with_fallback_warns(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"model": "gemini-2.5-flash", "fallback": True, "fallback_backend": "pi"}
        }))
        warns = gq.validate_fallback_config()
        assert any("does not contain 'pro'" in w for w in warns)
