"""Tests for _auto_push_and_close push retry / failure handling (#215)."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from engine.cc import _auto_push_and_close


PROJ = "test-pj"
GITLAB = "testns/test-pj"
REPO = "/tmp/fake-repo"
BATCH = [{"issue": 1}, {"issue": 2}]


def _ok(stdout: str = "", stderr: str = "") -> MagicMock:
    """returncode=0 の CompletedProcess もどき。"""
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    m.stderr = stderr
    return m


def _fail(stderr: str = "error") -> MagicMock:
    """returncode=1 の CompletedProcess もどき。"""
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    m.stderr = stderr
    return m


class TestPushSuccessClosesIssues:
    """push 成功 → issue close 実行。"""

    def test_push_ok_then_close(self) -> None:
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            _auto_push_and_close(REPO, GITLAB, BATCH, PROJ)

        close_calls = [
            c for c in mock_run.call_args_list
            if "close" in (c[0][0] if c[0] else c[1].get("args", []))
        ]
        assert len(close_calls) == 2


class TestPushRetryThenSuccess:
    """push 1回失敗 → リトライで成功 → issue close 実行。"""

    def test_retry_success(self) -> None:
        push_fail = _fail()
        push_ok = _ok()
        close_ok = _ok()

        def side_effect(cmd, *args, **kwargs):
            if cmd[0] == "git":
                return results_git.pop(0)
            return close_ok

        results_git = [push_fail, push_ok]
        with patch("subprocess.run", side_effect=side_effect) as mock_run, \
             patch("time.sleep"):
            _auto_push_and_close(REPO, GITLAB, BATCH, PROJ)

        close_calls = [
            c for c in mock_run.call_args_list
            if isinstance(c[0][0], list) and "close" in c[0][0]
        ]
        assert len(close_calls) == 2


class TestPushAllRetriesFail:
    """push 3回失敗 → issue close スキップ + タイトル更新。"""

    def test_all_retries_fail(self) -> None:
        push_fail = _fail()
        view_ok = MagicMock(
            returncode=0,
            stdout=json.dumps({"title": "Original title"}),
            stderr="",
        )
        update_ok = _ok()

        call_count = {"git": 0}

        def side_effect(cmd, *args, **kwargs):
            if cmd[0] == "git":
                call_count["git"] += 1
                return push_fail
            if "view" in cmd:
                return view_ok
            if "update" in cmd:
                return update_ok
            return _fail()

        with patch("subprocess.run", side_effect=side_effect) as mock_run, \
             patch("time.sleep"):
            _auto_push_and_close(REPO, GITLAB, BATCH, PROJ)

        # push は 3 回試行
        assert call_count["git"] == 3
        # issue close は呼ばれない
        close_calls = [
            c for c in mock_run.call_args_list
            if isinstance(c[0][0], list) and "close" in c[0][0]
        ]
        assert len(close_calls) == 0
        # タイトル更新が呼ばれる
        update_calls = [
            c for c in mock_run.call_args_list
            if isinstance(c[0][0], list) and "update" in c[0][0]
        ]
        assert len(update_calls) == 2
        for uc in update_calls:
            assert "[PUSH FAILED] Original title" in uc[0][0]


class TestPushExceptionRetryAllFail:
    """push で例外 → リトライ → 全失敗 → issue close スキップ。"""

    def test_timeout_all_fail(self) -> None:
        call_count = {"git": 0}
        view_ok = MagicMock(
            returncode=0,
            stdout=json.dumps({"title": "Title"}),
            stderr="",
        )

        def side_effect(cmd, *args, **kwargs):
            if cmd[0] == "git":
                call_count["git"] += 1
                raise subprocess.TimeoutExpired(cmd, 60)
            if "view" in cmd:
                return view_ok
            if "update" in cmd:
                return _ok()
            return _fail()

        with patch("subprocess.run", side_effect=side_effect) as mock_run, \
             patch("time.sleep"):
            _auto_push_and_close(REPO, GITLAB, BATCH, PROJ)

        assert call_count["git"] == 3
        close_calls = [
            c for c in mock_run.call_args_list
            if isinstance(c[0][0], list) and "close" in c[0][0]
        ]
        assert len(close_calls) == 0


class TestPushExceptionThenSuccess:
    """push 1回例外 → 2回目成功 → issue close 実行。"""

    def test_exception_then_ok(self) -> None:
        push_ok = _ok()
        close_ok = _ok()
        git_calls = {"n": 0}

        def side_effect(cmd, *args, **kwargs):
            if cmd[0] == "git":
                git_calls["n"] += 1
                if git_calls["n"] == 1:
                    raise subprocess.TimeoutExpired(cmd, 60)
                return push_ok
            return close_ok

        with patch("subprocess.run", side_effect=side_effect) as mock_run, \
             patch("time.sleep"):
            _auto_push_and_close(REPO, GITLAB, BATCH, PROJ)

        close_calls = [
            c for c in mock_run.call_args_list
            if isinstance(c[0][0], list) and "close" in c[0][0]
        ]
        assert len(close_calls) == 2


class TestRepoPathEmpty:
    """repo_path 空文字 → push スキップ、issue close は実行。"""

    def test_empty_repo_path(self) -> None:
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            _auto_push_and_close("", GITLAB, BATCH, PROJ)

        # git push は呼ばれない
        git_calls = [
            c for c in mock_run.call_args_list
            if isinstance(c[0][0], list) and c[0][0][0] == "git"
        ]
        assert len(git_calls) == 0
        # issue close は呼ばれる
        close_calls = [
            c for c in mock_run.call_args_list
            if isinstance(c[0][0], list) and "close" in c[0][0]
        ]
        assert len(close_calls) == 2


class TestTitleUpdateFailContinues:
    """タイトル更新失敗時の継続: glab issue view 失敗でも他 Issue は続行。"""

    def test_view_fail_continues(self) -> None:
        push_fail = _fail()
        view_fail = _fail(stderr="not found")
        view_ok = MagicMock(
            returncode=0,
            stdout=json.dumps({"title": "Title2"}),
            stderr="",
        )
        update_ok = _ok()

        view_results = [view_fail, view_ok]

        def side_effect(cmd, *args, **kwargs):
            if cmd[0] == "git":
                return push_fail
            if "view" in cmd:
                return view_results.pop(0)
            if "update" in cmd:
                return update_ok
            return _fail()

        with patch("subprocess.run", side_effect=side_effect) as mock_run, \
             patch("time.sleep"):
            _auto_push_and_close(REPO, GITLAB, BATCH, PROJ)

        # Issue #1 の view が失敗、Issue #2 の view が成功 → update は 1 回だけ
        update_calls = [
            c for c in mock_run.call_args_list
            if isinstance(c[0][0], list) and "update" in c[0][0]
        ]
        assert len(update_calls) == 1


class TestTitleUpdateNonZeroExitContinues:
    """issue update が非ゼロ終了でも他 Issue の更新は続行される。"""

    def test_update_fail_logged_and_continues(self) -> None:
        push_fail = _fail()
        view_ok_1 = MagicMock(
            returncode=0,
            stdout=json.dumps({"title": "Title1"}),
            stderr="",
        )
        view_ok_2 = MagicMock(
            returncode=0,
            stdout=json.dumps({"title": "Title2"}),
            stderr="",
        )
        update_fail = _fail(stderr="permission denied")
        update_ok = _ok()

        view_results = [view_ok_1, view_ok_2]
        update_results = [update_fail, update_ok]

        def side_effect(cmd, *args, **kwargs):
            if cmd[0] == "git":
                return push_fail
            if "view" in cmd:
                return view_results.pop(0)
            if "update" in cmd:
                return update_results.pop(0)
            return _fail()

        with patch("subprocess.run", side_effect=side_effect) as mock_run, \
             patch("time.sleep"):
            _auto_push_and_close(REPO, GITLAB, BATCH, PROJ)

        # update は 2 回呼ばれる（1回目失敗、2回目成功）
        update_calls = [
            c for c in mock_run.call_args_list
            if isinstance(c[0][0], list) and "update" in c[0][0]
        ]
        assert len(update_calls) == 2


class TestIdempotentPushFailedPrefix:
    """冪等性: 既に [PUSH FAILED] プレフィックスがあれば重複付与しない。"""

    def test_no_duplicate_prefix(self) -> None:
        push_fail = _fail()
        view_already = MagicMock(
            returncode=0,
            stdout=json.dumps({"title": "[PUSH FAILED] Already marked"}),
            stderr="",
        )

        def side_effect(cmd, *args, **kwargs):
            if cmd[0] == "git":
                return push_fail
            if "view" in cmd:
                return view_already
            return _ok()

        with patch("subprocess.run", side_effect=side_effect) as mock_run, \
             patch("time.sleep"):
            _auto_push_and_close(REPO, GITLAB, BATCH, PROJ)

        # update は呼ばれない
        update_calls = [
            c for c in mock_run.call_args_list
            if isinstance(c[0][0], list) and "update" in c[0][0]
        ]
        assert len(update_calls) == 0
