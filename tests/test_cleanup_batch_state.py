"""tests/test_cleanup_batch_state.py — _cleanup_batch_state / _reset_to_idle テスト (Issue #221)"""

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_full_data() -> dict:
    """クリーンアップ対象の全フィールド + 非対象フィールドを持つ data を返す。"""
    return {
        # 非対象（変更されてはならない）
        "state": "DONE",
        "history": [{"from": "X", "to": "Y"}],
        "project": "testpj",
        "spec_mode": False,
        "spec_config": {},
        # batch / enabled
        "batch": [{"issue": 1}],
        "enabled": True,
        # REVISE counters
        "design_revise_count": 3,
        "code_revise_count": 2,
        "max_design_revise_cycles": 8,
        "max_code_revise_cycles": 12,
        # Queue options
        "automerge": True,
        "p2_fix": True,
        "cc_plan_model": "opus",
        "cc_impl_model": "sonnet",
        "keep_context": True,
        "keep_ctx_batch": True,
        "keep_ctx_intra": True,
        "comment": "note",
        "skip_cc_plan": True,
        "skip_test": True,
        "skip_assess": True,
        "skip_design": True,
        "no_cc": True,
        "exclude_high_risk": True,
        "exclude_any_risk": True,
        "assessment": {"domain_risk": "HIGH"},
        # Timeout
        "timeout_extension": 300,
        "extend_count": 1,
        # Queue mode
        "queue_mode": True,
        # pytest baseline
        "test_baseline": "ok",
        "_pytest_baseline": {"pid": 12345},
        # CODE_TEST
        "test_result": "fail",
        "test_output": "some output",
        "test_retry_count": 2,
        # CC
        "cc_pid": 99999,
        "cc_session_id": "sess-abc",
        # Base commit
        "base_commit": "abc123",
        # Reviewer
        "excluded_reviewers": ["r1"],
        "min_reviews_override": 2,
        "review_config": {"design": {}},
        "reviewer_number_map": {"r1": 1},
        # Merge summary
        "summary_message_id": "msg-123",
        # Pending notifications
        "_pending_notifications": ["n1"],
        # State timer
        "_state_entered_at": "2025-01-01T00:00:00+09:00",
        # Previous reviews
        "_prev_design_reviews": {},
        "_prev_code_reviews": {},
        # NPASS
        "_npass_target_reviewers": ["r1"],
        # Nudge (static)
        "_last_nudge_at": "2025-01-01T00:00:00+09:00",
        # Nudge (dynamic)
        "_nudge_failed_r1": True,
        "_last_nudge_r2": "2025-01-01",
        # notify count (dynamic)
        "design_review_notify_count": 3,
        "code_review_notify_count": 1,
    }


# クリーンアップ対象として期待される全キー
_CLEANUP_KEYS = {
    "design_revise_count", "code_revise_count",
    "max_design_revise_cycles", "max_code_revise_cycles",
    "automerge", "p2_fix", "cc_plan_model", "cc_impl_model",
    "keep_context", "keep_ctx_batch", "keep_ctx_intra",
    "comment", "skip_cc_plan", "skip_test", "skip_assess", "skip_design",
    "no_cc", "exclude_high_risk", "exclude_any_risk", "assessment",
    "timeout_extension", "extend_count",
    "queue_mode",
    "test_baseline", "_pytest_baseline",
    "test_result", "test_output", "test_retry_count",
    "cc_pid", "cc_session_id",
    "base_commit",
    "excluded_reviewers", "min_reviews_override", "review_config", "reviewer_number_map",
    "summary_message_id",
    "_pending_notifications",
    "_state_entered_at",
    "_prev_design_reviews", "_prev_code_reviews",
    "_npass_target_reviewers",
    "_last_nudge_at",
    "_nudge_failed_r1", "_last_nudge_r2",
    "design_review_notify_count", "code_review_notify_count",
}

# 非対象キー（変更されてはならない）
_PRESERVED_KEYS = {"state", "history", "project", "spec_mode", "spec_config"}


class TestCleanupBatchState:
    """Test A: _cleanup_batch_state が全フィールドをクリアする"""

    def test_all_fields_cleared(self):
        from engine.cleanup import _cleanup_batch_state

        data = _make_full_data()
        preserved = {k: data[k] for k in _PRESERVED_KEYS}

        with patch("engine.cc._kill_pytest_baseline") as mock_kpb, \
             patch("engine.cc._kill_code_test") as mock_kct, \
             patch("engine.reviewer._cleanup_review_files") as mock_crf, \
             patch("notify.cleanup_npass_files") as mock_cnf:
            _cleanup_batch_state(data, "testpj")

            mock_kpb.assert_called_once_with(data, "testpj")
            mock_kct.assert_called_once_with(data, "testpj")
            mock_crf.assert_called_once_with("testpj")
            mock_cnf.assert_called_once_with("testpj")

        # batch/enabled は設定される
        assert data["batch"] == []
        assert data["enabled"] is False

        # 対象キーが全て除去されている
        for key in _CLEANUP_KEYS:
            assert key not in data, f"key {key!r} should be removed"

        # 非対象キーが変更されていない
        for key in _PRESERVED_KEYS:
            assert data[key] == preserved[key], f"key {key!r} should be preserved"


class TestResetToIdleDelegation:
    """Test B: _reset_to_idle が _cleanup_batch_state に委譲する"""

    def test_delegates_to_cleanup_batch_state(self):
        from commands.dev import _reset_to_idle

        data = {"project": "testpj", "state": "DONE", "batch": [{"issue": 1}]}

        with patch("engine.cleanup._cleanup_batch_state") as mock_cleanup:
            _reset_to_idle(data)
            mock_cleanup.assert_called_once_with(data, "testpj")

    def test_empty_project(self):
        from commands.dev import _reset_to_idle

        data = {"state": "DONE", "batch": [{"issue": 1}]}

        with patch("engine.cleanup._cleanup_batch_state") as mock_cleanup:
            _reset_to_idle(data)
            mock_cleanup.assert_called_once_with(data, "")


class TestCleanupStructuralEquivalence:
    """Test F: 3経路が全て _cleanup_batch_state を呼ぶことの構造的検証。

    _cleanup_batch_state が唯一のクリーンアップ実装なので、
    3経路全てがこの関数を呼ぶことで同値性が構造的に保証される。
    """

    def test_cleanup_removes_expected_keys(self):
        """_cleanup_batch_state の削除キー集合が期待値と一致する"""
        from engine.cleanup import _cleanup_batch_state

        data = _make_full_data()
        keys_before = set(data.keys())

        with patch("engine.cc._kill_pytest_baseline"), \
             patch("engine.cc._kill_code_test"), \
             patch("engine.reviewer._cleanup_review_files"), \
             patch("notify.cleanup_npass_files"):
            _cleanup_batch_state(data, "testpj")

        keys_after = set(data.keys())
        removed = keys_before - keys_after
        assert removed == _CLEANUP_KEYS
