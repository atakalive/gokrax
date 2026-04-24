"""tests/test_spec_cli.py — spec CLI commands: start/approve/continue/done/retry/resume/extend/status"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import MAX_SPEC_RETRIES
from pipeline_io import default_spec_config
from tests.conftest import write_pipeline


# ── Module-level helpers ──────────────────────────────────────────────────────


def _make_pipeline(state="IDLE", spec_mode=False, spec_config=None, **kwargs):
    """テスト用 pipeline JSON 辞書を生成。"""
    data = {
        "project": "test-pj",
        "gitlab": "testns/test-pj",
        "state": state,
        "spec_mode": spec_mode,
        "spec_config": spec_config if spec_config is not None else {},
        "enabled": True,
        "implementer": "implementer1",
        "review_mode": "full",
        "batch": [],
        "history": [],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    data.update(kwargs)
    return data


def _make_spec_config(**overrides):
    """default_spec_config() をベースに上書きした spec_config を生成。"""
    cfg = default_spec_config()
    cfg.update(overrides)
    return cfg


def _make_active_pipeline(state="SPEC_REVIEW", **sc_overrides):
    """spec_mode=True の active pipeline を生成。"""
    sc = _make_spec_config(
        spec_path="docs/spec.md",
        spec_implementer="implementer1",
        review_requests={
            "reviewer1": {"status": "pending", "sent_at": None, "timeout_at": None,
                       "last_nudge_at": None, "response": None},
            "aria": {"status": "pending", "sent_at": None, "timeout_at": None,
                     "last_nudge_at": None, "response": None},
        },
        **sc_overrides,
    )
    return _make_pipeline(state=state, spec_mode=True, spec_config=sc)


def _args(**kwargs):
    """argparse.Namespace を簡単に生成するヘルパー。"""
    return argparse.Namespace(**kwargs)


# ── TestCmdSpecStart ──────────────────────────────────────────────────────────


class TestCmdSpecStart:

    @pytest.fixture(autouse=True)
    def _spec_file(self, tmp_pipelines, monkeypatch):
        """spec start テスト用: cwd を tmp_pipelines に変更し docs/spec.md を作成"""
        (tmp_pipelines / "docs").mkdir(exist_ok=True)
        (tmp_pipelines / "docs" / "spec.md").write_text("# test spec")
        monkeypatch.chdir(tmp_pipelines)

    def test_start_basic(self, tmp_pipelines):
        """IDLE → SPEC_REVIEW、spec_config 全フィールドの確認"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE"))

        from commands.spec import cmd_spec_start
        args = _args(
            project="test-pj", spec="docs/spec.md", implementer="implementer1",
            review_only=False, no_queue=False, skip_review=False,
            max_cycles=None, review_mode=None, model=None, auto_continue=False, auto_qrun=False,
        )
        with patch("gokrax._start_loop"):
            cmd_spec_start(args)

        data = json.loads(path.read_text())
        assert data["state"] == "SPEC_REVIEW"
        assert data["spec_mode"] is True
        assert data["enabled"] is True
        sc = data["spec_config"]
        assert sc["spec_path"].endswith("docs/spec.md")
        assert sc["spec_implementer"] == "implementer1"
        assert sc["auto_continue"] is False
        assert sc["review_only"] is False
        assert sc["no_queue"] is False
        assert sc["skip_review"] is False
        assert "reviewer1" in sc["review_requests"]
        assert sc["review_requests"]["reviewer1"]["status"] == "pending"
        assert sc["revise_count"] == 0
        assert sc["review_history"] == []
        assert sc["force_events"] == []

    def test_start_skip_review(self, tmp_pipelines):
        """--skip-review: IDLE → SPEC_APPROVED、auto_continue=True 強制"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE"))

        from commands.spec import cmd_spec_start
        args = _args(
            project="test-pj", spec="docs/spec.md", implementer="implementer1",
            review_only=False, no_queue=False, skip_review=True,
            max_cycles=None, review_mode=None, model=None, auto_continue=False, auto_qrun=False,
        )
        with patch("gokrax._start_loop"):
            cmd_spec_start(args)

        data = json.loads(path.read_text())
        assert data["state"] == "SPEC_APPROVED"
        assert data["spec_config"]["skip_review"] is True
        assert data["spec_config"]["auto_continue"] is True

    def test_start_review_only(self, tmp_pipelines):
        """--review-only: auto_continue=False、no_queue=True 強制"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE"))

        from commands.spec import cmd_spec_start
        args = _args(
            project="test-pj", spec="docs/spec.md", implementer="implementer1",
            review_only=True, no_queue=False, skip_review=False,
            max_cycles=None, review_mode=None, model=None, auto_continue=True, auto_qrun=False,
        )
        with patch("gokrax._start_loop"):
            cmd_spec_start(args)

        data = json.loads(path.read_text())
        sc = data["spec_config"]
        assert sc["review_only"] is True
        assert sc["auto_continue"] is False
        assert sc["no_queue"] is True

    def test_start_skip_and_review_only_exclusive(self, tmp_pipelines):
        """--skip-review と --review-only の排他チェック → SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE"))

        from commands.spec import cmd_spec_start
        args = _args(
            project="test-pj", spec="docs/spec.md", implementer="implementer1",
            review_only=True, no_queue=False, skip_review=True,
            max_cycles=None, review_mode=None, model=None, auto_continue=False, auto_qrun=False,
        )
        with pytest.raises(SystemExit, match="mutually exclusive"):
            cmd_spec_start(args)

    def test_start_non_idle_state(self, tmp_pipelines):
        """非IDLE状態からの start → SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="DESIGN_PLAN"))

        from commands.spec import cmd_spec_start
        args = _args(
            project="test-pj", spec="docs/spec.md", implementer="implementer1",
            review_only=False, no_queue=False, skip_review=False,
            max_cycles=None, review_mode=None, model=None, auto_continue=False, auto_qrun=False,
        )
        with pytest.raises(SystemExit, match="expected IDLE"):
            cmd_spec_start(args)

    def test_start_spec_mode_already_active(self, tmp_pipelines):
        """spec_mode=True 時の start → SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE", spec_mode=True))

        from commands.spec import cmd_spec_start
        args = _args(
            project="test-pj", spec="docs/spec.md", implementer="implementer1",
            review_only=False, no_queue=False, skip_review=False,
            max_cycles=None, review_mode=None, model=None, auto_continue=False, auto_qrun=False,
        )
        with pytest.raises(SystemExit, match="already active"):
            cmd_spec_start(args)

    def test_start_spec_file_not_found(self, tmp_pipelines):
        """spec ファイルが存在しない場合 → SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE"))

        from commands.spec import cmd_spec_start
        args = _args(
            project="test-pj", spec="docs/nonexistent.md", implementer="implementer1",
            review_only=False, no_queue=False, skip_review=False,
            max_cycles=None, review_mode=None, model=None, auto_continue=False, auto_qrun=False,
        )
        with pytest.raises(SystemExit, match="Spec file not found"):
            cmd_spec_start(args)

    def test_start_max_cycles_0(self, tmp_pipelines):
        """--max-cycles 0 → max_revise_cycles=0（0 は None でないため有効値）"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE"))

        from commands.spec import cmd_spec_start
        args = _args(
            project="test-pj", spec="docs/spec.md", implementer="implementer1",
            review_only=False, no_queue=False, skip_review=False,
            max_cycles=0, review_mode=None, model=None, auto_continue=False, auto_qrun=False,
        )
        with patch("gokrax._start_loop"):
            cmd_spec_start(args)

        data = json.loads(path.read_text())
        assert data["spec_config"]["max_revise_cycles"] == 0


# ── TestCmdSpecApprove ────────────────────────────────────────────────────────


class TestCmdSpecApprove:

    def test_approve_basic(self, tmp_pipelines):
        """SPEC_REVIEW (P0/P1 なし) → SPEC_APPROVED"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(
            spec_path="docs/spec.md", spec_implementer="implementer1",
            current_reviews={
                "entries": {
                    "reviewer1": {"verdict": "APPROVE", "items": []},
                }
            },
        )
        write_pipeline(path, _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=sc))

        from commands.spec import cmd_spec_approve
        cmd_spec_approve(_args(project="test-pj", force=False))

        data = json.loads(path.read_text())
        assert data["state"] == "SPEC_APPROVED"

    def test_approve_blocked_by_p1(self, tmp_pipelines):
        """P1 verdict があれば --force なしで SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(
            current_reviews={
                "entries": {
                    "reviewer1": {"verdict": "P1", "items": [{"id": "r1", "severity": "major"}]},
                }
            },
        )
        write_pipeline(path, _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=sc))

        from commands.spec import cmd_spec_approve
        with pytest.raises(SystemExit, match="Use --force"):
            cmd_spec_approve(_args(project="test-pj", force=False))

    def test_approve_force(self, tmp_pipelines):
        """--force: review_history に追加、current_reviews クリア、force_events 記録"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(
            spec_path="docs/spec.md",
            current_rev="2",
            current_reviews={
                "entries": {
                    "reviewer1": {
                        "verdict": "P0",
                        "items": [{"id": "item1", "severity": "critical"}],
                    },
                }
            },
        )
        write_pipeline(path, _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=sc))

        from commands.spec import cmd_spec_approve
        with patch("commands.spec.notify_discord"):
            cmd_spec_approve(_args(project="test-pj", force=True))

        data = json.loads(path.read_text())
        assert data["state"] == "SPEC_APPROVED"
        sc_out = data["spec_config"]
        assert sc_out["current_reviews"] == {}
        assert len(sc_out["review_history"]) == 1
        assert sc_out["review_history"][0]["rev"] == "2"
        assert len(sc_out["force_events"]) == 1
        fe = sc_out["force_events"][0]
        assert fe["actor"] == "M"
        assert fe["from_state"] == "SPEC_REVIEW"
        assert "reviewer1:item1" in fe["remaining_p1_items"]


    def test_approve_invalid_state(self, tmp_pipelines):
        """IDLE等の不正状態からapproveするとSystemExit"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config()
        write_pipeline(path, _make_pipeline(state="IDLE", spec_mode=True, spec_config=sc))

        from commands.spec import cmd_spec_approve
        with pytest.raises(SystemExit, match="Cannot approve"):
            cmd_spec_approve(_args(project="test-pj", force=False))

    def test_approve_no_spec_mode(self, tmp_pipelines):
        """spec_mode=False ならSystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="SPEC_REVIEW", spec_mode=False))

        from commands.spec import cmd_spec_approve
        with pytest.raises(SystemExit, match="spec_mode"):
            cmd_spec_approve(_args(project="test-pj", force=False))


# ── TestCmdSpecContinue ───────────────────────────────────────────────────────


class TestCmdSpecContinue:

    def test_continue_basic(self, tmp_pipelines):
        """SPEC_APPROVED → ISSUE_SUGGESTION"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_active_pipeline(state="SPEC_APPROVED"))

        from commands.spec import cmd_spec_continue
        cmd_spec_continue(_args(project="test-pj"))

        data = json.loads(path.read_text())
        assert data["state"] == "ISSUE_SUGGESTION"

    def test_continue_wrong_state(self, tmp_pipelines):
        """非 SPEC_APPROVED → SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_active_pipeline(state="SPEC_REVIEW"))

        from commands.spec import cmd_spec_continue
        with pytest.raises(SystemExit, match="expected SPEC_APPROVED"):
            cmd_spec_continue(_args(project="test-pj"))


# ── TestCmdSpecDone ───────────────────────────────────────────────────────────


class TestCmdSpecDone:

    def test_done_basic(self, tmp_pipelines):
        """SPEC_DONE → IDLE、spec_mode=False、spec_config={}"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_active_pipeline(state="SPEC_DONE"))

        from commands.spec import cmd_spec_done
        cmd_spec_done(_args(project="test-pj"))

        data = json.loads(path.read_text())
        assert data["state"] == "IDLE"
        assert data["spec_mode"] is False
        assert data["spec_config"] == {}


# ── TestCmdSpecRetry ──────────────────────────────────────────────────────────


class TestCmdSpecRetry:

    def test_retry_basic(self, tmp_pipelines):
        """SPEC_REVIEW_FAILED → SPEC_REVIEW、review_requests リセット確認"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(
            spec_path="docs/spec.md",
            review_requests={
                "reviewer1": {"status": "responded", "sent_at": "2026-01-01T00:00:00+09:00",
                           "timeout_at": None, "last_nudge_at": None, "response": "P0"},
                "aria": {"status": "timeout", "sent_at": "2026-01-01T00:00:00+09:00",
                         "timeout_at": None, "last_nudge_at": None, "response": None},
            },
            current_reviews={"entries": {"reviewer1": {"verdict": "P0", "items": [{"id": "C-1", "severity": "critical"}]}}},
        )
        write_pipeline(path, _make_pipeline(
            state="SPEC_REVIEW_FAILED", spec_mode=True, spec_config=sc
        ))

        from commands.spec import cmd_spec_retry
        cmd_spec_retry(_args(project="test-pj"))

        data = json.loads(path.read_text())
        assert data["state"] == "SPEC_REVIEW"
        sc_out = data["spec_config"]
        assert sc_out["current_reviews"] == {}
        for r, entry in sc_out["review_requests"].items():
            assert entry["status"] == "pending", f"{r} status should be pending"
            assert entry["sent_at"] is None
            assert entry["response"] is None

    def test_retry_wrong_state(self, tmp_pipelines):
        """非 SPEC_REVIEW_FAILED → SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_active_pipeline(state="SPEC_REVIEW"))

        from commands.spec import cmd_spec_retry
        with pytest.raises(SystemExit, match="expected SPEC_REVIEW_FAILED"):
            cmd_spec_retry(_args(project="test-pj"))


# ── TestCmdSpecResume ─────────────────────────────────────────────────────────


class TestCmdSpecResume:

    def test_resume_basic(self, tmp_pipelines):
        """SPEC_PAUSED → paused_from 先、paused_from=None 確認"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(
            spec_path="docs/spec.md",
            paused_from="SPEC_REVISE",
            review_requests={},
        )
        write_pipeline(path, _make_pipeline(state="SPEC_PAUSED", spec_mode=True, spec_config=sc))

        from commands.spec import cmd_spec_resume
        cmd_spec_resume(_args(project="test-pj"))

        data = json.loads(path.read_text())
        assert data["state"] == "SPEC_REVISE"
        assert data["spec_config"]["paused_from"] is None

    def test_resume_from_spec_review_resets_requests(self, tmp_pipelines):
        """paused_from=SPEC_REVIEW: review_requests リセット + current_reviews クリア"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(
            spec_path="docs/spec.md",
            paused_from="SPEC_REVIEW",
            review_requests={
                "reviewer1": {"status": "responded", "sent_at": "2026-01-01T00:00:00+09:00",
                           "timeout_at": None, "last_nudge_at": None, "response": "APPROVE"},
            },
            current_reviews={"entries": {"reviewer1": {"verdict": "APPROVE", "items": []}}},
        )
        write_pipeline(path, _make_pipeline(state="SPEC_PAUSED", spec_mode=True, spec_config=sc))

        from commands.spec import cmd_spec_resume
        cmd_spec_resume(_args(project="test-pj"))

        data = json.loads(path.read_text())
        assert data["state"] == "SPEC_REVIEW"
        sc_out = data["spec_config"]
        assert sc_out["current_reviews"] == {}
        assert sc_out["review_requests"]["reviewer1"]["status"] == "pending"
        assert sc_out["review_requests"]["reviewer1"]["response"] is None

    def test_resume_from_issue_suggestion_recalculates_timeout(self, tmp_pipelines):
        """paused_from=ISSUE_SUGGESTION: issue_suggestions の timeout_at 再計算"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(
            spec_path="docs/spec.md",
            paused_from="ISSUE_SUGGESTION",
            review_requests={},
            issue_suggestions={
                "s1": {"status": "pending", "timeout_at": "2025-01-01T00:00:00+09:00"},
                "s2": {"status": "responded", "timeout_at": None},
            },
        )
        write_pipeline(path, _make_pipeline(state="SPEC_PAUSED", spec_mode=True, spec_config=sc))

        from commands.spec import cmd_spec_resume
        cmd_spec_resume(_args(project="test-pj"))

        data = json.loads(path.read_text())
        sc_out = data["spec_config"]
        # pending の s1 は timeout_at が更新されている
        assert sc_out["issue_suggestions"]["s1"]["timeout_at"] != "2025-01-01T00:00:00+09:00"
        assert sc_out["issue_suggestions"]["s1"]["timeout_at"] is not None
        # responded の s2 は変更なし
        assert sc_out["issue_suggestions"]["s2"]["timeout_at"] is None

    def test_resume_resets_retry_counts(self, tmp_pipelines):
        """resume で対象状態の retry_counts が 0 にリセットされる"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(
            spec_path="docs/spec.md",
            paused_from="SPEC_REVISE",
            review_requests={},
            retry_counts={"SPEC_REVISE": MAX_SPEC_RETRIES, "SPEC_REVIEW": 1},
        )
        write_pipeline(path, _make_pipeline(state="SPEC_PAUSED", spec_mode=True, spec_config=sc))

        from commands.spec import cmd_spec_resume
        cmd_spec_resume(_args(project="test-pj"))

        data = json.loads(path.read_text())
        sc_out = data["spec_config"]
        assert sc_out["retry_counts"]["SPEC_REVISE"] == 0

    def test_resume_null_paused_from(self, tmp_pipelines):
        """paused_from=None → SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(spec_path="docs/spec.md", paused_from=None)
        write_pipeline(path, _make_pipeline(state="SPEC_PAUSED", spec_mode=True, spec_config=sc))

        from commands.spec import cmd_spec_resume
        with pytest.raises(SystemExit, match="paused_from is null"):
            cmd_spec_resume(_args(project="test-pj"))


# ── TestCmdSpecExtend ─────────────────────────────────────────────────────────


class TestCmdSpecExtend:

    def test_extend_basic(self, tmp_pipelines):
        """SPEC_STALLED → SPEC_REVISE、max_revise_cycles += N"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(spec_path="docs/spec.md", max_revise_cycles=5)
        write_pipeline(path, _make_pipeline(state="SPEC_STALLED", spec_mode=True, spec_config=sc))

        from commands.spec import cmd_spec_extend
        cmd_spec_extend(_args(project="test-pj", cycles=3))

        data = json.loads(path.read_text())
        assert data["state"] == "SPEC_REVISE"
        assert data["spec_config"]["max_revise_cycles"] == 8

    def test_extend_default_n_is_2(self, tmp_pipelines):
        """--cycles 省略時のデフォルト N=2"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(spec_path="docs/spec.md", max_revise_cycles=5)
        write_pipeline(path, _make_pipeline(state="SPEC_STALLED", spec_mode=True, spec_config=sc))

        from commands.spec import cmd_spec_extend
        cmd_spec_extend(_args(project="test-pj", cycles=2))

        data = json.loads(path.read_text())
        assert data["spec_config"]["max_revise_cycles"] == 7

    def test_extend_preserves_current_reviews(self, tmp_pipelines):
        """extend で current_reviews はクリアされない"""
        path = tmp_pipelines / "test-pj.json"
        existing_reviews = {
            "entries": {"reviewer1": {"verdict": "P1", "items": [{"id": "x", "severity": "major"}]}}
        }
        sc = _make_spec_config(
            spec_path="docs/spec.md",
            max_revise_cycles=5,
            current_reviews=existing_reviews,
        )
        write_pipeline(path, _make_pipeline(state="SPEC_STALLED", spec_mode=True, spec_config=sc))

        from commands.spec import cmd_spec_extend
        cmd_spec_extend(_args(project="test-pj", cycles=2))

        data = json.loads(path.read_text())
        assert data["spec_config"]["current_reviews"] == existing_reviews


# ── TestCmdSpecStatus ─────────────────────────────────────────────────────────


class TestCmdSpecStatus:

    def test_status_basic(self, tmp_pipelines, capsys):
        """status 出力フォーマットの確認"""
        path = tmp_pipelines / "test-pj.json"
        sc = _make_spec_config(
            spec_path="docs/spec.md",
            spec_implementer="implementer1",
            current_rev="3",
            rev_index=2,
            max_revise_cycles=5,
            retry_counts={"SPEC_REVIEW": 1},
            review_requests={
                "reviewer1": {"status": "pending", "sent_at": None, "timeout_at": None,
                           "last_nudge_at": None, "response": None},
            },
            pipelines_dir="/tmp/spec-reviews",
        )
        write_pipeline(path, _make_pipeline(
            state="SPEC_REVIEW", spec_mode=True, spec_config=sc, review_mode="full"
        ))

        from commands.spec import cmd_spec_status
        cmd_spec_status(_args(project="test-pj"))

        captured = capsys.readouterr()
        assert "SPEC_REVIEW" in captured.out
        assert "rev3" in captured.out
        assert "cycle rev2/5" in captured.out
        assert "docs/spec.md" in captured.out
        assert "implementer1" in captured.out
        assert "reviewer1" in captured.out
        assert "/tmp/spec-reviews" in captured.out

    def test_status_not_active(self, tmp_pipelines, capsys):
        """spec_mode=False の時の 'not active' メッセージ"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE", spec_mode=False))

        from commands.spec import cmd_spec_status
        cmd_spec_status(_args(project="test-pj"))

        captured = capsys.readouterr()
        assert "not active" in captured.out


# ── TestCmdSpecStartTOCTOU (BA-025) ──────────────────────────────────────────


class TestCmdSpecStartTOCTOU:
    """BA-025: cmd_spec_start の update_pipeline 統合テスト"""

    @pytest.fixture(autouse=True)
    def _spec_file(self, tmp_pipelines, monkeypatch):
        (tmp_pipelines / "docs").mkdir(exist_ok=True)
        (tmp_pipelines / "docs" / "spec.md").write_text("# test spec")
        monkeypatch.chdir(tmp_pipelines)

    def _base_args(self, **overrides):
        defaults = dict(
            project="test-pj", spec="docs/spec.md", implementer="implementer1",
            review_only=False, no_queue=False, skip_review=False,
            max_cycles=None, review_mode=None, model=None,
            auto_continue=False, auto_qrun=False,
        )
        defaults.update(overrides)
        return _args(**defaults)

    def test_single_update_with_excluded(self, tmp_pipelines):
        """Test A: excluded ありで update_pipeline が1回、excluded_reviewers と spec_config が含まれる"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE"))

        from commands.spec import cmd_spec_start

        call_count = 0
        orig_update = __import__("pipeline_io").update_pipeline

        def counting_update(p, cb):
            nonlocal call_count
            call_count += 1
            return orig_update(p, cb)

        # reviewer1 を excluded として返す
        with patch("gokrax._start_loop"), \
             patch("engine.reviewer._reset_reviewers", return_value=["reviewer1"]), \
             patch("commands.spec.update_pipeline", side_effect=counting_update):
            cmd_spec_start(self._base_args())

        assert call_count == 1
        data = json.loads(path.read_text())
        assert data["excluded_reviewers"] == ["reviewer1"]
        assert "spec_config" in data
        assert "reviewer1" not in data["spec_config"]["review_requests"]

    def test_single_update_no_excluded(self, tmp_pipelines):
        """Test B: excluded なしで update_pipeline が1回、excluded_reviewers キーなし"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE"))

        from commands.spec import cmd_spec_start

        call_count = 0
        orig_update = __import__("pipeline_io").update_pipeline

        def counting_update(p, cb):
            nonlocal call_count
            call_count += 1
            return orig_update(p, cb)

        with patch("gokrax._start_loop"), \
             patch("engine.reviewer._reset_reviewers", return_value=[]), \
             patch("commands.spec.update_pipeline", side_effect=counting_update):
            cmd_spec_start(self._base_args())

        assert call_count == 1
        data = json.loads(path.read_text())
        assert "excluded_reviewers" not in data

    def test_clears_stale_excluded_and_min_reviews_override(self, tmp_pipelines):
        """Test C: 前回 run の excluded_reviewers と min_reviews_override がクリアされる"""
        path = tmp_pipelines / "test-pj.json"
        stale = _make_pipeline(state="IDLE")
        stale["excluded_reviewers"] = ["old_reviewer"]
        stale["min_reviews_override"] = 2
        write_pipeline(path, stale)

        from commands.spec import cmd_spec_start

        with patch("gokrax._start_loop"), \
             patch("engine.reviewer._reset_reviewers", return_value=[]):
            cmd_spec_start(self._base_args())

        data = json.loads(path.read_text())
        assert "excluded_reviewers" not in data
        assert "min_reviews_override" not in data

    def test_min_reviews_override_boundary(self, tmp_pipelines):
        """Test D: effective_count < min_reviews で min_reviews_override が設定される"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE"))

        from commands.spec import cmd_spec_start

        # full mode: members=["reviewer1","reviewer3","reviewer5","reviewer6"], min_reviews=4
        # reviewer1, reviewer3 を excluded → effective_count=2 < 4
        with patch("gokrax._start_loop"), \
             patch("engine.reviewer._reset_reviewers", return_value=["reviewer1", "reviewer3"]):
            cmd_spec_start(self._base_args())

        data = json.loads(path.read_text())
        assert data["min_reviews_override"] == 2  # max(1, 4-2=2)

    def test_skip_review_no_reset_reviewers_call(self, tmp_pipelines):
        """skip_review=True で _reset_reviewers が呼ばれないことを検証"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE"))

        from commands.spec import cmd_spec_start

        with patch("gokrax._start_loop"), \
             patch("engine.reviewer._reset_reviewers") as mock_reset:
            cmd_spec_start(self._base_args(skip_review=True))

        mock_reset.assert_not_called()
        data = json.loads(path.read_text())
        assert "excluded_reviewers" not in data
        assert data["state"] == "SPEC_APPROVED"

    def test_min_reviews_override_not_set_when_sufficient(self, tmp_pipelines):
        """Test D(後半): effective_count >= min_reviews で min_reviews_override なし"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="IDLE", review_mode="lite"))

        from commands.spec import cmd_spec_start

        # lite mode: members=["reviewer1","reviewer3"], min_reviews=2
        # implementer のみ excluded（mode members 外）→ excluded_reviewers_only は空
        with patch("gokrax._start_loop"), \
             patch("engine.reviewer._reset_reviewers", return_value=["implementer1"]):
            cmd_spec_start(self._base_args(review_mode="lite"))

        data = json.loads(path.read_text())
        assert "min_reviews_override" not in data


# ── TestCmdSpecStopTOCTOU (BA-037) ───────────────────────────────────────────


class TestCmdSpecStopTOCTOU:
    """BA-037: cmd_spec_stop の old_state がロック内で取得されるテスト"""

    def test_old_state_from_lock_inner_data(self, tmp_pipelines):
        """Test E: add_history にはロック内の state が使われる"""
        path = tmp_pipelines / "test-pj.json"
        # ロック外の load_pipeline では SPEC_REVIEW を返す
        write_pipeline(path, _make_active_pipeline(state="SPEC_REVIEW"))

        from commands.spec import cmd_spec_stop

        orig_update = __import__("pipeline_io").update_pipeline

        def intercept_update(p, cb):
            def wrapper(data):
                # ロック内で state を SPEC_REVISE に差し替え（watchdog が遷移した想定）
                data["state"] = "SPEC_REVISE"
                cb(data)
            return orig_update(p, wrapper)

        with patch("commands.spec.update_pipeline", side_effect=intercept_update), \
             patch("gokrax._any_pj_enabled", return_value=False), \
             patch("gokrax._stop_loop"):
            cmd_spec_stop(_args(project="test-pj"))

        data = json.loads(path.read_text())
        # add_history に記録された old_state はロック内の SPEC_REVISE であるべき
        hist = data["history"]
        stop_entry = [h for h in hist if h.get("actor") == "cli:spec-stop"]
        assert len(stop_entry) == 1
        assert stop_entry[0]["from"] == "SPEC_REVISE"
        assert stop_entry[0]["to"] == "IDLE"
