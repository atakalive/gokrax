"""Tests for engine/openai_codex_quota.py — Codex usage-based fallback."""

from __future__ import annotations

import json
import pathlib
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import config.paths as _paths
from engine import openai_codex_quota as oq
from engine.openai_codex_quota import CodexUsage


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

    def test_pi_expired_falls_through_to_codex(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"], expires_ms=int((time.time() - 60) * 1000))
        _write_codex_auth(tmp_paths["codex_auth"])
        assert oq._load_codex_auth() == ("tok_codex", "acct_codex")

    def test_pi_expired_no_codex_returns_none(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"], expires_ms=int((time.time() - 60) * 1000))
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
        u = oq.get_codex_usage()
        assert u == oq._FAIL_OPEN

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
            u = oq.get_codex_usage()
        assert u.ok is True
        assert u.weekly_used == pytest.approx(95.0)
        assert u.weekly_reset is not None and u.weekly_reset > datetime.now(timezone.utc)
        assert u.primary_used == pytest.approx(20.0)

    def test_field_aliases_rate_limits_secondary_remaining(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({
            "rate_limits": {
                "secondary": {"remaining_percent": 10, "reset_at": "2099-01-01T00:00:00Z"},
            }
        })
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        assert u.ok is True
        assert u.weekly_used == pytest.approx(90.0)
        assert u.weekly_reset == datetime(2099, 1, 1, tzinfo=timezone.utc)

    def test_5h_window_exhausted_parsed_alongside_weekly(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({
            "rate_limit": {
                "five_hour": {"percent_left": 0, "reset_time_ms": int((time.time() + 7200) * 1000)},
                "weekly": {"percent_left": 50, "reset_time_ms": int((time.time() + 3600) * 1000)},
            }
        })
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        assert u.ok is True
        assert u.weekly_used == pytest.approx(50.0)
        assert u.primary_used == pytest.approx(100.0)
        assert u.primary_reset is not None

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
            u = oq.get_codex_usage()
        assert u.ok is True
        assert u.weekly_used == pytest.approx(expected_used)

    @pytest.mark.parametrize("raw", ["abc", None, float("nan"), float("inf")])
    def test_percent_left_invalid_fail_open(self, tmp_paths, raw):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {"weekly": {"percent_left": raw}}})
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        assert u == oq._FAIL_OPEN

    def test_reset_at_iso_naive_assumes_utc(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({
            "rate_limit": {"weekly": {"percent_left": 50, "reset_at": "2099-06-15T12:00:00"}}
        })
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        assert u.weekly_reset == datetime(2099, 6, 15, 12, 0, tzinfo=timezone.utc)

    def test_no_reset_fields_returns_none(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {"weekly": {"percent_left": 50}}})
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        assert u.weekly_reset is None

    def test_http_error_fail_open(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            u = oq.get_codex_usage()
        assert u == oq._FAIL_OPEN

    def test_new_schema_secondary_window_used_percent(self, tmp_paths):
        """New API schema: rate_limit.secondary_window.used_percent (direct used %),
        reset_at as Unix seconds. Mirrors the actual chatgpt.com response shape."""
        _write_pi_auth(tmp_paths["pi_auth"])
        reset_sec = int(time.time() + 86400 * 2)
        resp = _mk_response({
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 29,
                    "limit_window_seconds": 18000,
                    "reset_at": int(time.time() + 3600),
                },
                "secondary_window": {
                    "used_percent": 85,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 195583,
                    "reset_at": reset_sec,
                },
            },
        })
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        assert u.ok is True
        assert u.weekly_used == pytest.approx(85.0)
        assert u.weekly_reset == datetime.fromtimestamp(reset_sec, tz=timezone.utc)
        assert u.primary_used == pytest.approx(29.0)
        assert u.limit_reached is False

    def test_new_schema_reset_after_seconds(self, tmp_paths):
        """reset_after_seconds is used when reset_at absent."""
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({
            "rate_limit": {
                "secondary_window": {
                    "used_percent": 90,
                    "reset_after_seconds": 3600,
                },
            },
        })
        before = datetime.now(timezone.utc)
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        after = datetime.now(timezone.utc)
        assert u.ok is True
        assert u.weekly_used == pytest.approx(90.0)
        assert u.weekly_reset is not None
        assert before + timedelta(seconds=3590) <= u.weekly_reset <= after + timedelta(seconds=3610)

    def test_new_schema_used_percent_takes_precedence_over_legacy(self, tmp_paths):
        """When both used_percent and percent_left are present (mixed shape), the
        direct used_percent wins."""
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({
            "rate_limit": {
                "secondary_window": {"used_percent": 77, "percent_left": 99},
            },
        })
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        assert u.ok is True
        assert u.weekly_used == pytest.approx(77.0)

    def test_secondary_window_preferred_over_legacy_weekly(self, tmp_paths):
        """When both secondary_window (new) and weekly (legacy) coexist, new wins."""
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({
            "rate_limit": {
                "secondary_window": {"used_percent": 70},
                "weekly": {"percent_left": 50},
            },
        })
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        assert u.ok is True
        assert u.weekly_used == pytest.approx(70.0)

    def test_silent_paths_now_log(self, tmp_paths):
        """Each formerly-silent shape-mismatch branch must emit a WARN log line."""
        _write_pi_auth(tmp_paths["pi_auth"])
        # missing window key entirely
        resp = _mk_response({"rate_limit": {"unknown": {}}})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.openai_codex_quota.log") as mock_log:
            u = oq.get_codex_usage()
        assert u == oq._FAIL_OPEN
        assert mock_log.called
        msg = mock_log.call_args[0][0]
        assert "no usage window found" in msg
        assert "unknown" in msg

    @pytest.mark.parametrize("flags,expected", [
        ({"allowed": False}, True),
        ({"limit_reached": True}, True),
        ({"allowed": "false"}, False),
        ({"allowed": 0}, False),
        ({"limit_reached": "true"}, False),
        ({"limit_reached": 1}, False),
        ({"allowed": True, "limit_reached": False}, False),
    ])
    def test_limit_reached_flags_strict_identity(self, tmp_paths, flags, expected):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {**flags, "secondary_window": {"used_percent": 10}}})
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        assert u.ok is True
        assert u.limit_reached is expected

    def test_allowed_false_only_no_windows_is_ok(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {"allowed": False}})
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        assert u == CodexUsage(ok=True, limit_reached=True)

    @pytest.mark.parametrize("weekly", [
        {"used_percent": "abc"},
        {"used_percent": 10**400},
        {"used_percent": float("nan")},
        {"percent_left": None},
        {"used_percent": True},
        {"percent_left": False},
    ])
    def test_invalid_weekly_window_warns_but_primary_still_parsed(self, tmp_paths, weekly):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {
            "secondary_window": weekly,
            "primary_window": {"used_percent": 100, "reset_at": int(time.time() + 3600)},
        }})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.openai_codex_quota.log") as mock_log:
            u = oq.get_codex_usage()
        msgs = [c.args[0] for c in mock_log.call_args_list]
        assert u.ok is True
        assert u.weekly_used is None
        assert u.primary_used == pytest.approx(100.0)
        assert any("weekly window:" in m for m in msgs)
        assert not any("no usage window found" in m for m in msgs)

    def test_bool_percent_in_5h_window_is_rejected(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {
            "primary_window": {"percent_left": False},
            "secondary_window": {"used_percent": 10},
        }})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.openai_codex_quota.log") as mock_log:
            u = oq.get_codex_usage()
        msgs = [c.args[0] for c in mock_log.call_args_list]
        assert u.primary_used is None
        assert oq._evaluate(u, 95) == ([], [])
        assert any("5h window: no usable percent" in m for m in msgs)

    def test_non_dict_window_is_ignored_with_warn(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {
            "secondary_window": ["x"],
            "primary_window": {"used_percent": 30},
        }})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.openai_codex_quota.log") as mock_log:
            u = oq.get_codex_usage()
        msgs = [c.args[0] for c in mock_log.call_args_list]
        assert u.ok is True
        assert u.weekly_used is None
        assert u.primary_used == pytest.approx(30.0)
        assert any("weekly window: not a dict" in m for m in msgs)

    @pytest.mark.parametrize("bad", [0, "", []])
    def test_falsy_window_value_is_present_and_warns(self, tmp_paths, bad):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {
            "secondary_window": bad,
            "primary_window": {"used_percent": 30},
        }})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.openai_codex_quota.log") as mock_log:
            u = oq.get_codex_usage()
        msgs = [c.args[0] for c in mock_log.call_args_list]
        assert u.ok is True
        assert u.weekly_used is None
        assert u.primary_used == pytest.approx(30.0)
        assert any("weekly window: not a dict" in m for m in msgs)

    def test_empty_window_dict_is_present_and_warns(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {
            "primary_window": {},
            "secondary_window": {"used_percent": 10},
        }})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.openai_codex_quota.log") as mock_log:
            u = oq.get_codex_usage()
        msgs = [c.args[0] for c in mock_log.call_args_list]
        assert u.ok is True
        assert u.primary_used is None
        assert u.weekly_used == pytest.approx(10.0)
        assert any("5h window: no usable percent" in m for m in msgs)
        assert not any("no usage window found" in m for m in msgs)

    def test_null_window_falls_through_to_legacy_alias(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {
            "secondary_window": None,
            "weekly": {"percent_left": 40},
        }})
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        assert u.weekly_used == pytest.approx(60.0)

    @pytest.mark.parametrize("rl", [
        {"secondary_window": {"used_percent": "x"}, "primary_window": {"used_percent": "y"}},
        {"secondary_window": {}, "primary_window": {}},
    ])
    def test_both_windows_invalid_no_flag_fail_open(self, tmp_paths, rl):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": rl})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.openai_codex_quota.log") as mock_log:
            u = oq.get_codex_usage()
        msgs = [c.args[0] for c in mock_log.call_args_list]
        assert u == oq._FAIL_OPEN
        assert any("weekly window:" in m for m in msgs)
        assert any("5h window:" in m for m in msgs)
        assert not any("no usage window found" in m for m in msgs)

    @pytest.mark.parametrize("left,expected_reasons", [
        (1e-15, []),
        (0, ["Codex usage 100% (>=100)", "Codex 5h window exhausted (100%)"]),
    ])
    def test_tiny_positive_percent_left_is_not_exhausted(self, tmp_paths, left, expected_reasons):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {
            "primary_window": {"percent_left": left},
            "secondary_window": {"percent_left": left},
        }})
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        if left > 0:
            assert u.primary_used < 100.0
            assert u.weekly_used < 100.0
        else:
            assert u.primary_used == 100.0
        assert oq._evaluate(u, 100)[0] == expected_reasons

    def test_primary_window_preferred_over_legacy_five_hour(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        resp = _mk_response({"rate_limit": {
            "secondary_window": {"used_percent": 10},
            "primary_window": {"used_percent": 40},
            "five_hour": {"percent_left": 0},
        }})
        with patch("urllib.request.urlopen", return_value=resp):
            u = oq.get_codex_usage()
        assert u.primary_used == pytest.approx(40.0)

    def test_new_schema_at_euler_threshold_triggers(self, tmp_paths):
        """Integration: real-world euler scenario — secondary_window.used_percent=85,
        threshold=80 → fallback fires and cache is written."""
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=80,
                      fallback_provider="github-copilot", fallback_model="gpt-5.4")
        resp = _mk_response({
            "rate_limit": {
                "secondary_window": {
                    "used_percent": 85,
                    "reset_at": int(time.time() + 86400 * 2),
                },
            },
        })
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_pi.reset_session"):
            active, fb_p, fb_m, new = oq.should_fallback("impl1")
        assert (active, fb_p, fb_m, new) == (True, "github-copilot", "gpt-5.4", True)
        cache = json.loads((tmp_paths["cache_dir"] / "impl1.json").read_text())
        assert cache["reason"] == "Codex usage 85% (>=80)"


# ---------------------------------------------------------------------------
# _evaluate (pure)
# ---------------------------------------------------------------------------

_R_WEEKLY = datetime(2099, 1, 8, tzinfo=timezone.utc)
_R_5H = datetime(2099, 1, 1, 5, tzinfo=timezone.utc)
_FLAG = "Codex limit reached (allowed/limit_reached flag)"


class TestEvaluate:
    def test_weekly_at_threshold_fires(self):
        u = CodexUsage(ok=True, weekly_used=90.0, weekly_reset=_R_WEEKLY)
        assert oq._evaluate(u, 90) == (["Codex usage 90% (>=90)"], [_R_WEEKLY])

    def test_primary_exhausted_fires(self):
        u = CodexUsage(ok=True, primary_used=100.0, primary_reset=_R_5H)
        assert oq._evaluate(u, 90) == (["Codex 5h window exhausted (100%)"], [_R_5H])

    def test_primary_95_does_not_fire(self):
        u = CodexUsage(ok=True, primary_used=95.0, primary_reset=_R_5H)
        assert oq._evaluate(u, 90) == ([], [])

    def test_flag_only_uses_primary_reset(self):
        u = CodexUsage(ok=True, weekly_used=10.0, weekly_reset=_R_WEEKLY,
                       primary_used=50.0, primary_reset=_R_5H, limit_reached=True)
        assert oq._evaluate(u, 90) == ([_FLAG], [_R_5H])

    def test_flag_only_without_resets_yields_none_candidate(self):
        u = CodexUsage(ok=True, limit_reached=True)
        assert oq._evaluate(u, 90) == ([_FLAG], [None])

    def test_flag_with_window_hit_adds_no_candidate(self):
        u = CodexUsage(ok=True, primary_used=100.0, primary_reset=_R_5H, limit_reached=True)
        assert oq._evaluate(u, 90) == (["Codex 5h window exhausted (100%)", _FLAG], [_R_5H])

    def test_both_windows_fire_yield_two_candidates(self):
        u = CodexUsage(ok=True, weekly_used=95.0, weekly_reset=_R_WEEKLY,
                       primary_used=100.0, primary_reset=_R_5H)
        assert oq._evaluate(u, 90) == (
            ["Codex usage 95% (>=90)", "Codex 5h window exhausted (100%)"],
            [_R_WEEKLY, _R_5H],
        )

    def test_weekly_none_skips_threshold_check(self):
        u = CodexUsage(ok=True, primary_used=10.0)
        assert oq._evaluate(u, 0) == ([], [])

    def test_nothing_fires(self):
        u = CodexUsage(ok=True, weekly_used=50.0, primary_used=50.0)
        assert oq._evaluate(u, 95) == ([], [])


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

    def test_5h_window_exhausted_fires_even_when_weekly_fine(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        p_ms = int((time.time() + 7200) * 1000)
        resp = _mk_response({"rate_limit": {
            "five_hour": {"percent_left": 0, "reset_time_ms": p_ms},
            "weekly": {"percent_left": 50, "reset_time_ms": int((time.time() + 86400 * 3) * 1000)},
        }})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_pi.reset_session") as mock_reset:
            result = oq.should_fallback("impl1")
        assert result == (True, "github-copilot", "gpt-5.4", True)
        cache = json.loads((tmp_paths["cache_dir"] / "impl1.json").read_text())
        assert cache["reason"] == "Codex 5h window exhausted (100%)"
        until = datetime.fromisoformat(cache["until"])
        expected = datetime.fromtimestamp(p_ms / 1000.0, tz=timezone.utc)
        assert abs(until - expected) <= timedelta(seconds=1)
        mock_reset.assert_called_once_with("impl1")

    def test_incident_replay_primary_100_weekly_68_threshold_90(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=90)
        p = int(time.time() + 7200)
        s = int(time.time() + 86400 * 5)
        resp = _mk_response({"rate_limit": {
            "allowed": False,
            "limit_reached": True,
            "primary_window": {"used_percent": 100, "limit_window_seconds": 18000, "reset_at": p},
            "secondary_window": {"used_percent": 68, "limit_window_seconds": 604800, "reset_at": s},
        }})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_pi.reset_session") as mock_reset:
            result = oq.should_fallback("impl1")
        assert result == (True, "github-copilot", "gpt-5.4", True)
        cache = json.loads((tmp_paths["cache_dir"] / "impl1.json").read_text())
        assert cache["reason"] == (
            "Codex 5h window exhausted (100%); Codex limit reached (allowed/limit_reached flag)"
        )
        assert datetime.fromisoformat(cache["until"]) == datetime.fromtimestamp(p, tz=timezone.utc)
        mock_reset.assert_called_once_with("impl1")
        assert cache["clear_on_revert"] is True

    def test_primary_95_weekly_68_no_fire(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=90)
        resp = _mk_response({"rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {"used_percent": 95},
            "secondary_window": {"used_percent": 68},
        }})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_pi.reset_session") as mock_reset:
            result = oq.should_fallback("impl1")
        assert result == (False, "", "", False)
        assert not (tmp_paths["cache_dir"] / "impl1.json").exists()
        mock_reset.assert_not_called()

    def test_allowed_false_without_percent_fires(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        resp = _mk_response({"rate_limit": {"allowed": False}})
        now = datetime.now(timezone.utc)
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_pi.reset_session"):
            result = oq.should_fallback("impl1")
        assert result == (True, "github-copilot", "gpt-5.4", True)
        cache = json.loads((tmp_paths["cache_dir"] / "impl1.json").read_text())
        assert cache["reason"] == "Codex limit reached (allowed/limit_reached flag)"
        until = datetime.fromisoformat(cache["until"])
        assert timedelta(hours=5, minutes=59) <= until - now <= timedelta(hours=6, minutes=1)

    def test_weekly_over_threshold_primary_low_uses_weekly_reason_and_reset(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=90)
        s = int(time.time() + 86400 * 5)
        resp = _mk_response({"rate_limit": {
            "primary_window": {"used_percent": 50, "reset_at": int(time.time() + 3600)},
            "secondary_window": {"used_percent": 95, "reset_at": s},
        }})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_pi.reset_session"):
            oq.should_fallback("impl1")
        cache = json.loads((tmp_paths["cache_dir"] / "impl1.json").read_text())
        assert cache["reason"] == "Codex usage 95% (>=90)"
        assert datetime.fromisoformat(cache["until"]) == datetime.fromtimestamp(s, tz=timezone.utc)

    def test_both_windows_fire_until_is_later_reset(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=90)
        p = int(time.time() + 7200)
        s = int(time.time() + 86400 * 5)
        resp = _mk_response({"rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {"used_percent": 100, "reset_at": p},
            "secondary_window": {"used_percent": 95, "reset_at": s},
        }})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_pi.reset_session"):
            oq.should_fallback("impl1")
        cache = json.loads((tmp_paths["cache_dir"] / "impl1.json").read_text())
        assert cache["reason"] == "Codex usage 95% (>=90); Codex 5h window exhausted (100%)"
        assert datetime.fromisoformat(cache["until"]) == datetime.fromtimestamp(s, tz=timezone.utc)

    def test_primary_reset_in_past_clamped_to_min_window(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        resp = _mk_response({"rate_limit": {
            "primary_window": {"used_percent": 100, "reset_at": int(time.time() - 60)},
            "secondary_window": {"used_percent": 10},
        }})
        now = datetime.now(timezone.utc)
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_pi.reset_session"):
            result = oq.should_fallback("impl1")
        assert result[0] is True
        cache = json.loads((tmp_paths["cache_dir"] / "impl1.json").read_text())
        until = datetime.fromisoformat(cache["until"])
        assert timedelta(minutes=4) <= until - now <= timedelta(minutes=6)

    def test_activation_logs_reason(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        p = int(time.time() + 7200)
        resp = _mk_response({"rate_limit": {
            "primary_window": {"used_percent": 100, "reset_at": p},
            "secondary_window": {"used_percent": 10},
        }})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_pi.reset_session"), \
             patch("engine.openai_codex_quota.log") as mock_log:
            result = oq.should_fallback("impl1")
        assert result[3] is True
        msgs = [c.args[0] for c in mock_log.call_args_list]
        assert any(
            "fallback activated" in m and "mode=A" in m and "Codex 5h window exhausted (100%)" in m
            for m in msgs
        )


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

    def test_mode_b_backend_only_ok(self, tmp_paths):
        """Mode B: fallback_backend='cc' alone is valid; no provider/model warns."""
        tmp_paths["pi_cfg"].write_text(json.dumps({
            "a": {"provider": "openai-codex", "fallback": True,
                  "fallback_backend": "cc", "usage_threshold": 95}
        }))
        warns = oq.validate_fallback_config()
        assert not any("fallback_provider" in w for w in warns)
        assert not any("fallback_model" in w for w in warns)
        assert warns == []

    def test_mode_b_invalid_backend_warns(self, tmp_paths):
        """Invalid fallback_backend → WARN, treated as Mode A."""
        tmp_paths["pi_cfg"].write_text(json.dumps({
            "a": {"provider": "openai-codex", "fallback": True,
                  "fallback_backend": "gemini",
                  "fallback_provider": "x", "fallback_model": "y"}
        }))
        warns = oq.validate_fallback_config()
        assert any("invalid fallback_backend" in w for w in warns)

    def test_mode_b_missing_provider_model_warns(self, tmp_paths):
        """Mode A (no fallback_backend) with missing provider/model → WARN."""
        tmp_paths["pi_cfg"].write_text(json.dumps({
            "a": {"provider": "openai-codex", "fallback": True}
        }))
        warns = oq.validate_fallback_config()
        assert any("fallback_provider" in w for w in warns)
        assert any("fallback_model" in w for w in warns)

    def test_mode_b_without_fallback_true_warns(self, tmp_paths):
        """fallback_backend valid but fallback not true → WARN."""
        tmp_paths["pi_cfg"].write_text(json.dumps({
            "a": {"provider": "openai-codex", "fallback_backend": "cc"}
        }))
        warns = oq.validate_fallback_config()
        assert any("fallback is not true" in w for w in warns)


# ---------------------------------------------------------------------------
# is_mode_b_backend
# ---------------------------------------------------------------------------

class TestIsModeBBackend:
    @pytest.mark.parametrize("value,expected", [
        ("cc", True),
        ("openclaw", True),
        ("pi", False),
        ("gemini", False),
        ("agy", False),
        ("kimi", False),
        ("", False),
        ("foo", False),
        (None, False),
        (123, False),
    ])
    def test_is_mode_b_backend(self, value, expected):
        assert oq.is_mode_b_backend(value) is expected


# ---------------------------------------------------------------------------
# Mode B: should_fallback / cache / resolve_fallback_backend
# ---------------------------------------------------------------------------

class TestModeB:
    def _write_mode_b_cfg(self, path, agent="impl1", **fields):
        cfg = {
            agent: {
                "provider": "openai-codex",
                "model": "gpt-5.4",
                "fallback": True,
                "fallback_backend": "cc",
                "usage_threshold": 95,
                **fields,
            }
        }
        path.write_text(json.dumps(cfg))

    def test_should_fallback_mode_b_triggers(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        self._write_mode_b_cfg(tmp_paths["pi_cfg"])
        resp = _mk_response({
            "rate_limit": {"weekly": {"percent_left": 5.0,
                                      "reset_time_ms": int((time.time() + 3600) * 1000)}}
        })
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_cc.reset_session") as mock_cc_reset, \
             patch("engine.backend_pi.reset_session") as mock_pi_reset:
            result = oq.should_fallback("impl1")
        assert result == (True, "cc", "", True)
        # Mode B resets the redirect target (cc), not pi.
        mock_cc_reset.assert_called_once_with("impl1")
        mock_pi_reset.assert_not_called()
        cache = json.loads((tmp_paths["cache_dir"] / "impl1.json").read_text())
        assert cache["fallback_backend"] == "cc"
        assert "fallback_provider" not in cache

    def test_should_fallback_mode_b_cache_hit(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        self._write_mode_b_cfg(tmp_paths["pi_cfg"])
        cache_dir = tmp_paths["cache_dir"]
        cache_dir.mkdir(parents=True, exist_ok=True)
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        (cache_dir / "impl1.json").write_text(json.dumps({
            "active": True, "fallback_backend": "cc", "until": until, "reason": "cached",
        }))
        with patch("urllib.request.urlopen") as mock_open:
            result = oq.should_fallback("impl1")
        assert result == (True, "cc", "", False)
        mock_open.assert_not_called()

    def test_should_fallback_mode_a_still_works(self, tmp_paths):
        """Mode A profile (provider/model, no fallback_backend) unchanged."""
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        resp = _mk_response({
            "rate_limit": {"weekly": {"percent_left": 5.0,
                                      "reset_time_ms": int((time.time() + 3600) * 1000)}}
        })
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_pi.reset_session"):
            result = oq.should_fallback("impl1")
        assert result == (True, "github-copilot", "gpt-5.4", True)

    def test_cache_active_mode_b(self, tmp_paths):
        until_future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        until_past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        # active
        assert oq._cache_active_mode_b({
            "active": True, "fallback_backend": "cc", "until": until_future,
        }) is True
        # inactive flag
        assert oq._cache_active_mode_b({
            "active": False, "fallback_backend": "cc", "until": until_future,
        }) is False
        # expired
        assert oq._cache_active_mode_b({
            "active": True, "fallback_backend": "cc", "until": until_past,
        }) is False
        # fallback_backend == "pi" is not a Mode B target
        assert oq._cache_active_mode_b({
            "active": True, "fallback_backend": "pi", "until": until_future,
        }) is False
        # missing
        assert oq._cache_active_mode_b(None) is False

    def test_resolve_fallback_backend(self, tmp_paths):
        cache_dir = tmp_paths["cache_dir"]
        cache_dir.mkdir(parents=True, exist_ok=True)
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        cache_file = cache_dir / "impl1.json"
        # active → "cc"
        cache_file.write_text(json.dumps({
            "active": True, "fallback_backend": "cc", "until": until,
        }))
        assert oq.resolve_fallback_backend("impl1") == "cc"
        # inactive → ""
        cache_file.write_text(json.dumps({
            "active": False, "fallback_backend": "cc", "until": until,
        }))
        assert oq.resolve_fallback_backend("impl1") == ""
        # fallback_backend == "pi" → ""
        cache_file.write_text(json.dumps({
            "active": True, "fallback_backend": "pi", "until": until,
        }))
        assert oq.resolve_fallback_backend("impl1") == ""
        # no cache → ""
        cache_file.unlink()
        assert oq.resolve_fallback_backend("impl1") == ""

    def test_resolve_fallback_backend_exception(self, tmp_paths, monkeypatch):
        monkeypatch.setattr(oq.fallback_cache, "read_cache",
                            lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        assert oq.resolve_fallback_backend("impl1") == ""

    def test_should_fallback_mode_b_5h_exhausted_resets_cc(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        self._write_mode_b_cfg(tmp_paths["pi_cfg"])
        resp = _mk_response({"rate_limit": {
            "primary_window": {"used_percent": 100, "reset_at": int(time.time() + 7200)},
            "secondary_window": {"used_percent": 10},
        }})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("engine.backend_cc.reset_session") as mock_cc_reset, \
             patch("engine.backend_pi.reset_session") as mock_pi_reset:
            result = oq.should_fallback("impl1")
        assert result == (True, "cc", "", True)
        mock_cc_reset.assert_called_once_with("impl1")
        mock_pi_reset.assert_not_called()
        cache = json.loads((tmp_paths["cache_dir"] / "impl1.json").read_text())
        assert cache["fallback_backend"] == "cc"
        assert cache["reason"] == "Codex 5h window exhausted (100%)"


# ---------------------------------------------------------------------------
# Session continuity on re-fire
# ---------------------------------------------------------------------------

class TestSessionContinuity:
    _FIRE = {
        "primary_window": {"used_percent": 100, "reset_at": int(time.time() + 7200)},
        "secondary_window": {"used_percent": 10},
    }
    _NOFIRE = {
        "primary_window": {"used_percent": 10},
        "secondary_window": {"used_percent": 10},
    }

    @staticmethod
    def _write_stale(tmp_paths, **fields):
        cache_dir = tmp_paths["cache_dir"]
        cache_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "active": True,
            "fallback_provider": "github-copilot",
            "fallback_model": "gpt-5.4",
            "until": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            "reason": "old",
            "clear_on_revert": True,
        }
        data.update(fields)
        data = {k: v for k, v in data.items() if v is not None}
        (cache_dir / "impl1.json").write_text(json.dumps(data))

    @staticmethod
    def _expire(tmp_paths):
        path = tmp_paths["cache_dir"] / "impl1.json"
        data = json.loads(path.read_text())
        data["until"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        path.write_text(json.dumps(data))

    @staticmethod
    def _read_cache(tmp_paths):
        return json.loads((tmp_paths["cache_dir"] / "impl1.json").read_text())

    def test_flag_only_weekly_origin_refire_does_not_reset_session(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=100)
        p = int(time.time() + 7200)
        s = int(time.time() + 86400 * 5)
        rl = {
            "allowed": False,
            "primary_window": {"used_percent": 50, "reset_at": p},
            "secondary_window": {"used_percent": 99.9, "reset_at": s},
        }
        with patch("engine.backend_pi.reset_session") as mock_reset, \
             patch("engine.backend_cc.reset_session"), \
             patch("engine.openai_codex_quota.log") as mock_log:
            with patch("urllib.request.urlopen", return_value=_mk_response({"rate_limit": rl})):
                first = oq.should_fallback("impl1")
            assert first == (True, "github-copilot", "gpt-5.4", True)
            cache = self._read_cache(tmp_paths)
            assert cache["reason"] == "Codex limit reached (allowed/limit_reached flag)"
            assert datetime.fromisoformat(cache["until"]) == datetime.fromtimestamp(p, tz=timezone.utc)
            assert mock_reset.call_count == 1
            assert any("fallback activated" in c.args[0] for c in mock_log.call_args_list)

            self._expire(tmp_paths)
            mock_reset.reset_mock()
            mock_log.reset_mock()
            with patch("urllib.request.urlopen", return_value=_mk_response({"rate_limit": rl})):
                second = oq.should_fallback("impl1")
        assert second == (True, "github-copilot", "gpt-5.4", False)
        mock_reset.assert_not_called()
        cache = self._read_cache(tmp_paths)
        assert datetime.fromisoformat(cache["until"]) == datetime.fromtimestamp(p, tz=timezone.utc)
        assert any(
            "fallback renewed" in c.args[0] and "mode=A" in c.args[0]
            for c in mock_log.call_args_list
        )

    def test_past_reset_refire_does_not_reset_session(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        rl = {
            "primary_window": {"used_percent": 100, "reset_at": int(time.time() - 60)},
            "secondary_window": {"used_percent": 10},
        }
        with patch("engine.backend_pi.reset_session") as mock_reset, \
             patch("engine.backend_cc.reset_session"), \
             patch("engine.openai_codex_quota.log"):
            with patch("urllib.request.urlopen", return_value=_mk_response({"rate_limit": rl})):
                oq.should_fallback("impl1")
            assert mock_reset.call_count == 1
            self._expire(tmp_paths)
            mock_reset.reset_mock()
            with patch("urllib.request.urlopen", return_value=_mk_response({"rate_limit": rl})):
                second = oq.should_fallback("impl1")
        assert second[3] is False
        mock_reset.assert_not_called()

    def test_no_fire_decision_clears_stale_cache_then_next_fire_resets(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        self._write_stale(tmp_paths)
        with patch("engine.backend_pi.reset_session") as mock_reset, \
             patch("engine.backend_cc.reset_session"), \
             patch("engine.openai_codex_quota.log"):
            with patch("urllib.request.urlopen",
                       return_value=_mk_response({"rate_limit": self._NOFIRE})):
                first = oq.should_fallback("impl1")
            assert first == (False, "", "", False)
            assert not (tmp_paths["cache_dir"] / "impl1.json").exists()
            mock_reset.assert_not_called()
            with patch("urllib.request.urlopen",
                       return_value=_mk_response({"rate_limit": self._FIRE})):
                second = oq.should_fallback("impl1")
        assert second == (True, "github-copilot", "gpt-5.4", True)
        mock_reset.assert_called_once_with("impl1")

    def test_fail_open_clears_stale_cache(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        self._write_stale(tmp_paths)
        with patch("engine.backend_pi.reset_session"), \
             patch("engine.backend_cc.reset_session"), \
             patch("engine.openai_codex_quota.log"), \
             patch("urllib.request.urlopen", side_effect=Exception("boom")):
            result = oq.should_fallback("impl1")
        assert result == (False, "", "", False)
        assert not (tmp_paths["cache_dir"] / "impl1.json").exists()

    def test_stale_cache_for_different_target_resets(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        self._write_stale(tmp_paths, fallback_model="other-model")
        with patch("engine.backend_pi.reset_session") as mock_reset, \
             patch("engine.backend_cc.reset_session"), \
             patch("engine.openai_codex_quota.log"), \
             patch("urllib.request.urlopen",
                   return_value=_mk_response({"rate_limit": self._FIRE})):
            result = oq.should_fallback("impl1")
        assert result[3] is True
        mock_reset.assert_called_once_with("impl1")

    def test_stale_cache_without_marker_resets(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        self._write_stale(tmp_paths, clear_on_revert=None)
        with patch("engine.backend_pi.reset_session") as mock_reset, \
             patch("engine.backend_cc.reset_session"), \
             patch("engine.openai_codex_quota.log"), \
             patch("urllib.request.urlopen",
                   return_value=_mk_response({"rate_limit": self._FIRE})):
            result = oq.should_fallback("impl1")
        assert result[3] is True
        mock_reset.assert_called_once_with("impl1")

    def test_clear_stale_cache_keeps_active_cache(self, tmp_paths):
        self._write_stale(
            tmp_paths,
            until=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        with patch("engine.backend_pi.reset_session"), \
             patch("engine.backend_cc.reset_session"), \
             patch("engine.openai_codex_quota.log"):
            oq._clear_stale_cache("impl1", {"stale": True})
        assert (tmp_paths["cache_dir"] / "impl1.json").exists()

    def test_clear_failure_is_logged_and_keeps_no_fire(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        self._write_stale(tmp_paths)
        with patch("engine.backend_pi.reset_session") as mock_reset, \
             patch("engine.backend_cc.reset_session"), \
             patch("engine.openai_codex_quota.log") as mock_log:
            with patch.object(pathlib.Path, "unlink", side_effect=OSError("denied")), \
                 patch("urllib.request.urlopen",
                       return_value=_mk_response({"rate_limit": self._NOFIRE})):
                first = oq.should_fallback("impl1")
            assert first == (False, "", "", False)
            assert any(
                "clear stale cache(impl1) failed" in c.args[0] for c in mock_log.call_args_list
            )
            assert (tmp_paths["cache_dir"] / "impl1.json").exists()
            with patch("urllib.request.urlopen",
                       return_value=_mk_response({"rate_limit": self._FIRE})):
                second = oq.should_fallback("impl1")
        assert second == (True, "github-copilot", "gpt-5.4", False)
        mock_reset.assert_not_called()

    def test_concurrent_activation_during_no_fire_is_kept_and_later_continues(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_pi_cfg(tmp_paths["pi_cfg"], usage_threshold=95)
        self._write_stale(tmp_paths)

        def racing():
            # Another process fires while this one decides "no fallback".
            self._write_stale(
                tmp_paths,
                until=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            )
            return CodexUsage(ok=True, weekly_used=10.0, primary_used=10.0)

        with patch("engine.backend_pi.reset_session") as mock_reset, \
             patch("engine.backend_cc.reset_session"), \
             patch("engine.openai_codex_quota.log"):
            with patch("engine.openai_codex_quota.get_codex_usage", side_effect=racing):
                first = oq.should_fallback("impl1")
            assert first == (False, "", "", False)
            assert (tmp_paths["cache_dir"] / "impl1.json").exists()
            assert oq._cache_active(self._read_cache(tmp_paths)) is True
            mock_reset.assert_not_called()
            self._expire(tmp_paths)
            with patch("urllib.request.urlopen",
                       return_value=_mk_response({"rate_limit": self._FIRE})):
                second = oq.should_fallback("impl1")
        assert second == (True, "github-copilot", "gpt-5.4", False)
        mock_reset.assert_not_called()

    def test_mode_b_refire_does_not_reset_cc(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        tmp_paths["pi_cfg"].write_text(json.dumps({
            "impl1": {
                "provider": "openai-codex",
                "model": "gpt-5.4",
                "fallback": True,
                "fallback_backend": "cc",
                "usage_threshold": 95,
            }
        }))
        self._write_stale(tmp_paths, fallback_provider=None, fallback_model=None,
                          fallback_backend="cc")
        with patch("engine.backend_pi.reset_session") as mock_reset, \
             patch("engine.backend_cc.reset_session") as mock_cc_reset, \
             patch("engine.openai_codex_quota.log"), \
             patch("urllib.request.urlopen",
                   return_value=_mk_response({"rate_limit": self._FIRE})):
            result = oq.should_fallback("impl1")
        assert result == (True, "cc", "", False)
        mock_cc_reset.assert_not_called()
        mock_reset.assert_not_called()
        cache = self._read_cache(tmp_paths)
        assert cache["fallback_backend"] == "cc"
        assert cache["clear_on_revert"] is True


# ---------------------------------------------------------------------------
# Split auth loaders (#398)
# ---------------------------------------------------------------------------

class TestSplitAuthLoaders:
    def test_codex_cli_auth_ignores_pi_auth(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        _write_codex_auth(tmp_paths["codex_auth"])
        assert oq._load_codex_cli_auth() == ("tok_codex", "acct_codex")

    def test_codex_cli_auth_none_when_only_pi(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        assert oq._load_codex_cli_auth() is None

    def test_pi_codex_auth_standalone(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"])
        assert oq._load_pi_codex_auth() == ("tok_pi", "acct_pi")

    def test_pi_codex_auth_none_when_only_codex(self, tmp_paths):
        _write_codex_auth(tmp_paths["codex_auth"])
        assert oq._load_pi_codex_auth() is None

    def test_pi_codex_auth_expired(self, tmp_paths):
        _write_pi_auth(tmp_paths["pi_auth"], expires_ms=int((time.time() - 100) * 1000))
        assert oq._load_pi_codex_auth() is None


class TestFetchCodexUsage:
    def test_uses_given_credentials(self, tmp_paths):
        data = {"rate_limit": {"secondary_window": {"used_percent": 42},
                               "primary_window": {"used_percent": 7}}}
        with patch("urllib.request.urlopen", return_value=_mk_response(data)) as mock_urlopen:
            usage = oq._fetch_codex_usage("tok_x", "acct_x")
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer tok_x"
        assert req.get_header("Chatgpt-account-id") == "acct_x"
        assert usage.ok is True
        assert usage.weekly_used == 42.0
        assert usage.primary_used == 7.0

    def test_http_error_returns_fail_open(self, tmp_paths):
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert oq._fetch_codex_usage("tok_x", "acct_x") == CodexUsage(ok=False)
