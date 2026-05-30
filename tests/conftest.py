"""共通fixture — pipeline JSONのtmpディレクトリ等"""

import contextlib
import json
import os as _os
import pytest
from pathlib import Path
from unittest.mock import patch

# --- Hermetic config -------------------------------------------------------
# Load a committed test settings file instead of the developer's local,
# .gitignore'd settings.py. This MUST run before `config` is first imported
# (the `from notify import ...` below pulls in config transitively), so test
# behavior never depends on a machine-specific settings.py.
import importlib.util as _ilu  # noqa: E402

_HERMETIC_SETTINGS = Path(__file__).resolve().parent / "hermetic_settings.py"
_os.environ["GOKRAX_SETTINGS"] = str(_HERMETIC_SETTINGS)
_spec = _ilu.spec_from_file_location("_hermetic_settings", _HERMETIC_SETTINGS)
_hermetic_settings = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_hermetic_settings)

from notify import DiscordPostResult  # noqa: E402


@pytest.fixture(autouse=True)
def _block_external_calls(request, tmp_path):
    """全テストで外部通知（Discord投稿・エージェント送信）をブロック。
    LOG_FILE も tmp に差し替えてテストが本番ログを汚さないようにする。
    test_notify.py と test_config.py では適用しない（自前でmockするため）。
    """
    import config
    import watchdog
    orig_config = config.LOG_FILE
    orig_watchdog = watchdog.LOG_FILE
    tmp_log = tmp_path / "watchdog.log"
    config.LOG_FILE = tmp_log
    watchdog.LOG_FILE = tmp_log
    import commands.dev.helpers as _helpers
    orig_helpers = _helpers.LOG_FILE
    _helpers.LOG_FILE = tmp_log

    module = Path(request.node.fspath).stem
    if module in ("test_notify", "test_config", "test_short_context", "test_phase_override", "test_run_glab", "test_gemini_quota", "test_openai_codex_quota", "test_agy_quota"):
        yield
        config.LOG_FILE = orig_config
        watchdog.LOG_FILE = orig_watchdog
        _helpers.LOG_FILE = orig_helpers
        return
    _patches = [
        patch("notify.post_discord", return_value=DiscordPostResult("mock-msg-id")),
        patch("notify.send_to_agent", return_value=True),
        patch("notify.send_to_agent_queued", return_value=True),
        patch("notify.ping_agent", return_value=True),
        patch("watchdog.send_to_agent", return_value=True),
        patch("watchdog.send_to_agent_queued", return_value=True),
        patch("watchdog.ping_agent", return_value=True),
        patch("engine.fsm.send_to_agent", return_value=True),
        patch("engine.reviewer._reset_reviewers", return_value=[]),
        patch("engine.reviewer._reset_short_context_reviewers"),
        patch("engine.backend.soft_reap"),
        patch("watchdog._start_cc"),
        patch("watchdog._start_code_test"),
        patch("watchdog._start_cc_test_fix"),
        patch("watchdog.notify_discord"),
        patch("engine.glab.fetch_issue_state", return_value="opened"),
        patch("engine.gemini_quota.resolve_fallback", return_value=""),
        patch("engine.gemini_quota.should_fallback", return_value=(False, "", False)),
        patch("engine.gemini_quota.get_pro_quota", return_value=(False, 0.0, None)),
        patch("engine.openai_codex_quota.should_fallback", return_value=(False, "", "", False)),
        patch("engine.openai_codex_quota.get_codex_usage", return_value=(False, 0.0, None)),
        patch("engine.agy_quota.resolve_fallback", return_value=""),
        patch("engine.agy_quota.should_fallback", return_value=(False, "", False)),
        patch("engine.agy_quota.get_agy_quota", return_value=(False, 0.0, None)),
        patch("time.sleep"),
    ]
    with contextlib.ExitStack() as stack:
        for p in _patches:
            stack.enter_context(p)
        yield
    config.LOG_FILE = orig_config
    watchdog.LOG_FILE = orig_watchdog
    _helpers.LOG_FILE = orig_helpers


@pytest.fixture(autouse=True)
def block_dangerous_subprocess(monkeypatch):
    """Prevent tests from invoking real external processes."""
    import subprocess as _subprocess

    original_run = _subprocess.run
    original_popen = _subprocess.Popen

    BLOCKED_PATTERNS = ["claude", "glab", "pi"]

    def _check_cmd(cmd):
        if isinstance(cmd, (list, tuple)):
            cmd_str = " ".join(str(c) for c in cmd)
        else:
            cmd_str = str(cmd)
        for pattern in BLOCKED_PATTERNS:
            if pattern in cmd_str:
                raise RuntimeError(
                    f"Test attempted to invoke blocked process: {cmd_str!r}. "
                    f"Use mock/monkeypatch instead."
                )

    def guarded_run(cmd, *args, **kwargs):
        _check_cmd(cmd)
        return original_run(cmd, *args, **kwargs)

    def guarded_popen(cmd, *args, **kwargs):
        _check_cmd(cmd)
        return original_popen(cmd, *args, **kwargs)

    def blocked_os_system(cmd):
        raise RuntimeError(
            f"Test attempted to use os.system({cmd!r}). "
            f"Use subprocess + mock instead."
        )

    def blocked_os_popen(cmd, *args, **kwargs):
        raise RuntimeError(
            f"Test attempted to use os.popen({cmd!r}). "
            f"Use subprocess + mock instead."
        )

    monkeypatch.setattr(_subprocess, "run", guarded_run)
    monkeypatch.setattr(_subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(_os, "system", blocked_os_system)
    monkeypatch.setattr(_os, "popen", blocked_os_popen)


@pytest.fixture(autouse=True)
def _clear_default_queue_options(monkeypatch):
    """全テストで DEFAULT_QUEUE_OPTIONS / PROJECT_QUEUE_OPTIONS を空にし、デフォルト注入を無効化する。
    resolve_queue_options は config.* を直接参照するため、config モジュールの定義元をパッチする。
    """
    monkeypatch.setattr("config.DEFAULT_QUEUE_OPTIONS", {})
    monkeypatch.setattr("config.PROJECT_QUEUE_OPTIONS", {})


@pytest.fixture
def tmp_pipelines(tmp_path, monkeypatch):
    """PIPELINES_DIR を tmp_path に差し替え、テスト用パイプラインを返すヘルパー。"""
    import config
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
    # from config import で取り込んだローカル参照も差し替え
    for mod_name in ("pipeline_io", "gokrax", "commands.dev",
                      "commands.dev.lifecycle", "commands.dev.review", "commands.dev.queue"):
        try:
            import importlib
            mod = importlib.import_module(mod_name)
            monkeypatch.setattr(mod, "PIPELINES_DIR", tmp_path)
        except (ImportError, AttributeError):
            pass
    return tmp_path


# ---------------------------------------------------------------------------
# Test-only constants — use these instead of real agent/project names
# ---------------------------------------------------------------------------
# Sourced from tests/hermetic_settings.py (the committed config the suite
# loads) so these in-process overrides cannot drift from what config loads.
TEST_REVIEWERS = _hermetic_settings.REVIEWERS[:]
TEST_IMPLEMENTERS = _hermetic_settings.IMPLEMENTERS[:]
TEST_PROJECTS = ["project1", "project2", "project3"]
TEST_GITLAB_NS = _hermetic_settings.GITLAB_NAMESPACE

# Reviewer tiers / review modes using test-only names
TEST_REVIEWER_TIERS = _hermetic_settings.REVIEWER_TIERS
TEST_REVIEW_MODES = _hermetic_settings.REVIEW_MODES
# AGENTS maps all known agents (reviewers + implementers).
TEST_AGENTS = {name: f"agent:{name}:main" for name in TEST_REVIEWERS + TEST_IMPLEMENTERS}


@pytest.fixture(autouse=True)
def _override_config_names(monkeypatch):
    """Replace real agent/project names in config with test-only names."""
    import config
    # Preserve original values for CLI integration tests (subprocess doesn't inherit monkeypatch)
    if not hasattr(config, "_REAL_REVIEWERS"):
        config._REAL_REVIEWERS = config.REVIEWERS[:]
    _config_overrides = {
        "REVIEWERS": TEST_REVIEWERS[:],
        "IMPLEMENTERS": TEST_IMPLEMENTERS[:],
        "REVIEWER_TIERS": TEST_REVIEWER_TIERS,
        "REVIEW_MODES": TEST_REVIEW_MODES,
        "GITLAB_NAMESPACE": TEST_GITLAB_NS,
        "AGENTS": TEST_AGENTS,
    }
    for attr, val in _config_overrides.items():
        if hasattr(config, attr):
            monkeypatch.setattr(config, attr, val)
    # Patch modules that bind these config values at import time
    # (`from config import REVIEWERS/REVIEW_MODES/...`). If you add such a
    # module, add it here too, or its binding won't see these overrides
    # (see CLAUDE.md "Testing Rules"). spec_review / engine.fsm_spec are
    # listed for exactly this reason.
    for mod_name in ("notify", "engine.reviewer", "engine.fsm", "engine.fsm_spec",
                      "spec_review", "task_queue",
                      "commands.dev", "commands.dev.lifecycle", "commands.dev.review",
                      "commands.dev.queue", "commands.spec", "watchdog", "gokrax"):
        try:
            import importlib
            mod = importlib.import_module(mod_name)
            for attr, val in _config_overrides.items():
                if hasattr(mod, attr):
                    monkeypatch.setattr(mod, attr, val)
        except (ImportError, AttributeError):
            pass


@pytest.fixture
def sample_pipeline():
    """最小限のパイプラインデータ。"""
    return {
        "project": "test-pj",
        "gitlab": f"{TEST_GITLAB_NS}/test-pj",
        "state": "IDLE",
        "enabled": False,
        "implementer": TEST_IMPLEMENTERS[0],
        "review_mode": "standard",
        "batch": [],
        "history": [],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }


def write_pipeline(path: Path, data: dict):
    """テスト用: パイプラインJSONを書き込む。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
