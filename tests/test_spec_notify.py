"""notify.py spec mode 通知フォーマッターのテスト"""
import pytest
from notify import (
    spec_notify_review_start,
    spec_notify_review_complete,
    spec_notify_approved,
    spec_notify_approved_auto,
    spec_notify_approved_forced,
    spec_notify_stalled,
    spec_notify_review_failed,
    spec_notify_paused,
    spec_notify_revise_done,
    spec_notify_revise_commit_failed,
    spec_notify_revise_no_changes,
    spec_notify_issue_plan_done,
    spec_notify_queue_plan_done,
    spec_notify_done,
    spec_notify_failure,
)


class TestSpecNotifyBasicFormat:
    """各関数が [Spec] prefix / project名 / rev番号を含むことを確認"""

    def test_review_start(self):
        s = spec_notify_review_start("myproj", "2", 3)
        assert s.startswith("[Spec]")
        assert "myproj" in s
        assert "rev2" in s
        assert "3" in s

    def test_approved(self):
        s = spec_notify_approved("myproj", "1")
        assert "[Spec]" in s
        assert "myproj" in s
        assert "rev1" in s

    def test_approved_auto(self):
        s = spec_notify_approved_auto("myproj", "1")
        assert "[Spec]" in s
        assert "myproj" in s
        assert "rev1" in s
        assert "自動進行" in s

    def test_approved_forced(self):
        s = spec_notify_approved_forced("myproj", "3", 5)
        assert "[Spec]" in s
        assert "myproj" in s
        assert "5" in s

    def test_revise_no_changes(self):
        s = spec_notify_revise_no_changes("myproj", "1")
        assert "[Spec]" in s
        assert "myproj" in s
        assert "rev1" in s

    def test_stalled(self):
        s = spec_notify_stalled("myproj", "2", 7)
        assert "[Spec]" in s
        assert "myproj" in s
        assert "7" in s

    def test_review_failed(self):
        s = spec_notify_review_failed("myproj", "1")
        assert "[Spec]" in s
        assert "myproj" in s

    def test_paused(self):
        s = spec_notify_paused("myproj", "some reason")
        assert "[Spec]" in s
        assert "myproj" in s

    def test_revise_commit_failed(self):
        s = spec_notify_revise_commit_failed("myproj", "2")
        assert "[Spec]" in s
        assert "myproj" in s
        assert "rev2" in s

    def test_issue_plan_done(self):
        s = spec_notify_issue_plan_done("myproj", 10)
        assert "[Spec]" in s
        assert "myproj" in s
        assert "10" in s

    def test_queue_plan_done(self):
        s = spec_notify_queue_plan_done("myproj", 4)
        assert "[Spec]" in s
        assert "myproj" in s
        assert "4" in s

    def test_done(self):
        s = spec_notify_done("myproj")
        assert "[Spec]" in s
        assert "myproj" in s


class TestSpecNotifyReviewComplete:
    """spec_notify_review_complete: C/M/m/s カウントが正しく埋め込まれること"""

    def test_counts_embedded(self):
        s = spec_notify_review_complete("proj", "1", critical=2, major=3, minor=5, suggestion=8)
        assert "C:2" in s
        assert "M:3" in s
        assert "m:5" in s
        assert "s:8" in s
        assert "proj" in s
        assert "rev1" in s

    def test_zero_counts(self):
        s = spec_notify_review_complete("proj", "2", 0, 0, 0, 0)
        assert "C:0" in s
        assert "M:0" in s
        assert "m:0" in s
        assert "s:0" in s


class TestSpecNotifyForcedAndStalled:
    """残存件数が正しいこと"""

    def test_approved_forced_remaining(self):
        s = spec_notify_approved_forced("proj", "1", 3)
        assert "3" in s
        assert "P1" in s

    def test_stalled_remaining(self):
        s = spec_notify_stalled("proj", "2", 12)
        assert "12" in s
        assert "P1" in s


class TestSpecNotifyReviseDone:
    """commit hash が7文字に切り詰められること"""

    def test_commit_truncated_to_7(self):
        commit = "abcdef1234567890"
        s = spec_notify_revise_done("proj", "1", commit)
        assert "abcdef1" in s
        assert "234567890" not in s

    def test_commit_short(self):
        # 7文字未満でも例外が出ないこと
        s = spec_notify_revise_done("proj", "1", "abc")
        assert "abc" in s

    def test_commit_exactly_7(self):
        s = spec_notify_revise_done("proj", "1", "abcdef1")
        assert "abcdef1" in s

    def test_project_and_rev_present(self):
        s = spec_notify_revise_done("myproj", "3", "a1b2c3d4e5f6")
        assert "myproj" in s
        assert "rev3" in s


class TestSpecNotifyPaused:
    """reason が末尾に付くこと"""

    def test_reason_in_output(self):
        s = spec_notify_paused("proj", "パース失敗")
        assert "パース失敗" in s

    def test_reason_after_separator(self):
        s = spec_notify_paused("proj", "タイムアウト")
        assert "—" in s or "—" in s  # em dash or CJK dash
        assert "タイムアウト" in s

    def test_empty_reason(self):
        s = spec_notify_paused("proj", "")
        assert "[Spec]" in s
        assert "proj" in s


class TestSpecNotifyFailure:
    """spec_notify_failure の detail/no-detail 挙動"""

    def test_with_detail(self):
        s = spec_notify_failure("proj", "送信失敗", "agent=foo")
        assert "送信失敗" in s
        assert "agent=foo" in s
        assert "—" in s

    def test_without_detail(self):
        s = spec_notify_failure("proj", "送信失敗")
        assert "送信失敗" in s
        # detail が空の場合は " — " が付かないこと
        assert "— " not in s

    def test_empty_detail_no_separator(self):
        s = spec_notify_failure("proj", "エラー", "")
        assert "—" not in s

    def test_project_and_kind_present(self):
        s = spec_notify_failure("myproj", "git push失敗", "branch=main")
        assert "myproj" in s
        assert "git push失敗" in s


class TestSpecNotifyFailureLongDetail:
    """長大な detail を渡しても例外が出ないこと（分割は呼び出し側の責務）"""

    def test_very_long_detail_no_exception(self):
        long_detail = "x" * 3000
        s = spec_notify_failure("proj", "エラー", long_detail)
        assert "エラー" in s
        assert len(s) > 2000  # 関数自体は分割しない
