"""Tests for Issue #272: QueueSkipError drain loop in _check_queue / _handle_qrun."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from task_queue import QueueSkipError


# ---------------------------------------------------------------------------
# Test 0: main() converts QueueSkipError to exit code 75
# ---------------------------------------------------------------------------

def test_main_queue_skip_error_exit_75():
    """main() catches QueueSkipError and exits with EXIT_QUEUE_SKIP (75)."""
    import sys
    from config import EXIT_QUEUE_SKIP

    # Patch cmd_start at gokrax module level (where cmds dict references it)
    with patch("gokrax.cmd_start", side_effect=QueueSkipError("all closed")), \
         patch.object(sys, "argv", ["gokrax", "start", "--pj", "test-pj", "--issue", "1"]):
        with pytest.raises(SystemExit) as exc_info:
            from gokrax import main
            main()
        assert exc_info.value.code == EXIT_QUEUE_SKIP


# ---------------------------------------------------------------------------
# Test 1: cmd_qrun re-raises QueueSkipError after cleanup
# ---------------------------------------------------------------------------

def test_cmd_qrun_reraises_queue_skip_error():
    """cmd_qrun re-raises QueueSkipError after rollback_queue_mode + _rollback_pipeline."""
    mock_entry = {
        "project": "testpj",
        "issues": "1",
        "original_line": "testpj 1\n",
    }

    with patch("task_queue.pop_next_queue_entry", return_value=mock_entry), \
         patch("commands.dev.cmd_start", side_effect=QueueSkipError("all closed")), \
         patch("task_queue.rollback_queue_mode") as mock_rb, \
         patch("commands.dev.update_pipeline"), \
         patch("commands.dev.get_path", return_value=Path("/tmp/fake.json")), \
         patch("gokrax._any_pj_enabled", return_value=True):
        with pytest.raises(QueueSkipError):
            from commands.dev import cmd_qrun
            cmd_qrun(MagicMock(queue=None, dry_run=False))
        mock_rb.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2: _check_queue retries on skip then succeeds
# ---------------------------------------------------------------------------

def test_check_queue_retries_on_skip():
    """exit 75 → retry → exit 0: subprocess.run called twice."""
    from config import EXIT_QUEUE_SKIP

    mock_result_skip = MagicMock(returncode=EXIT_QUEUE_SKIP, stdout="", stderr="skipped")
    mock_result_ok = MagicMock(returncode=0, stdout="started testpj", stderr="")

    with patch("subprocess.run", side_effect=[mock_result_skip, mock_result_ok]) as mock_run, \
         patch("config.QUEUE_FILE", MagicMock(exists=MagicMock(return_value=True))):
        from watchdog import _check_queue
        _check_queue()

    assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# Test 3: _check_queue stops on success (exit 0)
# ---------------------------------------------------------------------------

def test_check_queue_stops_on_success():
    """exit 0 → stops after 1 call."""
    mock_result = MagicMock(returncode=0, stdout="Queue empty", stderr="")

    with patch("subprocess.run", return_value=mock_result) as mock_run, \
         patch("config.QUEUE_FILE", MagicMock(exists=MagicMock(return_value=True))):
        from watchdog import _check_queue
        _check_queue()

    assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# Test 4: _check_queue drains all skipped entries then stops
# ---------------------------------------------------------------------------

def test_check_queue_drains_all_skipped_entries():
    """5x exit 75 → exit 0: subprocess.run called 6 times."""
    from config import EXIT_QUEUE_SKIP

    skip_results = [MagicMock(returncode=EXIT_QUEUE_SKIP, stdout="", stderr="skipped")] * 5
    empty_result = MagicMock(returncode=0, stdout="Queue empty", stderr="")

    with patch("subprocess.run", side_effect=skip_results + [empty_result]) as mock_run, \
         patch("config.QUEUE_FILE", MagicMock(exists=MagicMock(return_value=True))):
        from watchdog import _check_queue
        _check_queue()

    assert mock_run.call_count == 6


# ---------------------------------------------------------------------------
# Test 5: _handle_qrun retries on skip then succeeds
# ---------------------------------------------------------------------------

def test_handle_qrun_retries_on_skip():
    """QueueSkipError → next entry pop → success."""
    entry_skip = {"project": "pj1", "issues": "1", "original_line": "pj1 1\n"}
    entry_ok = {"project": "pj2", "issues": "2", "original_line": "pj2 2\n", "automerge": True}

    with patch("task_queue.pop_next_queue_entry", side_effect=[entry_skip, entry_ok]), \
         patch("gokrax.cmd_start", side_effect=[QueueSkipError("closed"), None]), \
         patch("notify.post_discord") as mock_discord, \
         patch("task_queue.rollback_queue_mode"), \
         patch("task_queue.save_queue_options_to_pipeline"), \
         patch("pipeline_io.update_pipeline"), \
         patch("pipeline_io.get_path", return_value=Path("/tmp/fake.json")):
        from watchdog import _handle_qrun
        _handle_qrun("test-msg-id")

    # Discord notification is 1 call (batched skip + success)
    assert mock_discord.call_count == 1
    msg = mock_discord.call_args.args[1]
    assert "skipped" in msg
    assert "started" in msg


# ---------------------------------------------------------------------------
# Test 6: _handle_qrun posts "Queue empty" when queue is empty
# ---------------------------------------------------------------------------

def test_handle_qrun_empty_queue():
    """pop_next_queue_entry → None → 'Queue empty' notification."""
    with patch("task_queue.pop_next_queue_entry", return_value=None), \
         patch("notify.post_discord") as mock_discord:
        from watchdog import _handle_qrun
        _handle_qrun("test-msg-id")

    mock_discord.assert_called_once()
    assert "Queue empty" in mock_discord.call_args.args[1]


# ---------------------------------------------------------------------------
# Test 7: _handle_qrun drains all skipped entries then posts summary
# ---------------------------------------------------------------------------

def test_handle_qrun_drains_all_skipped_then_empty():
    """4x skip → queue empty → batched notification."""
    entries = [
        {"project": f"pj{i}", "issues": str(i), "original_line": f"pj{i} {i}\n"}
        for i in range(1, 5)
    ]

    with patch("task_queue.pop_next_queue_entry", side_effect=entries + [None]), \
         patch("gokrax.cmd_start", side_effect=QueueSkipError("closed")), \
         patch("notify.post_discord") as mock_discord, \
         patch("task_queue.rollback_queue_mode"), \
         patch("pipeline_io.update_pipeline"), \
         patch("pipeline_io.get_path", return_value=Path("/tmp/fake.json")):
        from watchdog import _handle_qrun
        _handle_qrun("test-msg-id")

    # Discord notification is 1 call (batched)
    assert mock_discord.call_count == 1
    msg = mock_discord.call_args.args[1]
    assert "skipped 4" in msg
