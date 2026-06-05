"""
test_llm_diagnostics.py
Tests for #33E read-only provider diagnostics.

Diagnostics reuse the existing resolution + validation path (resolve_role_config,
get_provider_for_role, provider.validate_config, the #33D fake guard). They make
NO LLM completion call and mutate nothing.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.config.keys import settings
from backend.db.database import engine
from backend.llm.diagnostics import diagnose_all_roles, diagnose_role
from backend.llm.providers.fake import FakeProvider
from backend.llm.role_config import Role
from backend.main import app

pytestmark = pytest.mark.unit

_SECRET = "SUPER_SECRET_KEY_VALUE_DO_NOT_LEAK"


def _set_gemini_key(monkeypatch, value):
    monkeypatch.setattr(settings, "gemini_api_key", value)


def _disallow_fake(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("PIPEWRIGHT_ALLOW_FAKE_PROVIDER", raising=False)


# --- per-role helper ---------------------------------------------------------

def test_one_entry_per_role(clear_llm_env):
    report = diagnose_all_roles()
    roles_seen = [d.role for d in report.roles]
    assert roles_seen == [r.value for r in Role]
    assert len(report.roles) == len(list(Role))


def test_configured_provider_is_available(clear_llm_env, monkeypatch):
    monkeypatch.setenv("CODER_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("CODER_LLM_MODEL", "gemini-2.5-flash-lite")
    _set_gemini_key(monkeypatch, _SECRET)

    diag = diagnose_role(Role.CODER)
    assert diag.status == "available"
    assert diag.validated is True
    assert diag.fake_blocked is False
    assert diag.provider == "gemini"
    assert diag.model == "gemini-2.5-flash-lite"
    # Static success copy never echoes the key.
    assert _SECRET not in diag.message


def test_missing_key_is_unavailable_sanitized(clear_llm_env, monkeypatch):
    monkeypatch.setenv("CODER_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("CODER_LLM_MODEL", "gemini-2.5-flash-lite")
    _set_gemini_key(monkeypatch, None)

    diag = diagnose_role(Role.CODER)
    assert diag.status == "unavailable"
    assert diag.validated is False
    assert "GEMINI_API_KEY is not configured" in diag.message
    assert _SECRET not in diag.message


def test_fake_without_allowance_is_blocked(clear_llm_env, monkeypatch):
    monkeypatch.setenv("CODER_LLM_PROVIDER", "fake")
    monkeypatch.setenv("CODER_LLM_MODEL", "fake-model")
    _disallow_fake(monkeypatch)

    diag = diagnose_role(Role.CODER)
    assert diag.status == "blocked"
    assert diag.fake_blocked is True
    assert diag.validated is False
    assert "Fake LLM provider is disabled outside tests" in diag.message


def test_fake_with_allowance_is_available(clear_llm_env, monkeypatch):
    monkeypatch.setenv("CODER_LLM_PROVIDER", "fake")
    monkeypatch.setenv("CODER_LLM_MODEL", "fake-model")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("PIPEWRIGHT_ALLOW_FAKE_PROVIDER", "true")

    diag = diagnose_role(Role.CODER)
    assert diag.status == "available"
    assert diag.fake_blocked is False
    assert diag.validated is True


def test_unsupported_model_is_unavailable(clear_llm_env, monkeypatch):
    monkeypatch.setenv("CODER_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("CODER_LLM_MODEL", "totally-not-a-real-model")
    _set_gemini_key(monkeypatch, _SECRET)

    diag = diagnose_role(Role.CODER)
    assert diag.status == "unavailable"
    assert "does not support model" in diag.message
    assert _SECRET not in diag.message


def test_unsupported_provider_is_unavailable(clear_llm_env, monkeypatch):
    monkeypatch.setenv("CODER_LLM_PROVIDER", "bogusprovider")
    monkeypatch.setenv("CODER_LLM_MODEL", "x")

    diag = diagnose_role(Role.CODER)
    assert diag.status == "unavailable"


def test_diagnostics_never_calls_complete(clear_llm_env, monkeypatch):
    # If diagnostics ever invoked a completion, this would raise.
    def _boom(*args, **kwargs):
        raise AssertionError("diagnostics must not call provider.complete")

    monkeypatch.setattr(FakeProvider, "complete", _boom)
    monkeypatch.setenv("CODER_LLM_PROVIDER", "fake")
    monkeypatch.setenv("CODER_LLM_MODEL", "fake-model")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("PIPEWRIGHT_ALLOW_FAKE_PROVIDER", "true")

    diag = diagnose_role(Role.CODER)
    assert diag.status == "available"  # validated without any completion call


def test_diagnostics_does_not_mutate_run_state(clear_llm_env):
    def _count(table):
        with engine.connect() as conn:
            return conn.execute(
                text(f"SELECT COUNT(*) FROM {table}")
            ).scalar_one()

    before = (_count("pipeline_runs"), _count("chunks"), _count("llm_call_provenance"))
    diagnose_all_roles()
    after = (_count("pipeline_runs"), _count("chunks"), _count("llm_call_provenance"))
    assert before == after


def test_messages_never_leak_secret(clear_llm_env, monkeypatch):
    _set_gemini_key(monkeypatch, _SECRET)
    report = diagnose_all_roles()
    for diag in report.roles:
        assert _SECRET not in diag.message


# --- route -------------------------------------------------------------------

def test_diagnostics_route_returns_per_role(clear_llm_env, monkeypatch):
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "gemini-2.5-flash-lite")
    _set_gemini_key(monkeypatch, _SECRET)

    client = TestClient(app)
    response = client.get("/llm/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert "roles" in body
    assert len(body["roles"]) == len(list(Role))
    for entry in body["roles"]:
        assert set(entry).issuperset(
            {"role", "provider", "model", "status", "message", "fake_blocked", "validated"}
        )
        assert entry["status"] in {"available", "unavailable", "blocked", "unknown"}
        assert _SECRET not in entry["message"]
