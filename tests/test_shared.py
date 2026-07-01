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


def test_sanitize_agent_message_nul():
    """NUL(\\x00) が U+2400(␀) へ可視化され、生 NUL が残らないこと。"""
    from engine.shared import sanitize_agent_message

    result = sanitize_agent_message("a\x00b")
    assert result == "a␀b"
    assert "\x00" not in result


def test_sanitize_agent_message_preserves_tab_lf_cr():
    """TAB/LF/CR は保持される。"""
    from engine.shared import sanitize_agent_message

    s = "x\ty\nz\r"
    assert sanitize_agent_message(s) == s


def test_sanitize_agent_message_c0_range():
    """代表的な C0 制御文字が対応する Control Pictures へ置換される。"""
    from engine.shared import sanitize_agent_message

    assert sanitize_agent_message("\x01") == "␁"  # U+2401
    assert sanitize_agent_message("\x1f") == "␟"  # U+241F


def test_sanitize_agent_message_del():
    """DEL(\\x7f) は U+2421(␡) へ置換される。"""
    from engine.shared import sanitize_agent_message

    assert sanitize_agent_message("\x7f") == "␡"


def test_sanitize_agent_message_idempotent():
    """二重適用しても不変（冪等）。"""
    from engine.shared import sanitize_agent_message

    s = "a\x00b\x01c\x7fd"
    once = sanitize_agent_message(s)
    assert sanitize_agent_message(once) == once


def test_sanitize_agent_message_empty():
    """空文字列は空文字列のまま。"""
    from engine.shared import sanitize_agent_message

    assert sanitize_agent_message("") == ""
