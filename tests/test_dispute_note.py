"""Test dispute GitLab note formatting."""

from unittest.mock import patch, MagicMock
import pytest


def test_dispute_note_format(tmp_path):
    """dispute note の Reason が太字ラベル + 段落分離されている。"""
    from commands.dev import cmd_dispute

    # pipeline JSON を用意
    import json
    pipeline = {
        "state": "DESIGN_REVISE",
        "batch": [
            {
                "issue": 99,
                "design_reviews": {
                    "reviewer_a": {"verdict": "P0", "summary": "bad", "at": "2026-01-01T00:00:00+09:00"}
                },
                "disputes": [],
            }
        ],
        "gitlab": "test/repo",
    }
    pj_path = tmp_path / "pipeline.json"
    pj_path.write_text(json.dumps(pipeline))

    args = MagicMock()
    args.project = "test"
    args.issue = 99
    args.reviewer = "reviewer_a"
    args.reason = "This is wrong because X.\nAlso Y is incorrect."

    posted_notes: list[str] = []

    with (
        patch("commands.dev.get_path", return_value=pj_path),
        patch("commands.dev.update_pipeline") as mock_update,
        patch("commands.dev._post_gitlab_note", side_effect=lambda g, i, body: (posted_notes.append(body), True)[-1]) as mock_note,
        patch("commands.dev.send_to_agent_queued", return_value=True),
        patch("commands.dev.find_issue", return_value=pipeline["batch"][0]),
        patch("commands.dev.mask_agent_name", return_value="Reviewer 1"),
    ):
        mock_update.return_value = pipeline
        cmd_dispute(args)

    assert len(posted_notes) == 1
    note = posted_notes[0]
    # ヘッダと Reason ラベルが段落分離されている
    assert "**Reason:**" in note
    # Reason ラベルと本文が別段落（\n\n で区切り）
    assert "**Reason:**\n\n" in note
    # 理由テキストが含まれている
    assert "This is wrong because X." in note


# ---------------------------------------------------------------------------
# Issue #400: boost — dispute send / boost_sent / [boost] tag / cross-issue regression
# ---------------------------------------------------------------------------

def _boost_pipeline(tmp_pipelines, *, issues=(1, 2), state="DESIGN_REVISE"):
    import json
    from engine.fsm import _build_phase_config
    mc = {"members": ["reviewer1"], "min_reviews": 1, "grace_period_sec": 0,
          "design": {"boost": True}}
    key = "design_reviews" if "DESIGN" in state else "code_reviews"
    data = {
        "project": "test-pj", "gitlab": "testns/test-pj", "state": state,
        "enabled": True, "implementer": "implementer1", "review_mode": "min",
        "boost": True,
        "review_config": {"design": _build_phase_config(mc, "design"),
                          "code": _build_phase_config(mc, "code")},
        "batch": [{"issue": n, "title": f"t{n}", "commit": None,
                   "design_reviews": {}, "code_reviews": {},
                   key: {"reviewer1": {"verdict": "P1", "at": "2025-01-01T00:00:00+09:00",
                                       "summary": "bad"}}}
                  for n in issues],
        "history": [{"from": state.replace("REVISE", "REVIEW"), "to": state,
                     "at": "2025-01-01T00:00:00+09:00", "actor": "watchdog"}],
    }
    path = tmp_pipelines / "test-pj.json"
    path.write_text(json.dumps(data))
    return path


def _fake_resolve_factory(results: list[bool]):
    from engine.agent_meta import AgentMeta

    def fake_resolve(agent_id, boost=False):
        boosted = results.pop(0) if boost and results else False
        return AgentMeta(backend="cc", provider="anthropic", model="m",
                         think_level="", captured_at="t", boosted=boosted)
    return fake_resolve


def _dispute(issue: int, notes: list[str], sends: list):
    import argparse
    from commands.dev import cmd_dispute
    args = argparse.Namespace(project="test-pj", issue=issue, reviewer="reviewer1",
                              reason="not a bug")
    with patch("commands.dev._post_gitlab_note",
               side_effect=lambda g, i, body: notes.append(body) or True), \
         patch("commands.dev.send_to_agent_queued",
               side_effect=lambda r, m, **kw: sends.append(kw) or True):
        cmd_dispute(args)


def _review(issue: int, notes: list[str]):
    import argparse
    from commands.dev import cmd_review
    args = argparse.Namespace(project="test-pj", issue=issue, reviewer="reviewer1",
                              verdict="P1", summary="still bad", force=True,
                              round=None, phase=None)
    with patch("commands.dev._post_gitlab_note",
               side_effect=lambda g, i, body: notes.append(body) or True), \
         patch("pipeline_io.append_metric") as mm:
        cmd_review(args)
    return [c for c in mm.call_args_list if c.args[0] == "review_response"]


@pytest.mark.parametrize("mask", [True, False])
def test_dispute_boost_send_and_record(tmp_pipelines, monkeypatch, mask):
    import json
    from notify import mask_agent_name
    monkeypatch.setattr("config.MASK_AGENT_NAMES", mask)
    path = _boost_pipeline(tmp_pipelines, issues=(1,))
    raw = json.loads(path.read_text())
    raw["batch"][0]["boost_sent"] = {"design": {"reviewer1": True}}
    path.write_text(json.dumps(raw))
    monkeypatch.setattr("engine.agent_meta._resolve", _fake_resolve_factory([False]))
    notes: list[str] = []
    sends: list = []
    _dispute(1, notes, sends)
    assert sends == [{"boost": True}]
    saved = json.loads(path.read_text())
    # record after send overwrites with this send's result
    assert saved["batch"][0]["boost_sent"]["design"]["reviewer1"] is False
    assert saved["reviewer_meta"]["reviewer1"]["boosted"] is False
    # note reflects the disputed (earlier) review, which was boosted
    masked = mask_agent_name("reviewer1")
    assert notes[0].startswith(f"[dispute] #1: {masked} [boost]'s verdict disputed")


def test_dispute_note_no_boost(tmp_pipelines, monkeypatch):
    _boost_pipeline(tmp_pipelines, issues=(1,))
    monkeypatch.setattr("engine.agent_meta._resolve", _fake_resolve_factory([True]))
    notes: list[str] = []
    _dispute(1, notes, [])
    assert "[boost]" not in notes[0]


def test_dispute_code_revise_not_boosted(tmp_pipelines, monkeypatch):
    import json
    path = _boost_pipeline(tmp_pipelines, issues=(1,), state="CODE_REVISE")
    monkeypatch.setattr("engine.agent_meta._resolve", _fake_resolve_factory([True]))
    sends: list = []
    _dispute(1, [], sends)
    assert sends == [{"boost": False}]
    saved = json.loads(path.read_text())
    assert saved["batch"][0]["boost_sent"] == {"code": {"reviewer1": False}}


@pytest.mark.parametrize("mask", [True, False])
def test_cross_issue_boost_not_overwritten(tmp_pipelines, monkeypatch, mask):
    """#1 boosted dispute, #2 non-boosted dispute → #1's answer still tagged [boost]."""
    import json
    monkeypatch.setattr("config.MASK_AGENT_NAMES", mask)
    path = _boost_pipeline(tmp_pipelines)
    monkeypatch.setattr("engine.agent_meta._resolve", _fake_resolve_factory([True, False]))
    _dispute(1, [], [])
    _dispute(2, [], [])
    saved = json.loads(path.read_text())
    assert saved["batch"][0]["boost_sent"]["design"]["reviewer1"] is True
    assert saved["batch"][1]["boost_sent"]["design"]["reviewer1"] is False

    notes1: list[str] = []
    m1 = _review(1, notes1)
    assert len(notes1) == 1 and "] [boost] P1 (design review)" in notes1[0]
    assert m1[0].kwargs.get("boosted") is True

    notes2: list[str] = []
    m2 = _review(2, notes2)
    assert len(notes2) == 1 and "[boost]" not in notes2[0]
    assert "boosted" not in m2[0].kwargs
