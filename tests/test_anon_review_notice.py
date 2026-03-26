"""匿名レビュー注意書きが todo_header に含まれることを検証。"""

import notify
import config


class TestAnonReviewNotice:

    def test_format_review_request_includes_anon_notice(self, monkeypatch):
        """format_review_request の戻り値に匿名レビュー注意書きが含まれること。"""
        monkeypatch.setattr("notify.fetch_issue_body", lambda *a, **kw: "test body")
        monkeypatch.setattr(notify, "GOKRAX_CLI", "gokrax")
        monkeypatch.setattr(notify, "GLAB_BIN", "glab")
        monkeypatch.setattr(config, "OWNER_NAME", "TestOwner")

        batch = [{"issue": 1, "title": "test", "code_reviews": {}}]
        result = notify.format_review_request(
            project="test",
            state="CODE_REVIEW",
            batch=batch,
            gitlab="test/test",
            reviewer="reviewer1",
        )
        assert "Anonymous review" in result
        assert "your name" in result

    def test_build_npass_review_message_includes_anon_notice(self, monkeypatch):
        """_build_npass_review_message の戻り値に匿名レビュー注意書きが含まれること。"""
        monkeypatch.setattr(notify, "GOKRAX_CLI", "gokrax")
        monkeypatch.setattr(config, "OWNER_NAME", "TestOwner")

        batch = [
            {
                "issue": 1,
                "title": "test",
                "code_reviews": {
                    "reviewer1": {"verdict": "P2", "pass": 1, "target_pass": 2},
                },
            },
        ]
        result = notify._build_npass_review_message(
            project="test",
            state="CODE_REVIEW_NPASS",
            batch=batch,
            reviewer="reviewer1",
        )
        assert "Anonymous review" in result
        assert "your name" in result
