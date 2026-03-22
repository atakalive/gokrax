"""tests/test_assessment_prompt.py — ASSESSMENT 判定プロンプトと自動依頼 (Issue #170)"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_batch(n=1, **kwargs):
    items = []
    for i in range(1, n + 1):
        item = {
            "issue": i, "title": f"Issue {i}", "commit": None,
            "cc_session_id": None,
            "design_reviews": {}, "code_reviews": {},
            "added_at": "2025-01-01T00:00:00+09:00",
        }
        item.update(kwargs)
        items.append(item)
    return items


class TestAssessmentTransitionMessage:

    def test_assessment_transition_message_ja(self):
        """6-1: 日本語プロンプトに必須要素が含まれる"""
        from messages import render
        msg = render("dev.assessment", "transition",
            project="test", issues_str="#1, #2",
            comment_line="", GOKRAX_CLI="gokrax", lang="ja",
        )
        assert "難易度判定フェーズ" in msg
        for lvl in range(1, 6):
            assert f"Lvl {lvl}" in msg
        assert "assess-done --project test --level N" in msg

    def test_assessment_transition_message_en(self):
        """6-2: 英語プロンプトに必須要素が含まれる"""
        from messages import render
        msg = render("dev.assessment", "transition",
            project="test", issues_str="#1, #2",
            comment_line="", GOKRAX_CLI="gokrax", lang="en",
        )
        assert "assessment phase" in msg
        for lvl in range(1, 6):
            assert f"Lvl {lvl}" in msg
        assert "assess-done --project test --level N" in msg

    def test_assessment_nudge_ja(self):
        """6-3: 日本語催促に assess-done が含まれる"""
        from messages import render
        msg = render("dev.assessment", "nudge", lang="ja")
        assert "assess-done" in msg

    def test_assessment_nudge_en(self):
        """6-4: 英語催促に assess-done が含まれる"""
        from messages import render
        msg = render("dev.assessment", "nudge", lang="en")
        assert "assess-done" in msg


class TestAssessmentNotification:

    def test_get_notification_for_state_assessment(self):
        """6-5: get_notification_for_state で ASSESSMENT が impl_msg を返す"""
        from engine.fsm import get_notification_for_state
        action = get_notification_for_state(
            "ASSESSMENT", project="test", batch=[{"issue": 1}],
        )
        assert action.impl_msg is not None

    def test_design_approved_to_assessment_has_impl_msg(self):
        """6-6: DESIGN_APPROVED → ASSESSMENT 遷移で impl_msg が設定される"""
        from engine.fsm import check_transition
        data = {"project": "test"}
        action = check_transition("DESIGN_APPROVED", _make_batch(), data)
        assert action.new_state == "ASSESSMENT"
        assert action.impl_msg is not None

    def test_assessment_comment_line(self):
        """6-7: comment_line が出力に含まれる"""
        from messages import render
        msg = render("dev.assessment", "transition",
            project="test", issues_str="#1",
            comment_line="オーナーからの要望: テスト要望\n",
            GOKRAX_CLI="gokrax", lang="ja",
        )
        assert "オーナーからの要望: テスト要望" in msg
