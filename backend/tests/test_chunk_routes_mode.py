"""
test_chunk_routes_mode.py
#42C: explicit requested_mode + confirm_conflict + ModeConflictResponse.

auto/omitted keeps the classifier as the router (backward compatible); a
concrete requested_mode is the source of truth for routing; selecting
implementation over a read-only/no-code signal returns a mode_conflict envelope
(no run) unless confirm_conflict=true. No API calls. Triage/analyzer mocked.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.llm.base import LLMResponse
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(
                text("DELETE FROM approval_gates WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            conn.execute(
                text("DELETE FROM chunks WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :run_id"),
                {"run_id": run_id},
            )


def make_project(tmp_repo):
    return create_project(
        name=f"Mode Route Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def make_triage(run_id: str, project_id: str) -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description="Add route chunks",
        complexity="easy",
        total_chunks=1,
        reasoning="One chunk is enough.",
        chunks=[ChunkDefinition(
            chunk_number=1,
            title="Route chunk",
            description="Plan route chunk.",
            files_expected=["backend/routes/chunks.py"],
            depends_on=[],
            risk_level="low",
            token_estimate=100,
            requires_human_review=False,
            rationale="Small route change.",
        )],
    )


VALID_ANALYZER_JSON = json.dumps({
    "summary": "The repository is a small FastAPI service with chunked runs.",
    "findings": [
        {
            "title": "Broad exception handling in routes",
            "severity": "medium",
            "confidence": "medium",
            "file": "backend/routes/chunks.py",
            "evidence": "Several handlers catch bare Exception.",
            "recommendation": "Narrow exception types where practical.",
        }
    ],
    "limitations": ["Only a bounded sample of files was reviewed."],
    "suggested_next_action": "Request a plan for the highest-severity finding.",
})


def _fake_analyzer_llm_response(text_payload: str):
    async def fake_complete_for_role(role, request):
        return LLMResponse(
            text=text_payload,
            provider="fake",
            model=request.model or "fake-model",
            input_tokens=10,
            output_tokens=5,
            finish_reason="stop",
        )

    return fake_complete_for_role


def _throw(message: str):
    def _raise(*args, **kwargs):
        raise AssertionError(message)

    return _raise


def _run_row(run_id: str):
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT status, current_step, intent, total_chunks
            FROM pipeline_runs WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()


def _count_runs_for_project(project_id: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs WHERE project_id = :pid
        """), {"pid": project_id}).fetchone()[0]


def _post(client, project_id, feature, **extra):
    body = {"project_id": project_id, "feature_description": feature}
    body.update(extra)
    return client.post("/runs/chunked", json=body)


def test_42c_a_omitted_requested_mode_routes_via_classifier(
    monkeypatch, tmp_repo, tracked_runs,
):
    # A: no requested_mode → classifier routes implementation text to a plan.
    project = make_project(tmp_repo)

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = _post(client, project["id"], "Add route chunks")

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "awaiting_approval"


def test_42c_b_auto_requested_mode_routes_via_classifier(
    monkeypatch, tmp_repo, tracked_runs,
):
    # B: explicit requested_mode="auto" behaves like the classifier router.
    project = make_project(tmp_repo)

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = _post(client, project["id"], "Add route chunks", requested_mode="auto")

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "awaiting_approval"


def test_42c_c_report_only_selected_over_impl_text_creates_report_ready(
    monkeypatch, tmp_repo, tracked_runs,
):
    # C: selected report_only is safer than implementation-like text; honored,
    # creates a REPORT_READY run with no chunk plan and no triage.
    project = make_project(tmp_repo)
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        _throw("triage must not run for selected report_only"),
    )
    monkeypatch.setattr(
        "backend.pipeline.report_analyzer.complete_for_role",
        _fake_analyzer_llm_response(VALID_ANALYZER_JSON),
    )
    client = TestClient(app)

    response = _post(
        client,
        project["id"],
        "Add a login endpoint to the API.",
        requested_mode="report_only",
    )

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "none"
    assert data["total_chunks"] == 0
    row = _run_row(data["run_id"])
    assert row[0] == "report_ready"
    assert row[2] == "report_only"


def test_42c_d_plan_only_selected_over_impl_text_creates_plan_ready(
    monkeypatch, tmp_repo, tracked_runs,
):
    # D: selected plan_only is honored over implementation-like text → PLAN_READY,
    # and create_chunked_run (the execution path) is never reached.
    project = make_project(tmp_repo)

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    monkeypatch.setattr(
        "backend.routes.chunks.create_chunked_run",
        _throw("create_chunked_run must not run for selected plan_only"),
    )
    client = TestClient(app)

    response = _post(
        client,
        project["id"],
        "Add a login endpoint to the API.",
        requested_mode="plan_only",
    )

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "none"
    row = _run_row(data["run_id"])
    assert row[0] == "plan_ready"
    assert row[2] == "plan_only"


def test_42c_e_implementation_selected_clear_text_awaiting_approval(
    monkeypatch, tmp_repo, tracked_runs,
):
    # E: selected implementation + clear text → chunk plan awaiting approval.
    # No execution: the plan only awaits the chunk-plan approval gate.
    project = make_project(tmp_repo)

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = _post(
        client, project["id"], "Add route chunks", requested_mode="implementation",
    )

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "awaiting_approval"
    row = _run_row(data["run_id"])
    assert row[2] == "implementation"


def test_42c_f_implementation_conflict_returns_mode_conflict_no_run(
    monkeypatch, tmp_repo,
):
    # F: implementation selected + "implement but change nothing" contradiction
    # + confirm_conflict=false → ModeConflictResponse and NO run created.
    project = make_project(tmp_repo)
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        _throw("triage must not run for a blocked conflict"),
    )
    client = TestClient(app)

    response = _post(
        client,
        project["id"],
        "Implement add(a, b) in src/app.py but do not change any code.",
        requested_mode="implementation",
        confirm_conflict=False,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "mode_conflict"
    assert data["type"] == "mode_conflict"
    assert data["run_created"] is False
    assert data["requested_mode"] == "implementation"
    assert data["detected_intent"] == "needs_clarification"
    assert data["message"]
    assert isinstance(data["options"], list) and data["options"]
    assert "run_id" not in data
    assert _count_runs_for_project(project["id"]) == 0


def test_42c_g_implementation_conflict_confirmed_creates_plan(
    monkeypatch, tmp_repo, tracked_runs,
):
    # G: same as F but confirm_conflict=true → chunk plan awaiting approval.
    project = make_project(tmp_repo)

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = _post(
        client,
        project["id"],
        "Implement add(a, b) in src/app.py but do not change any code.",
        requested_mode="implementation",
        confirm_conflict=True,
    )

    assert response.status_code == 200
    data = response.json()
    assert data.get("status") != "mode_conflict"
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "awaiting_approval"
    row = _run_row(data["run_id"])
    assert row[2] == "implementation"


def test_42c_h_report_only_selected_no_conflict_block(
    monkeypatch, tmp_repo, tracked_runs,
):
    # H: report_only selected over contradictory text is honored, never blocked.
    project = make_project(tmp_repo)
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        _throw("triage must not run for selected report_only"),
    )
    monkeypatch.setattr(
        "backend.pipeline.report_analyzer.complete_for_role",
        _fake_analyzer_llm_response(VALID_ANALYZER_JSON),
    )
    client = TestClient(app)

    response = _post(
        client,
        project["id"],
        "Implement add() but do not change code.",
        requested_mode="report_only",
    )

    assert response.status_code == 200
    data = response.json()
    assert data.get("status") != "mode_conflict"
    tracked_runs.append(data["run_id"])
    row = _run_row(data["run_id"])
    assert row[0] == "report_ready"
    assert row[2] == "report_only"


def test_42c_i_invalid_requested_mode_returns_validation_error(tmp_repo):
    # I: an unknown requested_mode is a 422 validation error (no run).
    project = make_project(tmp_repo)
    client = TestClient(app)

    response = _post(
        client, project["id"], "Add route chunks", requested_mode="banana",
    )

    assert response.status_code == 422
    assert _count_runs_for_project(project["id"]) == 0
