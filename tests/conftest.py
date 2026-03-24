"""共通fixture — pipeline JSONのtmpディレクトリ等"""

import json
import os as _os
import pytest
from pathlib import Path
from unittest.mock import patch


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

    module = Path(request.node.fspath).stem
    if module in ("test_notify", "test_config", "test_short_context"):
        yield
        config.LOG_FILE = orig_config
        watchdog.LOG_FILE = orig_watchdog
        return
    with patch("notify.post_discord", return_value="mock-msg-id"), \
         patch("notify.send_to_agent", return_value=True), \
         patch("notify.send_to_agent_queued", return_value=True), \
         patch("notify.ping_agent", return_value=True), \
         patch("watchdog.send_to_agent", return_value=True), \
         patch("watchdog.send_to_agent_queued", return_value=True), \
         patch("watchdog.ping_agent", return_value=True), \
         patch("engine.reviewer._reset_reviewers", return_value=[]), \
         patch("engine.reviewer._reset_short_context_reviewers"), \
         patch("watchdog._start_cc"), \
         patch("watchdog._start_code_test"), \
         patch("watchdog._start_cc_test_fix"), \
         patch("watchdog.notify_discord"), \
         patch("time.sleep"):
        yield
    config.LOG_FILE = orig_config
    watchdog.LOG_FILE = orig_watchdog


@pytest.fixture(autouse=True)
def block_dangerous_subprocess(monkeypatch):
    """Prevent tests from invoking real external processes."""
    import subprocess as _subprocess

    original_run = _subprocess.run
    original_popen = _subprocess.Popen

    BLOCKED_PATTERNS = ["claude", "glab"]

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
    """全テストで DEFAULT_QUEUE_OPTIONS を空にし、デフォルト注入を無効化する。
    新規テストだけが明示的にデフォルトを設定してテストする。
    """
    monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {})


@pytest.fixture
def tmp_pipelines(tmp_path, monkeypatch):
    """PIPELINES_DIR を tmp_path に差し替え、テスト用パイプラインを返すヘルパー。"""
    import config
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
    # from config import で取り込んだローカル参照も差し替え
    for mod_name in ("pipeline_io", "gokrax", "commands.dev"):
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
TEST_REVIEWERS = ["reviewer1", "reviewer2", "reviewer3", "reviewer4", "reviewer5"]
TEST_IMPLEMENTERS = ["implementer1", "implementer2"]
TEST_PROJECTS = ["project1", "project2", "project3"]
TEST_GITLAB_NS = "testns"

# Reviewer tiers / review modes using test-only names
TEST_REVIEWER_TIERS = {
    "regular": ["reviewer1", "reviewer3", "reviewer6"],
    "free": [],
    "short-context": ["reviewer2", "reviewer4", "reviewer5"],
}
TEST_REVIEW_MODES = {
    "full": {"members": ["reviewer1", "reviewer3", "reviewer5", "reviewer6"], "min_reviews": 4, "grace_period_sec": 0},
    "standard": {"members": ["reviewer1", "reviewer3", "reviewer6"], "min_reviews": 3, "grace_period_sec": 0},
    "lite": {"members": ["reviewer1", "reviewer3"], "min_reviews": 2, "grace_period_sec": 0},
    "min": {"members": ["reviewer1"], "min_reviews": 1, "grace_period_sec": 0},
    "skip": {"members": [], "min_reviews": 0, "grace_period_sec": 0},
}
# AGENTS maps all known agents (reviewers + implementers).
TEST_AGENTS = {name: f"agent:{name}:main" for name in TEST_REVIEWERS + TEST_IMPLEMENTERS}
TEST_ALLOWED_REVIEWERS_AND_IMPLEMENTERS = TEST_REVIEWERS + TEST_IMPLEMENTERS


@pytest.fixture(autouse=True)
def _override_config_names(monkeypatch):
    """Replace real agent/project names in config with test-only names."""
    import config
    # Preserve original values for CLI integration tests (subprocess doesn't inherit monkeypatch)
    if not hasattr(config, "_REAL_ALLOWED_REVIEWERS"):
        config._REAL_ALLOWED_REVIEWERS = config.ALLOWED_REVIEWERS[:]
    _config_overrides = {
        "ALLOWED_REVIEWERS": TEST_ALLOWED_REVIEWERS_AND_IMPLEMENTERS[:],
        "REVIEWER_TIERS": TEST_REVIEWER_TIERS,
        "REVIEW_MODES": TEST_REVIEW_MODES,
        "GITLAB_NAMESPACE": TEST_GITLAB_NS,
        "AGENTS": TEST_AGENTS,
    }
    for attr, val in _config_overrides.items():
        if hasattr(config, attr):
            monkeypatch.setattr(config, attr, val)
    # Patch modules that import these at module level
    for mod_name in ("notify", "engine.reviewer", "engine.fsm", "task_queue",
                      "commands.dev", "commands.spec", "watchdog", "gokrax"):
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
