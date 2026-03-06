"""tests/test_dispute.py — Issue #86: dispute（異議申し立て）テスト"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_pipeline(tmp_pipelines, state="DESIGN_REVISE", extra_issue_fields=None):
    """REVISE 状態 + issue #1 入りのパイプラインを作成。"""
    issue = {
        "issue": 1,
        "title": "Test Issue",
        "commit": None,
        "cc_session_id": None,
        "design_reviews": {
            "pascal": {"verdict": "P0", "at": "2025-01-01T00:00:00+09:00", "summary": "bad"},
            "leibniz": {"verdict": "APPROVE", "at": "2025-01-01T00:00:00+09:00"},
        },
        "code_reviews": {},
        "added_at": "2025-01-01T00:00:00+09:00",
    }
    if extra_issue_fields:
        issue.update(extra_issue_fields)

    data = {
        "project": "test-pj",
        "gitlab": "atakalive/test-pj",
        "state": state,
        "enabled": True,
        "implementer": "kaneko",
        "batch": [issue],
        "history": [{"from": "DESIGN_REVIEW", "to": state,
                      "at": "2025-01-01T00:00:00+09:00", "actor": "watchdog"}],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    path = tmp_pipelines / "test-pj.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path


def _read_pipeline(path: Path) -> dict:
    return json.loads(path.read_text())


def _dispute_args(**kwargs):
    defaults = dict(project="test-pj", issue=1, reviewer="pascal", reason="理由テスト")
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _review_args(**kwargs):
    defaults = dict(project="test-pj", issue=1, reviewer="pascal",
                    verdict="APPROVE", summary="", force=True)
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# 1. cmd_dispute テスト
# ---------------------------------------------------------------------------

class TestCmdDispute:

    def test_normal_creates_entry(self, tmp_pipelines):
        """正常系: DESIGN_REVISE + P0レビュアーへのdispute → disputes追加"""
        _make_pipeline(tmp_pipelines)
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_dispute(_dispute_args())

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        disputes = data["batch"][0]["disputes"]
        assert len(disputes) == 1
        d = disputes[0]
        assert d["reviewer"] == "pascal"
        assert d["status"] == "pending"
        assert d["filed_verdict"] == "P0"
        assert d["phase"] == "design"
        assert d["reason"] == "理由テスト"
        assert "filed_at" in d

    def test_error_on_non_revise_state(self, tmp_pipelines):
        """DESIGN_REVIEW 中は dispute 不可 → SystemExit"""
        _make_pipeline(tmp_pipelines, state="DESIGN_REVIEW")
        import devbar
        with pytest.raises(SystemExit):
            devbar.cmd_dispute(_dispute_args())

    def test_error_on_approve_reviewer(self, tmp_pipelines):
        """APPROVE verdict のレビュアーへの dispute → SystemExit"""
        _make_pipeline(tmp_pipelines)
        import devbar
        with pytest.raises(SystemExit):
            devbar.cmd_dispute(_dispute_args(reviewer="leibniz"))

    def test_error_on_duplicate_pending(self, tmp_pipelines):
        """同一レビュアーに pending dispute 既存 → SystemExit"""
        existing_disputes = [
            {"reviewer": "pascal", "status": "pending", "phase": "design",
             "reason": "already", "filed_at": "2025-01-01T00:00:00+09:00",
             "filed_verdict": "P0"}
        ]
        _make_pipeline(tmp_pipelines, extra_issue_fields={"disputes": existing_disputes})
        import devbar
        with pytest.raises(SystemExit):
            devbar.cmd_dispute(_dispute_args())

    def test_error_on_empty_reason(self, tmp_pipelines):
        """空 reason → SystemExit"""
        _make_pipeline(tmp_pipelines)
        import devbar
        with pytest.raises(SystemExit):
            devbar.cmd_dispute(_dispute_args(reason="   "))

    def test_after_resolved_dispute_new_pending_allowed(self, tmp_pipelines):
        """resolved 後の再 dispute → 新規 pending 追加可能"""
        existing_disputes = [
            {"reviewer": "pascal", "status": "accepted", "phase": "design",
             "reason": "old", "filed_at": "2025-01-01T00:00:00+09:00",
             "filed_verdict": "P0", "resolved_at": "2025-01-02T00:00:00+09:00"}
        ]
        _make_pipeline(tmp_pipelines, extra_issue_fields={"disputes": existing_disputes})
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_dispute(_dispute_args())

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        disputes = data["batch"][0]["disputes"]
        assert len(disputes) == 2
        assert disputes[1]["status"] == "pending"

    def test_code_revise_with_p1(self, tmp_pipelines):
        """CODE_REVISE + code_reviews P1 → dispute 成功"""
        issue_fields = {
            "code_reviews": {
                "pascal": {"verdict": "P1", "at": "2025-01-01T00:00:00+09:00", "summary": "minor"},
            }
        }
        _make_pipeline(tmp_pipelines, state="CODE_REVISE", extra_issue_fields=issue_fields)
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_dispute(_dispute_args())

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        disputes = data["batch"][0]["disputes"]
        assert len(disputes) == 1
        assert disputes[0]["filed_verdict"] == "P1"
        assert disputes[0]["phase"] == "code"

    def test_posts_gitlab_note(self, tmp_pipelines):
        """GitLab note が投稿されること"""
        _make_pipeline(tmp_pipelines)
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_dispute(_dispute_args())

        calls = mock_run.call_args_list
        glab_calls = [c for c in calls if "glab" in str(c)]
        assert len(glab_calls) >= 1


# ---------------------------------------------------------------------------
# 2. cmd_review REVISE 中受け入れテスト
# ---------------------------------------------------------------------------

class TestCmdReviewDisputeResolution:

    def _make_revise_pipeline_with_dispute(
        self, tmp_pipelines, filed_verdict="P0", dispute_status="pending"
    ):
        dispute = {
            "reviewer": "pascal",
            "status": dispute_status,
            "phase": "design",
            "reason": "理由",
            "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": filed_verdict,
        }
        if dispute_status != "pending":
            dispute["resolved_at"] = "2025-01-02T00:00:00+09:00"
        return _make_pipeline(tmp_pipelines, extra_issue_fields={"disputes": [dispute]})

    def test_accepted_when_verdict_lighter(self, tmp_pipelines):
        """dispute pending(P0) → review APPROVE → status=accepted"""
        self._make_revise_pipeline_with_dispute(tmp_pipelines)
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="APPROVE"))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        d = data["batch"][0]["disputes"][0]
        assert d["status"] == "accepted"
        assert "resolved_at" in d

    def test_rejected_when_verdict_same(self, tmp_pipelines):
        """dispute pending(P0) → review P0 → status=rejected"""
        self._make_revise_pipeline_with_dispute(tmp_pipelines)
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="P0", summary="維持"))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        d = data["batch"][0]["disputes"][0]
        assert d["status"] == "rejected"

    def test_accepted_p0_to_p1(self, tmp_pipelines):
        """dispute pending(P0) → review P1 → status=accepted（severity P0>P1）"""
        self._make_revise_pipeline_with_dispute(tmp_pipelines)
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="P1", summary="軽微に変更"))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        d = data["batch"][0]["disputes"][0]
        assert d["status"] == "accepted"

    def test_no_dispute_raises(self, tmp_pipelines):
        """dispute なし → review 拒否 (SystemExit)"""
        _make_pipeline(tmp_pipelines)  # no disputes
        import devbar
        with pytest.raises(SystemExit):
            devbar.cmd_review(_review_args())

    def test_review_recorded_after_dispute_resolution(self, tmp_pipelines):
        """dispute 解決後、review エントリが記録されること"""
        self._make_revise_pipeline_with_dispute(tmp_pipelines)
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="APPROVE"))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        review = data["batch"][0]["design_reviews"].get("pascal", {})
        assert review.get("verdict") == "APPROVE"

    def test_requires_force_flag(self, tmp_pipelines):
        """--force なしでは既存レビューがスキップされること"""
        dispute = {
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "理由", "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": "P0",
        }
        _make_pipeline(tmp_pipelines, extra_issue_fields={"disputes": [dispute]})
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            # --force=False: already reviewed by pascal → skip
            devbar.cmd_review(_review_args(verdict="APPROVE", force=False))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        # review が元の P0 のまま（スキップされた）
        review = data["batch"][0]["design_reviews"].get("pascal", {})
        assert review.get("verdict") == "P0"


# ---------------------------------------------------------------------------
# 3. watchdog dispute 通知テスト
# ---------------------------------------------------------------------------

class TestCheckTransitionDisputeNotification:

    def _make_revise_data(self, disputes=None, state="DESIGN_REVISE"):
        issue = {
            "issue": 1,
            "design_reviews": {
                "pascal": {"verdict": "P0", "at": "2025-01-01T00:00:00+09:00"},
            },
            "code_reviews": {},
        }
        if disputes is not None:
            issue["disputes"] = disputes
        return {
            "project": "test-pj",
            "state": state,
            "batch": [issue],
        }

    def test_pending_dispute_queues_notification(self):
        """pending dispute → pending_notifications に dispute エントリ追加"""
        dispute = {
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "理由", "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": "P0",
        }
        data = self._make_revise_data(disputes=[dispute])
        import watchdog
        watchdog.check_transition("DESIGN_REVISE", data["batch"], data)

        pn = data.get("pending_notifications", {})
        assert any(k.startswith("dispute_1_pascal") for k in pn)
        entry = next(v for k, v in pn.items() if k.startswith("dispute_1_pascal"))
        assert entry["type"] == "dispute"
        assert entry["reviewer"] == "pascal"
        assert entry["issue"] == 1
        assert entry["reason"] == "理由"

    def test_resolved_dispute_not_queued(self):
        """resolved dispute → pending_notifications に追加されない"""
        dispute = {
            "reviewer": "pascal", "status": "accepted", "phase": "design",
            "reason": "理由", "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": "P0",
        }
        data = self._make_revise_data(disputes=[dispute])
        import watchdog
        watchdog.check_transition("DESIGN_REVISE", data["batch"], data)

        pn = data.get("pending_notifications", {})
        assert not any(k.startswith("dispute_") for k in pn)

    def test_dispute_notification_idempotent(self):
        """同一 notif_key が既存 → 二重追加されない"""
        dispute = {
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "理由", "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": "P0",
        }
        data = self._make_revise_data(disputes=[dispute])
        import watchdog
        watchdog.check_transition("DESIGN_REVISE", data["batch"], data)
        pn_count_after_first = len(data.get("pending_notifications", {}))
        watchdog.check_transition("DESIGN_REVISE", data["batch"], data)
        pn_count_after_second = len(data.get("pending_notifications", {}))

        assert pn_count_after_first == pn_count_after_second


# ---------------------------------------------------------------------------
# 4. 状態遷移テスト
# ---------------------------------------------------------------------------

class TestDisputeStateTransition:

    def test_dispute_accepted_p0_vanishes_allows_review_transition(self, tmp_pipelines):
        """dispute accepted で P0 → APPROVE になった場合 REVIEW 遷移可能"""
        # 全 issue が revised、かつ P0 レビューが APPROVE に変わった状態
        issue = {
            "issue": 1,
            "design_reviews": {
                "pascal": {"verdict": "APPROVE", "at": "2025-01-01T00:00:00+09:00"},
                "leibniz": {"verdict": "APPROVE", "at": "2025-01-01T00:00:00+09:00"},
            },
            "code_reviews": {},
            "design_revised": True,
            "disputes": [
                {"reviewer": "pascal", "status": "accepted", "phase": "design",
                 "reason": "理由", "filed_at": "2025-01-01T00:00:00+09:00",
                 "filed_verdict": "P0", "resolved_at": "2025-01-02T00:00:00+09:00"}
            ],
        }
        data = {
            "project": "test-pj", "state": "DESIGN_REVISE", "batch": [issue],
        }
        import watchdog
        action = watchdog.check_transition("DESIGN_REVISE", data["batch"], data)
        assert action.new_state == "DESIGN_REVIEW"

    def test_dispute_rejected_with_revised_allows_review_transition(self, tmp_pipelines):
        """dispute rejected + revised フラグあり → REVIEW 遷移"""
        issue = {
            "issue": 1,
            "design_reviews": {
                "pascal": {"verdict": "P0", "at": "2025-01-01T00:00:00+09:00"},
            },
            "code_reviews": {},
            "design_revised": True,
            "disputes": [
                {"reviewer": "pascal", "status": "rejected", "phase": "design",
                 "reason": "理由", "filed_at": "2025-01-01T00:00:00+09:00",
                 "filed_verdict": "P0", "resolved_at": "2025-01-02T00:00:00+09:00"}
            ],
        }
        data = {
            "project": "test-pj", "state": "DESIGN_REVISE", "batch": [issue],
        }
        import watchdog
        action = watchdog.check_transition("DESIGN_REVISE", data["batch"], data)
        assert action.new_state == "DESIGN_REVIEW"


# ---------------------------------------------------------------------------
# 5. dispute 解決後の通知抑制テスト
# ---------------------------------------------------------------------------

class TestDisputeNotificationSuppression:

    def test_resolved_dispute_skipped_in_process(self, tmp_pipelines):
        """解決済み dispute の通知はスキップされ、pending_notifications から削除される"""
        # dispute は accepted (解決済み) だが pending_notifications にはまだ残っている
        dispute = {
            "reviewer": "pascal", "status": "accepted", "phase": "design",
            "reason": "理由", "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": "P0", "resolved_at": "2025-01-02T00:00:00+09:00",
        }
        notif_key = "dispute_1_pascal_2025-01-01T00:00:00+09:00"
        issue = {
            "issue": 1,
            "design_reviews": {
                "pascal": {"verdict": "APPROVE", "at": "2025-01-02T00:00:00+09:00"},
            },
            "code_reviews": {},
            "design_revised": True,
            "disputes": [dispute],
        }
        data = {
            "project": "test-pj",
            "gitlab": "atakalive/test-pj",
            "state": "DESIGN_REVISE",
            "enabled": True,
            "implementer": "kaneko",
            "batch": [issue],
            "history": [],
            "pending_notifications": {
                notif_key: {
                    "type": "dispute",
                    "issue": 1,
                    "reviewer": "pascal",
                    "reason": "理由",
                    "filed_at": "2025-01-01T00:00:00+09:00",
                    "queued_at": "2025-01-01T01:00:00+09:00",
                }
            },
            "created_at": "2025-01-01T00:00:00+09:00",
            "updated_at": "2025-01-01T00:00:00+09:00",
        }
        path = tmp_pipelines / "test-pj.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

        from notify import notify_dispute as real_notify_dispute
        import watchdog

        notify_called = []

        def mock_notify_dispute(*args, **kwargs):
            notify_called.append(args)
            return True

        with patch("watchdog.notify_dispute", side_effect=mock_notify_dispute):
            watchdog.process(path)

        # notify_dispute は呼ばれない（解決済みのためスキップ）
        assert len(notify_called) == 0

        # pending_notifications からキーが削除されていること
        result = json.loads(path.read_text())
        assert notif_key not in result.get("pending_notifications", {})
