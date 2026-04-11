"""Tests for phase override: _reset_reviewers / _reset_short_context_reviewers / notify_reviewers
respect phase_config instead of base REVIEW_MODES members."""

from unittest.mock import patch

from tests.conftest import TEST_REVIEW_MODES
from engine.fsm import _build_phase_config, get_phase_config  # noqa: F401


# ---------------------------------------------------------------------------
# T1: _reset_reviewers — code phase_config excludes reviewer5
# ---------------------------------------------------------------------------
class TestResetReviewersPhaseOverride:
    def test_code_phase_excludes_overridden_member(self):
        """code phase_config should only reset override members (reviewer5 excluded)."""
        phase_config = _build_phase_config(TEST_REVIEW_MODES["full"], "code")
        with patch("engine.reviewer.send_to_agent_queued", return_value=True) as mock_send, \
             patch("engine.reviewer.ping_agent", return_value=True), \
             patch("engine.backend.resolve_backend", return_value="openclaw"), \
             patch("time.sleep"):
            from engine.reviewer import _reset_reviewers
            _reset_reviewers(phase_config)

        sent_agents = [c.args[0] for c in mock_send.call_args_list]
        assert "reviewer5" not in sent_agents
        assert "reviewer1" in sent_agents
        assert "reviewer3" in sent_agents
        assert "reviewer6" in sent_agents

    # ---------------------------------------------------------------------------
    # T2: _reset_reviewers — design phase_config includes all base members
    # ---------------------------------------------------------------------------
    def test_design_phase_includes_all_base_members(self):
        """design phase_config should reset all base members including reviewer5."""
        phase_config = _build_phase_config(TEST_REVIEW_MODES["full"], "design")
        with patch("engine.reviewer.send_to_agent_queued", return_value=True) as mock_send, \
             patch("engine.reviewer.ping_agent", return_value=True), \
             patch("engine.backend.resolve_backend", return_value="openclaw"), \
             patch("time.sleep"):
            from engine.reviewer import _reset_reviewers
            _reset_reviewers(phase_config)

        sent_agents = [c.args[0] for c in mock_send.call_args_list]
        assert "reviewer1" in sent_agents
        assert "reviewer3" in sent_agents
        assert "reviewer5" in sent_agents
        assert "reviewer6" in sent_agents


# ---------------------------------------------------------------------------
# T3: _reset_short_context_reviewers — code phase_config skips short-context not in override
# ---------------------------------------------------------------------------
class TestResetShortContextPhaseOverride:
    def test_code_phase_no_short_context(self):
        """code phase_config has no short-context members -> no /new sent."""
        phase_config = _build_phase_config(TEST_REVIEW_MODES["full"], "code")
        with patch("engine.reviewer.send_to_agent_queued", return_value=True) as mock_send, \
             patch("engine.backend.resolve_backend", return_value="openclaw"), \
             patch("time.sleep"):
            from engine.reviewer import _reset_short_context_reviewers
            _reset_short_context_reviewers(phase_config)

        mock_send.assert_not_called()

    # ---------------------------------------------------------------------------
    # T4: _reset_short_context_reviewers — design phase_config includes short-context
    # ---------------------------------------------------------------------------
    def test_design_phase_resets_short_context(self):
        """design phase_config includes reviewer5 (short-context) -> /new sent."""
        phase_config = _build_phase_config(TEST_REVIEW_MODES["full"], "design")
        with patch("engine.reviewer.send_to_agent_queued", return_value=True) as mock_send, \
             patch("engine.backend.resolve_backend", return_value="openclaw"), \
             patch("time.sleep"):
            from engine.reviewer import _reset_short_context_reviewers
            _reset_short_context_reviewers(phase_config)

        sent_agents = [c.args[0] for c in mock_send.call_args_list]
        assert "reviewer5" in sent_agents


# ---------------------------------------------------------------------------
# T5: notify_reviewers — CODE_REVIEW + code phase_config excludes reviewer5
# ---------------------------------------------------------------------------
class TestNotifyReviewersPhaseOverride:
    def _make_batch(self):
        return [{"issue": 1, "title": "Test"}]

    def test_code_review_excludes_overridden_member(self):
        """CODE_REVIEW with code phase_config should not notify reviewer5."""
        phase_config = _build_phase_config(TEST_REVIEW_MODES["full"], "code")
        with patch("notify.send_to_agent", return_value=True) as mock_send, \
             patch("notify.format_review_request", return_value="review msg"), \
             patch("notify._check_squash", return_value=[]), \
             patch("notify._write_review_file"), \
             patch("pipeline_io.append_metric"):
            from notify import notify_reviewers
            notify_reviewers(
                "test-pj", "CODE_REVIEW", self._make_batch(), "testns/test-pj",
                review_mode="full", phase_config=phase_config,
            )

        sent_agents = [c.args[0] for c in mock_send.call_args_list]
        assert "reviewer5" not in sent_agents
        assert "reviewer1" in sent_agents
        assert "reviewer3" in sent_agents
        assert "reviewer6" in sent_agents

    # ---------------------------------------------------------------------------
    # T6: notify_reviewers — DESIGN_REVIEW + design phase_config includes all members
    # ---------------------------------------------------------------------------
    def test_design_review_includes_all_base_members(self):
        """DESIGN_REVIEW with design phase_config should notify all base members."""
        phase_config = _build_phase_config(TEST_REVIEW_MODES["full"], "design")
        with patch("notify.send_to_agent", return_value=True) as mock_send, \
             patch("notify.format_review_request", return_value="review msg"), \
             patch("notify._check_squash", return_value=[]), \
             patch("notify._write_review_file"), \
             patch("pipeline_io.append_metric"):
            from notify import notify_reviewers
            notify_reviewers(
                "test-pj", "DESIGN_REVIEW", self._make_batch(), "testns/test-pj",
                review_mode="full", phase_config=phase_config,
            )

        sent_agents = [c.args[0] for c in mock_send.call_args_list]
        assert "reviewer1" in sent_agents
        assert "reviewer3" in sent_agents
        assert "reviewer5" in sent_agents
        assert "reviewer6" in sent_agents

    # ---------------------------------------------------------------------------
    # T7: notify_reviewers — CODE_REVIEW n_pass from phase_config
    # ---------------------------------------------------------------------------
    def test_code_review_n_pass_from_phase_config(self):
        """CODE_REVIEW should use n_pass from phase_config for force_externalize."""
        phase_config = _build_phase_config(TEST_REVIEW_MODES["full"], "code")
        phase_config["n_pass"] = {"reviewer1": 2}

        written_reviewers = []

        def fake_write_review_file(project, reviewer, msg):
            written_reviewers.append(reviewer)
            return f"/tmp/fake-{reviewer}"

        with patch("notify.send_to_agent", return_value=True), \
             patch("notify.format_review_request", return_value="short msg"), \
             patch("notify._check_squash", return_value=[]), \
             patch("notify._write_review_file", side_effect=fake_write_review_file), \
             patch("notify._save_npass_review_file_path"), \
             patch("notify._build_file_review_message", return_value="file msg"), \
             patch("pipeline_io.append_metric"):
            from notify import notify_reviewers
            notify_reviewers(
                "test-pj", "CODE_REVIEW", self._make_batch(), "testns/test-pj",
                review_mode="full", phase_config=phase_config,
            )

        # reviewer1 should be force-externalized (n_pass=2 > 1)
        assert "reviewer1" in written_reviewers


# ---------------------------------------------------------------------------
# T8: watchdog _save_excluded — effective_count / min_reviews from phase_config
# ---------------------------------------------------------------------------
class TestSaveExcludedPhaseConfig:
    def test_effective_count_uses_phase_config(self):
        """CODE_REVIEW transition should use phase_config members for effective_count."""
        phase_config = _build_phase_config(TEST_REVIEW_MODES["full"], "code")
        # code override: ["reviewer1", "reviewer3", "reviewer6"] -> 3 members
        assert len(phase_config["members"]) == 3

        # effective_count with no exclusions
        excluded = []
        effective_count = len([m for m in phase_config["members"] if m not in excluded])
        assert effective_count == 3

        from engine.fsm import get_min_reviews
        min_reviews = get_min_reviews(phase_config)
        # min_reviews should be capped at members count (3)
        assert min_reviews <= 3


# ---------------------------------------------------------------------------
# T9: _recover_pending_notifications — phase_config passed to notify_reviewers
# ---------------------------------------------------------------------------
class TestRecoverPendingNotificationsPhaseConfig:
    def test_phase_config_passed_on_recovery(self, tmp_path, monkeypatch):
        """CODE_REVIEW recovery should pass phase_config to notify_reviewers."""
        import config
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)

        review_config = {
            "design": _build_phase_config(TEST_REVIEW_MODES["full"], "design"),
            "code": _build_phase_config(TEST_REVIEW_MODES["full"], "code"),
        }
        pipeline_data = {
            "project": "test-pj",
            "state": "CODE_REVIEW",
            "review_config": review_config,
            "excluded_reviewers": [],
            "comment": "",
            "pending_notifications": {
                "review": {
                    "new_state": "CODE_REVIEW",
                    "batch": [{"issue": 1, "title": "Test"}],
                    "gitlab": "testns/test-pj",
                    "repo_path": "",
                    "review_mode": "full",
                },
            },
        }

        with patch("engine.fsm.load_pipeline", return_value=pipeline_data), \
             patch("engine.fsm.notify_reviewers") as mock_notify, \
             patch("engine.fsm.clear_pending_notification"):
            from engine.fsm import _recover_pending_notifications
            _recover_pending_notifications("test-pj", pipeline_data["pending_notifications"])

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert "phase_config" in kwargs
        assert kwargs["phase_config"] is not None
        # code phase_config should have the override members
        assert kwargs["phase_config"]["members"] == ["reviewer1", "reviewer3", "reviewer6"]
