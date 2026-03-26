"""tests/test_qrun_skip.py — QueueSkipError によるキュースキップのテスト"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _write_pipeline(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _make_idle_pipeline(project: str = "gokrax") -> dict:
    return {
        "project": project, "state": "IDLE",
        "enabled": False, "batch": [], "history": [],
        "gitlab": f"testns/{project}", "repo_path": f"/tmp/repo/{project}",
        "implementer": "implementer1",
    }


class TestCmdQrunSkip:
    """Test A: cmd_qrun で QueueSkipError 時にエントリが復元されない"""

    def test_skip_error_no_restore(self, tmp_path, monkeypatch):
        import config as _config
        import pipeline_io as _pio
        from task_queue import QueueSkipError

        pipelines_dir = tmp_path / "pipelines"
        pipelines_dir.mkdir()
        path = pipelines_dir / "gokrax.json"
        _write_pipeline(path, _make_idle_pipeline())

        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("gokrax 100\n")

        monkeypatch.setattr(_config, "PIPELINES_DIR", pipelines_dir)
        monkeypatch.setattr(_config, "QUEUE_FILE", queue_file)
        monkeypatch.setattr(_pio, "PIPELINES_DIR", pipelines_dir)

        with patch("commands.dev.cmd_start",
                   side_effect=QueueSkipError("All issues are closed.")), \
             patch("commands.dev.update_pipeline"), \
             patch("task_queue.restore_queue_entry") as mock_restore, \
             patch("task_queue.rollback_queue_mode") as mock_rollback:
            from commands.dev import cmd_qrun
            args = argparse.Namespace(queue=str(queue_file), dry_run=False)
            # QueueSkipError 時は raise せず return する
            cmd_qrun(args)
            mock_restore.assert_not_called()
            mock_rollback.assert_called_once()

        # キューファイルの内容: "# done: " prefix が残っている（復元されていない）
        content = queue_file.read_text()
        assert "# done:" in content
        assert "gokrax 100" in content


class TestCmdQrunSystemExitRestore:
    """Test B: cmd_qrun で SystemExit 時にエントリが復元される（回帰テスト）"""

    def test_system_exit_restores_entry(self, tmp_path, monkeypatch):
        import config as _config
        import pipeline_io as _pio

        pipelines_dir = tmp_path / "pipelines"
        pipelines_dir.mkdir()
        path = pipelines_dir / "gokrax.json"
        _write_pipeline(path, _make_idle_pipeline())

        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("gokrax 100\n")

        monkeypatch.setattr(_config, "PIPELINES_DIR", pipelines_dir)
        monkeypatch.setattr(_config, "QUEUE_FILE", queue_file)
        monkeypatch.setattr(_pio, "PIPELINES_DIR", pipelines_dir)

        with patch("commands.dev.cmd_start",
                   side_effect=SystemExit("Cannot start: ...")), \
             patch("commands.dev.update_pipeline"), \
             patch("task_queue.restore_queue_entry") as mock_restore, \
             patch("task_queue.rollback_queue_mode") as mock_rollback:
            from commands.dev import cmd_qrun
            args = argparse.Namespace(queue=str(queue_file), dry_run=False)
            with pytest.raises(SystemExit):
                cmd_qrun(args)
            mock_restore.assert_called_once()
            mock_rollback.assert_called_once()


class TestHandleQrunSkip:
    """Test C: _handle_qrun で QueueSkipError 時にエントリが復元されない + Discord 通知"""

    def test_skip_error_no_restore_discord(self, tmp_path, monkeypatch):
        import config as _config
        import pipeline_io as _pio
        import gokrax as _gokrax
        from task_queue import QueueSkipError

        pipelines_dir = tmp_path / "pipelines"
        pipelines_dir.mkdir()
        path = pipelines_dir / "gokrax.json"
        _write_pipeline(path, _make_idle_pipeline())

        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("gokrax 100\n")

        monkeypatch.setattr(_config, "PIPELINES_DIR", pipelines_dir)
        monkeypatch.setattr(_config, "QUEUE_FILE", queue_file)
        monkeypatch.setattr(_pio, "PIPELINES_DIR", pipelines_dir)
        monkeypatch.setattr(_gokrax, "PIPELINES_DIR", pipelines_dir)

        def mock_get_path(project: str) -> Path:
            return pipelines_dir / f"{project}.json"

        def mock_load_pipeline(p: Path) -> dict:
            return {"state": "IDLE"}

        monkeypatch.setattr("task_queue.get_path", mock_get_path)
        monkeypatch.setattr("task_queue.load_pipeline", mock_load_pipeline)

        with patch("gokrax.cmd_start",
                   side_effect=QueueSkipError("All issues are closed.")), \
             patch("watchdog.update_pipeline"), \
             patch("task_queue.restore_queue_entry") as mock_restore, \
             patch("task_queue.rollback_queue_mode") as mock_rollback, \
             patch("notify.post_discord") as mock_post, \
             patch("watchdog.log"):
            from watchdog import _handle_qrun
            _handle_qrun("test-msg-id")
            mock_restore.assert_not_called()
            mock_rollback.assert_called_once()
            # Discord に "skipped" を含むメッセージが送信される
            assert mock_post.called
            call_args = mock_post.call_args[0]
            assert "skipped" in call_args[1]


class TestCmdTriageQueueSkipError:
    """Test D: cmd_triage で全 issue closed 時に QueueSkipError が raise される"""

    def test_all_closed_raises_queue_skip_error(self, tmp_pipelines, sample_pipeline):
        from task_queue import QueueSkipError

        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, sample_pipeline)

        with patch("commands.dev._fetch_issue_info", return_value=("title", "closed")):
            from commands.dev import cmd_triage
            args = argparse.Namespace(
                project="test-pj", issue=[100], title=[],
                allow_closed=False,
            )
            with pytest.raises(QueueSkipError, match="All issues are closed"):
                cmd_triage(args)


class TestMainQueueSkipError:
    """Test E: main() で QueueSkipError が SystemExit に変換される"""

    def test_queue_skip_error_becomes_system_exit(self, monkeypatch):
        from task_queue import QueueSkipError

        monkeypatch.setattr(
            sys, "argv",
            ["gokrax", "start", "--pj", "gokrax", "--issue", "100"],
        )
        with patch("gokrax.cmd_start",
                   side_effect=QueueSkipError("All issues are closed.")):
            from gokrax import main
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert "All issues are closed" in str(exc_info.value)
