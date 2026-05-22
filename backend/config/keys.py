"""
keys.py
Loads and validates all environment variables.
Raises clear errors on startup if anything is missing.
Never stores keys in memory beyond what is needed.
"""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    gemini_api_key: str
    target_repo_path: str
    test_command: str

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8"
    }


settings = Settings()
