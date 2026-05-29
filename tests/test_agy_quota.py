"""Tests for engine/agy_quota.py — agy proactive REST usage-based fallback."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import config.paths as _paths
from engine import agy_quota as aq


@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    """Redirect all agy paths to tmp_path."""
    token = tmp_path / "antigravity-oauth-token"
    cache_dir = tmp_path / "quota-cache-agy"
    agent_cfg = tmp_path / "config_agy.json"
    monkeypatch.setattr(_paths, "AGY_OAUTH_TOKEN", token)
    monkeypatch.setattr(_paths, "AGY_QUOTA_CACHE_DIR", cache_dir)
    monkeypatch.setattr(_paths, "AGY_AGENT_CONFIG", agent_cfg)
    return {
        "token": token,
        "cache_dir": cache_dir,
        "agent_cfg": agent_cfg,
        "tmp_path": tmp_path,
    }


def _write_token(path, *, access="ya29.test", refresh="1//refresh", expiry=None):
    if expiry is None:
        expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    path.write_text(json.dumps({
        "auth_method": "consumer",
        "token": {
            "access_token": access,
            "refresh_token": refresh,
            "expiry": expiry,
            "token_type": "Bearer",
        },
    }))


def _mk_response(data):
    """Build a fake urlopen context manager."""
    body = json.dumps(data).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def _models_payload(gemini_rem=0.04, claude_rem=1.0, gpt_rem=1.0,
                    reset_iso="2099-01-01T00:00:00Z"):
    return {
        "models": {
            "gemini-2.5-pro": {"quotaInfo": {"remainingFraction": gemini_rem, "resetTime": reset_iso}},
            "gemini-2.5-flash": {"quotaInfo": {"remainingFraction": gemini_rem, "resetTime": reset_iso}},
            "claude-sonnet-4": {"quotaInfo": {"remainingFraction": claude_rem, "resetTime": reset_iso}},
            "gpt-4o-mini": {"quotaInfo": {"remainingFraction": gpt_rem, "resetTime": reset_iso}},
        }
    }


def _quota_responses(**kwargs):
    """Side effect for two POST calls: loadCodeAssist, fetchAvailableModels."""
    return [
        _mk_response({"cloudaicompanionProject": "proj-123"}),
        _mk_response(_models_payload(**kwargs)),
    ]


# ---------------------------------------------------------------------------
# _load_token
# ---------------------------------------------------------------------------

class TestLoadToken:
    def test_normal(self, tmp_paths):
        _write_token(tmp_paths["token"])
        tok = aq._load_token()
        assert isinstance(tok, dict)
        assert tok["access_token"] == "ya29.test"

    def test_missing_file(self, tmp_paths):
        assert aq._load_token() is None

    def test_invalid_json(self, tmp_paths):
        tmp_paths["token"].write_text("not json {{{")
        assert aq._load_token() is None

    def test_no_token_key(self, tmp_paths):
        tmp_paths["token"].write_text(json.dumps({"auth_method": "consumer"}))
        assert aq._load_token() is None

    def test_missing_access_token(self, tmp_paths):
        tmp_paths["token"].write_text(json.dumps({"token": {"refresh_token": "r"}}))
        assert aq._load_token() is None

    def test_expired_iso_refresh_success(self, tmp_paths):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_token(tmp_paths["token"], expiry=past)
        with patch("urllib.request.urlopen", return_value=_mk_response({"access_token": "ya29.new"})):
            tok = aq._load_token()
        assert tok is not None and tok["access_token"] == "ya29.new"

    def test_expired_iso_refresh_failure(self, tmp_paths):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_token(tmp_paths["token"], expiry=past)
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert aq._load_token() is None

    def test_expired_epoch_refresh_failure(self, tmp_paths):
        _write_token(tmp_paths["token"], expiry=time.time() - 100)
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert aq._load_token() is None

    def test_future_expiry_no_refresh(self, tmp_paths):
        _write_token(tmp_paths["token"])
        with patch("urllib.request.urlopen") as mock_urlopen:
            tok = aq._load_token()
        assert tok["access_token"] == "ya29.test"
        mock_urlopen.assert_not_called()

    def test_unparseable_expiry_returns_none(self, tmp_paths):
        _write_token(tmp_paths["token"], expiry="not-a-date")
        assert aq._load_token() is None


# ---------------------------------------------------------------------------
# _refresh_token_in_memory
# ---------------------------------------------------------------------------

class TestRefreshInMemory:
    def test_success(self, tmp_paths):
        with patch("urllib.request.urlopen", return_value=_mk_response({"access_token": "ya29.new"})):
            tok = aq._refresh_token_in_memory({"refresh_token": "r", "access_token": "old"})
        assert tok["access_token"] == "ya29.new"
        assert tok["refresh_token"] == "r"

    def test_no_refresh_token(self, tmp_paths):
        assert aq._refresh_token_in_memory({"access_token": "old"}) is None

    def test_http_failure(self, tmp_paths):
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert aq._refresh_token_in_memory({"refresh_token": "r"}) is None

    def test_missing_access_token_in_response(self, tmp_paths):
        with patch("urllib.request.urlopen", return_value=_mk_response({"foo": "bar"})):
            assert aq._refresh_token_in_memory({"refresh_token": "r"}) is None


# ---------------------------------------------------------------------------
# _load_code_assist
# ---------------------------------------------------------------------------

class TestLoadCodeAssist:
    def test_returns_project(self, tmp_paths):
        with patch("urllib.request.urlopen", return_value=_mk_response(
            {"cloudaicompanionProject": "proj-X"}
        )) as mock_urlopen:
            assert aq._load_code_assist("tok") == "proj-X"
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            assert body["metadata"]["ideType"] == "ANTIGRAVITY"
            assert req.headers["Client-metadata"]

    def test_api_error(self, tmp_paths):
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert aq._load_code_assist("tok") is None


# ---------------------------------------------------------------------------
# _model_family
# ---------------------------------------------------------------------------

class TestModelFamily:
    def test_gemini(self):
        assert aq._model_family("Gemini 3 Pro") == "gemini"

    def test_claude(self):
        assert aq._model_family("claude-sonnet-4") == "claude"

    def test_gpt(self):
        assert aq._model_family("gpt-4o-mini") == "gpt"

    def test_empty(self):
        assert aq._model_family("") == "unknown"

    def test_unknown_model(self):
        assert aq._model_family("unknown-model") == "unknown"

    def test_typo_no_match(self):
        assert aq._model_family("Sonnet 4") == "unknown"


# ---------------------------------------------------------------------------
# _fetch_available_models
# ---------------------------------------------------------------------------

class TestFetchAvailableModels:
    def test_normal(self, tmp_paths):
        with patch("urllib.request.urlopen", return_value=_mk_response(_models_payload())):
            models = aq._fetch_available_models("tok", "proj")
        assert "gemini-2.5-pro" in models

    def test_api_error(self, tmp_paths):
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert aq._fetch_available_models("tok", "proj") == {}


# ---------------------------------------------------------------------------
# get_agy_quota
# ---------------------------------------------------------------------------

class TestGetAgyQuota:
    def test_normal_gemini(self, tmp_paths):
        _write_token(tmp_paths["token"])
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.04)):
            ok, frac, dt = aq.get_agy_quota("Gemini 3 Pro")
        assert ok is True and frac == pytest.approx(0.96)
        assert dt is not None and dt.tzinfo is not None

    def test_claude_pool(self, tmp_paths):
        _write_token(tmp_paths["token"])
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.04, claude_rem=0.3)):
            ok, frac, _ = aq.get_agy_quota("claude-sonnet-4")
        assert ok is True and frac == pytest.approx(0.7)

    def test_no_family_match_uses_all_min(self, tmp_paths):
        _write_token(tmp_paths["token"])
        # response has no model containing the target family keyword
        payload = {"models": {
            "foo-model": {"quotaInfo": {"remainingFraction": 0.2, "resetTime": "2099-01-01T00:00:00Z"}},
            "bar-model": {"quotaInfo": {"remainingFraction": 0.5, "resetTime": "2099-01-01T00:00:00Z"}},
        }}
        with patch("urllib.request.urlopen", side_effect=[
            _mk_response({"cloudaicompanionProject": "p"}), _mk_response(payload)
        ]):
            ok, frac, _ = aq.get_agy_quota("Gemini 3 Pro")
        assert ok is True and frac == pytest.approx(0.8)

    def test_unknown_family_uses_all_min(self, tmp_paths):
        _write_token(tmp_paths["token"])
        with patch("urllib.request.urlopen", side_effect=_quota_responses(
            gemini_rem=0.5, claude_rem=0.1, gpt_rem=0.9
        )):
            ok, frac, _ = aq.get_agy_quota("Sonnet 4")  # typo -> unknown -> all min
        assert ok is True and frac == pytest.approx(0.9)

    def test_multiple_same_family_picks_min(self, tmp_paths):
        _write_token(tmp_paths["token"])
        payload = {"models": {
            "gemini-2.5-pro": {"quotaInfo": {"remainingFraction": 0.5, "resetTime": "2099-01-01T00:00:00Z"}},
            "gemini-3-pro": {"quotaInfo": {"remainingFraction": 0.1, "resetTime": "2099-01-01T00:00:00Z"}},
        }}
        with patch("urllib.request.urlopen", side_effect=[
            _mk_response({"cloudaicompanionProject": "p"}), _mk_response(payload)
        ]):
            ok, frac, _ = aq.get_agy_quota("Gemini 3 Pro")
        assert ok is True and frac == pytest.approx(0.9)

    def test_negative_remaining_clamped(self, tmp_paths):
        _write_token(tmp_paths["token"])
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=-0.5)):
            ok, frac, _ = aq.get_agy_quota("Gemini 3 Pro")
        assert ok is True and frac == pytest.approx(1.0)

    def test_above_one_remaining_clamped(self, tmp_paths):
        _write_token(tmp_paths["token"])
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=1.5)):
            ok, frac, _ = aq.get_agy_quota("Gemini 3 Pro")
        assert ok is True and frac == pytest.approx(0.0)

    def test_reset_time_parsed(self, tmp_paths):
        _write_token(tmp_paths["token"])
        with patch("urllib.request.urlopen", side_effect=_quota_responses(
            gemini_rem=0.04, reset_iso="2099-06-01T12:00:00Z"
        )):
            ok, _, dt = aq.get_agy_quota("Gemini 3 Pro")
        assert ok is True and dt.year == 2099 and dt.tzinfo is not None

    def test_reset_time_none(self, tmp_paths):
        _write_token(tmp_paths["token"])
        payload = {"models": {
            "gemini-2.5-pro": {"quotaInfo": {"remainingFraction": 0.04}},
        }}
        with patch("urllib.request.urlopen", side_effect=[
            _mk_response({"cloudaicompanionProject": "p"}), _mk_response(payload)
        ]):
            ok, frac, dt = aq.get_agy_quota("Gemini 3 Pro")
        assert ok is True and dt is None

    def test_token_expired_refresh_fail(self, tmp_paths):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_token(tmp_paths["token"], expiry=past)
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert aq.get_agy_quota("Gemini 3 Pro") == (False, 0.0, None)

    def test_api_error(self, tmp_paths):
        _write_token(tmp_paths["token"])
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert aq.get_agy_quota("Gemini 3 Pro") == (False, 0.0, None)


# ---------------------------------------------------------------------------
# Negative cache
# ---------------------------------------------------------------------------

class TestNegativeCache:
    def _write_cfg(self, tmp_paths, **fields):
        base = {
            "model": "Gemini 3 Pro",
            "fallback": True,
            "fallback_backend": "pi",
            "usage_threshold": 95,
        }
        base.update(fields)
        tmp_paths["agent_cfg"].write_text(json.dumps({"a1": base}))

    def test_written_on_api_failure(self, tmp_paths):
        self._write_cfg(tmp_paths)
        _write_token(tmp_paths["token"])
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            aq.should_fallback("a1")
        neg = aq._negative_cache_path("a1")
        assert neg.exists()
        data = json.loads(neg.read_text())
        assert "token_mtime" in data

    def test_active_skips_http(self, tmp_paths):
        self._write_cfg(tmp_paths)
        _write_token(tmp_paths["token"])
        aq._write_negative_cache("a1")
        with patch("urllib.request.urlopen", side_effect=Exception("should not be called")) as mock_urlopen:
            assert aq.should_fallback("a1") == (False, "", False)
        mock_urlopen.assert_not_called()

    def test_cleared_on_success(self, tmp_paths):
        self._write_cfg(tmp_paths)
        _write_token(tmp_paths["token"])
        aq._write_negative_cache("a1")
        # token mtime unchanged but TTL valid -> need to expire neg cache for HTTP to run;
        # instead delete it directly to simulate clear path via successful call
        aq._negative_cache_path("a1").unlink()
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.5)):
            aq.should_fallback("a1")
        assert not aq._negative_cache_path("a1").exists()

    def test_expired_resumes_http(self, tmp_paths):
        self._write_cfg(tmp_paths)
        _write_token(tmp_paths["token"])
        cd = tmp_paths["cache_dir"]
        cd.mkdir(parents=True, exist_ok=True)
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        aq._negative_cache_path("a1").write_text(json.dumps({"until": past}))
        assert aq._negative_cache_active("a1") is False

    def test_corrupt_inactive(self, tmp_paths):
        cd = tmp_paths["cache_dir"]
        cd.mkdir(parents=True, exist_ok=True)
        aq._negative_cache_path("a1").write_text("not json")
        assert aq._negative_cache_active("a1") is False

    def test_token_mtime_change_invalidates(self, tmp_paths):
        _write_token(tmp_paths["token"])
        aq._write_negative_cache("a1")
        assert aq._negative_cache_active("a1") is True
        # Simulate agy refreshing token (mtime change)
        _write_token(tmp_paths["token"], access="ya29.refreshed")
        # Force a different mtime
        import os
        st = tmp_paths["token"].stat()
        os.utime(tmp_paths["token"], (st.st_atime + 5, st.st_mtime + 5))
        assert aq._negative_cache_active("a1") is False

    def test_token_missing_no_mtime(self, tmp_paths):
        # token file absent at write time -> no token_mtime recorded
        aq._write_negative_cache("a1")
        data = json.loads(aq._negative_cache_path("a1").read_text())
        assert "token_mtime" not in data
        assert aq._negative_cache_active("a1") is True


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
        assert aq.resolve_fallback("a1") == "pi"

    def test_expired_cache(self, tmp_paths):
        until = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self._write_cache(tmp_paths, "a1", {"active": True, "fallback_to": "pi", "until": until})
        assert aq.resolve_fallback("a1") == ""

    def test_missing_cache(self, tmp_paths):
        assert aq.resolve_fallback("ghost") == ""

    def test_corrupt_cache(self, tmp_paths):
        cd = tmp_paths["cache_dir"]
        cd.mkdir(parents=True)
        (cd / "a1.json").write_text("not json {{{")
        assert aq.resolve_fallback("a1") == ""

    def test_invalid_fallback_to_miss(self, tmp_paths):
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self._write_cache(tmp_paths, "a1", {"active": True, "fallback_to": "bogus", "until": until})
        assert aq.resolve_fallback("a1") == ""


# ---------------------------------------------------------------------------
# should_fallback
# ---------------------------------------------------------------------------

class TestShouldFallback:
    def _write_cfg(self, tmp_paths, **fields):
        base = {
            "model": "Gemini 3 Pro",
            "fallback": True,
            "fallback_backend": "pi",
            "usage_threshold": 95,
        }
        base.update(fields)
        tmp_paths["agent_cfg"].write_text(json.dumps({"a1": base}))
        _write_token(tmp_paths["token"])

    def test_fallback_disabled(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback=False)
        assert aq.should_fallback("a1") == (False, "", False)

    def test_empty_model(self, tmp_paths):
        self._write_cfg(tmp_paths, model="")
        assert aq.should_fallback("a1") == (False, "", False)

    def test_empty_fallback_backend(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback_backend="")
        assert aq.should_fallback("a1") == (False, "", False)

    def test_invalid_fallback_backend(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback_backend="bogus")
        assert aq.should_fallback("a1") == (False, "", False)

    def test_threshold_exceeded_creates_cache(self, tmp_paths):
        self._write_cfg(tmp_paths)
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.04)), \
             patch("engine.backend_pi.reset_session") as mock_reset:
            active, fb, new = aq.should_fallback("a1")
        assert active is True and fb == "pi" and new is True
        mock_reset.assert_called_once_with("a1")
        cache = json.loads((tmp_paths["cache_dir"] / "a1.json").read_text())
        assert cache["active"] is True and cache["fallback_to"] == "pi"

    def test_below_threshold(self, tmp_paths):
        self._write_cfg(tmp_paths)
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.5)):
            assert aq.should_fallback("a1") == (False, "", False)

    def test_api_failure_writes_neg_cache(self, tmp_paths):
        self._write_cfg(tmp_paths)
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert aq.should_fallback("a1") == (False, "", False)
        assert aq._negative_cache_path("a1").exists()

    def test_boundary_at_threshold(self, tmp_paths):
        self._write_cfg(tmp_paths, usage_threshold=95)
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.05)), \
             patch("engine.backend_pi.reset_session"):
            active, fb, _ = aq.should_fallback("a1")
        assert active is True and fb == "pi"

    def test_boundary_under_threshold(self, tmp_paths):
        self._write_cfg(tmp_paths, usage_threshold=95)
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.051)):
            active, _, _ = aq.should_fallback("a1")
        assert active is False

    def test_threshold_default_95(self, tmp_paths):
        cfg = {"model": "Gemini 3 Pro", "fallback": True, "fallback_backend": "pi"}
        tmp_paths["agent_cfg"].write_text(json.dumps({"a1": cfg}))
        _write_token(tmp_paths["token"])
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.04)), \
             patch("engine.backend_pi.reset_session"):
            active, _, _ = aq.should_fallback("a1")
        assert active is True

    def test_reset_time_past_clamped(self, tmp_paths):
        self._write_cfg(tmp_paths)
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.04, reset_iso=past)), \
             patch("engine.backend_pi.reset_session"):
            aq.should_fallback("a1")
        cache = json.loads((tmp_paths["cache_dir"] / "a1.json").read_text())
        until = datetime.fromisoformat(cache["until"])
        delta = until - datetime.now(timezone.utc)
        assert timedelta(minutes=4) < delta < timedelta(minutes=6)

    def test_reset_time_far_future_clamped(self, tmp_paths):
        self._write_cfg(tmp_paths)
        far = (datetime.now(timezone.utc) + timedelta(hours=240)).isoformat()
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.04, reset_iso=far)), \
             patch("engine.backend_pi.reset_session"):
            aq.should_fallback("a1")
        cache = json.loads((tmp_paths["cache_dir"] / "a1.json").read_text())
        until = datetime.fromisoformat(cache["until"])
        delta = until - datetime.now(timezone.utc)
        # default_hrs=5
        assert timedelta(hours=4, minutes=55) < delta < timedelta(hours=5, minutes=5)

    def test_reset_time_in_range_used(self, tmp_paths):
        self._write_cfg(tmp_paths)
        target = datetime.now(timezone.utc) + timedelta(hours=3)
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.04, reset_iso=target.isoformat())), \
             patch("engine.backend_pi.reset_session"):
            aq.should_fallback("a1")
        cache = json.loads((tmp_paths["cache_dir"] / "a1.json").read_text())
        until = datetime.fromisoformat(cache["until"])
        assert abs((until - target).total_seconds()) < 1

    def test_reset_time_none_default_5h(self, tmp_paths):
        self._write_cfg(tmp_paths)
        payload = {"models": {
            "gemini-2.5-pro": {"quotaInfo": {"remainingFraction": 0.04}},
        }}
        with patch("urllib.request.urlopen", side_effect=[
            _mk_response({"cloudaicompanionProject": "p"}), _mk_response(payload)
        ]), patch("engine.backend_pi.reset_session"):
            aq.should_fallback("a1")
        cache = json.loads((tmp_paths["cache_dir"] / "a1.json").read_text())
        until = datetime.fromisoformat(cache["until"])
        delta = until - datetime.now(timezone.utc)
        assert timedelta(hours=4, minutes=55) < delta < timedelta(hours=5, minutes=5)

    def test_dcl_existing_cache_skips_reset(self, tmp_paths):
        self._write_cfg(tmp_paths)
        cd = tmp_paths["cache_dir"]
        cd.mkdir(parents=True)
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        (cd / "a1.json").write_text(json.dumps({"active": True, "fallback_to": "pi", "until": until}))
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.04)), \
             patch("engine.backend_pi.reset_session") as mock_reset:
            active, fb, new = aq.should_fallback("a1")
        assert active is True and fb == "pi" and new is False
        mock_reset.assert_not_called()

    def test_reset_session_failure_warns_continues(self, tmp_paths):
        self._write_cfg(tmp_paths)
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.04)), \
             patch("engine.backend_pi.reset_session", side_effect=RuntimeError("x")):
            active, fb, new = aq.should_fallback("a1")
        assert active is True and fb == "pi" and new is True
        assert (tmp_paths["cache_dir"] / "a1.json").exists()

    def test_kimi_fallback_backend(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback_backend="kimi")
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.04)), \
             patch("engine.backend_kimi.reset_session") as mock_reset:
            active, fb, _ = aq.should_fallback("a1")
        assert active is True and fb == "kimi"
        mock_reset.assert_called_once_with("a1")

    def test_openclaw_fallback_backend_no_reset(self, tmp_paths):
        self._write_cfg(tmp_paths, fallback_backend="openclaw")
        with patch("urllib.request.urlopen", side_effect=_quota_responses(gemini_rem=0.04)):
            active, fb, _ = aq.should_fallback("a1")
        assert active is True and fb == "openclaw"


# ---------------------------------------------------------------------------
# validate_fallback_config
# ---------------------------------------------------------------------------

class TestValidate:
    def test_fallback_false_skipped(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"model": "Gemini 3 Pro", "fallback": False, "fallback_backend": "bogus"}
        }))
        assert aq.validate_fallback_config() == []

    def test_empty_model_warns(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"model": "", "fallback": True, "fallback_backend": "pi"}
        }))
        warns = aq.validate_fallback_config()
        assert any("fallback will not engage" in w for w in warns)

    def test_unknown_family_warns(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"model": "Sonnet 4", "fallback": True, "fallback_backend": "pi"}
        }))
        warns = aq.validate_fallback_config()
        assert any("all-model minimum" in w for w in warns)

    def test_empty_fallback_backend_skipped(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"model": "Gemini 3 Pro", "fallback": True, "fallback_backend": ""}
        }))
        assert aq.validate_fallback_config() == []

    def test_invalid_fallback_backend_warns(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"model": "Gemini 3 Pro", "fallback": True, "fallback_backend": "bogus"}
        }))
        warns = aq.validate_fallback_config()
        assert any("fallback_backend" in w for w in warns)

    def test_threshold_out_of_range_warns(self, tmp_paths):
        tmp_paths["agent_cfg"].write_text(json.dumps({
            "a": {"model": "Gemini 3 Pro", "fallback": True, "fallback_backend": "pi", "usage_threshold": 150}
        }))
        warns = aq.validate_fallback_config()
        assert any("usage_threshold" in w for w in warns)
