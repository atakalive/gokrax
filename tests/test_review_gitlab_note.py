"""tests/test_review_gitlab_note.py — post_gitlab_note() のテスト (retries=1)。"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

from engine.glab import GlabResult

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ok() -> GlabResult:
    return GlabResult(ok=True, stdout="", stderr="", returncode=0, error=None)


def _fail(stderr: str = "server error") -> GlabResult:
    return GlabResult(ok=False, stdout="", stderr=stderr, returncode=1, error=None)


def _make_pipeline(tmp_pipelines, state="DESIGN_REVIEW"):
    """DESIGN_REVIEW 状態 + issue #1 入りのパイプラインを作成して返す。"""
    data = {
        "project": "test-pj",
        "gitlab": "testns/test-pj",
        "state": state,
        "enabled": True,
        "implementer": "implementer1",
        "review_mode": "standard",
        "batch": [
            {
                "issue": 1,
                "title": "Test Issue",
                "commit": None,
                "cc_session_id": None,
                "design_reviews": {},
                "code_reviews": {},
                "added_at": "2025-01-01T00:00:00+09:00",
            }
        ],
        "history": [],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    path = tmp_pipelines / "test-pj.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path


class TestReviewGitlabNote:

    def test_success_single_call(self, tmp_pipelines):
        """run_glab(ok=True): 1回呼び出しで成功し、pipeline に review が記録される。"""
        _make_pipeline(tmp_pipelines)

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="reviewer1",
            verdict="APPROVE",
            summary="LGTM",
            force=False,
            phase="design",
        )
        with patch("notify.run_glab", return_value=_ok()) as run:
            gokrax.cmd_review(args)

        assert run.call_count == 1

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        reviews = data["batch"][0]["design_reviews"]
        assert "reviewer1" in reviews
        assert reviews["reviewer1"]["verdict"] == "APPROVE"

    def test_failure_pipeline_updated_and_log_warning(self, tmp_pipelines, caplog):
        """run_glab 失敗: pipeline JSON には review が記録され、logger に警告が出る。"""
        _make_pipeline(tmp_pipelines)

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="reviewer3",
            verdict="P1",
            summary="minor issue",
            force=False,
            phase="design",
        )
        import logging
        with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
            with patch("notify.run_glab", return_value=_fail()) as run:
                gokrax.cmd_review(args)

        assert run.call_count == 1

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        reviews = data["batch"][0]["design_reviews"]
        assert "reviewer3" in reviews
        assert reviews["reviewer3"]["verdict"] == "P1"

        assert any("glab note failed" in r.message for r in caplog.records)


class TestReviewForce:

    def test_force_overwrites_existing_review(self, tmp_pipelines):
        """--force あり: 既存レビューが新しい verdict/at で上書きされる。"""
        _make_pipeline(tmp_pipelines)

        import gokrax

        # 1回目: reviewer1 が P0 を投稿
        args1 = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="reviewer1",
            verdict="P0",
            summary="",
            force=False,
            phase="design",
        )
        with patch("notify.run_glab", return_value=_ok()):
            with patch("commands.dev.now_iso", return_value="2026-01-01T00:00:00+09:00"):
                gokrax.cmd_review(args1)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"]["reviewer1"]["verdict"] == "P0"
        assert data["batch"][0]["design_reviews"]["reviewer1"]["at"] == "2026-01-01T00:00:00+09:00"

        # 2回目: reviewer1 が APPROVE を --force で上書き
        args2 = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="reviewer1",
            verdict="APPROVE",
            summary="",
            force=True,
            phase="design",
        )
        with patch("notify.run_glab", return_value=_ok()):
            with patch("commands.dev.now_iso", return_value="2026-01-01T01:00:00+09:00"):
                gokrax.cmd_review(args2)

        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"]["reviewer1"]["verdict"] == "APPROVE"
        assert data["batch"][0]["design_reviews"]["reviewer1"]["at"] == "2026-01-01T01:00:00+09:00"

    def test_without_force_skips_existing_review(self, tmp_pipelines):
        """--force なし: 既存レビューはスキップされ、GitLab note も投稿されない。"""
        _make_pipeline(tmp_pipelines)

        import gokrax

        # 1回目: reviewer1 が P0 を投稿
        args1 = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="reviewer1",
            verdict="P0",
            summary="",
            force=False,
            phase="design",
        )
        with patch("notify.run_glab", return_value=_ok()):
            gokrax.cmd_review(args1)

        # 2回目: reviewer1 が APPROVE を --force なしで投稿（スキップされるはず）
        args2 = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="reviewer1",
            verdict="APPROVE",
            summary="",
            force=False,
            phase="design",
        )
        with patch("notify.run_glab", return_value=_ok()) as run_2nd:
            gokrax.cmd_review(args2)

        # verdict は P0 のまま（上書きされていない）
        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"]["reviewer1"]["verdict"] == "P0"

        # スキップ時は run_glab が呼ばれない
        assert run_2nd.call_count == 0

    def test_force_on_new_reviewer_works_normally(self, tmp_pipelines):
        """--force あり + 新規レビュアー: 通常通りレビューが記録される。"""
        _make_pipeline(tmp_pipelines)

        import gokrax

        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="reviewer1",
            verdict="APPROVE",
            summary="LGTM",
            force=True,
            phase="design",
        )
        with patch("notify.run_glab", return_value=_ok()):
            gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"]["reviewer1"]["verdict"] == "APPROVE"


class TestReviewNotePending:
    """Issue #379: review note を verdict と原子的に pending へ積み、at-least-once で配送する。"""

    def test_failure_queues_pending(self, tmp_pipelines):
        """投稿失敗時に _pending_notifications へ review_note:* が積まれる。verdict も記録済み。"""
        _make_pipeline(tmp_pipelines)

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="reviewer3",
            verdict="P1",
            summary="minor issue",
            force=False,
            phase="design",
        )
        with patch("notify.run_glab", return_value=_fail()):
            gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"]["reviewer3"]["verdict"] == "P1"

        pending = data.get("_pending_notifications", {})
        note_keys = [k for k in pending if k.startswith("review_note:1:reviewer3:design:")]
        assert len(note_keys) == 1
        entry = pending[note_keys[0]]
        assert entry["kind"] == "review_note"
        assert "P1" in entry["body"]

    def test_success_clears_pending(self, tmp_pipelines):
        """投稿成功時は review_note:* キーが pending に残らない。即時投稿のみ（call_count==1）。"""
        _make_pipeline(tmp_pipelines)

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="reviewer1",
            verdict="P1",
            summary="needs work",
            force=False,
            phase="design",
        )
        with patch("notify.run_glab", return_value=_ok()) as run:
            gokrax.cmd_review(args)

        assert run.call_count == 1

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        assert data["batch"][0]["design_reviews"]["reviewer1"]["verdict"] == "P1"
        pending = data.get("_pending_notifications", {})
        assert not [k for k in pending if k.startswith("review_note:")]

    def test_npass_intermediate_approve_not_queued(self, tmp_pipelines):
        """NPASS 中間パスの APPROVE はノートを積まない（call_count==0、pending にもなし）。"""
        data = {
            "project": "test-pj",
            "gitlab": "testns/test-pj",
            "state": "DESIGN_REVIEW_NPASS",
            "enabled": True,
            "implementer": "implementer1",
            "review_mode": "standard",
            "review_config": {
                "design": {"members": ["reviewer1"], "min_reviews": 1,
                           "n_pass": {"reviewer1": 2}, "grace_period_sec": 0},
                "code": {"members": ["reviewer1"], "min_reviews": 1,
                         "n_pass": {}, "grace_period_sec": 0},
            },
            "batch": [
                {
                    "issue": 1,
                    "title": "Test Issue",
                    "commit": None,
                    "cc_session_id": None,
                    "design_reviews": {},
                    "code_reviews": {},
                    "added_at": "2025-01-01T00:00:00+09:00",
                }
            ],
            "history": [],
            "created_at": "2025-01-01T00:00:00+09:00",
            "updated_at": "2025-01-01T00:00:00+09:00",
        }
        path = tmp_pipelines / "test-pj.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="reviewer1",
            verdict="APPROVE",
            summary="ok so far",
            force=False,
            phase="design",
        )
        with patch("notify.run_glab", return_value=_ok()) as run:
            gokrax.cmd_review(args)

        assert run.call_count == 0
        data = json.loads(path.read_text())
        pending = data.get("_pending_notifications", {})
        assert not [k for k in pending if k.startswith("review_note:")]

    def test_multiple_reviewers_no_key_collision(self, tmp_pipelines):
        """複数レビュアーで review_note:* キーが衝突せず 2 件残り、両 body が保持される。"""
        _make_pipeline(tmp_pipelines)

        import gokrax
        for reviewer, verdict in (("reviewer1", "P0"), ("reviewer3", "P1")):
            args = argparse.Namespace(
                project="test-pj",
                issue=1,
                reviewer=reviewer,
                verdict=verdict,
                summary=f"from {reviewer}",
                force=False,
                phase="design",
            )
            with patch("notify.run_glab", return_value=_fail()):
                gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        pending = data.get("_pending_notifications", {})
        note_keys = [k for k in pending if k.startswith("review_note:")]
        assert len(note_keys) == 2
        bodies = [pending[k]["body"] for k in note_keys]
        assert any("from reviewer1" in b for b in bodies)
        assert any("from reviewer3" in b for b in bodies)

    def test_dispute_accepted_still_posts_note(self, tmp_pipelines):
        """dijkstra P1 回帰: dispute 受理でもノートが即時投稿される（従来挙動維持）。"""
        data = {
            "project": "test-pj",
            "gitlab": "testns/test-pj",
            "state": "DESIGN_REVISE",
            "enabled": True,
            "implementer": "implementer1",
            "review_mode": "standard",
            "batch": [
                {
                    "issue": 1,
                    "title": "Test Issue",
                    "commit": None,
                    "cc_session_id": None,
                    "design_reviews": {"reviewer1": {"verdict": "P1", "at": "2025-01-01T00:00:00+09:00"}},
                    "code_reviews": {},
                    "disputes": [
                        {"reviewer": "reviewer1", "status": "pending",
                         "phase": "design", "filed_verdict": "P1"}
                    ],
                    "added_at": "2025-01-01T00:00:00+09:00",
                }
            ],
            "history": [],
            "created_at": "2025-01-01T00:00:00+09:00",
            "updated_at": "2025-01-01T00:00:00+09:00",
        }
        path = tmp_pipelines / "test-pj.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="reviewer1",
            verdict="APPROVE",
            summary="dispute resolved",
            force=True,
            phase=None,
        )
        with patch("notify.run_glab", return_value=_ok()) as run:
            gokrax.cmd_review(args)

        assert run.call_count == 1
        data = json.loads(path.read_text())
        issue = data["batch"][0]
        assert issue["disputes"][0]["status"] == "accepted"
        assert "reviewer1" not in issue["design_reviews"]
