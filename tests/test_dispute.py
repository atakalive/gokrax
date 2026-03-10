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
        assert d.get("resolved_verdict") == "APPROVE"

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

    def test_wrong_phase_dispute_raises(self, tmp_pipelines):
        """design dispute が CODE_REVISE では解決されないこと"""
        dispute = {
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "理由", "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": "P0",
        }
        _make_pipeline(tmp_pipelines, state="CODE_REVISE",
                       extra_issue_fields={
                           "disputes": [dispute],
                           "code_reviews": {
                               "pascal": {"verdict": "P0", "at": "2025-01-01T00:00:00+09:00"},
                           },
                       })
        import devbar
        with pytest.raises(SystemExit, match="dispute pending"):
            devbar.cmd_review(_review_args(verdict="APPROVE"))

    def test_review_recorded_after_dispute_resolution(self, tmp_pipelines):
        """dispute accepted 時はレビューが pop されること（次 REVIEW サイクルで再レビューを強制）"""
        self._make_revise_pipeline_with_dispute(tmp_pipelines)
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="APPROVE"))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        review = data["batch"][0]["design_reviews"].get("pascal")
        assert review is None

    def test_requires_force_flag(self, tmp_pipelines):
        """--force なしでは SystemExit になり、dispute status も変わらないこと"""
        dispute = {
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "理由", "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": "P0",
        }
        _make_pipeline(tmp_pipelines, extra_issue_fields={"disputes": [dispute]})
        import devbar
        with pytest.raises(SystemExit):
            devbar.cmd_review(_review_args(verdict="APPROVE", force=False))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        # review が元の P0 のまま
        review = data["batch"][0]["design_reviews"].get("pascal", {})
        assert review.get("verdict") == "P0"
        # dispute status も pending のまま（変更されていない）
        disp = data["batch"][0].get("disputes", [{}])[0]
        assert disp.get("status") == "pending"


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


# ---------------------------------------------------------------------------
# 6. cmd_review REVISE ブロック: accepted pop / rejected keep / resolved fields
# ---------------------------------------------------------------------------

class TestCmdReviewDisputeResolutionV2:
    """変更 1a の新規テスト: pop / keep / resolved_fields"""

    def _make_revise_pipeline(self, tmp_pipelines, filed_verdict="P0"):
        dispute = {
            "reviewer": "pascal",
            "status": "pending",
            "phase": "design",
            "reason": "理由",
            "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": filed_verdict,
        }
        return _make_pipeline(tmp_pipelines, extra_issue_fields={"disputes": [dispute]})

    def test_accepted_pops_review(self, tmp_pipelines):
        """dispute accepted → design_reviews から pascal エントリが削除される"""
        self._make_revise_pipeline(tmp_pipelines)
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="APPROVE"))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        assert data["batch"][0]["design_reviews"].get("pascal") is None

    def test_rejected_keeps_review(self, tmp_pipelines):
        """dispute rejected → 新 verdict でレビューが上書きされる"""
        self._make_revise_pipeline(tmp_pipelines)
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="P0", summary="維持"))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        review = data["batch"][0]["design_reviews"].get("pascal", {})
        assert review.get("verdict") == "P0"

    def test_accepted_stores_resolved_fields(self, tmp_pipelines):
        """dispute accepted → resolved_verdict / resolved_summary が保存される"""
        self._make_revise_pipeline(tmp_pipelines)
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="APPROVE", summary="問題なし"))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        d = data["batch"][0]["disputes"][0]
        assert d.get("resolved_verdict") == "APPROVE"
        assert d.get("resolved_summary") == "問題なし"


# ---------------------------------------------------------------------------
# 7. cmd_review REVIEW 状態: pending dispute 自動解決
# ---------------------------------------------------------------------------

def _make_review_pipeline(tmp_pipelines, state="DESIGN_REVIEW", extra_issue_fields=None):
    """REVIEW 状態 + issue #1 入りのパイプラインを作成。"""
    issue = {
        "issue": 1,
        "title": "Test Issue",
        "commit": None,
        "cc_session_id": None,
        "design_reviews": {},
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
        "history": [{"from": "DESIGN_REVISE", "to": state,
                      "at": "2025-01-01T00:00:00+09:00", "actor": "watchdog"}],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    path = tmp_pipelines / "test-pj.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path


class TestCmdReviewDisputeAutoResolve:
    """変更 1b のテスト: REVIEW 状態での pending dispute 自動解決"""

    def test_review_in_review_state_resolves_pending_dispute(self, tmp_pipelines):
        """DESIGN_REVIEW で pending dispute → review 投稿後に dispute accepted + review 記録"""
        dispute = {
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "理由", "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": "P0",
        }
        _make_review_pipeline(tmp_pipelines, extra_issue_fields={"disputes": [dispute]})
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="APPROVE", force=False))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        d = data["batch"][0]["disputes"][0]
        assert d["status"] == "accepted"
        assert d.get("resolved_verdict") == "APPROVE"
        # REVIEW での投稿はレビューとして記録される（pop しない）
        review = data["batch"][0]["design_reviews"].get("pascal", {})
        assert review.get("verdict") == "APPROVE"

    def test_review_in_review_state_no_dispute(self, tmp_pipelines):
        """dispute なしの通常レビュー → 正常記録、disputes 影響なし"""
        _make_review_pipeline(tmp_pipelines)
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="APPROVE", force=False))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        review = data["batch"][0]["design_reviews"].get("pascal", {})
        assert review.get("verdict") == "APPROVE"
        assert data["batch"][0].get("disputes", []) == []

    def test_review_in_review_state_rejected_dispute(self, tmp_pipelines):
        """P0 verdict を再投稿 → dispute rejected + review 記録"""
        dispute = {
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "理由", "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": "P0",
        }
        _make_review_pipeline(tmp_pipelines, extra_issue_fields={"disputes": [dispute]})
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="P0", summary="やはり P0", force=False))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        d = data["batch"][0]["disputes"][0]
        assert d["status"] == "rejected"
        review = data["batch"][0]["design_reviews"].get("pascal", {})
        assert review.get("verdict") == "P0"

    def test_review_in_review_state_existing_review_no_force_with_pending_dispute(
        self, tmp_pipelines
    ):
        """既存レビューあり + pending dispute + force なし → 冪等性バイパスで上書き"""
        dispute = {
            "reviewer": "pascal", "status": "pending", "phase": "design",
            "reason": "理由", "filed_at": "2025-01-01T00:00:00+09:00",
            "filed_verdict": "P0",
        }
        existing_reviews = {
            "pascal": {"verdict": "P0", "at": "2025-01-01T00:00:00+09:00"},
        }
        _make_review_pipeline(
            tmp_pipelines,
            extra_issue_fields={"design_reviews": existing_reviews, "disputes": [dispute]},
        )
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="APPROVE", force=False))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        review = data["batch"][0]["design_reviews"].get("pascal", {})
        assert review.get("verdict") == "APPROVE"

    def test_review_in_review_state_existing_review_no_force_without_dispute(
        self, tmp_pipelines
    ):
        """既存レビューあり + dispute なし + force なし → 冪等性チェックで skip"""
        existing_reviews = {
            "pascal": {"verdict": "P0", "at": "2025-01-01T00:00:00+09:00"},
        }
        _make_review_pipeline(
            tmp_pipelines,
            extra_issue_fields={"design_reviews": existing_reviews},
        )
        import devbar
        with patch("devbar.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            devbar.cmd_review(_review_args(verdict="APPROVE", force=False))

        data = _read_pipeline(tmp_pipelines / "test-pj.json")
        # 上書きされず P0 のまま
        review = data["batch"][0]["design_reviews"].get("pascal", {})
        assert review.get("verdict") == "P0"


# ---------------------------------------------------------------------------
# 8. _awaiting_dispute_re_review ユニットテスト
# ---------------------------------------------------------------------------

class TestAwaitingDisputeReReview:

    def _call(self, batch, review_key="design_reviews"):
        import watchdog
        return watchdog._awaiting_dispute_re_review(batch, review_key)

    def test_no_disputes(self):
        batch = [{"issue": 1, "design_reviews": {}, "disputes": []}]
        assert self._call(batch) == []

    def test_pending_always_awaiting(self):
        batch = [{"issue": 1, "design_reviews": {}, "disputes": [
            {"reviewer": "pascal", "status": "pending", "phase": "design",
             "filed_verdict": "P0"},
        ]}]
        assert self._call(batch) == ["pascal"]

    def test_accepted_no_review(self):
        batch = [{"issue": 1, "design_reviews": {}, "disputes": [
            {"reviewer": "pascal", "status": "accepted", "phase": "design",
             "resolved_at": "2025-01-02T00:00:00+09:00"},
        ]}]
        assert self._call(batch) == ["pascal"]

    def test_accepted_old_review(self):
        batch = [{"issue": 1, "design_reviews": {
            "pascal": {"verdict": "APPROVE", "at": "2025-01-01T00:00:00+09:00"},
        }, "disputes": [
            {"reviewer": "pascal", "status": "accepted", "phase": "design",
             "resolved_at": "2025-01-02T00:00:00+09:00"},
        ]}]
        assert self._call(batch) == ["pascal"]

    def test_accepted_new_review_after_resolved_at(self):
        batch = [{"issue": 1, "design_reviews": {
            "pascal": {"verdict": "APPROVE", "at": "2025-01-03T00:00:00+09:00"},
        }, "disputes": [
            {"reviewer": "pascal", "status": "accepted", "phase": "design",
             "resolved_at": "2025-01-02T00:00:00+09:00"},
        ]}]
        assert self._call(batch) == []

    def test_rejected_not_counted(self):
        batch = [{"issue": 1, "design_reviews": {}, "disputes": [
            {"reviewer": "pascal", "status": "rejected", "phase": "design",
             "resolved_at": "2025-01-02T00:00:00+09:00"},
        ]}]
        assert self._call(batch) == []

    def test_wrong_phase_not_counted(self):
        """design dispute を code_reviews で検査 → 空リスト"""
        batch = [{"issue": 1, "code_reviews": {}, "disputes": [
            {"reviewer": "pascal", "status": "pending", "phase": "design"},
        ]}]
        assert self._call(batch, review_key="code_reviews") == []

    def test_multiple_issues_all_scanned(self):
        """2 Issues、別レビュアーに dispute → 両方の awaiting を返す"""
        batch = [
            {"issue": 1, "design_reviews": {}, "disputes": [
                {"reviewer": "pascal", "status": "pending", "phase": "design"},
            ]},
            {"issue": 2, "design_reviews": {}, "disputes": [
                {"reviewer": "leibniz", "status": "pending", "phase": "design"},
            ]},
        ]
        assert self._call(batch) == ["leibniz", "pascal"]


# ---------------------------------------------------------------------------
# 9. check_transition での dispute 再レビュー待ち遷移ブロック
# ---------------------------------------------------------------------------

class TestCheckTransitionDisputeReReview:

    def _make_review_data(self, state="DESIGN_REVIEW", reviews=None, disputes=None,
                          entered_at=None):
        """DESIGN_REVIEW 状態のパイプラインデータを構築。"""
        import datetime as dt_mod
        if entered_at is None:
            # NUDGE_GRACE_SEC より十分前（催促対象）
            entered_at = (
                dt_mod.datetime.now(dt_mod.timezone.utc) - dt_mod.timedelta(seconds=600)
            ).isoformat()
        issue = {
            "issue": 1,
            "design_reviews": reviews or {},
            "code_reviews": {},
        }
        if disputes:
            issue["disputes"] = disputes
        return {
            "project": "test-pj",
            "state": state,
            "batch": [issue],
            "history": [{"from": "DESIGN_REVISE", "to": state, "at": entered_at,
                         "actor": "watchdog"}],
        }

    def test_pending_blocks_transition(self):
        """DESIGN_REVIEW、min_reviews 達成、pending dispute あり → 遷移しない"""
        reviews = {
            "pascal": {"verdict": "APPROVE", "at": "2025-01-03T00:00:00+09:00"},
            "leibniz": {"verdict": "APPROVE", "at": "2025-01-03T00:00:00+09:00"},
        }
        disputes = [
            {"reviewer": "pascal", "status": "pending", "phase": "design",
             "filed_verdict": "P0"},
        ]
        data = self._make_review_data(reviews=reviews, disputes=disputes)
        import watchdog
        action = watchdog.check_transition("DESIGN_REVIEW", data["batch"], data)
        assert action.new_state is None

    def test_accepted_awaiting_blocks_transition(self):
        """min_reviews 達成、accepted + review なし → 遷移しない"""
        reviews = {
            "leibniz": {"verdict": "APPROVE", "at": "2025-01-03T00:00:00+09:00"},
        }
        disputes = [
            {"reviewer": "pascal", "status": "accepted", "phase": "design",
             "resolved_at": "2025-01-02T00:00:00+09:00"},
        ]
        data = self._make_review_data(reviews=reviews, disputes=disputes)
        import watchdog
        action = watchdog.check_transition("DESIGN_REVIEW", data["batch"], data)
        assert action.new_state is None

    def test_re_reviewed_allows_transition(self):
        """accepted + resolved_at より後に re-review 済み → DESIGN_APPROVED に遷移"""
        reviews = {
            "pascal": {"verdict": "APPROVE", "at": "2025-01-03T00:00:00+09:00"},
            "leibniz": {"verdict": "APPROVE", "at": "2025-01-03T00:00:00+09:00"},
            "dijkstra": {"verdict": "APPROVE", "at": "2025-01-03T00:00:00+09:00"},
        }
        disputes = [
            {"reviewer": "pascal", "status": "accepted", "phase": "design",
             "resolved_at": "2025-01-02T00:00:00+09:00"},
        ]
        data = self._make_review_data(reviews=reviews, disputes=disputes)
        import watchdog
        action = watchdog.check_transition("DESIGN_REVIEW", data["batch"], data)
        assert action.new_state == "DESIGN_APPROVED"

    def test_dispute_awaiting_nudge_respects_grace_period(self):
        """grace 期間内は催促なし、grace 後は dispute_awaiting が nudge_reviewers に含まれる"""
        import datetime as dt_mod
        from config import NUDGE_GRACE_SEC

        reviews = {}
        disputes = [
            {"reviewer": "pascal", "status": "pending", "phase": "design",
             "filed_verdict": "P0"},
        ]

        # grace 期間内
        recent_at = (
            dt_mod.datetime.now(dt_mod.timezone.utc) - dt_mod.timedelta(seconds=10)
        ).isoformat()
        data_grace = self._make_review_data(reviews=reviews, disputes=disputes,
                                            entered_at=recent_at)
        import watchdog
        action_grace = watchdog.check_transition("DESIGN_REVIEW", data_grace["batch"],
                                                 data_grace)
        assert action_grace.nudge_reviewers is None

        # grace 期間後
        old_at = (
            dt_mod.datetime.now(dt_mod.timezone.utc)
            - dt_mod.timedelta(seconds=NUDGE_GRACE_SEC + 60)
        ).isoformat()
        data_old = self._make_review_data(reviews=reviews, disputes=disputes,
                                          entered_at=old_at)
        action_old = watchdog.check_transition("DESIGN_REVIEW", data_old["batch"], data_old)
        assert action_old.nudge_reviewers is not None
        assert "pascal" in action_old.nudge_reviewers


class TestAwaitingDisputeExcluded:
    """excluded_reviewers が _awaiting_dispute_re_review で除外されること"""

    def _call(self, batch, review_key="design_reviews", excluded=None):
        import watchdog
        return watchdog._awaiting_dispute_re_review(batch, review_key, excluded=excluded)

    def test_excluded_reviewer_not_in_awaiting(self):
        """excluded に入ったレビュアーの pending dispute は awaiting に含まれない"""
        batch = [{
            "issue": 1,
            "design_reviews": {},
            "disputes": [
                {"reviewer": "pascal", "status": "pending", "phase": "design",
                 "filed_verdict": "P0"},
            ],
        }]
        assert self._call(batch, excluded=["pascal"]) == []

    def test_excluded_does_not_affect_others(self):
        """excluded でないレビュアーは通常通り awaiting"""
        batch = [{
            "issue": 1,
            "design_reviews": {},
            "disputes": [
                {"reviewer": "pascal", "status": "pending", "phase": "design",
                 "filed_verdict": "P0"},
                {"reviewer": "leibniz", "status": "pending", "phase": "design",
                 "filed_verdict": "P0"},
            ],
        }]
        result = self._call(batch, excluded=["pascal"])
        assert result == ["leibniz"]

    def test_excluded_accepted_not_in_awaiting(self):
        """excluded に入ったレビュアーの accepted dispute も awaiting に含まれない"""
        batch = [{
            "issue": 1,
            "design_reviews": {},
            "disputes": [
                {"reviewer": "pascal", "status": "accepted", "phase": "design",
                 "filed_verdict": "P0", "resolved_at": "2025-01-01T00:00:00+09:00"},
            ],
        }]
        assert self._call(batch, excluded=["pascal"]) == []


# ---------------------------------------------------------------------------
# 11. dispute_nudge_reviewers: 催促リストの分離 (Issue #100)
# ---------------------------------------------------------------------------

class TestDisputeNudgeReviewers:
    """check_transition が dispute_nudge_reviewers と nudge_reviewers を正しく分離すること"""

    def _make_data(self, state="DESIGN_REVIEW", reviews=None, disputes=None,
                   review_mode="min", entered_at=None, excluded=None):
        import datetime as dt_mod
        from config import NUDGE_GRACE_SEC
        if entered_at is None:
            entered_at = (
                dt_mod.datetime.now(dt_mod.timezone.utc)
                - dt_mod.timedelta(seconds=NUDGE_GRACE_SEC + 60)
            ).isoformat()
        issue = {
            "issue": 1,
            "design_reviews": reviews or {},
            "code_reviews": {},
        }
        if disputes:
            issue["disputes"] = disputes
        data = {
            "project": "test-pj",
            "state": state,
            "review_mode": review_mode,
            "batch": [issue],
            "history": [{"from": "DESIGN_REVISE", "to": state, "at": entered_at,
                         "actor": "watchdog"}],
        }
        if excluded:
            data["excluded_reviewers"] = excluded
        return data

    def test_nudge_dispute_pending_reviewer_gets_dispute_nudge(self):
        """dispute pending のレビュアーは dispute_nudge_reviewers に含まれること"""
        # review_mode="min": members=["leibniz"], min_reviews=1
        # leibniz が既にレビュー済み → count >= min_rev だが dispute が遷移をブロック
        # pending = [] (leibniz は全 Issue をレビュー済み), dispute_awaiting = ["leibniz"]
        reviews = {"leibniz": {"verdict": "P1", "at": "2025-01-01T00:00:00+09:00"}}
        disputes = [{"reviewer": "leibniz", "status": "pending", "phase": "design",
                     "filed_verdict": "P1", "reason": "理由A"}]
        data = self._make_data(reviews=reviews, disputes=disputes)
        import watchdog
        action = watchdog.check_transition("DESIGN_REVIEW", data["batch"], data)
        assert action.dispute_nudge_reviewers is not None
        assert "leibniz" in action.dispute_nudge_reviewers
        # 通常未レビューはなし
        assert action.nudge_reviewers is None

    def test_nudge_normal_reviewer_gets_normal_nudge(self):
        """dispute なしのレビュアーは nudge_reviewers に入り、dispute_nudge_reviewers は None"""
        # review_mode="min": members=["leibniz"], min_reviews=1
        # leibniz が未レビュー → pending = ["leibniz"], dispute_awaiting = []
        data = self._make_data()
        import watchdog
        action = watchdog.check_transition("DESIGN_REVIEW", data["batch"], data)
        assert action.nudge_reviewers is not None
        assert "leibniz" in action.nudge_reviewers
        assert action.dispute_nudge_reviewers is None

    def test_nudge_mixed_dispute_and_normal_different_reviewers(self):
        """dispute pending のレビュアーA と通常未レビューのレビュアーB が別々のリストに入ること"""
        # review_mode="min": members=["leibniz"], min_reviews=1
        # leibniz: 未レビュー (normal pending)
        # pascal: dispute pending (pascal は min mode のメンバーではない)
        disputes = [{"reviewer": "pascal", "status": "pending", "phase": "design",
                     "filed_verdict": "P0", "reason": "理由B"}]
        data = self._make_data(disputes=disputes)
        import watchdog
        action = watchdog.check_transition("DESIGN_REVIEW", data["batch"], data)
        assert action.nudge_reviewers is not None
        assert "leibniz" in action.nudge_reviewers
        assert "pascal" not in (action.nudge_reviewers or [])
        assert action.dispute_nudge_reviewers is not None
        assert "pascal" in action.dispute_nudge_reviewers
        assert "leibniz" not in (action.dispute_nudge_reviewers or [])

    def test_dispute_nudge_excluded_reviewer_skipped(self):
        """excluded_reviewers に含まれる dispute pending レビュアーは dispute_nudge_reviewers に入らない"""
        disputes = [{"reviewer": "pascal", "status": "pending", "phase": "design",
                     "filed_verdict": "P0"}]
        data = self._make_data(disputes=disputes, excluded=["pascal"])
        import watchdog
        action = watchdog.check_transition("DESIGN_REVIEW", data["batch"], data)
        assert not (action.dispute_nudge_reviewers and "pascal" in action.dispute_nudge_reviewers)

    def test_nudge_same_reviewer_dispute_and_normal_across_issues(self):
        """同一レビュアーが Issue A で dispute pending + Issue B で通常未レビューの場合、両リストに含まれる"""
        import datetime as dt_mod
        from config import NUDGE_GRACE_SEC
        entered_at = (
            dt_mod.datetime.now(dt_mod.timezone.utc)
            - dt_mod.timedelta(seconds=NUDGE_GRACE_SEC + 60)
        ).isoformat()
        # Issue 1: leibniz が P1 を提出、dispute pending
        issue1 = {
            "issue": 1,
            "design_reviews": {"leibniz": {"verdict": "P1", "at": "2025-01-01T00:00:00+09:00"}},
            "code_reviews": {},
            "disputes": [{"reviewer": "leibniz", "status": "pending", "phase": "design",
                          "filed_verdict": "P1", "reason": "理由C"}],
        }
        # Issue 2: leibniz は未レビュー
        issue2 = {
            "issue": 2,
            "design_reviews": {},
            "code_reviews": {},
        }
        data = {
            "project": "test-pj",
            "state": "DESIGN_REVIEW",
            "review_mode": "min",
            "batch": [issue1, issue2],
            "history": [{"from": "DESIGN_REVISE", "to": "DESIGN_REVIEW", "at": entered_at,
                         "actor": "watchdog"}],
        }
        import watchdog
        action = watchdog.check_transition("DESIGN_REVIEW", data["batch"], data)
        assert action.nudge_reviewers is not None
        assert "leibniz" in action.nudge_reviewers
        assert action.dispute_nudge_reviewers is not None
        assert "leibniz" in action.dispute_nudge_reviewers


# ---------------------------------------------------------------------------
# 12. dispute 催促メッセージ送信の統合テスト (Issue #100)
# ---------------------------------------------------------------------------

class TestDisputeNudgeIntegration:
    """process() を通した dispute 催促メッセージ送信のテスト (Issue #100)"""

    def _write_pipeline(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def _make_pipeline_data(self, batch, entered_at=None, review_mode="min"):
        import datetime as dt_mod
        from config import NUDGE_GRACE_SEC
        if entered_at is None:
            entered_at = (
                dt_mod.datetime.now(dt_mod.timezone.utc)
                - dt_mod.timedelta(seconds=NUDGE_GRACE_SEC + 60)
            ).isoformat()
        return {
            "project": "test-pj",
            "state": "DESIGN_REVIEW",
            "enabled": True,
            "review_mode": review_mode,
            "batch": batch,
            "implementer": "kaneko",
            "history": [{"from": "DESIGN_REVISE", "to": "DESIGN_REVIEW", "at": entered_at,
                         "actor": "watchdog"}],
            "created_at": "2025-01-01T00:00:00+09:00",
            "updated_at": "2025-01-01T00:00:00+09:00",
        }

    def test_watchdog_process_dispute_nudge_sends_dispute_message(
            self, tmp_path, monkeypatch):
        """dispute pending レビュアーに【異議申し立て — 回答催促】メッセージが送られること"""
        import config, pipeline_io
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)

        # leibniz が review 済み (count=1 >= min_rev=1) かつ dispute pending
        batch = [{
            "issue": 1,
            "design_reviews": {"leibniz": {"verdict": "P1", "at": "2025-01-01T00:00:00+09:00"}},
            "code_reviews": {},
            "disputes": [{"reviewer": "leibniz", "status": "pending", "phase": "design",
                          "filed_verdict": "P1", "reason": "テスト理由"}],
        }]
        data = self._make_pipeline_data(batch=batch)
        path = tmp_path / "test-pj.json"
        self._write_pipeline(path, data)

        captured = []

        def fake_update(p, cb):
            cb(data)
            return data

        from watchdog import process
        with patch("watchdog.update_pipeline", side_effect=fake_update), \
             patch("watchdog.send_to_agent_queued",
                   side_effect=lambda r, m: captured.append((r, m)) or True), \
             patch("watchdog._is_agent_inactive", return_value=True), \
             patch("watchdog.notify_discord"):
            process(path)

        assert len(captured) == 1, f"Expected 1 send, got {len(captured)}: {captured}"
        reviewer, msg = captured[0]
        assert reviewer == "leibniz"
        assert "【異議申し立て — 回答催促】" in msg
        assert "テスト理由" in msg
        assert "--force" in msg
        # 通常の [Remind] は含まれないこと
        assert "[Remind]" not in msg

    def test_watchdog_process_dispute_only_no_implementer_nudge(
            self, tmp_path, monkeypatch):
        """dispute pending レビュアーのみの場合、実装担当催促が発火しないこと"""
        import config, pipeline_io
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)

        batch = [{
            "issue": 1,
            "design_reviews": {"leibniz": {"verdict": "P1", "at": "2025-01-01T00:00:00+09:00"}},
            "code_reviews": {},
            "disputes": [{"reviewer": "leibniz", "status": "pending", "phase": "design",
                          "filed_verdict": "P1", "reason": "理由X"}],
        }]
        data = self._make_pipeline_data(batch=batch)
        path = tmp_path / "test-pj.json"
        self._write_pipeline(path, data)

        def fake_update(p, cb):
            cb(data)
            return data

        from watchdog import process
        with patch("watchdog.update_pipeline", side_effect=fake_update), \
             patch("watchdog.send_to_agent_queued", return_value=True) as mock_send, \
             patch("watchdog._is_agent_inactive", return_value=True), \
             patch("watchdog.notify_discord"):
            process(path)

        # send_to_agent_queued が呼ばれたこと（dispute 催促）
        assert mock_send.called
        # 実装担当への催促（kaneko）は呼ばれていないこと
        called_recipients = [call[0][0] for call in mock_send.call_args_list]
        assert "kaneko" not in called_recipients

    def test_watchdog_process_mixed_reviewer_gets_combined_message(
            self, tmp_path, monkeypatch):
        """同一レビュアーが Issue A で dispute pending + Issue B で未レビューの場合、
        1通の複合メッセージが送られること"""
        import config, pipeline_io
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_path)

        # Issue 1: leibniz が P1、dispute pending
        # Issue 2: leibniz 未レビュー (normal pending)
        batch = [
            {
                "issue": 1,
                "design_reviews": {"leibniz": {"verdict": "P1", "at": "2025-01-01T00:00:00+09:00"}},
                "code_reviews": {},
                "disputes": [{"reviewer": "leibniz", "status": "pending", "phase": "design",
                              "filed_verdict": "P1", "reason": "複合理由"}],
            },
            {
                "issue": 2,
                "design_reviews": {},
                "code_reviews": {},
            },
        ]
        data = self._make_pipeline_data(batch=batch)
        path = tmp_path / "test-pj.json"
        self._write_pipeline(path, data)

        captured = []

        def fake_update(p, cb):
            cb(data)
            return data

        from watchdog import process
        with patch("watchdog.update_pipeline", side_effect=fake_update), \
             patch("watchdog.send_to_agent_queued",
                   side_effect=lambda r, m: captured.append((r, m)) or True), \
             patch("watchdog._is_agent_inactive", return_value=True), \
             patch("watchdog.notify_discord"):
            process(path)

        # leibniz に対して1回だけ送信されること
        leibniz_calls = [(r, m) for r, m in captured if r == "leibniz"]
        assert len(leibniz_calls) == 1, \
            f"Expected 1 send to leibniz, got {len(leibniz_calls)}"
        msg = leibniz_calls[0][1]
        # 両セクションが1通に含まれること
        assert "【異議申し立て — 回答催促】" in msg
        assert "[Remind]" in msg
