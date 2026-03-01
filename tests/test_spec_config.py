"""tests/test_spec_config.py — spec mode 基盤のテスト"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
from pipeline_io import (
    default_spec_config,
    validate_spec_config,
    check_spec_mode_exclusive,
    ensure_spec_reviews_dir,
)


class TestSpecStatesIntegration:

    def test_all_spec_states_in_valid_states(self):
        for s in config.SPEC_STATES:
            assert s in config.VALID_STATES, f"{s} not in VALID_STATES"

    def test_valid_states_sorted(self):
        assert config.VALID_STATES == sorted(config.VALID_STATES)

    def test_no_duplicates_in_valid_states(self):
        assert len(config.VALID_STATES) == len(set(config.VALID_STATES))

    def test_spec_transitions_in_valid_transitions(self):
        for state, targets in config.SPEC_TRANSITIONS.items():
            assert state in config.VALID_TRANSITIONS
            for t in targets:
                assert t in config.VALID_TRANSITIONS[state]

    def test_spec_states_in_phase_map(self):
        for s in config.SPEC_STATES:
            assert config.STATE_PHASE_MAP.get(s) == "spec"

    def test_existing_states_preserved(self):
        for s in ["IDLE", "DESIGN_PLAN", "IMPLEMENTATION", "DONE", "BLOCKED"]:
            assert s in config.VALID_STATES

    def test_existing_transitions_preserved(self):
        assert "DESIGN_REVIEW" in config.VALID_TRANSITIONS["DESIGN_PLAN"]


class TestSpecConstants:

    def test_max_revise_cycles(self):
        assert config.MAX_SPEC_REVISE_CYCLES == 5

    def test_min_valid_reviews_by_mode(self):
        assert config.MIN_VALID_REVIEWS_BY_MODE == {
            "full": 2, "standard": 2, "lite": 1, "min": 1,
        }

    def test_timeouts(self):
        assert config.SPEC_REVIEW_TIMEOUT_SEC == 1800
        assert config.SPEC_REVISE_TIMEOUT_SEC == 1800
        assert config.SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC == 600

    def test_self_review_passes(self):
        assert config.SPEC_REVISE_SELF_REVIEW_PASSES == 2

    def test_max_retries(self):
        assert config.MAX_SPEC_RETRIES == 3

    def test_raw_retention_days(self):
        assert config.SPEC_REVIEW_RAW_RETENTION_DAYS == 30


class TestDefaultSpecConfig:

    def test_returns_dict(self):
        cfg = default_spec_config()
        assert isinstance(cfg, dict)

    def test_required_fields_empty(self):
        cfg = default_spec_config()
        assert cfg["spec_path"] == ""
        assert cfg["spec_implementer"] == ""

    def test_default_values(self):
        cfg = default_spec_config()
        assert cfg["review_only"] is False
        assert cfg["max_revise_cycles"] == config.MAX_SPEC_REVISE_CYCLES
        assert cfg["revise_count"] == 0
        assert cfg["current_rev"] == "1"
        assert cfg["rev_index"] == 1
        assert cfg["self_review_passes"] == config.SPEC_REVISE_SELF_REVIEW_PASSES
        assert cfg["created_issues"] == []
        assert cfg["retry_counts"] == {}
        assert cfg["paused_from"] is None
        assert cfg["last_changes"] is None


class TestValidateSpecConfig:

    def test_valid_config(self):
        cfg = default_spec_config()
        cfg["spec_path"] = "docs/spec.md"
        cfg["spec_implementer"] = "second"
        assert validate_spec_config(cfg) == []

    def test_missing_spec_path(self):
        cfg = default_spec_config()
        cfg["spec_implementer"] = "second"
        errors = validate_spec_config(cfg)
        assert any("spec_path" in e for e in errors)

    def test_missing_implementer(self):
        cfg = default_spec_config()
        cfg["spec_path"] = "docs/spec.md"
        errors = validate_spec_config(cfg)
        assert any("spec_implementer" in e for e in errors)

    def test_both_missing(self):
        cfg = default_spec_config()
        errors = validate_spec_config(cfg)
        assert len(errors) == 2


class TestSpecModeExclusive:

    def test_raises_when_spec_mode_true(self):
        data = {"spec_mode": True}
        with pytest.raises(SystemExit):
            check_spec_mode_exclusive(data)

    def test_no_raise_when_spec_mode_false(self):
        data = {"spec_mode": False}
        check_spec_mode_exclusive(data)

    def test_no_raise_when_spec_mode_absent(self):
        data = {}
        check_spec_mode_exclusive(data)


class TestEnsureSpecReviewsDir:

    def test_creates_directory(self, tmp_pipelines):
        d = ensure_spec_reviews_dir("test-pj")
        assert d.is_dir()
        assert d == tmp_pipelines / "test-pj" / "spec-reviews"

    def test_permissions(self, tmp_pipelines):
        d = ensure_spec_reviews_dir("test-pj")
        assert (d.stat().st_mode & 0o777) == 0o700

    def test_idempotent(self, tmp_pipelines):
        d1 = ensure_spec_reviews_dir("test-pj")
        d2 = ensure_spec_reviews_dir("test-pj")
        assert d1 == d2
