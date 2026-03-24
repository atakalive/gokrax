"""Tests for the exclude CLI command."""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from commands.dev import cmd_exclude

_TEST_LITE_MEMBERS = ["reviewer1", "reviewer2"]
_TEST_STANDARD_MEMBERS = ["reviewer1", "reviewer2", "reviewer3"]
_TEST_ALLOWED = ["reviewer1", "reviewer2", "reviewer3", "reviewer4"]
_TEST_REVIEW_MODES = {
    "lite": {"members": _TEST_LITE_MEMBERS, "min_reviews": 2},
    "standard": {"members": _TEST_STANDARD_MEMBERS, "min_reviews": 3},
}


def _write_pipeline(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture
def patch_allowed(monkeypatch):
    """config の ALLOWED_REVIEWERS / REVIEW_MODES をテスト用値に差し替える。"""
    import config
    monkeypatch.setattr(config, "ALLOWED_REVIEWERS", _TEST_ALLOWED)
    monkeypatch.setattr("commands.dev.ALLOWED_REVIEWERS", _TEST_ALLOWED)
    monkeypatch.setattr(config, "REVIEW_MODES", _TEST_REVIEW_MODES)
    monkeypatch.setattr("commands.dev.REVIEW_MODES", _TEST_REVIEW_MODES)


class TestExcludeAdd:

    def test_add_reviewer(self, tmp_path, monkeypatch, patch_allowed):
        """--add で reviewer が追加されること"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": [],
        })
        args = argparse.Namespace(
            project="myproject", add=["reviewer1"], remove=None, list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert data["excluded_reviewers"] == ["reviewer1"]

    def test_add_idempotent(self, tmp_path, monkeypatch, patch_allowed):
        """--add の冪等性: 既に excluded なら変化なし"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": ["reviewer1"],
        })
        args = argparse.Namespace(
            project="myproject", add=["reviewer1"], remove=None, list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert data["excluded_reviewers"] == ["reviewer1"]

    def test_add_unknown_reviewer(self, tmp_path, monkeypatch, patch_allowed):
        """不明なレビュアー名でエラー"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": [],
        })
        args = argparse.Namespace(
            project="myproject", add=["unknown_reviewer"], remove=None, list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            with pytest.raises(SystemExit):
                cmd_exclude(args)

    def test_remove_unknown_reviewer(self, tmp_path, monkeypatch, patch_allowed):
        """--remove で不明なレビュアー名はエラー"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": [],
        })
        args = argparse.Namespace(
            project="myproject", add=None, remove=["unknown_reviewer"], list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            with pytest.raises(SystemExit):
                cmd_exclude(args)


class TestExcludeRemove:

    def test_remove_reviewer(self, tmp_path, monkeypatch, patch_allowed):
        """--remove で reviewer が削除されること"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": ["reviewer1"],
        })
        args = argparse.Namespace(
            project="myproject", add=None, remove=["reviewer1"], list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert data["excluded_reviewers"] == []

    def test_remove_idempotent(self, tmp_path, monkeypatch, patch_allowed):
        """--remove の冪等性: excluded にいなければ変化なし"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": [],
        })
        args = argparse.Namespace(
            project="myproject", add=None, remove=["reviewer1"], list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert data["excluded_reviewers"] == []


class TestDeadlockClamp:

    def test_clamp_on_add(self, tmp_path, monkeypatch, patch_allowed):
        """deadlock clamp: 全 members を除外すると min_reviews_override == 0"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "review_mode": "lite",
            "excluded_reviewers": [],
        })
        args = argparse.Namespace(
            project="myproject", add=list(_TEST_LITE_MEMBERS), remove=None, list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert data["min_reviews_override"] == 0

    def test_clamp_removed_on_remove(self, tmp_path, monkeypatch, patch_allowed):
        """deadlock clamp 解除: --remove で復旧すると min_reviews_override が消える"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "review_mode": "lite",
            "excluded_reviewers": list(_TEST_LITE_MEMBERS),
            "min_reviews_override": 0,
        })
        args = argparse.Namespace(
            project="myproject", add=None, remove=list(_TEST_LITE_MEMBERS), list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert "min_reviews_override" not in data

    def test_cross_mode_reviewer_no_clamp(self, tmp_path, monkeypatch, patch_allowed):
        """モード外レビュアーの追加が clamp に影響しないこと"""
        pipeline = tmp_path / "myproject.json"
        # rev_c は _TEST_ALLOWED にいるが lite の members にはいない
        assert "reviewer3" in _TEST_ALLOWED
        assert "reviewer3" not in _TEST_LITE_MEMBERS
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "review_mode": "lite",
            "excluded_reviewers": [],
        })
        args = argparse.Namespace(
            project="myproject", add=["reviewer3"], remove=None, list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert data["excluded_reviewers"] == ["reviewer3"]
        assert "min_reviews_override" not in data


class TestIntersectionClampLogic:
    """交差ベース deadlock clamp ロジックのテスト。

    watchdog.py の _save_excluded は process() 内のローカル関数で直接呼べないため、
    同一ロジックのレプリカで交差計算の正しさを検証する。
    watchdog 側の実装が乖離した場合はこのテストでは検出できない点に注意。
    """

    def test_cross_mode_excluded_no_spurious_clamp(self, tmp_path):
        """モード外 excluded reviewer が deadlock clamp を誤発動させないことを状態ベースで検証。"""
        from pipeline_io import update_pipeline as _up

        pipeline = tmp_path / "testpj.json"
        # lite mode: members=["reviewer1", "reviewer2"], min_reviews=2
        # rev_c はモード外 → excluded に入れても effective_count は 2 のまま
        _write_pipeline(pipeline, {
            "project": "testpj",
            "state": "DESIGN_APPROVED",
            "review_mode": "lite",
            "excluded_reviewers": [],
        })

        lite_config = _TEST_REVIEW_MODES["lite"]
        excluded = ["reviewer3"]  # モード外レビュアー

        def _save_excluded_replica(data):
            """watchdog.py L933-951 の _save_excluded と同一のロジック"""
            data["excluded_reviewers"] = excluded
            effective_count = len([m for m in lite_config["members"] if m not in excluded])
            min_reviews = lite_config["min_reviews"]
            if effective_count < min_reviews:
                clamped = max(effective_count, 0)
                data["min_reviews_override"] = clamped
            else:
                data.pop("min_reviews_override", None)

        _up(pipeline, _save_excluded_replica)
        data = _load(pipeline)

        # モード外 excluded なので effective_count == 2 (rev_a, rev_b 健在)
        # min_reviews == 2 なので clamp は不要
        assert data["excluded_reviewers"] == ["reviewer3"]
        assert "min_reviews_override" not in data, (
            "Cross-mode excluded reviewer should not trigger spurious deadlock clamp"
        )


class TestExcludeList:
    def test_list_empty(self, tmp_path, capsys):
        """--list: 空リストの表示"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": [],
        })
        args = argparse.Namespace(
            project="myproject", add=None, remove=None, list=True,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        out = capsys.readouterr().out
        assert "excluded_reviewers = []" in out

    def test_list_nonempty(self, tmp_path, capsys, monkeypatch):
        """--list: 非空リストの表示"""
        monkeypatch.setattr("config.MASK_AGENT_NAMES", False)
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": ["reviewer1"],
        })
        args = argparse.Namespace(
            project="myproject", add=None, remove=None, list=True,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        out = capsys.readouterr().out
        assert "excluded_reviewers = ['reviewer1']" in out
