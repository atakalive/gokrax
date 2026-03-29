"""Tests for save_queue_options_to_pipeline automerge behavior (Issue #269)."""

from task_queue import save_queue_options_to_pipeline


class TestSaveQueueOptionsAutomerge:
    def test_automerge_true_in_entry(self):
        """entry has automerge=True -> data gets automerge=True."""
        data: dict = {}
        entry = {"automerge": True}
        save_queue_options_to_pipeline(data, entry)
        assert data["automerge"] is True

    def test_automerge_false_in_entry(self):
        """entry has automerge=False -> data gets automerge=False."""
        data: dict = {}
        entry = {"automerge": False}
        save_queue_options_to_pipeline(data, entry)
        assert data["automerge"] is False

    def test_automerge_not_in_entry(self):
        """entry has no automerge key -> data has no automerge key."""
        data: dict = {}
        entry: dict = {}
        save_queue_options_to_pipeline(data, entry)
        assert "automerge" not in data
