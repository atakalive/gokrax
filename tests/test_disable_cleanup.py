"""tests/test_disable_cleanup.py — cmd_disable 全PJ無効化時のクリーンアップテスト"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_cmd_disable_all_pj_disabled_removes_cron_and_cleans_up(tmp_path, monkeypatch):
    """全PJがdisabledになったとき、loop停止→PIDFILEのみ削除。LOCKFILEは維持（crontabは残す）。"""
    import gokrax
    import commands.dev.lifecycle as lifecycle

    fake_pidfile = tmp_path / "watchdog-loop.pid"
    fake_lockfile = tmp_path / "watchdog-loop.lock"
    fake_pidfile.write_text("12345")
    fake_lockfile.write_text("")

    monkeypatch.setattr(lifecycle, "WATCHDOG_LOOP_PIDFILE", fake_pidfile)
    monkeypatch.setattr(lifecycle, "WATCHDOG_LOOP_LOCKFILE", fake_lockfile)

    call_order: list[str] = []

    def mock_remove_cron_entry() -> None:
        call_order.append("_remove_cron_entry")

    def mock_stop_loop() -> None:
        call_order.append("_stop_loop")

    def mock_update_pipeline(path: Path, fn: object) -> None:
        fn({})

    with patch.object(gokrax, "_remove_cron_entry", side_effect=mock_remove_cron_entry), \
         patch.object(gokrax, "_stop_loop", side_effect=mock_stop_loop), \
         patch.object(gokrax, "_any_pj_enabled", return_value=False), \
         patch.object(lifecycle, "update_pipeline", side_effect=mock_update_pipeline), \
         patch.object(lifecycle, "get_path", return_value=tmp_path / "test.json"):
        args = MagicMock()
        args.project = "test"
        gokrax.cmd_disable(args)

    # _stop_loop のみ呼ばれる（crontab は残す — Issue #135）
    assert call_order == ["_stop_loop"]

    # PIDFILE は削除、LOCKFILE は維持（inode 不変で singleton 保護を保つ）
    assert not fake_pidfile.exists()
    assert fake_lockfile.exists()


def test_cmd_disable_partial_does_not_cleanup(tmp_path, monkeypatch):
    """他にenabledなPJがある場合、crontab削除もファイルクリーンアップも行わない。"""
    import gokrax
    import commands.dev.lifecycle as lifecycle

    fake_pidfile = tmp_path / "watchdog-loop.pid"
    fake_lockfile = tmp_path / "watchdog-loop.lock"
    fake_pidfile.write_text("12345")
    fake_lockfile.write_text("")

    monkeypatch.setattr(lifecycle, "WATCHDOG_LOOP_PIDFILE", fake_pidfile)
    monkeypatch.setattr(lifecycle, "WATCHDOG_LOOP_LOCKFILE", fake_lockfile)

    def mock_update_pipeline(path: Path, fn: object) -> None:
        fn({})

    with patch.object(gokrax, "_remove_cron_entry") as mock_remove, \
         patch.object(gokrax, "_stop_loop") as mock_stop, \
         patch.object(gokrax, "_any_pj_enabled", return_value=True), \
         patch.object(lifecycle, "update_pipeline", side_effect=mock_update_pipeline), \
         patch.object(lifecycle, "get_path", return_value=tmp_path / "test.json"):
        args = MagicMock()
        args.project = "test"
        gokrax.cmd_disable(args)

    mock_remove.assert_not_called()
    mock_stop.assert_not_called()

    assert fake_pidfile.exists()
    assert fake_lockfile.exists()


def test_cmd_disable_preserves_lockfile_inode(tmp_path, monkeypatch):
    """cmd_disable 後の LOCKFILE は同一 inode を維持する（flock singleton 保護）。"""
    import gokrax
    import commands.dev.lifecycle as lifecycle

    fake_pidfile = tmp_path / "watchdog-loop.pid"
    fake_lockfile = tmp_path / "watchdog-loop.lock"
    fake_pidfile.write_text("12345")
    fake_lockfile.write_text("")

    inode_before = fake_lockfile.stat().st_ino

    monkeypatch.setattr(lifecycle, "WATCHDOG_LOOP_PIDFILE", fake_pidfile)
    monkeypatch.setattr(lifecycle, "WATCHDOG_LOOP_LOCKFILE", fake_lockfile)

    def mock_update_pipeline(path: Path, fn: object) -> None:
        fn({})

    with patch.object(gokrax, "_stop_loop"), \
         patch.object(gokrax, "_any_pj_enabled", return_value=False), \
         patch.object(lifecycle, "update_pipeline", side_effect=mock_update_pipeline), \
         patch.object(lifecycle, "get_path", return_value=tmp_path / "test.json"):
        args = MagicMock()
        args.project = "test"
        gokrax.cmd_disable(args)

    assert fake_lockfile.stat().st_ino == inode_before
    assert not fake_pidfile.exists()
