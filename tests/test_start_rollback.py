"""tests/test_start_rollback.py — cmd_start / cmd_qrun 失敗時のロールバックテスト (Issue #251)"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _write_pipeline(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _read_pipeline(path: Path) -> dict:
    return json.loads(path.read_text())


def _make_idle_pipeline(project: str = "testpj") -> dict:
    return {
        "project": project, "state": "IDLE",
        "enabled": False, "batch": [], "history": [],
        "gitlab": f"testns/{project}", "repo_path": f"/tmp/repo/{project}",
        "implementer": "implementer1",
        "review_mode": "standard",
    }


def _setup_env(tmp_path: Path, monkeypatch, project: str = "testpj") -> Path:
    """Common environment setup. Returns pipeline path."""
    import config as _config
    import pipeline_io as _pio

    pipelines_dir = tmp_path / "pipelines"
    pipelines_dir.mkdir()
    path = pipelines_dir / f"{project}.json"
    _write_pipeline(path, _make_idle_pipeline(project))

    monkeypatch.setattr(_config, "PIPELINES_DIR", pipelines_dir)
    monkeypatch.setattr(_pio, "PIPELINES_DIR", pipelines_dir)
    # Also patch commands.dev and gokrax PIPELINES_DIR
    import commands.dev as _cdev
    import gokrax as _gk
    monkeypatch.setattr(_cdev, "PIPELINES_DIR", pipelines_dir)
    monkeypatch.setattr(_gk, "PIPELINES_DIR", pipelines_dir)
    return path


class TestCmdStartRollback:
    """Test: cmd_start が step 6 以降で失敗した場合のロールバック"""

    def test_cmd_start_rollback_on_transition_failure(self, tmp_path, monkeypatch):
        """cmd_transition が失敗した場合、pipeline が IDLE + enabled=False に戻る"""
        path = _setup_env(tmp_path, monkeypatch)

        monkeypatch.setattr("commands.dev.cmd_triage", lambda a: None)
        monkeypatch.setattr("commands.dev._fetch_open_issues",
                            lambda g: [(100, "Test issue")])
        monkeypatch.setattr("commands.dev.cmd_transition",
                            MagicMock(side_effect=SystemExit("transition failed")))
        monkeypatch.setattr("gokrax._start_loop", lambda: None)
        monkeypatch.setattr("gokrax._stop_loop", lambda: None)
        monkeypatch.setattr("gokrax._any_pj_enabled", lambda: False)

        from commands.dev import cmd_start
        args = argparse.Namespace(
            project="testpj", issue=[100], mode=None,
            keep_context=None, keep_ctx_batch=None, keep_ctx_intra=None,
            keep_ctx_all=None, keep_ctx_none=None,
            p2_fix=None, comment=None,
            skip_cc_plan=None, no_skip_cc_plan=None,
            skip_test=None, no_skip_test=None,
            skip_assess=None, no_skip_assess=None,
            skip_design=None, no_skip_design=None,
            no_cc=None, no_no_cc=None,
            exclude_high_risk=None, no_exclude_high_risk=None,
            exclude_any_risk=None, no_exclude_any_risk=None,
            allow_closed=False,
        )

        with pytest.raises(SystemExit):
            cmd_start(args)

        data = _read_pipeline(path)
        assert data["state"] == "IDLE"
        assert data["enabled"] is False

    def test_cmd_start_rollback_stops_loop(self, tmp_path, monkeypatch):
        """cmd_transition 失敗時、他に enabled な PJ がなければ _stop_loop が呼ばれる"""
        path = _setup_env(tmp_path, monkeypatch)

        monkeypatch.setattr("commands.dev.cmd_triage", lambda a: None)
        monkeypatch.setattr("commands.dev._fetch_open_issues",
                            lambda g: [(100, "Test issue")])
        monkeypatch.setattr("commands.dev.cmd_transition",
                            MagicMock(side_effect=SystemExit("transition failed")))
        monkeypatch.setattr("gokrax._start_loop", lambda: None)

        stop_loop_mock = MagicMock()
        monkeypatch.setattr("gokrax._stop_loop", stop_loop_mock)
        monkeypatch.setattr("gokrax._any_pj_enabled", lambda: False)

        from commands.dev import cmd_start
        args = argparse.Namespace(
            project="testpj", issue=[100], mode=None,
            keep_context=None, keep_ctx_batch=None, keep_ctx_intra=None,
            keep_ctx_all=None, keep_ctx_none=None,
            p2_fix=None, comment=None,
            skip_cc_plan=None, no_skip_cc_plan=None,
            skip_test=None, no_skip_test=None,
            skip_assess=None, no_skip_assess=None,
            skip_design=None, no_skip_design=None,
            no_cc=None, no_no_cc=None,
            exclude_high_risk=None, no_exclude_high_risk=None,
            exclude_any_risk=None, no_exclude_any_risk=None,
            allow_closed=False,
        )

        with pytest.raises(SystemExit):
            cmd_start(args)

        stop_loop_mock.assert_called_once()

    def test_cmd_start_rollback_keeps_loop_if_other_pj_enabled(self, tmp_path, monkeypatch):
        """他に enabled な PJ がある場合、_stop_loop は呼ばれない"""
        path = _setup_env(tmp_path, monkeypatch)

        monkeypatch.setattr("commands.dev.cmd_triage", lambda a: None)
        monkeypatch.setattr("commands.dev._fetch_open_issues",
                            lambda g: [(100, "Test issue")])
        monkeypatch.setattr("commands.dev.cmd_transition",
                            MagicMock(side_effect=SystemExit("transition failed")))
        monkeypatch.setattr("gokrax._start_loop", lambda: None)

        stop_loop_mock = MagicMock()
        monkeypatch.setattr("gokrax._stop_loop", stop_loop_mock)
        monkeypatch.setattr("gokrax._any_pj_enabled", lambda: True)

        from commands.dev import cmd_start
        args = argparse.Namespace(
            project="testpj", issue=[100], mode=None,
            keep_context=None, keep_ctx_batch=None, keep_ctx_intra=None,
            keep_ctx_all=None, keep_ctx_none=None,
            p2_fix=None, comment=None,
            skip_cc_plan=None, no_skip_cc_plan=None,
            skip_test=None, no_skip_test=None,
            skip_assess=None, no_skip_assess=None,
            skip_design=None, no_skip_design=None,
            no_cc=None, no_no_cc=None,
            exclude_high_risk=None, no_exclude_high_risk=None,
            exclude_any_risk=None, no_exclude_any_risk=None,
            allow_closed=False,
        )

        with pytest.raises(SystemExit):
            cmd_start(args)

        stop_loop_mock.assert_not_called()


class TestCmdQrunRollback:
    """Test: cmd_qrun の失敗パスでのロールバック"""

    def test_cmd_qrun_failure_stops_loop(self, tmp_path, monkeypatch):
        """パス A: cmd_start 内で失敗 → _stop_loop が呼ばれる"""
        import config as _config

        path = _setup_env(tmp_path, monkeypatch)

        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("testpj 100\n")
        monkeypatch.setattr(_config, "QUEUE_FILE", queue_file)

        monkeypatch.setattr("commands.dev.cmd_start",
                            MagicMock(side_effect=SystemExit("start failed")))

        stop_loop_mock = MagicMock()
        monkeypatch.setattr("gokrax._stop_loop", stop_loop_mock)
        monkeypatch.setattr("gokrax._any_pj_enabled", lambda: False)

        from commands.dev import cmd_qrun
        args = argparse.Namespace(queue=str(queue_file), dry_run=False)

        with pytest.raises(SystemExit):
            cmd_qrun(args)

        stop_loop_mock.assert_called_once()

        # queue entry が復元されていることも確認
        content = queue_file.read_text()
        assert "testpj 100" in content
        assert "# done:" not in content

    def test_cmd_qrun_post_start_failure_rollback(self, tmp_path, monkeypatch):
        """パス B: cmd_start 成功後に後続処理で失敗 → pipeline が IDLE + enabled=False に戻る"""
        import config as _config

        path = _setup_env(tmp_path, monkeypatch)

        # cmd_start が成功したかのように pipeline を INITIALIZE + enabled=True にする
        def fake_cmd_start(a):
            data = _read_pipeline(path)
            data["state"] = "INITIALIZE"
            data["enabled"] = True
            data["batch"] = [{"issue": 100}]
            _write_pipeline(path, data)

        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("testpj 100\n")
        monkeypatch.setattr(_config, "QUEUE_FILE", queue_file)

        monkeypatch.setattr("commands.dev.cmd_start", fake_cmd_start)
        monkeypatch.setattr("task_queue.save_queue_options_to_pipeline",
                            MagicMock(side_effect=RuntimeError("save failed")))

        stop_loop_mock = MagicMock()
        monkeypatch.setattr("gokrax._stop_loop", stop_loop_mock)
        monkeypatch.setattr("gokrax._any_pj_enabled", lambda: False)

        from commands.dev import cmd_qrun
        args = argparse.Namespace(queue=str(queue_file), dry_run=False)

        with pytest.raises(RuntimeError, match="save failed"):
            cmd_qrun(args)

        data = _read_pipeline(path)
        assert data["state"] == "IDLE"
        assert data["enabled"] is False
        stop_loop_mock.assert_called_once()
