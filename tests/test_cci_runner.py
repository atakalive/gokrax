"""Tests for engine/cci_runner.py — one-shot CCI driver."""

from __future__ import annotations

import argparse
import itertools
import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pexpect
import pytest

from engine import cci_runner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_module_state():
    cci_runner._child = None
    cci_runner._prompt_file_path = None
    yield
    cci_runner._child = None
    cci_runner._prompt_file_path = None


def _write_ndjson(path: Path, entries: list[dict]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _user(content) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}}


def _assistant(content, stop_reason="end_turn") -> dict:
    return {"type": "assistant", "message": {"content": content, "stop_reason": stop_reason}}


# ===========================================================================
# _clean_env_for_cci
# ===========================================================================

class TestCleanEnv:
    def test_removes_and_adds_keys(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("KEEP_ME", "yes")
        env = cci_runner._clean_env_for_cci()
        assert "ANTHROPIC_API_KEY" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "CLAUDECODE" not in env
        assert env["KEEP_ME"] == "yes"
        assert env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"] == "1"
        assert env["TERM"] == "xterm-256color"


# ===========================================================================
# _ensure_trust_accepted
# ===========================================================================

class TestEnsureTrustAccepted:
    def test_file_missing_returns_quietly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # No ~/.claude.json present
        cci_runner._ensure_trust_accepted(str(tmp_path))  # must not raise
        assert not (tmp_path / ".claude.json").exists()

    def test_already_true_returns_before_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cwd = str(tmp_path)
        cwd_abs = str(Path(cwd).resolve())
        (tmp_path / ".claude.json").write_text(json.dumps(
            {"projects": {cwd_abs: {"hasTrustDialogAccepted": True}}}
        ))

        def _boom(*a, **k):
            raise AssertionError("flock must not be called on optimistic-true path")

        with patch("engine.cci_runner.fcntl.flock", _boom):
            cci_runner._ensure_trust_accepted(cwd)

    def test_sets_true_via_lock_and_preserves_others(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir()
        cwd_abs = str(Path(cwd).resolve())
        other = "/some/other/project"
        (tmp_path / ".claude.json").write_text(json.dumps({
            "projects": {
                other: {"hasTrustDialogAccepted": True, "x": 1},
                cwd_abs: {"hasTrustDialogAccepted": False},
            }
        }))
        cci_runner._ensure_trust_accepted(cwd)
        data = json.loads((tmp_path / ".claude.json").read_text())
        assert data["projects"][cwd_abs]["hasTrustDialogAccepted"] is True
        assert data["projects"][other] == {"hasTrustDialogAccepted": True, "x": 1}
        # Lock file was created
        assert (tmp_path / ".claude.json.lock").exists()

    def test_concurrent_no_lost_update(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude.json").write_text(json.dumps({"projects": {}}))

        # Two distinct cwds updated concurrently; both must survive.
        cwds = []
        for name in ("a", "b"):
            d = tmp_path / name
            d.mkdir()
            cwds.append(str(d))

        barrier = threading.Barrier(2)

        def worker(cwd):
            barrier.wait()
            cci_runner._ensure_trust_accepted(cwd)

        threads = [threading.Thread(target=worker, args=(c,)) for c in cwds]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        data = json.loads((tmp_path / ".claude.json").read_text())
        for cwd in cwds:
            cwd_abs = str(Path(cwd).resolve())
            assert data["projects"][cwd_abs]["hasTrustDialogAccepted"] is True

    def test_corrupt_json_not_clobbered(self, tmp_path, monkeypatch):
        """Invalid JSON must not be overwritten with a minimal dict."""
        monkeypatch.setenv("HOME", str(tmp_path))
        corrupt = "not valid json {{{{"
        (tmp_path / ".claude.json").write_text(corrupt)
        cci_runner._ensure_trust_accepted(str(tmp_path))
        # File must remain unchanged — not overwritten.
        assert (tmp_path / ".claude.json").read_text() == corrupt

    def test_non_dict_root_not_clobbered(self, tmp_path, monkeypatch):
        """JSON root that is not a dict must not be overwritten."""
        monkeypatch.setenv("HOME", str(tmp_path))
        content = json.dumps([1, 2, 3])
        (tmp_path / ".claude.json").write_text(content)
        cci_runner._ensure_trust_accepted(str(tmp_path))
        assert (tmp_path / ".claude.json").read_text() == content


# ===========================================================================
# CLI argument parsing
# ===========================================================================

class TestParseArgs:
    def test_session_id_and_resume_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            cci_runner._parse_args(
                ["--session-id", "a", "--resume", "b", "--prompt-file", "-"]
            )

    def test_prompt_file_required(self):
        with pytest.raises(SystemExit):
            cci_runner._parse_args(["--session-id", "a"])

    def test_one_of_session_or_resume_required(self):
        with pytest.raises(SystemExit):
            cci_runner._parse_args(["--prompt-file", "-"])

    def test_effort_xhigh_accepted(self):
        args = cci_runner._parse_args(
            ["--session-id", "a", "--prompt-file", "-", "--effort", "xhigh"]
        )
        assert args.effort == "xhigh"

    def test_effort_ultracode_accepted(self):
        args = cci_runner._parse_args(
            ["--session-id", "a", "--prompt-file", "-", "--effort", "ultracode"]
        )
        assert args.effort == "ultracode"

    def test_build_claude_args_ultracode(self):
        args = argparse.Namespace(
            model=None, thinking=None, effort="ultracode",
            append_system_prompt=None, disallowed_tools=None,
        )
        cmd_args = cci_runner._build_claude_args(args, "sess", False)
        idx = cmd_args.index("--effort")
        assert cmd_args[idx + 1] == "xhigh"
        sidx = cmd_args.index("--settings")
        assert cmd_args[sidx + 1] == '{"ultracode": true}'

    def test_build_claude_args_xhigh(self):
        args = argparse.Namespace(
            model=None, thinking=None, effort="xhigh",
            append_system_prompt=None, disallowed_tools=None,
        )
        cmd_args = cci_runner._build_claude_args(args, "sess", False)
        idx = cmd_args.index("--effort")
        assert cmd_args[idx + 1] == "xhigh"
        assert "--settings" not in cmd_args

    def test_completion_timeout_default(self):
        import config
        args = cci_runner._parse_args(["--session-id", "test", "--prompt-file", "-"])
        assert args.completion_timeout == config.CCI_COMPLETION_TIMEOUT_SEC

    def test_completion_timeout_value(self):
        args = cci_runner._parse_args(
            ["--session-id", "a", "--prompt-file", "-", "--completion-timeout", "55"]
        )
        assert args.completion_timeout == 55

    def test_append_system_prompt_passthrough(self):
        args = cci_runner._parse_args(
            ["--session-id", "xxx", "--prompt-file", "f.txt",
             "--append-system-prompt", "test prompt"]
        )
        assert args.append_system_prompt == "test prompt"

    def test_append_system_prompt_default_none(self):
        args = cci_runner._parse_args(
            ["--session-id", "xxx", "--prompt-file", "f.txt"]
        )
        assert args.append_system_prompt is None

    def test_disallowed_tools_passthrough(self):
        args = cci_runner._parse_args(
            ["--session-id", "xxx", "--prompt-file", "f.txt",
             "--disallowed-tools", "Edit,Write"]
        )
        assert args.disallowed_tools == "Edit,Write"

    def test_disallowed_tools_default_none(self):
        args = cci_runner._parse_args(
            ["--session-id", "xxx", "--prompt-file", "f.txt"]
        )
        assert args.disallowed_tools is None

    def test_prompt_file_stdin(self):
        args = cci_runner._parse_args(
            ["--session-id", "xxx", "--prompt-file", "-"]
        )
        assert args.prompt_file == "-"


# ===========================================================================
# _CompletionDetector
# ===========================================================================

class TestCompletionDetector:
    def test_assistant_ignored_before_watermark(self):
        det = cci_runner._CompletionDetector("hello")
        done = det.process_entry(_assistant([{"type": "text", "text": "hi"}]))
        assert done is False

    def test_watermark_then_assistant_completes(self):
        det = cci_runner._CompletionDetector("hello")
        assert det.process_entry(_user("hello")) is False
        assert det.process_entry(_assistant([{"type": "text", "text": "hi"}])) is True

    def test_tool_result_only_user_not_watermark(self):
        det = cci_runner._CompletionDetector("hello")
        # tool_result-only user must NOT set the watermark
        det.process_entry(_user([{"type": "tool_result", "content": "x"}]))
        # subsequent assistant therefore ignored
        assert det.process_entry(_assistant([{"type": "text", "text": "hi"}])) is False

    def test_empty_assistant_does_not_falsely_complete(self):
        det = cci_runner._CompletionDetector("hello")
        det.process_entry(_user("hello"))
        # assistant with neither text nor tool_use + stop_reason → not complete
        assert det.process_entry(_assistant([], stop_reason="end_turn")) is False

    def test_tool_use_assistant_completes(self):
        det = cci_runner._CompletionDetector("hello")
        det.process_entry(_user("hello"))
        block = [{"type": "tool_use", "name": "Read", "input": {}}]
        assert det.process_entry(_assistant(block)) is True

    def test_queue_operation_enqueue_is_watermark(self):
        det = cci_runner._CompletionDetector("hello")
        det.process_entry({"type": "queue-operation", "operation": "enqueue", "content": "hello"})
        assert det.process_entry(_assistant([{"type": "text", "text": "hi"}])) is True

    def test_turn_duration_completes_after_assistant(self):
        det = cci_runner._CompletionDetector("hello")
        det.process_entry(_user("hello"))
        det.process_entry(_assistant([{"type": "text", "text": "hi"}], stop_reason="tool_use"))
        done = det.process_entry({"type": "system", "subtype": "turn_duration"})
        assert done is True


# ===========================================================================
# _wait_for_completion
# ===========================================================================

class TestWaitForCompletion:
    def test_happy_path_returns_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr("engine.cci_runner.time.monotonic", itertools.count(1.0).__next__)
        path = tmp_path / "s.jsonl"
        _write_ndjson(path, [_user("hello"), _assistant([{"type": "text", "text": "hi"}])])
        done = cci_runner._wait_for_completion(
            None, path, "hello", 0, 1000.0, lambda stem: None,
        )
        assert done is True

    def test_offset_skips_old_watermark_resume(self, tmp_path, monkeypatch):
        monkeypatch.setattr("engine.cci_runner.time.monotonic", itertools.count(1.0).__next__)
        path = tmp_path / "s.jsonl"
        # Old region: a user watermark that must be skipped.
        _write_ndjson(path, [_user("hello")])
        offset = path.stat().st_size
        # New region: only an assistant (no fresh watermark).
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_assistant([{"type": "text", "text": "stale"}])) + "\n")
        # deadline=8: fake clock 1,2,... crosses 8 → returns False (watermark skipped).
        done = cci_runner._wait_for_completion(
            None, path, "hello", offset, 8.0, lambda stem: None,
        )
        assert done is False

    def test_glob_fallback_when_predicted_path_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("engine.cci_runner.time.monotonic", itertools.count(1.0).__next__)
        predicted = tmp_path / "missing.jsonl"
        real = tmp_path / "real.jsonl"
        _write_ndjson(real, [_user("hello"), _assistant([{"type": "text", "text": "hi"}])])
        done = cci_runner._wait_for_completion(
            None, predicted, "hello", 0, 1000.0, lambda stem: real,
        )
        assert done is True

    def test_timeout_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("engine.cci_runner.time.monotonic", itertools.count(1.0).__next__)
        path = tmp_path / "s.jsonl"
        _write_ndjson(path, [_user("hello")])  # watermark but no assistant
        done = cci_runner._wait_for_completion(
            None, path, "hello", 0, 5.0, lambda stem: None,
        )
        assert done is False


# ===========================================================================
# _glob_fallback_path
# ===========================================================================

class TestGlobFallback:
    def test_finds_uuid_jsonl(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        proj = home / ".claude" / "projects" / "some-dir"
        proj.mkdir(parents=True)
        sid = "11111111-2222-3333-4444-555555555555"
        f = proj / f"{sid}.jsonl"
        f.write_text("{}")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        assert cci_runner._glob_fallback_path(sid) == f

    def test_returns_none_when_absent(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        assert cci_runner._glob_fallback_path("nope") is None


# ===========================================================================
# _drain_pty
# ===========================================================================

class TestDrainPty:
    def test_reads_until_empty(self):
        child = MagicMock()
        child.read_nonblocking.side_effect = ["a", "b", pexpect.TIMEOUT("done")]
        cci_runner._drain_pty(child)
        assert child.read_nonblocking.call_count == 3

    def test_stops_on_eof(self):
        child = MagicMock()
        child.read_nonblocking.side_effect = ["a", pexpect.EOF("eof")]
        cci_runner._drain_pty(child)
        assert child.read_nonblocking.call_count == 2


# ===========================================================================
# Signal handler
# ===========================================================================

class TestSignalHandler:
    def test_no_child_no_terminate(self, monkeypatch):
        monkeypatch.setattr(cci_runner, "_child", None)
        with pytest.raises(SystemExit):
            cci_runner._cleanup_handler(15, None)

    def test_live_child_terminated(self, monkeypatch):
        child = MagicMock()
        child.isalive.return_value = True
        monkeypatch.setattr(cci_runner, "_child", child)
        with pytest.raises(SystemExit):
            cci_runner._cleanup_handler(15, None)
        child.terminate.assert_called_once_with(force=True)


# ===========================================================================
# main() — prompt file cleanup + child reaping
# ===========================================================================

def _patch_main_internals(monkeypatch, *, handshake_raises=False, completion=True):
    monkeypatch.setattr("engine.cci_runner.signal.signal", lambda *a, **k: None)
    monkeypatch.setattr(cci_runner, "_ensure_trust_accepted", lambda cwd: None)
    child = MagicMock()
    child.isalive.return_value = True
    monkeypatch.setattr("engine.cci_runner.pexpect.spawn", lambda *a, **k: child)
    if handshake_raises:
        def _hs(c):
            raise RuntimeError("handshake failed")
        monkeypatch.setattr(cci_runner, "_startup_handshake", _hs)
    else:
        monkeypatch.setattr(cci_runner, "_startup_handshake", lambda c: None)
    monkeypatch.setattr(cci_runner, "_send_prompt", lambda c, m: None)
    monkeypatch.setattr(cci_runner, "_send_exit", lambda c: None)
    monkeypatch.setattr(
        "engine.cci_runner._wait_for_completion",
        lambda *a, **k: completion,
    )
    return child


class TestMain:
    def test_delete_prompt_file_unlinks_in_finally(self, tmp_path, monkeypatch):
        _patch_main_internals(monkeypatch)
        pf = tmp_path / "prompt.txt"
        pf.write_text("do the thing")
        rc = cci_runner.main([
            "--session-id", "11111111-2222-3333-4444-555555555555",
            "--prompt-file", str(pf), "--delete-prompt-file",
            "--cwd", str(tmp_path),
        ])
        assert rc == 0
        assert not pf.exists()

    def test_without_delete_flag_keeps_file(self, tmp_path, monkeypatch):
        _patch_main_internals(monkeypatch)
        pf = tmp_path / "prompt.txt"
        pf.write_text("keep me")
        rc = cci_runner.main([
            "--session-id", "11111111-2222-3333-4444-555555555555",
            "--prompt-file", str(pf),
            "--cwd", str(tmp_path),
        ])
        assert rc == 0
        assert pf.exists()

    def test_stdin_prompt_no_unlink(self, tmp_path, monkeypatch):
        _patch_main_internals(monkeypatch)
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO("from stdin"))
        with patch("os.unlink") as mock_unlink:
            rc = cci_runner.main([
                "--session-id", "11111111-2222-3333-4444-555555555555",
                "--prompt-file", "-", "--delete-prompt-file",
                "--cwd", str(tmp_path),
            ])
        assert rc == 0
        mock_unlink.assert_not_called()

    def test_handshake_failure_reaps_child(self, tmp_path, monkeypatch):
        child = _patch_main_internals(monkeypatch, handshake_raises=True)
        pf = tmp_path / "prompt.txt"
        pf.write_text("x")
        rc = cci_runner.main([
            "--session-id", "11111111-2222-3333-4444-555555555555",
            "--prompt-file", str(pf),
            "--cwd", str(tmp_path),
        ])
        assert rc == 1
        child.terminate.assert_called_with(force=True)

    def test_completion_timeout_returns_nonzero(self, tmp_path, monkeypatch):
        _patch_main_internals(monkeypatch, completion=False)
        pf = tmp_path / "prompt.txt"
        pf.write_text("x")
        rc = cci_runner.main([
            "--session-id", "11111111-2222-3333-4444-555555555555",
            "--prompt-file", str(pf),
            "--cwd", str(tmp_path),
        ])
        assert rc == 1
