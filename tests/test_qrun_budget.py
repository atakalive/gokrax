"""tests/test_qrun_budget.py — qrun budget 上限保証の回帰テスト。"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.glab import GlabResult


def _ok_json(data: dict) -> GlabResult:
    return GlabResult(ok=True, stdout=json.dumps(data), stderr="", returncode=0, error=None)


class TestFetchIssueStateRetries:
    def test_uses_retries_2(self):
        import engine.glab
        source = Path(engine.glab.__file__).read_text()
        in_func = False
        found = False
        for line in source.splitlines():
            if line.startswith("def fetch_issue_state"):
                in_func = True
            elif in_func and line and not line[0].isspace():
                break
            if in_func and "retries=2" in line:
                found = True
                break
        assert found, "fetch_issue_state must call run_glab with retries=2"


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


class TestParseQueueLineMaxBatch:
    def test_rejects_over_max_batch(self):
        from task_queue import parse_queue_line
        from config import MAX_BATCH
        issues = ",".join(str(i) for i in range(1, MAX_BATCH + 2))
        with pytest.raises(ValueError, match="Too many issues"):
            parse_queue_line(f"gokrax {issues}")


class TestPopNextQueueEntryOverflow:
    def test_skips_over_max_batch_entry(self, tmp_path):
        from task_queue import pop_next_queue_entry
        from config import MAX_BATCH

        issues = ",".join(str(i) for i in range(1, MAX_BATCH + 2))
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text(f"gokrax {issues}\n")

        with patch("notify.post_discord"):
            result = pop_next_queue_entry(queue_file)

        assert result is None
