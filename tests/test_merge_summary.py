"""tests/test_merge_summary.py — #18 マージサマリー承認フローのテスト"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def write_pipeline(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _make_pipeline(state="CODE_APPROVED", **kwargs):
    data = {
        "project": "test-pj",
        "gitlab": "testns/test-pj",
        "state": state,
        "enabled": True,
        "implementer": "implementer1",
        "batch": [
            {
                "issue": 1, "title": "Fix bug", "commit": "abc123",
                "cc_session_id": None, "design_reviews": {}, "code_reviews": {},
                "added_at": "2025-01-01T00:00:00+09:00",
            }
        ],
        "history": [],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    data.update(kwargs)
    return data


def _make_discord_msg(message_id, ref_id, author_id, content):
    """Discord メッセージ辞書を生成。"""
    return {
        "id": message_id,
        "content": content,
        "author": {"id": author_id},
        "message_reference": {"message_id": ref_id},
    }


class TestCmdMergeSummary:

    def test_merge_summary_posts_and_saves_id(self, tmp_pipelines):
        """post_discord をモックして message_id が pipeline JSON に保存されること"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline())
        from gokrax import cmd_merge_summary
        args = argparse.Namespace(project="test-pj")
        with patch("notify.post_discord", return_value="1234567890") as mock_post, \
             patch("notify.send_to_agent", return_value=True):
            cmd_merge_summary(args)
        with open(path) as f:
            data = json.load(f)
        assert data["state"] == "MERGE_SUMMARY_SENT"
        assert data["summary_message_id"] == "1234567890"
        mock_post.assert_called_once()

    def test_merge_summary_wrong_state(self, tmp_pipelines):
        """CODE_APPROVED 以外の状態では SystemExit が発生すること"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IMPLEMENTATION"))
        from gokrax import cmd_merge_summary
        args = argparse.Namespace(project="test-pj")
        with pytest.raises(SystemExit, match="Cannot send merge summary"):
            cmd_merge_summary(args)

    def test_merge_summary_discord_post_fails(self, tmp_pipelines):
        """post_discord が None を返したとき SystemExit が発生すること"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline())
        from gokrax import cmd_merge_summary
        args = argparse.Namespace(project="test-pj")
        with patch("notify.post_discord", return_value=None), \
             patch("notify.send_to_agent", return_value=True):
            with pytest.raises(SystemExit, match="Discord 投稿に失敗"):
                cmd_merge_summary(args)

    def test_merge_summary_content_contains_project_and_issues(self, tmp_pipelines):
        """投稿内容にプロジェクト名・Issue番号・commit が含まれること"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline())
        from gokrax import cmd_merge_summary
        args = argparse.Namespace(project="test-pj")
        posted_content = []
        def mock_post(channel_id, content):
            posted_content.append(content)
            return "msg-id-999"
        with patch("notify.post_discord", side_effect=mock_post), \
             patch("notify.send_to_agent", return_value=True):
            cmd_merge_summary(args)
        assert posted_content
        content = posted_content[0]
        assert "test-pj" in content
        assert "#1" in content
        assert "abc123" in content


    def test_merge_summary_content_contains_footer(self, tmp_pipelines):
        """投稿内容に MERGE_SUMMARY_FOOTER が含まれること"""
        from config import MERGE_SUMMARY_FOOTER
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline())
        from gokrax import cmd_merge_summary
        args = argparse.Namespace(project="test-pj")
        posted_content = []
        def mock_post(channel_id, content):
            posted_content.append(content)
            return "msg-id-999"
        with patch("notify.post_discord", side_effect=mock_post), \
             patch("notify.send_to_agent", return_value=True):
            cmd_merge_summary(args)
        assert MERGE_SUMMARY_FOOTER.strip() in posted_content[0]

    def test_merge_summary_multi_issue_batch(self, tmp_pipelines):
        """複数Issue batch でそれぞれのIssue番号が含まれること"""
        data = _make_pipeline()
        data["batch"].append({
            "issue": 2, "title": "Add feature", "commit": "def456",
            "cc_session_id": None, "design_reviews": {}, "code_reviews": {},
            "added_at": "2025-01-01T00:00:00+09:00",
        })
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, data)
        from gokrax import cmd_merge_summary
        args = argparse.Namespace(project="test-pj")
        posted_content = []
        def mock_post(channel_id, content):
            posted_content.append(content)
            return "msg-id-999"
        with patch("notify.post_discord", side_effect=mock_post), \
             patch("notify.send_to_agent", return_value=True):
            cmd_merge_summary(args)
        content = posted_content[0]
        assert "#1" in content
        assert "#2" in content
        assert "def456" in content

    def test_merge_summary_notifies_implementer(self, tmp_pipelines):
        """merge-summary が完了時に implementer に通知すること (Issue #48)"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline())
        from gokrax import cmd_merge_summary
        args = argparse.Namespace(project="test-pj")

        with patch("notify.post_discord", return_value="1234567890"), \
             patch("notify.send_to_agent", return_value=True) as mock_send:
            cmd_merge_summary(args)

        # Verify send_to_agent was called once
        mock_send.assert_called_once()

        # Verify arguments
        call_args = mock_send.call_args
        agent_id = call_args[0][0]
        message = call_args[0][1]

        assert agent_id == "implementer1"
        assert "[gokrax] test-pj: バッチ完了" in message
        assert "上記の作業を振り返り" in message
        assert "NO_REPLY で構いません" in message
        assert "#1" in message  # Issue from batch

    def test_merge_summary_notifies_custom_implementer(self, tmp_pipelines):
        """implementer フィールドがカスタム値の場合、正しいエージェントに通知すること"""
        path = tmp_pipelines / "test-pj.json"
        data = _make_pipeline()
        data["implementer"] = "reviewer1"
        write_pipeline(path, data)
        from gokrax import cmd_merge_summary
        args = argparse.Namespace(project="test-pj")

        with patch("notify.post_discord", return_value="1234567890"), \
             patch("notify.send_to_agent", return_value=True) as mock_send:
            cmd_merge_summary(args)

        call_args = mock_send.call_args
        agent_id = call_args[0][0]
        assert agent_id == "reviewer1"

    def test_merge_summary_continues_on_send_failure(self, tmp_pipelines):
        """send_to_agent が失敗してもフローが継続すること (Issue #48)"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline())
        from gokrax import cmd_merge_summary
        args = argparse.Namespace(project="test-pj")

        with patch("notify.post_discord", return_value="1234567890"), \
             patch("notify.send_to_agent", return_value=False):
            # Should not raise exception
            cmd_merge_summary(args)

        # Verify state transition completed despite send failure
        with open(path) as f:
            data = json.load(f)
        assert data["state"] == "MERGE_SUMMARY_SENT"
        assert data["summary_message_id"] == "1234567890"

    def test_merge_summary_continues_on_send_exception(self, tmp_pipelines):
        """send_to_agent が例外を投げてもフローが継続すること (Issue #48)"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline())
        from gokrax import cmd_merge_summary
        args = argparse.Namespace(project="test-pj")

        with patch("notify.post_discord", return_value="1234567890"), \
             patch("notify.send_to_agent", side_effect=RuntimeError("Network error")):
            # Should not raise exception
            cmd_merge_summary(args)

        # Verify state transition completed despite exception
        with open(path) as f:
            data = json.load(f)
        assert data["state"] == "MERGE_SUMMARY_SENT"
        assert data["summary_message_id"] == "1234567890"


class TestWatchdogCodeApprovedPostsSummary:
    """CODE_APPROVED → MERGE_SUMMARY_SENT 遷移時に watchdog がサマリーを投稿すること"""

    def test_check_transition_sets_send_merge_summary(self):
        """check_transition が send_merge_summary=True を返すこと"""
        from engine.fsm import check_transition
        batch = [{"issue": 1, "title": "Fix", "commit": "abc123",
                  "design_reviews": {"reviewer1": {"verdict": "APPROVE"}},
                  "code_reviews": {"reviewer1": {"verdict": "APPROVE"}}}]
        action = check_transition("CODE_APPROVED", batch)
        assert action.new_state == "MERGE_SUMMARY_SENT"
        assert action.send_merge_summary is True

    def test_process_posts_merge_summary(self, tmp_pipelines, monkeypatch):
        """process() が CODE_APPROVED 時に post_discord を呼んで summary_message_id を保存すること"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="CODE_APPROVED"))

        monkeypatch.setattr("watchdog.PIPELINES_DIR", tmp_pipelines)

        posted = []
        def mock_post(channel_id, content):
            posted.append(content)
            return "summary-msg-42"

        with patch("watchdog.notify_discord"), \
             patch("watchdog.notify_implementer"), \
             patch("notify.post_discord", side_effect=mock_post):
            from watchdog import process
            process(path)

        # Discord に投稿されたか
        assert len(posted) == 1
        assert "test-pj" in posted[0]
        assert "#1" in posted[0]

        # summary_message_id が保存されたか
        with open(path) as f:
            data = json.load(f)
        assert data["state"] == "MERGE_SUMMARY_SENT"
        assert data["summary_message_id"] == "summary-msg-42"


class TestWatchdogMergeSummary:

    M_ID = "1469758184456589550"
    SUMMARY_ID = "111222333444555666"

    def _make_data(self, **kwargs):
        data = _make_pipeline(state="MERGE_SUMMARY_SENT")
        data["summary_message_id"] = self.SUMMARY_ID
        data.update(kwargs)
        return data

    def test_watchdog_detects_ok_reply(self):
        """3条件（message_reference + author.id + 'ok'で始まる）一致で DONE 遷移"""
        from engine.fsm import check_transition
        msg = _make_discord_msg("reply-1", self.SUMMARY_ID, self.M_ID, "ok")
        data = self._make_data()
        with patch("notify.fetch_discord_replies", return_value=[msg]):
            action = check_transition("MERGE_SUMMARY_SENT", data["batch"], data)
        assert action.new_state == "DONE"
        # DONE遷移は自動push+close。impl_msgは不要（watchdogが直接処理する）
        assert action.send_review is False

    def test_watchdog_ignores_wrong_author(self):
        """author.id が M でなければ遷移しない"""
        from engine.fsm import check_transition
        msg = _make_discord_msg("reply-1", self.SUMMARY_ID, "9999999999", "ok")
        data = self._make_data()
        with patch("notify.fetch_discord_replies", return_value=[msg]):
            action = check_transition("MERGE_SUMMARY_SENT", data["batch"], data)
        assert action.new_state is None

    def test_watchdog_ignores_non_reply(self):
        """message_reference なしのメッセージは無視"""
        from engine.fsm import check_transition
        msg = {
            "id": "reply-1", "content": "ok",
            "author": {"id": self.M_ID},
            "message_reference": {},
        }
        data = self._make_data()
        with patch("notify.fetch_discord_replies", return_value=[msg]):
            action = check_transition("MERGE_SUMMARY_SENT", data["batch"], data)
        assert action.new_state is None

    def test_watchdog_ignores_wrong_content(self):
        """「NG」等は承認とみなさない"""
        from engine.fsm import check_transition
        msg = _make_discord_msg("reply-1", self.SUMMARY_ID, self.M_ID, "NG")
        data = self._make_data()
        with patch("notify.fetch_discord_replies", return_value=[msg]):
            action = check_transition("MERGE_SUMMARY_SENT", data["batch"], data)
        assert action.new_state is None

    def test_watchdog_accepts_variants(self):
        """「ok」「OK」「ok.」「ok、マージして」がすべて承認"""
        from engine.fsm import check_transition
        for content in ["ok", "OK", "ok.", "ok、マージして"]:
            msg = _make_discord_msg("reply-1", self.SUMMARY_ID, self.M_ID, content)
            data = self._make_data()
            with patch("notify.fetch_discord_replies", return_value=[msg]):
                action = check_transition("MERGE_SUMMARY_SENT", data["batch"], data)
            assert action.new_state == "DONE", f"'{content}' should be accepted"

    def test_watchdog_no_summary_id_returns_no_action(self):
        """summary_message_id がなければ遷移しない"""
        from engine.fsm import check_transition
        data = _make_pipeline(state="MERGE_SUMMARY_SENT")
        # summary_message_id なし
        action = check_transition("MERGE_SUMMARY_SENT", data["batch"], data)
        assert action.new_state is None

    def test_watchdog_data_none_returns_no_action(self):
        """data=None のとき遷移しない（既存テストとの互換）"""
        from engine.fsm import check_transition
        action = check_transition("MERGE_SUMMARY_SENT", [], None)
        assert action.new_state is None
