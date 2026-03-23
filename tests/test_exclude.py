"""Tests for the exclude CLI command."""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from commands.dev import cmd_exclude
from config import ALLOWED_REVIEWERS, REVIEW_MODES


def _write_pipeline(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


class TestExcludeAdd:
    def test_add_reviewer(self, tmp_path):
        """--add で reviewer が追加されること"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": [],
        })
        args = argparse.Namespace(
            project="myproject", add=["pascal"], remove=None, list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert data["excluded_reviewers"] == ["pascal"]

    def test_add_idempotent(self, tmp_path):
        """--add の冪等性: 既に excluded なら変化なし"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": ["pascal"],
        })
        args = argparse.Namespace(
            project="myproject", add=["pascal"], remove=None, list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert data["excluded_reviewers"] == ["pascal"]

    def test_add_unknown_reviewer(self, tmp_path):
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


    def test_remove_unknown_reviewer(self, tmp_path):
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
    def test_remove_reviewer(self, tmp_path):
        """--remove で reviewer が削除されること"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": ["pascal"],
        })
        args = argparse.Namespace(
            project="myproject", add=None, remove=["pascal"], list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert data["excluded_reviewers"] == []

    def test_remove_idempotent(self, tmp_path):
        """--remove の冪等性: excluded にいなければ変化なし"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": [],
        })
        args = argparse.Namespace(
            project="myproject", add=None, remove=["pascal"], list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert data["excluded_reviewers"] == []


class TestDeadlockClamp:
    def test_clamp_on_add(self, tmp_path):
        """deadlock clamp: 全 members を除外すると min_reviews_override == 0"""
        pipeline = tmp_path / "myproject.json"
        lite_members = REVIEW_MODES["lite"]["members"]
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "review_mode": "lite",
            "excluded_reviewers": [],
        })
        args = argparse.Namespace(
            project="myproject", add=list(lite_members), remove=None, list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert data["min_reviews_override"] == 0

    def test_clamp_removed_on_remove(self, tmp_path):
        """deadlock clamp 解除: --remove で復旧すると min_reviews_override が消える"""
        pipeline = tmp_path / "myproject.json"
        lite_members = REVIEW_MODES["lite"]["members"]
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "review_mode": "lite",
            "excluded_reviewers": list(lite_members),
            "min_reviews_override": 0,
        })
        args = argparse.Namespace(
            project="myproject", add=None, remove=list(lite_members), list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert "min_reviews_override" not in data

    def test_cross_mode_reviewer_no_clamp(self, tmp_path):
        """モード外レビュアーの追加が clamp に影響しないこと"""
        pipeline = tmp_path / "myproject.json"
        lite_members = REVIEW_MODES["lite"]["members"]
        # kaneko は ALLOWED_REVIEWERS にいるが lite の members にはいない
        assert "kaneko" in ALLOWED_REVIEWERS
        assert "kaneko" not in lite_members
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "review_mode": "lite",
            "excluded_reviewers": [],
        })
        args = argparse.Namespace(
            project="myproject", add=["kaneko"], remove=None, list=False,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        data = _load(pipeline)
        assert data["excluded_reviewers"] == ["kaneko"]
        assert "min_reviews_override" not in data


class TestWatchdogSaveExcludedRegression:
    """watchdog.py の _save_excluded 内 effective_count 計算が交差ベースであることの回帰テスト"""

    def test_save_excluded_uses_intersection_calc(self):
        """watchdog.py の _save_excluded が交差ベースの effective_count 計算を使っていることを確認"""
        import inspect
        import watchdog
        source = inspect.getsource(watchdog.process)
        # 旧式の計算（len(members) - len(excluded)）が含まれていないこと
        assert "len(mode_config[\"members\"]) - len(excluded)" not in source, (
            "watchdog.py still uses the old subtraction-based effective_count calculation"
        )
        # 交差ベースの計算が含まれていること
        assert "if m not in excluded" in source, (
            "watchdog.py _save_excluded should use intersection-based effective_count"
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

    def test_list_nonempty(self, tmp_path, capsys):
        """--list: 非空リストの表示"""
        pipeline = tmp_path / "myproject.json"
        _write_pipeline(pipeline, {
            "project": "myproject",
            "state": "IDLE",
            "excluded_reviewers": ["pascal"],
        })
        args = argparse.Namespace(
            project="myproject", add=None, remove=None, list=True,
        )
        with patch("commands.dev.get_path", return_value=pipeline):
            cmd_exclude(args)
        out = capsys.readouterr().out
        assert "excluded_reviewers = ['pascal']" in out
