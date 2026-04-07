"""Tests for shlex.quote() usage in engine/cc.py bash script generation."""

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_script(os_write_calls: list) -> str:
    """Extract the script content from os.write calls.

    The script file is the last os.write call (after plan/impl prompts).
    """
    for fd, data in reversed(os_write_calls):
        text = data.decode()
        if text.startswith("#!/bin/bash"):
            return text
    raise AssertionError("No bash script found in os.write calls")


def _make_os_write_capture() -> tuple:
    """Return (side_effect_fn, captured_list) for patching os.write."""
    captured: list[tuple[int, bytes]] = []

    def _fake_write(fd: int, data: bytes) -> int:
        captured.append((fd, data))
        return len(data)

    return _fake_write, captured


# ---------------------------------------------------------------------------
# Common mock data
# ---------------------------------------------------------------------------

_PIPELINE_DATA = {
    "skip_cc_plan": False,
    "cc_plan_model": "",
    "cc_impl_model": "",
    "queue_mode": False,
    "keep_ctx_batch": False,
    "cc_session_id": "",
    "base_commit": "abc1234",
    "repo_path": "/safe/path",
}

_BATCH = [{"issue": 1, "title": "Test issue"}]


def _apply_cc_patches(stack: ExitStack, fake_write, mkstemp_returns: list,
                      pipeline_data: dict | None = None,
                      extra_patches: dict | None = None) -> None:
    """Apply common patches for _start_cc tests via an ExitStack."""
    pd = pipeline_data if pipeline_data is not None else dict(_PIPELINE_DATA)
    stack.enter_context(patch("engine.cc.load_pipeline", return_value=pd))
    stack.enter_context(patch("engine.cc.update_pipeline"))
    stack.enter_context(patch("notify.fetch_issue_body", return_value="body text"))
    stack.enter_context(patch("subprocess.run",
                              return_value=MagicMock(returncode=0, stdout="abc1234\n")))
    stack.enter_context(patch("subprocess.Popen",
                              return_value=MagicMock(pid=12345)))
    stack.enter_context(patch("os.close"))
    stack.enter_context(patch("os.chmod"))
    stack.enter_context(patch("tempfile.mkstemp", side_effect=mkstemp_returns))
    stack.enter_context(patch("os.write", side_effect=fake_write))
    if extra_patches:
        for target, mock_val in extra_patches.items():
            stack.enter_context(patch(target, mock_val))


# ===========================================================================
# Test 1: _start_cc normal — repo_path is single-quoted
# ===========================================================================

class TestStartCcNormalQuotesRepoPath:
    def test_start_cc_normal_quotes_repo_path(self) -> None:
        from engine.cc import _start_cc

        malicious_repo = "path with $(whoami)"
        pipeline_data = dict(_PIPELINE_DATA, repo_path=malicious_repo)
        fake_write, captured = _make_os_write_capture()

        mkstemp_returns = [
            (10, "/tmp/gokrax-plan.txt"),
            (11, "/tmp/gokrax-impl.txt"),
            (12, "/tmp/gokrax-cc.sh"),
        ]

        with ExitStack() as stack:
            _apply_cc_patches(stack, fake_write, mkstemp_returns,
                              pipeline_data=pipeline_data)
            _start_cc("testpj", _BATCH, "git@example.com:a/b.git",
                       malicious_repo, Path("/tmp/dummy.json"))

        script = _capture_script(captured)
        # repo_path must be single-quoted by shlex.quote
        assert "'path with $(whoami)'" in script
        # Must NOT appear double-quoted (vulnerable form)
        assert '"path with $(whoami)"' not in script


# ===========================================================================
# Test 2: _start_cc skip_plan — impl_model is single-quoted
# ===========================================================================

class TestStartCcSkipPlanQuotesModel:
    def test_start_cc_skip_plan_quotes_model(self) -> None:
        from engine.cc import _start_cc

        malicious_model = "model$(id)"
        pipeline_data = dict(_PIPELINE_DATA, skip_cc_plan=True,
                             cc_impl_model=malicious_model)
        fake_write, captured = _make_os_write_capture()

        mkstemp_returns = [
            (11, "/tmp/gokrax-impl.txt"),
            (12, "/tmp/gokrax-cc.sh"),
        ]

        with ExitStack() as stack:
            _apply_cc_patches(stack, fake_write, mkstemp_returns,
                              pipeline_data=pipeline_data)
            _start_cc("testpj", _BATCH, "git@example.com:a/b.git",
                       "/safe/repo", Path("/tmp/dummy.json"))

        script = _capture_script(captured)
        # impl_model must be single-quoted
        assert "'model$(id)'" in script
        # Must NOT appear double-quoted
        assert '"model$(id)"' not in script


# ===========================================================================
# Test 3: _start_code_test — all paths quoted, test_command NOT quoted
# ===========================================================================

class TestStartCodeTestQuotesAllPaths:
    def test_start_code_test_quotes_all_paths(self) -> None:
        from engine.cc import _start_code_test

        malicious_repo = "/repo/$(whoami)/path"
        malicious_tmp = "/tmp/evil $(id)"

        data = {
            "repo_path": malicious_repo,
            "_pytest_baseline": None,
        }

        fake_write, captured = _make_os_write_capture()

        mkstemp_returns = [
            (20, malicious_tmp + "/output.txt"),
            (21, malicious_tmp + "/script.sh"),
        ]

        with patch("subprocess.Popen", return_value=MagicMock(pid=999)), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="abc123\n")), \
             patch("tempfile.mkstemp", side_effect=mkstemp_returns), \
             patch("os.write", side_effect=fake_write), \
             patch("os.close"), \
             patch("os.chmod"), \
             patch("engine.cc.update_pipeline"), \
             patch("engine.cc._kill_pytest_baseline"), \
             patch("config.TEST_CONFIG", {"testpj": {"test_command": "pytest -x"}}):
            _start_code_test("testpj", data, Path("/tmp/dummy.json"))

        script = _capture_script(captured)
        # repo_path must be single-quoted
        assert "cd '/repo/$(whoami)/path'" in script
        # tempfile paths must be single-quoted
        assert "'" + malicious_tmp + "/output.txt'" in script
        assert "'" + malicious_tmp + "/script.sh'" in script
        # test_command must NOT be single-quoted (owner-controlled shell command)
        assert "pytest -x >" in script


# ===========================================================================
# Test 4: _start_cc_test_fix — all vars quoted
# ===========================================================================

class TestStartCcTestFixQuotesAll:
    def test_start_cc_test_fix_quotes_all(self) -> None:
        from engine.cc import _start_cc_test_fix

        malicious_repo = "/repo/$(whoami)"
        malicious_model = "model$(id)"
        malicious_session = "sess$(cat /etc/passwd)"

        data = {
            "cc_impl_model": malicious_model,
            "cc_session_id": malicious_session,
            "test_output": "FAILED test_foo.py",
            "test_retry_count": 1,
            "repo_path": malicious_repo,
        }

        fake_write, captured = _make_os_write_capture()

        mkstemp_returns = [
            (30, "/tmp/gokrax-testfix-prompt.txt"),
            (31, "/tmp/gokrax-testfix-script.sh"),
        ]

        with patch("subprocess.Popen", return_value=MagicMock(pid=888)), \
             patch("tempfile.mkstemp", side_effect=mkstemp_returns), \
             patch("os.write", side_effect=fake_write), \
             patch("os.close"), \
             patch("os.chmod"), \
             patch("engine.cc.update_pipeline"), \
             patch("config.MAX_TEST_RETRY", 3):
            _start_cc_test_fix("testpj", _BATCH, data, Path("/tmp/dummy.json"))

        script = _capture_script(captured)
        # All variables must be single-quoted
        assert "'/repo/$(whoami)'" in script
        assert "'model$(id)'" in script
        assert "'sess$(cat /etc/passwd)'" in script
        # Must NOT appear double-quoted
        assert '"/repo/$(whoami)"' not in script
        assert '"model$(id)"' not in script


# ===========================================================================
# Test 5: _start_cc — notification messages with $(...) are single-quoted
# ===========================================================================

class TestMsgVariablesQuoted:
    def test_msg_variables_quoted(self) -> None:
        from engine.cc import _start_cc

        fake_write, captured = _make_os_write_capture()

        def mock_render(category: str, template: str, **kwargs: object) -> str:
            if template == "notify_cc_plan_start":
                return "Plan start $(whoami)"
            if template == "notify_cc_impl_done":
                return "Impl done $(id)"
            if template == "notify_cc_no_commit_blocked":
                return "Blocked $(uname)"
            return f"safe-{template}"

        mkstemp_returns = [
            (10, "/tmp/gokrax-plan.txt"),
            (11, "/tmp/gokrax-impl.txt"),
            (12, "/tmp/gokrax-cc.sh"),
        ]

        with ExitStack() as stack:
            _apply_cc_patches(stack, fake_write, mkstemp_returns,
                              extra_patches={
                                  "engine.cc.render": mock_render,
                              })
            _start_cc("testpj", _BATCH, "git@example.com:a/b.git",
                       "/safe/repo", Path("/tmp/dummy.json"))

        script = _capture_script(captured)
        # All notification messages must be single-quoted
        assert "'Plan start $(whoami)'" in script
        assert "'Impl done $(id)'" in script
        assert "'Blocked $(uname)'" in script


# ===========================================================================
# Test 6: _start_cc — $RETRY is NOT single-quoted (must expand at bash runtime)
# ===========================================================================

class TestRetryNotificationPreservesBashVar:
    def test_retry_notification_preserves_bash_var(self) -> None:
        from engine.cc import _start_cc

        fake_write, captured = _make_os_write_capture()

        mkstemp_returns = [
            (10, "/tmp/gokrax-plan.txt"),
            (11, "/tmp/gokrax-impl.txt"),
            (12, "/tmp/gokrax-cc.sh"),
        ]

        with ExitStack() as stack:
            _apply_cc_patches(stack, fake_write, mkstemp_returns)
            _start_cc("testpj", _BATCH, "git@example.com:a/b.git",
                       "/safe/repo", Path("/tmp/dummy.json"))

        script = _capture_script(captured)
        # $RETRY must appear in double quotes (for bash expansion), NOT single-quoted
        assert '"$RETRY/2"' in script
        # The _notify line with retry should use bash string concatenation
        # where static parts are single-quoted and $RETRY is double-quoted
        for line in script.splitlines():
            if "$RETRY/2" in line and "_notify" in line:
                # $RETRY/2 must be in double quotes
                assert '"$RETRY/2"' in line
                # The line should also have single-quoted segments (static parts)
                assert "'" in line
                break
        else:
            pytest.fail("No _notify line with $RETRY/2 found in script")
