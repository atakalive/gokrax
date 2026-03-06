"""tests/test_notify.py — notify.py の例外処理テスト"""

import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from config import REVIEW_MODES

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestGetBotToken:

    def test_returns_config_token(self):
        import notify
        with patch.object(notify, "DISCORD_BOT_TOKEN", "test-token-123"):
            result = notify.get_bot_token()
        assert result == "test-token-123"

    def test_returns_none_when_empty(self):
        import notify
        with patch.object(notify, "DISCORD_BOT_TOKEN", None):
            result = notify.get_bot_token()
        assert result is None


class TestSendToAgent:

    def test_openclaw_not_found(self, caplog):
        import notify
        with patch("notify.subprocess.run", side_effect=FileNotFoundError("openclaw")):
            with caplog.at_level(logging.ERROR, logger="devbar.notify"):
                result = notify.send_to_agent("test-agent", "hello")
        assert result is False
        assert "node not found in PATH" in caplog.text

    def test_timeout(self, caplog):
        import notify
        with patch("notify.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
            with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                result = notify.send_to_agent("test-agent", "hello", timeout=1)
        assert result is False
        assert "timed out" in caplog.text


class TestPostDiscord:

    def test_no_token_returns_none(self):
        import notify
        with patch.object(notify, "get_bot_token", return_value=None):
            result = notify.post_discord("123456", "test message")
        assert result is None

    def test_4xx_response(self, caplog):
        import notify
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.post", return_value=mock_resp):
                with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                    result = notify.post_discord("123456", "test message")
        assert result is None
        assert "Discord post failed" in caplog.text

    def test_request_exception(self, caplog):
        import notify
        import requests
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.post", side_effect=requests.ConnectionError("refused")):
                with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                    result = notify.post_discord("123456", "test message")
        assert result is None
        assert "Discord post error" in caplog.text

    def test_success_returns_message_id(self):
        import notify
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "9999000111222333"}
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.post", return_value=mock_resp):
                result = notify.post_discord("123456", "test message")
        assert result == "9999000111222333"


class TestFormatReviewRequest:

    def _make_batch_item(self, issue_num, title="t", commit=None):
        return {
            "issue": issue_num, "title": title, "commit": commit,
            "design_reviews": {}, "code_reviews": {},
            "cc_session_id": None, "added_at": "",
        }

    def test_command_uses_devbar_cli_path(self):
        """format_review_request() のコマンドが DEVBAR_CLI パスを使うこと。"""
        import notify
        import config
        batch = [self._make_batch_item(42, "Test Issue", "abc123")]
        result = notify.format_review_request(
            project="test-pj", state="DESIGN_REVIEW",
            batch=batch, gitlab="atakalive/test-pj", reviewer="pascal",
        )
        assert str(config.DEVBAR_CLI) in result
        assert "/home/ataka/.openclaw/shared/bin/devbar" in result

    def test_command_contains_reviewer_name(self):
        """format_review_request() のコマンドに reviewer 名が含まれること。"""
        import notify
        batch = [self._make_batch_item(10)]
        result = notify.format_review_request(
            project="test-pj", state="CODE_REVIEW",
            batch=batch, gitlab="atakalive/test-pj", reviewer="leibniz",
        )
        assert "--reviewer leibniz" in result

    def test_command_structure(self):
        """生成コマンドが python3 <DEVBAR_CLI> review ... 形式であること。"""
        import notify
        import config
        batch = [self._make_batch_item(5)]
        result = notify.format_review_request(
            project="proj", state="DESIGN_REVIEW",
            batch=batch, gitlab="atakalive/proj", reviewer="hanfei",
        )
        assert f"python3 {config.DEVBAR_CLI} review" in result


class TestNotifyImplementer:

    def test_known_agent_sends_with_agent_id(self):
        """notify_implementer('kaneko', ...) → send_to_agent が agentId 'kaneko' で呼ばれること。"""
        import notify
        with patch("notify.send_to_agent") as mock_send:
            notify.notify_implementer("kaneko", "test message")
        mock_send.assert_called_once_with("kaneko", "test message")

    def test_unknown_agent_logs_error_no_send(self, caplog):
        """未知のキー → logger.error が呼ばれ、send_to_agent は呼ばれないこと。"""
        import notify
        import logging
        with patch("notify.send_to_agent") as mock_send:
            with caplog.at_level(logging.ERROR, logger="devbar.notify"):
                notify.notify_implementer("unknown_agent", "test message")
        mock_send.assert_not_called()
        assert "Unknown agent" in caplog.text


class TestNotifyReviewers:

    def _make_batch_item(self, issue_num):
        return {
            "issue": issue_num, "title": "t", "commit": None,
            "design_reviews": {}, "code_reviews": {},
            "cc_session_id": None, "added_at": "",
        }

    def test_each_reviewer_uses_agent_id(self):
        """notify_reviewers → 各レビュアーの agentId で send_to_agent が呼ばれること。"""
        import notify
        import config
        batch = [self._make_batch_item(1)]
        with patch("notify.send_to_agent") as mock_send:
            with patch("notify.fetch_issue_body", return_value="body"):
                notify.notify_reviewers("proj", "DESIGN_REVIEW", batch, "atakalive/proj")

        # レビュー依頼メッセージのみ（/new はwatchdog側で先行送信）
        called_agents = [c.args[0] for c in mock_send.call_args_list]
        for r in config.REVIEW_MODES["standard"]["members"]:
            assert called_agents.count(r) == 1, \
                f"{r} should be called once (review message only)"

    def test_unknown_reviewer_logs_error_and_continues(self, caplog, monkeypatch):
        """未知のレビュアーはスキップされ、既知のレビュアーには送信が継続されること。"""
        import notify
        import config
        import logging

        # REVIEW_MODES に未知のキーを混入
        test_mode = {"members": ["pascal", "unknown_reviewer"], "min_reviews": 1}
        monkeypatch.setitem(config.REVIEW_MODES, "test_mode", test_mode)
        monkeypatch.setattr(notify, "REVIEW_MODES", config.REVIEW_MODES)

        batch = [self._make_batch_item(1)]
        with patch("notify.send_to_agent") as mock_send:
            with patch("notify.fetch_issue_body", return_value="body"):
                with caplog.at_level(logging.ERROR, logger="devbar.notify"):
                    notify.notify_reviewers("proj", "CODE_REVIEW", batch, "atakalive/proj",
                                          review_mode="test_mode")

        # 既知の pascal にはレビュー依頼が送信される
        called_agents = [c.args[0] for c in mock_send.call_args_list]
        assert "pascal" in called_agents
        assert called_agents.count("pascal") == 1


class TestFetchDiscordReplies:
    """fetch_discord_replies のテスト（#18）"""

    def test_no_token_returns_empty(self):
        import notify
        with patch.object(notify, "get_bot_token", return_value=None):
            result = notify.fetch_discord_replies("123456", "999")
        assert result == []

    def test_success_returns_messages(self):
        import notify
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"id": "1001", "content": "ok", "author": {"id": "user1"}},
            {"id": "1002", "content": "lgtm", "author": {"id": "user2"}},
        ]
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.get", return_value=mock_resp):
                result = notify.fetch_discord_replies("123456", "999")
        assert len(result) == 2
        assert result[0]["id"] == "1001"

    def test_4xx_returns_empty(self, caplog):
        import notify
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.get", return_value=mock_resp):
                with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                    result = notify.fetch_discord_replies("123456", "999")
        assert result == []
        assert "Discord fetch failed" in caplog.text

    def test_request_exception_returns_empty(self, caplog):
        import notify
        import requests
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.get", side_effect=requests.ConnectionError("refused")):
                with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                    result = notify.fetch_discord_replies("123456", "999")
        assert result == []
        assert "Discord fetch error" in caplog.text

    def test_passes_after_param(self):
        """after パラメータが正しく渡されること"""
        import notify
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.get", return_value=mock_resp) as mock_get:
                notify.fetch_discord_replies("ch123", "msg456")
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["after"] == "msg456"
        assert kwargs["params"]["limit"] == 50


class TestNotifyDiscord:
    """notify_discord のテスト（#19）"""

    def test_calls_post_discord_with_channel(self):
        import notify
        import config
        with patch.object(notify, "post_discord", return_value="msg-id") as mock_post:
            notify.notify_discord("test message")
        mock_post.assert_called_once_with(config.DISCORD_CHANNEL, "test message")


class TestFetchIssueBody:
    """_fetch_issue_body のテスト（Issue #23）"""

    def test_success_returns_description(self):
        import notify
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"description": "Issue body text"})
        with patch("notify.subprocess.run", return_value=mock_result):
            result = notify.fetch_issue_body(42, "atakalive/proj")
        assert result == "Issue body text"

    def test_glab_failure_returns_none(self, caplog):
        import notify
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "issue not found"
        with patch("notify.subprocess.run", return_value=mock_result):
            with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                result = notify.fetch_issue_body(999, "atakalive/proj")
        assert result is None
        assert "glab issue show failed" in caplog.text

    def test_empty_description_returns_empty_string(self):
        import notify
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"description": ""})
        with patch("notify.subprocess.run", return_value=mock_result):
            result = notify.fetch_issue_body(10, "atakalive/proj")
        assert result == ""

    def test_timeout_returns_none(self, caplog):
        import notify
        with patch("notify.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15)):
            with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                result = notify.fetch_issue_body(5, "atakalive/proj")
        assert result is None
        assert "timed out" in caplog.text


class TestFetchCommitDiff:
    """_fetch_commit_diff のテスト（Issue #23）"""

    def test_success_returns_diff(self):
        import notify
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "diff --git a/file.py b/file.py\n+new line"
        with patch("notify.subprocess.run", return_value=mock_result):
            result = notify._fetch_commit_diff("abc123", "/repo")
        assert "diff --git" in result
        assert "+new line" in result

    def test_git_failure_returns_none(self, caplog):
        import notify
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stderr = "bad object abc123"
        with patch("notify.subprocess.run", return_value=mock_result):
            with caplog.at_level(logging.WARNING, logger="devbar.notify"):
                result = notify._fetch_commit_diff("abc123", "/repo")
        assert result is None
        assert "git diff/show failed" in caplog.text


class TestFormatReviewRequestEmbedded:
    """format_review_request の埋め込みデータテスト（Issue #23）"""

    def _make_batch_item(self, issue_num, title="t", commit=None):
        return {
            "issue": issue_num, "title": title, "commit": commit,
            "design_reviews": {}, "code_reviews": {},
        }

    def test_embeds_issue_body(self):
        import notify
        batch = [self._make_batch_item(10, "Test")]
        with patch("notify.fetch_issue_body", return_value="Issue body content"):
            result = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch, "atakalive/proj", "pascal"
            )
        assert "Issue body content" in result
        assert "**Issue本文:**" in result

    def test_embeds_commit_diff(self):
        import notify
        batch = [self._make_batch_item(20, "Fix", "abc123")]
        with patch("notify.fetch_issue_body", return_value="body"):
            with patch("notify._fetch_commit_diff", return_value="diff content"):
                result = notify.format_review_request(
                    "proj", "CODE_REVIEW", batch, "atakalive/proj", "pascal",
                    repo_path="/repo"
                )
        assert "diff content" in result
        assert "**変更内容:**" in result

    def test_fallback_on_fetch_failure(self):
        import notify
        batch = [self._make_batch_item(30, "Broken")]
        with patch("notify.fetch_issue_body", return_value=None):
            result = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch, "atakalive/proj", "pascal"
            )
        assert "glab issue show 30" in result
        assert "Issue詳細:" in result

    def test_truncation_with_marker(self, monkeypatch):
        import notify
        import config
        monkeypatch.setattr(config, "MAX_EMBED_CHARS", 100)
        monkeypatch.setattr(notify, "MAX_EMBED_CHARS", 100)

        batch = [self._make_batch_item(i, "Long") for i in range(1, 6)]
        with patch("notify.fetch_issue_body", return_value="A" * 50):
            result = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch, "atakalive/proj", "pascal"
            )
        assert "(truncated)" in result
        assert "文字数制限のため省略" in result


class TestNotifyReviewersWithMode:
    """notify_reviewers の review_mode 連携テスト（Issue #24）"""

    def test_sends_review_to_all_reviewers(self):
        """notify_reviewers はレビュー依頼メッセージのみ送信（/new は watchdog 側）"""
        import notify
        batch = [{"issue": 1, "title": "t", "commit": None, "design_reviews": {}, "code_reviews": {}}]
        with patch("notify.send_to_agent") as mock_send:
            with patch("notify.fetch_issue_body", return_value="body"):
                notify.notify_reviewers("proj", "DESIGN_REVIEW", batch, "atakalive/proj",
                                       review_mode="standard")

        # /new は送信されない（watchdog 側で先行送信済み）
        new_calls = [c for c in mock_send.call_args_list if c.args[1] == "/new"]
        assert len(new_calls) == 0
        # レビュー依頼のみ
        assert mock_send.call_count == len(REVIEW_MODES["standard"]["members"])

    def test_skip_mode_sends_no_notifications(self):
        import notify
        batch = [{"issue": 1, "title": "t", "commit": None, "design_reviews": {}, "code_reviews": {}}]
        with patch("notify.send_to_agent") as mock_send:
            notify.notify_reviewers("proj", "DESIGN_REVIEW", batch, "atakalive/proj",
                                   review_mode="skip")
        mock_send.assert_not_called()

    def test_full_mode_sends_to_four_reviewers(self):
        import notify
        batch = [{"issue": 1, "title": "t", "commit": None, "design_reviews": {}, "code_reviews": {}}]
        with patch("notify.send_to_agent") as mock_send:
            with patch("notify.fetch_issue_body", return_value="body"):
                notify.notify_reviewers("proj", "DESIGN_REVIEW", batch, "atakalive/proj",
                                       review_mode="full")

        # full mode has 3 reviewers (pascal, leibniz, dijkstra) × 1 call = 3 calls
        assert mock_send.call_count == 3


class TestPrevReviews:
    """前回レビューの引用表示テスト（Issue #35）"""

    def _make_batch_item(self, issue_num, title="t"):
        return {
            "issue": issue_num, "title": title, "commit": None,
            "design_reviews": {}, "code_reviews": {},
        }

    def test_format_review_request_with_prev_feedback(self):
        """再レビュー時に前回の指摘が引用される"""
        import notify
        batch = [self._make_batch_item(42, "Test Issue")]
        prev_reviews = {
            42: {
                "pascal": {
                    "verdict": "P0",
                    "summary": "InitAsync メソッドがありません",
                    "at": "2026-02-25T10:00:00+09:00"
                }
            }
        }

        with patch("notify.fetch_issue_body", return_value="Fix the bug"):
            msg = notify.format_review_request(
                "TestPJ", "DESIGN_REVIEW", batch, "test/repo",
                reviewer="pascal", prev_reviews=prev_reviews
            )

        # Check that previous feedback is quoted
        assert "**前回のP0指摘（あなた）:**" in msg
        assert "> InitAsync メソッドがありません" in msg

    def test_prev_feedback_multiline_quote(self):
        """複数行のフィードバックが正しく引用される"""
        import notify
        batch = [self._make_batch_item(1, "Test")]
        prev_reviews = {
            1: {
                "pascal": {
                    "verdict": "P1",
                    "summary": "Line 1\nLine 2\nLine 3"
                }
            }
        }

        with patch("notify.fetch_issue_body", return_value=""):
            msg = notify.format_review_request(
                "TestPJ", "DESIGN_REVIEW", batch, "test/repo",
                reviewer="pascal", prev_reviews=prev_reviews
            )

        assert "> Line 1\n> Line 2\n> Line 3" in msg

    def test_format_review_request_no_prev_feedback(self):
        """初回レビュー（prev_reviews=None）では引用なし"""
        import notify
        batch = [self._make_batch_item(1, "Bug")]

        with patch("notify.fetch_issue_body", return_value="Fix it"):
            msg = notify.format_review_request(
                "TestPJ", "DESIGN_REVIEW", batch, "test/repo",
                reviewer="pascal", prev_reviews=None
            )

        assert "前回の" not in msg

    def test_prev_feedback_different_reviewer(self):
        """他のレビュアーのフィードバックは表示されない"""
        import notify
        batch = [self._make_batch_item(1, "Test")]
        prev_reviews = {
            1: {
                "leibniz": {"verdict": "P0", "summary": "Bad code"}
            }
        }

        with patch("notify.fetch_issue_body", return_value=""):
            msg = notify.format_review_request(
                "TestPJ", "DESIGN_REVIEW", batch, "test/repo",
                reviewer="pascal", prev_reviews=prev_reviews
            )

        assert "前回の" not in msg
        assert "Bad code" not in msg

    def test_prev_feedback_empty_summary(self):
        """空のsummaryは引用されない"""
        import notify
        batch = [self._make_batch_item(1, "Test")]
        prev_reviews = {
            1: {"pascal": {"verdict": "P0", "summary": ""}}
        }

        with patch("notify.fetch_issue_body", return_value=""):
            msg = notify.format_review_request(
                "TestPJ", "DESIGN_REVIEW", batch, "test/repo",
                reviewer="pascal", prev_reviews=prev_reviews
            )

        assert "前回の" not in msg


class TestBaseCommitDiff:
    """base_commit を使った累積diff取得テスト（Issue #82）"""

    def test_fetch_commit_diff_with_base_commit(self):
        """base_commit 指定時に git diff base..commit が実行されること"""
        import notify
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "diff --git a/file.py b/file.py\n+cumulative change"
        with patch("notify.subprocess.run", return_value=mock_result) as mock_run:
            result = notify._fetch_commit_diff("def456", "/repo", base_commit="abc123")
        assert result == mock_result.stdout
        cmd = mock_run.call_args[0][0]
        assert cmd == ["git", "-C", "/repo", "diff", "abc123..def456"]

    def test_fetch_commit_diff_without_base_commit(self):
        """base_commit=None 時に git show が実行されること"""
        import notify
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "commit abc123\ndiff --git ..."
        with patch("notify.subprocess.run", return_value=mock_result) as mock_run:
            result = notify._fetch_commit_diff("abc123", "/repo", base_commit=None)
        assert result == mock_result.stdout
        cmd = mock_run.call_args[0][0]
        assert cmd == ["git", "-C", "/repo", "show", "abc123"]

    def test_format_review_request_passes_base_commit(self):
        """base_commit が format_review_request → _fetch_commit_diff に伝播すること"""
        import notify
        batch = [{
            "issue": 1, "title": "Fix", "commit": "def456",
            "design_reviews": {}, "code_reviews": {},
        }]
        with patch("notify.fetch_issue_body", return_value="body"):
            with patch("notify._fetch_commit_diff", return_value="diff") as mock_diff:
                notify.format_review_request(
                    "proj", "CODE_REVIEW", batch, "atakalive/proj", "pascal",
                    repo_path="/repo", base_commit="abc123"
                )
        mock_diff.assert_called_once_with("def456", "/repo", base_commit="abc123")

    def test_notify_reviewers_passes_base_commit(self):
        """notify_reviewers が format_review_request に base_commit を渡すこと"""
        import notify
        batch = [{
            "issue": 1, "title": "t", "commit": None,
            "design_reviews": {}, "code_reviews": {},
        }]
        with patch("notify.send_to_agent") as mock_send:
            with patch("notify.fetch_issue_body", return_value="body"):
                with patch("notify.format_review_request", return_value="msg") as mock_fmt:
                    notify.notify_reviewers(
                        "proj", "CODE_REVIEW", batch, "atakalive/proj",
                        base_commit="abc123"
                    )
        # format_review_request が base_commit="abc123" で呼ばれたことを検証
        for call in mock_fmt.call_args_list:
            assert call.kwargs.get("base_commit") == "abc123"
