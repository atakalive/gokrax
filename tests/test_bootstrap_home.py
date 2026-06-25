"""Tests for bootstrap_home — HOME normalization (login-user passwd home).

Hermetic: never uses the real host passwd home as an expected value (the isdir
guard makes that environment-dependent). Normalization logic is verified with
``real_login_home`` monkeypatched to a tmp_path-based path. The module is always
loaded behind the escape valve so a drifted pytest HOME cannot leak into the
process env via the import-time side-effect.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

BH_PATH = str(Path(__file__).resolve().parent.parent / "bootstrap_home.py")


def _load_bootstrap_home():
    spec = importlib.util.spec_from_file_location("bootstrap_home", BH_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # runs module-level normalize_home() (import side-effect)
    return mod


@pytest.fixture
def bh(monkeypatch, tmp_path):
    # Gate the import-time normalize BEFORE load so a drifted pytest HOME cannot
    # leak into the process env. Both env vars go through monkeypatch (restored).
    monkeypatch.setenv("GOKRAX_ALLOW_FOREIGN_HOME", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "load-sentinel"))
    mod = _load_bootstrap_home()
    assert os.environ["HOME"] == str(tmp_path / "load-sentinel")  # load was a no-op
    return mod


def test_normalize_existing_passwd_home(bh, monkeypatch, tmp_path):
    target = tmp_path / "login"
    target.mkdir()
    monkeypatch.setattr(bh, "real_login_home", lambda: str(target))
    monkeypatch.setenv("HOME", str(tmp_path / "drifted"))
    monkeypatch.delenv("GOKRAX_ALLOW_FOREIGN_HOME", raising=False)
    bh.normalize_home()
    assert os.environ["HOME"] == str(target)


def test_isdir_guard_nonexistent_home_is_noop(bh, monkeypatch, tmp_path):
    monkeypatch.setattr(bh, "real_login_home", lambda: str(tmp_path / "does-not-exist"))
    known = str(tmp_path / "known")
    monkeypatch.setenv("HOME", known)
    monkeypatch.delenv("GOKRAX_ALLOW_FOREIGN_HOME", raising=False)
    bh.normalize_home()
    assert os.environ["HOME"] == known


def test_none_passwd_home_is_noop(bh, monkeypatch, tmp_path):
    monkeypatch.setattr(bh, "real_login_home", lambda: None)
    known = str(tmp_path / "known")
    monkeypatch.setenv("HOME", known)
    monkeypatch.delenv("GOKRAX_ALLOW_FOREIGN_HOME", raising=False)
    bh.normalize_home()
    assert os.environ["HOME"] == known


def test_escape_valve_truthy_is_noop(bh, monkeypatch, tmp_path):
    target = tmp_path / "login"
    target.mkdir()
    monkeypatch.setattr(bh, "real_login_home", lambda: str(target))
    other = str(tmp_path / "other")
    monkeypatch.setenv("GOKRAX_ALLOW_FOREIGN_HOME", "1")
    monkeypatch.setenv("HOME", other)
    bh.normalize_home()
    assert os.environ["HOME"] == other


@pytest.mark.parametrize("falsy", ["", "0", "false", " 0 ", " false "])
def test_escape_valve_falsy_normalizes(bh, monkeypatch, tmp_path, falsy):
    target = tmp_path / "login"
    target.mkdir()
    monkeypatch.setattr(bh, "real_login_home", lambda: str(target))
    monkeypatch.setenv("HOME", str(tmp_path / "drifted"))
    monkeypatch.setenv("GOKRAX_ALLOW_FOREIGN_HOME", falsy)
    bh.normalize_home()
    assert os.environ["HOME"] == str(target)


def test_idempotent(bh, monkeypatch, tmp_path):
    target = tmp_path / "login"
    target.mkdir()
    monkeypatch.setattr(bh, "real_login_home", lambda: str(target))
    monkeypatch.setenv("HOME", str(tmp_path / "drifted"))
    monkeypatch.delenv("GOKRAX_ALLOW_FOREIGN_HOME", raising=False)
    bh.normalize_home()
    bh.normalize_home()
    assert os.environ["HOME"] == str(target)


@pytest.mark.skipif(sys.platform == "win32", reason="passwd not available on Windows")
def test_real_login_home_matches_passwd(bh):
    import pwd

    assert bh.real_login_home() == pwd.getpwuid(os.getuid()).pw_dir


def test_import_side_effect_respects_escape_valve(monkeypatch, tmp_path):
    sentinel = str(tmp_path / "sentinel")
    monkeypatch.setenv("GOKRAX_ALLOW_FOREIGN_HOME", "1")
    monkeypatch.setenv("HOME", sentinel)
    _load_bootstrap_home()
    assert os.environ["HOME"] == sentinel
