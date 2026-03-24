"""Tests for config/__init__.py settings.py override logic."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import config


@pytest.fixture()
def _patch_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a settings.py, set GOKRAX_SETTINGS env var, reload config, and restore."""
    # Snapshot mutable config attributes before reload
    _snapshot: dict[str, object] = {}
    for attr in dir(config):
        if attr.isupper() and not attr.startswith("_"):
            _snapshot[attr] = getattr(config, attr)

    def _write_and_reload(content: str) -> None:
        settings_file = tmp_path / "settings.py"
        settings_file.write_text(content, encoding="utf-8")
        monkeypatch.setenv("GOKRAX_SETTINGS", str(settings_file))
        importlib.reload(config)

    yield _write_and_reload

    # Restore: unset env var, reload to reset, then forcibly restore snapshot
    monkeypatch.delenv("GOKRAX_SETTINGS", raising=False)
    importlib.reload(config)
    # Restore exact object references so other modules' top-level imports stay valid
    for attr, val in _snapshot.items():
        setattr(config, attr, val)



def test_settings_override(_patch_settings) -> None:
    _patch_settings('OWNER_NAME = "TestUser"\nREVIEWERS = ["r1"]\nIMPLEMENTERS = ["i1"]\n')
    assert config.OWNER_NAME == "TestUser"


def test_no_settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """settings.py が存在しない場合は FileNotFoundError。"""
    # Snapshot before breaking config
    snapshot: dict[str, object] = {}
    for attr in dir(config):
        if attr.isupper() and not attr.startswith("_"):
            snapshot[attr] = getattr(config, attr)

    monkeypatch.setenv("GOKRAX_SETTINGS", str(tmp_path / "nonexistent.py"))
    with pytest.raises(FileNotFoundError, match="settings.py not found"):
        importlib.reload(config)

    # Restore config fully
    monkeypatch.delenv("GOKRAX_SETTINGS", raising=False)
    importlib.reload(config)
    for attr, val in snapshot.items():
        setattr(config, attr, val)


def test_derived_vars_recalculated(_patch_settings) -> None:
    _patch_settings('REVIEWERS = ["rev_a"]\nIMPLEMENTERS = ["impl_a"]\n')
    assert config.AGENTS == {"rev_a": "agent:rev_a:main", "impl_a": "agent:impl_a:main"}
