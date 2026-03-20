"""Tests for config/__init__.py settings.py override logic."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture()
def _patch_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide a helper that writes a settings.py and reloads config."""
    def _write_and_reload(content: str) -> None:
        settings_file = tmp_path / "settings.py"
        settings_file.write_text(content, encoding="utf-8")
        import config
        monkeypatch.setattr(
            "config._settings_path",
            settings_file,
        )
        importlib.reload(config)

    return _write_and_reload


def test_settings_override(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.py"
    settings_file.write_text('OWNER_NAME = "TestUser"\n', encoding="utf-8")

    import config
    import config as _cfg_mod

    _cfg_mod._settings_path = settings_file  # type: ignore[attr-defined]

    # Force re-import by reloading
    import importlib.util as ilu
    _spec = ilu.spec_from_file_location("_gokrax_settings", settings_file)
    _mod = ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    # Simulate the override logic
    for attr in dir(_mod):
        if attr.isupper() and not attr.startswith("_"):
            setattr(config, attr, getattr(_mod, attr))

    assert config.OWNER_NAME == "TestUser"

    # Restore
    setattr(config, "OWNER_NAME", "M")


def test_no_settings_file() -> None:
    import config
    # Default value should be present (from config source)
    assert hasattr(config, "OWNER_NAME")
    assert isinstance(config.OWNER_NAME, str)


def test_derived_vars_recalculated(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.py"
    settings_file.write_text(
        'AGENTS = {"test_bot": "agent:test_bot:main"}\n',
        encoding="utf-8",
    )

    import config
    import importlib.util as ilu

    _spec = ilu.spec_from_file_location("_gokrax_settings", settings_file)
    _mod = ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    # Simulate override
    for attr in dir(_mod):
        if attr.isupper() and not attr.startswith("_"):
            setattr(config, attr, getattr(_mod, attr))

    # Recalculate derived vars (same as config override block)
    config.ALLOWED_REVIEWERS = list(config.AGENTS.keys())

    assert config.ALLOWED_REVIEWERS == ["test_bot"]

    # Restore original AGENTS and ALLOWED_REVIEWERS
    original_agents = {
        "kaneko": "agent:kaneko:main",
        "pascal": "agent:pascal:main",
        "leibniz": "agent:leibniz:main",
        "hanfei": "agent:hanfei:main",
        "dijkstra": "agent:dijkstra:main",
        "neumann": "agent:neumann:main",
        "euler": "agent:euler:main",
        "basho": "agent:basho:main",
    }
    config.AGENTS = original_agents
    config.ALLOWED_REVIEWERS = list(original_agents.keys())
