"""Tests for engine/progress.py (#382)."""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import config
from notify import DiscordPostResult


# ---------------------------------------------------------------------------
# ClaudeJsonlReader.count_tool_calls — direct
# ---------------------------------------------------------------------------

def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class TestClaudeJsonlReaderCount:

    def _write_transcript(self, path: Path) -> int:
        line1 = json.dumps({
            "type": "assistant",
            "timestamp": "2026-06-17T01:00:00.000Z",
            "message": {"content": [{"type": "tool_use"}, {"type": "text", "text": "hi"}]},
        })
        line2 = json.dumps({
            "type": "user",
            "message": {"content": [{"type": "tool_result"}]},
        })
        line3 = json.dumps({
            "type": "assistant",
            "timestamp": "2026-06-17T02:00:00.000Z",
            "message": {"content": [{"type": "tool_use"}, {"type": "tool_use"}]},
        })
        partial = '{"type":"assistant"'  # no trailing newline
        content = line1 + "\n" + line2 + "\n" + line3 + "\n" + partial
        path.write_text(content, encoding="utf-8")
        # offset of the byte after line3's newline
        return len((line1 + "\n" + line2 + "\n" + line3 + "\n").encode("utf-8"))

    def test_increment_count_and_offset_advance(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        p = tmp_path / "t.jsonl"
        expected_offset = self._write_transcript(p)
        reader = ClaudeJsonlReader(tmp_path)

        count, offset = reader.count_tool_calls(p, 0, since_epoch=None)
        assert count == 3  # 1 + 2 tool_use; partial line excluded
        assert offset == expected_offset

        # Re-read from new offset: only the trailing partial remains → no progress.
        count2, offset2 = reader.count_tool_calls(p, offset, since_epoch=None)
        assert count2 == 0
        assert offset2 == offset

    def test_since_epoch_filters_old_entries(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        p = tmp_path / "t.jsonl"
        self._write_transcript(p)
        reader = ClaudeJsonlReader(tmp_path)
        threshold = _ts("2026-06-17T01:30:00.000Z")
        count, _ = reader.count_tool_calls(p, 0, since_epoch=threshold)
        assert count == 2  # only the 02:00 entry (2 tool_use) passes

    def test_read_error_returns_none(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        reader = ClaudeJsonlReader(tmp_path)
        missing = tmp_path / "nope.jsonl"
        assert reader.count_tool_calls(missing, 0, since_epoch=None) is None


class TestClaudeJsonlReaderResolve:

    def test_missing_session_id_returns_none(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        reader = ClaudeJsonlReader(tmp_path)
        assert reader.resolve("agent1", "/some/repo") is None

    def test_predicted_path_resolution(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        sid = "12345678-1234-1234-1234-123456789abc"
        sessions = tmp_path / "sessions"
        (sessions / "agent1").mkdir(parents=True)
        (sessions / "agent1" / "session_id").write_text(sid, encoding="utf-8")

        home = tmp_path / "home"
        repo = tmp_path / "repo"
        repo.mkdir()
        key = str(repo.resolve()).replace("/", "-")
        proj = home / ".claude" / "projects" / key
        proj.mkdir(parents=True)
        transcript = proj / f"{sid}.jsonl"
        transcript.write_text("{}", encoding="utf-8")

        reader = ClaudeJsonlReader(sessions)
        with patch("engine.progress.Path.home", return_value=home):
            resolved = reader.resolve("agent1", str(repo))
        assert resolved == transcript


# ---------------------------------------------------------------------------
# update_phase_progress — orchestration
# ---------------------------------------------------------------------------

NOW = 2000.0
STARTED = 1880.0  # entered.timestamp(); elapsed = 120s


class _FakeReader:
    def __init__(self, path, counts):
        self.path = path
        self.counts = list(counts)
        self.calls = []

    def resolve(self, agent_id, repo_path):
        return self.path

    def count_tool_calls(self, path, offset, since_epoch):
        self.calls.append((offset, since_epoch))
        return self.counts.pop(0)


@contextmanager
def _patched(reader, entered, now=NOW, post_result=None, edit_result="ok",
             backend="faketest"):
    if post_result is None:
        post_result = DiscordPostResult("new-id")
    with patch("engine.progress.resolve_backend", return_value=backend), \
         patch("engine.progress.get_reader", return_value=reader), \
         patch("engine.progress._get_state_entered_at", return_value=entered), \
         patch("engine.progress.time.time", return_value=now), \
         patch("notify.post_discord", return_value=post_result) as post_mock, \
         patch("notify.edit_discord_message", return_value=edit_result) as edit_mock, \
         patch.object(config, "PROGRESS_NOTIFY", True), \
         patch.object(config, "DISCORD_CHANNEL", "chan-123"):
        yield post_mock, edit_mock


def _entered(ts=STARTED):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _base_data(**over):
    d = {
        "project": "proj",
        "enabled": True,
        "implementer": "impl1",
        "repo_path": "/x",
        "state": "DESIGN_PLAN",
    }
    d.update(over)
    return d


class TestUpdatePhaseProgress:

    def test_a_first_tick_establishes_and_posts(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x", encoding="utf-8")
        data = _base_data()
        _write(pf, data)
        reader = _FakeReader(transcript, [(5, 100)])

        with _patched(reader, _entered()) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)

        assert post.call_count == 1
        edit.assert_not_called()
        text = post.call_args.args[1]
        assert "DESIGN_PLAN in progress" in text
        assert "5 tool calls" in text
        assert "avg 2.5/min" in text
        # establish read: offset 0, since_epoch = phase_start
        assert reader.calls == [(0, STARTED)]
        out = _read(pf)
        assert out["progress_count"] == 5
        assert out["progress_offset"] == 100
        assert out["progress_msg_id"] == "new-id"
        assert out["progress_phase"] == "DESIGN_PLAN"
        assert out["progress_started_ts"] == STARTED

    def test_b_c_increment_tick_edits_with_rates(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x" * 200, encoding="utf-8")  # size > offset (no truncation)
        data = _base_data(
            progress_phase="DESIGN_PLAN",
            progress_transcript=str(transcript),
            progress_started_ts=STARTED,
            progress_offset=100,
            progress_count=5,
            progress_prev_count=5,
            progress_prev_ts=NOW - 60.0,
            progress_msg_id="existing",
        )
        _write(pf, data)
        reader = _FakeReader(transcript, [(3, 160)])

        with _patched(reader, _entered()) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)

        post.assert_not_called()
        assert edit.call_count == 1
        text = edit.call_args.args[2]
        assert "8 tool calls" in text
        assert "avg 4.0/min" in text   # 8 / (120/60)
        assert "now 3.0/min" in text   # (8-5)/(60/60)
        # increment read: since_epoch None
        assert reader.calls == [(100, None)]
        out = _read(pf)
        assert out["progress_count"] == 8
        assert out["progress_offset"] == 160
        assert out["progress_msg_id"] == "existing"

    def test_d_finalize_forward_complete(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data(
            state="DESIGN_REVIEW",
            progress_phase="DESIGN_PLAN",
            progress_count=12,
            progress_msg_id="m1",
        )
        _write(pf, data)
        with _patched(None, None) as (post, edit):
            update_phase_progress(pf, "DESIGN_REVIEW", data)
        assert edit.call_count == 1
        text = edit.call_args.args[2]
        assert "✅ DESIGN_PLAN complete" in text
        assert "12 tool calls" in text
        out = _read(pf)
        assert "progress_phase" not in out
        assert "progress_msg_id" not in out

    def test_d_finalize_blocked(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data(
            state="BLOCKED", enabled=False, blocked_reason="timeout",
            progress_phase="DESIGN_PLAN", progress_count=3, progress_msg_id="m1",
        )
        _write(pf, data)
        with _patched(None, None) as (post, edit):
            update_phase_progress(pf, "BLOCKED", data)
        text = edit.call_args.args[2]
        assert "⏹ DESIGN_PLAN ended (timeout)" in text
        assert "progress_phase" not in _read(pf)

    def test_d_finalize_idle(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data(
            state="IDLE", enabled=False,
            progress_phase="CODE_REVISE", progress_count=9, progress_msg_id="m1",
        )
        _write(pf, data)
        with _patched(None, None) as (post, edit):
            update_phase_progress(pf, "IDLE", data)
        text = edit.call_args.args[2]
        assert "⏹ CODE_REVISE ended" in text
        assert "(timeout)" not in text
        assert "progress_phase" not in _read(pf)

    def test_e_post_failure_keeps_count_no_msg_id(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x", encoding="utf-8")
        data = _base_data()
        _write(pf, data)
        reader = _FakeReader(transcript, [(5, 100)])
        with _patched(reader, _entered(), post_result=DiscordPostResult(None)) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)
        assert post.call_count == 1
        out = _read(pf)
        assert out["progress_count"] == 5
        assert out["progress_offset"] == 100
        assert "progress_msg_id" not in out

    def test_f_edit_error_keeps_msg_id(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x" * 200, encoding="utf-8")
        data = _base_data(
            progress_phase="DESIGN_PLAN", progress_transcript=str(transcript),
            progress_started_ts=STARTED, progress_offset=100, progress_count=5,
            progress_prev_count=5, progress_prev_ts=NOW - 60.0, progress_msg_id="keep-me",
        )
        _write(pf, data)
        reader = _FakeReader(transcript, [(1, 110)])
        with _patched(reader, _entered(), edit_result="error") as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)
        assert _read(pf)["progress_msg_id"] == "keep-me"

    def test_f_l_edit_deleted_clears_msg_id_and_reposts_next_tick(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x" * 200, encoding="utf-8")
        data = _base_data(
            progress_phase="DESIGN_PLAN", progress_transcript=str(transcript),
            progress_started_ts=STARTED, progress_offset=100, progress_count=5,
            progress_prev_count=5, progress_prev_ts=NOW - 60.0, progress_msg_id="gone",
        )
        _write(pf, data)
        # tick 1: edit returns "deleted" → msg_id cleared
        reader1 = _FakeReader(transcript, [(1, 110)])
        with _patched(reader1, _entered(), edit_result="deleted") as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)
        assert edit.call_count == 1
        out1 = _read(pf)
        assert "progress_msg_id" not in out1

        # tick 2: msg_id absent → re-post
        reader2 = _FakeReader(transcript, [(2, 120)])
        with _patched(reader2, _entered(), post_result=DiscordPostResult("fresh")) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", out1)
        assert post.call_count == 1
        assert _read(pf)["progress_msg_id"] == "fresh"

    def test_g_reader_none_noop(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data()
        _write(pf, data)
        with _patched(None, _entered()) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)
        post.assert_not_called()
        edit.assert_not_called()
        assert "progress_phase" not in _read(pf)

    def test_g_resolve_none_noop(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data()
        _write(pf, data)
        reader = _FakeReader(None, [])  # resolve returns None
        with _patched(reader, _entered()) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)
        post.assert_not_called()
        assert "progress_phase" not in _read(pf)

    def test_g_empty_implementers_noop(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x", encoding="utf-8")
        data = _base_data()
        data.pop("implementer")
        _write(pf, data)
        reader = _FakeReader(transcript, [(5, 100)])
        with _patched(reader, _entered()) as (post, edit), \
             patch.object(config, "IMPLEMENTERS", []):
            update_phase_progress(pf, "DESIGN_PLAN", data)
        post.assert_not_called()
        assert "progress_phase" not in _read(pf)

    def test_g_resolve_backend_valueerror_noop(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data()
        _write(pf, data)
        with patch("engine.progress.resolve_backend", side_effect=ValueError("x")), \
             patch("engine.progress._get_state_entered_at", return_value=_entered()), \
             patch("notify.post_discord") as post, \
             patch("notify.edit_discord_message") as edit, \
             patch.object(config, "DISCORD_CHANNEL", "chan-123"):
            update_phase_progress(pf, "DESIGN_PLAN", data)
        post.assert_not_called()
        edit.assert_not_called()
        assert "progress_phase" not in _read(pf)

    def test_h_truncation_reestablishes(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x", encoding="utf-8")  # tiny file
        data = _base_data(
            progress_phase="DESIGN_PLAN", progress_transcript=str(transcript),
            progress_started_ts=STARTED, progress_offset=99999, progress_count=5,
            progress_prev_count=5, progress_prev_ts=NOW - 60.0, progress_msg_id="existing",
        )
        _write(pf, data)
        reader = _FakeReader(transcript, [(2, 50)])
        with _patched(reader, _entered()) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)
        # re-established: offset 0, since_epoch = phase_start; same cycle keeps editing msg
        assert reader.calls == [(0, STARTED)]
        post.assert_not_called()
        assert edit.call_count == 1
        out = _read(pf)
        assert out["progress_count"] == 2
        assert out["progress_offset"] == 50
        assert out["progress_msg_id"] == "existing"

    def test_i_finalize_clears_without_msg_id(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data(
            state="DESIGN_REVIEW",
            progress_phase="DESIGN_PLAN", progress_count=7,
        )  # no progress_msg_id (post had failed)
        _write(pf, data)
        with _patched(None, None) as (post, edit):
            update_phase_progress(pf, "DESIGN_REVIEW", data)
        edit.assert_not_called()  # no msg_id to edit
        out = _read(pf)
        assert "progress_phase" not in out
        assert "progress_count" not in out

    def test_j_reentry_same_state_reestablishes_fresh(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x", encoding="utf-8")
        # saved started_ts differs from the new entry time → new cycle
        old_started = STARTED - 5000.0
        data = _base_data(
            progress_phase="DESIGN_PLAN", progress_transcript=str(transcript),
            progress_started_ts=old_started, progress_offset=500, progress_count=99,
            progress_prev_count=99, progress_prev_ts=old_started, progress_msg_id="old-msg",
        )
        _write(pf, data)
        reader = _FakeReader(transcript, [(4, 80)])
        with _patched(reader, _entered(), post_result=DiscordPostResult("brand-new")) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)
        # different cycle → fresh establish + new message (post, not edit)
        assert reader.calls == [(0, STARTED)]
        assert post.call_count == 1
        edit.assert_not_called()
        out = _read(pf)
        assert out["progress_count"] == 4
        assert out["progress_started_ts"] == STARTED
        assert out["progress_msg_id"] == "brand-new"

    def test_k_disabled_target_phase_noop(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data(enabled=False)
        _write(pf, data)
        reader = _FakeReader(tmp_path / "t.jsonl", [(5, 100)])
        with _patched(reader, _entered()) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)
        post.assert_not_called()
        edit.assert_not_called()
        assert "progress_phase" not in _read(pf)

    def test_m_no_entered_eof_baseline(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("abcdef", encoding="utf-8")  # size 6
        data = _base_data()
        _write(pf, data)
        reader = _FakeReader(transcript, [])  # count_tool_calls must NOT be called
        with _patched(reader, None, post_result=DiscordPostResult("eof-id")) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)
        # EOF baseline: no establish read, count 0, offset = size
        assert reader.calls == []
        text = post.call_args.args[1]
        assert "0 tool calls" in text
        out = _read(pf)
        assert out["progress_count"] == 0
        assert out["progress_offset"] == 6
        assert out["progress_started_ts"] == NOW

    def test_no_discord_channel_noop(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data()
        _write(pf, data)
        with patch.object(config, "PROGRESS_NOTIFY", True), \
             patch.object(config, "DISCORD_CHANNEL", ""), \
             patch("notify.post_discord") as post:
            update_phase_progress(pf, "DESIGN_PLAN", data)
        post.assert_not_called()

    def test_progress_notify_disabled_noop(self, tmp_path):
        # マスタースイッチ OFF（デフォルト）では reader/Discord に一切触れず、
        # progress_* も書かない（#382 トグル）。
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data()
        _write(pf, data)
        reader = _FakeReader(tmp_path / "t.jsonl", [(5, 100)])
        with patch.object(config, "PROGRESS_NOTIFY", False), \
             patch.object(config, "DISCORD_CHANNEL", "chan-123"), \
             patch("engine.progress.get_reader", return_value=reader) as get_reader, \
             patch("notify.post_discord") as post, \
             patch("notify.edit_discord_message") as edit:
            update_phase_progress(pf, "DESIGN_PLAN", data)
        get_reader.assert_not_called()
        post.assert_not_called()
        edit.assert_not_called()
        assert "progress_phase" not in _read(pf)
        assert "progress_phase" not in _read(pf)
