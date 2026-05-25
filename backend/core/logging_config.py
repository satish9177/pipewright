"""
logging_config.py
Centralized backend logging setup.
"""

import logging
import os
import sys


DEFAULT_LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
BACKEND_LOGGER_NAME = "backend"
_PIPEWRIGHT_HANDLER_ATTR = "_pipewright_console_handler"


def _resolve_log_level(value: str | None) -> int:
    level_name = (value or DEFAULT_LOG_LEVEL).strip().upper()
    level = getattr(logging, level_name, None)
    if isinstance(level, int):
        return level
    return logging.INFO


def configure_logging() -> None:
    """
    Configure backend console logging.

    This function is safe to call repeatedly during pytest imports or uvicorn
    reloads. It updates the existing Pipewright handler instead of adding a
    duplicate, and it leaves uvicorn/root handlers in place.
    """
    level = _resolve_log_level(os.getenv("LOG_LEVEL"))
    formatter = logging.Formatter(LOG_FORMAT)
    logger = logging.getLogger(BACKEND_LOGGER_NAME)

    handler = None
    for existing_handler in logger.handlers:
        if getattr(existing_handler, _PIPEWRIGHT_HANDLER_ATTR, False):
            handler = existing_handler
            break

    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        setattr(handler, _PIPEWRIGHT_HANDLER_ATTR, True)
        logger.addHandler(handler)

    handler.setFormatter(formatter)
    handler.setLevel(level)
    logger.setLevel(level)
    logger.propagate = False
