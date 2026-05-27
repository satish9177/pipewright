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
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    pipewright_encryption_key: str | None = None
    target_repo_path: str | None = None
    test_command: str | None = None
    default_llm_provider: str | None = None
    default_llm_model: str | None = None
    triage_llm_provider: str | None = None
    triage_llm_model: str | None = None
    planner_llm_provider: str | None = None
    planner_llm_model: str | None = None
    coder_llm_provider: str | None = None
    coder_llm_model: str | None = None
    reviewer_llm_provider: str | None = None
    reviewer_llm_model: str | None = None
    summary_llm_provider: str | None = None
    summary_llm_model: str | None = None
    architect_llm_provider: str | None = None
    architect_llm_model: str | None = None

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore"
    }


settings = Settings()
