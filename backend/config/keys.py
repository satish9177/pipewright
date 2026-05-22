"""
keys.py
Loads and validates all environment variables.
Raises clear errors on startup if anything is missing.
Never stores keys in memory beyond what is needed.
"""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    anthropic_api_key: str = Field(..., env="ANTHROPIC_API_KEY")
    target_repo_path: str = Field(..., env="TARGET_REPO_PATH")
    test_command: str = Field(..., env="TEST_COMMAND")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
