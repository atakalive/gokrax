"""tests/test_npass_transition.py — Nパスレビュー遷移テスト (Issue #177)"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.fsm import (  # noqa: E402
    TransitionAction,
    _get_reviewer_entry,
    check_transition,
)

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_pipeline(tmp_pipelines: Path, state: str = "CODE_REVIEW",
                   review_mode: str = "standard",
                   extra: dict | None = None) -> Path:
    """パイプラインJSONを作成。"""
    data = {
        "project": "test-pj",
        "gitlab": "testns/test-pj",
        "state": state,
        "enabled": True,
        "implementer": "implementer1",
        "review_mode": review_mode,
        "batch": [
            {
                "issue": 1,
                "title": "Test Issue",
                "commit": "abc123" if "CODE" in state else None,
                "cc_session_id": None,
                "design_reviews": {},
                "code_reviews": {},
                "added_at": "2025-01-01T00:00:00+09:00",
            }
        ],
        "history": [],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    if extra:
        data.update(extra)
    path = tmp_pipelines / "test-pj.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _now_iso() -> str:
    return datetime.now(JST).isoformat()


# ---------------------------------------------------------------------------
# 1. _get_reviewer_entry
# ---------------------------------------------------------------------------

class TestGetReviewerEntry:
    def test_returns_entry(self):
        batch = [{"issue": 1, "code_reviews": {"alice": {"verdict": "APPROVE", "pass": 1}}}]
        entry = _get_reviewer_entry(batch, "code_reviews", "alice")
        assert entry is not None
        assert entry["verdict"] == "APPROVE"

    def test_returns_none_for_missing(self):
        batch = [{"issue": 1, "code_reviews": {}}]
        assert _get_reviewer_entry(batch, "code_reviews", "alice") is None


# ---------------------------------------------------------------------------
# 2. TransitionAction.npass_target_reviewers
# ---------------------------------------------------------------------------

class TestTransitionActionNpass:
    def test_default_none(self):
        action = TransitionAction()
        assert action.npass_target_reviewers is None

    def test_set_value(self):
        action = TransitionAction(npass_target_reviewers=["alice", "bob"])
        assert action.npass_target_reviewers == ["alice", "bob"]


# ---------------------------------------------------------------------------
# 3. REVIEW → NPASS interception (§1-1)
# ---------------------------------------------------------------------------

class TestReviewToNpass:
    """REVIEW block で APPROVE 判定時に pass < target_pass のレビュアーがいれば NPASS へ。"""

    def test_approve_with_npass_targets_goes_to_npass(self):
        """全レビュアー APPROVE だが alice が pass=1, target_pass=2 → NPASS。"""
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "APPROVE", "pass": 1, "target_pass": 2, "at": _now_iso()},
                "bob": {"verdict": "APPROVE", "pass": 1, "target_pass": 1, "at": _now_iso()},
            },
        }]
        data = {
            "project": "test-pj",
            "review_mode": "standard",
            "history": [{"from": "IMPLEMENTATION", "to": "CODE_REVIEW", "at": _now_iso()}],
        }
        with patch("engine.fsm.REVIEW_MODES", {
            "standard": {"members": ["alice", "bob"], "min_reviews": 2, "grace_period_sec": 0},
        }):
            action = check_transition("CODE_REVIEW", batch, data)
        assert action.new_state == "CODE_REVIEW_NPASS"
        assert action.send_review is True
        assert action.npass_target_reviewers == ["alice"]

    def test_approve_without_npass_goes_to_approved(self):
        """全レビュアー APPROVE で pass == target_pass → 通常 APPROVED。"""
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "APPROVE", "pass": 1, "target_pass": 1, "at": _now_iso()},
                "bob": {"verdict": "APPROVE", "pass": 1, "target_pass": 1, "at": _now_iso()},
            },
        }]
        data = {
            "project": "test-pj",
            "review_mode": "standard",
            "history": [{"from": "IMPLEMENTATION", "to": "CODE_REVIEW", "at": _now_iso()}],
        }
        with patch("engine.fsm.REVIEW_MODES", {
            "standard": {"members": ["alice", "bob"], "min_reviews": 2, "grace_period_sec": 0},
        }):
            action = check_transition("CODE_REVIEW", batch, data)
        assert action.new_state == "CODE_APPROVED"

    def test_p0_round1_npass_fires(self):
        """Round 1 で P0 + pass < target_pass → NPASS に遷移する（#182 修正）。"""
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "P0", "pass": 1, "target_pass": 2, "at": _now_iso()},
                "bob": {"verdict": "APPROVE", "pass": 1, "target_pass": 1, "at": _now_iso()},
            },
        }]
        data = {
            "project": "test-pj",
            "review_mode": "standard",
            "history": [{"from": "IMPLEMENTATION", "to": "CODE_REVIEW", "at": _now_iso()}],
        }
        with patch("engine.fsm.REVIEW_MODES", {
            "standard": {"members": ["alice", "bob"], "min_reviews": 2, "grace_period_sec": 0},
        }):
            action = check_transition("CODE_REVIEW", batch, data)
        assert action.new_state == "CODE_REVIEW_NPASS"
        assert action.npass_target_reviewers == ["alice"]

    def test_design_review_npass(self):
        """DESIGN_REVIEW でも NPASS 遷移が動作する。"""
        batch = [{
            "issue": 1,
            "design_reviews": {
                "alice": {"verdict": "APPROVE", "pass": 1, "target_pass": 2, "at": _now_iso()},
                "bob": {"verdict": "APPROVE", "pass": 1, "target_pass": 1, "at": _now_iso()},
            },
        }]
        data = {
            "project": "test-pj",
            "review_mode": "standard",
            "history": [{"from": "DESIGN_PLAN", "to": "DESIGN_REVIEW", "at": _now_iso()}],
        }
        with patch("engine.fsm.REVIEW_MODES", {
            "standard": {"members": ["alice", "bob"], "min_reviews": 2, "grace_period_sec": 0},
        }):
            action = check_transition("DESIGN_REVIEW", batch, data)
        assert action.new_state == "DESIGN_REVIEW_NPASS"
        assert action.npass_target_reviewers == ["alice"]


# ---------------------------------------------------------------------------
# 4. REVIEW_NPASS check_transition (§1-3)
# ---------------------------------------------------------------------------

class TestNpassCheckTransition:
    """REVIEW_NPASS ブロックのテスト。"""

    def _entered_at(self, seconds_ago: int = 10) -> str:
        return (datetime.now(JST) - timedelta(seconds=seconds_ago)).isoformat()

    def test_all_submitted_approved(self):
        """全 NPASS ターゲットが提出 + pass == target_pass → APPROVED。"""
        now = _now_iso()
        entered = self._entered_at(10)
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "APPROVE", "pass": 2, "target_pass": 2, "at": now},
            },
        }]
        data = {
            "project": "test-pj",
            "_npass_target_reviewers": ["alice"],
            "history": [{"from": "CODE_REVIEW", "to": "CODE_REVIEW_NPASS", "at": entered}],
        }
        action = check_transition("CODE_REVIEW_NPASS", batch, data)
        assert action.new_state == "CODE_APPROVED"

    def test_p0_in_npass_goes_to_revise(self):
        """NPASS で P0 → REVISE。"""
        now = _now_iso()
        entered = self._entered_at(10)
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "P0", "pass": 2, "target_pass": 2, "at": now},
            },
        }]
        data = {
            "project": "test-pj",
            "_npass_target_reviewers": ["alice"],
            "history": [{"from": "CODE_REVIEW", "to": "CODE_REVIEW_NPASS", "at": entered}],
        }
        action = check_transition("CODE_REVIEW_NPASS", batch, data)
        assert action.new_state == "CODE_REVISE"

    def test_self_transition_more_passes(self):
        """提出済みだが pass < target_pass → 自己遷移（更にパスが必要）。"""
        now = _now_iso()
        entered = self._entered_at(10)
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "APPROVE", "pass": 2, "target_pass": 3, "at": now},
            },
        }]
        data = {
            "project": "test-pj",
            "_npass_target_reviewers": ["alice"],
            "history": [{"from": "CODE_REVIEW_NPASS", "to": "CODE_REVIEW_NPASS", "at": entered}],
        }
        action = check_transition("CODE_REVIEW_NPASS", batch, data)
        assert action.new_state == "CODE_REVIEW_NPASS"
        assert action.send_review is True
        assert action.npass_target_reviewers == ["alice"]

    def test_not_submitted_yet_waits(self):
        """レビュー at が state 進入前 → 未提出として待機。"""
        old_time = (datetime.now(JST) - timedelta(minutes=5)).isoformat()
        entered = self._entered_at(10)
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "APPROVE", "pass": 1, "target_pass": 2, "at": old_time},
            },
        }]
        data = {
            "project": "test-pj",
            "_npass_target_reviewers": ["alice"],
            "history": [{"from": "CODE_REVIEW", "to": "CODE_REVIEW_NPASS", "at": entered}],
        }
        action = check_transition("CODE_REVIEW_NPASS", batch, data)
        assert action.new_state is None

    def test_timeout_forces_approve(self):
        """タイムアウト → BLOCKED ではなく強制 APPROVED。"""
        entered = (datetime.now(JST) - timedelta(hours=2)).isoformat()
        old_time = (datetime.now(JST) - timedelta(hours=3)).isoformat()  # NPASS 進入前
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "APPROVE", "pass": 1, "target_pass": 2, "at": old_time},
            },
        }]
        data = {
            "project": "test-pj",
            "_npass_target_reviewers": ["alice"],
            "history": [{"from": "CODE_REVIEW", "to": "CODE_REVIEW_NPASS", "at": entered}],
        }
        action = check_transition("CODE_REVIEW_NPASS", batch, data)
        assert action.new_state == "CODE_APPROVED"

    def test_timeout_with_p0_goes_to_revise(self):
        """タイムアウト時に提出済み P0 → REVISE（無条件 APPROVED にならない）。"""
        entered = (datetime.now(JST) - timedelta(hours=2)).isoformat()
        old_time = (datetime.now(JST) - timedelta(hours=3)).isoformat()
        after_enter = (datetime.now(JST) - timedelta(minutes=30)).isoformat()
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "P0", "pass": 2, "target_pass": 2, "at": after_enter,
                          "summary": "bug found"},
                "bob": {"verdict": "APPROVE", "pass": 1, "target_pass": 2, "at": old_time},
            },
        }]
        data = {
            "project": "test-pj",
            "_npass_target_reviewers": ["alice", "bob"],
            "history": [{"from": "CODE_REVIEW", "to": "CODE_REVIEW_NPASS", "at": entered}],
        }
        action = check_transition("CODE_REVIEW_NPASS", batch, data)
        assert action.new_state == "CODE_REVISE"

    def test_npass1_reviewer_p0_causes_revise(self):
        """n_pass==1 レビュアーの P0 が NPASS 完了判定で無視されない。"""
        entered = (datetime.now(JST) - timedelta(minutes=5)).isoformat()
        after_enter = (datetime.now(JST) - timedelta(minutes=1)).isoformat()
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "APPROVE", "pass": 2, "target_pass": 2, "at": after_enter},
                "bob": {"verdict": "P0", "pass": 1, "target_pass": 1, "at": after_enter},
            },
        }]
        data = {
            "project": "test-pj",
            "_npass_target_reviewers": ["alice"],
            "review_mode": "standard",
            "history": [{"from": "CODE_REVIEW", "to": "CODE_REVIEW_NPASS", "at": entered}],
        }
        action = check_transition("CODE_REVIEW_NPASS", batch, data)
        assert action.new_state == "CODE_REVISE"

    def test_not_submitted_p1_verdict_does_not_cause_revise(self):
        """前回パスの P1 verdict が NPASS 未提出時に即 REVISE を引き起こさないこと。"""
        old_time = (datetime.now(JST) - timedelta(minutes=5)).isoformat()
        entered = self._entered_at(10)
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "P1", "pass": 1, "target_pass": 2, "at": old_time,
                          "summary": "naming issue"},
            },
        }]
        data = {
            "project": "test-pj",
            "_npass_target_reviewers": ["alice"],
            "history": [{"from": "CODE_REVIEW", "to": "CODE_REVIEW_NPASS", "at": entered}],
        }
        action = check_transition("CODE_REVIEW_NPASS", batch, data)
        assert action.new_state is None  # 待機（即 REVISE にならない）

    def test_not_submitted_p0_verdict_does_not_cause_revise(self):
        """前回パスの P0 verdict が NPASS 未提出時に即 REVISE を引き起こさないこと。"""
        old_time = (datetime.now(JST) - timedelta(minutes=5)).isoformat()
        entered = self._entered_at(10)
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "P0", "pass": 1, "target_pass": 2, "at": old_time,
                          "summary": "critical bug"},
            },
        }]
        data = {
            "project": "test-pj",
            "_npass_target_reviewers": ["alice"],
            "history": [{"from": "CODE_REVIEW", "to": "CODE_REVIEW_NPASS", "at": entered}],
        }
        action = check_transition("CODE_REVIEW_NPASS", batch, data)
        assert action.new_state is None  # 待機（即 REVISE にならない）

    def test_submitted_p1_in_npass_causes_revise(self):
        """NPASS パスで提出された P1 verdict は即 REVISE を引き起こすこと。"""
        entered = self._entered_at(10)
        after_enter = (datetime.now(JST) - timedelta(seconds=5)).isoformat()
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "P1", "pass": 2, "target_pass": 2, "at": after_enter,
                          "summary": "found issue in pass 2"},
            },
        }]
        data = {
            "project": "test-pj",
            "_npass_target_reviewers": ["alice"],
            "history": [{"from": "CODE_REVIEW", "to": "CODE_REVIEW_NPASS", "at": entered}],
        }
        action = check_transition("CODE_REVIEW_NPASS", batch, data)
        assert action.new_state == "CODE_REVISE"

    def test_mixed_submitted_not_submitted_only_submitted_verdict_counts(self):
        """提出済み APPROVE + 未提出 P1(前回パス) → 即 REVISE にならず待機。"""
        old_time = (datetime.now(JST) - timedelta(minutes=5)).isoformat()
        entered = self._entered_at(10)
        after_enter = (datetime.now(JST) - timedelta(seconds=5)).isoformat()
        batch = [{
            "issue": 1,
            "code_reviews": {
                "alice": {"verdict": "APPROVE", "pass": 2, "target_pass": 2, "at": after_enter},
                "bob": {"verdict": "P1", "pass": 1, "target_pass": 2, "at": old_time,
                        "summary": "issue from pass 1"},
            },
        }]
        data = {
            "project": "test-pj",
            "_npass_target_reviewers": ["alice", "bob"],
            "history": [{"from": "CODE_REVIEW", "to": "CODE_REVIEW_NPASS", "at": entered}],
        }
        action = check_transition("CODE_REVIEW_NPASS", batch, data)
        assert action.new_state is None  # bob の未提出分を待機

    def test_no_targets_returns_noop(self):
        """_npass_target_reviewers が空 → 何もしない。"""
        data = {
            "project": "test-pj",
            "_npass_target_reviewers": [],
            "history": [{"from": "CODE_REVIEW", "to": "CODE_REVIEW_NPASS", "at": _now_iso()}],
        }
        action = check_transition("CODE_REVIEW_NPASS", [{"issue": 1}], data)
        assert action.new_state is None


# ---------------------------------------------------------------------------
# 5. post_gitlab_note (moved to notify.py)
# ---------------------------------------------------------------------------

class TestPostGitlabNote:
    def test_import_from_notify(self):
        """notify.post_gitlab_note がインポート可能であること。"""
        from notify import post_gitlab_note
        assert callable(post_gitlab_note)

    def test_commands_dev_reexport(self):
        """commands.dev._post_gitlab_note が notify からの再エクスポートであること。"""
        from commands.dev import _post_gitlab_note
        from notify import post_gitlab_note
        assert _post_gitlab_note is post_gitlab_note


# ---------------------------------------------------------------------------
# 6. NPASS file helpers
# ---------------------------------------------------------------------------

class TestNpassFileHelpers:
    def test_save_load_cleanup(self, tmp_path, monkeypatch):
        """save → load → cleanup のサイクル。"""
        from notify import _save_npass_review_file_path, _load_npass_review_file_path, cleanup_npass_files
        monkeypatch.setattr("notify.REVIEW_FILE_DIR", tmp_path)

        # save
        review_file = tmp_path / "dummy-review.md"
        review_file.write_text("review content", encoding="utf-8")
        _save_npass_review_file_path("test-pj", "alice", review_file)

        # load
        loaded = _load_npass_review_file_path("test-pj", "alice")
        assert loaded == str(review_file)

        # cleanup
        cleanup_npass_files("test-pj")
        assert not review_file.exists()
        assert _load_npass_review_file_path("test-pj", "alice") is None


# ---------------------------------------------------------------------------
# 7. _reset_to_idle pops NPASS fields
# ---------------------------------------------------------------------------

class TestResetToIdleNpass:
    def test_pops_npass_fields(self, tmp_pipelines):
        """_reset_to_idle が _npass_target_reviewers をクリアすること。"""
        from commands.dev import _reset_to_idle
        data = {
            "project": "test-pj",
            "state": "CODE_REVIEW_NPASS",
            "batch": [{"issue": 1}],
            "enabled": True,
            "_npass_target_reviewers": ["alice"],
            "history": [],
        }
        with patch("engine.cc._kill_pytest_baseline"), \
             patch("engine.reviewer._cleanup_review_files"), \
             patch("notify.cleanup_npass_files") as mock_cleanup:
            _reset_to_idle(data)
        assert "_npass_target_reviewers" not in data
        mock_cleanup.assert_called_once_with("test-pj")


# ---------------------------------------------------------------------------
# 8. Forced externalization for CODE_REVIEW with n_pass
# ---------------------------------------------------------------------------

class TestForcedExternalization:
    def test_code_review_npass_forces_externalize(self, monkeypatch):
        """CODE_REVIEW で n_pass > 1 のレビュアー → 強制外部化。"""
        from notify import notify_reviewers

        monkeypatch.setattr("notify.REVIEW_MODES", {
            "standard": {
                "members": ["alice"],
                "min_reviews": 1,
                "grace_period_sec": 0,
                "n_pass": {"alice": 2},
            },
        })
        monkeypatch.setattr("notify.AGENTS", {"alice": "agent:alice:main"})

        # Track what _write_review_file and _save_npass_review_file_path do
        saved_paths = {}

        def mock_write_review_file(project, reviewer, content):
            from pathlib import Path
            return Path("/tmp/fake-review-file.md")

        def mock_save_npass(project, reviewer, file_path):
            saved_paths[(project, reviewer)] = str(file_path)

        with patch("notify._write_review_file", side_effect=mock_write_review_file), \
             patch("notify._save_npass_review_file_path", side_effect=mock_save_npass) as mock_save, \
             patch("notify.send_to_agent", return_value=True), \
             patch("notify.format_review_request", return_value="review content"), \
             patch("notify._build_file_review_message", return_value="short msg"), \
             patch("notify._check_squash", return_value=[]), \
             patch("notify.config.MAX_CLI_ARG_BYTES", 999999999):  # 大きい値でサイズ判定を無効化
            notify_reviewers("test-pj", "CODE_REVIEW", [{"issue": 1}],
                           "testns/test-pj", review_mode="standard")

        mock_save.assert_called_once()
        assert ("test-pj", "alice") in saved_paths
