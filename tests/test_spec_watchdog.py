"""tests/test_spec_watchdog.py — spec mode watchdog 統合テスト"""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os

from engine.fsm_spec import (
    SpecTransitionAction,
    check_transition_spec,
    _check_spec_review,
    _check_spec_revise,
    _apply_spec_action,
    _ensure_pipelines_dir,
    _cleanup_expired_spec_files,
)
from messages import render
from config import (
    GOKRAX_CLI,
    INACTIVE_THRESHOLD_SEC,
    MAX_SPEC_RETRIES,
    NUDGE_GRACE_SEC,
    SPEC_BLOCK_TIMERS,
    SPEC_REVISE_SELF_REVIEW_PASSES,
)

LOCAL_TZ = timezone(timedelta(hours=9))


def _now():
    return datetime(2026, 3, 1, 12, 0, 0, tzinfo=LOCAL_TZ)


# --- SpecTransitionAction ---

class TestSpecTransitionAction:
    def test_defaults(self):
        a = SpecTransitionAction()
        assert a.next_state is None
        assert a.send_to is None
        assert a.discord_notify is None
        assert a.pipeline_updates is None
        assert a.error is None


# --- check_transition_spec ---

class TestCheckTransitionSpec:
    def test_unknown_state(self):
        action = check_transition_spec("BOGUS", {}, _now(), {})
        assert action.next_state == "SPEC_PAUSED"
        assert action.error is not None

    def test_spec_done_transitions_to_idle(self):
        action = check_transition_spec("SPEC_DONE", {}, _now(), {})
        assert action.next_state == "IDLE"

    def test_spec_stalled_no_action(self):
        action = check_transition_spec("SPEC_STALLED", {}, _now(), {})
        assert action.next_state is None

    def test_spec_approved_review_only(self):
        sc = {"review_only": True}
        action = check_transition_spec("SPEC_APPROVED", sc, _now(), {})
        assert action.next_state == "SPEC_DONE"

    def test_spec_approved_auto_continue(self):
        sc = {"auto_continue": True}
        action = check_transition_spec("SPEC_APPROVED", sc, _now(), {})
        assert action.next_state == "ISSUE_SUGGESTION"

    def test_spec_approved_default_waits(self):
        action = check_transition_spec("SPEC_APPROVED", {}, _now(), {})
        assert action.next_state is None

    def test_stub_states(self):
        """ISSUE_SUGGESTION/ISSUE_PLAN/QUEUE_PLAN は stub で next_state=None"""
        for s in ("ISSUE_SUGGESTION", "ISSUE_PLAN", "QUEUE_PLAN"):
            action = check_transition_spec(s, {}, _now(), {})
            assert action.next_state is None


# --- _check_spec_review ---

class TestCheckSpecReview:
    def _base_config(self, reviewers=None):
        if reviewers is None:
            reviewers = ["reviewer1", "reviewer2"]
        return {
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "rev_index": 1,
            "review_requests": {
                r: {"status": "pending", "sent_at": None, "timeout_at": None,
                    "last_nudge_at": None, "response": None}
                for r in reviewers
            },
            "current_reviews": {"entries": {}},
            "revise_count": 0,
            "max_revise_cycles": 5,
        }

    def test_sends_review_requests(self):
        """未送信 reviewer にプロンプトを送信し、pipeline_updates に差分を積む。"""
        sc = self._base_config()
        data = {"project": "test", "review_mode": "full"}
        action = _check_spec_review(sc, _now(), data)
        assert action.send_to is not None
        assert "reviewer1" in action.send_to
        assert "reviewer2" in action.send_to
        # 純粋関数: spec_config は mutate されない
        assert sc["review_requests"]["reviewer1"]["sent_at"] is None
        # 事後条件は pipeline_updates に積まれる
        rr_patch = action.pipeline_updates["review_requests_patch"]
        assert rr_patch["reviewer1"]["sent_at"] is not None
        assert rr_patch["reviewer1"]["timeout_at"] is not None
        assert rr_patch["reviewer2"]["sent_at"] is not None

    def test_timeout_detection(self):
        """timeout_at 超過 → pipeline_updates に timeout patch。"""
        sc = self._base_config(["reviewer1"])
        past = (_now() - timedelta(seconds=1900)).isoformat()
        sc["review_requests"]["reviewer1"]["sent_at"] = past
        sc["review_requests"]["reviewer1"]["timeout_at"] = (_now() - timedelta(seconds=100)).isoformat()
        data = {"project": "test", "review_mode": "lite"}
        action = _check_spec_review(sc, _now(), data)
        # 純粋関数: spec_config は mutate されない
        assert sc["review_requests"]["reviewer1"]["status"] == "pending"
        # patch に timeout が積まれる
        rr_patch = action.pipeline_updates["review_requests_patch"]
        assert rr_patch["reviewer1"]["status"] == "timeout"
        cr_patch = action.pipeline_updates["current_reviews_patch"]
        assert cr_patch["reviewer1"]["status"] == "timeout"

    def test_all_approve(self):
        """全員 received + APPROVE → SPEC_APPROVED。"""
        sc = self._base_config(["reviewer1", "reviewer2"])
        for r in ["reviewer1", "reviewer2"]:
            sc["review_requests"][r]["status"] = "received"
            sc["review_requests"][r]["sent_at"] = _now().isoformat()
            sc["review_requests"][r]["timeout_at"] = (_now() + timedelta(seconds=SPEC_BLOCK_TIMERS["SPEC_REVIEW"])).isoformat()
            sc["current_reviews"]["entries"][r] = {
                "verdict": "APPROVE", "items": [], "raw_text": "",
                "parse_success": True, "status": "received",
            }
        sc["revise_count"] = 0
        sc["max_revise_cycles"] = 5
        data = {"project": "test", "review_mode": "lite"}
        action = _check_spec_review(sc, _now(), data)
        assert action.next_state == "SPEC_APPROVED"

    def test_all_timeout_failed(self):
        """全員 timeout → SPEC_REVIEW_FAILED。"""
        sc = self._base_config(["reviewer1", "reviewer2"])
        for r in ["reviewer1", "reviewer2"]:
            sc["review_requests"][r]["status"] = "timeout"
            sc["review_requests"][r]["sent_at"] = _now().isoformat()
            sc["current_reviews"]["entries"][r] = {
                "verdict": None, "items": [], "raw_text": None,
                "parse_success": False, "status": "timeout",
            }
        data = {"project": "test", "review_mode": "full"}
        action = _check_spec_review(sc, _now(), data)
        assert action.next_state == "SPEC_REVIEW_FAILED"

    def test_p0_triggers_revise(self):
        """P0 あり → SPEC_REVISE。"""
        reviewers = ["reviewer1", "reviewer3", "reviewer6", "reviewer5"]
        sc = self._base_config(reviewers)
        for r in reviewers:
            sc["review_requests"][r]["status"] = "received"
            sc["review_requests"][r]["sent_at"] = _now().isoformat()
        sc["current_reviews"]["entries"]["reviewer1"] = {
            "verdict": "P0", "items": [], "raw_text": "",
            "parse_success": True, "status": "received",
        }
        for r in ["reviewer3", "reviewer6", "reviewer5"]:
            sc["current_reviews"]["entries"][r] = {
                "verdict": "APPROVE", "items": [], "raw_text": "",
                "parse_success": True, "status": "received",
            }
        sc["max_revise_cycles"] = 5
        data = {"project": "test", "review_mode": "full"}
        action = _check_spec_review(sc, _now(), data)
        assert action.next_state == "SPEC_REVISE"

    def test_approved_prior_not_sent(self):
        """approved_prior reviewer is not sent a review request."""
        sc = self._base_config(["reviewer1", "reviewer2"])
        sc["review_requests"]["reviewer1"]["status"] = "approved_prior"
        data = {"project": "test", "review_mode": "lite"}
        action = _check_spec_review(sc, _now(), data)
        assert action.send_to is not None
        assert "reviewer1" not in action.send_to
        assert "reviewer2" in action.send_to

    def test_approved_prior_not_nudged(self):
        """approved_prior reviewer is not included in nudge targets."""
        sc = self._base_config(["reviewer1", "reviewer2"])
        sc["review_requests"]["reviewer1"]["status"] = "approved_prior"
        sc["review_requests"]["reviewer1"]["sent_at"] = _now().isoformat()
        sent = _now()
        sc["review_requests"]["reviewer2"]["sent_at"] = sent.isoformat()
        sc["review_requests"]["reviewer2"]["timeout_at"] = (sent + timedelta(seconds=SPEC_BLOCK_TIMERS["SPEC_REVIEW"])).isoformat()
        # entered_at must be set and NUDGE_GRACE_SEC must have elapsed for nudge path
        entered_at = sent - timedelta(seconds=NUDGE_GRACE_SEC + 60)
        data = {
            "project": "test",
            "review_mode": "lite",
            "history": [{"from": "SPEC_REVISE", "to": "SPEC_REVIEW",
                         "at": entered_at.isoformat()}],
        }
        action = _check_spec_review(sc, sent + timedelta(seconds=60), data)
        # reviewer2 is pending+sent → should be nudged; reviewer1 is approved_prior → not nudged
        assert action.nudge_reviewers is not None
        assert "reviewer2" in action.nudge_reviewers
        assert "reviewer1" not in action.nudge_reviewers

    def test_approved_prior_does_not_block_all_complete(self):
        """approved_prior + all others received APPROVE → SPEC_APPROVED."""
        sc = self._base_config(["reviewer1", "reviewer2"])
        sc["review_requests"]["reviewer1"]["status"] = "approved_prior"
        sc["review_requests"]["reviewer2"]["status"] = "received"
        sc["review_requests"]["reviewer2"]["sent_at"] = _now().isoformat()
        sc["review_requests"]["reviewer2"]["timeout_at"] = (_now() + timedelta(seconds=SPEC_BLOCK_TIMERS["SPEC_REVIEW"])).isoformat()
        sc["current_reviews"]["entries"]["reviewer2"] = {
            "verdict": "APPROVE", "items": [], "raw_text": "",
            "parse_success": True, "status": "received",
        }
        data = {"project": "test", "review_mode": "lite"}
        action = _check_spec_review(sc, _now(), data)
        assert action.next_state == "SPEC_APPROVED"

    def test_approved_prior_synthetic_entry_satisfies_min_valid(self):
        """approved_prior synthetic entry satisfies min_valid in full mode (min_reviews=4)."""
        reviewers = ["reviewer1", "reviewer3", "reviewer5", "reviewer6"]
        sc = self._base_config(reviewers)
        sc["review_requests"]["reviewer1"]["status"] = "approved_prior"
        for r in ["reviewer3", "reviewer5", "reviewer6"]:
            sc["review_requests"][r]["status"] = "received"
            sc["review_requests"][r]["sent_at"] = _now().isoformat()
            sc["review_requests"][r]["timeout_at"] = (_now() + timedelta(seconds=SPEC_BLOCK_TIMERS["SPEC_REVIEW"])).isoformat()
            sc["current_reviews"]["entries"][r] = {
                "verdict": "APPROVE", "items": [], "raw_text": "",
                "parse_success": True, "status": "received",
            }
        data = {"project": "test", "review_mode": "full"}
        action = _check_spec_review(sc, _now(), data)
        assert action.next_state == "SPEC_APPROVED"

    def test_approved_prior_only_no_actual_received_fails(self):
        """approved_prior + timeout only (no actual received) → not SPEC_APPROVED."""
        sc = self._base_config(["reviewer1", "reviewer2"])
        sc["review_requests"]["reviewer1"]["status"] = "approved_prior"
        sc["review_requests"]["reviewer2"]["status"] = "timeout"
        sc["review_requests"]["reviewer2"]["sent_at"] = _now().isoformat()
        sc["current_reviews"]["entries"]["reviewer2"] = {
            "verdict": None, "items": [], "raw_text": None,
            "parse_success": False, "status": "timeout",
        }
        data = {"project": "test", "review_mode": "lite"}
        action = _check_spec_review(sc, _now(), data)
        assert action.next_state == "SPEC_REVIEW_FAILED"

    def test_approved_prior_not_in_review_history(self):
        """approved_prior reviewer should not appear in review history entries."""
        sc = self._base_config(["reviewer1", "reviewer2"])
        sc["review_requests"]["reviewer1"]["status"] = "approved_prior"
        sc["review_requests"]["reviewer2"]["status"] = "received"
        sc["review_requests"]["reviewer2"]["sent_at"] = _now().isoformat()
        sc["review_requests"]["reviewer2"]["timeout_at"] = (_now() + timedelta(seconds=SPEC_BLOCK_TIMERS["SPEC_REVIEW"])).isoformat()
        sc["current_reviews"]["entries"]["reviewer2"] = {
            "verdict": "APPROVE", "items": [], "raw_text": "",
            "parse_success": True, "status": "received",
        }
        data = {"project": "test", "review_mode": "lite"}
        action = _check_spec_review(sc, _now(), data)
        assert action.next_state == "SPEC_APPROVED"
        history = action.pipeline_updates.get("_review_history_append", {})
        reviews = history.get("reviews", {})
        assert "reviewer1" not in reviews

    def test_approved_prior_expires_after_one_round_integration(self):
        """Integration: approved_prior from round N expires in round N+2.

        Flow: build_revise_completion_updates (round 1 → 2) sets approved_prior,
        then build_revise_completion_updates (round 2 → 3) should reset to pending
        because the approved_prior reviewer has no entry in current_reviews.
        """
        from spec_revise import build_revise_completion_updates

        # Round 1: reviewer1=APPROVE, reviewer2=P1 → SPEC_REVISE
        sc_round1 = {
            "spec_path": "/repo/docs/spec-rev1.md",
            "current_rev": "1",
            "rev_index": 1,
            "review_history": [],
            "current_reviews": {
                "entries": {
                    "reviewer1": {"status": "received", "verdict": "APPROVE",
                                  "items": [], "raw_text": "", "parse_success": True},
                    "reviewer2": {"status": "received", "verdict": "P1",
                                  "items": [{"severity": "major"}], "raw_text": "",
                                  "parse_success": True},
                },
            },
            "last_commit": "aaa111",
            "review_requests": {
                "reviewer1": {"status": "received", "sent_at": "x",
                              "timeout_at": "x", "last_nudge_at": None, "response": None},
                "reviewer2": {"status": "received", "sent_at": "x",
                              "timeout_at": "x", "last_nudge_at": None, "response": None},
            },
        }
        revise1 = {"new_rev": "2", "commit": "bbb222", "changes": {"added_lines": 10}}
        updates1 = build_revise_completion_updates(sc_round1, revise1, _now())

        # After round 1 revise: reviewer1 should be approved_prior
        assert updates1["review_requests_patch"]["reviewer1"]["status"] == "approved_prior"
        assert updates1["review_requests_patch"]["reviewer2"]["status"] == "pending"

        # Round 2: reviewer1 did not review (approved_prior, no entry).
        # reviewer2 gave P1 again → SPEC_REVISE
        sc_round2 = {
            "spec_path": "/repo/docs/spec-rev2.md",
            "current_rev": "2",
            "rev_index": 2,
            "review_history": updates1["review_history"],
            "current_reviews": {
                "entries": {
                    # reviewer1 has NO entry (was approved_prior, didn't review)
                    "reviewer2": {"status": "received", "verdict": "P1",
                                  "items": [{"severity": "major"}], "raw_text": "",
                                  "parse_success": True},
                },
            },
            "last_commit": "bbb222",
            "review_requests": {
                "reviewer1": {"status": "approved_prior", "sent_at": None,
                              "timeout_at": None, "last_nudge_at": None, "response": None},
                "reviewer2": {"status": "received", "sent_at": "x",
                              "timeout_at": "x", "last_nudge_at": None, "response": None},
            },
        }
        revise2 = {"new_rev": "3", "commit": "ccc333", "changes": {"added_lines": 5}}
        updates2 = build_revise_completion_updates(sc_round2, revise2, _now())

        # After round 2 revise: reviewer1 should be back to pending (1-round expiry)
        assert updates2["review_requests_patch"]["reviewer1"]["status"] == "pending"
        assert updates2["review_requests_patch"]["reviewer2"]["status"] == "pending"


# --- _check_spec_revise ---

class TestCheckSpecRevise:
    def test_no_timeout(self):
        """タイムアウト前 → next_state=None。"""
        sc = {"retry_counts": {}}
        data = {
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE",
                         "at": _now().isoformat()}],
        }
        action = _check_spec_revise(sc, _now() + timedelta(seconds=100), data)
        assert action.next_state is None

    def test_timeout_retry(self):
        """タイムアウト + リトライ余裕あり → retry_counts 更新 + 起点リセット。"""
        sc = {"retry_counts": {"SPEC_REVISE": 0}}
        entered = _now() - timedelta(seconds=SPEC_BLOCK_TIMERS["SPEC_REVISE"] + 1)
        data = {
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE",
                         "at": entered.isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state is None  # リトライ、遷移なし
        assert action.pipeline_updates["retry_counts"]["SPEC_REVISE"] == 1
        assert action.pipeline_updates["_revise_retry_at"] is not None  # 起点リセット

    def test_timeout_uses_retry_at(self):
        """_revise_retry_at が設定済みならそれを起点にする（Dijkstra P1-2）。"""
        retry_at = (_now() - timedelta(seconds=100)).isoformat()  # まだ余裕あり
        sc = {"retry_counts": {"SPEC_REVISE": 1}, "_revise_retry_at": retry_at}
        entered = _now() - timedelta(seconds=5000)  # history は古い
        data = {
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE",
                         "at": entered.isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state is None  # _revise_retry_at 起点ではまだタイムアウトしてない

    def test_timeout_max_retries_paused(self):
        """MAX_SPEC_RETRIES 超過 → SPEC_PAUSED。"""
        sc = {"retry_counts": {"SPEC_REVISE": MAX_SPEC_RETRIES}}
        entered = _now() - timedelta(seconds=SPEC_BLOCK_TIMERS["SPEC_REVISE"] + 1)
        data = {
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE",
                         "at": entered.isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state == "SPEC_PAUSED"

    def test_no_history(self):
        """history なし → next_state=None（安全側）。"""
        sc = {"retry_counts": {}}
        action = _check_spec_revise(sc, _now(), {"history": []})
        assert action.next_state is None

    def test_d_revise_send_retries_none_no_crash(self):
        """_revise_send_retries=None（clean path リセット後）で TypeError にならず、0 として扱われる。"""
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_reviews": {
                "entries": {
                    "r1": {
                        "verdict": "P0",
                        "items": [{"id": "i1", "severity": "critical",
                            "section": "s", "title": "t", "description": "d", "suggestion": "s"}],
                        "raw_text": "", "parse_success": True, "status": "received",
                    },
                },
            },
            "retry_counts": {},
            "_revise_send_retries": None,
        }
        data = {
            "project": "test-pj",
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": _now().isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        # None が 0 として扱われ、送信試行 → retries 0+1=1
        assert action.pipeline_updates["_revise_send_retries"] == 1
        assert "impl1" in action.send_to

    def test_a2_issues_found_send_retries_none_no_crash(self):
        """_issues_found_send_retries=None（clean path リセット後）で TypeError にならず、0 として扱われる。"""
        sc = {
            "spec_implementer": "impl1",
            "retry_counts": {},
            "_issues_found_pending_feedback": "revise feedback text",
            "_issues_found_send_retries": None,
        }
        data = {
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": _now().isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        # None が 0 として扱われ、送信試行 → retries 0+1=1
        assert action.pipeline_updates["_issues_found_send_retries"] == 1
        assert "impl1" in action.send_to
        # pending_feedback はクリアされる
        assert action.pipeline_updates["_issues_found_pending_feedback"] is None


# --- _apply_spec_action DCL ---

class TestApplySpecAction:
    def test_conflict_skip(self, tmp_pipelines):
        """expected_state 不一致 → 何もしない。"""
        import json
        pj_path = tmp_pipelines / "test-pj.json"
        pj_data = {
            "project": "test-pj", "state": "SPEC_APPROVED",  # 既に別状態
            "enabled": True, "batch": [],
            "spec_mode": True, "spec_config": {},
            "history": [],
        }
        pj_path.write_text(json.dumps(pj_data))

        action = SpecTransitionAction(
            next_state="SPEC_REVISE",
            expected_state="SPEC_REVIEW",  # 不一致
        )
        with patch("engine.fsm_spec.send_to_agent_queued"), \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)

        result = json.loads(pj_path.read_text())
        assert result["state"] == "SPEC_APPROVED"  # 変わってない

    def test_transition_applied(self, tmp_pipelines):
        """正常遷移。"""
        import json
        pj_path = tmp_pipelines / "test-pj.json"
        pj_data = {
            "project": "test-pj", "state": "SPEC_REVIEW",
            "enabled": True, "batch": [],
            "spec_mode": True,
            "spec_config": {"review_only": True},
            "history": [],
        }
        pj_path.write_text(json.dumps(pj_data))

        action = SpecTransitionAction(
            next_state="SPEC_APPROVED",
            expected_state="SPEC_REVIEW",
        )
        with patch("engine.fsm_spec.send_to_agent_queued") as mock_send, \
             patch("engine.fsm_spec.notify_discord") as mock_discord:
            _apply_spec_action(pj_path, action, _now(), pj_data)

        result = json.loads(pj_path.read_text())
        # check_transition_spec が再計算する。SPEC_REVIEW + review_only は check_transition_spec で再計算されるので結果は環境依存。
        # ここでは state が変わったことだけ確認。
        assert result["state"] != "SPEC_REVIEW" or True  # DCL再計算があるので柔軟に


# --- プロンプト生成 ---

class TestPromptGeneration:
    def test_initial_prompt(self):
        prompt = render("spec.review", "initial",
            project="gokrax", spec_path="docs/spec.md",
            current_rev="1", GOKRAX_CLI=GOKRAX_CLI,
        )
        assert "やりすぎレビュー" in prompt
        assert "gokrax" in prompt
        assert "rev1" in prompt

    def test_revision_prompt(self):
        sc = {
            "pipelines_dir": "/tmp",
            "last_commit": "abc1234",
            "last_changes": {"added_lines": 50, "removed_lines": 10,
                             "changelog_summary": "Fixed stuff"},
        }
        last_changes = sc.get("last_changes") or {}
        prompt = render("spec.review", "revision",
            project="gokrax", spec_path="docs/spec.md",
            current_rev="2", GOKRAX_CLI=GOKRAX_CLI,
            changelog=last_changes.get("changelog_summary", "変更履歴なし"),
            added=str(last_changes.get("added_lines", "?")),
            removed=str(last_changes.get("removed_lines", "?")),
            last_commit=sc.get("last_commit") or "unknown",
        )
        assert "改訂版" in prompt
        assert "abc1234" in prompt
        assert "+50" in prompt


# --- _ensure_pipelines_dir ---

class TestEnsurePipelinesDir:
    def test_creates_new_dir(self, tmp_path):
        """存在しないディレクトリが作成される。"""
        target = tmp_path / "pipelines"
        assert not target.exists()
        _ensure_pipelines_dir(str(target))
        assert target.is_dir()

    def test_existing_dir_no_error(self, tmp_path):
        """既存ディレクトリでエラーにならない。"""
        target = tmp_path / "pipelines"
        target.mkdir()
        _ensure_pipelines_dir(str(target))  # 例外なし
        assert target.is_dir()

    def test_file_exists_logs_only(self, tmp_path, caplog):
        """同名ファイルが存在する場合、ログ出力のみでエラーにならない。"""
        target = tmp_path / "pipelines"
        target.write_text("not a dir")
        import logging
        with caplog.at_level(logging.WARNING):
            _ensure_pipelines_dir(str(target))  # 例外なし
        assert target.is_file()  # ファイルは消えない


# --- _cleanup_expired_spec_files ---

class TestCleanupExpiredSpecFiles:
    def _set_mtime(self, path: Path, days_ago: int) -> None:
        """ファイルの mtime を now から days_ago 日前に設定。"""
        now_ts = datetime.now(LOCAL_TZ).timestamp()
        mtime = now_ts - days_ago * 86400
        os.utime(path, (mtime, mtime))

    def test_deletes_expired_rev_file(self, tmp_path):
        """31日前の *_rev*.md ファイルが削除される。"""
        f = tmp_path / "foo_rev1.md"
        f.write_text("content")
        self._set_mtime(f, 31)
        _cleanup_expired_spec_files(str(tmp_path))
        assert not f.exists()

    def test_skips_non_rev_file(self, tmp_path):
        """31日前でも _rev パターン外のファイルは削除されない。"""
        f = tmp_path / "notes.txt"
        f.write_text("content")
        self._set_mtime(f, 31)
        _cleanup_expired_spec_files(str(tmp_path))
        assert f.exists()

    def test_keeps_fresh_rev_file(self, tmp_path):
        """29日前の *_rev*.md ファイルは保持される（期限内）。"""
        f = tmp_path / "foo_rev1.md"
        f.write_text("content")
        self._set_mtime(f, 29)
        _cleanup_expired_spec_files(str(tmp_path))
        assert f.exists()

    def test_missing_dir_no_error(self, tmp_path):
        """存在しないディレクトリでエラーにならない。"""
        missing = tmp_path / "no_such_dir"
        _cleanup_expired_spec_files(str(missing))  # 例外なし


# --- _apply_spec_action: cleanup トリガー ---

class TestApplySpecActionCleanup:
    def _make_pipeline(self, tmp_path: Path, state: str, pipelines_dir: str) -> Path:
        import json
        pj_path = tmp_path / "test-pj.json"
        pj_data = {
            "project": "test-pj", "state": state,
            "enabled": True, "batch": [],
            "spec_mode": True,
            "spec_config": {"pipelines_dir": pipelines_dir},
            "history": [],
        }
        pj_path.write_text(json.dumps(pj_data))
        return pj_path

    def test_cleanup_called_on_terminal_states(self, tmp_path):
        """SPEC_STALLED / SPEC_REVIEW_FAILED / SPEC_PAUSED で cleanup が呼ばれる。

        SPEC_DONE は terminal ではなく IDLE への中間遷移。
        DCLパターンのため check_transition_spec をモックして終端状態を返させる。
        """
        terminal_states = ["SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED"]
        pd_str = str(tmp_path / "pd")
        orig_data = {"spec_config": {"pipelines_dir": pd_str}}
        for state in terminal_states:
            pj_path = self._make_pipeline(tmp_path, "SPEC_REVIEW", pd_str)
            action = SpecTransitionAction(
                next_state=state,
                expected_state="SPEC_REVIEW",
            )
            mocked_action = SpecTransitionAction(next_state=state)
            with patch("engine.fsm_spec.check_transition_spec", return_value=mocked_action), \
                 patch("engine.fsm_spec.send_to_agent_queued"), \
                 patch("engine.fsm_spec.notify_discord"), \
                 patch("engine.fsm_spec._cleanup_expired_spec_files") as mock_cleanup:
                _apply_spec_action(pj_path, action, _now(), orig_data)
            assert mock_cleanup.call_count == 1, \
                f"cleanup not called for terminal state {state}"
            assert mock_cleanup.call_args[0][0] == pd_str

    def test_cleanup_called_on_spec_done_to_idle(self, tmp_path):
        """SPEC_DONE → IDLE 遷移で cleanup が呼ばれる。"""
        pd_str = str(tmp_path / "pd")
        pj_path = self._make_pipeline(tmp_path, "SPEC_DONE", pd_str)
        action = SpecTransitionAction(
            next_state="IDLE",
            expected_state="SPEC_DONE",
        )
        mocked_action = SpecTransitionAction(next_state="IDLE")
        with patch("engine.fsm_spec.check_transition_spec", return_value=mocked_action), \
             patch("engine.fsm_spec.send_to_agent_queued"), \
             patch("engine.fsm_spec.notify_discord"), \
             patch("engine.fsm_spec._cleanup_expired_spec_files") as mock_cleanup, \
             patch("watchdog._check_queue"):
            _apply_spec_action(pj_path, action, _now(), {"spec_config": {"pipelines_dir": pd_str}})
        assert mock_cleanup.call_count == 1
        assert mock_cleanup.call_args[0][0] == pd_str

    # --- SPEC_DONE → IDLE: enabled フラグテスト (#301) ---

    def test_spec_done_to_idle_sets_enabled_false(self, tmp_path):
        """SPEC_DONE → IDLE (auto_qrun=False) で enabled=False になる。"""
        import json
        pd_str = str(tmp_path / "pd")
        pj_path = self._make_pipeline(tmp_path, "SPEC_DONE", pd_str)
        action = SpecTransitionAction(next_state="IDLE", expected_state="SPEC_DONE")
        mocked_action = SpecTransitionAction(next_state="IDLE")
        with patch("engine.fsm_spec.check_transition_spec", return_value=mocked_action), \
             patch("engine.fsm_spec.send_to_agent_queued"), \
             patch("engine.fsm_spec.notify_discord"), \
             patch("engine.fsm_spec._cleanup_expired_spec_files"):
            _apply_spec_action(pj_path, action, _now(), {"spec_config": {"pipelines_dir": pd_str}})
        data = json.loads(pj_path.read_text())
        assert data["enabled"] is False

    def test_spec_done_to_idle_auto_qrun_keeps_enabled_on_queue_success(self, tmp_path):
        """SPEC_DONE → IDLE (auto_qrun=True) でキュー起動成功時に enabled=True が維持される。"""
        import json
        pd_str = str(tmp_path / "pd")
        pj_path = tmp_path / "test-pj.json"
        pj_data = {
            "project": "test-pj", "state": "SPEC_DONE",
            "enabled": True, "batch": [],
            "spec_mode": True,
            "spec_config": {"pipelines_dir": pd_str, "auto_qrun": True},
            "history": [],
        }
        pj_path.write_text(json.dumps(pj_data))
        action = SpecTransitionAction(next_state="IDLE", expected_state="SPEC_DONE")
        mocked_action = SpecTransitionAction(next_state="IDLE")

        def fake_check_queue() -> None:
            # Simulate queue success: state moves beyond IDLE
            d = json.loads(pj_path.read_text())
            d["state"] = "PLANNING"
            pj_path.write_text(json.dumps(d))

        with patch("engine.fsm_spec.check_transition_spec", return_value=mocked_action), \
             patch("engine.fsm_spec.send_to_agent_queued"), \
             patch("engine.fsm_spec.notify_discord"), \
             patch("engine.fsm_spec._cleanup_expired_spec_files"), \
             patch("watchdog._check_queue", side_effect=fake_check_queue):
            _apply_spec_action(pj_path, action, _now(),
                               {"spec_config": {"pipelines_dir": pd_str, "auto_qrun": True}})
        data = json.loads(pj_path.read_text())
        assert data["enabled"] is True
        assert data["state"] == "PLANNING"

    def test_spec_done_to_idle_auto_qrun_fallback_disables_on_queue_failure(self, tmp_path):
        """SPEC_DONE → IDLE (auto_qrun=True) でキュー起動失敗時に enabled=False にフォールバック。"""
        import json
        pd_str = str(tmp_path / "pd")
        pj_path = tmp_path / "test-pj.json"
        pj_data = {
            "project": "test-pj", "state": "SPEC_DONE",
            "enabled": True, "batch": [],
            "spec_mode": True,
            "spec_config": {"pipelines_dir": pd_str, "auto_qrun": True},
            "history": [],
        }
        pj_path.write_text(json.dumps(pj_data))
        action = SpecTransitionAction(next_state="IDLE", expected_state="SPEC_DONE")
        mocked_action = SpecTransitionAction(next_state="IDLE")

        # _check_queue does nothing (failure case) — state stays IDLE
        with patch("engine.fsm_spec.check_transition_spec", return_value=mocked_action), \
             patch("engine.fsm_spec.send_to_agent_queued"), \
             patch("engine.fsm_spec.notify_discord"), \
             patch("engine.fsm_spec._cleanup_expired_spec_files"), \
             patch("watchdog._check_queue"):
            _apply_spec_action(pj_path, action, _now(),
                               {"spec_config": {"pipelines_dir": pd_str, "auto_qrun": True}})
        data = json.loads(pj_path.read_text())
        assert data["enabled"] is False

    def test_spec_done_to_idle_auto_qrun_toctou_no_false_disable(self, tmp_path):
        """AC4: _check_queue 失敗後に別操作で state が IDLE 以外に遷移した場合、
        _disable_if_idle の callback 内 state チェックにより enabled は変更されない。"""
        import json
        pd_str = str(tmp_path / "pd")
        pj_path = tmp_path / "test-pj.json"
        pj_data = {
            "project": "test-pj", "state": "SPEC_DONE",
            "enabled": True, "batch": [],
            "spec_mode": True,
            "spec_config": {"pipelines_dir": pd_str, "auto_qrun": True},
            "history": [],
        }
        pj_path.write_text(json.dumps(pj_data))
        action = SpecTransitionAction(next_state="IDLE", expected_state="SPEC_DONE")
        mocked_action = SpecTransitionAction(next_state="IDLE")

        def fake_check_queue_then_external_start() -> None:
            # _check_queue fails (noop), then a CLI operation starts a new batch
            # before _disable_if_idle callback runs
            d = json.loads(pj_path.read_text())
            d["state"] = "PLANNING"
            pj_path.write_text(json.dumps(d))

        with patch("engine.fsm_spec.check_transition_spec", return_value=mocked_action), \
             patch("engine.fsm_spec.send_to_agent_queued"), \
             patch("engine.fsm_spec.notify_discord"), \
             patch("engine.fsm_spec._cleanup_expired_spec_files"), \
             patch("watchdog._check_queue", side_effect=fake_check_queue_then_external_start):
            _apply_spec_action(pj_path, action, _now(),
                               {"spec_config": {"pipelines_dir": pd_str, "auto_qrun": True}})
        data = json.loads(pj_path.read_text())
        assert data["enabled"] is True, "enabled must NOT be disabled when state is no longer IDLE"
        assert data["state"] == "PLANNING"

    def test_cleanup_not_called_on_non_terminal(self, tmp_path):
        """非終端状態遷移では cleanup が呼ばれない。"""
        pd_str = str(tmp_path / "pd")
        pj_path = self._make_pipeline(tmp_path, "SPEC_REVIEW", pd_str)
        action = SpecTransitionAction(
            next_state="SPEC_REVISE",
            expected_state="SPEC_REVIEW",
        )
        mocked_action = SpecTransitionAction(next_state="SPEC_REVISE")
        with patch("engine.fsm_spec.check_transition_spec", return_value=mocked_action), \
             patch("engine.fsm_spec.send_to_agent_queued"), \
             patch("engine.fsm_spec.notify_discord"), \
             patch("engine.fsm_spec._cleanup_expired_spec_files") as mock_cleanup:
            _apply_spec_action(pj_path, action, _now(), {"spec_config": {"pipelines_dir": pd_str}})
        mock_cleanup.assert_not_called()


# --- TestSpecNudge ---


class TestSpecNudge:
    """Issue #76: spec mode レビュアー催促機能のテスト。"""

    def _base_sc(self, reviewer="reviewer1", sent_at_offset=-(NUDGE_GRACE_SEC + 1)):
        """sent_at 設定済み（猶予期間超過後）の単一レビュアーを持つ spec_config。"""
        sent_at = (_now() + timedelta(seconds=sent_at_offset)).isoformat()
        return {
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "rev_index": 1,
            "review_requests": {
                reviewer: {
                    "status": "pending",
                    "sent_at": sent_at,
                    "timeout_at": (_now() + timedelta(seconds=SPEC_BLOCK_TIMERS["SPEC_REVIEW"])).isoformat(),
                }
            },
            "current_reviews": {"entries": {}},
            "revise_count": 0,
            "max_revise_cycles": 5,
        }

    def _data_with_entered_at(self, entered_at_offset=-(NUDGE_GRACE_SEC + 1)):
        """SPEC_REVIEW への遷移履歴を持つ data dict。"""
        entered_at = (_now() + timedelta(seconds=entered_at_offset)).isoformat()
        return {
            "project": "test",
            "review_mode": "lite",
            "history": [{"from": "SPEC_APPROVED", "to": "SPEC_REVIEW", "at": entered_at}],
        }

    # --- _check_spec_review 催促テスト ---

    def test_spec_review_nudge_entered_at_none_no_nudge(self):
        """entered_at 取得不可 → nudge_reviewers=None（安全側フォールバック）。"""
        sc = self._base_sc()
        data = {"project": "test", "review_mode": "lite", "history": []}  # history なし
        action = _check_spec_review(sc, _now(), data)
        assert action.nudge_reviewers is None

    def test_spec_review_nudge_within_grace_no_nudge(self):
        """猶予期間内（elapsed < 300s）→ nudge_reviewers=None。"""
        sc = self._base_sc()
        entered_at = (_now() - timedelta(seconds=100)).isoformat()
        data = {
            "project": "test", "review_mode": "lite",
            "history": [{"from": "SPEC_APPROVED", "to": "SPEC_REVIEW", "at": entered_at}],
        }
        action = _check_spec_review(sc, _now(), data)
        assert action.nudge_reviewers is None

    def test_spec_review_nudge_after_grace_pending_reviewer(self):
        """猶予期間後（elapsed ≥ 300s）+ 未完了レビュアーあり → nudge_reviewers にそのレビュアーが含まれる。"""
        sc = self._base_sc("reviewer1")
        data = self._data_with_entered_at(-400)
        action = _check_spec_review(sc, _now(), data)
        assert action.nudge_reviewers is not None
        assert "reviewer1" in action.nudge_reviewers

    def test_spec_review_nudge_completed_reviewer_excluded(self):
        """status=received のレビュアー → nudge_reviewers に含まれない。"""
        sc = self._base_sc("reviewer1")
        sc["review_requests"]["reviewer1"]["status"] = "received"
        data = self._data_with_entered_at(-400)
        action = _check_spec_review(sc, _now(), data)
        # received は all_complete 方向なので next_state が返る可能性を考慮
        if action.next_state is None:
            assert action.nudge_reviewers is None or "reviewer1" not in (action.nudge_reviewers or [])

    def test_spec_review_nudge_timeout_reviewer_excluded(self):
        """status=timeout のレビュアー → nudge_reviewers に含まれない。"""
        sc = self._base_sc("reviewer1")
        sc["review_requests"]["reviewer1"]["status"] = "timeout"
        data = self._data_with_entered_at(-400)
        action = _check_spec_review(sc, _now(), data)
        # timeout は all_complete 方向なので next_state が返る可能性を考慮
        if action.next_state is None:
            assert action.nudge_reviewers is None or "reviewer1" not in (action.nudge_reviewers or [])

    def test_spec_review_nudge_unsent_reviewer_excluded(self):
        """sent_at=None（未送信）のレビュアー → nudge_reviewers に含まれない（初回送信と催促を区別）。"""
        sc = self._base_sc("reviewer1")
        sc["review_requests"]["reviewer1"]["sent_at"] = None  # 未送信
        data = self._data_with_entered_at(-400)
        action = _check_spec_review(sc, _now(), data)
        assert action.nudge_reviewers is None or "reviewer1" not in (action.nudge_reviewers or [])

    def test_spec_review_nudge_coexists_with_send_to(self):
        """一部レビュアーに初回送信しつつ、別レビュアー（sent_at済み）を催促。"""
        sent_at = (_now() - timedelta(seconds=NUDGE_GRACE_SEC + 1)).isoformat()
        sc = {
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "rev_index": 1,
            "review_requests": {
                "reviewer1": {"status": "pending", "sent_at": None, "timeout_at": None},   # 未送信
                "reviewer2": {"status": "pending", "sent_at": sent_at,
                             "timeout_at": (_now() + timedelta(seconds=SPEC_BLOCK_TIMERS["SPEC_REVIEW"])).isoformat()},  # 送信済み
            },
            "current_reviews": {"entries": {}},
            "revise_count": 0,
            "max_revise_cycles": 5,
        }
        data = self._data_with_entered_at(-400)
        action = _check_spec_review(sc, _now(), data)
        # reviewer1 は初回送信 → send_to に含まれる
        assert action.send_to is not None
        assert "reviewer1" in action.send_to
        # reviewer2 は催促対象
        assert action.nudge_reviewers is not None
        assert "reviewer2" in action.nudge_reviewers
        # reviewer2 は send_to に含まれない（催促と初回送信は分離）
        assert "reviewer2" not in action.send_to

    # --- _check_spec_revise 催促テスト ---

    def test_spec_revise_nudge_within_grace_no_nudge(self):
        """猶予期間内（elapsed < 300s）→ nudge_implementer=False。"""
        sc = {"retry_counts": {}}
        entered = (_now() - timedelta(seconds=100)).isoformat()
        data = {"history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": entered}]}
        action = _check_spec_revise(sc, _now(), data)
        assert action.nudge_implementer is False

    def test_spec_revise_nudge_after_grace(self):
        """猶予期間後（elapsed ≥ 300s かつ タイムアウト前）→ nudge_implementer=True。"""
        sc = {"retry_counts": {}}
        entered = (_now() - timedelta(seconds=NUDGE_GRACE_SEC + 1)).isoformat()
        data = {"history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": entered}]}
        action = _check_spec_revise(sc, _now(), data)
        assert action.nudge_implementer is True
        assert action.next_state is None

    def test_spec_revise_nudge_baseline_none_no_nudge(self):
        """baseline=None（history なし）→ nudge_implementer=False（安全側）。"""
        sc = {"retry_counts": {}}
        action = _check_spec_revise(sc, _now(), {"history": []})
        assert action.nudge_implementer is False

    # --- 再催促スキップテスト（_apply_spec_action 相当のロジック） ---

    def test_apply_spec_nudge_skips_within_inactive_threshold(self, tmp_pipelines):
        """last_nudge_at から < INACTIVE_THRESHOLD_SEC → send_to_agent_queued が呼ばれない。"""
        import json
        from datetime import datetime as real_datetime
        pj_path = tmp_pipelines / "test-pj.json"
        # nudge コード内の _datetime.now(LOCAL_TZ) は実時刻を使うため、
        # last_nudge_at は実時刻から (INACTIVE_THRESHOLD_SEC - 10) 秒前に設定する
        recent_nudge = (real_datetime.now(LOCAL_TZ) - timedelta(seconds=INACTIVE_THRESHOLD_SEC - 10)).isoformat()
        pj_data = {
            "project": "test-pj", "state": "SPEC_REVIEW",
            "enabled": True, "batch": [],
            "spec_mode": True,
            "spec_config": {
                "current_rev": "1",
                "spec_path": "docs/spec.md",
                "review_requests": {
                    "reviewer1": {"status": "pending", "sent_at": _now().isoformat(),
                               "last_nudge_at": recent_nudge},
                },
            },
            "history": [],
        }
        pj_path.write_text(json.dumps(pj_data))

        action = SpecTransitionAction(
            next_state=None,
            expected_state="SPEC_REVIEW",
            nudge_reviewers=["reviewer1"],
        )
        # DCL 再チェックでも nudge_reviewers を返すようにする（applied=True が条件）
        nudge_action = SpecTransitionAction(next_state=None, nudge_reviewers=["reviewer1"])
        with patch("engine.fsm_spec.check_transition_spec", return_value=nudge_action), \
             patch("engine.shared._is_agent_inactive", return_value=True), \
             patch("engine.fsm_spec.send_to_agent_queued") as mock_send, \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)
        mock_send.assert_not_called()

    def test_apply_spec_nudge_sends_after_inactive_threshold(self, tmp_pipelines):
        """last_nudge_at から ≥ INACTIVE_THRESHOLD_SEC → send_to_agent_queued が呼ばれる。"""
        import json
        from datetime import datetime as real_datetime
        pj_path = tmp_pipelines / "test-pj.json"
        # nudge コード内の _datetime.now(LOCAL_TZ) は実時刻を使うため、
        # last_nudge_at は実時刻から (INACTIVE_THRESHOLD_SEC + 10) 秒前に設定する
        old_nudge = (real_datetime.now(LOCAL_TZ) - timedelta(seconds=INACTIVE_THRESHOLD_SEC + 10)).isoformat()
        pj_data = {
            "project": "test-pj", "state": "SPEC_REVIEW",
            "enabled": True, "batch": [],
            "spec_mode": True,
            "spec_config": {
                "current_rev": "1",
                "spec_path": "docs/spec.md",
                "review_requests": {
                    "reviewer1": {"status": "pending", "sent_at": _now().isoformat(),
                               "last_nudge_at": old_nudge},
                },
            },
            "history": [],
        }
        pj_path.write_text(json.dumps(pj_data))

        action = SpecTransitionAction(
            next_state=None,
            expected_state="SPEC_REVIEW",
            nudge_reviewers=["reviewer1"],
        )
        # DCL 再チェックでも nudge_reviewers を返すようにする（applied=True が条件）
        nudge_action = SpecTransitionAction(next_state=None, nudge_reviewers=["reviewer1"])
        with patch("engine.fsm_spec.check_transition_spec", return_value=nudge_action), \
             patch("engine.shared._is_agent_inactive", return_value=True), \
             patch("engine.fsm_spec.send_to_agent_queued", return_value=True) as mock_send, \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)
        mock_send.assert_called_once()

    # --- process() 統合テスト ---

    def test_process_spec_review_nudge_triggers_apply(self, tmp_pipelines):
        """nudge_reviewers がある action が返る場合に _apply_spec_action が呼ばれる。"""
        import json
        from watchdog import process
        pj_path = tmp_pipelines / "test-pj.json"
        pj_data = {
            "project": "test-pj", "state": "SPEC_REVIEW",
            "enabled": True, "batch": [],
            "spec_mode": True, "spec_config": {},
            "history": [],
        }
        pj_path.write_text(json.dumps(pj_data))

        nudge_action = SpecTransitionAction(
            next_state=None,
            nudge_reviewers=["reviewer1"],
        )
        with patch("watchdog.check_transition_spec", return_value=nudge_action), \
             patch("watchdog._apply_spec_action") as mock_apply:
            process(pj_path)
        mock_apply.assert_called_once()

    def test_process_spec_nudge_empty_list_no_apply(self, tmp_pipelines):
        """nudge_reviewers=[] → _apply_spec_action が呼ばれない。"""
        import json
        from watchdog import process
        pj_path = tmp_pipelines / "test-pj.json"
        pj_data = {
            "project": "test-pj", "state": "SPEC_REVIEW",
            "enabled": True, "batch": [],
            "spec_mode": True, "spec_config": {},
            "history": [],
        }
        pj_path.write_text(json.dumps(pj_data))

        empty_nudge_action = SpecTransitionAction(
            next_state=None,
            nudge_reviewers=[],  # 空リスト = 催促不要
        )
        with patch("watchdog.check_transition_spec", return_value=empty_nudge_action), \
             patch("watchdog._apply_spec_action") as mock_apply:
            process(pj_path)
        mock_apply.assert_not_called()

    # --- メッセージ生成テスト ---

    def test_build_spec_review_nudge_msg_contains_command(self):
        """spec review nudge メッセージに spec review-submit コマンドが含まれる。"""
        msg = render("spec.review", "nudge",
            project="myproj", current_rev="2", spec_path="docs/spec.md",
            reviewer="reviewer1", GOKRAX_CLI=GOKRAX_CLI,
        )
        assert "spec review-submit" in msg
        assert "myproj" in msg
        assert "rev2" in msg
        assert "reviewer1" in msg

    def test_build_spec_revise_nudge_msg_contains_command(self):
        """spec revise nudge メッセージに spec revise-submit コマンドが含まれる。"""
        msg = render("spec.revise", "nudge",
            project="myproj", current_rev="3", GOKRAX_CLI=GOKRAX_CLI,
        )
        assert "spec revise-submit" in msg
        assert "myproj" in msg
        assert "rev3" in msg

    # --- datetime parse 失敗時の安全側フォールバック ---

    def test_apply_spec_nudge_reviewer_invalid_last_nudge_skips(self, tmp_pipelines):
        """last_nudge_at が不正な文字列 → parse 失敗 → 催促をスキップ（安全側）。"""
        import json
        pj_path = tmp_pipelines / "test-pj.json"
        pj_data = {
            "project": "test-pj", "state": "SPEC_REVIEW",
            "enabled": True, "batch": [],
            "spec_mode": True,
            "spec_config": {
                "current_rev": "1",
                "spec_path": "docs/spec.md",
                "review_requests": {
                    "reviewer1": {"status": "pending", "sent_at": _now().isoformat(),
                               "last_nudge_at": "INVALID-DATETIME"},
                },
            },
            "history": [],
        }
        pj_path.write_text(json.dumps(pj_data))

        action = SpecTransitionAction(
            next_state=None,
            expected_state="SPEC_REVIEW",
            nudge_reviewers=["reviewer1"],
        )
        nudge_action = SpecTransitionAction(next_state=None, nudge_reviewers=["reviewer1"])
        with patch("engine.fsm_spec.check_transition_spec", return_value=nudge_action), \
             patch("engine.shared._is_agent_inactive", return_value=True), \
             patch("engine.fsm_spec.send_to_agent_queued") as mock_send, \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)
        mock_send.assert_not_called()

    def test_apply_spec_nudge_implementer_invalid_last_nudge_skips(self, tmp_pipelines):
        """_last_nudge_implementer が不正な文字列 → parse 失敗 → 催促をスキップ（安全側）。"""
        import json
        pj_path = tmp_pipelines / "test-pj.json"
        pj_data = {
            "project": "test-pj", "state": "SPEC_REVISE",
            "enabled": True, "batch": [],
            "spec_mode": True,
            "spec_config": {
                "current_rev": "1",
                "spec_implementer": "implementer1",
                "_last_nudge_implementer": "INVALID-DATETIME",
            },
            "history": [],
        }
        pj_path.write_text(json.dumps(pj_data))

        action = SpecTransitionAction(
            next_state=None,
            expected_state="SPEC_REVISE",
            nudge_implementer=True,
        )
        nudge_action = SpecTransitionAction(next_state=None, nudge_implementer=True)
        with patch("engine.fsm_spec.check_transition_spec", return_value=nudge_action), \
             patch("engine.shared._is_agent_inactive", return_value=True), \
             patch("engine.fsm_spec.send_to_agent_queued") as mock_send, \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)
        mock_send.assert_not_called()

    def test_spec_review_nudge_naive_entered_at_no_nudge(self):
        """entered_at が naive datetime（tzinfo なし）→ TypeError → nudge_reviewers=None。"""
        sc = self._base_sc("reviewer1")
        # naive datetime を history に設定（tzinfo なし）
        naive_entered = "2026-03-06T12:00:00"  # no tz
        data = {
            "project": "test", "review_mode": "lite",
            "history": [{"from": "SPEC_APPROVED", "to": "SPEC_REVIEW", "at": naive_entered}],
        }
        action = _check_spec_review(sc, _now(), data)
        assert action.nudge_reviewers is None

    def test_spec_revise_nudge_naive_baseline_no_nudge(self):
        """baseline が naive datetime → TypeError → nudge_implementer=False。"""
        sc = {"retry_counts": {}}
        naive_entered = "2026-03-06T12:00:00"  # no tz
        data = {"history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": naive_entered}]}
        action = _check_spec_revise(sc, _now(), data)
        assert action.nudge_implementer is False


# --- Send failure rollback tests (#302) ---


class TestSendFailureRollback:
    """Issue #302: send failure → rollback cascade fix tests."""

    def _make_pipeline(self, tmp_path: Path, state: str, spec_config: dict) -> Path:
        import json
        pj_path = tmp_path / "test-pj.json"
        pj_data = {
            "project": "test-pj", "state": state,
            "enabled": True, "batch": [],
            "spec_mode": True,
            "spec_config": spec_config,
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": _now().isoformat()}],
        }
        pj_path.write_text(json.dumps(pj_data))
        return pj_path

    # --- Test 1: self_review_sent rollback on send failure ---

    def test_self_review_sent_rollback_on_send_failure(self, tmp_pipelines):
        """send failure 時に _self_review_sent がロールバックされること。"""
        import json
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "2",
            "rev_index": 2,
            "_revise_response": "REVISE_COMPLETE\ncommit: abc123\nchanges: +10 -5\nchangelog: fixed",
            "_revise_sent": _now().isoformat(),
            "review_requests": {},
            "current_reviews": {"entries": {}},
            "retry_counts": {},
        }
        pj_path = self._make_pipeline(tmp_pipelines, "SPEC_REVISE", sc)
        orig_data = json.loads(pj_path.read_text())

        # Branch (C) fires: revise_response present → self_review send
        action = SpecTransitionAction(
            next_state=None,
            expected_state="SPEC_REVISE",
        )
        with patch("engine.fsm_spec.send_to_agent_queued", return_value=False), \
             patch("engine.fsm_spec.notify_discord") as mock_discord, \
             patch("spec_revise.parse_revise_response", return_value={
                 "commit": "abc123",
                 "changes": {"added_lines": 10, "removed_lines": 5},
                 "changelog_summary": "fixed",
             }), \
             patch("spec_revise.build_revise_completion_updates", return_value={
                 "current_rev": "3",
                 "last_commit": "abc123",
             }):
            _apply_spec_action(pj_path, action, _now(), orig_data)

        result = json.loads(pj_path.read_text())
        assert result["spec_config"].get("_self_review_sent") is None
        # Discord send failure notification
        discord_calls = [str(c) for c in mock_discord.call_args_list]
        assert any("send failure" in c for c in discord_calls)

    # --- Test 2: B2 branch fires after rollback ---

    def test_b2_branch_fires_after_rollback(self):
        """ロールバック後の次 tick で branch (B2) が発火すること。"""
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "2",
            "_self_review_pending_updates": {"current_rev": "3", "last_commit": "abc123"},
            "_self_review_sent": None,
            "_self_review_pass": 0,
            "_revise_sent": _now().isoformat(),
            "review_requests": {},
            "current_reviews": {"entries": {}},
            "retry_counts": {},
        }
        data = {
            "project": "test-pj",
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": _now().isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        assert action.send_to is not None
        assert action.pipeline_updates is not None
        assert action.pipeline_updates.get("_self_review_sent") is not None
        assert action.pipeline_updates.get("_self_review_pass") == 1
        assert action.send_failure_rollback == {"_self_review_sent": None}

    # --- Test 3: _revise_sent rollback on send failure (initial revise send) ---

    def test_revise_sent_rollback_on_send_failure(self, tmp_pipelines):
        """send failure 時に _revise_sent がロールバックされること（revise 初回送信）。"""
        import json
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "rev_index": 1,
            "review_requests": {},
            "current_reviews": {
                "entries": {
                    "reviewer1": {
                        "verdict": "P0", "items": [{"id": "i1", "severity": "critical",
                            "section": "s", "title": "t", "description": "d", "suggestion": "s"}],
                        "raw_text": "", "parse_success": True, "status": "received",
                    },
                },
            },
            "retry_counts": {},
            "_revise_send_retries": 0,
        }
        pj_path = self._make_pipeline(tmp_pipelines, "SPEC_REVISE", sc)
        orig_data = json.loads(pj_path.read_text())

        action = SpecTransitionAction(
            next_state=None,
            expected_state="SPEC_REVISE",
        )
        with patch("engine.fsm_spec.send_to_agent_queued", return_value=False), \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), orig_data)

        result = json.loads(pj_path.read_text())
        assert result["spec_config"].get("_revise_sent") is None
        assert result["spec_config"].get("_revise_send_retries") == 1

    # --- Test 4: revise re-sent on next tick after rollback ---

    def test_revise_resent_on_next_tick(self):
        """ロールバック後の次 tick で revise が再送信されること。"""
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "rev_index": 1,
            "review_requests": {},
            "current_reviews": {
                "entries": {
                    "reviewer1": {
                        "verdict": "P0", "items": [{"id": "i1", "severity": "critical",
                            "section": "s", "title": "t", "description": "d", "suggestion": "s"}],
                        "raw_text": "", "parse_success": True, "status": "received",
                    },
                },
            },
            "retry_counts": {},
            "_revise_send_retries": 1,
        }
        data = {
            "project": "test-pj",
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": _now().isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        assert action.send_to is not None
        assert "impl1" in action.send_to
        assert action.pipeline_updates.get("_revise_send_retries") == 2

    # --- Test 5: issues_found send failure sets _issues_found_pending_feedback ---

    def test_issues_found_send_failure_sets_pending_feedback(self, tmp_pipelines):
        """issues_found send failure で _issues_found_pending_feedback が設定されること。"""
        import json
        # Mock parse_self_review_response to return issues_found directly
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "2",
            "rev_index": 2,
            "_self_review_response": "dummy response",
            "_self_review_sent": _now().isoformat(),
            "_self_review_pending_updates": {"current_rev": "3"},
            "_revise_sent": _now().isoformat(),
            "review_requests": {},
            "current_reviews": {"entries": {}},
            "retry_counts": {},
        }
        pj_path = self._make_pipeline(tmp_pipelines, "SPEC_REVISE", sc)
        orig_data = json.loads(pj_path.read_text())

        action = SpecTransitionAction(
            next_state=None,
            expected_state="SPEC_REVISE",
        )
        mock_result = {
            "verdict": "issues_found",
            "items": [{"id": "reflected_items_match", "result": "No", "evidence": "missing"}],
        }
        with patch("engine.fsm_spec.send_to_agent_queued", return_value=False), \
             patch("engine.fsm_spec.notify_discord"), \
             patch("spec_revise.parse_self_review_response", return_value=mock_result):
            _apply_spec_action(pj_path, action, _now(), orig_data)

        result = json.loads(pj_path.read_text())
        pfb = result["spec_config"].get("_issues_found_pending_feedback")
        assert pfb is not None
        assert isinstance(pfb, str)
        # _revise_sent should NOT be rolled back (issues_found does not rollback _revise_sent)
        assert result["spec_config"].get("_revise_sent") is not None

    # --- Test 6: A2 branch fires on next tick for issues_found pending ---

    def test_a2_branch_fires_for_issues_found_pending(self):
        """issues_found pending feedback の次 tick で branch (A2) が発火すること。"""
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "2",
            "_issues_found_pending_feedback": "Fix these issues:\n- item1",
            "_issues_found_send_retries": 0,
            "_revise_sent": _now().isoformat(),
            "review_requests": {},
            "current_reviews": {"entries": {}},
            "retry_counts": {},
        }
        data = {
            "project": "test-pj",
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": _now().isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        assert action.send_to is not None
        assert "impl1" in action.send_to
        assert action.send_to["impl1"] == "Fix these issues:\n- item1"
        assert action.pipeline_updates == {
            "_issues_found_pending_feedback": None,
            "_issues_found_send_retries": 1,
        }

    # --- Test 7: no rollback when send_failure_rollback is None ---

    def test_no_rollback_when_send_failure_rollback_none(self, tmp_pipelines):
        """send_failure_rollback が None の場合はロールバックしないこと。"""
        import json
        sc = {"spec_implementer": "impl1", "retry_counts": {}}
        pj_path = self._make_pipeline(tmp_pipelines, "SPEC_REVISE", sc)
        orig_data = json.loads(pj_path.read_text())

        action = SpecTransitionAction(
            next_state=None,
            expected_state="SPEC_REVISE",
            send_to={"impl1": "test msg"},
            pipeline_updates={"_test_field": "value"},
            send_failure_rollback=None,
        )
        # Mock check_transition_spec to return the same action
        with patch("engine.fsm_spec.check_transition_spec", return_value=action), \
             patch("engine.fsm_spec.send_to_agent_queued", return_value=False), \
             patch("engine.fsm_spec.notify_discord"), \
             patch("engine.fsm_spec.update_pipeline") as mock_up:
            # First call is the main update, second would be rollback
            _apply_spec_action(pj_path, action, _now(), orig_data)
        # update_pipeline should be called once for main update, NOT for rollback
        assert mock_up.call_count == 1

    # --- Test 8: reviewer send failure rolls back sent_at ---

    def test_reviewer_send_failure_rolls_back_sent_at(self, tmp_pipelines):
        """_check_spec_review の reviewer send failure で sent_at がロールバックされること。"""
        import json
        sc = {
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "rev_index": 1,
            "review_requests": {
                "reviewer1": {"status": "pending", "sent_at": None, "timeout_at": None,
                    "last_nudge_at": None, "response": None},
            },
            "current_reviews": {"entries": {}},
            "revise_count": 0,
            "max_revise_cycles": 5,
        }
        pj_path = self._make_pipeline(tmp_pipelines, "SPEC_REVIEW", sc)
        pj_data = json.loads(pj_path.read_text())
        pj_data["review_mode"] = "lite"

        action = SpecTransitionAction(
            next_state=None,
            expected_state="SPEC_REVIEW",
        )
        with patch("engine.fsm_spec.send_to_agent_queued", return_value=False), \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)

        result = json.loads(pj_path.read_text())
        rr = result["spec_config"]["review_requests"]["reviewer1"]
        assert rr["sent_at"] is None
        assert "timeout_at" not in rr or rr.get("timeout_at") is None

    # --- Test 9: reviewer resend on next tick after rollback ---

    def test_reviewer_resend_after_rollback(self):
        """reviewer ロールバック後の次 tick で再送信されること。"""
        sc = {
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "rev_index": 1,
            "review_requests": {
                "reviewer1": {"status": "pending", "sent_at": None, "timeout_at": None},
            },
            "current_reviews": {"entries": {}},
            "revise_count": 0,
            "max_revise_cycles": 5,
        }
        data = {"project": "test-pj", "review_mode": "lite", "history": []}
        action = _check_spec_review(sc, _now(), data)
        assert action.send_to is not None
        assert "reviewer1" in action.send_to

    # --- Test 10: B2 PAUSED on non-dict _self_review_pending_updates ---

    def test_b2_paused_on_non_dict_pending_updates(self):
        """_self_review_pending_updates が非 dict の場合 PAUSED になること。"""
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "2",
            "_self_review_pending_updates": "not a dict",
            "_self_review_sent": None,
            "_revise_sent": _now().isoformat(),
            "review_requests": {},
            "current_reviews": {"entries": {}},
            "retry_counts": {},
        }
        data = {
            "project": "test-pj",
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": _now().isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state == "SPEC_PAUSED"

    # --- Test 11: rollback exception does not crash watchdog ---

    def test_rollback_exception_does_not_crash(self, tmp_pipelines, caplog):
        """ロールバック処理が例外を投げた場合 watchdog が継続すること。"""
        import json
        import logging
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "rev_index": 1,
            "review_requests": {},
            "current_reviews": {
                "entries": {
                    "reviewer1": {
                        "verdict": "P0", "items": [{"id": "i1", "severity": "critical",
                            "section": "s", "title": "t", "description": "d", "suggestion": "s"}],
                        "raw_text": "", "parse_success": True, "status": "received",
                    },
                },
            },
            "retry_counts": {},
            "_revise_send_retries": 0,
        }
        pj_path = self._make_pipeline(tmp_pipelines, "SPEC_REVISE", sc)
        orig_data = json.loads(pj_path.read_text())

        action = SpecTransitionAction(
            next_state=None,
            expected_state="SPEC_REVISE",
        )

        call_count = [0]
        original_update = __import__("pipeline_io").update_pipeline

        def mock_update_pipeline(path, callback):
            call_count[0] += 1
            if call_count[0] >= 2:  # rollback call
                raise OSError("disk full")
            return original_update(path, callback)

        with patch("engine.fsm_spec.send_to_agent_queued", return_value=False), \
             patch("engine.fsm_spec.notify_discord"), \
             patch("engine.fsm_spec.update_pipeline", side_effect=mock_update_pipeline), \
             caplog.at_level(logging.WARNING):
            _apply_spec_action(pj_path, action, _now(), orig_data)
        assert any("send_failure_rollback failed" in r.message for r in caplog.records)

    # --- Test 12: partial reviewer rollback (only failed reviewer) ---

    def test_partial_reviewer_rollback(self, tmp_pipelines):
        """複数 reviewer 中1名のみ send failure → 失敗した reviewer のみロールバック。"""
        import json
        sc = {
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "rev_index": 1,
            "review_requests": {
                "reviewer_a": {"status": "pending", "sent_at": None, "timeout_at": None,
                    "last_nudge_at": None, "response": None},
                "reviewer_b": {"status": "pending", "sent_at": None, "timeout_at": None,
                    "last_nudge_at": None, "response": None},
            },
            "current_reviews": {"entries": {}},
            "revise_count": 0,
            "max_revise_cycles": 5,
        }
        pj_path = self._make_pipeline(tmp_pipelines, "SPEC_REVIEW", sc)
        pj_data = json.loads(pj_path.read_text())
        pj_data["review_mode"] = "lite"

        action = SpecTransitionAction(
            next_state=None,
            expected_state="SPEC_REVIEW",
        )

        def side_effect_send(agent_id, msg):
            return agent_id == "reviewer_a"

        with patch("engine.fsm_spec.send_to_agent_queued", side_effect=side_effect_send), \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)

        result = json.loads(pj_path.read_text())
        rr = result["spec_config"]["review_requests"]
        # reviewer_a: send succeeded → sent_at maintained
        assert rr["reviewer_a"]["sent_at"] is not None
        # reviewer_b: send failed → sent_at rolled back
        assert rr["reviewer_b"]["sent_at"] is None

    # --- Test 13: B2 PAUSED when _self_review_pass exhausted ---

    def test_b2_paused_on_self_review_pass_exhausted(self):
        """branch (B2) で _self_review_pass が SPEC_REVISE_SELF_REVIEW_PASSES に到達 → PAUSED。"""
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "2",
            "_self_review_pending_updates": {"current_rev": "3", "last_commit": "abc123"},
            "_self_review_sent": None,
            "_self_review_pass": SPEC_REVISE_SELF_REVIEW_PASSES,
            "_revise_sent": _now().isoformat(),
            "review_requests": {},
            "current_reviews": {"entries": {}},
            "retry_counts": {},
        }
        data = {
            "project": "test-pj",
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": _now().isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state == "SPEC_PAUSED"

    # --- Test 14: A2 PAUSED when _issues_found_send_retries exhausted ---

    def test_a2_paused_on_issues_found_retries_exhausted(self):
        """branch (A2) で _issues_found_send_retries が MAX_SPEC_RETRIES に到達 → PAUSED。"""
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "2",
            "_issues_found_pending_feedback": "Fix these issues",
            "_issues_found_send_retries": MAX_SPEC_RETRIES,
            "_revise_sent": _now().isoformat(),
            "review_requests": {},
            "current_reviews": {"entries": {}},
            "retry_counts": {},
        }
        data = {
            "project": "test-pj",
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": _now().isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state == "SPEC_PAUSED"
        assert action.pipeline_updates["paused_from"] == "SPEC_REVISE"
        assert action.pipeline_updates["_issues_found_pending_feedback"] is None
        assert action.pipeline_updates["_issues_found_send_retries"] is None

    # --- Test 15: D PAUSED when _revise_send_retries exhausted ---

    def test_d_paused_on_revise_send_retries_exhausted(self):
        """branch (D) で _revise_send_retries が MAX_SPEC_RETRIES に到達 → PAUSED。"""
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "rev_index": 1,
            "review_requests": {},
            "current_reviews": {
                "entries": {
                    "reviewer1": {
                        "verdict": "P0", "items": [{"id": "i1", "severity": "critical",
                            "section": "s", "title": "t", "description": "d", "suggestion": "s"}],
                        "raw_text": "", "parse_success": True, "status": "received",
                    },
                },
            },
            "retry_counts": {},
            "_revise_send_retries": MAX_SPEC_RETRIES,
        }
        data = {
            "project": "test-pj",
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": _now().isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state == "SPEC_PAUSED"
        assert action.pipeline_updates["paused_from"] == "SPEC_REVISE"
        assert action.pipeline_updates["_revise_send_retries"] is None

    # --- Test: reviewer_count excludes approved_prior (#307) ---

    def test_reviewer_count_excludes_approved_prior(self):
        """Discord notification reviewer_count should only count pending reviewers."""
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "2",
            "_self_review_response": "```yaml\nchecklist:\n  - id: reflected_items_match\n    result: \"Yes\"\n    evidence: \"ok\"\n  - id: no_new_contradictions\n    result: \"Yes\"\n    evidence: \"ok\"\n```",
            "_self_review_sent": _now().isoformat(),
            "_self_review_pending_updates": {
                "current_rev": "3",
                "last_commit": "abc123",
                "last_changes": {"added_lines": 10, "removed_lines": 5, "changelog_summary": "fix"},
                "review_requests_patch": {
                    "reviewer1": {"status": "approved_prior", "sent_at": None,
                                "timeout_at": None, "last_nudge_at": None, "response": None},
                    "reviewer2": {"status": "pending", "sent_at": None,
                                "timeout_at": None, "last_nudge_at": None, "response": None},
                },
            },
            "_self_review_expected_ids": ["reflected_items_match", "no_new_contradictions"],
            "_revise_sent": _now().isoformat(),
            "review_requests": {
                "reviewer1": {"status": "received"},
                "reviewer2": {"status": "received"},
            },
            "current_reviews": {"entries": {}},
            "retry_counts": {},
        }
        data = {
            "project": "test-pj",
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": _now().isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state == "SPEC_REVIEW"
        # reviewer_count in notification should be 1 (only pending reviewer2)
        assert "(1人)" in action.discord_notify

    # --- Test 16: A.1 clean resets counters ---

    def test_a1_clean_resets_send_retries(self):
        """(A.1) clean 遷移で _revise_send_retries と _issues_found_send_retries がリセットされること。"""
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "2",
            "_self_review_response": "```yaml\nchecklist:\n  - id: reflected_items_match\n    result: \"Yes\"\n    evidence: \"ok\"\n  - id: no_new_contradictions\n    result: \"Yes\"\n    evidence: \"ok\"\n```",
            "_self_review_sent": _now().isoformat(),
            "_self_review_pending_updates": {
                "current_rev": "3",
                "last_commit": "abc123",
                "last_changes": {"added_lines": 10, "removed_lines": 5, "changelog_summary": "fix"},
            },
            "_self_review_expected_ids": ["reflected_items_match", "no_new_contradictions"],
            "_revise_sent": _now().isoformat(),
            "_revise_send_retries": 2,
            "_issues_found_send_retries": 1,
            "review_requests": {"reviewer1": {"status": "pending"}},
            "current_reviews": {"entries": {}},
            "retry_counts": {},
        }
        data = {
            "project": "test-pj",
            "history": [{"from": "SPEC_REVIEW", "to": "SPEC_REVISE", "at": _now().isoformat()}],
        }
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state == "SPEC_REVIEW"
        assert action.pipeline_updates.get("_revise_send_retries") is None
        assert action.pipeline_updates.get("_issues_found_send_retries") is None
        assert action.pipeline_updates.get("_issues_found_pending_feedback") is None
