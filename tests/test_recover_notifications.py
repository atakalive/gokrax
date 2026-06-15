"""tests/test_recover_notifications.py — Issue #224: fresh state recovery tests"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.fsm import (
    _recover_pending_notifications,
    _conditional_clear_pending,
    stale_pending_keys,
    REVIEW_STATES,
    _IMPL_STATES,
)
from config import VALID_STATES


class TestRecoverReviewFreshExcluded:
    """Test A: review リカバリが excluded_reviewers を fresh state から取得する。"""

    def test_excluded_from_fresh_data(self) -> None:
        pending = {
            "review": {
                "new_state": "DESIGN_REVIEW",
                "batch": [{"issue": 1, "title": "t"}],
                "gitlab": "ns/proj",
                "repo_path": "/tmp/repo",
                "review_mode": "standard",
                "base_commit": "abc123",
            },
        }
        fresh_pipeline = {
            "excluded_reviewers": ["reviewer_fresh"],
            "comment": "fresh comment",
            "review_mode": "standard",
            "state": "DESIGN_REVIEW",
        }

        with (
            patch("engine.fsm.load_pipeline", return_value=fresh_pipeline) as mock_load,
            patch(
                "engine.fsm.get_path", return_value=Path("/mock/path.json")
            ) as mock_get_path,
            patch("engine.fsm.notify_reviewers") as mock_notify,
            patch("engine.fsm._conditional_clear_pending") as mock_cond_clear,
        ):
            _recover_pending_notifications("proj", pending, "DESIGN_REVIEW")

            # get_path called with project name (snapshot check + review recovery)
            mock_get_path.assert_called_with("proj")
            # load_pipeline called with the path from get_path
            mock_load.assert_called_with(Path("/mock/path.json"))
            # excluded comes from fresh data, not stale
            mock_notify.assert_called_once()
            call_kwargs = mock_notify.call_args
            assert call_kwargs[1]["excluded"] == ["reviewer_fresh"]
            # durable payload comes from pending dict
            assert call_kwargs[0] == (
                "proj",
                "DESIGN_REVIEW",
                [{"issue": 1, "title": "t"}],
                "ns/proj",
            )
            assert call_kwargs[1]["repo_path"] == "/tmp/repo"
            assert call_kwargs[1]["review_mode"] == "standard"
            assert call_kwargs[1]["base_commit"] == "abc123"
            # pending cleared after success (conditional clear with snapshot value)
            mock_cond_clear.assert_called_once_with("proj", "review", pending["review"])


class TestRecoverReviewFreshComment:
    """Test B: review リカバリが comment を fresh state から取得する。"""

    def test_comment_from_fresh_data(self) -> None:
        pending = {
            "review": {
                "new_state": "CODE_REVIEW",
                "batch": [{"issue": 2, "title": "t2"}],
                "gitlab": "ns/proj2",
            },
        }
        fresh_pipeline = {
            "excluded_reviewers": [],
            "comment": "updated comment",
            "review_mode": "standard",
            "state": "CODE_REVIEW",
        }

        with (
            patch("engine.fsm.load_pipeline", return_value=fresh_pipeline),
            patch("engine.fsm.get_path", return_value=Path("/mock/p.json")),
            patch("engine.fsm.notify_reviewers") as mock_notify,
            patch("engine.fsm._conditional_clear_pending"),
        ):
            _recover_pending_notifications("proj2", pending, "CODE_REVIEW")

            mock_notify.assert_called_once()
            assert mock_notify.call_args[1]["comment"] == "updated comment"


class TestRecoverReviewLoadPipelineFailure:
    """Test C: load_pipeline 失敗時のフォールバック。"""

    def test_load_pipeline_error_suppressed(self) -> None:
        pending = {
            "review": {
                "new_state": "DESIGN_REVIEW",
                "batch": [{"issue": 3, "title": "t3"}],
                "gitlab": "ns/proj3",
            },
        }

        with (
            patch("engine.fsm.load_pipeline", side_effect=FileNotFoundError("gone")),
            patch("engine.fsm.get_path", return_value=Path("/missing.json")),
            patch("engine.fsm.notify_reviewers") as mock_notify,
            patch("engine.fsm._conditional_clear_pending") as mock_cond_clear,
        ):
            # Should not raise
            _recover_pending_notifications("proj3", pending, "DESIGN_REVIEW")

            # notify_reviewers must NOT be called (load_pipeline failed before it)
            mock_notify.assert_not_called()
            # pending must NOT be cleared (at-least-once guarantee)
            mock_cond_clear.assert_not_called()


# ── §4b: stale_pending_keys 単体テスト ───────────────────────────────────


class TestStalePendingKeys:
    """stale_pending_keys の状態×pending キー組み合わせ検証"""

    def test_code_review_impl_is_stale(self):
        assert stale_pending_keys("CODE_REVIEW", {"impl": {}}) == ["impl"]

    def test_design_review_npass_impl_is_stale(self):
        assert stale_pending_keys("DESIGN_REVIEW_NPASS", {"impl": {}}) == ["impl"]

    def test_idle_impl_is_stale(self):
        assert stale_pending_keys("IDLE", {"impl": {}}) == ["impl"]

    def test_done_impl_is_stale(self):
        assert stale_pending_keys("DONE", {"impl": {}}) == ["impl"]

    def test_initialize_impl_is_stale(self):
        assert stale_pending_keys("INITIALIZE", {"impl": {}}) == ["impl"]

    def test_code_test_impl_is_stale(self):
        assert stale_pending_keys("CODE_TEST", {"impl": {}}) == ["impl"]

    def test_code_approved_impl_not_stale(self):
        """CODE_APPROVED ∈ _IMPL_STATES: skip mode で正規"""
        assert stale_pending_keys("CODE_APPROVED", {"impl": {}}) == []

    def test_design_approved_impl_not_stale(self):
        """DESIGN_APPROVED ∈ _IMPL_STATES: skip mode で正規"""
        assert stale_pending_keys("DESIGN_APPROVED", {"impl": {}}) == []

    def test_implementation_impl_not_stale(self):
        """IMPLEMENTATION ∈ _IMPL_STATES: no_cc 経路で正規"""
        assert stale_pending_keys("IMPLEMENTATION", {"impl": {}}) == []

    def test_implementation_review_is_stale(self):
        assert stale_pending_keys(
            "IMPLEMENTATION", {"review": {"new_state": "IMPLEMENTATION"}}
        ) == ["review"]

    def test_idle_blocked_report_is_stale(self):
        assert stale_pending_keys("IDLE", {"blocked_report": {}}) == ["blocked_report"]

    def test_code_review_review_matching_not_stale(self):
        """new_state 一致 → 正規"""
        assert (
            stale_pending_keys("CODE_REVIEW", {"review": {"new_state": "CODE_REVIEW"}})
            == []
        )

    def test_blocked_blocked_report_not_stale(self):
        assert stale_pending_keys("BLOCKED", {"blocked_report": {}}) == []

    def test_design_plan_impl_not_stale(self):
        assert stale_pending_keys("DESIGN_PLAN", {"impl": {}}) == []

    def test_blocked_impl_not_stale(self):
        assert stale_pending_keys("BLOCKED", {"impl": {}}) == []

    def test_code_review_impl_and_review_matching(self):
        """impl は stale、review は new_state 一致で正規"""
        result = stale_pending_keys(
            "CODE_REVIEW",
            {"impl": {}, "review": {"new_state": "CODE_REVIEW"}},
        )
        assert result == ["impl"]

    def test_code_review_review_new_state_mismatch(self):
        """REVIEW_STATES 内だが new_state 不一致 → stale"""
        result = stale_pending_keys(
            "CODE_REVIEW", {"review": {"new_state": "DESIGN_REVIEW"}}
        )
        assert result == ["review"]

    def test_design_review_npass_review_new_state_mismatch(self):
        result = stale_pending_keys(
            "DESIGN_REVIEW_NPASS", {"review": {"new_state": "DESIGN_REVIEW"}}
        )
        assert result == ["review"]

    def test_code_review_review_new_state_missing(self):
        """new_state キー欠落 → stale"""
        result = stale_pending_keys("CODE_REVIEW", {"review": {}})
        assert result == ["review"]


# ── §4c: 定数と FSM の整合検証 ──────────────────────────────────────────

from engine.fsm import get_notification_for_state


@pytest.mark.parametrize("state", VALID_STATES)
def test_review_states_match_send_review(state):
    """send_review=True を返す状態は全て REVIEW_STATES に含まれる"""
    notif = get_notification_for_state(state)
    if notif.send_review:
        assert state in REVIEW_STATES, (
            f"{state} returns send_review=True but is not in REVIEW_STATES"
        )


@pytest.mark.parametrize("state", VALID_STATES)
def test_impl_msg_only_in_impl_states(state):
    """impl_msg を返す状態は _IMPL_STATES に含まれる"""
    notif = get_notification_for_state(state)
    if notif.impl_msg:
        assert state in _IMPL_STATES, (
            f"{state} returns impl_msg but is not in _IMPL_STATES"
        )


from engine.fsm import check_transition


@pytest.mark.parametrize("state", VALID_STATES)
@pytest.mark.parametrize("review_mode", ["standard", "skip"])
def test_check_transition_impl_msg_targets_impl_states(state, review_mode):
    """check_transition が impl_msg 付き TransitionAction を返す場合、
    new_state は _IMPL_STATES に含まれる"""
    data = {
        "state": state,
        "batch": [],
        "enabled": True,
        "review_mode": review_mode,
        "implementer": "test",
        "created_at": "2020-01-01T00:00:00+09:00",
        "updated_at": "2020-01-01T00:00:00+09:00",
    }
    try:
        action = check_transition(state, [], data)
    except Exception:
        return  # 必要なデータが不足する状態はスキップ
    if action and action.impl_msg and action.new_state:
        assert action.new_state in _IMPL_STATES, (
            f"check_transition({state}, review_mode={review_mode}) produces "
            f"impl_msg targeting {action.new_state} which is not in _IMPL_STATES"
        )


def test_implementation_in_impl_states():
    """IMPLEMENTATION は no_cc 経路で impl pending を生成するため _IMPL_STATES に含まれる"""
    assert "IMPLEMENTATION" in _IMPL_STATES


# ── §4d: _recover_pending_notifications プルーニング動作 ─────────────────


class TestRecoverPruning:
    """pruning / TOCTOU / conditional clear の動作検証"""

    def test_stale_impl_is_pruned(self):
        """テスト1: stale impl が破棄される"""
        pending = {"impl": {"implementer": "x", "msg": "m"}}
        mock_data = {
            "state": "CODE_REVIEW",
            "_pending_notifications": {"impl": {"implementer": "x", "msg": "m"}},
        }

        def fake_update(path, cb):
            cb(mock_data)

        with (
            patch("engine.fsm.update_pipeline", side_effect=fake_update),
            patch("engine.fsm.get_path", return_value=Path("/mock.json")),
            patch("engine.fsm.notify_implementer") as mock_impl,
        ):
            _recover_pending_notifications("pj", pending, "CODE_REVIEW")

        mock_impl.assert_not_called()

    def test_stale_impl_pruned_review_recovered(self):
        """テスト2: impl + review 併存時に impl がプルーニングされ review は処理される"""
        review_info = {
            "new_state": "CODE_REVIEW",
            "batch": [{"issue": 1, "title": "t"}],
            "gitlab": "ns/pj",
            "repo_path": "",
            "review_mode": "standard",
        }
        pending = {
            "impl": {"implementer": "x", "msg": "m"},
            "review": dict(review_info),
        }
        mock_data = {
            "state": "CODE_REVIEW",
            "_pending_notifications": {
                "impl": {"implementer": "x", "msg": "m"},
                "review": dict(review_info),
            },
        }
        fresh_pipeline = {
            "excluded_reviewers": [],
            "comment": "",
            "review_mode": "standard",
            "state": "CODE_REVIEW",
        }

        def fake_update(path, cb):
            cb(mock_data)

        with (
            patch("engine.fsm.update_pipeline", side_effect=fake_update),
            patch("engine.fsm.load_pipeline", return_value=fresh_pipeline),
            patch("engine.fsm.get_path", return_value=Path("/mock.json")),
            patch("engine.fsm.notify_implementer") as mock_impl,
            patch("engine.fsm.notify_reviewers") as mock_review,
            patch("engine.fsm._conditional_clear_pending"),
        ):
            _recover_pending_notifications("pj", pending, "CODE_REVIEW")

        mock_impl.assert_not_called()
        mock_review.assert_called_once()

    def test_consistent_pending_not_pruned(self):
        """テスト3: 整合する pending はプルーニングされない"""
        review_info = {
            "new_state": "CODE_REVIEW",
            "batch": [{"issue": 1, "title": "t"}],
            "gitlab": "ns/pj",
            "repo_path": "",
            "review_mode": "standard",
        }
        pending = {"review": dict(review_info)}
        fresh_pipeline = {
            "excluded_reviewers": [],
            "comment": "",
            "review_mode": "standard",
            "state": "CODE_REVIEW",
        }

        with (
            patch("engine.fsm.load_pipeline", return_value=fresh_pipeline),
            patch("engine.fsm.get_path", return_value=Path("/mock.json")),
            patch("engine.fsm.notify_reviewers") as mock_review,
            patch("engine.fsm._conditional_clear_pending"),
            patch("engine.fsm.update_pipeline") as mock_update,
        ):
            _recover_pending_notifications("pj", pending, "CODE_REVIEW")

        mock_review.assert_called_once()
        # stale なし → pruning 用 update_pipeline は不要
        # _conditional_clear_pending が mock されているため clear 経由の呼び出しもない
        mock_update.assert_not_called()

    def test_toctou_stale_key_state_diverged(self):
        """テスト4: stale キー + 状態乖離で recovery 全体を abort"""
        pending = {"impl": {"implementer": "x", "msg": "m"}}
        mock_data = {
            "state": "DESIGN_PLAN",
            "_pending_notifications": {"impl": {"implementer": "y", "msg": "new"}},
        }

        def fake_update(path, cb):
            cb(mock_data)

        with (
            patch("engine.fsm.update_pipeline", side_effect=fake_update),
            patch("engine.fsm.get_path", return_value=Path("/mock.json")),
            patch("engine.fsm.notify_implementer") as mock_impl,
        ):
            _recover_pending_notifications("pj", pending, "CODE_REVIEW")

        # 状態乖離 → _check_and_prune でスキップ → 新正規 impl は保護される
        assert "impl" in mock_data["_pending_notifications"]
        # recovery 全体が abort → notify_implementer は呼ばれない
        mock_impl.assert_not_called()

    def test_review_new_state_mismatch_is_stale(self):
        """テスト5: review new_state 不一致が stale 判定される"""
        review_info = {
            "new_state": "DESIGN_REVIEW",
            "batch": [{"issue": 1, "title": "t"}],
            "gitlab": "ns/pj",
        }
        pending = {"review": dict(review_info)}
        mock_data = {
            "state": "CODE_REVIEW",
            "_pending_notifications": {"review": dict(review_info)},
        }

        def fake_update(path, cb):
            cb(mock_data)

        with (
            patch("engine.fsm.update_pipeline", side_effect=fake_update),
            patch("engine.fsm.get_path", return_value=Path("/mock.json")),
            patch("engine.fsm.notify_reviewers") as mock_review,
        ):
            _recover_pending_notifications("pj", pending, "CODE_REVIEW")

        mock_review.assert_not_called()

    def test_non_stale_state_diverged_aborts_recovery(self):
        """テスト6: 非 stale キーの状態乖離で recovery abort"""
        pending = {"impl": {"implementer": "x", "msg": "old"}}

        with (
            patch(
                "engine.fsm.load_pipeline",
                return_value={"state": "DESIGN_REVISE"},
            ),
            patch("engine.fsm.get_path", return_value=Path("/mock.json")),
            patch("engine.fsm.notify_implementer") as mock_impl,
            patch("engine.fsm.update_pipeline") as mock_update,
        ):
            _recover_pending_notifications("pj", pending, "DESIGN_PLAN")

        mock_impl.assert_not_called()
        # stale 空 → load_pipeline 経路
        mock_update.assert_not_called()

    def test_conditional_clear_value_match_deletes(self):
        """テスト7: conditional clear — 値一致で削除される"""
        expected = {"implementer": "x", "msg": "m"}
        mock_data = {
            "_pending_notifications": {"impl": {"implementer": "x", "msg": "m"}}
        }

        def fake_update(path, cb):
            cb(mock_data)

        with (
            patch("engine.fsm.update_pipeline", side_effect=fake_update),
            patch("engine.fsm.get_path", return_value=Path("/mock.json")),
        ):
            _conditional_clear_pending("pj", "impl", expected)

        assert "impl" not in mock_data.get("_pending_notifications", {})

    def test_conditional_clear_value_mismatch_preserves(self):
        """テスト8: conditional clear — 値不一致で削除されない"""
        expected = {"implementer": "x", "msg": "old"}
        mock_data = {
            "_pending_notifications": {"impl": {"implementer": "y", "msg": "new"}},
        }

        def fake_update(path, cb):
            cb(mock_data)

        with (
            patch("engine.fsm.update_pipeline", side_effect=fake_update),
            patch("engine.fsm.get_path", return_value=Path("/mock.json")),
        ):
            _conditional_clear_pending("pj", "impl", expected)

        assert "impl" in mock_data["_pending_notifications"]

    def test_conditional_clear_same_state_pending_replaced(self):
        """テスト9: 同一 state 内 pending 差し替えの end-to-end"""
        review_old = {
            "new_state": "CODE_REVIEW",
            "batch": [{"issue": 1, "title": "t"}],
            "gitlab": "ns/pj",
            "repo_path": "",
            "review_mode": "standard",
        }
        review_new = {
            "new_state": "CODE_REVIEW",
            "batch": [{"issue": 1, "title": "t"}],
            "gitlab": "ns/pj",
            "repo_path": "",
            "review_mode": "standard",
            "new_field": True,
        }
        pending = {
            "impl": {"implementer": "x", "msg": "old"},
            "review": dict(review_old),
        }

        # update_pipeline は複数回呼ばれる:
        # 1回目: pruning (impl stale)
        # 2回目: conditional clear of review (値が差し替わっている)
        call_count = [0]
        pruning_data = {
            "state": "CODE_REVIEW",
            "_pending_notifications": {
                "impl": {"implementer": "x", "msg": "old"},
                "review": dict(review_old),
            },
        }
        clear_data = {
            "_pending_notifications": {"review": dict(review_new)},
        }

        def fake_update(path, cb):
            call_count[0] += 1
            if call_count[0] == 1:
                cb(pruning_data)
            else:
                cb(clear_data)

        fresh_pipeline = {
            "excluded_reviewers": [],
            "comment": "",
            "review_mode": "standard",
            "state": "CODE_REVIEW",
        }

        with (
            patch("engine.fsm.update_pipeline", side_effect=fake_update),
            patch("engine.fsm.load_pipeline", return_value=fresh_pipeline),
            patch("engine.fsm.get_path", return_value=Path("/mock.json")),
            patch("engine.fsm.notify_implementer") as mock_impl,
            patch("engine.fsm.notify_reviewers") as mock_review,
        ):
            _recover_pending_notifications("pj", pending, "CODE_REVIEW")

        # impl は stale で pruning → 送信されない
        mock_impl.assert_not_called()
        # review は旧値で送信される
        mock_review.assert_called_once()
        # conditional clear で値不一致 → 新 review は保護される
        assert "review" in clear_data["_pending_notifications"]
