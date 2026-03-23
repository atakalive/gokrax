"""tests/test_npass_base.py — Nパスレビュー基盤テスト (Issue #176)"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.states import VALID_TRANSITIONS, STATE_PHASE_MAP  # noqa: E402


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_pipeline(tmp_pipelines: Path, state: str = "CODE_REVIEW",
                   review_mode: str = "standard",
                   existing_reviews: dict | None = None) -> Path:
    """パイプラインJSONを作成。"""
    data = {
        "project": "test-pj",
        "gitlab": "atakalive/test-pj",
        "state": state,
        "enabled": True,
        "implementer": "kaneko",
        "review_mode": review_mode,
        "batch": [
            {
                "issue": 1,
                "title": "Test Issue",
                "commit": "abc123" if "CODE" in state else None,
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
    if existing_reviews:
        key = "code_reviews" if "CODE" in state else "design_reviews"
        data["batch"][0][key] = existing_reviews
    path = tmp_pipelines / "test-pj.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path


# ---------------------------------------------------------------------------
# 1. VALID_TRANSITIONS に NPASS ステートが存在する
# ---------------------------------------------------------------------------

class TestNpassTransitions:

    def test_design_review_npass_in_transitions(self):
        assert "DESIGN_REVIEW_NPASS" in VALID_TRANSITIONS

    def test_code_review_npass_in_transitions(self):
        assert "CODE_REVIEW_NPASS" in VALID_TRANSITIONS

    def test_design_review_can_transition_to_npass(self):
        assert "DESIGN_REVIEW_NPASS" in VALID_TRANSITIONS["DESIGN_REVIEW"]

    def test_code_review_can_transition_to_npass(self):
        assert "CODE_REVIEW_NPASS" in VALID_TRANSITIONS["CODE_REVIEW"]


# ---------------------------------------------------------------------------
# 2. NPASS → APPROVED / REVISE / 自己遷移が有効
# ---------------------------------------------------------------------------

class TestNpassAllowedTransitions:

    def test_design_npass_to_approved(self):
        assert "DESIGN_APPROVED" in VALID_TRANSITIONS["DESIGN_REVIEW_NPASS"]

    def test_design_npass_to_revise(self):
        assert "DESIGN_REVISE" in VALID_TRANSITIONS["DESIGN_REVIEW_NPASS"]

    def test_design_npass_self_transition(self):
        assert "DESIGN_REVIEW_NPASS" in VALID_TRANSITIONS["DESIGN_REVIEW_NPASS"]

    def test_code_npass_to_approved(self):
        assert "CODE_APPROVED" in VALID_TRANSITIONS["CODE_REVIEW_NPASS"]

    def test_code_npass_to_revise(self):
        assert "CODE_REVISE" in VALID_TRANSITIONS["CODE_REVIEW_NPASS"]

    def test_code_npass_self_transition(self):
        assert "CODE_REVIEW_NPASS" in VALID_TRANSITIONS["CODE_REVIEW_NPASS"]


# ---------------------------------------------------------------------------
# 3. NPASS → BLOCKED の遷移が無効
# ---------------------------------------------------------------------------

class TestNpassNoBlocked:

    def test_design_npass_no_blocked(self):
        assert "BLOCKED" not in VALID_TRANSITIONS["DESIGN_REVIEW_NPASS"]

    def test_code_npass_no_blocked(self):
        assert "BLOCKED" not in VALID_TRANSITIONS["CODE_REVIEW_NPASS"]


# ---------------------------------------------------------------------------
# 4. STATE_PHASE_MAP に NPASS が含まれる
# ---------------------------------------------------------------------------

class TestNpassPhaseMap:

    def test_design_npass_phase(self):
        assert STATE_PHASE_MAP["DESIGN_REVIEW_NPASS"] == "design"

    def test_code_npass_phase(self):
        assert STATE_PHASE_MAP["CODE_REVIEW_NPASS"] == "code"


# ---------------------------------------------------------------------------
# 5. review_entry に pass / target_pass が記録される
# ---------------------------------------------------------------------------

class TestNpassReviewEntry:

    def test_review_entry_has_pass_and_target_pass(self, tmp_pipelines, monkeypatch):
        """cmd_review 実行後、review_entry に pass==1, target_pass が REVIEW_MODES から取得した値と一致。"""
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW", review_mode="npass-test")

        # n_pass 付きモードを設定
        monkeypatch.setattr("config.REVIEW_MODES", {
            "npass-test": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
                "n_pass": {"pascal": 2},
            },
            "standard": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
            },
        })

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="APPROVE",
            summary="LGTM",
            force=False,
        )
        ok_result = MagicMock()
        ok_result.returncode = 0
        ok_result.stderr = ""

        with patch("commands.dev.subprocess.run", return_value=ok_result), \
             patch("commands.dev.time.sleep"):
            gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        entry = data["batch"][0]["code_reviews"]["pascal"]
        assert entry["pass"] == 1
        assert entry["target_pass"] == 2


# ---------------------------------------------------------------------------
# 6. 冪等性ガードが NPASS パス2を許可する
# ---------------------------------------------------------------------------

class TestNpassIdempotencyBypass:

    def test_npass_allows_overwrite_when_pass_lt_target(self, tmp_pipelines, monkeypatch, capsys):
        """pass: 1, target_pass: 2 の状態で --force なしで cmd_review を実行 → スキップされない。"""
        existing = {"pascal": {"verdict": "APPROVE", "at": "2025-01-01T00:00:00+09:00", "pass": 1, "target_pass": 2}}
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW", review_mode="npass-test",
                       existing_reviews=existing)

        monkeypatch.setattr("config.REVIEW_MODES", {
            "npass-test": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
                "n_pass": {"pascal": 2},
            },
            "standard": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
            },
        })

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="APPROVE",
            summary="2nd pass",
            force=False,
        )
        ok_result = MagicMock()
        ok_result.returncode = 0
        ok_result.stderr = ""

        with patch("commands.dev.subprocess.run", return_value=ok_result), \
             patch("commands.dev.time.sleep"):
            gokrax.cmd_review(args)

        # レビューが更新されたことを確認（スキップされていない）
        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        entry = data["batch"][0]["code_reviews"]["pascal"]
        assert entry["pass"] == 2
        assert entry["summary"] == "2nd pass"

        captured = capsys.readouterr()
        assert "already reviewed" not in captured.out


# ---------------------------------------------------------------------------
# 7. APPROVE の中間パスで GitLab note がスキップされる
# ---------------------------------------------------------------------------

class TestNpassGitlabNoteSkip:

    def test_approve_intermediate_pass_skips_gitlab_note(self, tmp_pipelines, monkeypatch, capsys):
        """初回パス(pass=1, target_pass=2) + verdict=APPROVE → _post_gitlab_note が呼ばれない。"""
        # 既存レビューなし → cmd_review で pass=1, target_pass=2 が書き込まれる
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW", review_mode="npass-test")

        monkeypatch.setattr("config.REVIEW_MODES", {
            "npass-test": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
                "n_pass": {"pascal": 2},
            },
            "standard": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
            },
        })

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="APPROVE",
            summary="",
            force=False,
        )

        mock_note = MagicMock(return_value=True)
        with patch("commands.dev._post_gitlab_note", mock_note), \
             patch("commands.dev.time.sleep"):
            gokrax.cmd_review(args)

        mock_note.assert_not_called()
        captured = capsys.readouterr()
        assert "GitLab note skipped" in captured.out


# ---------------------------------------------------------------------------
# 8. P0/P1/P2 の中間パスで GitLab note が投稿される
# ---------------------------------------------------------------------------

class TestNpassGitlabNotePosted:

    def test_p1_intermediate_pass_posts_gitlab_note(self, tmp_pipelines, monkeypatch):
        """初回パス(pass=1, target_pass=2) + verdict=P1 → _post_gitlab_note が呼ばれる。"""
        # 既存レビューなし → cmd_review で pass=1, target_pass=2 が書き込まれる
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW", review_mode="npass-test")

        monkeypatch.setattr("config.REVIEW_MODES", {
            "npass-test": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
                "n_pass": {"pascal": 2},
            },
            "standard": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
            },
        })

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="P1",
            summary="issue found",
            force=False,
        )

        mock_note = MagicMock(return_value=True)
        with patch("commands.dev._post_gitlab_note", mock_note), \
             patch("commands.dev.time.sleep"):
            gokrax.cmd_review(args)

        mock_note.assert_called_once()


# ---------------------------------------------------------------------------
# 9. pass == target_pass で GitLab note が投稿される
# ---------------------------------------------------------------------------

class TestNpassFinalPassNote:

    def test_final_pass_posts_gitlab_note(self, tmp_pipelines, monkeypatch):
        """pass: 1, target_pass: 1 → _post_gitlab_note が呼ばれる。"""
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW", review_mode="npass-test")

        monkeypatch.setattr("config.REVIEW_MODES", {
            "npass-test": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
            },
            "standard": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
            },
        })

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="APPROVE",
            summary="LGTM",
            force=False,
        )

        mock_note = MagicMock(return_value=True)
        with patch("commands.dev._post_gitlab_note", mock_note), \
             patch("commands.dev.time.sleep"):
            gokrax.cmd_review(args)

        mock_note.assert_called_once()


# ---------------------------------------------------------------------------
# 10. n_pass 未設定のレビューモードでは target_pass == 1
# ---------------------------------------------------------------------------

class TestNpassDefaultTargetPass:

    def test_no_npass_config_defaults_to_1(self, tmp_pipelines, monkeypatch):
        """REVIEW_MODES に n_pass キーなし → target_pass == 1。"""
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW", review_mode="standard")

        monkeypatch.setattr("config.REVIEW_MODES", {
            "standard": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
            },
        })

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="APPROVE",
            summary="LGTM",
            force=False,
        )

        ok_result = MagicMock()
        ok_result.returncode = 0
        ok_result.stderr = ""

        with patch("commands.dev.subprocess.run", return_value=ok_result), \
             patch("commands.dev.time.sleep"):
            gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        entry = data["batch"][0]["code_reviews"]["pascal"]
        assert entry["target_pass"] == 1


# ---------------------------------------------------------------------------
# 11. n_pass の不正値バリデーション
# ---------------------------------------------------------------------------

class TestNpassValidation:

    @pytest.mark.parametrize("bad_value", [0, -1, "two"])
    def test_invalid_npass_defaults_to_1(self, tmp_pipelines, monkeypatch, bad_value, capsys):
        """n_pass に不正値 → target_pass が 1 にフォールバック。"""
        _make_pipeline(tmp_pipelines, state="CODE_REVIEW", review_mode="bad-mode")

        monkeypatch.setattr("config.REVIEW_MODES", {
            "bad-mode": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
                "n_pass": {"pascal": bad_value},
            },
            "standard": {
                "members": ["pascal"],
                "min_reviews": 1,
                "grace_period_sec": 0,
            },
        })

        import gokrax
        args = argparse.Namespace(
            project="test-pj",
            issue=1,
            reviewer="pascal",
            verdict="APPROVE",
            summary="",
            force=False,
        )

        ok_result = MagicMock()
        ok_result.returncode = 0
        ok_result.stderr = ""

        with patch("commands.dev.subprocess.run", return_value=ok_result), \
             patch("commands.dev.time.sleep"):
            gokrax.cmd_review(args)

        path = tmp_pipelines / "test-pj.json"
        data = json.loads(path.read_text())
        entry = data["batch"][0]["code_reviews"]["pascal"]
        assert entry["target_pass"] == 1

        captured = capsys.readouterr()
        assert "WARNING" in captured.out or "invalid" in captured.out


# ---------------------------------------------------------------------------
# 12. NPASS interception — Round 1 vs Round 2+ (Issue #182)
# ---------------------------------------------------------------------------

class TestNpassRoundGuard:
    """NPASS interception が Round 1 のみ発火し、Round 2+ ではスキップされる。"""

    def _now_iso(self) -> str:
        from datetime import datetime, timedelta, timezone
        JST = timezone(timedelta(hours=9))
        return datetime.now(JST).isoformat()

    def test_npass_fires_on_round1_with_p1_verdict(self):
        """Round 1（revise_count=0）で P1 verdict があっても NPASS に遷移する。"""
        from engine.fsm import check_transition
        now = self._now_iso()
        batch = [{
            "issue": 1,
            "design_reviews": {
                "euler": {"verdict": "P1", "at": now, "pass": 1, "target_pass": 1},
                "pascal": {"verdict": "APPROVE", "at": now, "pass": 1, "target_pass": 2},
            },
        }]
        data = {
            "project": "test-pj",
            "review_mode": "npass_mode",
            "design_revise_count": 0,
            "history": [{"from": "DESIGN_PLAN", "to": "DESIGN_REVIEW", "at": now}],
        }
        with patch("engine.fsm.REVIEW_MODES", {
            "npass_mode": {"members": ["euler", "pascal"], "min_reviews": 2, "grace_period_sec": 0},
            "standard": {"members": ["euler", "pascal"], "min_reviews": 2, "grace_period_sec": 0},
        }):
            action = check_transition("DESIGN_REVIEW", batch, data)
        assert action.new_state == "DESIGN_REVIEW_NPASS"
        assert action.npass_target_reviewers == ["pascal"]

    def test_npass_skipped_on_round2(self):
        """Round 2+（revise_count > 0）では NPASS をスキップして直接 APPROVE する。"""
        from engine.fsm import check_transition
        now = self._now_iso()
        batch = [{
            "issue": 1,
            "design_reviews": {
                "euler": {"verdict": "APPROVE", "at": now, "pass": 1, "target_pass": 1},
                "pascal": {"verdict": "APPROVE", "at": now, "pass": 1, "target_pass": 2},
            },
        }]
        data = {
            "project": "test-pj",
            "review_mode": "npass_mode",
            "design_revise_count": 1,
            "history": [{"from": "DESIGN_REVISE", "to": "DESIGN_REVIEW", "at": now}],
        }
        with patch("engine.fsm.REVIEW_MODES", {
            "npass_mode": {"members": ["euler", "pascal"], "min_reviews": 2, "grace_period_sec": 0},
            "standard": {"members": ["euler", "pascal"], "min_reviews": 2, "grace_period_sec": 0},
        }):
            action = check_transition("DESIGN_REVIEW", batch, data)
        assert action.new_state == "DESIGN_APPROVED"

    def test_code_npass_skipped_on_round2(self):
        """CODE_REVIEW Round 2+ でも NPASS をスキップする。"""
        from engine.fsm import check_transition
        now = self._now_iso()
        batch = [{
            "issue": 1,
            "code_reviews": {
                "euler": {"verdict": "APPROVE", "at": now, "pass": 1, "target_pass": 1},
                "dijkstra": {"verdict": "APPROVE", "at": now, "pass": 1, "target_pass": 2},
            },
        }]
        data = {
            "project": "test-pj",
            "review_mode": "npass_mode",
            "code_revise_count": 1,
            "history": [{"from": "CODE_REVISE", "to": "CODE_REVIEW", "at": now}],
        }
        with patch("engine.fsm.REVIEW_MODES", {
            "npass_mode": {"members": ["euler", "dijkstra"], "min_reviews": 2, "grace_period_sec": 0},
            "standard": {"members": ["euler", "dijkstra"], "min_reviews": 2, "grace_period_sec": 0},
        }):
            action = check_transition("CODE_REVIEW", batch, data)
        assert action.new_state == "CODE_APPROVED"
