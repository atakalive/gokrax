"""tests/test_spec_revise.py — spec_revise.py のテスト"""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spec_revise import (
    build_revise_prompt,
    build_self_review_prompt,
    get_self_review_agent,
    make_rev_path,
    extract_rev_from_path,
    parse_revise_response,
    parse_self_review_response,
    verify_git_diff,
    build_revise_completion_updates,
)

LOCAL_TZ = timezone(timedelta(hours=9))


def _now():
    return datetime(2026, 3, 1, 12, 0, 0, tzinfo=LOCAL_TZ)


# --- build_revise_prompt ---

class TestBuildRevisePrompt:
    def test_contains_report(self):
        sc = {"spec_path": "docs/spec.md", "current_rev": "2"}
        prompt = build_revise_prompt(sc, "# Report\nP0 found", {"project": "gokrax"})
        assert "Report" in prompt
        assert "P0 found" in prompt
        assert "gokrax" in prompt

    def test_contains_yaml_format(self):
        sc = {"spec_path": "docs/spec.md", "current_rev": "1"}
        prompt = build_revise_prompt(sc, "", {"project": "test"})
        assert "status: done" in prompt
        assert "new_rev" in prompt

    def test_empty_spec_path_raises(self):
        """spec_path 空文字 → ValueError（Leibniz P1-2 / Pascal P1）"""
        sc = {"spec_path": "", "current_rev": "1"}
        with pytest.raises(ValueError, match="spec_path"):
            build_revise_prompt(sc, "", {"project": "test"})

    def test_next_rev_expanded_in_rules(self):
        """改訂ルールの [vN] がf-string展開されること（Leibniz P1-1）"""
        sc = {"spec_path": "docs/spec.md", "current_rev": "2", "rev_index": 2}
        prompt = build_revise_prompt(sc, "", {"project": "test"})
        # next_rev = rev_index + 1 = 3
        assert "[v3]" in prompt
        assert "{{next_rev}}" not in prompt  # 二重波括弧が残っていないこと


# --- build_self_review_prompt ---

class TestBuildSelfReviewPrompt:
    def test_contains_check_items(self):
        sc = {"spec_path": "docs/spec.md", "current_rev": "3", "last_commit": "abc123"}
        prompt = build_self_review_prompt(sc, {"project": "gokrax"})
        # 新チェックリスト方式: デフォルト4項目が含まれること
        assert "reflected_items_match" in prompt
        assert "no_new_contradictions" in prompt
        assert "abc123" in prompt
        assert "self-review-submit" in prompt


# --- get_self_review_agent ---

class TestGetSelfReviewAgent:
    def test_explicit_agent(self):
        sc = {"self_review_agent": "leibniz", "review_requests": {"pascal": {}}}
        assert get_self_review_agent(sc) == "leibniz"

    def test_first_reviewer(self):
        sc = {"self_review_agent": None, "review_requests": {"dijkstra": {}, "pascal": {}}}
        assert get_self_review_agent(sc) == "dijkstra"

    def test_fallback(self):
        sc = {"review_requests": {}}
        assert get_self_review_agent(sc) == "kaneko"


# --- parse_revise_response ---

class TestParseReviseResponse:
    def test_valid(self):
        text = '```yaml\nstatus: done\nnew_rev: "2"\ncommit: "abc1234"\nchanges:\n  added_lines: 50\n  removed_lines: 10\n```'
        result = parse_revise_response(text, current_rev="1")
        assert result is not None
        assert result["status"] == "done"
        assert result["new_rev"] == "2"
        assert result["commit"] == "abc1234"

    def test_no_yaml(self):
        assert parse_revise_response("no yaml here") is None

    def test_status_not_done(self):
        text = '```yaml\nstatus: wip\nnew_rev: "2"\ncommit: "abc1234"\nchanges:\n  added_lines: 0\n  removed_lines: 0\n```'
        assert parse_revise_response(text, "1") is None

    def test_missing_commit(self):
        text = '```yaml\nstatus: done\nnew_rev: "2"\nchanges:\n  added_lines: 0\n  removed_lines: 0\n```'
        assert parse_revise_response(text, "1") is None

    def test_missing_new_rev(self):
        text = '```yaml\nstatus: done\ncommit: "abc1234"\nchanges:\n  added_lines: 0\n  removed_lines: 0\n```'
        assert parse_revise_response(text, "1") is None

    def test_malformed_yaml(self):
        text = '```yaml\n: : :\n```'
        assert parse_revise_response(text) is None

    def test_new_rev_not_incremented(self):
        """new_rev != current_rev + 1 → None（Leibniz P0-3）"""
        text = '```yaml\nstatus: done\nnew_rev: "5"\ncommit: "abc1234"\nchanges:\n  added_lines: 10\n  removed_lines: 5\n```'
        assert parse_revise_response(text, current_rev="2") is None

    def test_commit_too_short(self):
        """commit hash < 7文字 → None"""
        text = '```yaml\nstatus: done\nnew_rev: "2"\ncommit: "abc"\nchanges:\n  added_lines: 10\n  removed_lines: 5\n```'
        assert parse_revise_response(text, "1") is None

    def test_commit_not_hex(self):
        """commit hash に非hex文字 → None"""
        text = '```yaml\nstatus: done\nnew_rev: "2"\ncommit: "xyz1234"\nchanges:\n  added_lines: 10\n  removed_lines: 5\n```'
        assert parse_revise_response(text, "1") is None

    def test_negative_lines(self):
        """added_lines < 0 → None"""
        text = '```yaml\nstatus: done\nnew_rev: "2"\ncommit: "abc1234"\nchanges:\n  added_lines: -1\n  removed_lines: 5\n```'
        assert parse_revise_response(text, "1") is None

    def test_missing_changes_keys(self):
        """changes に added_lines/removed_lines なし → None"""
        text = '```yaml\nstatus: done\nnew_rev: "2"\ncommit: "abc1234"\nchanges:\n  foo: bar\n```'
        assert parse_revise_response(text, "1") is None


# --- parse_self_review_response --- (チェックリスト方式 #77)

def _all_yes_yaml():
    return """\
```yaml
checklist:
  - id: "reflected_items_match"
    result: "Yes"
    evidence: "OK"
  - id: "no_new_contradictions"
    result: "Yes"
    evidence: "OK"
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: "OK"
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: "OK"
```
"""


class TestParseSelfReviewResponse:
    def test_clean_yaml(self):
        """全 Yes → verdict: clean"""
        result = parse_self_review_response(_all_yes_yaml())
        assert result["verdict"] == "clean"
        assert result["items"] == []

    def test_issues_found_yaml(self):
        """1件 No → verdict: issues_found"""
        text = """\
```yaml
checklist:
  - id: "reflected_items_match"
    result: "No"
    evidence: "見つからない"
  - id: "no_new_contradictions"
    result: "Yes"
    evidence: "OK"
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: "OK"
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: "OK"
```
"""
        result = parse_self_review_response(text)
        assert result["verdict"] == "issues_found"
        assert len(result["items"]) == 1
        assert result["items"][0]["id"] == "reflected_items_match"

    def test_clean_inline(self):
        """テキストのみ（YAMLブロックなし）→ parse_failed（テキストフォールバック廃止）"""
        result = parse_self_review_response("All good. status: clean")
        assert result["verdict"] == "parse_failed"

    def test_issues_found_inline(self):
        """テキストのみ（YAMLブロックなし）→ parse_failed"""
        result = parse_self_review_response("Found problems. issues_found in section 3.")
        assert result["verdict"] == "parse_failed"

    def test_unknown(self):
        """不明テキスト → parse_failed"""
        result = parse_self_review_response("I have no idea what format to use")
        assert result["verdict"] == "parse_failed"

    def test_malformed_yaml(self):
        """不正YAML → parse_failed"""
        result = parse_self_review_response('```yaml\n: : :\n```')
        assert result["verdict"] == "parse_failed"


# --- verify_git_diff ---

class TestVerifyGitDiff:
    def test_match(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {
                "returncode": 0, "stdout": "50\t10\tdocs/spec.md\n", "stderr": ""
            })()
            result = verify_git_diff("/repo", "abc", "def1234", "docs/spec.md", {"added_lines": 50, "removed_lines": 10})
            assert result is None  # 一致
            # git コマンドが reported_commit を使っていることを確認
            args = mock_run.call_args[0][0]
            assert "abc..def1234" in args[3]

    def test_mismatch(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {
                "returncode": 0, "stdout": "60\t10\tdocs/spec.md\n", "stderr": ""
            })()
            result = verify_git_diff("/repo", "abc", "def1234", "docs/spec.md", {"added_lines": 50, "removed_lines": 10})
            assert result is not None
            assert "不一致" in result

    def test_git_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {
                "returncode": 1, "stdout": "", "stderr": "fatal: bad revision"
            })()
            result = verify_git_diff("/repo", "abc", "def1234", "docs/spec.md", {})
            assert result is not None


# --- build_revise_completion_updates ---

class TestBuildReviseCompletionUpdates:
    def test_basic(self):
        sc = {
            "spec_path": "/repo/docs/foo-spec-rev2.md",
            "current_rev": "2",
            "rev_index": 2,
            "review_history": [],
            "current_reviews": {
                "entries": {
                    "pascal": {"status": "received", "verdict": "P0",
                               "items": [{"severity": "critical"}]},
                },
            },
            "last_commit": "old123",
            "review_requests": {
                "pascal": {"status": "received", "sent_at": "x", "timeout_at": "x",
                           "last_nudge_at": None, "response": None},
            },
        }
        revise_data = {
            "new_rev": "3",
            "commit": "new456",
            "changes": {"added_lines": 50, "removed_lines": 10,
                        "reflected_items": ["pascal:C-1"]},
        }
        updates = build_revise_completion_updates(sc, revise_data, _now())
        assert updates["last_commit"] == "new456"
        assert updates["current_rev"] == "3"
        assert updates["rev_index"] == 3
        assert len(updates["review_history"]) == 1
        assert updates["current_reviews"] == {"entries": {}}
        assert updates["_revise_retry_at"] is None
        # review_requests リセット差分が含まれる（Dijkstra P1-2）
        assert "review_requests_patch" in updates

    def test_preserves_existing_history(self):
        sc = {
            "spec_path": "/repo/docs/foo-spec-rev1.md",
            "current_rev": "1", "rev_index": 1, "rev_index": 0,
            "review_history": [{"rev": "0", "timestamp": "old"}],
            "current_reviews": {"entries": {}},
        }
        revise_data = {"new_rev": "2", "commit": "abc", "changes": {}}
        updates = build_revise_completion_updates(sc, revise_data, _now())
        assert len(updates["review_history"]) == 2

    def test_does_not_mutate_spec_config(self):
        sc = {
            "spec_path": "/repo/docs/foo-spec-rev1.md",
            "current_rev": "1", "rev_index": 1, "rev_index": 0,
            "review_history": [], "current_reviews": {"entries": {}},
        }
        revise_data = {"new_rev": "2", "commit": "abc", "changes": {}}
        build_revise_completion_updates(sc, revise_data, _now())
        assert sc["current_rev"] == "1"  # 変更されていない
        assert sc["rev_index"] == 0


# ---------------------------------------------------------------------------
# make_rev_path のテスト
# ---------------------------------------------------------------------------

class TestMakeRevPath:
    def test_basic(self):
        assert make_rev_path("/repo/docs/foo-spec.md", 2) == "/repo/docs/foo-spec-rev2.md"

    def test_replace_existing(self):
        assert make_rev_path("/repo/docs/foo-spec-rev3.md", 4) == "/repo/docs/foo-spec-rev4.md"

    def test_no_extension(self):
        assert make_rev_path("/repo/docs/foo-spec", 1) == "/repo/docs/foo-spec-rev1"

    def test_absolute(self):
        assert make_rev_path("/abs/path/spec.md", 5) == "/abs/path/spec-rev5.md"

    def test_rev_in_middle(self):
        """stem に -rev が prefix として含まれるが末尾 -revN ではないケース"""
        assert make_rev_path("/repo/docs/foo-rev-notes.md", 2) == "/repo/docs/foo-rev-notes-rev2.md"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            make_rev_path("", 1)


# ---------------------------------------------------------------------------
# extract_rev_from_path のテスト
# ---------------------------------------------------------------------------

class TestExtractRevFromPath:
    def test_found(self):
        assert extract_rev_from_path("/repo/docs/foo-spec-rev4.md") == 4

    def test_not_found(self):
        assert extract_rev_from_path("/repo/docs/foo-spec.md") is None

    def test_rev_in_middle(self):
        assert extract_rev_from_path("/repo/docs/foo-rev-notes.md") is None

    def test_rev0_returns_none(self):
        """rev0 は None として扱う（rev >= 1 の方針）"""
        assert extract_rev_from_path("/repo/docs/foo-spec-rev0.md") is None


# ---------------------------------------------------------------------------
# build_revise_completion_updates の spec_path + rev_index テスト
# ---------------------------------------------------------------------------

class TestBuildReviseCompletionUpdatesEmptySpecPath:
    def test_empty_spec_path_raises(self):
        """spec_path 空文字 → ValueError（Pascal P1 / Leibniz P1-2）"""
        sc = {
            "spec_path": "",
            "current_rev": "1", "rev_index": 1, "rev_index": 0,
            "review_history": [], "current_reviews": {"entries": {}},
        }
        revise_data = {"new_rev": "2", "commit": "abc1234",
                       "changes": {"added_lines": 1, "removed_lines": 0}}
        with pytest.raises(ValueError, match="spec_path"):
            build_revise_completion_updates(sc, revise_data, _now())


class TestBuildReviseCompletionUpdatesSpecPath:
    def test_updates_spec_path_and_rev_index(self):
        spec_config = {
            "spec_path": "/repo/docs/testgen-surface-spec.md",
            "current_rev": "1",
            "rev_index": 1,
            "rev_index": 0,
            "review_history": [],
            "review_requests": {},
            "current_reviews": {"entries": {}},
        }
        revise_data = {
            "new_rev": "2",
            "commit": "abc1234",
            "changes": {"added_lines": 10, "removed_lines": 3,
                        "reflected_items": [], "deferred_items": []},
        }
        result = build_revise_completion_updates(spec_config, revise_data, _now())
        assert result["spec_path"] == "/repo/docs/testgen-surface-spec-rev2.md"
        assert result["current_rev"] == "2"
        assert result["rev_index"] == 2  # new_rev から直接算出
