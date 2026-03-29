"""tests/test_review_mode_validation.py — review_mode バリデーションテスト (Issue #252)"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Capture real functions before conftest mocks them (from-import binds to the
# original function object; conftest's patch("engine.reviewer._reset_reviewers")
# replaces the module attribute but does not affect this local binding).
from engine.reviewer import _reset_reviewers as _real_reset_reviewers
from engine.reviewer import _reset_short_context_reviewers as _real_reset_short_context_reviewers


def _write_pipeline(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _read_pipeline(path: Path) -> dict:
    return json.loads(path.read_text())


class TestResetReviewersValidation:
    """_reset_reviewers が不明モードで KeyError を出す"""

    def test_unknown_review_mode_raises_in_reset_reviewers(self, monkeypatch):
        import engine.reviewer as _mod
        monkeypatch.setattr(_mod, "REVIEW_MODES", {
            "standard": {"members": ["reviewer1"], "min_reviews": 1, "grace_period_sec": 0},
        })
        with pytest.raises(KeyError):
            _real_reset_reviewers(review_mode="nonexistent")


class TestGetPhaseConfigValidation:
    """get_phase_config が不明モードで KeyError を出す"""

    def test_unknown_review_mode_raises_in_get_phase_config(self, monkeypatch):
        import engine.fsm as _mod
        monkeypatch.setattr(_mod, "REVIEW_MODES", {
            "standard": {"members": ["reviewer1"], "min_reviews": 1, "grace_period_sec": 0},
        })
        data = {"review_mode": "nonexistent"}
        with pytest.raises(KeyError):
            _mod.get_phase_config(data, "design")


class TestShortContextResetValidation:
    """_reset_short_context_reviewers が不明モードで KeyError を出す"""

    def test_unknown_review_mode_raises_in_short_context_reset(self, monkeypatch):
        import engine.reviewer as _mod
        monkeypatch.setattr(_mod, "REVIEW_MODES", {
            "standard": {"members": ["reviewer1"], "min_reviews": 1, "grace_period_sec": 0},
        })
        with pytest.raises(KeyError):
            _real_reset_short_context_reviewers(review_mode="nonexistent")


class TestWatchdogDisablesOnKeyError:
    """watchdog の process() が KeyError 時に enabled=False にする"""

    def test_watchdog_disables_on_review_mode_keyerror(self, tmp_path, monkeypatch):
        import config as _config
        import pipeline_io as _pio
        import watchdog as _wd

        pipelines_dir = tmp_path / "pipelines"
        pipelines_dir.mkdir()
        path = pipelines_dir / "testpj.json"

        # INITIALIZE 状態 + 不明な review_mode を設定
        pipeline_data = {
            "project": "testpj",
            "state": "INITIALIZE",
            "enabled": True,
            "batch": [{"issue": 100}],
            "history": [],
            "gitlab": "testns/testpj",
            "implementer": "implementer1",
            "review_mode": "nonexistent",
        }
        _write_pipeline(path, pipeline_data)

        monkeypatch.setattr(_config, "PIPELINES_DIR", pipelines_dir)
        monkeypatch.setattr(_pio, "PIPELINES_DIR", pipelines_dir)

        # REVIEW_MODES に "nonexistent" を含まない設定
        limited_modes = {
            "standard": {"members": ["reviewer1"], "min_reviews": 1, "grace_period_sec": 0},
        }
        monkeypatch.setattr(_wd, "REVIEW_MODES", limited_modes)
        # check_transition 内で使う fsm モジュールの REVIEW_MODES も差し替え
        import engine.fsm as _fsm
        monkeypatch.setattr(_fsm, "REVIEW_MODES", limited_modes)

        # process() が例外を投げずに return すること
        _wd.process(path)

        # pipeline.json の enabled が False に設定されていること
        data = _read_pipeline(path)
        assert data["enabled"] is False

    def test_watchdog_disables_on_missing_review_mode(self, tmp_path, monkeypatch):
        """review_mode キーが存在しない場合も watchdog が disable する"""
        import config as _config
        import pipeline_io as _pio
        import watchdog as _wd

        pipelines_dir = tmp_path / "pipelines"
        pipelines_dir.mkdir()
        path = pipelines_dir / "testpj.json"

        # review_mode キーなしで INITIALIZE 状態のパイプラインを構築
        pipeline_data = {
            "project": "testpj",
            "state": "INITIALIZE",
            "enabled": True,
            "batch": [{"issue": 100}],
            "history": [],
            "gitlab": "testns/testpj",
            "implementer": "implementer1",
            # "review_mode" キーは意図的に省略
        }
        path.write_text(json.dumps(pipeline_data))

        monkeypatch.setattr(_config, "PIPELINES_DIR", pipelines_dir)
        monkeypatch.setattr(_pio, "PIPELINES_DIR", pipelines_dir)

        limited_modes = {
            "standard": {"members": ["reviewer1"], "min_reviews": 1, "grace_period_sec": 0},
        }
        monkeypatch.setattr(_wd, "REVIEW_MODES", limited_modes)
        import engine.fsm as _fsm
        monkeypatch.setattr(_fsm, "REVIEW_MODES", limited_modes)

        _wd.process(path)

        data = json.loads(path.read_text())
        assert data["enabled"] is False
