import logging
from unittest.mock import patch


def test_main_logging_no_stream_handler(tmp_path):
    """main() sets up only FileHandler, no StreamHandler."""
    log_file = tmp_path / "test.log"
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers.clear()

    try:
        with patch("watchdog.LOG_FILE", str(log_file)), \
             patch("watchdog.check_discord_commands"), \
             patch("watchdog.PIPELINES_DIR", tmp_path / "pipelines"):
            # PIPELINES_DIR が存在しないので early exit する
            from watchdog import main
            main()

        new_handlers = root.handlers[:]
        stream_handlers = [
            h for h in new_handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]
        assert len(stream_handlers) == 0, f"StreamHandler found: {stream_handlers}"
        file_handlers = [h for h in new_handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1
    finally:
        # クリーンアップ: main() が追加した handlers を除去し、元に戻す
        for h in root.handlers[:]:
            root.removeHandler(h)
            h.close()
        root.handlers = original_handlers
