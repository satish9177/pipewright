"""
test_memory_injection_analysis.py
Tests for M3C2: read-only, compute-on-read analysis over persisted memory
injection provenance.

Covers the pure analysis helper (empty input, duplicate/supersession candidates,
distinct-fact collapsing, traceability, advisory-only labels, input immutability,
purity of imports) and the read-only sibling endpoint
(GET /api/v1/runs/{run_id}/memory-injections/analysis): default list response
unchanged, project scoping, 404, empty, and content_hash parity.
"""

import ast
import copy
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.main import app
from backend.memory import injection_analysis
from backend.memory.injection_analysis import analyze_injection_events
from backend.memory.injection_store import record_memory_injection_event

pytestmark = pytest.mark.unit


# --- helpers ----------------------------------------------------------------

def _entry(fact_id, content, *, content_hash=None, category="other", scope="global"):
    return {
        "fact_id": fact_id,
        "content": content,
        "content_hash": content_hash or f"h-{fact_id or 'x'}",
        "category": category,
        "scope": scope,
        "priority": 100,
        "status_at_injection": "active",
    }


def _event(event_id, role, chunk_number, included):
    return {
        "id": event_id,
        "role": role,
        "chunk_number": chunk_number,
        "included_entries": included,
    }


def _insert_run(run_id: str, project_id: str) -> None:
    init_db()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs (id, project_id, feature_description, status)
            VALUES (:id, :project_id, :feature, 'running')
        """), {"id": run_id, "project_id": project_id, "feature": "test feature"})


def _delete_run(run_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM pipeline_runs WHERE id = :id"), {"id": run_id})
        conn.execute(
            text("DELETE FROM memory_injection_events WHERE run_id = :id"),
            {"id": run_id},
        )


# --- pure helper: empty / counts --------------------------------------------

def test_analysis_empty_for_no_events():
    analysis = analyze_injection_events([])
    assert analysis.total_events == 0
    assert analysis.total_included_entries == 0
    assert analysis.distinct_fact_count == 0
    assert analysis.duplicate_candidates == ()
    assert analysis.supersession_candidates == ()
    assert analysis.warnings == ()


def test_analysis_empty_when_events_have_no_included_entries():
    analysis = analyze_injection_events([_event("e1", "planner", 1, [])])
    assert analysis.total_events == 1
    assert analysis.total_included_entries == 0
    assert analysis.duplicate_candidates == ()


# --- duplicates -------------------------------------------------------------

def test_duplicate_candidates_from_pytest_like_facts():
    events = [_event("e1", "planner", 1, [
        _entry("f1", "Tests use pytest."),
        _entry("f2", "Run tests with pytest."),
    ])]
    analysis = analyze_injection_events(events)
    assert analysis.duplicate_candidate_count >= 1
    candidate = analysis.duplicate_candidates[0]
    assert candidate.candidate_type == "duplicate"
    assert candidate.advisory_only is True
    assert candidate.similarity > 0.0
    assert {candidate.left.fact_id, candidate.right.fact_id} == {"f1", "f2"}


def test_distinct_facts_are_not_flagged_as_duplicates():
    events = [_event("e1", "planner", 1, [
        _entry("f1", "Backend uses FastAPI."),
        _entry("f2", "Frontend uses React."),
    ])]
    analysis = analyze_injection_events(events)
    assert analysis.duplicate_candidates == ()


def test_same_fact_across_roles_is_not_a_self_duplicate():
    events = [
        _event("e1", "planner", 1, [_entry("f1", "Tests use pytest.")]),
        _event("e2", "coder", 1, [_entry("f1", "Tests use pytest.")]),
    ]
    analysis = analyze_injection_events(events)
    assert analysis.distinct_fact_count == 1
    assert analysis.total_included_entries == 2
    assert analysis.duplicate_candidates == ()


# --- supersession -----------------------------------------------------------

def test_supersession_candidates_same_dimension_different_value():
    events = [_event("e1", "planner", 1, [
        _entry("f1", "Tests use jest."),
        _entry("f2", "Tests use vitest."),
    ])]
    analysis = analyze_injection_events(events)
    assert analysis.supersession_candidate_count == 1
    candidate = analysis.supersession_candidates[0]
    assert candidate.candidate_type == "supersession"
    assert candidate.dimension == "test_runner"
    assert {candidate.left_value, candidate.right_value} == {"jest", "vitest"}
    assert candidate.relation == "possible_supersession"
    # Invariant: direction undecided, recency never implies truth.
    assert candidate.recency_implies_truth is False
    assert candidate.advisory_only is True


def test_no_supersession_for_unrelated_facts():
    events = [_event("e1", "planner", 1, [
        _entry("f1", "Backend uses FastAPI."),
        _entry("f2", "Frontend uses React."),
    ])]
    analysis = analyze_injection_events(events)
    assert analysis.supersession_candidates == ()


# --- traceability -----------------------------------------------------------

def test_candidate_references_include_event_role_chunk_fact():
    events = [_event("ev-9", "coder", 3, [
        _entry("f1", "Tests use pytest."),
        _entry("f2", "Run tests with pytest."),
    ])]
    analysis = analyze_injection_events(events)
    candidate = analysis.duplicate_candidates[0]
    for ref in (candidate.left, candidate.right):
        assert ref.event_id == "ev-9"
        assert ref.role == "coder"
        assert ref.chunk_number == 3
        assert ref.fact_id in {"f1", "f2"}
        assert "pytest" in ref.content


def test_candidates_are_advisory_only():
    events = [_event("e1", "planner", 1, [
        _entry("f1", "Tests use pytest."),
        _entry("f2", "Run tests with pytest."),
        _entry("f3", "Tests use jest."),
    ])]
    analysis = analyze_injection_events(events)
    for candidate in analysis.duplicate_candidates:
        assert candidate.advisory_only is True
        assert candidate.candidate_type == "duplicate"
    for candidate in analysis.supersession_candidates:
        assert candidate.advisory_only is True
        assert candidate.recency_implies_truth is False
        assert candidate.relation == "possible_supersession"


# --- purity / read-only -----------------------------------------------------

def test_analysis_does_not_mutate_input():
    events = [_event("e1", "planner", 1, [
        _entry("f1", "Tests use pytest."),
        _entry("f2", "Tests use jest."),
    ])]
    snapshot = copy.deepcopy(events)
    analyze_injection_events(events)
    assert events == snapshot


def test_module_has_no_db_llm_repo_or_git_imports():
    tree = ast.parse(Path(injection_analysis.__file__).read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    full_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
            full_modules.add(node.module)

    forbidden = {
        "sqlalchemy", "fastapi", "subprocess", "os", "requests", "httpx",
        "openai", "google", "git",
    }
    assert not (imported_roots & forbidden), (
        f"unexpected dependency in injection_analysis: "
        f"{sorted(imported_roots & forbidden)}"
    )
    # Must not reach the DB store/engine: analysis is pure compute-on-read.
    assert "backend.db.database" not in full_modules
    assert "backend.memory.injection_store" not in full_modules


# --- read-only endpoint -----------------------------------------------------

def test_analysis_endpoint_returns_candidates_and_strips_content_hash():
    client = TestClient(app)
    run_id = f"mia-ep-{uuid.uuid4().hex}"
    project_id = f"mia-epp-{uuid.uuid4().hex}"
    _insert_run(run_id, project_id)
    try:
        record_memory_injection_event(
            run_id=run_id, project_id=project_id, role="planner", chunk_number=1,
            token_budget=1200, category_policy=["test"],
            included_entries=[
                _entry("f1", "Tests use pytest."),
                _entry("f2", "Run tests with pytest."),
            ],
        )
        resp = client.get(f"/api/v1/runs/{run_id}/memory-injections/analysis")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == run_id
        analysis = body["analysis"]
        assert analysis["total_events"] == 1
        assert analysis["total_included_entries"] == 2
        assert analysis["distinct_fact_count"] == 2
        assert analysis["duplicate_candidate_count"] >= 1
        candidate = analysis["duplicate_candidates"][0]
        assert candidate["advisory_only"] is True
        assert candidate["candidate_type"] == "duplicate"
        assert "content_hash" not in candidate["left"]
        assert "content_hash" not in candidate["right"]
        assert candidate["left"]["fact_id"] in {"f1", "f2"}
    finally:
        _delete_run(run_id)


def test_analysis_endpoint_supersession_candidate():
    client = TestClient(app)
    run_id = f"mia-sup-{uuid.uuid4().hex}"
    project_id = f"mia-supp-{uuid.uuid4().hex}"
    _insert_run(run_id, project_id)
    try:
        record_memory_injection_event(
            run_id=run_id, project_id=project_id, role="coder", chunk_number=2,
            token_budget=1200, category_policy=["test"],
            included_entries=[
                _entry("f1", "Tests use jest."),
                _entry("f2", "Tests use vitest."),
            ],
        )
        resp = client.get(f"/api/v1/runs/{run_id}/memory-injections/analysis")
        analysis = resp.json()["analysis"]
        assert analysis["supersession_candidate_count"] == 1
        candidate = analysis["supersession_candidates"][0]
        assert candidate["dimension"] == "test_runner"
        assert candidate["recency_implies_truth"] is False
        assert candidate["relation"] == "possible_supersession"
        assert candidate["advisory_only"] is True
    finally:
        _delete_run(run_id)


def test_list_endpoint_default_response_is_unchanged():
    client = TestClient(app)
    run_id = f"mia-list-{uuid.uuid4().hex}"
    project_id = f"mia-listp-{uuid.uuid4().hex}"
    _insert_run(run_id, project_id)
    try:
        record_memory_injection_event(
            run_id=run_id, project_id=project_id, role="planner", chunk_number=1,
            token_budget=1200, category_policy=["test"],
            included_entries=[_entry("f1", "Tests use pytest.")],
        )
        body = client.get(f"/api/v1/runs/{run_id}/memory-injections").json()
        # M3C2 must not bloat the default provenance payload.
        assert set(body.keys()) == {"run_id", "events"}
        assert "analysis" not in body
    finally:
        _delete_run(run_id)


def test_analysis_endpoint_is_project_scoped():
    client = TestClient(app)
    run_id = f"mia-scope-{uuid.uuid4().hex}"
    project_a = f"mia-a-{uuid.uuid4().hex}"
    project_b = f"mia-b-{uuid.uuid4().hex}"
    _insert_run(run_id, project_a)
    try:
        # An event recorded under a different project (same run_id) must be
        # excluded by the run's project scope, so analysis sees nothing.
        record_memory_injection_event(
            run_id=run_id, project_id=project_b, role="planner", chunk_number=1,
            token_budget=1200, category_policy=["test"],
            included_entries=[
                _entry("f1", "Tests use pytest."),
                _entry("f2", "Run tests with pytest."),
            ],
        )
        analysis = client.get(
            f"/api/v1/runs/{run_id}/memory-injections/analysis"
        ).json()["analysis"]
        assert analysis["total_events"] == 0
        assert analysis["duplicate_candidates"] == []
    finally:
        _delete_run(run_id)
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM memory_injection_events WHERE project_id = :p"),
                {"p": project_b},
            )


def test_analysis_endpoint_404_for_unknown_run():
    client = TestClient(app)
    resp = client.get(
        f"/api/v1/runs/missing-{uuid.uuid4().hex}/memory-injections/analysis"
    )
    assert resp.status_code == 404


def test_analysis_endpoint_empty_for_run_without_provenance():
    client = TestClient(app)
    run_id = f"mia-empty-{uuid.uuid4().hex}"
    project_id = f"mia-emptyp-{uuid.uuid4().hex}"
    _insert_run(run_id, project_id)
    try:
        analysis = client.get(
            f"/api/v1/runs/{run_id}/memory-injections/analysis"
        ).json()["analysis"]
        assert analysis["total_events"] == 0
        assert analysis["total_included_entries"] == 0
        assert analysis["duplicate_candidates"] == []
        assert analysis["supersession_candidates"] == []
    finally:
        _delete_run(run_id)
