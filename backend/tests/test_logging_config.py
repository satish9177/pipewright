"""
test_logging_config.py
Tests for centralized backend logging configuration.
"""

import logging

import pytest

from backend.core import logging_config

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def restore_backend_logger():
    logger = logging.getLogger(logging_config.BACKEND_LOGGER_NAME)
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    logger.handlers = []
    yield
    logger.handlers = original_handlers
    logger.setLevel(original_level)
    logger.propagate = original_propagate


def _pipewright_handlers():
    logger = logging.getLogger(logging_config.BACKEND_LOGGER_NAME)
    return [
        handler for handler in logger.handlers
        if getattr(handler, logging_config._PIPEWRIGHT_HANDLER_ATTR, False)
    ]


def test_configure_logging_is_idempotent(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    logging_config.configure_logging()
    logging_config.configure_logging()

    assert len(_pipewright_handlers()) == 1


def test_configure_logging_respects_valid_log_level(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    logging_config.configure_logging()

    logger = logging.getLogger(logging_config.BACKEND_LOGGER_NAME)
    handler = _pipewright_handlers()[0]
    assert logger.level == logging.DEBUG
    assert handler.level == logging.DEBUG


def test_configure_logging_invalid_log_level_falls_back_to_info(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "LOUD")

    logging_config.configure_logging()

    logger = logging.getLogger(logging_config.BACKEND_LOGGER_NAME)
    handler = _pipewright_handlers()[0]
    assert logger.level == logging.INFO
    assert handler.level == logging.INFO


def test_configure_logging_does_not_crash(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    logging_config.configure_logging()

    assert _pipewright_handlers()
