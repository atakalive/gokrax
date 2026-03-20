"""Tests for update_settings.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from update_settings import main

EXAMPLE_SIMPLE = """\
from pathlib import Path

ALPHA = "a"
BETA: int = 2
GAMMA = {
    "x": 1,
    "y": 2,
}
"""


@pytest.fixture()
def base(tmp_path: Path) -> Path:
    """Create a base dir with a minimal settings.example.py."""
    (tmp_path / "settings.example.py").write_text(EXAMPLE_SIMPLE, encoding="utf-8")
    return tmp_path


def test_no_settings_file(base: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(base) == 1
    assert "settings.py not found" in capsys.readouterr().err


def test_up_to_date(base: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (base / "settings.py").write_text(
        "ALPHA = 'a'\nBETA: int = 2\nGAMMA = {}\n", encoding="utf-8"
    )
    assert main(base) == 0
    assert "up to date" in capsys.readouterr().out


def test_new_vars_appended(base: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (base / "settings.py").write_text("ALPHA = 'a'\n", encoding="utf-8")
    assert main(base) == 0

    out = capsys.readouterr().out
    assert "BETA" in out
    assert "GAMMA" in out

    content = (base / "settings.py").read_text(encoding="utf-8")
    assert "# BETA: int = 2  # NEW" in content
    assert "# NEW" in content


def test_multiline_var_appended(base: Path) -> None:
    (base / "settings.py").write_text("ALPHA = 'a'\nBETA = 2\n", encoding="utf-8")
    main(base)

    content = (base / "settings.py").read_text(encoding="utf-8")
    lines = content.splitlines()
    # Find the GAMMA block
    gamma_lines = [ln for ln in lines if "GAMMA" in ln or '"x"' in ln or '"y"' in ln]
    assert len(gamma_lines) >= 1
    # All lines should be commented
    for ln in gamma_lines:
        assert ln.startswith("# ")
    # Only the last line of the block has # NEW
    gamma_block_start = None
    gamma_block_end = None
    for idx, ln in enumerate(lines):
        if "GAMMA" in ln:
            gamma_block_start = idx
        if gamma_block_start is not None and "}" in ln:
            gamma_block_end = idx
            break
    assert gamma_block_start is not None
    assert gamma_block_end is not None
    assert lines[gamma_block_end].endswith("# NEW")
    # Middle lines should NOT have # NEW
    for i in range(gamma_block_start, gamma_block_end):
        assert not lines[i].endswith("# NEW")


def test_commented_vars_not_duplicated(base: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (base / "settings.py").write_text(
        "ALPHA = 'a'\n# BETA = 99\n# GAMMA = {}\n", encoding="utf-8"
    )
    assert main(base) == 0
    assert "up to date" in capsys.readouterr().out


def test_annotated_commented_vars_not_duplicated(
    base: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (base / "settings.py").write_text(
        "ALPHA = 'a'\n# BETA: int = 99\n# GAMMA = {}\n", encoding="utf-8"
    )
    assert main(base) == 0
    assert "up to date" in capsys.readouterr().out


def test_existing_content_unchanged(base: Path) -> None:
    original = "ALPHA = 'a'\n"
    (base / "settings.py").write_text(original, encoding="utf-8")
    main(base)

    content = (base / "settings.py").read_text(encoding="utf-8")
    assert content.startswith(original)


def test_idempotent(base: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (base / "settings.py").write_text("ALPHA = 'a'\n", encoding="utf-8")

    main(base)
    capsys.readouterr()  # clear

    content_after_first = (base / "settings.py").read_text(encoding="utf-8")

    assert main(base) == 0
    assert "up to date" in capsys.readouterr().out

    content_after_second = (base / "settings.py").read_text(encoding="utf-8")
    assert content_after_first == content_after_second
