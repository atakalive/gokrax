"""Tests for INITIALIZE→DESIGN_PLAN closed-issue guard (Issue #332)."""

import json
import types

import pytest
from unittest.mock import MagicMock, patch

import config
import pipeline_io


def _base_pipeline() -> dict:
    return {
        "project": "pj",
        "gitlab": "testns/pj",
        "state": "INITIALIZE",
        "enabled": True,
        "review_mode": "standard",
        "batch": [{"issue": 42}],
        "history": [],
        "repo_path": "",
        "implementer": "impl1",
    }


def _setup_pipeline(tmp_pipelines, data: dict) -> object:
    path = tmp_pipelines / "pj.json"
    path.write_text(json.dumps(data))
    return path


class TestClosedIssueGuard:
    def test_closed_issue_blocks(self, tmp_pipelines, monkeypatch):
        from watchdog import process

        monkeypatch.setattr("watchdog.PIPELINES_DIR", tmp_pipelines)
        monkeypatch.setattr("engine.glab.fetch_issue_state",
                            MagicMock(return_value="closed"))

        path = _setup_pipeline(tmp_pipelines, _base_pipeline())
        process(path)

        saved = json.loads(path.read_text())
        assert saved["state"] == "BLOCKED"
        assert saved["enabled"] is False
        assert saved["history"][-1]["actor"] == "watchdog:closed-issue-guard"

    def test_unverifiable_issue_blocks(self, tmp_pipelines, monkeypatch):
        from watchdog import process

        monkeypatch.setattr("watchdog.PIPELINES_DIR", tmp_pipelines)
        monkeypatch.setattr("engine.glab.fetch_issue_state",
                            MagicMock(return_value=None))

        path = _setup_pipeline(tmp_pipelines, _base_pipeline())
        process(path)

        saved = json.loads(path.read_text())
        assert saved["state"] == "BLOCKED"
        assert saved["enabled"] is False
        # notify_discord called with "unverifiable" in message
        import watchdog as _wd
        # notify_discord is patched in conftest; inspect call_args
        call_args_list = _wd.notify_discord.call_args_list
        joined = " ".join(str(c) for c in call_args_list)
        assert "unverifiable" in joined

    def test_allow_closed_skips_guard(self, tmp_pipelines, monkeypatch):
        from watchdog import process

        monkeypatch.setattr("watchdog.PIPELINES_DIR", tmp_pipelines)
        monkeypatch.setattr("engine.glab.fetch_issue_state",
                            MagicMock(return_value="closed"))

        data = _base_pipeline()
        data["allow_closed"] = True
        path = _setup_pipeline(tmp_pipelines, data)

        mock_git = MagicMock()
        mock_git.returncode = 0
        mock_git.stdout = "abc12345\n"
        with patch("watchdog._reset_reviewers", return_value=[]), \
             patch("watchdog._has_pytest", return_value=False), \
             patch("subprocess.run", return_value=mock_git), \
             patch("watchdog._poll_pytest_baseline"):
            process(path)

        saved = json.loads(path.read_text())
        assert saved["state"] != "BLOCKED"
        assert saved["state"] in ("DESIGN_PLAN", "DESIGN_APPROVED")

    def test_open_issue_proceeds(self, tmp_pipelines, monkeypatch):
        from watchdog import process

        monkeypatch.setattr("watchdog.PIPELINES_DIR", tmp_pipelines)
        # default conftest mock returns "opened" — no override needed

        path = _setup_pipeline(tmp_pipelines, _base_pipeline())
        mock_git = MagicMock()
        mock_git.returncode = 0
        mock_git.stdout = "abc12345\n"
        with patch("watchdog._reset_reviewers", return_value=[]), \
             patch("watchdog._has_pytest", return_value=False), \
             patch("subprocess.run", return_value=mock_git), \
             patch("watchdog._poll_pytest_baseline"):
            process(path)

        saved = json.loads(path.read_text())
        assert saved["state"] != "BLOCKED"
        assert saved["state"] in ("DESIGN_PLAN", "DESIGN_APPROVED")


class TestCmdStartAllowClosedPersist:
    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch, tmp_path):
        import commands.dev
        import gokrax as gokrax_mod
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)
        monkeypatch.setattr(commands.dev, "cmd_triage", lambda args: None)
        monkeypatch.setattr(commands.dev, "cmd_transition", lambda args: None)
        monkeypatch.setattr(gokrax_mod, "_start_loop", lambda: None)
        monkeypatch.setattr(config, "resolve_queue_options", lambda project: {})
        self.dir = tmp_path

    def _make_args(self, **overrides):
        defaults = dict(
            project="test", issue=[1], mode="min",
            automerge=None, no_automerge=None,
            keep_context=None, keep_ctx_batch=None, keep_ctx_intra=None,
            keep_ctx_all=None, keep_ctx_none=None, p2_fix=None,
            skip_cc_plan=None, no_skip_cc_plan=None,
            skip_test=None, no_skip_test=None,
            skip_assess=None, no_skip_assess=None,
            skip_design=None, no_skip_design=None,
            no_cc=None, no_no_cc=None,
            exclude_high_risk=None, no_exclude_high_risk=None,
            exclude_any_risk=None, no_exclude_any_risk=None,
            allow_closed=True, comment=None,
            cc_plan_model=None, cc_impl_model=None,
            implementer=None, gitlab=None,
        )
        defaults.update(overrides)
        return types.SimpleNamespace(**defaults)

    def _write(self, project: str = "test"):
        path = self.dir / f"{project}.json"
        path.write_text(json.dumps({
            "project": project,
            "state": "IDLE", "enabled": False, "batch": [], "history": [],
            "review_mode": "min", "repo_path": "/tmp/x", "implementer": "impl1",
            "gitlab": f"ns/{project}",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }))
        return path

    def test_allow_closed_persisted(self):
        from commands.dev import cmd_start
        path = self._write()
        cmd_start(self._make_args(allow_closed=True))
        data = json.loads(path.read_text())
        assert data.get("allow_closed") is True

    def test_allow_closed_default_not_persisted(self):
        from commands.dev import cmd_start
        path = self._write()
        cmd_start(self._make_args(allow_closed=False))
        data = json.loads(path.read_text())
        assert "allow_closed" not in data


class TestCleanupAllowClosed:
    def test_cleanup_pops_allow_closed(self):
        from engine.cleanup import _cleanup_batch_state
        data = {"allow_closed": True, "batch": [{"issue": 1}]}
        _cleanup_batch_state(data, "test_pj")
        assert "allow_closed" not in data
