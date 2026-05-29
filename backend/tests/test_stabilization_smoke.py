"""
test_stabilization_smoke.py
PR #10 — Final stabilization smoke / regression suite.

A single, documented place that re-asserts the end-to-end safety and routing
guarantees established by the stabilization PRs (#1-#9B). Each test maps to one
line of docs/stabilization/final-smoke-status.md. Deeper, exhaustive coverage
of each behavior still lives in the dedicated module test files (referenced in
the docstrings); this file is the fast regression gate that proves the
guarantees still hold together.

Everything here is read-only or mocked: no real LLM calls, no real Git, no
network. The analyzer/triage LLM seams are monkeypatched.
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
from backend.models.handoff import CoderHandoff, FileChange
from backend.memory.memory_store import add_fact, load_hard_facts
from backend.pipeline import chunked_orchestrator
from backend.pipeline.chunk_store import create_chunked_run, get_chunk_plan_status
from backend.pipeline.plan_path_grounding import ground_triage_result_paths
from backend.pipeline.risk_scanner import chunk_is_high_risk, scan_triage_result
from backend.pipeline.scope_guard import ScopeDriftError, assert_files_in_scope
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM approval_gates WHERE run_id = :r"), {"r": run_id})
            conn.execute(text("DELETE FROM chunks WHERE run_id = :r"), {"r": run_id})
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :r"), {"r": run_id})


@pytest.fixture()
def memory_project_ids():
    ids: list[str] = []
    yield ids
    with engine.begin() as conn:
        for project_id in ids:
            conn.execute(text("DELETE FROM memory_facts WHERE project_id = :p"), {"p": project_id})


def make_project(tmp_repo):
    return create_project(
        name=f"Smoke Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def make_triage(
    run_id: str,
    project_id: str,
    *,
    files_expected: list[str] | None = None,
    feature_description: str = "do the work",
    risk_level: str = "low",
) -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description=feature_description,
        complexity="easy",
        total_chunks=1,
        reasoning="Single chunk is enough.",
        chunks=[ChunkDefinition(
            chunk_number=1,
            title="Smoke chunk",
            description="Adjust a neutral area.",
            files_expected=files_expected if files_expected is not None else ["README.md"],
            depends_on=[],
            risk_level=risk_level,
            token_estimate=100,
            requires_human_review=False,
            rationale="base rationale",
        )],
    )


def seed_file_index(project_id: str, paths: list[str]) -> None:
    with engine.begin() as conn:
        for path in paths:
            conn.execute(text("""
                INSERT INTO file_index
                (id, project_id, path, file_type, summary, key_imports,
                 last_modified, token_estimate, line_count, size_bytes)
                VALUES
                (:id, :pid, :path, 'unknown', NULL, '[]', NULL, 100, 10, 100)
            """), {"id": str(uuid.uuid4()), "pid": project_id, "path": path})


def fake_analyzer_llm(payload: str):
    async def _fake(role, request):
        return LLMResponse(
            text=payload, provider="fake", model="fake-model",
            input_tokens=10, output_tokens=5, finish_reason="stop",
        )
    return _fake


VALID_ANALYZER_JSON = json.dumps({
    "summary": "A small FastAPI service.",
    "findings": [{
        "title": "Example finding",
        "severity": "info",
        "confidence": "medium",
        "file": "backend/main.py",
        "evidence": "Module defines the app.",
        "recommendation": "Read backend/main.py to go deeper.",
    }],
    "limitations": ["Only a sample of files was reviewed."],
    "suggested_next_action": "Explore backend/main.py.",
})


def empty_coder(run_id: str) -> CoderHandoff:
    return CoderHandoff(
        run_id=run_id,
        feature_description="smoke",
        files_changed=[],
        summary="no changes",
    )


def coder_writing(run_id: str, path: str) -> CoderHandoff:
    return CoderHandoff(
        run_id=run_id,
        feature_description="smoke",
        files_changed=[FileChange(path=path, action="modify", content="x\n", reason="r")],
        summary="one change",
    )


# ---------------------------------------------------------------------------
# 1. Non-action / greeting guard returns needs_clarification and creates no run
#    (PR #9A; deeper: test_chunk_routes.py)
# ---------------------------------------------------------------------------

def test_greeting_returns_needs_clarification_and_creates_no_run(tmp_repo):
    project = make_project(tmp_repo)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "hello",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert "run_id" not in data
    with engine.connect() as conn:
        count = conn.execute(text(
            "SELECT COUNT(*) FROM pipeline_runs WHERE project_id = :p"
        ), {"p": project["id"]}).fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# 2. Greeting + real implementation request still routes to implementation
#    (the actionability guard must not eat a real request that carries a
#     greeting prefix). Gap filled by PR #10.
# ---------------------------------------------------------------------------

def test_greeting_plus_implementation_still_reaches_chunk_plan(
    monkeypatch, tmp_repo, tracked_runs
):
    project = make_project(tmp_repo)
    calls = []

    async def fake_triage(run_id, project_id, feature_description):
        calls.append(run_id)
        return make_triage(run_id, project_id, feature_description=feature_description)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "hello, add a health check endpoint",
    })

    assert response.status_code == 200
    data = response.json()
    assert data.get("status") != "needs_clarification"
    tracked_runs.append(data["run_id"])
    assert len(calls) == 1
    assert data["chunk_plan_status"] == "awaiting_approval"
    with engine.connect() as conn:
        intent = conn.execute(text(
            "SELECT intent FROM pipeline_runs WHERE id = :r"
        ), {"r": data["run_id"]}).fetchone()[0]
    assert intent == "implementation"


# ---------------------------------------------------------------------------
# 3. report_only requests execute the read-only analyzer only
#    (PR #8A/#8B/#8C; deeper: test_report_analyzer.py, test_chunk_routes.py)
# ---------------------------------------------------------------------------

def test_report_only_runs_read_only_analyzer_and_nothing_else(
    monkeypatch, tmp_repo, tracked_runs
):
    project = make_project(tmp_repo)
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("triage called")),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.create_chunked_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("create_chunked_run called")),
    )
    monkeypatch.setattr(
        "backend.pipeline.report_analyzer.complete_for_role",
        fake_analyzer_llm(VALID_ANALYZER_JSON),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "explain this project",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "none"
    assert data["chunks"] == []
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, intent, plain_english_summary, report_json
            FROM pipeline_runs WHERE id = :r
        """), {"r": data["run_id"]}).fetchone()
        chunk_count = conn.execute(text(
            "SELECT COUNT(*) FROM chunks WHERE run_id = :r"
        ), {"r": data["run_id"]}).fetchone()[0]
        gate_count = conn.execute(text(
            "SELECT COUNT(*) FROM approval_gates WHERE run_id = :r"
        ), {"r": data["run_id"]}).fetchone()[0]
    assert row[0] == "report_ready"
    assert row[1] == "report_only"
    assert row[2]  # plain_english_summary populated
    assert json.loads(row[3])["report_kind"] == "project_explanation"
    assert chunk_count == 0
    assert gate_count == 0


# ---------------------------------------------------------------------------
# 4. plan_only requests create plan_ready and do not execute code
#    (PR #5; deeper: test_chunk_routes.py)
# ---------------------------------------------------------------------------

def test_plan_only_creates_plan_ready_without_executable_chunks(
    monkeypatch, tmp_repo, tracked_runs
):
    project = make_project(tmp_repo)

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage(run_id, project_id, feature_description=feature_description)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "give me a plan to add login",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "none"
    assert data["chunks"] == []
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT status, intent FROM pipeline_runs WHERE id = :r"
        ), {"r": data["run_id"]}).fetchone()
        chunk_count = conn.execute(text(
            "SELECT COUNT(*) FROM chunks WHERE run_id = :r"
        ), {"r": data["run_id"]}).fetchone()[0]
    assert row[0] == "plan_ready"
    assert row[1] == "plan_only"
    assert chunk_count == 0


def test_plan_only_run_rejects_mutating_execute_route(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (id, project_id, feature_description, status, current_step,
             intent, chunk_plan_status, total_chunks, current_chunk_number)
            VALUES (:r, :p, 'plan run', 'plan_ready', 'plan_ready',
                    'plan_only', 'none', 0, 0)
        """), {"r": run_id, "p": project["id"]})
    client = TestClient(app)

    response = client.post(f"/runs/{run_id}/chunks/execute")

    assert response.status_code == 400
    assert "read-only" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 5. plan-to-implementation creates a separate implementation run with
#    source_plan_run_id; the source plan run is unchanged.
#    (PR #7; deeper: test_plan_handoff.py)
# ---------------------------------------------------------------------------

def test_plan_to_implementation_creates_separate_linked_run(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    plan_run_id = str(uuid.uuid4())
    tracked_runs.append(plan_run_id)
    chunk_plan = make_triage(
        plan_run_id, project["id"], files_expected=["README.md"]
    ).model_dump_json()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (id, project_id, feature_description, status, current_step,
             intent, chunk_plan_status, chunk_plan, total_chunks, current_chunk_number)
            VALUES (:r, :p, 'Add login', 'plan_ready', 'plan_ready',
                    'plan_only', 'none', :cp, 1, 0)
        """), {"r": plan_run_id, "p": project["id"], "cp": chunk_plan})
    client = TestClient(app)

    response = client.post(f"/runs/{plan_run_id}/start-implementation")

    assert response.status_code == 200
    new_run_id = response.json()["run_id"]
    tracked_runs.append(new_run_id)
    assert new_run_id != plan_run_id
    with engine.connect() as conn:
        new_run = conn.execute(text(
            "SELECT intent, source_plan_run_id FROM pipeline_runs WHERE id = :r"
        ), {"r": new_run_id}).fetchone()
        source = conn.execute(text(
            "SELECT status, intent FROM pipeline_runs WHERE id = :r"
        ), {"r": plan_run_id}).fetchone()
    assert new_run[0] == "implementation"
    assert new_run[1] == plan_run_id
    # Source plan run remains a read-only plan_ready run.
    assert source[0] == "plan_ready"
    assert source[1] == "plan_only"


# ---------------------------------------------------------------------------
# 6. Vague implementation requests return needs_clarification
#    (PR #9A; deeper: test_chunk_routes.py, test_implementation_guard.py)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("feature", [
    "implement a big feature",
    "make the app better",
    "clean up the code",
])
def test_vague_implementation_returns_needs_clarification(
    monkeypatch, tmp_repo, feature
):
    project = make_project(tmp_repo)
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("triage called")),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.create_chunked_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("create called")),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": feature,
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["intent"] == "implementation"
    with engine.connect() as conn:
        count = conn.execute(text(
            "SELECT COUNT(*) FROM pipeline_runs WHERE project_id = :p"
        ), {"p": project["id"]}).fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# 7. Specific implementation requests proceed to chunk approval
#    (PR #9A; deeper: test_chunk_routes.py)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("feature", [
    "add health check endpoint",
    "update retry limit from 3 to 5",
    "fix typo in README",
])
def test_specific_implementation_reaches_chunk_approval(
    monkeypatch, tmp_repo, tracked_runs, feature
):
    project = make_project(tmp_repo)
    calls = []

    async def fake_triage(run_id, project_id, feature_description):
        calls.append(run_id)
        return make_triage(run_id, project_id, feature_description=feature_description)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": feature,
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert len(calls) == 1
    assert data["chunk_plan_status"] == "awaiting_approval"


# ---------------------------------------------------------------------------
# 8. Empty / no-change coder output never reaches patch / test / commit / push
#    (PR #1; deeper: test_chunked_orchestrator.py)
# ---------------------------------------------------------------------------

def test_empty_coder_output_never_commits(monkeypatch, tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id, project["id"], "smoke",
        make_triage(run_id, project["id"], files_expected=["README.md"]),
    )
    chunk = get_chunk_plan_status(run_id).triage.chunks[0]

    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "commit_files",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("commit_files called")),
    )

    with pytest.raises(RuntimeError, match=chunked_orchestrator.NO_CHANGES_MESSAGE):
        chunked_orchestrator._commit_and_complete_chunk(
            run_id, chunk, empty_coder(run_id), str(tmp_repo),
        )

    with engine.connect() as conn:
        chunk_row = conn.execute(text(
            "SELECT status, error_message FROM chunks WHERE run_id = :r AND chunk_number = 1"
        ), {"r": run_id}).fetchone()
    assert chunk_row[0] == "failed"
    assert chunk_row[1] == chunked_orchestrator.NO_CHANGES_MESSAGE


# ---------------------------------------------------------------------------
# 9. Scope drift fails before patch / test / commit
#    (PR #4; deeper: test_scope_guard.py, test_chunked_orchestrator.py)
# ---------------------------------------------------------------------------

def test_out_of_scope_coder_path_raises_scope_drift():
    run_id = str(uuid.uuid4())
    with pytest.raises(ScopeDriftError, match="outside approved scope"):
        assert_files_in_scope(
            coder_writing(run_id, "backend/secret_leak.py"),
            files_expected=["backend/routes/login.py"],
        )


def test_empty_files_expected_is_blocked_by_scope_guard():
    run_id = str(uuid.uuid4())
    with pytest.raises(ScopeDriftError, match="empty files_expected"):
        assert_files_in_scope(
            coder_writing(run_id, "backend/routes/login.py"),
            files_expected=[],
        )


# ---------------------------------------------------------------------------
# 10. Fake / unindexed files_expected paths are removed by plan_path_grounding
#     (PR #9B; deeper: test_plan_path_grounding.py)
# ---------------------------------------------------------------------------

def test_grounding_removes_fake_paths_and_hardens_chunk(tmp_repo, memory_project_ids):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/routes/users.py"])
    run_id = str(uuid.uuid4())
    triage = make_triage(
        run_id, project["id"],
        files_expected=[
            "src/models/user.py",          # fake -> removed
            "frontend/x/LoginPage.js",     # fake -> removed
            "backend/routes/users.py",     # real -> kept
        ],
    )

    grounded = ground_triage_result_paths(project["id"], triage)

    chunk = grounded.chunks[0]
    assert chunk.files_expected == ["backend/routes/users.py"]
    assert chunk.risk_level == "high"
    assert chunk.requires_human_review is True


def test_grounding_leaves_empty_when_all_paths_fake(tmp_repo):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/routes/users.py"])
    run_id = str(uuid.uuid4())
    triage = make_triage(
        run_id, project["id"],
        files_expected=["src/made_up.py", "frontend/imaginary.tsx"],
    )

    grounded = ground_triage_result_paths(project["id"], triage)

    chunk = grounded.chunks[0]
    # Never backfilled with an invented path; hardened for human review.
    assert chunk.files_expected == []
    assert chunk.risk_level == "high"
    assert chunk.requires_human_review is True


# ---------------------------------------------------------------------------
# 11. Empty files_expected forces high risk and human review
#     (PR #4; deeper: test_risk_scanner.py)
# ---------------------------------------------------------------------------

def test_empty_files_expected_forces_high_risk_and_review():
    run_id = str(uuid.uuid4())
    triage = make_triage(run_id, "proj", files_expected=[], risk_level="low")

    scanned = scan_triage_result(triage)

    chunk = scanned.chunks[0]
    assert chunk.risk_level == "high"
    assert chunk.requires_human_review is True
    assert chunk_is_high_risk(triage.chunks[0]) is True


# ---------------------------------------------------------------------------
# 12. Deterministic high-risk triggers for migration / auth / secrets / git /
#     checkpoint / routes / CI / dependency changes (PR #4; deeper:
#     test_risk_scanner.py)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category,title,description,files", [
    ("migration", "DB migration", "add an alembic migration", ["backend/db/x.py"]),
    ("auth", "Auth change", "modify the jwt login flow", ["backend/services/x.py"]),
    ("secrets", "Secret rotation", "rotate the api_key in .env", ["backend/config/x.py"]),
    ("git", "Git ops", "add commit, push and rollback handling", ["backend/util/x.py"]),
    ("checkpoint", "Resume logic", "resume from a checkpoint", ["backend/util/x.py"]),
    ("routes", "Route tweak", "adjust a handler", ["backend/routes/users.py"]),
    ("ci", "CI tweak", "adjust pipeline", [".github/workflows/ci.yml"]),
    ("dependency", "Dep bump", "bump dependencies", ["requirements.txt"]),
])
def test_deterministic_high_risk_categories(category, title, description, files):
    chunk = ChunkDefinition(
        chunk_number=1, title=title, description=description,
        files_expected=files, depends_on=[], risk_level="low",
        token_estimate=100, requires_human_review=False, rationale="r",
    )
    assert chunk_is_high_risk(chunk) is True, f"{category} should be high risk"


def test_benign_chunk_is_not_high_risk():
    chunk = ChunkDefinition(
        chunk_number=1, title="Tweak helper text",
        description="adjust a wording string in a helper",
        files_expected=["backend/utils/text_helper.py"], depends_on=[],
        risk_level="low", token_estimate=50, requires_human_review=False, rationale="r",
    )
    assert chunk_is_high_risk(chunk) is False


# ---------------------------------------------------------------------------
# 13. Memory facts are project-scoped; blank/None project_id returns no memory
#     (PR #3; deeper: test_memory.py)
# ---------------------------------------------------------------------------

def test_memory_is_project_scoped(memory_project_ids):
    project_a = f"smoke-a-{uuid.uuid4().hex}"
    project_b = f"smoke-b-{uuid.uuid4().hex}"
    memory_project_ids.extend([project_a, project_b])
    add_fact(project_a, "Backend uses FastAPI routes", category="stack")
    add_fact(project_b, "Frontend uses React pages", category="stack")

    facts_a = load_hard_facts(project_a)
    facts_b = load_hard_facts(project_b)

    assert "FastAPI" in facts_a and "React" not in facts_a
    assert "React" in facts_b and "FastAPI" not in facts_b


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_project_id_returns_no_memory(memory_project_ids, blank):
    project_id = f"smoke-c-{uuid.uuid4().hex}"
    memory_project_ids.append(project_id)
    add_fact(project_id, "Backend uses FastAPI routes", category="stack")

    assert load_hard_facts(blank) == ""
