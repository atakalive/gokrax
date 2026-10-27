"""Tests for engine/boost.py (Issue #400)."""

from __future__ import annotations

import copy

import pytest

import config
from engine import boost as boost_mod
from engine.boost import (
    boost_applies,
    boost_phases_from_data,
    boost_phases_from_mode,
    format_boost_label,
    is_boost_active,
    overlay_profile,
    validate_boost_config,
)


def _data(batch_boost, design_boost, code_boost) -> dict:
    data: dict = {
        "review_config": {
            "design": {"members": ["reviewer1"], "min_reviews": 1, "n_pass": {},
                       "grace_period_sec": 0, "boost": design_boost},
            "code": {"members": ["reviewer1"], "min_reviews": 1, "n_pass": {},
                     "grace_period_sec": 0, "boost": code_boost},
        },
    }
    if batch_boost is not None:
        data["boost"] = batch_boost
    return data


_DEV_STATES = [s for s, p in config.STATE_PHASE_MAP.items() if p in ("design", "code")]
_SPEC_STATES = [s for s, p in config.STATE_PHASE_MAP.items() if p == "spec"]


class TestIsBoostActive:
    @pytest.mark.parametrize("state", _DEV_STATES)
    @pytest.mark.parametrize("batch_boost", [True, False, None])
    @pytest.mark.parametrize("design_boost, code_boost",
                             [(True, False), (False, True), (True, True), (False, False)])
    def test_matrix(self, state, batch_boost, design_boost, code_boost):
        data = _data(batch_boost, design_boost, code_boost)
        phase = config.STATE_PHASE_MAP[state]
        phase_boost = design_boost if phase == "design" else code_boost
        assert is_boost_active(data, state) is (bool(batch_boost) and phase_boost)

    @pytest.mark.parametrize("state", ["IDLE", "INITIALIZE", "DONE", "BLOCKED", *_SPEC_STATES])
    def test_unmapped_and_spec_states_false(self, state):
        assert is_boost_active(_data(True, True, True), state) is False

    @pytest.mark.parametrize("state", [None, ""])
    def test_empty_state_false(self, state):
        assert is_boost_active(_data(True, True, True), state) is False

    def test_no_review_config_or_mode_false(self):
        assert is_boost_active({"boost": True}, "DESIGN_REVIEW") is False

    def test_old_review_config_without_boost_key(self):
        data = {"boost": True, "review_config": {"design": {"members": ["reviewer1"]}}}
        assert is_boost_active(data, "DESIGN_REVIEW") is False

    def test_fallback_to_review_mode(self, monkeypatch):
        modes = {"bm": {"members": ["reviewer1"], "design": {"boost": True}}}
        monkeypatch.setattr("engine.fsm.REVIEW_MODES", modes)
        data = {"boost": True, "review_mode": "bm"}
        assert is_boost_active(data, "DESIGN_REVIEW") is True
        assert is_boost_active(data, "CODE_REVIEW") is False


class TestOverlayProfile:
    def test_allowed_keys_overlaid(self):
        p = {"model": "a", "thinking": "low", "boost": {"model": "b", "thinking": "high"}}
        assert overlay_profile(p, "pi") == {"model": "b", "thinking": "high"}

    def test_disallowed_keys_dropped(self):
        p = {"provider": "x", "model": "a", "boost": {"provider": "y", "model": "b"}}
        assert overlay_profile(p, "pi") == {"provider": "x", "model": "b"}
        g = {"model": "a", "boost": {"model": "b", "effort": "max"}}
        assert overlay_profile(g, "gemini") == {"model": "b"}

    def test_result_has_no_boost_key(self):
        out = overlay_profile({"model": "a", "boost": {}}, "cc")
        assert "boost" not in out

    @pytest.mark.parametrize("boost_val", ["missing", None, "str", ["model"]])
    def test_non_dict_returns_same_object(self, boost_val):
        p: dict = {"model": "a"}
        if boost_val != "missing":
            p["boost"] = boost_val
        assert overlay_profile(p, "cc") is p

    def test_input_not_mutated(self):
        p = {"model": "a", "effort": "low", "boost": {"model": "b", "effort": "max"}}
        before = copy.deepcopy(p)
        overlay_profile(p, "cc")
        assert p == before

    def test_unknown_backend_overlays_nothing(self):
        p = {"model": "a", "boost": {"model": "b"}}
        assert overlay_profile(p, "openclaw") == {"model": "a"}


class TestBoostApplies:
    def test_allowed_key(self):
        assert boost_applies({"boost": {"model": "b"}}, "kimi") is True

    @pytest.mark.parametrize("boost_val", [{}, {"effort": "max"}])
    def test_no_allowed_key(self, boost_val):
        assert boost_applies({"boost": boost_val}, "kimi") is False

    @pytest.mark.parametrize("boost_val", [None, "x", 1])
    def test_non_dict(self, boost_val):
        assert boost_applies({"boost": boost_val}, "cc") is False
        assert boost_applies({}, "cc") is False


class TestFormatBoostLabel:
    @pytest.mark.parametrize("phases, expected", [
        (None, "boost[?]"),
        ([], "boost[-]"),
        (["design"], "boost[design]"),
        (["design", "code"], "boost[design,code]"),
    ])
    def test_label(self, phases, expected):
        assert format_boost_label(phases) == expected


class TestBoostPhases:
    def test_from_mode_top_level(self):
        assert boost_phases_from_mode({"members": [], "boost": True}) == ["design", "code"]

    def test_from_mode_phase_override(self):
        assert boost_phases_from_mode({"members": [], "code": {"boost": True}}) == ["code"]

    def test_from_mode_full2(self):
        mc = {"members": [], "boost": True, "code": {"boost": False}}
        assert boost_phases_from_mode(mc) == ["design"]

    def test_from_mode_none(self):
        assert boost_phases_from_mode({"members": []}) == []

    def test_from_data(self):
        assert boost_phases_from_data(_data(True, True, False)) == ["design"]
        assert boost_phases_from_data(_data(None, True, True)) == ["design", "code"]

    def test_from_data_error_returns_empty(self):
        assert boost_phases_from_data({}) == []


class TestValidateBoostConfig:
    def _patch(self, monkeypatch, configs: dict[str, object]) -> None:
        for b in boost_mod.BOOST_KEYS:
            val = configs.get(b, {})
            if isinstance(val, Exception):
                def _raise(exc=val):
                    raise exc
                monkeypatch.setattr(f"engine.backend_{b}._load_config", _raise)
            else:
                monkeypatch.setattr(f"engine.backend_{b}._load_config", lambda v=val: v)

    def test_no_warnings_for_clean_config(self, monkeypatch):
        self._patch(monkeypatch, {
            "cc": {"_comment": {"boost": "doc"}, "a": {"model": "x"},
                   "b": {"model": "x", "boost": {"model": "y", "effort": "max"}},
                   "c": "not-a-dict"},
        })
        assert validate_boost_config() == []

    def test_non_dict_boost(self, monkeypatch):
        self._patch(monkeypatch, {"gemini": {"a": {"boost": "big"}}})
        warns = validate_boost_config()
        assert len(warns) == 1
        assert warns[0].startswith("WARNING: ")
        assert "'a'" in warns[0] and "config_gemini.json" in warns[0]

    def test_disallowed_keys(self, monkeypatch):
        self._patch(monkeypatch, {"pi": {"a": {"boost": {"provider": "p", "model": "m", "effort": "x"}}}})
        warns = validate_boost_config()
        assert len(warns) == 1
        assert "['effort', 'provider']" in warns[0]
        assert "['model', 'thinking']" in warns[0]

    def test_load_failure(self, monkeypatch):
        self._patch(monkeypatch, {"kimi": RuntimeError("boom")})
        warns = validate_boost_config()
        assert len(warns) == 1
        assert "kimi" in warns[0] and "boom" in warns[0]
        assert warns[0].startswith("WARNING: ")
