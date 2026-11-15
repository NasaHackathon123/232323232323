"""Logging with a safety net: the MAP_KEY is scrubbed from every log line."""

from __future__ import annotations

import logging
import logging.handlers
from typing import Optional

from .config import Settings

LOGGER_NAME = "emberwatch"


class RedactingFilter(logging.Filter):
    def __init__(self, secret: Optional[str]):
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secret:
            try:
                msg = record.getMessage()
            except Exception:  # pragma: no cover - defensive
                return True
            if self._secret in msg:
                record.msg = msg.replace(self._secret, "***")
                record.args = ()
        return True


def setup_logging(cfg: Settings, to_file: bool = True) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, cfg.log_level, logging.INFO))
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s.%(module)s: %(message)s")
    redactor = RedactingFilter(cfg.map_key)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(redactor)
    logger.addHandler(console)

    if not to_file:
        return logger
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        fileh = logging.handlers.RotatingFileHandler(
            cfg.log_dir / "emberwatch.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        fileh.setFormatter(fmt)
        fileh.addFilter(redactor)
        logger.addHandler(fileh)
    except OSError:  # read-only file system etc.: console logging still works
        logger.warning("Could not open the log file; logging to console only.")
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
