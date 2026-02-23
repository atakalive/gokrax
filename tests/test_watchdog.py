"""tests/test_watchdog.py — watchdog.py の Double-Checked Locking / check_transition テスト"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, call, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── ヘルパー ──────────────────────────────────────────────────────────────────

def _make_batch(n=1, **kwargs):
    """テスト用バッチアイテムを生成。"""
    items = []
    for i in range(1, n + 1):
        item = {
            "issue": i, "title": f"Issue {i}", "commit": None,
            "cc_session_id": None,
            "design_reviews": {}, "code_reviews": {},
            "added_at": "2025-01-01T00:00:00+09:00",
        }
        item.update(kwargs)
        items.append(item)
    return items


def _make_reviews(verdicts: list[str]) -> dict:
    """レビュー辞書を生成。"""
    return {f"reviewer{i}": {"verdict": v, "at": ""} for i, v in enumerate(verdicts)}


def _write_pipeline(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── TestCheckTransition ───────────────────────────────────────────────────────

class TestCheckTransition:

    def test_idle_returns_no_action(self):
        from watchdog import check_transition
        action = check_transition("IDLE", _make_batch())
        assert action.new_state is None

    def test_triage_returns_no_action(self):
        from watchdog import check_transition
        assert check_transition("TRIAGE", _make_batch()).new_state is None

    def test_merge_summary_sent_returns_no_action(self):
        from watchdog import check_transition
        assert check_transition("MERGE_SUMMARY_SENT", _make_batch()).new_state is None

    def test_design_approved_auto_transitions_to_implementation(self):
        from watchdog import check_transition
        action = check_transition("DESIGN_APPROVED", _make_batch())
        assert action.new_state == "IMPLEMENTATION"

    def test_blocked_returns_no_action(self):
        from watchdog import check_transition
        assert check_transition("BLOCKED", _make_batch()).new_state is None

    def test_done_always_transitions_to_idle(self):
        from watchdog import check_transition
        action = check_transition("DONE", [])
        assert action.new_state == "IDLE"
        assert action.impl_msg is not None

    def test_done_with_batch_still_transitions(self):
        from watchdog import check_transition
        action = check_transition("DONE", _make_batch())
        assert action.new_state == "IDLE"

    def test_empty_batch_returns_no_action(self):
        from watchdog import check_transition
        for state in ("DESIGN_PLAN", "DESIGN_REVIEW", "CODE_REVIEW",
                      "DESIGN_REVISE", "CODE_REVISE", "IMPLEMENTATION"):
            action = check_transition(state, [])
            assert action.new_state is None, f"state={state} should be no-op with empty batch"

    def test_design_plan_all_ready(self):
        from watchdog import check_transition
        batch = _make_batch(2, design_ready=True)
        action = check_transition("DESIGN_PLAN", batch)
        assert action.new_state == "DESIGN_REVIEW"
        assert action.send_review is True

    def test_design_plan_not_all_ready(self):
        from watchdog import check_transition
        batch = _make_batch(2)
        batch[0]["design_ready"] = True
        action = check_transition("DESIGN_PLAN", batch)
        assert action.new_state is None

    def test_design_review_p0_enough_reviews(self):
        from watchdog import check_transition
        import config
        reviews = _make_reviews(["APPROVE"] * (config.MIN_REVIEWS - 1) + ["P0"])
        batch = [{"issue": 1, "design_reviews": reviews, "code_reviews": {}}]
        action = check_transition("DESIGN_REVIEW", batch)
        assert action.new_state == "DESIGN_REVISE"
        assert action.impl_msg is not None

    def test_design_review_approved_enough_reviews(self):
        from watchdog import check_transition
        import config
        reviews = _make_reviews(["APPROVE"] * config.MIN_REVIEWS)
        batch = [{"issue": 1, "design_reviews": reviews, "code_reviews": {}}]
        action = check_transition("DESIGN_REVIEW", batch)
        assert action.new_state == "DESIGN_APPROVED"

    def test_design_review_not_enough_reviews(self):
        from watchdog import check_transition
        batch = [{"issue": 1, "design_reviews": _make_reviews(["APPROVE"]), "code_reviews": {}}]
        action = check_transition("DESIGN_REVIEW", batch)
        assert action.new_state is None

    def test_code_review_p0_enough_reviews(self):
        from watchdog import check_transition
        import config
        reviews = _make_reviews(["APPROVE"] * (config.MIN_REVIEWS - 1) + ["REJECT"])
        batch = [{"issue": 1, "code_reviews": reviews, "design_reviews": {}}]
        action = check_transition("CODE_REVIEW", batch)
        assert action.new_state == "CODE_REVISE"

    def test_code_review_approved_enough_reviews(self):
        from watchdog import check_transition
        import config
        reviews = _make_reviews(["APPROVE"] * config.MIN_REVIEWS)
        batch = [{"issue": 1, "code_reviews": reviews, "design_reviews": {}}]
        action = check_transition("CODE_REVIEW", batch)
        assert action.new_state == "CODE_APPROVED"

    def test_design_revise_all_revised(self):
        from watchdog import check_transition
        batch = _make_batch(2, design_revised=True)
        action = check_transition("DESIGN_REVISE", batch)
        assert action.new_state == "DESIGN_REVIEW"
        assert action.send_review is True

    def test_design_revise_not_all_revised(self):
        from watchdog import check_transition
        batch = _make_batch(2)
        batch[0]["design_revised"] = True
        action = check_transition("DESIGN_REVISE", batch)
        assert action.new_state is None

    def test_code_revise_all_revised(self):
        from watchdog import check_transition
        batch = _make_batch(2, code_revised=True)
        action = check_transition("CODE_REVISE", batch)
        assert action.new_state == "CODE_REVIEW"
        assert action.send_review is True

    def test_implementation_all_committed(self):
        from watchdog import check_transition
        batch = _make_batch(2, commit="abc123")
        action = check_transition("IMPLEMENTATION", batch)
        assert action.new_state == "CODE_REVIEW"
        assert action.send_review is True

    def test_implementation_not_all_committed(self):
        from watchdog import check_transition
        batch = _make_batch(2)
        batch[0]["commit"] = "abc123"
        action = check_transition("IMPLEMENTATION", batch)
        assert action.new_state is None


# ── TestProcess ───────────────────────────────────────────────────────────────

class TestProcessDisabled:

    def test_disabled_pipeline_skips(self, tmp_path, monkeypatch):
        import config, pipeline_io
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)

        path = tmp_path / "test-pj.json"
        data = {
            "project": "test-pj", "state": "DESIGN_PLAN",
            "enabled": False, "batch": _make_batch(1, design_ready=True),
            "history": [], "created_at": "", "updated_at": "",
        }
        _write_pipeline(path, data)

        from watchdog import process
        with patch("watchdog.update_pipeline") as mock_up:
            process(path)
        mock_up.assert_not_called()


class TestProcessUpdatePipelineCalled:

    def test_update_pipeline_called_on_transition(self, tmp_path, monkeypatch):
        import config, pipeline_io
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)

        path = tmp_path / "test-pj.json"
        data = {
            "project": "test-pj", "state": "DESIGN_PLAN",
            "enabled": True, "batch": _make_batch(1, design_ready=True),
            "history": [], "created_at": "", "updated_at": "",
        }
        _write_pipeline(path, data)

        from watchdog import process

        def fake_update(p, cb):
            # コールバックを実際のデータで呼んで状態変化をシミュレート
            cb(data)
            return data

        with patch("watchdog.update_pipeline", side_effect=fake_update) as mock_up, \
             patch("watchdog.notify_discord"), \
             patch("watchdog.notify_reviewers"):
            process(path)

        mock_up.assert_called_once_with(path, mock_up.call_args[0][1])

    def test_empty_batch_skips_update_pipeline(self, tmp_path, monkeypatch):
        import config, pipeline_io
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)

        path = tmp_path / "test-pj.json"
        data = {
            "project": "test-pj", "state": "DESIGN_PLAN",
            "enabled": True, "batch": [],
            "history": [], "created_at": "", "updated_at": "",
        }
        _write_pipeline(path, data)

        from watchdog import process
        with patch("watchdog.update_pipeline") as mock_up:
            process(path)
        mock_up.assert_not_called()


class TestDoubleCheckedLocking:

    def test_state_change_during_lock_skips_notification(self, tmp_path, monkeypatch):
        """ロック取得中に状態が変わった場合、通知がスキップされること。"""
        import config, pipeline_io
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)

        path = tmp_path / "test-pj.json"
        original_data = {
            "project": "test-pj", "state": "DESIGN_PLAN",
            "enabled": True, "batch": _make_batch(1, design_ready=True),
            "history": [], "created_at": "", "updated_at": "",
        }
        _write_pipeline(path, original_data)

        from watchdog import process

        def fake_update(p, cb):
            # ロック取得中に別プロセスが状態を変えたとシミュレート
            changed_data = dict(original_data)
            changed_data["state"] = "DESIGN_REVIEW"  # 既に遷移済み
            cb(changed_data)
            return changed_data

        with patch("watchdog.update_pipeline", side_effect=fake_update), \
             patch("watchdog.notify_discord") as mock_discord, \
             patch("watchdog.notify_reviewers") as mock_reviewers:
            process(path)

        # 状態が変わっていたので通知はスキップ
        mock_discord.assert_not_called()
        mock_reviewers.assert_not_called()

    def test_notifications_sent_after_update_pipeline(self, tmp_path, monkeypatch):
        """通知がロック（update_pipeline）の後に送られること。"""
        import config, pipeline_io
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)

        path = tmp_path / "test-pj.json"
        data = {
            "project": "test-pj", "state": "DESIGN_PLAN",
            "enabled": True,
            "implementer": "kaneko",
            "gitlab": "atakalive/test-pj",
            "batch": _make_batch(1, design_ready=True),
            "history": [], "created_at": "", "updated_at": "",
        }
        _write_pipeline(path, data)

        from watchdog import process
        call_order = []

        def fake_update(p, cb):
            call_order.append("update_pipeline")
            cb(data)
            return data

        with patch("watchdog.update_pipeline", side_effect=fake_update), \
             patch("watchdog.notify_discord", side_effect=lambda *a: call_order.append("notify_discord")), \
             patch("watchdog.notify_reviewers", side_effect=lambda *a, **kw: call_order.append("notify_reviewers")):
            process(path)

        assert call_order[0] == "update_pipeline"
        assert "notify_discord" in call_order
        assert call_order.index("update_pipeline") < call_order.index("notify_discord")


class TestNoDirectSavePipeline:

    def test_save_pipeline_not_imported_or_called(self):
        """watchdog.py が save_pipeline を直接呼び出していないこと。"""
        import watchdog
        assert not hasattr(watchdog, "save_pipeline"), \
            "watchdog.py は save_pipeline をインポート・使用してはならない"

    def test_process_does_not_call_save_pipeline(self, tmp_path, monkeypatch):
        """process() 実行中に save_pipeline が呼ばれないこと。"""
        import config, pipeline_io
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)

        path = tmp_path / "test-pj.json"
        data = {
            "project": "test-pj", "state": "DESIGN_PLAN",
            "enabled": True, "batch": _make_batch(1, design_ready=True),
            "history": [], "created_at": "", "updated_at": "",
        }
        _write_pipeline(path, data)

        from watchdog import process

        def fake_update(p, cb):
            cb(data)
            return data

        with patch("watchdog.update_pipeline", side_effect=fake_update), \
             patch("watchdog.notify_discord"), \
             patch("watchdog.notify_reviewers"), \
             patch("pipeline_io.save_pipeline") as mock_save:
            process(path)

        mock_save.assert_not_called()


# ── TestIsAgentInactive ───────────────────────────────────────────────────────

class TestIsAgentInactive:
    """_is_agent_inactive() の新機能テスト (Issue #25)"""

    def test_active_when_cc_pid_exists_and_alive(self):
        """cc_pidが存在し、プロセスが生存していればアクティブ"""
        from watchdog import _is_agent_inactive

        data = {"cc_pid": 12345}

        # /proc/12345 が存在する（プロセス生存）
        with patch.object(Path, "exists", return_value=True):
            assert not _is_agent_inactive("kaneko", data)

    def test_inactive_when_cc_pid_exists_but_dead(self):
        """cc_pidが存在するがプロセス消滅時、セッション判定へ"""
        from watchdog import _is_agent_inactive

        data = {"cc_pid": 99999}

        # /proc/99999 が存在しない（プロセス消滅）
        with patch.object(Path, "exists", return_value=False):
            # セッションJSONも存在しない
            with patch("pathlib.Path.read_text", side_effect=FileNotFoundError):
                assert _is_agent_inactive("kaneko", data) is True

    def test_inactive_when_no_cc_pid(self):
        """cc_pidがない場合、セッション判定へ"""
        from watchdog import _is_agent_inactive

        data = {}

        # セッションJSONが存在しない
        with patch("pathlib.Path.read_text", side_effect=FileNotFoundError):
            assert _is_agent_inactive("kaneko", data) is True

    def test_inactive_when_cc_pid_is_none(self):
        """cc_pidがNoneの場合、セッション判定へ"""
        from watchdog import _is_agent_inactive

        data = {"cc_pid": None}

        # セッションJSONが存在しない
        with patch("pathlib.Path.read_text", side_effect=FileNotFoundError):
            assert _is_agent_inactive("kaneko", data) is True

    def test_active_with_valid_session_when_no_cc_pid(self):
        """cc_pidなし、セッションが最近更新されていればアクティブ"""
        from watchdog import _is_agent_inactive
        from datetime import datetime
        from config import JST
        import json

        data = {}

        # 10秒前に更新されたセッション
        now_ts = int(datetime.now(JST).timestamp() * 1000)
        recent_ts = now_ts - 10000  # 10秒前

        session_data = {
            "agent:kaneko:main": {
                "updatedAt": recent_ts
            }
        }

        with patch("pathlib.Path.read_text", return_value=json.dumps(session_data)):
            assert not _is_agent_inactive("kaneko", data)

    def test_inactive_with_old_session_when_no_cc_pid(self):
        """cc_pidなし、セッションが古ければ非アクティブ"""
        from watchdog import _is_agent_inactive
        from datetime import datetime
        from config import JST
        import json

        data = {}

        # 90秒前に更新されたセッション（閾値81秒超過）
        now_ts = int(datetime.now(JST).timestamp() * 1000)
        old_ts = now_ts - 90000  # 90秒前

        session_data = {
            "agent:kaneko:main": {
                "updatedAt": old_ts
            }
        }

        with patch("pathlib.Path.read_text", return_value=json.dumps(session_data)):
            assert _is_agent_inactive("kaneko", data) is True

    def test_pipeline_data_none_uses_session_fallback(self):
        """pipeline_data=None の場合、セッション判定のみ使用"""
        from watchdog import _is_agent_inactive

        # セッションJSONが存在しない
        with patch("pathlib.Path.read_text", side_effect=FileNotFoundError):
            assert _is_agent_inactive("kaneko", None) is True

    def test_is_cc_running_helper(self):
        """_is_cc_running() ヘルパー関数のテスト"""
        from watchdog import _is_cc_running

        # cc_pid が存在し、プロセス生存
        with patch.object(Path, "exists", return_value=True):
            assert _is_cc_running({"cc_pid": 123}) is True

        # cc_pid が存在し、プロセス消滅
        with patch.object(Path, "exists", return_value=False):
            assert _is_cc_running({"cc_pid": 456}) is False

        # cc_pid がない
        assert _is_cc_running({}) is False

        # cc_pid が None
        assert _is_cc_running({"cc_pid": None}) is False
