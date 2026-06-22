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


class _FakeReaderCtx(_FakeReader):
    def __init__(self, path, counts, ctx):
        super().__init__(path, counts)
        self._ctx = ctx

    def read_context_size(self, path):
        return self._ctx


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
            progress_samples=[[NOW - 60.0, 5]],
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
        assert "last min 3" in text    # 8 - 5 (baseline at window edge 1940)
        # increment read: since_epoch None
        assert reader.calls == [(100, None)]
        out = _read(pf)
        assert out["progress_count"] == 8
        assert out["progress_offset"] == 160
        assert out["progress_msg_id"] == "existing"
        assert out["progress_samples"] == [[1940.0, 5], [2000.0, 8]]

    def test_d_finalize_forward_complete(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data(
            state="DESIGN_REVIEW",
            progress_phase="DESIGN_PLAN",
            progress_count=12,
            progress_started_ts=STARTED,
            progress_msg_id="m1",
        )
        _write(pf, data)
        with _patched(None, None) as (post, edit):
            update_phase_progress(pf, "DESIGN_REVIEW", data)
        assert edit.call_count == 1
        text = edit.call_args.args[2]
        assert "✅ DESIGN_PLAN complete" in text
        assert "12 tool calls" in text
        assert "avg 6.0/min" in text   # 12 / (120/60)
        assert "⏱ 2m 0s" in text
        out = _read(pf)
        assert "progress_phase" not in out
        assert "progress_msg_id" not in out

    def test_d_finalize_blocked(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data(
            state="BLOCKED", enabled=False, blocked_reason="timeout",
            progress_phase="DESIGN_PLAN", progress_count=3,
            progress_started_ts=STARTED, progress_msg_id="m1",
        )
        _write(pf, data)
        with _patched(None, None) as (post, edit):
            update_phase_progress(pf, "BLOCKED", data)
        text = edit.call_args.args[2]
        assert "⏹ DESIGN_PLAN ended (timeout)" in text
        assert "avg 1.5/min" in text   # 3 / (120/60)
        assert "⏱ 2m 0s" in text
        assert "progress_phase" not in _read(pf)

    def test_d_finalize_idle(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data(
            state="IDLE", enabled=False,
            progress_phase="CODE_REVISE", progress_count=9,
            progress_started_ts=STARTED, progress_msg_id="m1",
        )
        _write(pf, data)
        with _patched(None, None) as (post, edit):
            update_phase_progress(pf, "IDLE", data)
        text = edit.call_args.args[2]
        assert "⏹ CODE_REVISE ended" in text
        assert "(timeout)" not in text
        assert "avg 4.5/min" in text   # 9 / (120/60)
        assert "⏱ 2m 0s" in text
        assert "progress_phase" not in _read(pf)

    def test_d_finalize_no_started_ts_degrades(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data(
            state="DESIGN_REVIEW",
            progress_phase="DESIGN_PLAN", progress_count=12, progress_msg_id="m1",
        )  # no progress_started_ts → no avg/⏱ suffix
        _write(pf, data)
        with _patched(None, None) as (post, edit):
            update_phase_progress(pf, "DESIGN_REVIEW", data)
        text = edit.call_args.args[2]
        assert "✅ DESIGN_PLAN complete" in text
        assert "12 tool calls" in text
        assert "avg" not in text
        assert "⏱" not in text

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
            progress_samples=[[NOW - 60.0, 5]], progress_msg_id="keep-me",
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
            progress_samples=[[NOW - 60.0, 5]], progress_msg_id="gone",
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
            progress_samples=[[NOW - 60.0, 5]], progress_msg_id="existing",
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
            progress_samples=[[NOW - 60.0, 5]], progress_msg_id="old-msg",
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

    # --- 7-3: 進行中表示への追記 + 永続化 ---
    def test_ctx_appended_in_progress_and_persisted(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x", encoding="utf-8")
        data = _base_data()
        _write(pf, data)
        reader = _FakeReaderCtx(transcript, [(5, 100)], ctx=12345)

        with _patched(reader, _entered()) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)

        assert "ctx ~12.3K tok" in post.call_args.args[1]
        out = _read(pf)
        assert out["progress_ctx_tokens"] == 12345

    # --- 7-4: 未実装 reader では ctx を出さず永続化もしない（fresh）---
    def test_ctx_skipped_when_reader_unsupported(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x", encoding="utf-8")
        data = _base_data()
        _write(pf, data)
        reader = _FakeReader(transcript, [(5, 100)])  # read_context_size を持たない

        with _patched(reader, _entered()) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)

        assert "ctx " not in post.call_args.args[1]
        assert "progress_ctx_tokens" not in _read(pf)

    # --- 7-5: 取得失敗 tick(非fresh)は直前の永続値にフォールバック表示し、値も保持 ---
    def test_ctx_fallback_to_persisted_when_tick_read_fails(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x" * 200, encoding="utf-8")  # size > offset（fresh でない）
        data = _base_data(
            progress_phase="DESIGN_PLAN", progress_transcript=str(transcript),
            progress_started_ts=STARTED, progress_offset=100, progress_count=5,
            progress_samples=[[NOW - 60.0, 5]], progress_msg_id="existing",
            progress_ctx_tokens=12345,
        )
        _write(pf, data)
        reader = _FakeReaderCtx(transcript, [(3, 160)], ctx=None)  # この tick は取得失敗

        with _patched(reader, _entered()) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)

        assert "ctx ~12.3K tok" in edit.call_args.args[2]   # 直前値にフォールバック表示
        assert _read(pf)["progress_ctx_tokens"] == 12345     # 永続値は保持

    # --- 7-6: 完了サマリーに persisted ctx を追記 ---
    def test_finalize_appends_persisted_ctx(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        data = _base_data(
            state="DONE", progress_phase="DESIGN_PLAN", progress_count=10,
            progress_started_ts=STARTED, progress_msg_id="m1",
            progress_ctx_tokens=366000,
        )
        _write(pf, data)
        reader = _FakeReader(tmp_path / "t.jsonl", [])

        with _patched(reader, None) as (post, edit):
            update_phase_progress(pf, "DONE", data)

        assert "ctx ~366K tok" in edit.call_args.args[2]

    # --- 7-7: fresh フェーズ初回の取得失敗では旧フェーズ残骸を出さず除去（フォールバックしない）---
    def test_ctx_no_stale_fallback_on_fresh_phase(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x", encoding="utf-8")
        data = _base_data(progress_phase="OTHER", progress_ctx_tokens=99999)
        _write(pf, data)
        reader = _FakeReaderCtx(transcript, [(5, 100)], ctx=None)

        with _patched(reader, _entered()) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)

        assert "ctx " not in post.call_args.args[1]      # fresh + None → フォールバックしない
        assert "progress_ctx_tokens" not in _read(pf)      # 残骸は除去

    # --- 7-8: 未対応 reader は非fresh+残存値でも ctx を出さず残骸を pop（euler R3 P2 不変条件）---
    def test_ctx_unsupported_reader_pops_stale_when_not_fresh(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x" * 200, encoding="utf-8")  # size > offset（fresh でない）
        data = _base_data(
            progress_phase="DESIGN_PLAN", progress_transcript=str(transcript),
            progress_started_ts=STARTED, progress_offset=100, progress_count=5,
            progress_samples=[[NOW - 60.0, 5]], progress_msg_id="existing",
            progress_ctx_tokens=12345,  # 残存値
        )
        _write(pf, data)
        reader = _FakeReader(transcript, [(3, 160)])  # read_context_size 未実装（supports_ctx=False）

        with _patched(reader, _entered()) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)

        assert "ctx " not in edit.call_args.args[2]       # 未対応 → フォールバック表示しない
        assert "progress_ctx_tokens" not in _read(pf)       # 残骸を pop

    # --- 7-9: 誤実装 reader(非int)は非fresh+残存値でも ctx を出さず残骸を pop（hanfei R4 / euler R5 P2）---
    def test_ctx_invalid_return_pops_stale_when_not_fresh(self, tmp_path):
        from engine.progress import update_phase_progress
        pf = tmp_path / "proj.json"
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x" * 200, encoding="utf-8")  # size > offset（fresh でない）
        data = _base_data(
            progress_phase="DESIGN_PLAN", progress_transcript=str(transcript),
            progress_started_ts=STARTED, progress_offset=100, progress_count=5,
            progress_samples=[[NOW - 60.0, 5]], progress_msg_id="existing",
            progress_ctx_tokens=12345,  # 残存値
        )
        _write(pf, data)
        reader = _FakeReaderCtx(transcript, [(3, 160)], ctx="oops")  # 非 int を返す誤実装

        with _patched(reader, _entered()) as (post, edit):
            update_phase_progress(pf, "DESIGN_PLAN", data)

        assert "ctx " not in edit.call_args.args[2]      # 誤実装 → フォールバック表示しない
        assert "progress_ctx_tokens" not in _read(pf)      # 残骸を pop


# ---------------------------------------------------------------------------
# format_ctx_tokens / read_context_size — direct (#386)
# ---------------------------------------------------------------------------

def test_format_ctx_tokens_boundaries():
    from engine.progress import format_ctx_tokens
    assert format_ctx_tokens(0) == "~0 tok"
    assert format_ctx_tokens(999) == "~999 tok"
    assert format_ctx_tokens(1000) == "~1.0K tok"
    assert format_ctx_tokens(12345) == "~12.3K tok"
    assert format_ctx_tokens(99999) == "~100K tok"
    assert format_ctx_tokens(100000) == "~100K tok"
    assert format_ctx_tokens(366000) == "~366K tok"
    assert format_ctx_tokens(999499) == "~999K tok"
    assert format_ctx_tokens(999500) == "~1.0M tok"
    assert format_ctx_tokens(999949) == "~1.0M tok"
    assert format_ctx_tokens(999999) == "~1.0M tok"
    assert format_ctx_tokens(1_000_000) == "~1.0M tok"
    assert format_ctx_tokens(2_500_000) == "~2.5M tok"


def test_format_ctx_tokens_huge_int_does_not_crash():
    from engine.progress import format_ctx_tokens
    # float 変換不能な巨大整数（壊れた transcript 由来）でもクラッシュせず、そのまま表示する。
    huge = 10 ** 400
    assert format_ctx_tokens(huge) == f"~{huge} tok"


class TestClaudeJsonlReaderContextSize:

    def test_latest_usage_sum_excludes_output(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        p = tmp_path / "t.jsonl"
        older = json.dumps({
            "type": "assistant",
            "message": {"usage": {
                "input_tokens": 100, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0, "output_tokens": 5,
            }},
        })
        newer = json.dumps({
            "type": "assistant",
            "message": {"usage": {
                "input_tokens": 1000, "cache_read_input_tokens": 2000,
                "cache_creation_input_tokens": 300, "output_tokens": 50,
            }},
        })
        partial = '{"type":"assistant"'  # 改行なしの末尾部分行
        p.write_text(older + "\n" + newer + "\n" + partial, encoding="utf-8")
        reader = ClaudeJsonlReader(tmp_path)
        # 末尾の部分行はスキップ、最新の完全 assistant 行(newer)の合計。output_tokens 除外。
        assert reader.read_context_size(p) == 3300

    def test_input_tokens_only_returns_input(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        p = tmp_path / "t.jsonl"
        # cache_* が欠落し input_tokens のみ有効 → None ではなく input_tokens を返す。
        line = json.dumps({"type": "assistant",
                           "message": {"usage": {"input_tokens": 500, "output_tokens": 9}}})
        p.write_text(line + "\n", encoding="utf-8")
        reader = ClaudeJsonlReader(tmp_path)
        assert reader.read_context_size(p) == 500

    def test_missing_usage_returns_none(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        p = tmp_path / "t.jsonl"
        line = json.dumps({"type": "assistant", "message": {"content": [{"type": "text"}]}})
        p.write_text(line + "\n", encoding="utf-8")
        reader = ClaudeJsonlReader(tmp_path)
        assert reader.read_context_size(p) is None

    def test_usage_without_token_keys_returns_none(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        p = tmp_path / "t.jsonl"
        # usage は存在するが input/cache_* が 1 つも無い → 0 ではなく None（取得失敗扱い）。
        line = json.dumps({"type": "assistant", "message": {"usage": {"output_tokens": 7}}})
        p.write_text(line + "\n", encoding="utf-8")
        reader = ClaudeJsonlReader(tmp_path)
        assert reader.read_context_size(p) is None

    def test_read_error_returns_none(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        reader = ClaudeJsonlReader(tmp_path)
        assert reader.read_context_size(tmp_path / "nope.jsonl") is None

    def test_non_finite_value_is_skipped_without_crash(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        p = tmp_path / "t.jsonl"
        # json.loads は Infinity を受理する。int(inf) で落とさず、非有限キーはスキップして
        # 有限キーのみ合計する（壊れた transcript 1 行で watchdog をクラッシュさせない）。
        line = json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": float("inf"), "cache_read_input_tokens": 2000,
        }}})
        assert "Infinity" in line  # allow_nan デフォルトで Infinity トークンが書かれる
        p.write_text(line + "\n", encoding="utf-8")
        reader = ClaudeJsonlReader(tmp_path)
        assert reader.read_context_size(p) == 2000

    def test_all_non_finite_usage_returns_none(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        p = tmp_path / "t.jsonl"
        # 全キーが NaN/Infinity → 有効キー皆無扱いで None（クラッシュもしない）。
        line = json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": float("nan"), "cache_read_input_tokens": float("inf"),
        }}})
        p.write_text(line + "\n", encoding="utf-8")
        reader = ClaudeJsonlReader(tmp_path)
        assert reader.read_context_size(p) is None

    def test_huge_int_usage_does_not_crash(self, tmp_path):
        from engine.progress import ClaudeJsonlReader
        p = tmp_path / "t.jsonl"
        # json は任意精度 int を生む。math.isfinite(巨大int) の OverflowError で落とさず、
        # int はそのまま合計する（「例外を投げない」契約を巨大整数に対しても守る）。
        huge = 10 ** 400
        p.write_text(
            '{"type":"assistant","message":{"usage":{"input_tokens":%d}}}\n' % huge,
            encoding="utf-8",
        )
        reader = ClaudeJsonlReader(tmp_path)
        assert reader.read_context_size(p) == huge


# ---------------------------------------------------------------------------
# _last_min_count / _append_and_prune — direct (#384)
# ---------------------------------------------------------------------------

class TestLastMinHelpers:

    def test_last_min_multi_sample_base_lookup(self):
        from engine.progress import _last_min_count
        # window edge 940; baseline = snapshot at/before 940 = 53; 64 - 53 = 11
        assert _last_min_count(
            [[940.0, 53], [955.0, 55], [985.0, 61]],
            count=64, now=1000.0, started_ts=500.0,
        ) == 11

    def test_last_min_phase_began_inside_window(self):
        from engine.progress import _last_min_count
        # started_ts >= now - 60 → all calls recent → full count
        assert _last_min_count([], count=7, now=1000.0, started_ts=970.0) == 7

    def test_last_min_empty_samples_old_start(self):
        from engine.progress import _last_min_count
        # no samples → baseline 0 → full count
        assert _last_min_count([], count=7, now=1000.0, started_ts=100.0) == 7

    def test_last_min_mid_restart_fallback(self):
        from engine.progress import _last_min_count
        # samples exist but none old enough → baseline = oldest sample (5); 9 - 5 = 4
        assert _last_min_count([[980.0, 5]], count=9, now=1000.0, started_ts=100.0) == 4

    def test_append_and_prune_two_pre_window(self):
        from engine.progress import _append_and_prune
        src = [[850.0, 0], [900.0, 1], [945.0, 3], [970.0, 5]]
        out = _append_and_prune(src, now=1000.0, count=8)
        assert out == [[900.0, 1], [945.0, 3], [970.0, 5], [1000.0, 8]]
        # fresh list: mutating the result does not touch the input
        out[0][1] = 999
        assert src == [[850.0, 0], [900.0, 1], [945.0, 3], [970.0, 5]]
