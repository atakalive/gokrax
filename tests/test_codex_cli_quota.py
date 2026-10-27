"""Tests for engine/codex_cli_quota.py — codex backend quota fallback (#398)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import config
import config.paths as _paths
import engine.backend as backend
from engine import backend_pi
from engine import codex_cli_quota as cq
from engine.backend_types import SendResult
from engine.openai_codex_quota import CodexUsage


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    pi_auth = tmp_path / "pi_auth.json"
    codex_auth = tmp_path / "codex_auth.json"
    cache_dir = tmp_path / "quota-cache-codex-cli"
    codex_cfg = tmp_path / "config_codex.json"
    pi_cfg = tmp_path / "config_pi.json"
    monkeypatch.setattr(_paths, "PI_AUTH_FILE", pi_auth)
    monkeypatch.setattr(_paths, "CODEX_AUTH_FILE", codex_auth)
    monkeypatch.setattr(_paths, "CODEX_CLI_QUOTA_CACHE_DIR", cache_dir)
    monkeypatch.setattr(_paths, "CODEX_AGENT_CONFIG", codex_cfg)
    monkeypatch.setattr(_paths, "PI_AGENT_CONFIG", pi_cfg)
    return {
        "pi_auth": pi_auth,
        "codex_auth": codex_auth,
        "cache_dir": cache_dir,
        "codex_cfg": codex_cfg,
        "pi_cfg": pi_cfg,
        "tmp_path": tmp_path,
    }


@pytest.fixture(autouse=True)
def _fallback_resets():
    # This module is exempt from the global mocks: never touch real pi/cc sessions.
    with patch("engine.backend_pi.reset_session") as pi_reset, \
         patch("engine.backend_cc.reset_session") as cc_reset:
        yield {"pi": pi_reset, "cc": cc_reset}


def _write_codex_auth(tmp_paths, expires_at=None):
    tokens = {"access_token": "tok_codex", "account_id": "acct_codex"}
    if expires_at is not None:
        tokens["expires_at"] = expires_at
    tmp_paths["codex_auth"].write_text(json.dumps({"tokens": tokens}))


def _write_pi_auth(tmp_paths, expires_ms=None):
    if expires_ms is None:
        expires_ms = int((time.time() + 3600) * 1000)
    tmp_paths["pi_auth"].write_text(json.dumps({
        "openai-codex": {
            "access": "tok_pi",
            "refresh": "rfr",
            "expires": expires_ms,
            "accountId": "acct_pi",
        }
    }))


def _mk_response(data):
    body = json.dumps(data).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def _payload(weekly, primary, *, weekly_reset=None, primary_reset=None, **flags):
    now = time.time()
    if primary_reset is None:
        primary_reset = int(now + 3600)
    if weekly_reset is None:
        weekly_reset = int(now + 3 * 86400)
    return {
        "rate_limit": {
            "primary_window": {"used_percent": primary, "reset_at": primary_reset},
            "secondary_window": {"used_percent": weekly, "reset_at": weekly_reset},
            **flags,
        }
    }


def _write_cfg(tmp_paths, **fields):
    entry = {"fallback": True, "fallback_backend": "pi", "usage_threshold": 95}
    entry.update(fields)
    tmp_paths["codex_cfg"].write_text(json.dumps({"a1": entry}))


def _write_cfg_raw(tmp_paths, entry):
    tmp_paths["codex_cfg"].write_text(json.dumps({"a1": entry}))


def _write_unreadable_cfg(tmp_paths):
    tmp_paths["codex_cfg"].write_text("{")


def _cache_file(tmp_paths):
    return tmp_paths["cache_dir"] / "a1.json"


def _write_cache(tmp_paths, **fields):
    payload = {
        "active": True,
        "fallback_to": "pi",
        "until": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "reason": "x",
        "clear_on_revert": True,
    }
    payload.update(fields)
    tmp_paths["cache_dir"].mkdir(parents=True, exist_ok=True)
    _cache_file(tmp_paths).write_text(json.dumps(payload))
    return payload


def _write_expired_cache(tmp_paths, **fields):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return _write_cache(tmp_paths, until=past, **fields)


def _read_cache(tmp_paths):
    return json.loads(_cache_file(tmp_paths).read_text())


def _until(tmp_paths):
    return datetime.fromisoformat(_read_cache(tmp_paths)["until"])


def _should(data):
    with patch("urllib.request.urlopen", return_value=_mk_response(data)) as mock_urlopen:
        result = cq.should_fallback("a1")
    return result, mock_urlopen


def _unreadable_calls(mock_log):
    return [c for c in mock_log.call_args_list if "unreadable" in str(c.args[0])]


# ---------------------------------------------------------------------------
# get_codex_cli_usage
# ---------------------------------------------------------------------------

class TestGetCodexCliUsage:
    def test_uses_codex_credentials_even_with_pi_auth(self, tmp_paths):
        _write_pi_auth(tmp_paths)
        _write_codex_auth(tmp_paths)
        with patch("urllib.request.urlopen",
                   return_value=_mk_response(_payload(10, 10))) as mock_urlopen:
            cq.get_codex_cli_usage()
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer tok_codex"
        assert req.get_header("Chatgpt-account-id") == "acct_codex"

    def test_only_pi_auth_fails_open(self, tmp_paths):
        _write_pi_auth(tmp_paths)
        with patch("urllib.request.urlopen") as mock_urlopen:
            assert cq.get_codex_cli_usage() == CodexUsage(ok=False)
        mock_urlopen.assert_not_called()

    def test_expired_codex_auth_fails_open(self, tmp_paths):
        _write_codex_auth(tmp_paths, expires_at=time.time() - 100)
        with patch("urllib.request.urlopen") as mock_urlopen:
            assert cq.get_codex_cli_usage() == CodexUsage(ok=False)
        mock_urlopen.assert_not_called()

    def test_http_error_fails_open(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert cq.get_codex_cli_usage().ok is False

    def test_parses_both_windows(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        wr = int(time.time() + 2 * 86400)
        pr = int(time.time() + 1800)
        data = _payload(11, 22, weekly_reset=wr, primary_reset=pr)
        with patch("urllib.request.urlopen", return_value=_mk_response(data)):
            usage = cq.get_codex_cli_usage()
        assert usage.ok is True
        assert usage.weekly_used == 11.0
        assert usage.primary_used == 22.0
        assert usage.weekly_reset == datetime.fromtimestamp(wr, tz=timezone.utc)
        assert usage.primary_reset == datetime.fromtimestamp(pr, tz=timezone.utc)

    def test_unexpected_exception_fails_open(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        with patch("engine.openai_codex_quota._fetch_codex_usage",
                   side_effect=RuntimeError("x")), \
             patch("engine.codex_cli_quota.log") as mock_log:
            assert cq.get_codex_cli_usage().ok is False
        assert any("get_codex_cli_usage exception" in str(c.args[0])
                   for c in mock_log.call_args_list)


# ---------------------------------------------------------------------------
# _load_agent_config
# ---------------------------------------------------------------------------

class TestLoadAgentConfig:
    def test_missing_file_is_empty(self, tmp_paths):
        with patch("engine.codex_cli_quota.log") as mock_log:
            assert cq._load_agent_config() == {}
        mock_log.assert_not_called()

    @pytest.mark.parametrize("content", [b"{", b"[]", b'{"a1": "\xff"}'])
    def test_unreadable_is_none(self, tmp_paths, content):
        tmp_paths["codex_cfg"].write_bytes(content)
        with patch("engine.codex_cli_quota.log") as mock_log:
            assert cq._load_agent_config() is None
        assert mock_log.call_count == 1
        assert "unreadable" in mock_log.call_args[0][0]


# ---------------------------------------------------------------------------
# resolve_fallback
# ---------------------------------------------------------------------------

class TestResolveFallback:
    def test_active_cache(self, tmp_paths):
        _write_cfg(tmp_paths)
        _write_cache(tmp_paths)
        assert cq.resolve_fallback("a1") == "pi"

    def test_no_cache(self, tmp_paths):
        _write_cfg(tmp_paths)
        assert cq.resolve_fallback("a1") == ""

    def test_expired_cache_kept(self, tmp_paths):
        _write_cfg(tmp_paths)
        _write_expired_cache(tmp_paths)
        assert cq.resolve_fallback("a1") == ""
        assert _cache_file(tmp_paths).exists()

    def test_corrupt_cache(self, tmp_paths):
        _write_cfg(tmp_paths)
        tmp_paths["cache_dir"].mkdir(parents=True)
        _cache_file(tmp_paths).write_text("{not json")
        assert cq.resolve_fallback("a1") == ""

    @pytest.mark.parametrize("target", ["gemini", "codex"])
    def test_invalid_target(self, tmp_paths, target):
        _write_cfg(tmp_paths)
        _write_cache(tmp_paths, fallback_to=target)
        assert cq.resolve_fallback("a1") == ""

    @pytest.mark.parametrize("variant", [
        "fallback_false", "backend_cc", "backend_empty", "backend_gemini",
        "agent_missing", "file_missing",
    ])
    def test_config_mismatch_deletes_cache(self, tmp_paths, variant):
        if variant == "fallback_false":
            _write_cfg(tmp_paths, fallback=False)
        elif variant == "backend_cc":
            _write_cfg(tmp_paths, fallback_backend="cc")
        elif variant == "backend_empty":
            _write_cfg(tmp_paths, fallback_backend="")
        elif variant == "backend_gemini":
            _write_cfg(tmp_paths, fallback_backend="gemini")
        elif variant == "agent_missing":
            tmp_paths["codex_cfg"].write_text(json.dumps({"other": {"fallback": True}}))
        _write_cache(tmp_paths)
        assert cq.resolve_fallback("a1") == ""
        assert not _cache_file(tmp_paths).exists()

    def test_no_cache_does_not_read_config(self, tmp_paths):
        with patch("engine.codex_cli_quota._load_agent_config") as mock_load:
            assert cq.resolve_fallback("a1") == ""
        mock_load.assert_not_called()

    def test_no_cache_unreadable_config_no_log(self, tmp_paths):
        _write_unreadable_cfg(tmp_paths)
        with patch("engine.codex_cli_quota.log") as mock_log:
            assert cq.resolve_fallback("a1") == ""
        mock_log.assert_not_called()

    def test_unreadable_config_keeps_active_cache(self, tmp_paths):
        _write_unreadable_cfg(tmp_paths)
        _write_cache(tmp_paths)
        with patch("engine.codex_cli_quota.log") as mock_log:
            assert cq.resolve_fallback("a1") == "pi"
            assert len(_unreadable_calls(mock_log)) == 1
            assert cq.resolve_fallback("a1") == "pi"
            assert len(_unreadable_calls(mock_log)) == 2
        assert mock_log.call_count == 2
        assert _cache_file(tmp_paths).exists()


# ---------------------------------------------------------------------------
# should_fallback: firing
# ---------------------------------------------------------------------------

class TestShouldFallbackFiring:
    @pytest.mark.parametrize("variant", ["no_cfg", "fallback_false", "backend_empty"])
    def test_disabled(self, tmp_paths, variant):
        _write_codex_auth(tmp_paths)
        if variant == "fallback_false":
            _write_cfg(tmp_paths, fallback=False)
        elif variant == "backend_empty":
            _write_cfg(tmp_paths, fallback_backend="")
        result, mock_urlopen = _should(_payload(100, 100))
        assert result == (False, "", False)
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("fb", ["gemini", "agy", "kimi", "cci", "codex", "bogus"])
    def test_invalid_backend(self, tmp_paths, fb):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths, fallback_backend=fb)
        result, mock_urlopen = _should(_payload(100, 100))
        assert result == (False, "", False)
        mock_urlopen.assert_not_called()

    def test_weekly_at_threshold_fires(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        result, _ = _should(_payload(95.0, 10))
        assert result == (True, "pi", True)
        _fallback_resets["pi"].assert_called_once_with("a1")
        cache = _read_cache(tmp_paths)
        assert cache["fallback_to"] == "pi"
        assert cache["clear_on_revert"] is True
        assert "Codex usage 95%" in cache["reason"]

    def test_weekly_below_threshold(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        result, _ = _should(_payload(94.9, 10))
        assert result == (False, "", False)
        _fallback_resets["pi"].assert_not_called()
        assert not _cache_file(tmp_paths).exists()

    def test_primary_exhausted_fires(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        result, _ = _should(_payload(10, 100))
        assert result == (True, "pi", True)
        assert "5h window exhausted" in _read_cache(tmp_paths)["reason"]

    def test_primary_not_compared_to_threshold(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        result, _ = _should(_payload(10, 97))
        assert result == (False, "", False)

    def test_allowed_false_fires(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        result, _ = _should(_payload(10, 10, allowed=False))
        assert result == (True, "pi", True)

    def test_cc_target(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths, fallback_backend="cc")
        result, _ = _should(_payload(10, 100))
        assert result == (True, "cc", True)
        _fallback_resets["cc"].assert_called_once_with("a1")
        _fallback_resets["pi"].assert_not_called()

    def test_openclaw_target(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths, fallback_backend="openclaw")
        result, _ = _should(_payload(10, 100))
        assert result == (True, "openclaw", True)
        _fallback_resets["cc"].assert_not_called()
        _fallback_resets["pi"].assert_not_called()

    @pytest.mark.parametrize("raw", [0.95, 94.9, 90.0, True, False, "90", "abc", None, 150, -1])
    def test_invalid_threshold_treated_as_95(self, tmp_paths, raw):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths, usage_threshold=raw)
        result, _ = _should(_payload(94, 10))
        assert result == (False, "", False)
        result, _ = _should(_payload(95, 10))
        assert result == (True, "pi", True)
        warnings = cq.validate_fallback_config()
        assert len(warnings) == 1
        assert "invalid usage_threshold" in warnings[0]

    @pytest.mark.parametrize("threshold,below,at", [(0, None, 0), (90, 89, 90), (100, 99, 100)])
    def test_valid_int_threshold(self, tmp_paths, threshold, below, at):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths, usage_threshold=threshold)
        if below is not None:
            result, _ = _should(_payload(below, 10))
            assert result == (False, "", False)
        result, _ = _should(_payload(at, 10))
        assert result == (True, "pi", True)
        assert cq.validate_fallback_config() == []

    def test_missing_threshold_defaults_95(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        _write_cfg_raw(tmp_paths, {"fallback": True, "fallback_backend": "pi"})
        result, _ = _should(_payload(94, 10))
        assert result == (False, "", False)
        result, _ = _should(_payload(95, 10))
        assert result == (True, "pi", True)
        assert cq.validate_fallback_config() == []

    def test_clamp_far_reset(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        _should(_payload(96, 10, weekly_reset=int(time.time() + 30 * 86400)))
        expected = datetime.now(timezone.utc) + timedelta(hours=6)
        assert abs(_until(tmp_paths) - expected) < timedelta(minutes=2)

    def test_clamp_past_reset(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        _should(_payload(96, 10, weekly_reset=int(time.time() - 3600)))
        expected = datetime.now(timezone.utc) + timedelta(minutes=5)
        assert abs(_until(tmp_paths) - expected) < timedelta(minutes=2)

    def test_until_is_later_reset(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        wr = int(time.time() + 3 * 86400)
        pr = int(time.time() + 3600)
        _should(_payload(96, 100, weekly_reset=wr, primary_reset=pr))
        assert _until(tmp_paths) == datetime.fromtimestamp(wr, tz=timezone.utc)

    def test_matching_active_cache_skips_http(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        _write_cache(tmp_paths)
        result, mock_urlopen = _should(_payload(100, 100))
        assert result == (True, "pi", False)
        mock_urlopen.assert_not_called()
        _fallback_resets["pi"].assert_not_called()

    def test_dcl_recheck(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        active = {
            "active": True,
            "fallback_to": "pi",
            "until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        with patch("engine.fallback_cache.read_cache", side_effect=[None, active]):
            result, _ = _should(_payload(10, 100))
        assert result == (True, "pi", False)
        _fallback_resets["pi"].assert_not_called()

    def test_reset_exception_still_activates(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        _fallback_resets["pi"].side_effect = RuntimeError("boom")
        with patch("engine.codex_cli_quota.log"):
            result, _ = _should(_payload(10, 100))
        assert result == (True, "pi", True)
        assert _read_cache(tmp_paths)["fallback_to"] == "pi"

    def test_unreadable_config_expired_cache(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        _write_unreadable_cfg(tmp_paths)
        _write_expired_cache(tmp_paths)
        with patch("engine.codex_cli_quota.log") as mock_log:
            result, mock_urlopen = _should(_payload(100, 100))
        assert result == (False, "", False)
        mock_urlopen.assert_not_called()
        assert _cache_file(tmp_paths).exists()
        assert len(_unreadable_calls(mock_log)) == 1

    def test_unreadable_config_active_cache(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        _write_unreadable_cfg(tmp_paths)
        _write_cache(tmp_paths)
        with patch("engine.codex_cli_quota.log"):
            result, mock_urlopen = _should(_payload(100, 100))
        assert result == (True, "pi", False)
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# should_fallback: period transitions
# ---------------------------------------------------------------------------

class TestShouldFallbackTransitions:
    def test_continuation_renews(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        _write_expired_cache(tmp_paths)
        with patch("engine.codex_cli_quota.log") as mock_log:
            result, _ = _should(_payload(10, 100))
        assert result == (True, "pi", False)
        _fallback_resets["pi"].assert_not_called()
        assert _until(tmp_paths) > datetime.now(timezone.utc)
        assert any("renewed" in str(c.args[0]) for c in mock_log.call_args_list)

    def test_no_fire_decision_clears_expired(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        _write_expired_cache(tmp_paths)
        result, _ = _should(_payload(10, 10))
        assert result == (False, "", False)
        assert not _cache_file(tmp_paths).exists()
        result, _ = _should(_payload(10, 100))
        assert result == (True, "pi", True)
        _fallback_resets["pi"].assert_called_once_with("a1")

    def test_usage_not_ok_clears_expired(self, tmp_paths):
        _write_cfg(tmp_paths)
        _write_expired_cache(tmp_paths)
        result, mock_urlopen = _should(_payload(10, 100))
        assert result == (False, "", False)
        mock_urlopen.assert_not_called()
        assert not _cache_file(tmp_paths).exists()

    def test_legacy_cache_resets(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        tmp_paths["cache_dir"].mkdir(parents=True)
        _cache_file(tmp_paths).write_text(json.dumps(
            {"active": True, "fallback_to": "pi", "until": past, "reason": "x"}))
        result, _ = _should(_payload(10, 100))
        assert result == (True, "pi", True)
        _fallback_resets["pi"].assert_called_once_with("a1")

    def test_disabled_deletes_active_cache(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths, fallback=False)
        _write_cache(tmp_paths)
        result, mock_urlopen = _should(_payload(100, 100))
        assert result == (False, "", False)
        mock_urlopen.assert_not_called()
        assert not _cache_file(tmp_paths).exists()

    def test_reenable_after_disable_is_new_period(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_expired_cache(tmp_paths)
        _write_cfg(tmp_paths, fallback=False)
        _should(_payload(100, 100))
        assert not _cache_file(tmp_paths).exists()
        _write_cfg(tmp_paths)
        result, _ = _should(_payload(10, 100))
        assert result == (True, "pi", True)
        _fallback_resets["pi"].assert_called_once_with("a1")

    def test_target_change_expired(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths, fallback_backend="cc")
        _write_expired_cache(tmp_paths)
        result, _ = _should(_payload(10, 100))
        assert result == (True, "cc", True)
        _fallback_resets["cc"].assert_called_once_with("a1")

    def test_target_change_active_fires(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths, fallback_backend="cc")
        _write_cache(tmp_paths)
        result, _ = _should(_payload(10, 100))
        assert result == (True, "cc", True)
        _fallback_resets["cc"].assert_called_once_with("a1")
        assert _read_cache(tmp_paths)["fallback_to"] == "cc"

    def test_target_change_active_no_fire(self, tmp_paths):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths, fallback_backend="cc")
        _write_cache(tmp_paths)
        result, _ = _should(_payload(10, 10))
        assert result == (False, "", False)
        assert not _cache_file(tmp_paths).exists()

    def test_clear_stale_cache_direct(self, tmp_paths):
        c = _write_cache(tmp_paths)
        cq._clear_stale_cache("a1", c, "pi")
        assert _cache_file(tmp_paths).exists()
        cq._clear_stale_cache("a1", c, "cc")
        assert not _cache_file(tmp_paths).exists()
        c = _write_cache(tmp_paths)
        cq._clear_stale_cache("a1", c, "")
        assert not _cache_file(tmp_paths).exists()


# ---------------------------------------------------------------------------
# settle_for_reset
# ---------------------------------------------------------------------------

class TestSettleForReset:
    def test_no_cache(self, tmp_paths):
        _write_cfg(tmp_paths)
        assert cq.settle_for_reset("a1") == ""

    def test_active_matching_kept(self, tmp_paths):
        _write_cfg(tmp_paths)
        _write_cache(tmp_paths)
        assert cq.settle_for_reset("a1") == "pi"
        assert _cache_file(tmp_paths).exists()

    @pytest.mark.parametrize("fields", [{"fallback": False}, {"fallback_backend": "cc"}])
    def test_active_mismatch_deleted(self, tmp_paths, fields):
        _write_cfg(tmp_paths, **fields)
        _write_cache(tmp_paths)
        assert cq.settle_for_reset("a1") == ""
        assert not _cache_file(tmp_paths).exists()

    def test_expired_deleted(self, tmp_paths):
        _write_cfg(tmp_paths)
        _write_expired_cache(tmp_paths)
        assert cq.settle_for_reset("a1") == ""
        assert not _cache_file(tmp_paths).exists()

    def test_unreadable_config(self, tmp_paths):
        _write_unreadable_cfg(tmp_paths)
        _write_cache(tmp_paths)
        with patch("engine.codex_cli_quota.log") as mock_log:
            assert cq.settle_for_reset("a1") == "pi"
        assert _cache_file(tmp_paths).exists()
        assert len(_unreadable_calls(mock_log)) == 1
        _write_expired_cache(tmp_paths)
        with patch("engine.codex_cli_quota.log"):
            assert cq.settle_for_reset("a1") == ""
        assert not _cache_file(tmp_paths).exists()


# ---------------------------------------------------------------------------
# engine.backend wiring
# ---------------------------------------------------------------------------

@pytest.fixture
def codex_default(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_AGENT_BACKEND", "codex")
    monkeypatch.setattr(config, "AGENT_BACKEND_OVERRIDE", {}, raising=False)


@pytest.fixture
def pi_state(tmp_paths, monkeypatch):
    backend_pi._starting_markers.clear()
    backend_pi._agent_config_cache = None
    sessions = tmp_paths["tmp_path"] / "sessions"
    monkeypatch.setattr("config.PI_SESSIONS_DIR", sessions)
    monkeypatch.setattr("engine.backend_pi.PI_SESSIONS_DIR", sessions)
    monkeypatch.setattr("config.PI_AGENT_CONFIG", tmp_paths["pi_cfg"])
    monkeypatch.setattr("engine.backend_pi.PI_AGENT_CONFIG", tmp_paths["pi_cfg"])
    yield
    backend_pi._starting_markers.clear()
    backend_pi._agent_config_cache = None


def _capture_popen():
    proc = MagicMock()
    proc.stdin = MagicMock()
    factory = MagicMock(return_value=proc)
    return factory, proc


def _model_arg(factory):
    cmd = factory.call_args[0][0]
    return cmd[cmd.index("--model") + 1]


@pytest.mark.usefixtures("codex_default")
class TestBackendWiring:
    def test_resolve_backend(self, tmp_paths):
        with patch("engine.codex_cli_quota.resolve_fallback", return_value="pi") as mock_rf:
            assert backend.resolve_backend("a1") == "pi"
            mock_rf.reset_mock()
            assert backend.resolve_backend("a1", ignore_fallback=True) == "codex"
            mock_rf.assert_not_called()

    def test_send_redirects(self, tmp_paths):
        with patch("engine.codex_cli_quota.resolve_fallback", return_value=""), \
             patch("engine.codex_cli_quota.should_fallback",
                   return_value=(True, "cc", True)) as mock_sf, \
             patch("engine.backend_cc.send", return_value=SendResult.OK) as mock_cc, \
             patch("engine.backend_codex.send") as mock_cx:
            backend.send("a1", "m", 10)
        mock_cc.assert_called_once()
        mock_cx.assert_not_called()
        mock_sf.assert_called_once_with("a1")

    def test_send_stays_on_codex(self, tmp_paths):
        with patch("engine.codex_cli_quota.resolve_fallback", return_value=""), \
             patch("engine.codex_cli_quota.should_fallback", return_value=(False, "", False)), \
             patch("engine.backend_codex.send", return_value=SendResult.OK) as mock_cx:
            backend.send("a1", "m", 10)
        mock_cx.assert_called_once()

    def test_no_transitive_chain_via_pi(self, tmp_paths, pi_state):
        _write_cfg(tmp_paths)
        _write_cache(tmp_paths)
        tmp_paths["pi_cfg"].write_text(json.dumps({"a1": {
            "provider": "openai-codex", "model": "gpt-5.4",
            "fallback": True, "fallback_backend": "cc",
        }}))
        factory, _ = _capture_popen()
        with patch("subprocess.Popen", factory), \
             patch("engine.codex_cli_quota.should_fallback") as mock_cxc_sf, \
             patch("engine.openai_codex_quota.should_fallback") as mock_ocq_sf, \
             patch("engine.backend_cc.send") as mock_cc, \
             patch("engine.backend_codex.send") as mock_cx:
            result = backend.send("a1", "m", 10)
        assert result is SendResult.OK
        assert mock_cxc_sf.call_count == 0
        assert mock_ocq_sf.call_count == 0
        mock_cc.assert_not_called()
        mock_cx.assert_not_called()
        assert _model_arg(factory) == "openai-codex/gpt-5.4"

    def test_pi_mode_a_applies_to_redirect(self, tmp_paths, pi_state):
        _write_cfg(tmp_paths)
        _write_cache(tmp_paths)
        tmp_paths["pi_cfg"].write_text(json.dumps({"a1": {
            "provider": "openai-codex", "model": "gpt-5.4", "fallback": True,
            "fallback_provider": "github-copilot", "fallback_model": "gpt-5.4",
        }}))
        factory, _ = _capture_popen()
        with patch("subprocess.Popen", factory), \
             patch("engine.codex_cli_quota.should_fallback") as mock_cxc_sf, \
             patch("engine.openai_codex_quota.should_fallback",
                   return_value=(True, "github-copilot", "gpt-5.4", True)) as mock_ocq_sf:
            result = backend.send("a1", "m", 10)
        assert result is SendResult.OK
        mock_ocq_sf.assert_called_once_with("a1")
        assert mock_cxc_sf.call_count == 0
        assert _model_arg(factory) == "github-copilot/gpt-5.4"

    def test_disable_end_to_end(self, tmp_paths):
        _write_cfg(tmp_paths, fallback=False)
        _write_cache(tmp_paths)
        assert backend.resolve_backend("a1") == "codex"
        assert not _cache_file(tmp_paths).exists()
        with patch("engine.backend_codex.send", return_value=SendResult.OK) as mock_cx, \
             patch("engine.backend_pi.send") as mock_pi, \
             patch("urllib.request.urlopen") as mock_urlopen:
            backend.send("a1", "m", 10)
        mock_cx.assert_called_once()
        mock_pi.assert_not_called()
        mock_urlopen.assert_not_called()

    def _disable_reset_reenable(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        _write_cache(tmp_paths)
        _write_cfg(tmp_paths, fallback=False)
        with patch("engine.backend_codex.reset_session") as mock_cx_reset:
            backend.reset_session("a1")
        mock_cx_reset.assert_called_once_with("a1")
        _fallback_resets["pi"].assert_not_called()
        assert not _cache_file(tmp_paths).exists()
        _write_cfg(tmp_paths)

    def test_r2_sequence_fires(self, tmp_paths, _fallback_resets):
        self._disable_reset_reenable(tmp_paths, _fallback_resets)
        calls: list[str] = []
        _fallback_resets["pi"].side_effect = lambda *a: calls.append("reset")

        def _pi_send(*a, **kwargs):
            calls.append("send")
            return SendResult.OK

        with patch("engine.backend_pi.send", side_effect=_pi_send), \
             patch("engine.backend_codex.send") as mock_cx, \
             patch("urllib.request.urlopen", return_value=_mk_response(_payload(10, 100))):
            backend.send("a1", "m", 10)
        assert calls == ["reset", "send"]
        mock_cx.assert_not_called()

    def test_r2_sequence_no_fire(self, tmp_paths, _fallback_resets):
        self._disable_reset_reenable(tmp_paths, _fallback_resets)
        with patch("engine.backend_pi.send") as mock_pi, \
             patch("engine.backend_codex.send", return_value=SendResult.OK) as mock_cx, \
             patch("urllib.request.urlopen", return_value=_mk_response(_payload(10, 10))):
            backend.send("a1", "m", 10)
        mock_cx.assert_called_once()
        mock_pi.assert_not_called()

    def test_target_change_without_send(self, tmp_paths):
        _write_cfg(tmp_paths)
        _write_cache(tmp_paths)
        _write_cfg(tmp_paths, fallback_backend="cc")
        assert backend.resolve_backend("a1") == "codex"
        assert not _cache_file(tmp_paths).exists()
        _write_cfg(tmp_paths)
        assert backend.resolve_backend("a1") == "codex"

    def test_expired_cache_and_reset_session(self, tmp_paths, _fallback_resets):
        _write_codex_auth(tmp_paths)
        _write_cfg(tmp_paths)
        _write_expired_cache(tmp_paths)
        with patch("engine.backend_codex.reset_session"):
            backend.reset_session("a1")
        _fallback_resets["pi"].assert_not_called()
        assert not _cache_file(tmp_paths).exists()
        calls: list[str] = []
        _fallback_resets["pi"].side_effect = lambda *a: calls.append("reset")

        def _pi_send(*a, **kwargs):
            calls.append("send")
            return SendResult.OK

        with patch("engine.backend_pi.send", side_effect=_pi_send), \
             patch("engine.backend_codex.send"), \
             patch("urllib.request.urlopen", return_value=_mk_response(_payload(10, 100))):
            backend.send("a1", "m", 10)
        assert calls == ["reset", "send"]

    def test_active_cache_and_reset_session(self, tmp_paths, _fallback_resets):
        _write_cfg(tmp_paths)
        _write_cache(tmp_paths)
        with patch("engine.backend_codex.reset_session") as mock_cx_reset:
            backend.reset_session("a1")
        mock_cx_reset.assert_called_once_with("a1")
        _fallback_resets["pi"].assert_called_once_with("a1")
        assert _cache_file(tmp_paths).exists()
        assert backend.resolve_backend("a1") == "pi"

    @pytest.mark.parametrize("fb,pi_n,cc_n", [
        ("pi", 1, 0), ("cc", 0, 1), ("openclaw", 0, 0), ("", 0, 0),
    ])
    def test_reset_session_wiring(self, tmp_paths, _fallback_resets, fb, pi_n, cc_n):
        with patch("engine.codex_cli_quota.settle_for_reset", return_value=fb) as mock_settle, \
             patch("engine.codex_cli_quota.resolve_fallback") as mock_rf, \
             patch("engine.backend_codex.reset_session") as mock_cx_reset:
            backend.reset_session("a1")
        mock_settle.assert_called_once_with("a1")
        mock_rf.assert_not_called()
        mock_cx_reset.assert_called_once_with("a1")
        assert _fallback_resets["pi"].call_count == pi_n
        assert _fallback_resets["cc"].call_count == cc_n


# ---------------------------------------------------------------------------
# validate_fallback_config
# ---------------------------------------------------------------------------

class TestValidate:
    @pytest.mark.parametrize("fields", [{"fallback": False}, {"fallback_backend": ""}])
    def test_disabled(self, tmp_paths, fields):
        _write_cfg(tmp_paths, **fields)
        assert cq.validate_fallback_config() == []

    @pytest.mark.parametrize("fb", ["bogus", "agy", "codex"])
    def test_invalid_backend(self, tmp_paths, fb):
        _write_cfg(tmp_paths, fallback_backend=fb)
        warnings = cq.validate_fallback_config()
        assert any("fallback_backend" in w for w in warnings)

    def test_shared_quota_warning(self, tmp_paths):
        _write_cfg(tmp_paths)
        tmp_paths["pi_cfg"].write_text(json.dumps({"a1": {"provider": "openai-codex"}}))
        warnings = cq.validate_fallback_config()
        assert len(warnings) == 1
        assert "same ChatGPT quota" in warnings[0]
        assert "Mode A" in warnings[0]

    def test_shared_quota_other_provider(self, tmp_paths):
        _write_cfg(tmp_paths)
        tmp_paths["pi_cfg"].write_text(json.dumps({"a1": {"provider": "github-copilot"}}))
        assert cq.validate_fallback_config() == []

    def test_shared_quota_no_pi_entry(self, tmp_paths):
        _write_cfg(tmp_paths)
        tmp_paths["pi_cfg"].write_text(json.dumps({"other": {"provider": "openai-codex"}}))
        assert cq.validate_fallback_config() == []

    def test_shared_quota_cc_target(self, tmp_paths):
        _write_cfg(tmp_paths, fallback_backend="cc")
        tmp_paths["pi_cfg"].write_text(json.dumps({"a1": {"provider": "openai-codex"}}))
        assert cq.validate_fallback_config() == []

    def test_valid(self, tmp_paths):
        _write_cfg(tmp_paths)
        assert cq.validate_fallback_config() == []

    def test_unreadable(self, tmp_paths):
        _write_unreadable_cfg(tmp_paths)
        with patch("engine.codex_cli_quota.log") as mock_log:
            warnings = cq.validate_fallback_config()
        assert len(warnings) == 1
        assert "cannot be read" in warnings[0]
        mock_log.assert_not_called()


# ---------------------------------------------------------------------------
# codex is never a fallback target
# ---------------------------------------------------------------------------

class TestNoTransitiveTarget:
    @pytest.mark.parametrize("mod", [
        "engine.gemini_quota", "engine.agy_quota", "engine.kimi_quota",
        "engine.openai_codex_quota", "engine.codex_cli_quota",
    ])
    def test_codex_not_a_target(self, mod):
        import importlib
        assert "codex" not in importlib.import_module(mod)._VALID_FALLBACK_BACKENDS


# ---------------------------------------------------------------------------
# Unreadable config warnings on runtime paths
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("codex_default")
class TestUnreadableConfigWarning:
    def test_send_and_ping_warn(self, tmp_paths):
        _write_cfg(tmp_paths)
        _write_cache(tmp_paths)
        _write_unreadable_cfg(tmp_paths)
        with patch("engine.codex_cli_quota.log") as mock_log, \
             patch("engine.backend_pi.send", return_value=SendResult.OK) as mock_pi, \
             patch("engine.backend_codex.send") as mock_cx:
            backend.send("a1", "m", 10)
            backend.ping("a1", 5)
        mock_pi.assert_called_once()
        mock_cx.assert_not_called()
        assert len(_unreadable_calls(mock_log)) == 2
        assert _cache_file(tmp_paths).exists()
