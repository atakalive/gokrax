"""Tests for automerge in DEFAULT_QUEUE_OPTIONS."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from task_queue import parse_queue_line  # noqa: E402


def test_parse_queue_line_uses_default_automerge(monkeypatch):
    """DEFAULT_QUEUE_OPTIONS の automerge 値が parse_queue_line に適用される"""
    monkeypatch.setattr("config.DEFAULT_QUEUE_OPTIONS", {"automerge": False})
    monkeypatch.setattr("config.PROJECT_QUEUE_OPTIONS", {})
    monkeypatch.setattr("task_queue.get_path", lambda proj: Path("/tmp/dummy"))
    entry = parse_queue_line("gokrax 1")
    assert entry["automerge"] is False


def test_parse_queue_line_explicit_overrides_default(monkeypatch):
    """明示的な automerge トークンは DEFAULT_QUEUE_OPTIONS より優先される"""
    monkeypatch.setattr("config.DEFAULT_QUEUE_OPTIONS", {"automerge": False})
    monkeypatch.setattr("config.PROJECT_QUEUE_OPTIONS", {})
    monkeypatch.setattr("task_queue.get_path", lambda proj: Path("/tmp/dummy"))
    entry = parse_queue_line("gokrax 1 automerge")
    assert entry["automerge"] is True


def test_parse_queue_line_project_override(monkeypatch):
    """PROJECT_QUEUE_OPTIONS の automerge がプロジェクト固有に適用される"""
    monkeypatch.setattr("config.DEFAULT_QUEUE_OPTIONS", {"automerge": True})
    monkeypatch.setattr("config.PROJECT_QUEUE_OPTIONS", {"gokrax": {"automerge": False}})
    monkeypatch.setattr("task_queue.get_path", lambda proj: Path("/tmp/dummy"))
    entry = parse_queue_line("gokrax 1")
    assert entry["automerge"] is False


def test_settings_example_has_automerge():
    """settings.example.py の DEFAULT_QUEUE_OPTIONS に automerge が含まれる"""
    content = (ROOT / "settings.example.py").read_text()
    assert '"automerge"' in content or "'automerge'" in content
