"""Tests for update_gokrax_state / load_gokrax_state in pipeline_io."""

import json
import threading

import pytest

import config
from pipeline_io import load_gokrax_state, update_gokrax_state


class TestUpdateGokraxState:
    """Test A: update_gokrax_state が排他ロック内で read-modify-write する"""

    def test_creates_file_from_scratch(self, tmp_path, monkeypatch):
        state_path = tmp_path / "gokrax-state.json"
        monkeypatch.setattr(config, "GOKRAX_STATE_PATH", state_path)

        def _cb(s):
            s["last_command_message_id"] = "123"

        result = update_gokrax_state(_cb)

        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert data["last_command_message_id"] == "123"
        assert "updated_at" not in data
        assert result["last_command_message_id"] == "123"


class TestUpdateGokraxStateCorrupt:
    """Test B: update_gokrax_state が JSON 破損時にデフォルトでフォールバックする"""

    def test_falls_back_on_corrupt_json(self, tmp_path, monkeypatch):
        state_path = tmp_path / "gokrax-state.json"
        state_path.write_text("{invalid json!!!")
        monkeypatch.setattr(config, "GOKRAX_STATE_PATH", state_path)

        def _cb(s):
            # callback に渡される state がデフォルト値ベースであることを検証
            assert s["last_command_message_id"] == "0"
            s["foo"] = "bar"

        result = update_gokrax_state(_cb)

        data = json.loads(state_path.read_text())
        assert data["foo"] == "bar"
        assert data["last_command_message_id"] == "0"
        assert result["foo"] == "bar"


class TestLoadGokraxState:
    """Test C: load_gokrax_state がファイル不在時にデフォルトを返す"""

    def test_returns_default_when_missing(self, tmp_path, monkeypatch):
        state_path = tmp_path / "nonexistent" / "gokrax-state.json"
        monkeypatch.setattr(config, "GOKRAX_STATE_PATH", state_path)

        result = load_gokrax_state()

        assert result == {"last_command_message_id": "0"}


class TestUpdateGokraxStateConcurrency:
    """Test D: update_gokrax_state の排他ロックが lost update を防止する"""

    def test_no_lost_updates(self, tmp_path, monkeypatch):
        state_path = tmp_path / "gokrax-state.json"
        state_path.write_text(json.dumps({"counter": 0}))
        monkeypatch.setattr(config, "GOKRAX_STATE_PATH", state_path)

        def _increment(s):
            s["counter"] = int(s.get("counter", 0)) + 1

        def _worker():
            for _ in range(50):
                update_gokrax_state(_increment)

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        data = json.loads(state_path.read_text())
        assert data["counter"] == 100


class TestUpdateGokraxStateCallbackException:
    """Test E: update_gokrax_state の callback 例外時にファイルが変更されない"""

    def test_file_unchanged_on_callback_error(self, tmp_path, monkeypatch):
        state_path = tmp_path / "gokrax-state.json"
        state_path.write_text(json.dumps({"key": "original"}))
        monkeypatch.setattr(config, "GOKRAX_STATE_PATH", state_path)

        def _cb(s):
            raise ValueError("test")

        with pytest.raises(ValueError, match="test"):
            update_gokrax_state(_cb)

        data = json.loads(state_path.read_text())
        assert data["key"] == "original"
