"""tests/test_extend.py — cmd_extend() テスト"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    BLOCK_TIMERS,
    EXTEND_NOTICE_THRESHOLD,
    LOCAL_TZ,
    MAX_EXTENDS,
    MAX_TIMEOUT_EXTENSION,
)


def write_pipeline(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _history(state: str, seconds_ago: float) -> list[dict]:
    at = datetime.now(LOCAL_TZ) - timedelta(seconds=seconds_ago)
    return [{"from": "IDLE", "to": state, "at": at.isoformat(), "actor": "cli"}]


def _near_deadline(
    state: str, remaining: float = EXTEND_NOTICE_THRESHOLD - 60, extension: int = 0
) -> list[dict]:
    """期限（BLOCK_TIMERS[state] + extension）まで残り remaining 秒の history。"""
    return _history(state, BLOCK_TIMERS[state] + extension - remaining)


class TestCmdExtend:

    def test_extend_adds_timeout_extension(self, tmp_pipelines, sample_pipeline):
        """cmd_extend() でtimeout_extensionが加算されること"""
        sample_pipeline["state"] = "DESIGN_PLAN"
        sample_pipeline["history"] = _near_deadline("DESIGN_PLAN")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        cmd_extend(args)

        with open(path) as f:
            data = json.load(f)
        assert data["timeout_extension"] == 600

    def test_extend_cumulative(self, tmp_pipelines, sample_pipeline):
        """延長済み（+600, 1 回）から再び期限間際で extend すると累積加算されること"""
        sample_pipeline["state"] = "IMPLEMENTATION"
        sample_pipeline["timeout_extension"] = 600
        sample_pipeline["extend_count"] = 1
        sample_pipeline["history"] = _near_deadline("IMPLEMENTATION", extension=600)
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse

        args = argparse.Namespace(project="test-pj", by=300)
        cmd_extend(args)

        with open(path) as f:
            data = json.load(f)
        assert data["timeout_extension"] == 900
        assert data["extend_count"] == 2

    def test_extend_invalid_state_idle(self, tmp_pipelines, sample_pipeline):
        """IDLE状態でextendするとSystemExit"""
        sample_pipeline["state"] = "IDLE"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        with pytest.raises(SystemExit, match="Cannot extend"):
            cmd_extend(args)

    def test_extend_invalid_state_review(self, tmp_pipelines, sample_pipeline):
        """DESIGN_REVIEW状態でextendするとSystemExit"""
        sample_pipeline["state"] = "DESIGN_REVIEW"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        with pytest.raises(SystemExit, match="Cannot extend"):
            cmd_extend(args)

    def test_extend_design_plan(self, tmp_pipelines, sample_pipeline):
        """DESIGN_PLAN状態でextendが成功すること"""
        sample_pipeline["state"] = "DESIGN_PLAN"
        sample_pipeline["history"] = _near_deadline("DESIGN_PLAN")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        cmd_extend(args)  # Should not raise

        with open(path) as f:
            data = json.load(f)
        assert data["timeout_extension"] == 600

    def test_extend_design_revise(self, tmp_pipelines, sample_pipeline):
        """DESIGN_REVISE状態でextendが成功すること"""
        sample_pipeline["state"] = "DESIGN_REVISE"
        sample_pipeline["history"] = _near_deadline("DESIGN_REVISE")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        cmd_extend(args)  # Should not raise

        with open(path) as f:
            data = json.load(f)
        assert data["timeout_extension"] == 600

    def test_extend_code_revise(self, tmp_pipelines, sample_pipeline):
        """CODE_REVISE状態でextendが成功すること"""
        sample_pipeline["state"] = "CODE_REVISE"
        sample_pipeline["history"] = _near_deadline("CODE_REVISE")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        cmd_extend(args)  # Should not raise

        with open(path) as f:
            data = json.load(f)
        assert data["timeout_extension"] == 600

    def test_extend_implementation(self, tmp_pipelines, sample_pipeline):
        """IMPLEMENTATION状態でextendが成功すること"""
        sample_pipeline["state"] = "IMPLEMENTATION"
        sample_pipeline["history"] = _near_deadline("IMPLEMENTATION")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        cmd_extend(args)  # Should not raise

        with open(path) as f:
            data = json.load(f)
        assert data["timeout_extension"] == 600

    def test_extend_discord_notification(self, tmp_pipelines, sample_pipeline):
        """Discord通知が送信されること"""
        sample_pipeline["state"] = "DESIGN_PLAN"
        sample_pipeline["implementer"] = "implementer1"
        sample_pipeline["history"] = _near_deadline("DESIGN_PLAN")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from gokrax import cmd_extend
        import argparse
        args = argparse.Namespace(project="test-pj", by=600)

        with patch("commands.dev.notify_discord") as mock_discord:
            cmd_extend(args)

        mock_discord.assert_called_once()
        call_args = mock_discord.call_args[0][0]
        assert "test-pj" in call_args
        assert "implementer1" in call_args
        assert "extended timeout by 600s" in call_args
        assert "total +600s" in call_args


def _run_extend(by: int = 600):
    from gokrax import cmd_extend

    cmd_extend(argparse.Namespace(project="test-pj", by=by))


def _read(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


class TestCmdExtendPrematureGuard:
    """期限が遠いうちの extend を拒否する（#399）"""

    def _write(self, tmp_pipelines, sample_pipeline, state: str = "DESIGN_PLAN", **fields) -> Path:
        sample_pipeline["state"] = state
        sample_pipeline.update(fields)
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        return path

    def test_rejects_when_deadline_far(self, tmp_pipelines, sample_pipeline):
        path = self._write(tmp_pipelines, sample_pipeline, history=_history("DESIGN_PLAN", 60))
        before = path.read_text()
        with patch("commands.dev.notify_discord") as mock_discord:
            with pytest.raises(SystemExit, match="deadline is not near yet") as exc:
                _run_extend()
        assert isinstance(exc.value.code, str)
        assert path.read_text() == before
        mock_discord.assert_not_called()

    def test_rejects_without_history(self, tmp_pipelines, sample_pipeline):
        self._write(tmp_pipelines, sample_pipeline, history=[])
        with pytest.raises(SystemExit, match="deadline is not near yet"):
            _run_extend()

    def test_accepts_just_below_threshold(self, tmp_pipelines, sample_pipeline):
        path = self._write(
            tmp_pipelines, sample_pipeline,
            history=_near_deadline("DESIGN_PLAN", remaining=EXTEND_NOTICE_THRESHOLD - 5),
        )
        _run_extend()
        assert _read(path)["extend_count"] == 1

    def test_rejects_just_above_threshold(self, tmp_pipelines, sample_pipeline):
        self._write(
            tmp_pipelines, sample_pipeline,
            history=_near_deadline("DESIGN_PLAN", remaining=EXTEND_NOTICE_THRESHOLD + 5),
        )
        with pytest.raises(SystemExit, match="deadline is not near yet"):
            _run_extend()

    def test_accepts_just_before_deadline(self, tmp_pipelines, sample_pipeline):
        path = self._write(
            tmp_pipelines, sample_pipeline, history=_near_deadline("DESIGN_PLAN", remaining=5)
        )
        _run_extend()
        assert _read(path)["timeout_extension"] == 600

    def test_rejects_when_already_overdue(self, tmp_pipelines, sample_pipeline):
        path = self._write(
            tmp_pipelines, sample_pipeline, history=_near_deadline("DESIGN_PLAN", remaining=-5)
        )
        before = path.read_text()
        with patch("commands.dev.notify_discord") as mock_discord:
            with pytest.raises(SystemExit, match="deadline has already passed"):
                _run_extend()
        assert path.read_text() == before
        mock_discord.assert_not_called()

    def test_rejects_no_cc_implementation(self, tmp_pipelines, sample_pipeline):
        path = self._write(
            tmp_pipelines, sample_pipeline, state="IMPLEMENTATION", no_cc=True,
            history=_near_deadline("IMPLEMENTATION"),
        )
        before = path.read_text()
        with patch("commands.dev.notify_discord") as mock_discord:
            with pytest.raises(SystemExit, match="has no timeout right now"):
                _run_extend()
        assert path.read_text() == before
        mock_discord.assert_not_called()

    def test_second_call_right_after_success_rejected(self, tmp_pipelines, sample_pipeline):
        path = self._write(tmp_pipelines, sample_pipeline, history=_near_deadline("DESIGN_PLAN"))
        with patch("commands.dev.notify_discord") as mock_discord:
            _run_extend()
            with pytest.raises(SystemExit, match="already in effect"):
                _run_extend()
        data = _read(path)
        assert data["extend_count"] == 1
        assert data["timeout_extension"] == 600
        assert mock_discord.call_count == 1

    def test_remaining_includes_existing_extension(self, tmp_pipelines, sample_pipeline):
        # BLOCK_TIMERS だけなら残り 300s（閾値未満）だが、+600 済みなので実際の残りは 900s → 拒否
        self._write(
            tmp_pipelines, sample_pipeline,
            timeout_extension=600, extend_count=1,
            history=_near_deadline("DESIGN_PLAN", remaining=300),
        )
        with pytest.raises(SystemExit, match="already in effect"):
            _run_extend()

    def test_max_extends_reached(self, tmp_pipelines, sample_pipeline):
        self._write(
            tmp_pipelines, sample_pipeline,
            timeout_extension=1200, extend_count=MAX_EXTENDS,
            history=_near_deadline("DESIGN_PLAN", extension=1200),
        )
        with pytest.raises(SystemExit, match=rf"maximum extension count \({MAX_EXTENDS}\) reached"):
            _run_extend()

    @pytest.mark.parametrize("by", [0, -600, MAX_TIMEOUT_EXTENSION + 1])
    def test_invalid_by_rejected(self, tmp_pipelines, sample_pipeline, by):
        path = self._write(tmp_pipelines, sample_pipeline, history=_near_deadline("DESIGN_PLAN"))
        before = path.read_text()
        with patch("commands.dev.notify_discord") as mock_discord:
            with pytest.raises(SystemExit, match="Cannot extend: --by"):
                _run_extend(by=by)
        assert path.read_text() == before
        mock_discord.assert_not_called()

    def test_total_cap_rejected(self, tmp_pipelines, sample_pipeline):
        path = self._write(
            tmp_pipelines, sample_pipeline,
            timeout_extension=3300, extend_count=1,
            history=_near_deadline("DESIGN_PLAN", extension=3300),
        )
        with pytest.raises(SystemExit, match="total extension cap"):
            _run_extend(by=600)
        assert _read(path)["timeout_extension"] == 3300

    def test_accepts_up_to_total_cap(self, tmp_pipelines, sample_pipeline):
        path = self._write(
            tmp_pipelines, sample_pipeline,
            timeout_extension=3300, extend_count=1,
            history=_near_deadline("DESIGN_PLAN", extension=3300),
        )
        _run_extend(by=300)
        assert _read(path)["timeout_extension"] == MAX_TIMEOUT_EXTENSION

    def test_success_stdout(self, tmp_pipelines, sample_pipeline, capsys):
        self._write(tmp_pipelines, sample_pipeline, history=_near_deadline("DESIGN_PLAN"))
        _run_extend()
        out = capsys.readouterr().out
        assert "test-pj: timeout extended +600s (total +600s)." in out
        assert f"{MAX_EXTENDS - 1} extension(s) remaining." in out
        assert "Do not call extend again unless the deadline gets near again" in out

    def test_success_stdout_when_last_extension(self, tmp_pipelines, sample_pipeline, capsys):
        self._write(
            tmp_pipelines, sample_pipeline,
            timeout_extension=600, extend_count=MAX_EXTENDS - 1,
            history=_near_deadline("DESIGN_PLAN", extension=600),
        )
        _run_extend()
        out = capsys.readouterr().out
        assert "0 extension(s) remaining." in out
        assert "No extensions remain for this batch" in out
        assert "Do not call extend again unless" not in out

    def test_success_stdout_when_total_cap_used(self, tmp_pipelines, sample_pipeline, capsys):
        # 回数は 1 回残るが合計上限を使い切る → 再延長不能と表示する
        self._write(tmp_pipelines, sample_pipeline, history=_near_deadline("DESIGN_PLAN"))
        _run_extend(by=MAX_TIMEOUT_EXTENSION)
        out = capsys.readouterr().out
        assert f"(total +{MAX_TIMEOUT_EXTENSION}s)." in out
        assert "0 extension(s) remaining." in out
        assert "No extensions remain for this batch" in out
        assert "Do not call extend again unless" not in out


class TestSecondCallFrozenClock:
    """1 回目（残り r で受理, --by 600）から Δ 秒後の 2 回目。2 回目の残りは r + 600 - Δ（凍結時計, #399）"""

    BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=LOCAL_TZ)

    def _first_call(self, tmp_pipelines, sample_pipeline, monkeypatch, r: float, delta: float) -> Path:
        import engine.fsm as fsm

        clock = {"now": self.BASE + timedelta(seconds=BLOCK_TIMERS["DESIGN_PLAN"] - r)}

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["now"]

        monkeypatch.setattr(fsm, "_datetime", _Frozen)
        sample_pipeline.update(
            state="DESIGN_PLAN",
            history=[{"from": "IDLE", "to": "DESIGN_PLAN", "at": self.BASE.isoformat(), "actor": "cli"}],
        )
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        _run_extend()
        assert _read(path)["extend_count"] == 1
        clock["now"] += timedelta(seconds=delta)
        return path

    def test_rejected_when_elapsed_below_first_remaining(self, tmp_pipelines, sample_pipeline, monkeypatch):
        path = self._first_call(tmp_pipelines, sample_pipeline, monkeypatch, r=300, delta=100)
        with pytest.raises(SystemExit, match="already in effect"):
            _run_extend()
        assert _read(path)["extend_count"] == 1

    def test_rejected_at_exact_boundary(self, tmp_pipelines, sample_pipeline, monkeypatch):
        # Δ == r → 2 回目の残りはちょうど 600 → 拒否（remaining >= EXTEND_NOTICE_THRESHOLD）
        path = self._first_call(tmp_pipelines, sample_pipeline, monkeypatch, r=5, delta=5)
        with pytest.raises(SystemExit, match="already in effect"):
            _run_extend()
        assert _read(path)["extend_count"] == 1

    def test_accepted_when_elapsed_exceeds_first_remaining(self, tmp_pipelines, sample_pipeline, monkeypatch):
        # 許容する境界: Δ > r → 2 回目の残りは 595 → 時間帯内なので受理される
        path = self._first_call(tmp_pipelines, sample_pipeline, monkeypatch, r=5, delta=10)
        _run_extend()
        data = _read(path)
        assert data["extend_count"] == 2
        assert data["timeout_extension"] == 1200


class TestNoticeCommandIsAccepted:
    """案内に載るコマンドは、案内を組み立てた時点の pipeline に対して cmd_extend が受理する（#399）"""

    def _write(self, tmp_pipelines, sample_pipeline, extend_count: int, timeout_extension: int) -> Path:
        sample_pipeline.update(
            state="DESIGN_PLAN",
            extend_count=extend_count,
            timeout_extension=timeout_extension,
            history=_near_deadline("DESIGN_PLAN", extension=timeout_extension),
        )
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)
        return path

    @pytest.mark.parametrize(
        ("extend_count", "timeout_extension", "expected_by"),
        [(0, 0, 600), (1, 600, 600), (1, MAX_TIMEOUT_EXTENSION - 300, 300)],
    )
    def test_suggested_command_is_accepted(
        self, tmp_pipelines, sample_pipeline, extend_count, timeout_extension, expected_by
    ):
        from engine.fsm import _check_nudge

        path = self._write(tmp_pipelines, sample_pipeline, extend_count, timeout_extension)
        action = _check_nudge("DESIGN_PLAN", _read(path))
        m = re.search(r"extend --project test-pj --by (\d+)", action.extend_notice)
        assert m is not None
        by = int(m.group(1))
        assert by == expected_by
        _run_extend(by=by)
        data = _read(path)
        assert data["extend_count"] == extend_count + 1
        assert data["timeout_extension"] == timeout_extension + by

    @pytest.mark.parametrize(
        ("extend_count", "timeout_extension"),
        [(MAX_EXTENDS, 1200), (1, MAX_TIMEOUT_EXTENSION)],
    )
    def test_no_command_when_extend_would_be_rejected(
        self, tmp_pipelines, sample_pipeline, extend_count, timeout_extension
    ):
        from engine.fsm import _check_nudge

        path = self._write(tmp_pipelines, sample_pipeline, extend_count, timeout_extension)
        action = _check_nudge("DESIGN_PLAN", _read(path))
        assert "Extension limit reached" in action.extend_notice
        assert "extend --project" not in action.extend_notice
        with pytest.raises(SystemExit, match="Cannot extend"):
            _run_extend()
