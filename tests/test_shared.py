"""Tests for engine/shared.py utilities."""


def test_log_uses_config_log_file(tmp_path, monkeypatch):
    """log() が config.LOG_FILE を遅延参照し、差し替え後のパスに書き込むことを検証。"""
    import config
    from engine.shared import log

    tmp_log = tmp_path / "test-watchdog.log"
    monkeypatch.setattr(config, "LOG_FILE", tmp_log)

    log("test message")

    assert tmp_log.exists()
    content = tmp_log.read_text()
    assert "test message" in content
