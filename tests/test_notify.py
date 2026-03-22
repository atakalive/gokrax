"""tests/test_notify.py — notify.py の例外処理テスト"""

import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import config
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

    def test_cli_openclaw_not_found(self, caplog):
        """CLI パス: openclaw が PATH にない場合 False を返すこと。"""
        import notify
        with patch("notify.subprocess.run", side_effect=FileNotFoundError("openclaw")):
            with caplog.at_level(logging.ERROR, logger="gokrax.notify"):
                result = notify._gateway_chat_send_cli('{"sessionKey":"x","message":"y","idempotencyKey":"z"}', 10)
        assert result is False
        assert "not found" in caplog.text.lower()

    def test_cli_timeout(self, caplog):
        """CLI パス: タイムアウト時に False を返すこと。"""
        import notify
        with patch("notify.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
            with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
                result = notify._gateway_chat_send_cli('{"sessionKey":"x","message":"y","idempotencyKey":"z"}', 5)
        assert result is False
        assert "timed out" in caplog.text.lower()

    def test_cli_success_status_started(self):
        """CLI パス正常系: status=started で成功。"""
        import notify
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"runId":"abc","status":"started"}'
        with patch("notify.subprocess.run", return_value=mock_result):
            result = notify._gateway_chat_send_cli('{"sessionKey":"x","message":"y","idempotencyKey":"z"}', 10)
        assert result is True

    def test_cli_success_ok_true(self):
        """CLI パス正常系: ok=true で成功（レスポンス形式の差異に対応）。"""
        import notify
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"ok":true,"runId":"abc"}'
        with patch("notify.subprocess.run", return_value=mock_result):
            result = notify._gateway_chat_send_cli('{"sessionKey":"x","message":"y","idempotencyKey":"z"}', 10)
        assert result is True

    def test_cli_nonzero_exit(self, caplog):
        """CLI パス: 非ゼロ終了コードで False を返すこと。"""
        import notify
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Gateway call failed"
        with patch("notify.subprocess.run", return_value=mock_result):
            with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
                result = notify._gateway_chat_send_cli('{"sessionKey":"x","message":"y","idempotencyKey":"z"}', 10)
        assert result is False

    def test_dispatch_small_message_uses_cli(self):
        """120KB 未満のメッセージは CLI パスを使うこと。"""
        import notify
        with patch("notify._gateway_chat_send_cli", return_value=True) as mock_cli:
            result = notify._gateway_chat_send("agent:test:main", "small msg", 10)
        assert result is True
        mock_cli.assert_called_once()

    def test_dispatch_boundary_just_under_limit_uses_cli(self):
        """120KB ギリギリ未満のメッセージは CLI パスを使うこと。"""
        import notify
        # _CLI_PARAMS_LIMIT = 120_000 bytes。params JSON にはメッセージ以外のキーも入るので
        # メッセージサイズは少し小さくても params 全体で 120KB 未満に収まるケースをテスト
        msg = "x" * 100_000  # params JSON 全体は ~100KB + overhead
        with patch("notify._gateway_chat_send_cli", return_value=True) as mock_cli:
            result = notify._gateway_chat_send("agent:test:main", msg, 10)
        assert result is True
        mock_cli.assert_called_once()

    def test_newline_preserved_cli(self):
        """CLI パス: 改行を含むメッセージが params JSON で保持されること。"""
        import notify
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"status":"started"}'
        msg = "line1\nline2\nline3"
        with patch("notify.subprocess.run", return_value=mock_result) as mock_run:
            notify._gateway_chat_send_cli(
                json.dumps({"sessionKey": "x", "message": msg, "idempotencyKey": "z"}), 10
            )
        # --params に渡された JSON 内に改行が保持されていること
        params_arg = mock_run.call_args.args[0][5]  # ["openclaw","gateway","call","chat.send","--params",<here>]
        parsed = json.loads(params_arg)
        assert parsed["message"] == msg


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
                with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
                    result = notify.post_discord("123456", "test message")
        assert result is None
        assert "Discord post failed" in caplog.text

    def test_request_exception(self, caplog):
        import notify
        import requests
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.post", side_effect=requests.ConnectionError("refused")):
                with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
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

    def test_command_uses_gokrax_cli_path(self):
        """format_review_request() のコマンドが GOKRAX_CLI パスを使うこと。"""
        import notify
        import config
        batch = [self._make_batch_item(42, "Test Issue", "abc123")]
        result = notify.format_review_request(
            project="test-pj", state="DESIGN_REVIEW",
            batch=batch, gitlab="atakalive/test-pj", reviewer="pascal",
        )
        assert str(config.GOKRAX_CLI) in result
        assert "/home/ataka/.openclaw/shared/bin/gokrax" in result

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
        """生成コマンドが <GOKRAX_CLI> review ... 形式であること（python3 prefix なし）。"""
        import notify
        import config
        batch = [self._make_batch_item(5)]
        result = notify.format_review_request(
            project="proj", state="DESIGN_REVIEW",
            batch=batch, gitlab="atakalive/proj", reviewer="hanfei",
        )
        assert f"{config.GOKRAX_CLI} review" in result
        # python3 prefix がないことを確認
        assert f"python3 {config.GOKRAX_CLI} review" not in result


class TestNotifyImplementer:

    def test_known_agent_sends_with_agent_id(self):
        """notify_implementer('kaneko', ...) → send_to_agent が agentId 'kaneko' で呼ばれること。"""
        import notify
        with patch("notify.send_to_agent") as mock_send:
            notify.notify_implementer("kaneko", "test message")
        mock_send.assert_called_once()
        args = mock_send.call_args[0]
        assert args[0] == "kaneko"
        assert "test message" in args[1]

    def test_unknown_agent_logs_error_no_send(self, caplog):
        """未知のキー → logger.error が呼ばれ、send_to_agent は呼ばれないこと。"""
        import notify
        import logging
        with patch("notify.send_to_agent") as mock_send:
            with caplog.at_level(logging.ERROR, logger="gokrax.notify"):
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
                with caplog.at_level(logging.ERROR, logger="gokrax.notify"):
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
                with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
                    result = notify.fetch_discord_replies("123456", "999")
        assert result == []
        assert "Discord fetch failed" in caplog.text

    def test_request_exception_returns_empty(self, caplog):
        import notify
        import requests
        with patch.object(notify, "get_bot_token", return_value="fake-token"):
            with patch("notify.requests.get", side_effect=requests.ConnectionError("refused")):
                with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
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
            with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
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
            with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
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
            with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
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
        assert "**変更内容 (commit abc123):**" in result

    def test_fallback_on_fetch_failure(self):
        import notify
        batch = [self._make_batch_item(30, "Broken")]
        with patch("notify.fetch_issue_body", return_value=None):
            result = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch, "atakalive/proj", "pascal"
            )
        assert "glab issue show 30" in result
        assert "Issue詳細:" in result

    def test_guidance_has_scope_constraint_for_code_review(self):
        import notify
        batch = [self._make_batch_item(10, "Fix", "abc123")]
        with patch("notify.fetch_issue_body", return_value="body"):
            with patch("notify._fetch_commit_diff", return_value="diff"):
                result = notify.format_review_request(
                    "proj", "CODE_REVIEW", batch, "atakalive/proj", "pascal",
                    repo_path="/repo"
                )
        assert "スコープ制約:" in result
        assert "P0/P1 を出す場合、該当コードが今回の diff に含まれることを確認せよ" in result
        assert "前バッチで既に入った変更を現バッチの責任にしない" in result
        assert "diff 外で気づいた問題は P2（提案）として報告せよ" in result

    def test_guidance_no_scope_constraint_for_design_review(self):
        import notify
        batch = [self._make_batch_item(10, "Spec")]
        with patch("notify.fetch_issue_body", return_value="body"):
            result = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch, "atakalive/proj", "pascal"
            )
        assert "スコープ制約:" not in result


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

        # full mode has 4 reviewers (pascal, leibniz, dijkstra, euler) × 1 call = 4 calls
        assert mock_send.call_count == 4


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
        assert cmd == ["git", "-C", "/repo", "diff", "-W", "abc123..def456"]

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
        assert cmd == ["git", "-C", "/repo", "show", "-W", "abc123"]

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


# ── TestFormatReviewRequestComment (Issue #88) ───────────────────────────────

class TestFormatReviewRequestComment:
    """format_review_request() の comment 引数テスト"""

    def _make_batch(self):
        return [{
            "issue": 1, "title": "test issue", "commit": None,
            "design_reviews": {}, "code_reviews": {},
        }]

    def test_no_comment_no_mention(self):
        """comment="" → Mからの要望 が含まれない"""
        import notify
        batch = self._make_batch()
        with patch("notify.fetch_issue_body", return_value="body"):
            msg = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch, "atakalive/proj", "pascal"
            )
        assert "Mからの要望" not in msg

    def test_with_comment_included(self):
        """comment あり → Mからの要望 が含まれる"""
        import notify
        batch = self._make_batch()
        with patch("notify.fetch_issue_body", return_value="body"):
            msg = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch, "atakalive/proj", "pascal",
                comment="テスト注意事項"
            )
        assert "Mからの要望: テスト注意事項" in msg

    def test_notify_reviewers_passes_comment(self):
        """notify_reviewers が format_review_request に comment を渡すこと"""
        import notify
        batch = self._make_batch()
        with patch("notify.send_to_agent") as mock_send:
            with patch("notify.fetch_issue_body", return_value="body"):
                with patch("notify.format_review_request", return_value="msg") as mock_fmt:
                    notify.notify_reviewers(
                        "proj", "DESIGN_REVIEW", batch, "atakalive/proj",
                        comment="コメントテスト"
                    )
        for c in mock_fmt.call_args_list:
            assert c.kwargs.get("comment") == "コメントテスト"


class TestCheckSquash:
    """_check_squash のテスト（Issue #98）"""

    def _make_batch_item(self, issue_num, commit):
        return {
            "issue": issue_num, "title": "t", "commit": commit,
            "design_reviews": {}, "code_reviews": {},
        }

    def _make_rev_list_result(self, returncode, stdout):
        mock = MagicMock()
        mock.returncode = returncode
        mock.stdout = stdout
        return mock

    def test_check_squash_single_commit(self):
        """predecessor..commit が 1 → 空リスト（警告なし）"""
        import notify
        batch = [self._make_batch_item(10, "d" * 40)]
        # rev-list --topo-order: base..HEAD returns full SHA
        # rev-list --count base..def456: returns 1
        topo_result = self._make_rev_list_result(0, "d" * 40 + "\n")
        count_result = self._make_rev_list_result(0, "1\n")
        with patch("notify.subprocess.run", side_effect=[topo_result, count_result]):
            warnings = notify._check_squash(batch, "a" * 40, "/repo")
        assert warnings == []

    def test_check_squash_multi_commit(self):
        """predecessor..commit が 2 以上 → 警告リストに issue 番号を含む"""
        import notify
        batch = [self._make_batch_item(10, "d" * 40)]
        topo_result = self._make_rev_list_result(0, "d" * 40 + "\n")
        count_result = self._make_rev_list_result(0, "3\n")
        with patch("notify.subprocess.run", side_effect=[topo_result, count_result]):
            warnings = notify._check_squash(batch, "a" * 40, "/repo")
        assert len(warnings) == 1
        assert "Issue #10" in warnings[0]
        assert "Squash required" in warnings[0]

    def test_check_squash_no_base_commit(self):
        """base_commit が None → 空リスト（検証スキップ）"""
        import notify
        batch = [self._make_batch_item(10, "def456")]
        with patch("notify.subprocess.run") as mock_run:
            warnings = notify._check_squash(batch, None, "/repo")
        assert warnings == []
        mock_run.assert_not_called()

    def test_check_squash_no_repo_path(self):
        """repo_path が空 → 空リスト（検証スキップ）"""
        import notify
        batch = [self._make_batch_item(10, "d" * 40)]
        with patch("notify.subprocess.run") as mock_run:
            warnings = notify._check_squash(batch, "a" * 40, "")
        assert warnings == []
        mock_run.assert_not_called()

    def test_check_squash_git_error(self):
        """git コマンド失敗 → 空リスト（安全側: 続行）"""
        import notify
        batch = [self._make_batch_item(10, "d" * 40)]
        topo_result = self._make_rev_list_result(1, "")
        with patch("notify.subprocess.run", return_value=topo_result):
            warnings = notify._check_squash(batch, "a" * 40, "/repo")
        assert warnings == []


class TestNotifyReviewersSquash:
    """squash 検証に関する notify_reviewers テスト（Issue #98）"""

    def _make_batch_item(self, issue_num, commit=None):
        return {
            "issue": issue_num, "title": "t", "commit": commit,
            "design_reviews": {}, "code_reviews": {},
        }

    def test_notify_reviewers_warns_on_multi_commit(self, caplog):
        """_check_squash が警告を返した場合、警告ログを出しつつレビュー送信は続行されること"""
        import notify
        import logging
        batch = [self._make_batch_item(10, "d" * 40)]
        with patch("notify.send_to_agent") as mock_send:
            with patch("notify._check_squash", return_value=["Issue #10: expected 1 commit after abc123, got 2. Squash required."]):
                with patch("notify.fetch_issue_body", return_value="body"):
                    with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
                        notify.notify_reviewers(
                            "proj", "CODE_REVIEW", batch, "atakalive/proj",
                            base_commit="a" * 40, repo_path="/repo"
                        )
        # squash 警告はログに出る
        assert "Multi-commit" in caplog.text
        # レビュー送信は中止されない（#100 で変更: 警告に留める）
        assert mock_send.call_count > 0

    def test_notify_reviewers_proceeds_when_squash_ok(self):
        """_check_squash が空リストを返した場合、通常通り送信されること"""
        import notify
        batch = [self._make_batch_item(10, "d" * 40)]
        with patch("notify.send_to_agent") as mock_send:
            with patch("notify._check_squash", return_value=[]):
                with patch("notify.fetch_issue_body", return_value="body"):
                    notify.notify_reviewers(
                        "proj", "CODE_REVIEW", batch, "atakalive/proj",
                        base_commit="a" * 40, repo_path="/repo"
                    )
        assert mock_send.call_count > 0


class TestFormatReviewRequestNoDiffBaseCommit:
    """format_review_request が _fetch_commit_diff に base_commit を渡さないテスト（Issue #98）"""

    def test_format_review_request_no_base_commit_in_diff_call(self):
        """_fetch_commit_diff が base_commit なしで呼ばれること"""
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
        mock_diff.assert_called_once_with("def456", "/repo")


# ── Issue #108: dispute 理由埋め込みテスト ────────────────────────────────────

class TestFormatReviewRequestDisputeEmbedding:
    """format_review_request() の dispute 理由埋め込みテスト（Issue #108）"""

    def _make_batch(self, disputes=None, is_code=False):
        review_key = "code_reviews" if is_code else "design_reviews"
        item = {
            "issue": 1, "title": "test issue", "commit": None,
            "design_reviews": {}, "code_reviews": {},
        }
        if disputes is not None:
            item["disputes"] = disputes
        return [item]

    def test_format_review_request_with_dispute(self):
        """pending dispute があるレビュアーへのレビュー依頼に「実装者からの異議」が含まれること"""
        import notify
        disputes = [{
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "理由テキスト", "filed_verdict": "P0",
        }]
        batch = self._make_batch(disputes=disputes)
        with patch("notify.fetch_issue_body", return_value="body"):
            msg = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch, "atakalive/proj", reviewer="pascal"
            )
        assert "実装者からの異議" in msg
        assert "理由テキスト" in msg

    def test_format_review_request_no_dispute(self):
        """dispute がないレビュアーへのレビュー依頼に「実装者からの異議」が含まれないこと"""
        import notify
        batch = self._make_batch(disputes=[])
        with patch("notify.fetch_issue_body", return_value="body"):
            msg = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch, "atakalive/proj", reviewer="pascal"
            )
        assert "実装者からの異議" not in msg

    def test_format_review_request_dispute_other_reviewer(self):
        """別のレビュアーの dispute は埋め込まれないこと"""
        import notify
        disputes = [{
            "reviewer": "leibniz", "status": "pending", "phase": "design",
            "reason": "理由テキスト", "filed_verdict": "P0",
        }]
        batch = self._make_batch(disputes=disputes)
        with patch("notify.fetch_issue_body", return_value="body"):
            msg = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch, "atakalive/proj", reviewer="pascal"
            )
        assert "実装者からの異議" not in msg
        assert "理由テキスト" not in msg

    def test_format_review_request_dispute_resolved(self):
        """resolved (accepted/rejected) の dispute は埋め込まれないこと"""
        import notify
        for status in ("accepted", "rejected"):
            disputes = [{
                "reviewer": "pascal", "status": status, "phase": "design",
                "reason": "理由テキスト", "filed_verdict": "P0",
            }]
            batch = self._make_batch(disputes=disputes)
            with patch("notify.fetch_issue_body", return_value="body"):
                msg = notify.format_review_request(
                    "proj", "DESIGN_REVIEW", batch, "atakalive/proj", reviewer="pascal"
                )
            assert "実装者からの異議" not in msg, f"status={status} should not embed dispute"

    def test_format_review_request_dispute_phase_mismatch(self):
        """design phase の dispute が CODE_REVIEW のレビュー依頼に埋め込まれないこと（逆も同様）"""
        import notify
        # design dispute → code review: not embedded
        disputes_design = [{
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "理由テキスト", "filed_verdict": "P0",
        }]
        batch = self._make_batch(disputes=disputes_design)
        with patch("notify.fetch_issue_body", return_value="body"):
            with patch("notify._fetch_commit_diff", return_value="diff"):
                msg = notify.format_review_request(
                    "proj", "CODE_REVIEW", batch, "atakalive/proj", reviewer="pascal"
                )
        assert "実装者からの異議" not in msg

        # code dispute → design review: not embedded
        disputes_code = [{
            "reviewer": "pascal", "status": "pending", "phase": "code",
            "reason": "理由テキスト", "filed_verdict": "P0",
        }]
        batch2 = self._make_batch(disputes=disputes_code)
        with patch("notify.fetch_issue_body", return_value="body"):
            msg2 = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch2, "atakalive/proj", reviewer="pascal"
            )
        assert "実装者からの異議" not in msg2

    def test_format_review_request_dispute_empty_reason(self):
        """reason が空文字列の dispute は「実装者からの異議」セクションを出力しないこと"""
        import notify
        disputes = [{
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "", "filed_verdict": "P0",
        }]
        batch = self._make_batch(disputes=disputes)
        with patch("notify.fetch_issue_body", return_value="body"):
            msg = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch, "atakalive/proj", reviewer="pascal"
            )
        assert "実装者からの異議" not in msg

    def test_format_review_request_header_preserved(self):
        """dispute 埋め込み後も、メッセージ末尾の {phase}レビュー依頼 が正しいこと（dispute_phase 変数名衝突回避の回帰テスト）"""
        import notify
        disputes = [{
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "理由テキスト", "filed_verdict": "P0",
        }]
        batch = self._make_batch(disputes=disputes)
        with patch("notify.fetch_issue_body", return_value="body"):
            msg_design = notify.format_review_request(
                "proj", "DESIGN_REVIEW", batch, "atakalive/proj", reviewer="pascal"
            )
        assert "設計レビュー依頼" in msg_design
        assert "コードレビュー依頼" not in msg_design

        disputes_code = [{
            "reviewer": "pascal", "status": "pending", "phase": "code",
            "reason": "理由テキスト", "filed_verdict": "P0",
        }]
        batch_code = [{
            "issue": 1, "title": "test", "commit": "abc123",
            "design_reviews": {}, "code_reviews": {},
            "disputes": disputes_code,
        }]
        with patch("notify.fetch_issue_body", return_value="body"):
            with patch("notify._fetch_commit_diff", return_value="diff"):
                msg_code = notify.format_review_request(
                    "proj", "CODE_REVIEW", batch_code, "atakalive/proj", reviewer="pascal",
                    repo_path="/repo"
                )
        assert "コードレビュー依頼" in msg_code
        assert "設計レビュー依頼" not in msg_code


class TestCheckTransitionNoDisputeQueueing:
    """check_transition() が pending_notifications に dispute エントリを追加しないことを検証（Issue #108）"""

    def test_check_transition_no_dispute_queueing(self):
        """pending dispute があっても pending_notifications に dispute エントリが追加されないこと"""
        import watchdog
        dispute = {
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "理由テキスト", "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": "P0",
        }
        issue = {
            "issue": 1,
            "design_reviews": {
                "pascal": {"verdict": "P0", "at": "2025-01-01T00:00:00+09:00"},
            },
            "code_reviews": {},
            "disputes": [dispute],
        }
        data = {
            "project": "test-pj",
            "state": "DESIGN_REVISE",
            "batch": [issue],
        }
        watchdog.check_transition("DESIGN_REVISE", data["batch"], data)
        pn = data.get("pending_notifications", {})
        assert not any(v.get("type") == "dispute" for v in pn.values())


class TestSkillInjection:

    def test_format_review_request_includes_skills(self, tmp_path, monkeypatch):
        """format_review_request の返値が skill_block で始まること。"""
        import config
        import notify

        # ダミースキルファイル作成
        skill_file = tmp_path / "skill.md"
        skill_file.write_text("Skill content here", encoding="utf-8")

        monkeypatch.setattr(config, "SKILLS", {"test-skill": str(skill_file)})
        monkeypatch.setattr(config, "AGENT_SKILLS", {"pascal": ["test-skill"]})

        batch = [{
            "issue": 1,
            "title": "Test issue",
            "design_reviews": {},
            "code_reviews": {},
        }]

        with patch("notify.fetch_issue_body", return_value="issue body"):
            result = notify.format_review_request(
                "test-pj", "DESIGN_REVIEW", batch, "gitlab/repo",
                reviewer="pascal",
            )

        assert result.startswith("<skills>\n")
        assert "--- skill: test-skill ---" in result
        assert "Skill content here" in result

    def test_notify_implementer_includes_skills(self, tmp_path, monkeypatch):
        """notify_implementer が send_to_agent に渡すメッセージの先頭にスキルブロックが付与されること。"""
        import config
        import notify

        # ダミースキルファイル作成
        skill_file = tmp_path / "impl-skill.md"
        skill_file.write_text("Impl skill", encoding="utf-8")

        monkeypatch.setattr(config, "SKILLS", {"impl-skill": str(skill_file)})
        monkeypatch.setattr(config, "AGENT_SKILLS", {"kaneko": {"code": ["impl-skill"]}})
        monkeypatch.setattr(config, "PROJECT_SKILLS", {})

        sent_messages = []

        def mock_send(agent_id, message, timeout=30):
            sent_messages.append((agent_id, message))
            return True

        monkeypatch.setattr(notify, "send_to_agent", mock_send)

        notify.notify_implementer("kaneko", "Original message", project="test-pj", phase="code")

        assert len(sent_messages) == 1
        _, msg = sent_messages[0]
        assert msg.startswith("<skills>\n")
        assert "--- skill: impl-skill ---" in msg
        assert "Impl skill" in msg
        assert msg.endswith("\n\nOriginal message")


class TestWriteReviewFile:

    def test_writes_file_successfully(self, tmp_path, monkeypatch):
        """正常系: ファイルが作成され、内容が一致すること"""
        import notify
        monkeypatch.setattr(config, "REVIEW_FILE_DIR", tmp_path)
        monkeypatch.setattr(notify, "REVIEW_FILE_DIR", tmp_path)
        result = notify._write_review_file("proj", "pascal", "review content")
        assert result is not None
        assert result.exists()
        assert result.read_text(encoding="utf-8") == "review content"
        assert result.parent == tmp_path
        assert "proj--pascal-" in result.name  # ダブルハイフンセパレータ
        assert result.suffix == ".md"

    def test_creates_directory_if_missing(self, tmp_path, monkeypatch):
        """ディレクトリが存在しない場合に自動作成されること"""
        import notify
        new_dir = tmp_path / "subdir"
        monkeypatch.setattr(config, "REVIEW_FILE_DIR", new_dir)
        monkeypatch.setattr(notify, "REVIEW_FILE_DIR", new_dir)
        result = notify._write_review_file("proj", "euler", "content")
        assert result is not None
        assert new_dir.exists()

    def test_retries_on_failure(self, tmp_path, monkeypatch):
        """書き込み失敗時にリトライし、最終的に成功すること"""
        import notify
        monkeypatch.setattr(config, "REVIEW_FILE_DIR", tmp_path)
        monkeypatch.setattr(notify, "REVIEW_FILE_DIR", tmp_path)
        monkeypatch.setattr(config, "REVIEW_FILE_WRITE_RETRIES", 3)
        monkeypatch.setattr(notify, "REVIEW_FILE_WRITE_RETRIES", 3)
        monkeypatch.setattr(config, "REVIEW_FILE_WRITE_RETRY_DELAY", 0.01)
        monkeypatch.setattr(notify, "REVIEW_FILE_WRITE_RETRY_DELAY", 0.01)
        call_count = 0
        original_write_text = Path.write_text
        def flaky_write(self_path, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise OSError("disk full")
            original_write_text(self_path, *args, **kwargs)
        monkeypatch.setattr(Path, "write_text", flaky_write)
        result = notify._write_review_file("proj", "pascal", "content")
        assert result is not None
        assert call_count == 3

    def test_returns_none_after_all_retries_fail(self, tmp_path, monkeypatch, caplog):
        """全リトライ失敗時にNoneを返すこと"""
        import notify
        monkeypatch.setattr(config, "REVIEW_FILE_DIR", tmp_path)
        monkeypatch.setattr(notify, "REVIEW_FILE_DIR", tmp_path)
        monkeypatch.setattr(config, "REVIEW_FILE_WRITE_RETRIES", 2)
        monkeypatch.setattr(notify, "REVIEW_FILE_WRITE_RETRIES", 2)
        monkeypatch.setattr(config, "REVIEW_FILE_WRITE_RETRY_DELAY", 0.01)
        monkeypatch.setattr(notify, "REVIEW_FILE_WRITE_RETRY_DELAY", 0.01)
        monkeypatch.setattr(Path, "write_text", lambda *a, **kw: (_ for _ in ()).throw(OSError("fail")))
        with caplog.at_level(logging.ERROR, logger="gokrax.notify"):
            result = notify._write_review_file("proj", "pascal", "content")
        assert result is None

    def test_project_name_sanitized(self, tmp_path, monkeypatch):
        """プロジェクト名のスラッシュ・空白がハイフンに正規化されること"""
        import notify
        monkeypatch.setattr(config, "REVIEW_FILE_DIR", tmp_path)
        monkeypatch.setattr(notify, "REVIEW_FILE_DIR", tmp_path)
        result = notify._write_review_file("my/project name", "pascal", "content")
        assert result is not None
        assert "my-project-name--pascal-" in result.name  # ダブルハイフンセパレータ

    def test_no_prefix_collision(self, tmp_path, monkeypatch):
        """プロジェクト名がプレフィックス関係でも衝突しないこと"""
        import notify
        monkeypatch.setattr(config, "REVIEW_FILE_DIR", tmp_path)
        monkeypatch.setattr(notify, "REVIEW_FILE_DIR", tmp_path)
        result_foo = notify._write_review_file("foo", "pascal", "content1")
        result_foobar = notify._write_review_file("foo-bar", "pascal", "content2")
        assert result_foo is not None
        assert result_foobar is not None
        assert result_foo.name.startswith("foo--")
        assert result_foobar.name.startswith("foo-bar--")
        # foo のプレフィックスで foo-bar のファイルがマッチしないことを確認
        assert not result_foobar.name.startswith("foo--")


class TestBuildFileReviewMessage:

    def test_contains_read_command(self, tmp_path):
        """メッセージにReadコマンドが含まれること"""
        import notify
        file_path = tmp_path / "test.md"
        batch = [{"issue": 1, "title": "t", "design_reviews": {}, "code_reviews": {}}]
        msg = notify._build_file_review_message("proj", False, "pascal", file_path, batch, round_num=1)
        assert f"Read {file_path}" in msg

    def test_contains_review_commands_for_pending_issues(self, tmp_path):
        """未APPROVEのIssueに対するreviewコマンドが含まれること"""
        import notify
        file_path = tmp_path / "test.md"
        batch = [
            {"issue": 1, "title": "a", "design_reviews": {}, "code_reviews": {}},
            {"issue": 2, "title": "b", "design_reviews": {"pascal": {"verdict": "APPROVE"}}, "code_reviews": {}},
            {"issue": 3, "title": "c", "design_reviews": {}, "code_reviews": {}},
        ]
        msg = notify._build_file_review_message("proj", False, "pascal", file_path, batch, round_num=1)
        assert "--issue 1" in msg
        assert "--issue 2" not in msg
        assert "--issue 3" in msg

    def test_includes_skill_block(self, tmp_path, monkeypatch):
        """スキルブロックが先頭に付与されること"""
        import notify
        import config as cfg
        skill_file = tmp_path / "skill.md"
        skill_file.write_text("Skill", encoding="utf-8")
        monkeypatch.setattr(cfg, "SKILLS", {"s": str(skill_file)})
        monkeypatch.setattr(cfg, "AGENT_SKILLS", {"pascal": ["s"]})
        file_path = tmp_path / "test.md"
        batch = [{"issue": 1, "title": "t", "design_reviews": {}, "code_reviews": {}}]
        msg = notify._build_file_review_message("proj", False, "pascal", file_path, batch, round_num=None)
        assert msg.startswith("<skills>\n")

    def test_is_code_true_uses_code_reviews(self, tmp_path):
        """is_code=True の場合、code_reviews を参照すること"""
        import notify
        file_path = tmp_path / "test.md"
        batch = [
            {"issue": 1, "title": "a", "design_reviews": {}, "code_reviews": {"pascal": {"verdict": "APPROVE"}}},
            {"issue": 2, "title": "b", "design_reviews": {}, "code_reviews": {}},
        ]
        msg = notify._build_file_review_message("proj", True, "pascal", file_path, batch, round_num=1)
        assert "--issue 1" not in msg
        assert "--issue 2" in msg
        assert "コードレビュー依頼" in msg


class TestNotifyReviewersExternalization:

    def _make_batch(self):
        return [{"issue": 1, "title": "t", "commit": None, "design_reviews": {}, "code_reviews": {}}]

    def test_small_message_sends_inline(self, monkeypatch):
        """閾値未満のメッセージはインライン送信されること"""
        import notify
        monkeypatch.setattr(config, "MAX_CLI_ARG_BYTES", 999_999)
        with patch("notify.send_to_agent", return_value=True) as mock_send:
            with patch("notify.fetch_issue_body", return_value="body"):
                with patch("notify._write_review_file") as mock_write:
                    notify.notify_reviewers("proj", "DESIGN_REVIEW", self._make_batch(),
                                           "atakalive/proj", review_mode="min")
        mock_write.assert_not_called()
        mock_send.assert_called()

    def test_large_message_externalizes(self, tmp_path, monkeypatch):
        """閾値以上のメッセージがファイル外部化されること"""
        import notify
        monkeypatch.setattr(config, "MAX_CLI_ARG_BYTES", 10)
        monkeypatch.setattr(config, "REVIEW_FILE_DIR", tmp_path)
        file_path = tmp_path / "test.md"
        with patch("notify.send_to_agent", return_value=True) as mock_send:
            with patch("notify.fetch_issue_body", return_value="body"):
                with patch("notify._write_review_file", return_value=file_path) as mock_write:
                    with patch("notify._build_file_review_message", return_value="short") as mock_short:
                        notify.notify_reviewers("proj", "DESIGN_REVIEW", self._make_batch(),
                                               "atakalive/proj", review_mode="min")
        mock_write.assert_called_once()
        mock_short.assert_called_once()
        mock_send.assert_called_once()
        assert mock_send.call_args[0][1] == "short"

    def test_file_write_failure_triggers_blocked(self, monkeypatch):
        """ファイル書き出し失敗時にBLOCKED遷移が呼ばれること"""
        import notify
        monkeypatch.setattr(config, "MAX_CLI_ARG_BYTES", 10)
        with patch("notify.send_to_agent") as mock_send:
            with patch("notify.fetch_issue_body", return_value="body"):
                with patch("notify._write_review_file", return_value=None):
                    with patch("notify._trigger_blocked") as mock_blocked:
                        notify.notify_reviewers("proj", "DESIGN_REVIEW", self._make_batch(),
                                               "atakalive/proj", review_mode="min")
        mock_blocked.assert_called_once()
        mock_send.assert_not_called()


class TestCleanupReviewFiles:

    def test_removes_project_files_only(self, tmp_path, monkeypatch):
        """当該プロジェクトのファイルのみ削除されること（ダブルハイフンセパレータ）"""
        import engine.reviewer
        monkeypatch.setattr(config, "REVIEW_FILE_DIR", tmp_path)
        (tmp_path / "projA--pascal-uuid1.md").write_text("a")
        (tmp_path / "projA--euler-uuid2.md").write_text("b")
        (tmp_path / "projB--pascal-uuid3.md").write_text("c")
        engine.reviewer._cleanup_review_files("projA")
        remaining = sorted(f.name for f in tmp_path.iterdir())
        assert remaining == ["projB--pascal-uuid3.md"]

    def test_no_prefix_collision_cleanup(self, tmp_path, monkeypatch):
        """プレフィックスが部分一致するプロジェクトのファイルを誤削除しないこと"""
        import engine.reviewer
        monkeypatch.setattr(config, "REVIEW_FILE_DIR", tmp_path)
        (tmp_path / "foo--pascal-uuid1.md").write_text("a")
        (tmp_path / "foo-bar--pascal-uuid2.md").write_text("b")
        engine.reviewer._cleanup_review_files("foo")
        remaining = sorted(f.name for f in tmp_path.iterdir())
        assert remaining == ["foo-bar--pascal-uuid2.md"]

    def test_directory_not_exists(self, tmp_path, monkeypatch):
        """ディレクトリが存在しない場合にエラーにならないこと"""
        import engine.reviewer
        monkeypatch.setattr(config, "REVIEW_FILE_DIR", tmp_path / "nonexistent")
        engine.reviewer._cleanup_review_files("proj")  # no error

    def test_directory_preserved(self, tmp_path, monkeypatch):
        """ディレクトリ自体は削除されないこと"""
        import engine.reviewer
        monkeypatch.setattr(config, "REVIEW_FILE_DIR", tmp_path)
        (tmp_path / "proj--pascal-uuid.md").write_text("x")
        engine.reviewer._cleanup_review_files("proj")
        assert tmp_path.exists()
        assert tmp_path.is_dir()
