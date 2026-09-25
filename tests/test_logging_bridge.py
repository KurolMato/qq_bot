import logging
from pathlib import Path

from loguru import logger as loguru_logger

from qq_bot.run import _install_logging_bridge


def test_stdlib_logging_reaches_loguru_file(tmp_path: Path) -> None:
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    log_path = tmp_path / "qq-bot.log"
    sink_id = loguru_logger.add(log_path, format="{message}", enqueue=False)
    try:
        _install_logging_bridge()
        logging.getLogger("qq_bot.test_logging_bridge").warning("bridge probe")
        logging.getLogger("uvicorn.error").warning("uvicorn probe")
        content = log_path.read_text(encoding="utf-8")
        assert "bridge probe" in content
        assert "uvicorn probe" not in content
    finally:
        loguru_logger.remove(sink_id)
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in old_handlers:
            root.addHandler(handler)
        root.setLevel(old_level)
