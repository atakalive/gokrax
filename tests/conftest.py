"""共通fixture — pipeline JSONのtmpディレクトリ等"""

import json
import pytest
from pathlib import Path


@pytest.fixture
def tmp_pipelines(tmp_path, monkeypatch):
    """PIPELINES_DIR を tmp_path に差し替え、テスト用パイプラインを返すヘルパー。"""
    import config
    monkeypatch.setattr(config, "PIPELINES_DIR", tmp_path)
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
