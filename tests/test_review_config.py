"""tests/test_review_config.py — Issue #203/#204: review_config 生成・読み取り・バリデーションテスト"""

import pytest

from config import _validate_review_modes
from engine.fsm import _build_phase_config, build_review_config, get_phase_config


# ---------------------------------------------------------------------------
# _build_phase_config
# ---------------------------------------------------------------------------

class TestBuildPhaseConfig:

    def test_no_override(self):
        """(a) フェーズ上書きなし: ベース構成がそのまま返る。"""
        mode_config = {
            "members": ["r1", "r2", "r3"],
            "min_reviews": 3,
            "n_pass": {"r1": 2},
            "grace_period_sec": 60,
        }
        result = _build_phase_config(mode_config, "design")
        assert result["members"] == ["r1", "r2", "r3"]
        assert result["min_reviews"] == 3
        assert result["n_pass"] == {"r1": 2}
        assert result["grace_period_sec"] == 60

    def test_override_members_only(self):
        """(b) フェーズ上書きで members のみ変更 — min_reviews が最終 members の len に再計算される。"""
        mode_config = {
            "members": ["r1", "r2", "r3"],
            # min_reviews 未指定 → len(members) にフォールバック
            "n_pass": {},
            "grace_period_sec": 0,
            "design": {
                "members": ["r1", "r2"],
            },
        }
        result = _build_phase_config(mode_config, "design")
        assert result["members"] == ["r1", "r2"]
        # override 後の members len(2) が使われる
        assert result["min_reviews"] == 2

    def test_override_members_with_base_min_reviews(self):
        """(b') ベースに min_reviews があり override で members のみ変更 — ベースの min_reviews が members 数でキャップされる。"""
        mode_config = {
            "members": ["r1", "r2", "r3"],
            "min_reviews": 2,
            "n_pass": {},
            "grace_period_sec": 0,
            "design": {
                "members": ["r1"],
            },
        }
        result = _build_phase_config(mode_config, "design")
        assert result["members"] == ["r1"]
        # ベースの min_reviews=2 が Step 4 で members 数(1) にキャップされる
        assert result["min_reviews"] == 1

    def test_override_with_explicit_min_reviews(self):
        """(c) フェーズ上書きで min_reviews も明示指定。"""
        mode_config = {
            "members": ["r1", "r2", "r3"],
            "min_reviews": 3,
            "n_pass": {},
            "grace_period_sec": 0,
            "code": {
                "members": ["r1", "r2"],
                "min_reviews": 1,
            },
        }
        result = _build_phase_config(mode_config, "code")
        assert result["members"] == ["r1", "r2"]
        assert result["min_reviews"] == 1

    def test_override_n_pass(self):
        """(d) フェーズ上書きで n_pass 部分上書き。"""
        mode_config = {
            "members": ["r1", "r2"],
            "min_reviews": 2,
            "n_pass": {"r1": 2},
            "grace_period_sec": 0,
            "design": {
                "n_pass": {"r2": 3},
            },
        }
        result = _build_phase_config(mode_config, "design")
        # n_pass は override で完全置換
        assert result["n_pass"] == {"r2": 3}
        assert result["members"] == ["r1", "r2"]
        assert result["min_reviews"] == 2

    def test_no_phase_key_returns_base(self):
        """フェーズキーが mode_config にない場合、ベース構成が返る。"""
        mode_config = {
            "members": ["r1"],
            "min_reviews": 1,
            "grace_period_sec": 0,
        }
        result = _build_phase_config(mode_config, "code")
        assert result["members"] == ["r1"]
        assert result["min_reviews"] == 1
        assert result["n_pass"] == {}
        assert result["grace_period_sec"] == 0

    def test_empty_mode_config(self):
        """空の mode_config でもクラッシュしない。"""
        result = _build_phase_config({}, "design")
        assert result["members"] == []
        assert result["min_reviews"] == 0
        assert result["n_pass"] == {}
        assert result["grace_period_sec"] == 0

    def test_override_grace_period(self):
        """フェーズ上書きで grace_period_sec を変更。"""
        mode_config = {
            "members": ["r1"],
            "min_reviews": 1,
            "grace_period_sec": 0,
            "design": {
                "grace_period_sec": 120,
            },
        }
        result = _build_phase_config(mode_config, "design")
        assert result["grace_period_sec"] == 120


# ---------------------------------------------------------------------------
# build_review_config
# ---------------------------------------------------------------------------

class TestBuildReviewConfig:

    def test_returns_both_phases(self):
        """design と code の両方が生成される。"""
        mode_config = {
            "members": ["r1", "r2"],
            "min_reviews": 2,
            "n_pass": {},
            "grace_period_sec": 0,
        }
        result = build_review_config(mode_config)
        assert "design" in result
        assert "code" in result
        assert result["design"]["members"] == ["r1", "r2"]
        assert result["code"]["members"] == ["r1", "r2"]

    def test_phase_overrides_applied_independently(self):
        """design と code で異なる override が適用される。"""
        mode_config = {
            "members": ["r1", "r2", "r3"],
            "min_reviews": 3,
            "n_pass": {},
            "grace_period_sec": 0,
            "design": {
                "members": ["r1", "r2"],
            },
            "code": {
                "min_reviews": 1,
            },
        }
        result = build_review_config(mode_config)
        assert result["design"]["members"] == ["r1", "r2"]
        assert result["code"]["members"] == ["r1", "r2", "r3"]
        assert result["code"]["min_reviews"] == 1


# ---------------------------------------------------------------------------
# get_phase_config
# ---------------------------------------------------------------------------

class TestGetPhaseConfig:

    def test_review_config_exists(self):
        """(a) review_config がある場合、そこから取得する。"""
        data = {
            "review_config": {
                "design": {
                    "members": ["x1", "x2"],
                    "min_reviews": 2,
                    "n_pass": {},
                    "grace_period_sec": 0,
                },
                "code": {
                    "members": ["y1"],
                    "min_reviews": 1,
                    "n_pass": {},
                    "grace_period_sec": 0,
                },
            },
        }
        result = get_phase_config(data, "design")
        assert result["members"] == ["x1", "x2"]
        result_code = get_phase_config(data, "code")
        assert result_code["members"] == ["y1"]

    def test_fallback_no_review_config(self):
        """(b) review_config がない場合、review_mode から REVIEW_MODES を引いてフォールバックする。"""
        data = {"review_mode": "standard"}
        result = get_phase_config(data, "design")
        # conftest で standard は ["reviewer1", "reviewer3", "reviewer6"]
        assert "members" in result
        assert "min_reviews" in result
        assert "n_pass" in result
        assert "grace_period_sec" in result

    def test_data_none(self):
        """(c) data=None の場合、REVIEW_MODES["standard"] にフォールバックする。"""
        result = get_phase_config(None, "design")
        assert "members" in result
        assert "min_reviews" in result
        assert "n_pass" in result
        assert "grace_period_sec" in result

    def test_fallback_returns_four_keys(self):
        """フォールバック時も4キーが保証される。"""
        data = {"review_mode": "lite"}
        result = get_phase_config(data, "code")
        assert set(result.keys()) >= {"members", "min_reviews", "n_pass", "grace_period_sec"}


# ---------------------------------------------------------------------------
# Integration: INITIALIZE → DESIGN_PLAN で review_config が書き込まれる
# ---------------------------------------------------------------------------

class TestInitializeWritesReviewConfig:
    """watchdog の do_transition 内で review_config が書き込まれることの間接テスト。

    check_transition 自体は review_config を書き込まないが、
    INITIALIZE → DESIGN_PLAN 遷移後に get_phase_config がフォールバックなしで動くことを確認。
    """

    def test_build_and_read_roundtrip(self):
        """build_review_config で生成 → get_phase_config で読み取り。"""
        from config import REVIEW_MODES
        mode_config = REVIEW_MODES.get("standard", REVIEW_MODES["standard"])
        review_config = build_review_config(mode_config)

        data = {
            "review_mode": "standard",
            "review_config": review_config,
        }
        design = get_phase_config(data, "design")
        code = get_phase_config(data, "code")
        assert design["members"] == mode_config["members"]
        assert code["members"] == mode_config["members"]
        assert design["min_reviews"] == mode_config["min_reviews"]
        assert code["min_reviews"] == mode_config["min_reviews"]

    def test_initialize_transition_with_review_config(self):
        """INITIALIZE → DESIGN_PLAN 遷移後、review_config が使える。"""
        from config import REVIEW_MODES
        from engine.fsm import check_transition

        batch = [
            {"issue": 1, "title": "T", "commit": None, "cc_session_id": None,
             "design_reviews": {}, "code_reviews": {},
             "added_at": "2025-01-01T00:00:00+09:00"},
        ]
        data = {"project": "pj", "review_mode": "standard", "comment": ""}

        # INITIALIZE → DESIGN_PLAN
        action = check_transition("INITIALIZE", batch, data)
        assert action.new_state == "DESIGN_PLAN"

        # watchdog の do_transition が行う処理をシミュレート
        mode_config = REVIEW_MODES.get("standard", REVIEW_MODES["standard"])
        data["review_config"] = build_review_config(mode_config)

        # review_config から読めることを確認
        design = get_phase_config(data, "design")
        assert design["members"] == mode_config["members"]
        assert isinstance(design["min_reviews"], int)


# ---------------------------------------------------------------------------
# Issue #204: min_reviews 自動キャップ
# ---------------------------------------------------------------------------

class TestMinReviewsAutoCap:

    def test_cap_base_min_reviews_exceeds_override_members(self):
        """ベース min_reviews=3 + override members=1人 → min_reviews=1 にキャップ。"""
        mode_config = {
            "members": ["r1", "r2", "r3"],
            "min_reviews": 3,
            "n_pass": {},
            "grace_period_sec": 0,
            "code": {
                "members": ["r1"],
            },
        }
        result = _build_phase_config(mode_config, "code")
        assert result["members"] == ["r1"]
        assert result["min_reviews"] == 1

    def test_cap_empty_members(self):
        """override で members=[] → min_reviews=0 にキャップ（skip 同等）。"""
        mode_config = {
            "members": ["r1", "r2"],
            "min_reviews": 2,
            "n_pass": {},
            "grace_period_sec": 0,
            "design": {
                "members": [],
            },
        }
        result = _build_phase_config(mode_config, "design")
        assert result["members"] == []
        assert result["min_reviews"] == 0

    def test_no_cap_when_min_reviews_within_members(self):
        """min_reviews <= len(members) の場合はキャップされない。"""
        mode_config = {
            "members": ["r1", "r2", "r3"],
            "min_reviews": 2,
            "n_pass": {},
            "grace_period_sec": 0,
        }
        result = _build_phase_config(mode_config, "design")
        assert result["min_reviews"] == 2

    def test_cap_override_explicit_min_reviews_exceeds_members(self):
        """override に明示 min_reviews があっても members 数を超えたらキャップ。"""
        mode_config = {
            "members": ["r1", "r2", "r3"],
            "min_reviews": 3,
            "n_pass": {},
            "grace_period_sec": 0,
            "code": {
                "members": ["r1"],
                "min_reviews": 5,
            },
        }
        result = _build_phase_config(mode_config, "code")
        assert result["members"] == ["r1"]
        assert result["min_reviews"] == 1


# ---------------------------------------------------------------------------
# Issue #204: _validate_review_modes
# ---------------------------------------------------------------------------

class TestValidateReviewModes:

    def test_valid_config_no_error(self):
        """正常な設定ではエラーが出ない。"""
        modes = {
            "standard": {
                "members": ["r1", "r2"],
                "min_reviews": 2,
                "grace_period_sec": 0,
                "code": {
                    "members": ["r1"],
                    "min_reviews": 1,
                },
            },
        }
        _validate_review_modes(modes, ["r1", "r2"])

    def test_phase_override_not_dict(self):
        """(a) フェーズ上書きが dict でない場合に ValueError。"""
        modes = {
            "bad": {
                "members": ["r1"],
                "design": "not_a_dict",
            },
        }
        with pytest.raises(ValueError, match="must be a dict"):
            _validate_review_modes(modes, ["r1"])

    def test_unknown_keys_in_override(self):
        """(b) フェーズ上書きに未知のキーがある場合に ValueError。"""
        modes = {
            "bad": {
                "members": ["r1"],
                "code": {
                    "members": ["r1"],
                    "unknown_field": 42,
                },
            },
        }
        with pytest.raises(ValueError, match="unknown keys"):
            _validate_review_modes(modes, ["r1"])

    def test_unknown_members_in_override(self):
        """(c) フェーズ上書き内の members が REVIEWERS にない場合に ValueError。"""
        modes = {
            "bad": {
                "members": ["r1"],
                "design": {
                    "members": ["r1", "unknown_reviewer"],
                },
            },
        }
        with pytest.raises(ValueError, match="unknown reviewers"):
            _validate_review_modes(modes, ["r1"])

    def test_no_phase_overrides_passes(self, monkeypatch):
        """フェーズ上書きなしの設定は正常。"""
        import config
        modes = {
            "simple": {
                "members": ["r1", "r2"],
                "min_reviews": 2,
            },
        }
        monkeypatch.setattr(config, "DEFAULT_REVIEW_MODE", "simple")
        _validate_review_modes(modes, ["r1", "r2"])

    def test_empty_modes_passes(self, monkeypatch):
        """空の REVIEW_MODES でもエラーにならない（DEFAULT_REVIEW_MODE を無効化）。"""
        import config
        monkeypatch.setattr(config, "DEFAULT_REVIEW_MODE", "__none__")
        # DEFAULT_REVIEW_MODE が REVIEW_MODES に存在しない場合は ValueError
        with pytest.raises(ValueError, match="DEFAULT_REVIEW_MODE"):
            _validate_review_modes({}, [])


# ---------------------------------------------------------------------------
# Issue #204: _validate_reviewer_tiers — フェーズ上書き warning
# ---------------------------------------------------------------------------

class TestValidateReviewerTiersPhaseOverride:

    def test_phase_override_unknown_reviewer_warns(self, monkeypatch):
        """フェーズ上書き内に REVIEWER_TIERS にないレビュアーがいると warning。"""
        import logging
        import config
        from engine.reviewer import _validate_reviewer_tiers

        monkeypatch.setattr(config, "REVIEWER_TIERS", {
            "regular": ["r1", "r2"],
            "free": [],
            "short-context": [],
        })
        monkeypatch.setattr(config, "REVIEW_MODES", {
            "test_mode": {
                "members": ["r1", "r2"],
                "design": {
                    "members": ["r1", "unknown_agent"],
                },
            },
        })

        warnings: list[str] = []

        def capture_warning(self: logging.Logger, msg: object, *args: object, **kwargs: object) -> None:
            warnings.append(str(msg) % args if args else str(msg))

        monkeypatch.setattr(logging.Logger, "warning", capture_warning)
        _validate_reviewer_tiers()

        assert any(
            "unknown_agent" in w and "test_mode" in w and "design" in w
            for w in warnings
        )
