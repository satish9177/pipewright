from pathlib import Path

import pytest

import backend.llm as llm
from backend.config.keys import settings
from backend.llm import validate_all_roles
from backend.llm.errors import UnsupportedModelError, UnsupportedProviderError

pytestmark = pytest.mark.unit


def _clear_llm_env(monkeypatch):
    for name in (
        "DEFAULT_LLM_PROVIDER",
        "DEFAULT_LLM_MODEL",
        "TRIAGE_LLM_PROVIDER",
        "TRIAGE_LLM_MODEL",
        "PLANNER_LLM_PROVIDER",
        "PLANNER_LLM_MODEL",
        "CODER_LLM_PROVIDER",
        "CODER_LLM_MODEL",
        "REVIEWER_LLM_PROVIDER",
        "REVIEWER_LLM_MODEL",
        "SUMMARY_LLM_PROVIDER",
        "SUMMARY_LLM_MODEL",
        "ARCHITECT_LLM_PROVIDER",
        "ARCHITECT_LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_validate_all_roles_passes_with_mocked_gemini_key(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setattr(settings, "gemini_api_key", "AIzaSyA123456789012345678901234567890")

    validate_all_roles()


def test_validate_all_roles_unknown_provider_raises(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "missing")
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "fake-model")

    with pytest.raises(UnsupportedProviderError):
        validate_all_roles()


def test_validate_all_roles_unsupported_model_raises(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "fake")
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "not-supported")

    with pytest.raises(UnsupportedModelError):
        validate_all_roles()


def test_backend_llm_google_imports_only_in_gemini_provider():
    llm_root = Path(llm.__file__).parent
    offenders = []

    for path in llm_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "google.generativeai" not in text:
            continue
        relative = path.relative_to(llm_root).as_posix()
        if relative != "providers/gemini.py":
            offenders.append(relative)

    assert offenders == []
