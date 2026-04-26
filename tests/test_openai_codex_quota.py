"""Tests for engine/openai_codex_quota.py — Codex usage-based fallback."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import config.paths as _paths
from engine import openai_codex_quota as oq


@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    pi_auth = tmp_path / "pi_auth.json"
    codex_auth = tmp_path / "codex_auth.json"
    cache_dir = tmp_path / "quota-cache-codex"
    pi_cfg = tmp_path / "config_pi.json"
    monkeypatch.setattr(_paths, "PI_AUTH_FILE", pi_auth)
    monkeypatch.setattr(_paths, "CODEX_AUTH_FILE", codex_auth)
    monkeypatch.setattr(_paths, "OPENAI_CODEX_QUOTA_CACHE_DIR", cache_dir)
    monkeypatch.setattr(_paths, "PI_AGENT_CONFIG", pi_cfg)
    return {
        "pi_auth": pi_auth,
        "codex_auth": codex_auth,
        "cache_dir": cache_dir,
        "pi_cfg": pi_cfg,
        "tmp_path": tmp_path,
    }


def _write_pi_auth(path, expires_ms=None):
    if expires_ms is None:
        expires_ms = int((time.time() + 3600) * 1000)
    path.write_text(json.dumps({
        "openai-codex": {
            "access": "tok_pi",
            "refresh": "rfr",
            "expires": expires_ms,
            "accountId": "acct_pi",
        }
    }))


def _write_codex_auth(path, expires_at_sec=None):
    if expires_at_sec is None:
        expires_at_sec = time.time() + 3600
    path.write_text(json.dumps({
        "tokens": {
            "access_token": "tok_codex",
            "account_id": "acct_codex",
            "expires_at": expires_at_sec,
        }
    }))


def _mk_response(data):
    body = json.dumps(data).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def _write_pi_cfg(path, agent="impl1", **fields):
    cfg = {
        agent: {
            "provider": "openai-codex",
            "model": "gpt-5.4",
            "fallback": True,
            "fallback_provider": "github-copilot",
            "fallback_model": "gpt-5.4",
            "usage_threshold": 95,
            **fields,
        }
    }
    path.write_text(json.dumps(cfg))


# ---------------------------------------------------------------------------
# _load_codex_auth
# ---------------------------------------------------------------------------

class TestLoadAuth:
    def test_pi_auth_preferred(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_codex_auth(tmp_paths["codex_auth"])
        assert oq._load_codex_auth() == ("tok_pi", "acct_pi")

    def test_codex_fallback_when_pi_missing(self, tmp_paths):
        _write_codex_auth(tmp_paths["codex_auth"])
        assert oq._load_codex_auth() == ("tok_codex", "acct_codex")

    def test_pi_expired_returns_none(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"], expires_ms=int((time.time() - 60) * 1000))
        # Even though codex auth is valid, expired pi auth fails closed (returns None)
        # per spec: "expires/1000 < time.time() なら期限切れ → None (fail-open)"
        _write_codex_auth(tmp_paths["codex_auth"])
        # Pi entry hits the expired branch and returns None directly.
        assert oq._load_codex_auth() is None

    def test_codex_expired_returns_none(self, tmp_paths):
        _write_codex_auth(tmp_paths["codex_auth"], expires_at_sec=time.time() - 60)
        assert oq._load_codex_auth() is None

    def test_both_missing(self, tmp_paths):
        assert oq._load_codex_auth() is None

    def test_pi_empty_dict(self, tmp_paths):
        tmp_paths["pi_auth"].write_text("{}")
        _write_codex_auth(tmp_paths["codex_auth"])
        assert oq._load_codex_auth() == ("tok_codex", "acct_codex")

    def test_codex_no_expires_at(self, tmp_paths):
        tmp_paths["codex_auth"].write_text(json.dumps({
            "tokens": {"access_token": "x", "account_id": "y"}
        }))
        assert oq._load_codex_auth() == ("x", "y")


# ---------------------------------------------------------------------------
# get_codex_usage
# ---------------------------------------------------------------------------

class TestGetUsage:
    def test_no_auth_fail_open(self, tmp_paths):
        ok, used, reset = oq.get_codex_usage()
        assert (ok, used, reset) == (False, 0.0, None)

    def test_basic_weekly(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        future_ms = int((time.time() + 86400 * 6) * 1000)
        resp = _mk_response({
            "rate_limit": {
                "five_hour": {"percent_left": 80, "reset_time_ms": int(time.time() * 1000)},
                "weekly": {"percent_left": 5, "reset_time_ms": future_ms},
            }
        })
        with patch("urllib.request.urlopen", return_value=resp):
            ok, used, reset = oq.get_codex_usage()
        assert ok is True
        assert used == pytest.approx(95.0)
        assert reset is not None and reset > datetime.now(timezone.utc)

    def test_field_aliases_rate_limits_secondary_remaining(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({
            "rate_limits": {
                "secondary": {"remaining_percent": 10, "reset_at": "2099-01-01T00:00:00Z"},
            }
        })
        with patch("urllib.request.urlopen", return_value=resp):
            ok, used, reset = oq.get_codex_usage()
        assert ok is True
        assert used == pytest.approx(90.0)
        assert reset == datetime(2099, 1, 1, tzinfo=timezone.utc)

    def test_5h_window_ignored_when_weekly_fine(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({
            "rate_limit": {
                "five_hour": {"percent_left": 0},
                "weekly": {"percent_left": 50, "reset_time_ms": int((time.time() + 3600) * 1000)},
            }
        })
        with patch("urllib.request.urlopen", return_value=resp):
            ok, used, _ = oq.get_codex_usage()
        # weekly half left → used=50, not impacted by 5h being depleted
        assert ok is True
        assert used == pytest.approx(50.0)

    @pytest.mark.parametrize("raw,expected_used", [
        ("5", 95.0),
        (5.4, 94.6),
        (150, 0.0),       # clamp to 100 → used 0
        (-10, 100.0),     # clamp to 0 → used 100
    ])
    def test_percent_left_normalization(self, tmp_paths, raw, expected_used):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {"weekly": {"percent_left": raw}}})
        with patch("urllib.request.urlopen", return_value=resp):
            ok, used, _ = oq.get_codex_usage()
        assert ok is True
        assert used == pytest.approx(expected_used)

    @pytest.mark.parametrize("raw", ["abc", None, float("nan"), float("inf")])
    def test_percent_left_invalid_fail_open(self, tmp_paths, raw):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {"weekly": {"percent_left": raw}}})
        with patch("urllib.request.urlopen", return_value=resp):
            ok, used, reset = oq.get_codex_usage()
        assert (ok, used, reset) == (False, 0.0, None)

    def test_reset_at_iso_naive_assumes_utc(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({
            "rate_limit": {"weekly": {"percent_left": 50, "reset_at": "2099-06-15T12:00:00"}}
        })
        with patch("urllib.request.urlopen", return_value=resp):
            _, _, reset = oq.get_codex_usage()
        assert reset == datetime(2099, 6, 15, 12, 0, tzinfo=timezone.utc)

    def test_no_reset_fields_returns_none(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {"weekly": {"percent_left": 50}}})
        with patch("urllib.request.urlopen", return_value=resp):
            _, _, reset = oq.get_codex_usage()
        assert reset is None

    def test_http_error_fail_open(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            ok, used, reset = oq.get_codex_usage()
        assert (ok, used, reset) == (False, 0.0, None)


# ---------------------------------------------------------------------------
# should_fallback
# ---------------------------------------------------------------------------

class TestShouldFallback:
    def test_no_config_no_fallback(self, tmp_paths):
        assert oq.should_fallback("impl1") == (False, "", "", False)

    def test_provider_not_codex(self, tmp_paths):
        _write_pi_cfg(tmp_paths["pi_cfg"], provider="github-copilot")
        assert oq.should_fallback("impl1") == (False, "", "", False)

    def test_fallback_disabled(self, tmp_paths):
        _write_pi_cfg(tmp_paths["pi_cfg"], fallback=False)
        assert oq.should_fallback("impl1") == (False, "", "", False)

    def test_empty_fallback_provider(self, tmp_paths):
        _write_pi_cfg(tmp_paths["pi_cfg"], fallback_provider="")
        assert oq.should_fallback("impl1") == (False, "", "", False)

    def test_below_threshold_no_trigger(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        # percent_left=10 → used=90 < 95
        resp = _mk_response({"rate_limit": {"weekly": {"percent_left": 10}}})
        with patch("urllib.request.urlopen", return_value=resp):
            result = oq.should_fallback("impl1")
        assert result == (False, "", "", False)

    def test_at_threshold_triggers(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        # percent_left=5.0 → used=95.0 >= 95 → trigger
        resp = _mk_response({
            "rate_limit": {"weekly": {"percent_left": 5.0,
                                      "reset_time_ms": int((time.time() + 3600) * 1000)}}
        })
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_pi.reset_session"):
            active, fb_p, fb_m, new = oq.should_fallback("impl1")
        assert (active, fb_p, fb_m, new) == (True, "github-copilot", "gpt-5.4", True)
        cache_file = tmp_paths["cache_dir"] / "impl1.json"
        assert cache_file.exists()
        cache = json.loads(cache_file.read_text())
        assert cache["fallback_provider"] == "github-copilot"
        assert cache["fallback_model"] == "gpt-5.4"
        assert "Codex usage 95% (>=95)" == cache["reason"]

    def test_float_precision_below_threshold(self, tmp_paths):
        """percent_left=5.4 → used=94.6 < 95: must NOT trigger.
        With int(round()) coercion this would round to 95 and falsely trigger."""
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        resp = _mk_response({"rate_limit": {"weekly": {"percent_left": 5.4}}})
        with patch("urllib.request.urlopen", return_value=resp):
            result = oq.should_fallback("impl1")
        assert result == (False, "", "", False)

    def test_cache_hit_skips_http(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"])
        cache_dir = tmp_paths["cache_dir"]
        cache_dir.mkdir(parents=True, exist_ok=True)
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        (cache_dir / "impl1.json").write_text(json.dumps({
            "active": True,
            "fallback_provider": "github-copilot",
            "fallback_model": "gpt-5.4",
            "until": until,
            "reason": "cached",
        }))
        with patch("urllib.request.urlopen") as mock_open:
            active, fb_p, fb_m, new = oq.should_fallback("impl1")
        assert (active, fb_p, fb_m, new) == (True, "github-copilot", "gpt-5.4", False)
        mock_open.assert_not_called()

    def test_invalid_cache_schema_treated_as_miss(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"])
        cache_dir = tmp_paths["cache_dir"]
        cache_dir.mkdir(parents=True, exist_ok=True)
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        # missing fallback_model
        (cache_dir / "impl1.json").write_text(json.dumps({
            "active": True,
            "fallback_provider": "github-copilot",
            "until": until,
        }))
        # below-threshold response so we just verify HTTP gets called (cache miss)
        resp = _mk_response({"rate_limit": {"weekly": {"percent_left": 50}}})
        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            oq.should_fallback("impl1")
        mock_open.assert_called()

    @pytest.mark.parametrize("threshold,expected_threshold", [
        ("abc", 95),
        (-1, 95),
        (200, 95),
    ])
    def test_invalid_threshold_normalizes_to_95(self, tmp_paths, threshold, expected_threshold):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=threshold)
        # used=94 → just below 95 default → no trigger; verifies normalization to 95
        resp = _mk_response({"rate_limit": {"weekly": {"percent_left": 6}}})
        with patch("urllib.request.urlopen", return_value=resp):
            result = oq.should_fallback("impl1")
        assert result == (False, "", "", False)

    def test_clamp_uses_weekly_window(self, tmp_paths):
        """Reset 6 days out should NOT be clamped to 1h (Gemini default) — must use
        max_hrs=168 / default_hrs=6."""
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"])
        future_ms = int((time.time() + 86400 * 6) * 1000)
        resp = _mk_response({
            "rate_limit": {"weekly": {"percent_left": 5, "reset_time_ms": future_ms}}
        })
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_pi.reset_session"):
            oq.should_fallback("impl1")
        cache = json.loads((tmp_paths["cache_dir"] / "impl1.json").read_text())
        until = datetime.fromisoformat(cache["until"])
        # Should be ~6 days out, definitely more than 24h
        assert until - datetime.now(timezone.utc) > timedelta(hours=24)


# ---------------------------------------------------------------------------
# validate_fallback_config
# ---------------------------------------------------------------------------

class TestValidate:
    def test_provider_not_codex_warns(self, tmp_paths):
        tmp_paths["pi_cfg"].write_text(json.dumps({
            "a": {"provider": "github-copilot", "fallback": True,
                  "fallback_provider": "x", "fallback_model": "y"}
        }))
        warns = oq.validate_fallback_config()
        assert any("not 'openai-codex'" in w for w in warns)

    def test_empty_fallback_fields_warn(self, tmp_paths):
        tmp_paths["pi_cfg"].write_text(json.dumps({
            "a": {"provider": "openai-codex", "fallback": True,
                  "fallback_provider": "", "fallback_model": ""}
        }))
        warns = oq.validate_fallback_config()
        assert any("fallback_provider" in w for w in warns)
        assert any("fallback_model" in w for w in warns)

    def test_threshold_out_of_range(self, tmp_paths):
        tmp_paths["pi_cfg"].write_text(json.dumps({
            "a": {"provider": "openai-codex", "fallback": True,
                  "fallback_provider": "x", "fallback_model": "y",
                  "usage_threshold": 200}
        }))
        warns = oq.validate_fallback_config()
        assert any("out of range" in w for w in warns)

    def test_threshold_non_int(self, tmp_paths):
        tmp_paths["pi_cfg"].write_text(json.dumps({
            "a": {"provider": "openai-codex", "fallback": True,
                  "fallback_provider": "x", "fallback_model": "y",
                  "usage_threshold": "abc"}
        }))
        warns = oq.validate_fallback_config()
        assert any("non-integer" in w for w in warns)

    def test_no_fallback_no_warn(self, tmp_paths):
        tmp_paths["pi_cfg"].write_text(json.dumps({
            "a": {"provider": "openai-codex", "fallback": False}
        }))
        assert oq.validate_fallback_config() == []

    def test_skips_comment_key(self, tmp_paths):
        tmp_paths["pi_cfg"].write_text(json.dumps({
            "_comment": {"provider": "doc"},
            "a": {"provider": "openai-codex", "fallback": True,
                  "fallback_provider": "x", "fallback_model": "y"},
        }))
        warns = oq.validate_fallback_config()
        # _comment must not produce warnings
        assert not any("'_comment'" in w for w in warns)
