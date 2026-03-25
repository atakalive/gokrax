"""tests/test_spec_done_stop.py — cmd_spec_done の enabled=False / _stop_loop テスト (Issue #221)"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _write_pipeline(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_pipeline(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _make_spec_done_pipeline(project: str) -> dict:
    return {
        "project": project,
        "gitlab": f"testns/{project}",
        "state": "SPEC_DONE",
        "enabled": True,
        "implementer": "implementer1",
        "batch": [],
        "history": [],
        "spec_mode": True,
        "spec_config": {"spec_path": "test.md"},
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }


class TestCmdSpecDoneEnabled:
    """Test C: cmd_spec_done が enabled=False を設定する（normal completion 経路）"""

    def test_sets_enabled_false(self, tmp_pipelines):
        pj = "spec-pj"
        path = tmp_pipelines / f"{pj}.json"
        _write_pipeline(path, _make_spec_done_pipeline(pj))

        args = argparse.Namespace(project=pj)
        from commands.spec import cmd_spec_done
        with patch("gokrax._any_pj_enabled", return_value=True), \
             patch("gokrax._stop_loop"):
            cmd_spec_done(args)

        saved = _read_pipeline(path)
        assert saved["state"] == "IDLE"
        assert saved["enabled"] is False
        assert saved["spec_mode"] is False
        assert saved["spec_config"] == {}


class TestCmdSpecDoneStopLoop:
    """Test D/E: cmd_spec_done の _stop_loop 呼び出し"""

    def test_calls_stop_loop_when_no_pj_enabled(self, tmp_pipelines, capsys):
        """Test D: 他PJ全無効時、_stop_loop が呼ばれる（normal completion）"""
        pj = "spec-pj"
        path = tmp_pipelines / f"{pj}.json"
        _write_pipeline(path, _make_spec_done_pipeline(pj))

        args = argparse.Namespace(project=pj)
        from commands.spec import cmd_spec_done
        with patch("gokrax._any_pj_enabled", return_value=False), \
             patch("gokrax._stop_loop") as mock_stop:
            cmd_spec_done(args)
            mock_stop.assert_called_once()

        out = capsys.readouterr().out
        assert "loop stopped" in out

    def test_no_stop_loop_when_other_pj_enabled(self, tmp_pipelines, capsys):
        """Test E: 他PJ有効時、_stop_loop は呼ばれない"""
        pj = "spec-pj"
        path = tmp_pipelines / f"{pj}.json"
        _write_pipeline(path, _make_spec_done_pipeline(pj))

        args = argparse.Namespace(project=pj)
        from commands.spec import cmd_spec_done
        with patch("gokrax._any_pj_enabled", return_value=True), \
             patch("gokrax._stop_loop") as mock_stop:
            cmd_spec_done(args)
            mock_stop.assert_not_called()

        out = capsys.readouterr().out
        assert "loop stopped" not in out
        assert "watchdog disabled" in out
