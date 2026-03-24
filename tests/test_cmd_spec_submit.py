"""tests/test_cmd_spec_submit.py — revise/issue/queue/suggestion-submit テスト"""

import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline_io import default_spec_config
from tests.conftest import write_pipeline


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_spec_config(**overrides):
    cfg = default_spec_config()
    cfg.update(overrides)
    return cfg


def _make_pipeline(state="IDLE", spec_mode=True, spec_config=None, **kwargs):
    data = {
        "project": "test-pj",
        "gitlab": "testns/test-pj",
        "state": state,
        "spec_mode": spec_mode,
        "spec_config": spec_config if spec_config is not None else {},
        "enabled": True,
        "implementer": "implementer1",
        "review_mode": "full",
        "batch": [],
        "history": [],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    data.update(kwargs)
    return data


def _review_requests(*reviewers, sent_at="2026-03-01T12:00:00+09:00"):
    return {
        r: {"status": "pending", "sent_at": sent_at,
            "timeout_at": "2026-03-01T12:30:00+09:00",
            "last_nudge_at": None, "response": None}
        for r in reviewers
    }


def _args(**kwargs):
    return argparse.Namespace(**kwargs)


# ── YAML fixtures ────────────────────────────────────────────────────────────

# revise-submit
REVISE_FENCED = """\
```yaml
status: done
new_rev: "2"
commit: abc1234def
changes:
  added_lines: 50
  removed_lines: 10
```
"""

REVISE_RAW = """\
status: done
new_rev: "2"
commit: abc1234def
changes:
  added_lines: 50
  removed_lines: 10
"""

# issue-submit
ISSUE_FENCED = """\
```yaml
status: done
created_issues:
  - 51
  - 52
```
"""

ISSUE_RAW = """\
status: done
created_issues:
  - 51
  - 52
"""

# queue-submit
QUEUE_FENCED = """\
```yaml
status: done
batches: 3
queue_file: "/tmp/queue.txt"
```
"""

QUEUE_RAW = """\
status: done
batches: 3
queue_file: "/tmp/queue.txt"
"""

# suggestion-submit
SUGGESTION_FENCED = """\
```yaml
phases:
  - name: "Phase 1"
    issues:
      - title: "Issue 1"
        description: "desc"
```
"""

SUGGESTION_RAW = """\
phases:
  - name: "Phase 1"
    issues:
      - title: "Issue 1"
        description: "desc"
"""


# ── revise-submit ─────────────────────────────────────────────────────────────


class TestReviseSubmitNormal:

    def test_fenced_yaml(self, tmp_pipelines, tmp_path):
        sc = _make_spec_config(current_rev="1")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))

        f = tmp_path / "revise.yaml"
        f.write_text(REVISE_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_revise_submit
        cmd_spec_revise_submit(_args(project="test-pj", file=str(f)))

        data = json.loads(path.read_text())
        assert data["spec_config"]["_revise_response"] == REVISE_FENCED

    def test_raw_yaml_fallback(self, tmp_pipelines, tmp_path):
        sc = _make_spec_config(current_rev="1")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))

        f = tmp_path / "revise.yaml"
        f.write_text(REVISE_RAW, encoding="utf-8")

        from gokrax import cmd_spec_revise_submit
        cmd_spec_revise_submit(_args(project="test-pj", file=str(f)))

        data = json.loads(path.read_text())
        stored = data["spec_config"]["_revise_response"]
        assert stored.startswith("```yaml\n")
        assert stored.endswith("\n```")

    def test_idempotent_skip(self, tmp_pipelines, tmp_path, capsys):
        sc = _make_spec_config(current_rev="1", _revise_response="already")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))

        f = tmp_path / "revise.yaml"
        f.write_text(REVISE_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_revise_submit
        cmd_spec_revise_submit(_args(project="test-pj", file=str(f)))

        assert "already submitted, skipping" in capsys.readouterr().out


class TestReviseSubmitErrors:

    def test_wrong_state(self, tmp_pipelines, tmp_path):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE", spec_config=_make_spec_config()))

        f = tmp_path / "revise.yaml"
        f.write_text(REVISE_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_revise_submit
        with pytest.raises(SystemExit, match="Not in SPEC_REVISE state"):
            cmd_spec_revise_submit(_args(project="test-pj", file=str(f)))

    def test_parse_failure(self, tmp_pipelines, tmp_path):
        sc = _make_spec_config(current_rev="1")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))

        f = tmp_path / "revise.yaml"
        f.write_text("not valid yaml: [[[", encoding="utf-8")

        from gokrax import cmd_spec_revise_submit
        with pytest.raises(SystemExit, match="Failed to parse revise response"):
            cmd_spec_revise_submit(_args(project="test-pj", file=str(f)))

    def test_file_not_found(self, tmp_pipelines):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="SPEC_REVISE", spec_config=_make_spec_config()))

        from gokrax import cmd_spec_revise_submit
        with pytest.raises(SystemExit, match="File not found"):
            cmd_spec_revise_submit(_args(project="test-pj", file="/nonexistent/f.yaml"))


# ── issue-submit ──────────────────────────────────────────────────────────────


class TestIssueSubmitNormal:

    def test_fenced_yaml(self, tmp_pipelines, tmp_path):
        sc = _make_spec_config()
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="ISSUE_PLAN", spec_config=sc))

        f = tmp_path / "issue.yaml"
        f.write_text(ISSUE_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_issue_submit
        cmd_spec_issue_submit(_args(project="test-pj", file=str(f)))

        data = json.loads(path.read_text())
        assert data["spec_config"]["_issue_plan_response"] == ISSUE_FENCED

    def test_raw_yaml_fallback(self, tmp_pipelines, tmp_path):
        sc = _make_spec_config()
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="ISSUE_PLAN", spec_config=sc))

        f = tmp_path / "issue.yaml"
        f.write_text(ISSUE_RAW, encoding="utf-8")

        from gokrax import cmd_spec_issue_submit
        cmd_spec_issue_submit(_args(project="test-pj", file=str(f)))

        data = json.loads(path.read_text())
        stored = data["spec_config"]["_issue_plan_response"]
        assert stored.startswith("```yaml\n")
        assert stored.endswith("\n```")

    def test_idempotent_skip(self, tmp_pipelines, tmp_path, capsys):
        sc = _make_spec_config(_issue_plan_response="already")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="ISSUE_PLAN", spec_config=sc))

        f = tmp_path / "issue.yaml"
        f.write_text(ISSUE_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_issue_submit
        cmd_spec_issue_submit(_args(project="test-pj", file=str(f)))

        assert "already submitted, skipping" in capsys.readouterr().out


class TestIssueSubmitErrors:

    def test_wrong_state(self, tmp_pipelines, tmp_path):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE", spec_config=_make_spec_config()))

        f = tmp_path / "issue.yaml"
        f.write_text(ISSUE_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_issue_submit
        with pytest.raises(SystemExit, match="Not in ISSUE_PLAN state"):
            cmd_spec_issue_submit(_args(project="test-pj", file=str(f)))

    def test_parse_failure(self, tmp_pipelines, tmp_path):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="ISSUE_PLAN", spec_config=_make_spec_config()))

        f = tmp_path / "issue.yaml"
        f.write_text("not valid yaml: [[[", encoding="utf-8")

        from gokrax import cmd_spec_issue_submit
        with pytest.raises(SystemExit, match="Failed to parse issue plan response"):
            cmd_spec_issue_submit(_args(project="test-pj", file=str(f)))


# ── queue-submit ──────────────────────────────────────────────────────────────


class TestQueueSubmitNormal:

    def test_fenced_yaml(self, tmp_pipelines, tmp_path):
        sc = _make_spec_config()
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="QUEUE_PLAN", spec_config=sc))

        f = tmp_path / "queue.yaml"
        f.write_text(QUEUE_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_queue_submit
        cmd_spec_queue_submit(_args(project="test-pj", file=str(f)))

        data = json.loads(path.read_text())
        assert data["spec_config"]["_queue_plan_response"] == QUEUE_FENCED

    def test_raw_yaml_fallback(self, tmp_pipelines, tmp_path):
        sc = _make_spec_config()
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="QUEUE_PLAN", spec_config=sc))

        f = tmp_path / "queue.yaml"
        f.write_text(QUEUE_RAW, encoding="utf-8")

        from gokrax import cmd_spec_queue_submit
        cmd_spec_queue_submit(_args(project="test-pj", file=str(f)))

        data = json.loads(path.read_text())
        stored = data["spec_config"]["_queue_plan_response"]
        assert stored.startswith("```yaml\n")
        assert stored.endswith("\n```")

    def test_idempotent_skip(self, tmp_pipelines, tmp_path, capsys):
        sc = _make_spec_config(_queue_plan_response="already")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="QUEUE_PLAN", spec_config=sc))

        f = tmp_path / "queue.yaml"
        f.write_text(QUEUE_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_queue_submit
        cmd_spec_queue_submit(_args(project="test-pj", file=str(f)))

        assert "already submitted, skipping" in capsys.readouterr().out


class TestQueueSubmitErrors:

    def test_wrong_state(self, tmp_pipelines, tmp_path):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE", spec_config=_make_spec_config()))

        f = tmp_path / "queue.yaml"
        f.write_text(QUEUE_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_queue_submit
        with pytest.raises(SystemExit, match="Not in QUEUE_PLAN state"):
            cmd_spec_queue_submit(_args(project="test-pj", file=str(f)))

    def test_parse_failure(self, tmp_pipelines, tmp_path):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="QUEUE_PLAN", spec_config=_make_spec_config()))

        f = tmp_path / "queue.yaml"
        f.write_text("not valid yaml: [[[", encoding="utf-8")

        from gokrax import cmd_spec_queue_submit
        with pytest.raises(SystemExit, match="Failed to parse queue plan response"):
            cmd_spec_queue_submit(_args(project="test-pj", file=str(f)))


# ── suggestion-submit ─────────────────────────────────────────────────────────


class TestSuggestionSubmitNormal:

    def _suggestion_pipeline(self, **sc_overrides):
        sc = _make_spec_config(
            review_requests=_review_requests("reviewer2", "reviewer1"),
            **sc_overrides,
        )
        return _make_pipeline(state="ISSUE_SUGGESTION", spec_config=sc)

    def test_fenced_yaml(self, tmp_pipelines, tmp_path):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, self._suggestion_pipeline())

        f = tmp_path / "suggestion.yaml"
        f.write_text(SUGGESTION_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_suggestion_submit
        cmd_spec_suggestion_submit(_args(project="test-pj", reviewer="reviewer2", file=str(f)))

        data = json.loads(path.read_text())
        sc = data["spec_config"]
        entry = sc["current_reviews"]["entries"]["reviewer2"]
        assert entry["status"] == "received"
        assert entry["raw_text"] == SUGGESTION_FENCED
        # review_requests.status は更新しない (Leibniz P0-1)
        assert sc["review_requests"]["reviewer2"]["status"] == "pending"

    def test_raw_yaml_fallback(self, tmp_pipelines, tmp_path):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, self._suggestion_pipeline())

        f = tmp_path / "suggestion.yaml"
        f.write_text(SUGGESTION_RAW, encoding="utf-8")

        from gokrax import cmd_spec_suggestion_submit
        cmd_spec_suggestion_submit(_args(project="test-pj", reviewer="reviewer2", file=str(f)))

        data = json.loads(path.read_text())
        stored = data["spec_config"]["current_reviews"]["entries"]["reviewer2"]["raw_text"]
        assert stored.startswith("```yaml\n")
        assert stored.endswith("\n```")
        # review_requests.status は更新しない
        assert data["spec_config"]["review_requests"]["reviewer2"]["status"] == "pending"

    def test_idempotent_skip(self, tmp_pipelines, tmp_path, capsys):
        pipeline = self._suggestion_pipeline()
        pipeline["spec_config"]["current_reviews"] = {
            "entries": {
                "reviewer2": {"status": "received", "raw_text": "..."},
            },
        }
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, pipeline)

        f = tmp_path / "suggestion.yaml"
        f.write_text(SUGGESTION_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_suggestion_submit
        cmd_spec_suggestion_submit(_args(project="test-pj", reviewer="reviewer2", file=str(f)))

        assert "already submitted, skipping" in capsys.readouterr().out


class TestSuggestionSubmitErrors:

    def _suggestion_pipeline(self, **sc_overrides):
        sc = _make_spec_config(
            review_requests=_review_requests("reviewer2", "reviewer1"),
            **sc_overrides,
        )
        return _make_pipeline(state="ISSUE_SUGGESTION", spec_config=sc)

    def test_wrong_state(self, tmp_pipelines, tmp_path):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE", spec_config=_make_spec_config(
            review_requests=_review_requests("reviewer2"),
        )))

        f = tmp_path / "suggestion.yaml"
        f.write_text(SUGGESTION_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_suggestion_submit
        with pytest.raises(SystemExit, match="Not in ISSUE_SUGGESTION state"):
            cmd_spec_suggestion_submit(_args(project="test-pj", reviewer="reviewer2", file=str(f)))

    def test_invalid_reviewer(self, tmp_pipelines, tmp_path):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, self._suggestion_pipeline())

        f = tmp_path / "suggestion.yaml"
        f.write_text(SUGGESTION_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_suggestion_submit
        with pytest.raises(SystemExit, match="not in review_requests"):
            cmd_spec_suggestion_submit(_args(project="test-pj", reviewer="unknown", file=str(f)))

    def test_sent_at_none(self, tmp_pipelines, tmp_path):
        """sent_at=None のレビュアーに応答投入 → SystemExit"""
        sc = _make_spec_config(
            review_requests=_review_requests("reviewer2", sent_at=None),
        )
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="ISSUE_SUGGESTION", spec_config=sc))

        f = tmp_path / "suggestion.yaml"
        f.write_text(SUGGESTION_FENCED, encoding="utf-8")

        from gokrax import cmd_spec_suggestion_submit
        with pytest.raises(SystemExit, match="sent_at is None"):
            cmd_spec_suggestion_submit(_args(project="test-pj", reviewer="reviewer2", file=str(f)))

    def test_parse_failure(self, tmp_pipelines, tmp_path):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, self._suggestion_pipeline())

        f = tmp_path / "suggestion.yaml"
        f.write_text("not valid yaml: [[[", encoding="utf-8")

        from gokrax import cmd_spec_suggestion_submit
        with pytest.raises(SystemExit, match="Failed to parse issue suggestion"):
            cmd_spec_suggestion_submit(_args(project="test-pj", reviewer="reviewer2", file=str(f)))
