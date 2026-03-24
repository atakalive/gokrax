"""tests/test_review_config.py — Issue #203: review_config 生成・読み取りテスト"""

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
        """(b') ベースに min_reviews があり override で members のみ変更 — ベースの min_reviews が使われる。"""
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
        # ベースの min_reviews=2 が使われる（override に min_reviews がないため）
        assert result["min_reviews"] == 2

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
