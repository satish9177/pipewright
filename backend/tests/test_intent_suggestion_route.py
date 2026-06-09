"""
test_intent_suggestion_route.py
#43A: advisory intent-mode suggestion endpoint (POST /runs/intent-suggestion).

The endpoint is read-only and side-effect-free: it never creates a run, never
writes to the DB, and never issues a new LLM call (the classifier runs
deterministic-only). It mirrors the #42B classifier so the UI can pre-highlight
a mode, but the suggestion is advisory and never authoritative.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.pipeline.intent import (
    IntentDecision,
    build_intent_mode_suggestion,
    suggest_intent_mode,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def no_llm(monkeypatch):
    """
    Fail loudly if the suggestion path ever issues an LLM call. The endpoint
    must stay deterministic and cheap (safe to call while the user types).
    """
    def _boom(*args, **kwargs):
        raise AssertionError(
            "Intent suggestion must not issue an LLM call (deterministic only)."
        )

    monkeypatch.setattr("backend.pipeline.intent.complete_for_role", _boom)


def _count_all_runs() -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM pipeline_runs")
        ).fetchone()[0]


def _suggest(client, feature: str):
    return client.post(
        "/runs/intent-suggestion", json={"feature_description": feature}
    )


# --- A–D: classifier-mirrored suggestions ----------------------------------


def test_a_report_only_suggested(no_llm):
    client = TestClient(app)
    response = _suggest(client, "Explain src/app.py. Do not change code.")
    assert response.status_code == 200
    data = response.json()
    assert data["suggested_mode"] == "report_only"
    assert data["detected_intent"] == "report_only"
    assert data["confidence"] == "high"
    assert isinstance(data["reason"], str) and data["reason"]


def test_b_plan_only_suggested(no_llm):
    client = TestClient(app)
    response = _suggest(client, "Plan how to add auth. Do not implement yet.")
    assert response.status_code == 200
    data = response.json()
    assert data["suggested_mode"] == "plan_only"
    assert data["detected_intent"] == "plan_only"
    assert data["confidence"] == "high"


def test_c_implementation_suggested(no_llm):
    client = TestClient(app)
    response = _suggest(client, "Implement add(a,b) in src/app.py.")
    assert response.status_code == 200
    data = response.json()
    assert data["suggested_mode"] == "implementation"
    assert data["detected_intent"] == "implementation"
    assert data["confidence"] == "high"


def test_d_contradiction_is_uncertain(no_llm):
    client = TestClient(app)
    response = _suggest(client, "Implement add(a,b) but do not change any code.")
    assert response.status_code == 200
    data = response.json()
    assert data["suggested_mode"] is None
    assert data["confidence"] == "uncertain"
    assert data["detected_intent"] == "needs_clarification"


def test_ambiguous_text_is_uncertain_without_llm(no_llm):
    # No deterministic layer matches and the LLM fallback is disabled for the
    # suggestion path → uncertain (no fabricated plan_only default).
    client = TestClient(app)
    response = _suggest(client, "the thing over there")
    assert response.status_code == 200
    data = response.json()
    assert data["suggested_mode"] is None
    assert data["confidence"] == "uncertain"
    assert data["detected_intent"] == "needs_clarification"


# --- E: empty / blank validation -------------------------------------------


def test_e_empty_text_is_rejected(no_llm):
    client = TestClient(app)
    response = _suggest(client, "")
    assert response.status_code == 422


def test_e_blank_text_is_rejected(no_llm):
    client = TestClient(app)
    response = _suggest(client, "   ")
    assert response.status_code == 422


def test_e_missing_field_is_rejected(no_llm):
    client = TestClient(app)
    response = client.post("/runs/intent-suggestion", json={})
    assert response.status_code == 422


# --- F: no run row is ever created -----------------------------------------


def test_f_no_run_row_created(no_llm):
    client = TestClient(app)
    before = _count_all_runs()
    for feature in [
        "Explain src/app.py. Do not change code.",
        "Plan how to add auth. Do not implement yet.",
        "Implement add(a,b) in src/app.py.",
        "Implement add(a,b) but do not change any code.",
    ]:
        assert _suggest(client, feature).status_code == 200
    assert _count_all_runs() == before


# --- pure mapping unit coverage --------------------------------------------


def test_build_suggestion_uncertain_decision_maps_to_needs_clarification():
    decision = IntentDecision(
        intent="plan_only", source="deterministic_only_ambiguous", uncertain=True
    )
    suggestion = build_intent_mode_suggestion(decision)
    assert suggestion.suggested_mode is None
    assert suggestion.confidence == "uncertain"
    assert suggestion.detected_intent == "needs_clarification"


def test_build_suggestion_deterministic_match_is_high():
    decision = IntentDecision(intent="implementation", source="deterministic_verb")
    suggestion = build_intent_mode_suggestion(decision)
    assert suggestion.suggested_mode == "implementation"
    assert suggestion.confidence == "high"
    assert suggestion.detected_intent == "implementation"


def test_build_suggestion_llm_confidence_buckets():
    high = build_intent_mode_suggestion(
        IntentDecision(intent="report_only", source="llm", from_llm=True, confidence=0.9)
    )
    medium = build_intent_mode_suggestion(
        IntentDecision(intent="report_only", source="llm", from_llm=True, confidence=0.6)
    )
    low = build_intent_mode_suggestion(
        IntentDecision(intent="report_only", source="llm", from_llm=True, confidence=0.2)
    )
    assert (high.confidence, medium.confidence, low.confidence) == (
        "high",
        "medium",
        "low",
    )


def test_build_suggestion_reason_never_leaks_internal_detail():
    # An internal reason code on the decision must never appear in the user-safe
    # suggestion reason (it is derived from the mode only).
    decision = IntentDecision(
        intent="plan_only", source="default", reason="llm_error", uncertain=True
    )
    suggestion = build_intent_mode_suggestion(decision)
    assert "llm_error" not in suggestion.reason


@pytest.mark.asyncio
async def test_suggest_intent_mode_is_deterministic(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("must not call LLM")

    monkeypatch.setattr("backend.pipeline.intent.complete_for_role", _boom)
    suggestion = await suggest_intent_mode("Implement add(a,b) in src/app.py.")
    assert suggestion.suggested_mode == "implementation"
