"""File logging for the MCP.

stdout carries the MCP wire protocol, so log output must never touch it —
everything goes to a rotating file (config.log_file, default
~/Library/Logs/email-mcp.log). Logging must never break mail handling: if
the log file cannot be opened, the logger silently degrades to a
NullHandler.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from . import config

_LOGGER: logging.Logger | None = None


def get_logger() -> logging.Logger:
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER
    logger = logging.getLogger("email_mcp")
    logger.setLevel(getattr(logging, config.log_level(), logging.INFO))
    logger.propagate = False
    path = config.log_file()
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            logger.addHandler(handler)
        except OSError:
            logger.addHandler(logging.NullHandler())
    else:
        logger.addHandler(logging.NullHandler())
    _LOGGER = logger
    return logger
