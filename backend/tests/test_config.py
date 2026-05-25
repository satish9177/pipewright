"""
test_config.py
Tests for safe non-secret backend config.
"""

import logging

import pytest

from backend.core import logging_config
from backend.core.config import (
    DEFAULT_CORS_ALLOWED_ORIGINS,
    DEFAULT_LOG_LEVEL,
    DEFAULT_WS_ALLOWED_ORIGINS,
    get_config,
)

pytestmark = pytest.mark.unit


def test_default_config_preserves_current_local_origins(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("WS_ALLOWED_ORIGINS", raising=False)

    config = get_config()

    assert config.cors_allowed_origins == DEFAULT_CORS_ALLOWED_ORIGINS
    assert config.ws_allowed_origins == DEFAULT_WS_ALLOWED_ORIGINS
    assert "http://localhost:5173" in config.cors_allowed_origins
    assert "http://localhost:3000" in config.cors_allowed_origins
    assert "http://127.0.0.1:5173" in config.ws_allowed_origins
    assert "http://127.0.0.1:3000" in config.ws_allowed_origins


def test_comma_separated_origin_parsing_trims_and_ignores_empty(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        " http://localhost:5173, ,https://app.example.com ",
    )
    monkeypatch.setenv(
        "WS_ALLOWED_ORIGINS",
        " ws://localhost:5173, ,wss://app.example.com ",
    )

    config = get_config()

    assert config.cors_allowed_origins == (
        "http://localhost:5173",
        "https://app.example.com",
    )
    assert config.ws_allowed_origins == (
        "ws://localhost:5173",
        "wss://app.example.com",
    )


def test_empty_origin_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "  ")
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "")

    config = get_config()

    assert config.cors_allowed_origins == DEFAULT_CORS_ALLOWED_ORIGINS
    assert config.ws_allowed_origins == DEFAULT_WS_ALLOWED_ORIGINS


def test_log_level_default_remains_info(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    assert get_config().log_level == DEFAULT_LOG_LEVEL


def test_logging_config_respects_log_level_through_config(monkeypatch):
    logger = logging.getLogger(logging_config.BACKEND_LOGGER_NAME)
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    logger.handlers = []
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    try:
        logging_config.configure_logging()
        handler = [
            existing_handler for existing_handler in logger.handlers
            if getattr(
                existing_handler,
                logging_config._PIPEWRIGHT_HANDLER_ATTR,
                False,
            )
        ][0]

        assert logger.level == logging.DEBUG
        assert handler.level == logging.DEBUG
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate
