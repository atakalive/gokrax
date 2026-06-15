"""tests/test_revise_nudge.py — Issue #380: revise nudge の具体化

revise nudge テンプレが「未完了の事実 + 完了の定義(revise CLI 実行) + 握りつぶし禁止」を
名指しで突きつける具体文になっていることを検証する。render() を直接呼び lang="en" /
GOKRAX_CLI="gokrax" を固定し、messages.PROMPT_LANG には依存しない。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from messages import render


def _code_nudge(issues_str: str, p2_note: str = "") -> str:
    return render(
        "dev.code_revise", "nudge",
        project="testpj", issues_str=issues_str,
        GOKRAX_CLI="gokrax", p2_note=p2_note, lang="en",
    )


def _design_nudge(issues_str: str, p2_note: str = "") -> str:
    return render(
        "dev.design_revise", "nudge",
        project="testpj", issues_str=issues_str,
        GOKRAX_CLI="gokrax", p2_note=p2_note, lang="en",
    )


def test_code_revise_nudge_concrete() -> None:
    out = _code_nudge("#7")
    assert "INCOMPLETE" in out
    assert "#7" in out
    assert "gokrax code-revise --pj testpj" in out
    assert "No response requested" in out
    assert "##" not in out


def test_design_revise_nudge_concrete() -> None:
    out = _design_nudge("#7")
    assert "INCOMPLETE" in out
    assert "#7" in out
    assert "No response requested" in out
    assert "gokrax design-revise --pj testpj" in out
    assert "gokrax issue-update --pj testpj" in out
    assert "once per Issue" in out
    assert "--hash" not in out


def test_revise_nudge_empty_issues_no_double_space() -> None:
    for out in (_code_nudge(""), _design_nudge("")):
        assert "testpj  " not in out
        assert "INCOMPLETE" in out


def test_revise_nudge_p2_note_injected() -> None:
    for out in (_code_nudge("#7", "\nP2_MARKER\n"), _design_nudge("#7", "\nP2_MARKER\n")):
        assert "P2_MARKER" in out
