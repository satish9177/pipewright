"""
test_plan_handoff.py
Tests for POST /runs/{run_id}/start-implementation: the plan-to-implementation
handoff that takes a plan_ready source run and creates a new implementation run
seeded with the same plan, without mutating the source.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.git.local_git import StartBranchInspection
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM chunks WHERE run_id = :run_id"), {
                "run_id": run_id,
            })
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :run_id"), {
                "run_id": run_id,
            })


def _make_project(tmp_repo):
    return create_project(
        name=f"Handoff Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def _make_triage(run_id: str, project_id: str, feature: str = "Add login") -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description=feature,
        complexity="easy",
        total_chunks=1,
        reasoning="Single chunk plan.",
        chunks=[ChunkDefinition(
            chunk_number=1,
            title="Login chunk",
            description="Implement login.",
            files_expected=["backend/routes/login.py"],
            depends_on=[],
            risk_level="low",
            token_estimate=200,
            requires_human_review=False,
            rationale="Small focused change.",
        )],
    )


def _insert_plan_ready_run(
    run_id: str,
    project_id: str,
    feature: str = "Add login",
    chunk_plan: str | None = None,
) -> None:
    if chunk_plan is None:
        chunk_plan = _make_triage(run_id, project_id, feature).model_dump_json()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (
                id, project_id, feature_description, status, current_step,
                intent, chunk_plan_status, chunk_plan,
                total_chunks, current_chunk_number
            )
            VALUES
            (
                :id, :project_id, :feature, 'plan_ready', 'plan_ready',
                'plan_only', 'none', :chunk_plan, 1, 0
            )
        """), {
            "id": run_id,
            "project_id": project_id,
            "feature": feature,
            "chunk_plan": chunk_plan,
        })


def _insert_run_with(
    run_id: str,
    project_id: str | None,
    *,
    status: str,
    intent: str,
    chunk_plan: str | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (
                id, project_id, feature_description, status, current_step,
                intent, chunk_plan_status, chunk_plan,
                total_chunks, current_chunk_number
            )
            VALUES
            (
                :id, :project_id, 'Test run', :status, :status,
                :intent, 'none', :chunk_plan, 0, 0
            )
        """), {
            "id": run_id,
            "project_id": project_id,
            "status": status,
            "intent": intent,
            "chunk_plan": chunk_plan,
        })


def _load_run(run_id: str) -> dict:
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT * FROM pipeline_runs WHERE id = :id"
        ), {"id": run_id}).fetchone()
    return dict(row._mapping) if row else {}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_plan_ready_starts_new_implementation_run(tmp_repo, tracked_runs):
    project = _make_project(tmp_repo)
    plan_run_id = str(uuid.uuid4())
    tracked_runs.append(plan_run_id)
    _insert_plan_ready_run(plan_run_id, project["id"], "Add login")

    client = TestClient(app)
    response = client.post(f"/runs/{plan_run_id}/start-implementation")

    assert response.status_code == 200
    body = response.json()
    new_run_id = body["run_id"]
    tracked_runs.append(new_run_id)

    assert new_run_id != plan_run_id
    assert body["chunk_plan_status"] == "awaiting_approval"
    assert body["total_chunks"] == 1

    new_run = _load_run(new_run_id)
    assert new_run["intent"] == "implementation"
    assert new_run["status"] == "awaiting_chunk_plan_approval"
    assert new_run["source_plan_run_id"] == plan_run_id
    assert new_run["project_id"] == project["id"]
    assert new_run["feature_description"] == "Add login"
    assert new_run["chunk_plan"], "plan seed should be propagated to new run"


def test_plan_seed_is_present_in_new_implementation_run(tmp_repo, tracked_runs):
    project = _make_project(tmp_repo)
    plan_run_id = str(uuid.uuid4())
    tracked_runs.append(plan_run_id)
    feature = "Add password reset"
    triage = _make_triage(plan_run_id, project["id"], feature)
    _insert_plan_ready_run(
        plan_run_id, project["id"], feature, triage.model_dump_json()
    )

    client = TestClient(app)
    response = client.post(f"/runs/{plan_run_id}/start-implementation")
    assert response.status_code == 200
    new_run_id = response.json()["run_id"]
    tracked_runs.append(new_run_id)

    new_run = _load_run(new_run_id)
    seed = json.loads(new_run["chunk_plan"])
    assert seed["feature_description"] == feature
    assert [c["title"] for c in seed["chunks"]] == ["Login chunk"]
    # The seed is re-stamped with the new run_id so downstream stages
    # do not confuse it with the source plan.
    assert seed["run_id"] == new_run_id


def test_source_plan_run_remains_unchanged_after_handoff(tmp_repo, tracked_runs):
    project = _make_project(tmp_repo)
    plan_run_id = str(uuid.uuid4())
    tracked_runs.append(plan_run_id)
    _insert_plan_ready_run(plan_run_id, project["id"])
    before = _load_run(plan_run_id)

    client = TestClient(app)
    response = client.post(f"/runs/{plan_run_id}/start-implementation")
    assert response.status_code == 200
    tracked_runs.append(response.json()["run_id"])

    after = _load_run(plan_run_id)
    for field in (
        "status", "current_step", "intent", "chunk_plan_status",
        "chunk_plan", "feature_description", "project_id",
    ):
        assert before[field] == after[field], f"{field} mutated on source"


def test_handoff_is_idempotent(tmp_repo, tracked_runs):
    project = _make_project(tmp_repo)
    plan_run_id = str(uuid.uuid4())
    tracked_runs.append(plan_run_id)
    _insert_plan_ready_run(plan_run_id, project["id"])

    client = TestClient(app)
    first = client.post(f"/runs/{plan_run_id}/start-implementation")
    assert first.status_code == 200
    new_run_id = first["run_id"] if False else first.json()["run_id"]
    tracked_runs.append(new_run_id)

    second = client.post(f"/runs/{plan_run_id}/start-implementation")
    assert second.status_code == 200
    assert second.json()["run_id"] == new_run_id

    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs
            WHERE source_plan_run_id = :source
        """), {"source": plan_run_id}).fetchone()[0]
    assert count == 1


def test_plan_handoff_on_pipewright_branch_returns_no_run(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = _make_project(tmp_repo)
    plan_run_id = str(uuid.uuid4())
    tracked_runs.append(plan_run_id)
    _insert_plan_ready_run(plan_run_id, project["id"])
    monkeypatch.setattr(
        "backend.routes.chunks.inspect_start_branch",
        lambda _repo_path: StartBranchInspection(
            current_branch="pipewright/old-run",
            head_sha_short="abcdef123456",
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.ground_triage_result_paths",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("plan handoff must not re-ground after unsafe start")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.create_chunked_run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("plan handoff must not create an unsafe run")
        ),
    )

    client = TestClient(app)
    response = client.post(f"/runs/{plan_run_id}/start-implementation")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unsafe_start_branch"
    assert body["outcome"] == "unsafe_start_branch"
    assert body["run_created"] is False
    assert body["current_branch"] == "pipewright/old-run"
    assert body["current_head_sha_short"] == "abcdef123456"
    assert "run_id" not in body
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs
            WHERE source_plan_run_id = :source
        """), {"source": plan_run_id}).fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# Rejected source-run states
# ---------------------------------------------------------------------------

def test_unknown_run_returns_404(tmp_repo):
    client = TestClient(app)
    response = client.post(f"/runs/{uuid.uuid4()}/start-implementation")
    assert response.status_code == 404


def test_report_ready_run_is_rejected(tmp_repo, tracked_runs):
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    _insert_run_with(
        run_id, project["id"],
        status="report_ready", intent="report_only",
    )
    client = TestClient(app)
    response = client.post(f"/runs/{run_id}/start-implementation")
    assert response.status_code == 400
    assert "plan-only" in response.json()["detail"].lower()


def test_implementation_run_is_rejected(tmp_repo, tracked_runs):
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    _insert_run_with(
        run_id, project["id"],
        status="awaiting_chunk_plan_approval", intent="implementation",
    )
    client = TestClient(app)
    response = client.post(f"/runs/{run_id}/start-implementation")
    assert response.status_code == 400
    assert "plan-only" in response.json()["detail"].lower()


@pytest.mark.parametrize("bad_status", ["running", "failed", "complete"])
def test_non_plan_ready_status_is_rejected(tmp_repo, tracked_runs, bad_status):
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    _insert_run_with(
        run_id, project["id"],
        status=bad_status, intent="plan_only",
        chunk_plan=_make_triage(run_id, project["id"]).model_dump_json(),
    )
    client = TestClient(app)
    response = client.post(f"/runs/{run_id}/start-implementation")
    assert response.status_code == 400
    assert "plan_ready" in response.json()["detail"]


def test_missing_plan_output_is_rejected(tmp_repo, tracked_runs):
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    _insert_run_with(
        run_id, project["id"],
        status="plan_ready", intent="plan_only",
        chunk_plan=None,
    )
    client = TestClient(app)
    response = client.post(f"/runs/{run_id}/start-implementation")
    assert response.status_code == 400
    assert "plan output" in response.json()["detail"].lower()


def test_missing_project_id_is_rejected(tracked_runs):
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    _insert_run_with(
        run_id, None,
        status="plan_ready", intent="plan_only",
        chunk_plan=_make_triage(run_id, "unused").model_dump_json(),
    )
    client = TestClient(app)
    response = client.post(f"/runs/{run_id}/start-implementation")
    assert response.status_code == 400
    assert "project_id" in response.json()["detail"]


def test_unparseable_plan_output_is_rejected(tmp_repo, tracked_runs):
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    _insert_run_with(
        run_id, project["id"],
        status="plan_ready", intent="plan_only",
        chunk_plan="not valid json",
    )
    client = TestClient(app)
    response = client.post(f"/runs/{run_id}/start-implementation")
    assert response.status_code == 400
    assert "parseable" in response.json()["detail"].lower()
