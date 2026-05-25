"""
config.py
Safe non-secret backend configuration.
"""

import os
from dataclasses import dataclass


DEFAULT_APP_ENV = "local"
DEFAULT_APP_NAME = "Pipewright"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_CORS_ALLOWED_ORIGINS = (
    "http://localhost:5173",
    "http://localhost:3000",
)
DEFAULT_WS_ALLOWED_ORIGINS = (
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
)


@dataclass(frozen=True)
class AppConfig:
    app_env: str
    app_name: str
    log_level: str
    cors_allowed_origins: tuple[str, ...]
    ws_allowed_origins: tuple[str, ...]


def _read_string(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _read_origin_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default

    origins = tuple(
        origin.strip()
        for origin in value.split(",")
        if origin.strip()
    )
    return origins or default


def get_config() -> AppConfig:
    return AppConfig(
        app_env=_read_string("APP_ENV", DEFAULT_APP_ENV),
        app_name=_read_string("APP_NAME", DEFAULT_APP_NAME),
        log_level=_read_string("LOG_LEVEL", DEFAULT_LOG_LEVEL),
        cors_allowed_origins=_read_origin_list(
            "CORS_ALLOWED_ORIGINS",
            DEFAULT_CORS_ALLOWED_ORIGINS,
        ),
        ws_allowed_origins=_read_origin_list(
            "WS_ALLOWED_ORIGINS",
            DEFAULT_WS_ALLOWED_ORIGINS,
        ),
    )
