"""共通fixture — pipeline JSONのtmpディレクトリ等"""

import json
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
    if module in ("test_notify", "test_config"):
        yield
        config.LOG_FILE = orig_config
        watchdog.LOG_FILE = orig_watchdog
        return
    with patch("notify.post_discord", return_value="mock-msg-id"), \
         patch("notify.send_to_agent", return_value=True), \
         patch("notify.send_to_agent_queued", return_value=True), \
         patch("watchdog.send_to_agent", return_value=True), \
         patch("watchdog.send_to_agent_queued", return_value=True):
        yield
    config.LOG_FILE = orig_config
    watchdog.LOG_FILE = orig_watchdog


@pytest.fixture
def tmp_pipelines(tmp_path, monkeypatch):
    """PIPELINES_DIR を tmp_path に差し替え、テスト用パイプラインを返すヘルパー。"""
    import config
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
    # from config import で取り込んだローカル参照も差し替え
    for mod_name in ("pipeline_io", "devbar"):
        try:
            import importlib
            mod = importlib.import_module(mod_name)
            monkeypatch.setattr(mod, "PIPELINES_DIR", tmp_path)
        except (ImportError, AttributeError):
            pass
    return tmp_path


@pytest.fixture
def sample_pipeline():
    """最小限のパイプラインデータ。"""
    return {
        "project": "test-pj",
        "gitlab": "atakalive/test-pj",
        "state": "IDLE",
        "enabled": False,
        "implementer": "kaneko",
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
