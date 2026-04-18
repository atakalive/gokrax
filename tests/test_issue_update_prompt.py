"""tests/test_issue_update_prompt.py — Issue #320: design_plan / design_revise プロンプトが gokrax issue-update を案内する"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from messages import render

# render() を直接呼んで GOKRAX_CLI を固定する（環境依存を排除）
EXPECTED_BODY = "gokrax issue-update --pj testpj --issue N --body-file /tmp/gokrax-testpj-N.md"
EXPECTED_TMPFILE = "/tmp/gokrax-testpj-N.md"


def _render_design_plan(lang: str) -> str:
    return render(
        "dev.design_plan", "transition",
        project="testpj", issues_str="#1", comment_line="",
        GOKRAX_CLI="gokrax", repo_path="", lang=lang,
    )


def _render_design_revise(lang: str) -> str:
    return render(
        "dev.design_revise", "transition",
        project="testpj", issues_str="#1", comment_line="",
        fix_label="P0/P1 findings", p2_note="",
        GOKRAX_CLI="gokrax", repo_path="", lang=lang,
    )


def _render_code_revise(lang: str) -> str:
    return render(
        "dev.code_revise", "transition",
        project="testpj", issues_str="#1", comment_line="",
        fix_label="P0/P1 findings", p2_note="",
        GOKRAX_CLI="gokrax", repo_path="", lang=lang,
    )


class TestDesignPlanIssueUpdate:
    """design_plan (ja/en) は本 Issue の変更対象 — issue-update を含むこと。"""

    def test_ja_contains_issue_update(self) -> None:
        msg = _render_design_plan("ja")
        assert EXPECTED_BODY in msg
        assert EXPECTED_TMPFILE in msg
        assert "glab issue update" not in msg

    def test_en_contains_issue_update(self) -> None:
        msg = _render_design_plan("en")
        assert EXPECTED_BODY in msg
        assert EXPECTED_TMPFILE in msg
        assert "glab issue update" not in msg


class TestDesignReviseIssueUpdate:
    """design_revise (ja/en) は本 Issue の変更対象 — issue-update を含むこと。"""

    def test_ja_contains_issue_update(self) -> None:
        msg = _render_design_revise("ja")
        assert EXPECTED_BODY in msg
        assert EXPECTED_TMPFILE in msg
        assert "glab issue update" not in msg
        # design-revise の完了報告行が残ること
        assert "gokrax design-revise --pj testpj" in msg

    def test_en_contains_issue_update(self) -> None:
        msg = _render_design_revise("en")
        assert EXPECTED_BODY in msg
        assert EXPECTED_TMPFILE in msg
        assert "glab issue update" not in msg
        assert "gokrax design-revise --pj testpj" in msg


class TestCodeReviseUnchanged:
    """CODE_REVISE (ja/en) は本 Issue の変更対象外 —
    プロンプト本体は変更しないが、『変更していない』ことを回帰テストで固定する。
    """

    def test_ja(self) -> None:
        msg = _render_code_revise("ja")
        assert "issue-update" not in msg
        assert "/tmp/gokrax-" not in msg

    def test_en(self) -> None:
        msg = _render_code_revise("en")
        assert "issue-update" not in msg
        assert "/tmp/gokrax-" not in msg
