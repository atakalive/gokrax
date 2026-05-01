"""tests/test_qrun_budget.py — qrun budget 上限保証の回帰テスト。"""

import importlib
import json
from unittest.mock import patch

import pytest

from engine.glab import GlabResult


def _ok_json(data: dict) -> GlabResult:
    return GlabResult(ok=True, stdout=json.dumps(data), stderr="", returncode=0, error=None)


class TestFetchIssueStateRetries:
    def test_uses_retries_2(self):
        import engine.glab
        importlib.reload(engine.glab)
        with patch("engine.glab.run_glab", return_value=_ok_json({"state": "opened"})) as mock:
            engine.glab.fetch_issue_state(42, "testns/proj")
        _, kwargs = mock.call_args
        assert kwargs["retries"] == 2


class TestCmdTriageMaxBatchGuard:
    def test_rejects_over_max_batch(self):
        import argparse
        from commands.dev.lifecycle import cmd_triage
        from config import MAX_BATCH

        args = argparse.Namespace(
            project="gokrax",
            issue=list(range(1, MAX_BATCH + 2)),
            title=[],
            allow_closed=False,
        )
        with patch("commands.dev.lifecycle.run_glab") as mock:
            with pytest.raises(SystemExit, match="Too many issues"):
                cmd_triage(args)
        mock.assert_not_called()


class TestAppendEntryMaxBatch:
    def test_rejects_over_max_batch(self, tmp_path):
        from task_queue import append_entry
        from config import MAX_BATCH

        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")
        issues = ",".join(str(i) for i in range(1, MAX_BATCH + 2))
        with pytest.raises(ValueError, match="Too many issues"):
            append_entry(queue_file, f"gokrax {issues}")


class TestPopNextQueueEntryOverflow:
    def test_skips_over_max_batch_entry(self, tmp_path):
        from task_queue import pop_next_queue_entry
        from config import MAX_BATCH

        issues = ",".join(str(i) for i in range(1, MAX_BATCH + 2))
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text(f"gokrax {issues}\n")

        pipeline_path = tmp_path / "gokrax.json"
        pipeline_path.write_text('{}')

        with patch("task_queue.get_path", return_value=pipeline_path), \
             patch("task_queue.load_pipeline", return_value={"state": "IDLE", "gitlab": "testns/gokrax"}), \
             patch("engine.glab.fetch_issue_state") as mock_fetch:
            result = pop_next_queue_entry(queue_file)

        assert result is None
        mock_fetch.assert_not_called()
        content = queue_file.read_text()
        assert "# done:" in content
