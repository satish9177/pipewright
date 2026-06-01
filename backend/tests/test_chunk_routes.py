"""
test_chunk_routes.py
Tests for Phase 2B chunk planning routes.
No API calls. Triage is mocked.
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
from backend.pipeline.approval_gate import create_final_approval_gate
from backend.pipeline.chunk_store import create_chunked_run
from backend.pipeline.run_locks import (
    PROJECT_LOCK_CONFLICT_MESSAGE,
    ProjectRepoLockError,
)
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM approval_gates WHERE run_id = :run_id"), {
                "run_id": run_id,
            })
            conn.execute(text("DELETE FROM chunks WHERE run_id = :run_id"), {
                "run_id": run_id,
            })
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :run_id"), {
                "run_id": run_id,
            })


def make_project(tmp_repo):
    return create_project(
        name=f"Route Chunk Project {uuid.uuid4()}",
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


def seed_file_index(project_id: str, paths: list[str]) -> None:
    """Seed the repo index for a project (PR #9B grounding tests)."""
    with engine.begin() as conn:
        for path in paths:
            conn.execute(text("""
                INSERT INTO file_index
                (id, project_id, path, file_type, summary, key_imports,
                 last_modified, token_estimate, line_count, size_bytes)
                VALUES
                (:id, :project_id, :path, 'unknown', NULL, '[]',
                 NULL, 100, 10, 100)
            """), {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "path": path,
            })


def make_triage_with_files(
    run_id: str,
    project_id: str,
    files_expected: list[str],
    *,
    title: str = "Neutral chunk",
    description: str = "Adjust the layout area.",
    feature_description: str = "do the work",
) -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description=feature_description,
        complexity="easy",
        total_chunks=1,
        reasoning="One chunk is enough.",
        chunks=[ChunkDefinition(
            chunk_number=1,
            title=title,
            description=description,
            files_expected=files_expected,
            depends_on=[],
            risk_level="low",
            token_estimate=100,
            requires_human_review=False,
            rationale="base rationale",
        )],
    )


def test_post_runs_chunked_returns_awaiting_approval_plan(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "Add route chunks",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "awaiting_approval"
    assert data["total_chunks"] == 1
    assert len(data["chunks"]) == 1


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


def test_report_only_chunked_run_creates_read_only_run_without_triage(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("triage should not be called")
        ),
    )
    monkeypatch.setattr(
        "backend.pipeline.report_analyzer.complete_for_role",
        _fake_analyzer_llm_response(VALID_ANALYZER_JSON),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "find bugs in the codebase",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "none"
    assert data["total_chunks"] == 0
    assert data["triage"] is None
    assert data["chunks"] == []
    with engine.connect() as conn:
        run = conn.execute(text("""
            SELECT status, current_step, intent, plain_english_summary,
                   report_json
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": data["run_id"]}).fetchone()
        chunk_count = conn.execute(text("""
            SELECT COUNT(*) FROM chunks WHERE run_id = :run_id
        """), {"run_id": data["run_id"]}).fetchone()[0]
        gate_count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates WHERE run_id = :run_id
        """), {"run_id": data["run_id"]}).fetchone()[0]
    assert run[0] == "report_ready"
    assert run[1] == "report_ready"
    assert run[2] == "report_only"
    summary = run[3]
    # Useful, non-canned report content from the analyzer.
    assert "Broad exception handling in routes" in summary
    assert "## Findings" in summary
    # Read-only / no-mutation messaging is always present.
    assert (
        "No code was changed, no tests were run, no commits or PRs were created"
        in summary
    )
    # Structured report_json persisted alongside the markdown summary.
    report_json = run[4]
    assert report_json
    structured = json.loads(report_json)
    assert structured["summary"]
    assert isinstance(structured["findings"], list) and structured["findings"]
    finding = structured["findings"][0]
    assert finding["title"] == "Broad exception handling in routes"
    assert finding["severity"] == "medium"
    assert finding["confidence"] == "medium"
    assert finding["file_path"] == "backend/routes/chunks.py"
    assert finding["evidence"]
    assert finding["suggested_next_action"]
    assert isinstance(structured["files_reviewed"], list)
    assert isinstance(structured["limitations"], list)
    assert structured["next_action"]
    assert chunk_count == 0
    assert gate_count == 0


def test_report_only_malformed_analyzer_fails_safely_no_implementation(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("triage should not be called")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.create_chunked_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("implementation path should not be reached")
        ),
    )
    # Analyzer LLM returns unparseable garbage on every attempt.
    monkeypatch.setattr(
        "backend.pipeline.report_analyzer.complete_for_role",
        _fake_analyzer_llm_response("this is not json at all"),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "review this repo",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "none"
    assert data["chunks"] == []
    with engine.connect() as conn:
        run = conn.execute(text("""
            SELECT status, intent, plain_english_summary, report_json
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": data["run_id"]}).fetchone()
    assert run[0] == "report_ready"
    assert run[1] == "report_only"
    # A safe, limited read-only report is stored; still carries the no-mutation note.
    assert "limited" in run[2].lower()
    assert (
        "No code was changed, no tests were run, no commits or PRs were created"
        in run[2]
    )
    # Malformed analyzer output: no structured report_json is persisted, so the
    # run falls back to plain_english_summary and never reaches implementation.
    assert run[3] is None


def test_report_only_run_fetch_exposes_report_json(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    monkeypatch.setattr(
        "backend.pipeline.report_analyzer.complete_for_role",
        _fake_analyzer_llm_response(VALID_ANALYZER_JSON),
    )
    client = TestClient(app)

    create = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "review this repo",
    })
    assert create.status_code == 200
    run_id = create.json()["run_id"]
    tracked_runs.append(run_id)

    # The run-status endpoint surfaces report_json for ReportView to render.
    fetched = client.get(f"/runs/{run_id}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["plain_english_summary"]
    assert body["report_json"]
    structured = json.loads(body["report_json"])
    assert structured["findings"]


def test_old_report_without_report_json_remains_compatible(
    tmp_repo,
    tracked_runs,
):
    """An existing report_ready row predating PR #8B has report_json = NULL and
    must still be fetchable, rendering via plain_english_summary."""
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (
                id, project_id, feature_description, plain_english_summary,
                status, current_step, intent, chunk_plan_status,
                total_chunks, current_chunk_number
            )
            VALUES
            (
                :run_id, :project_id, 'old report', 'legacy markdown report',
                'report_ready', 'report_ready', 'report_only', 'none', 0, 0
            )
        """), {"run_id": run_id, "project_id": project["id"]})
    client = TestClient(app)

    fetched = client.get(f"/runs/{run_id}")

    assert fetched.status_code == 200
    body = fetched.json()
    assert body["status"] == "report_ready"
    assert body["plain_english_summary"] == "legacy markdown report"
    assert body.get("report_json") is None


VAGUE_IMPLEMENTATION_REQUESTS = [
    "implement a small safe change",
    "implement a big feature",
    "implement a medium feature",
    "implement a feature",
    "implement one extraordinary feature",
    "implement one extra ordinary feature",
    "implement one extra-ordinary feature",
    "implement an amazing feature",
    "create a cool feature",
    "build something useful",
    "add a nice improvement",
    "add a feature",
    "make the app better",
    "fix something",
    "change the code",
    "clean up the code",
    "update something",
    "do some cleanup",
]


@pytest.mark.parametrize("feature", VAGUE_IMPLEMENTATION_REQUESTS)
def test_vague_implementation_returns_needs_clarification_without_run(
    monkeypatch,
    tmp_repo,
    feature,
):
    project = make_project(tmp_repo)

    # None of the implementation/plan/report paths may be touched.
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("run_triage must not be called for vague requests")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.create_chunked_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("create_chunked_run must not be called")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.run_report_analysis",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("report analyzer must not be called")
        ),
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
    assert data["message"]
    assert isinstance(data["missing_details"], list) and data["missing_details"]
    assert isinstance(data["examples"], list) and data["examples"]
    # No run row, chunks, or gates may be created.
    assert "run_id" not in data
    with engine.connect() as conn:
        run_count = conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs
            WHERE project_id = :project_id
        """), {"project_id": project["id"]}).fetchone()[0]
    assert run_count == 0


def test_valid_short_implementation_still_proceeds_to_chunk_plan(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    calls = []

    async def fake_triage(run_id, project_id, feature_description):
        calls.append((run_id, project_id, feature_description))
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "fix typo in README",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert len(calls) == 1
    assert data["chunk_plan_status"] == "awaiting_approval"
    assert data["total_chunks"] == 1


NON_ACTIONABLE_INPUTS = [
    "hello", "hello bro", "hi", "hey", "yo", "test", "ok", "thanks", "thank you",
]


@pytest.mark.parametrize("feature", NON_ACTIONABLE_INPUTS)
def test_non_actionable_request_returns_clarification_without_run(
    monkeypatch,
    tmp_repo,
    feature,
):
    project = make_project(tmp_repo)

    # The pre-intent guard must short-circuit BEFORE classification or any
    # report/plan/implementation path.
    monkeypatch.setattr(
        "backend.routes.chunks.classify_intent_details_async",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("intent classifier must not be called")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.run_report_analysis",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("report analyzer must not be called")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("triage must not be called")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.create_chunked_run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("create_chunked_run must not be called")
        ),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": feature,
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["intent"] == "unknown"
    assert "run_id" not in data
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs WHERE project_id = :pid
        """), {"pid": project["id"]}).fetchone()[0]
    assert count == 0


def test_split_adjective_implementation_blocks_before_triage(
    monkeypatch,
    tmp_repo,
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
        "feature_description": "implement one extra ordinary feature",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["intent"] == "implementation"
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs WHERE project_id = :pid
        """), {"pid": project["id"]}).fetchone()[0]
    assert count == 0


def test_plan_request_to_add_login_still_creates_plan_ready(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "give me a plan to add login",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data.get("status") != "needs_clarification"
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, intent FROM pipeline_runs WHERE id = :rid
        """), {"rid": data["run_id"]}).fetchone()
    assert row[0] == "plan_ready"
    assert row[1] == "plan_only"


def test_explain_project_still_creates_report_ready(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    monkeypatch.setattr(
        "backend.pipeline.report_analyzer.complete_for_role",
        _fake_analyzer_llm_response(VALID_ANALYZER_JSON),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "explain this project",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data.get("status") != "needs_clarification"
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, intent FROM pipeline_runs WHERE id = :rid
        """), {"rid": data["run_id"]}).fetchone()
    assert row[0] == "report_ready"
    assert row[1] == "report_only"


DISCOVERY_QUESTIONS = [
    "what features can we add to this project",
    "what feature kind can we add to this project",
    "suggest features for this project",
    "suggest improvements for this project",
    "what can we improve in this project",
    "how can we improve this app",
    "what should we build next",
]


@pytest.mark.parametrize("feature", DISCOVERY_QUESTIONS)
def test_discovery_questions_route_to_report_only(
    monkeypatch,
    tmp_repo,
    tracked_runs,
    feature,
):
    project = make_project(tmp_repo)
    # Discovery is read-only: it must reach the report analyzer, never triage
    # or implementation.
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("triage must not be called for discovery")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.create_chunked_run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("create_chunked_run must not be called for discovery")
        ),
    )
    monkeypatch.setattr(
        "backend.pipeline.report_analyzer.complete_for_role",
        _fake_analyzer_llm_response(VALID_ANALYZER_JSON),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": feature,
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data.get("status") != "needs_clarification"
    assert data["chunk_plan_status"] == "none"
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, intent FROM pipeline_runs WHERE id = :rid
        """), {"rid": data["run_id"]}).fetchone()
    assert row[0] == "report_ready"
    assert row[1] == "report_only"


# PR #8C: report intent specialization persists a specialized report_kind.
@pytest.mark.parametrize("feature,expected_kind", [
    ("explain this project", "project_explanation"),
    ("is there any issue in the code", "issue_review"),
    ("what features can we add to this project", "feature_discovery"),
])
def test_report_only_persists_specialized_report_kind(
    monkeypatch,
    tmp_repo,
    tracked_runs,
    feature,
    expected_kind,
):
    project = make_project(tmp_repo)
    monkeypatch.setattr(
        "backend.pipeline.report_analyzer.complete_for_role",
        _fake_analyzer_llm_response(VALID_ANALYZER_JSON),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": feature,
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, intent, report_json
            FROM pipeline_runs WHERE id = :rid
        """), {"rid": data["run_id"]}).fetchone()
    assert row[0] == "report_ready"
    assert row[1] == "report_only"
    assert row[2]
    structured = json.loads(row[2])
    assert structured["report_kind"] == expected_kind


VALID_IMPLEMENTATION_REQUESTS = [
    "implement login feature",
    "add CSV export feature",
    "add health check endpoint",
    "fix typo in README",
    "update retry limit from 3 to 5",
]


@pytest.mark.parametrize("feature", VALID_IMPLEMENTATION_REQUESTS)
def test_valid_implementation_requests_reach_chunk_plan(
    monkeypatch,
    tmp_repo,
    tracked_runs,
    feature,
):
    project = make_project(tmp_repo)
    calls = []

    async def fake_triage(run_id, project_id, feature_description):
        calls.append(run_id)
        return make_triage(run_id, project_id)

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


def _install_intent_llm(monkeypatch, payload):
    """
    Force the intent LLM fallback to return ``payload`` (a JSON string). The
    request used with this helper must be deterministically ambiguous so the
    fallback actually fires.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload)

    async def fake_complete_for_role(role, request, overrides=None):
        return LLMResponse(
            text=text,
            provider="fake",
            model="fake-model",
            input_tokens=10,
            output_tokens=10,
            finish_reason="stop",
        )

    monkeypatch.setattr(
        "backend.pipeline.intent.complete_for_role", fake_complete_for_role
    )


# "take care of the auth flow" matches no deterministic rule, so the intent
# LLM fallback fires; the deterministic implementation guard alone would let it
# pass (concrete tokens "auth"/"flow"), so the LLM specificity verdict decides.
_AMBIGUOUS_IMPLEMENTATION_TEXT = "take care of the auth flow"


def test_llm_fallback_implementation_needs_clarification_blocks(
    monkeypatch,
    tmp_repo,
):
    project = make_project(tmp_repo)
    _install_intent_llm(monkeypatch, {
        "intent": "implementation",
        "confidence": 0.95,
        "specificity": "needs_clarification",
        "specificity_confidence": 0.9,
        "reason": "too vague",
        "clarification_message": "Which auth behavior should change?",
        "missing_details": ["target auth behavior"],
    })
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
        "feature_description": _AMBIGUOUS_IMPLEMENTATION_TEXT,
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    # LLM-provided clarification copy is surfaced when present.
    assert data["message"] == "Which auth behavior should change?"
    assert data["missing_details"] == ["target auth behavior"]
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs WHERE project_id = :pid
        """), {"pid": project["id"]}).fetchone()[0]
    assert count == 0


def test_llm_fallback_implementation_specific_proceeds(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    _install_intent_llm(monkeypatch, {
        "intent": "implementation",
        "confidence": 0.95,
        "specificity": "specific",
        "specificity_confidence": 0.9,
        "reason": "clear enough",
        "clarification_message": None,
        "missing_details": [],
    })
    calls = []

    async def fake_triage(run_id, project_id, feature_description):
        calls.append(run_id)
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": _AMBIGUOUS_IMPLEMENTATION_TEXT,
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert len(calls) == 1
    assert data["chunk_plan_status"] == "awaiting_approval"


def test_llm_fallback_implementation_low_specificity_confidence_blocks(
    monkeypatch,
    tmp_repo,
):
    project = make_project(tmp_repo)
    _install_intent_llm(monkeypatch, {
        "intent": "implementation",
        "confidence": 0.95,
        "specificity": "specific",
        "specificity_confidence": 0.4,
        "reason": "unsure",
    })
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("triage called")),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": _AMBIGUOUS_IMPLEMENTATION_TEXT,
    })

    assert response.status_code == 200
    assert response.json()["status"] == "needs_clarification"


def test_uncertain_classification_returns_needs_clarification_not_plan(
    monkeypatch,
    tmp_repo,
):
    project = make_project(tmp_repo)
    # Invalid LLM JSON => uncertain. The router must NOT fall into the
    # plan_only bucket and fabricate a plan; it returns needs_clarification
    # and creates no run.
    _install_intent_llm(monkeypatch, "not json at all")
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("triage must not be called for uncertain input")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.create_chunked_run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("create_chunked_run must not be called")
        ),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": _AMBIGUOUS_IMPLEMENTATION_TEXT,
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["intent"] == "unknown"
    assert "run_id" not in data
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs WHERE project_id = :pid
        """), {"pid": project["id"]}).fetchone()[0]
    assert count == 0


def test_plan_only_chunked_run_stores_plan_without_executable_chunks(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    calls = []

    async def fake_triage(run_id, project_id, feature_description):
        calls.append((run_id, project_id, feature_description))
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "give me a plan to add route chunks",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert len(calls) == 1
    assert data["chunk_plan_status"] == "none"
    assert data["total_chunks"] == 1
    assert data["triage"]["total_chunks"] == 1
    assert data["chunks"] == []
    with engine.connect() as conn:
        run = conn.execute(text("""
            SELECT status, current_step, intent, chunk_plan
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": data["run_id"]}).fetchone()
        chunk_count = conn.execute(text("""
            SELECT COUNT(*) FROM chunks WHERE run_id = :run_id
        """), {"run_id": data["run_id"]}).fetchone()[0]
        gate_count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates WHERE run_id = :run_id
        """), {"run_id": data["run_id"]}).fetchone()[0]
    assert run[0] == "plan_ready"
    assert run[1] == "plan_ready"
    assert run[2] == "plan_only"
    assert "Route chunk" in run[3]
    assert chunk_count == 0
    assert gate_count == 0


def test_triage_failure_does_not_create_parent_run(monkeypatch, tmp_repo):
    project = make_project(tmp_repo)
    # A specific implementation request so it reaches triage (not the
    # actionability / uncertain / vague guards).
    feature = f"add health check endpoint {uuid.uuid4()}"

    async def failing_triage(run_id, project_id, feature_description):
        raise RuntimeError("triage failed")

    monkeypatch.setattr("backend.routes.chunks.run_triage", failing_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": feature,
    })

    assert response.status_code == 500
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs
            WHERE feature_description = :feature
        """), {"feature": feature}).fetchone()[0]
    assert count == 0


def test_missing_project_returns_404_before_triage(monkeypatch):
    called = {"value": False}

    async def fake_triage(run_id, project_id, feature_description):
        called["value"] = True
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": "proj-missing",
        "feature_description": "Add route chunks",
    })

    assert response.status_code == 404
    assert called["value"] is False


def test_chunked_run_rejects_empty_feature_description(tmp_repo):
    project = make_project(tmp_repo)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "",
    })

    assert response.status_code == 422


def test_chunked_run_rejects_too_long_feature_description(tmp_repo):
    project = make_project(tmp_repo)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "x" * 12001,
    })

    assert response.status_code == 422


def _insert_read_only_run(run_id: str, project_id: str, intent: str):
    status = "report_ready" if intent == "report_only" else "plan_ready"
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (
                id, project_id, feature_description, status, current_step,
                intent, chunk_plan_status, total_chunks, current_chunk_number
            )
            VALUES
            (
                :run_id, :project_id, 'Read-only run', :status, :status,
                :intent, 'none', 0, 0
            )
        """), {
            "run_id": run_id,
            "project_id": project_id,
            "status": status,
            "intent": intent,
        })


@pytest.mark.parametrize("intent", ["report_only", "plan_only"])
@pytest.mark.parametrize("path,body", [
    ("/runs/{run_id}/chunks/approve", None),
    ("/runs/{run_id}/chunks/reject", {"reason": "no"}),
    ("/runs/{run_id}/chunks/execute", None),
    ("/runs/{run_id}/chunks/resume", None),
    ("/runs/{run_id}/chunks/1/approve", None),
    ("/runs/{run_id}/chunks/1/reject", {"reason": "no"}),
    ("/runs/{run_id}/final-approval/approve", None),
    ("/runs/{run_id}/final-approval/reject", {"reason": "no"}),
    ("/runs/{run_id}/memory-conflict/approve", None),
    ("/runs/{run_id}/memory-conflict/reject", {"reason": "no"}),
    ("/runs/{run_id}/push-pr", None),
])
def test_read_only_runs_reject_mutating_routes(
    monkeypatch,
    tmp_repo,
    tracked_runs,
    intent,
    path,
    body,
):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    _insert_read_only_run(run_id, project["id"], intent)

    monkeypatch.setattr(
        "backend.routes.chunks.approve_chunk_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("approve called")),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.reject_chunk_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("reject called")),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.execute_approved_chunks",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("execute called")),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.resume_chunked_pipeline",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("resume called")),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.approve_chunk_and_commit",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("chunk approve called")),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.reject_chunk_and_rollback",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("chunk reject called")),
    )
    monkeypatch.setattr(
        "backend.routes.chunks._decide_final_gate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("final gate called")),
    )
    monkeypatch.setattr(
        "backend.routes.chunks._decide_memory_conflict_gate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("memory conflict gate called")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.push_and_create_pr",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("push called")),
    )
    client = TestClient(app)

    response = client.post(path.format(run_id=run_id), json=body) if body else client.post(
        path.format(run_id=run_id)
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "This run is read-only and cannot execute code changes."


def test_get_chunks_route_returns_plan(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Add route chunks",
        make_triage(run_id, project["id"]),
    )
    client = TestClient(app)

    response = client.get(f"/runs/{run_id}/chunks")

    assert response.status_code == 200
    assert response.json()["run_id"] == run_id


def test_approve_endpoint_approves_only_and_does_not_execute(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Add route chunks",
        make_triage(run_id, project["id"]),
    )
    client = TestClient(app)

    response = client.post(f"/runs/{run_id}/chunks/approve")

    assert response.status_code == 200
    data = response.json()
    assert data["chunk_plan_status"] == "approved"
    assert data["chunks"][0]["status"] == "pending"


def test_reject_endpoint_rejects_only(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Add route chunks",
        make_triage(run_id, project["id"]),
    )
    client = TestClient(app)

    response = client.post(f"/runs/{run_id}/chunks/reject", json={
        "reason": "not ready",
    })

    assert response.status_code == 200
    assert response.json()["chunk_plan_status"] == "rejected"


def test_execute_and_resume_routes_exist():
    paths = {route.path for route in app.routes}

    assert "/runs/{run_id}/chunks/execute" in paths
    assert "/runs/{run_id}/chunks/resume" in paths
    assert "/runs/{run_id}/chunks/{chunk_number}/approve" in paths
    assert "/runs/{run_id}/chunks/{chunk_number}/reject" in paths
    assert "/runs/{run_id}/final-approval/approve" in paths
    assert "/runs/{run_id}/final-approval/reject" in paths
    assert "/runs/{run_id}/memory-conflict/approve" in paths
    assert "/runs/{run_id}/memory-conflict/reject" in paths
    assert "/runs/{run_id}/push-pr" in paths


def test_chunk_approve_route_calls_helper(monkeypatch):
    called = {"run_id": None, "chunk_number": None}

    def fake_approve(run_id, chunk_number):
        called["run_id"] = run_id
        called["chunk_number"] = chunk_number
        return {
            "status": "chunk_approved",
            "run_id": run_id,
            "chunk_number": chunk_number,
            "next_action": f"call /runs/{run_id}/chunks/resume to continue",
        }

    monkeypatch.setattr("backend.routes.chunks.approve_chunk_and_commit", fake_approve)
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/2/approve")

    assert response.status_code == 200
    assert response.json()["status"] == "chunk_approved"
    assert called == {"run_id": "run-123", "chunk_number": 2}


def test_chunk_reject_route_calls_helper(monkeypatch):
    called = {"run_id": None, "chunk_number": None, "reason": None}

    def fake_reject(run_id, chunk_number, reason=None):
        called["run_id"] = run_id
        called["chunk_number"] = chunk_number
        called["reason"] = reason
        return {
            "status": "chunk_rejected",
            "run_id": run_id,
            "chunk_number": chunk_number,
        }

    monkeypatch.setattr("backend.routes.chunks.reject_chunk_and_rollback", fake_reject)
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/2/reject", json={
        "reason": "not safe",
    })

    assert response.status_code == 200
    assert response.json()["status"] == "chunk_rejected"
    assert called == {
        "run_id": "run-123",
        "chunk_number": 2,
        "reason": "not safe",
    }


def test_chunk_approve_route_returns_controlled_error(monkeypatch):
    def fake_approve(run_id, chunk_number):
        raise RuntimeError("pending chunk gate not found")

    monkeypatch.setattr("backend.routes.chunks.approve_chunk_and_commit", fake_approve)
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/2/approve")

    assert response.status_code == 400
    assert "pending chunk gate not found" in response.json()["detail"]


def test_chunk_reject_route_returns_controlled_error(monkeypatch):
    def fake_reject(run_id, chunk_number, reason=None):
        raise RuntimeError("pending chunk gate not found")

    monkeypatch.setattr("backend.routes.chunks.reject_chunk_and_rollback", fake_reject)
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/2/reject", json={
        "reason": "not safe",
    })

    assert response.status_code == 400
    assert "pending chunk gate not found" in response.json()["detail"]


def test_chunk_plan_reject_route_rejects_too_long_reason():
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/reject", json={
        "reason": "x" * 2001,
    })

    assert response.status_code == 422


def test_chunk_approval_reject_route_rejects_too_long_reason():
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/1/reject", json={
        "reason": "x" * 2001,
    })

    assert response.status_code == 422


def test_final_approval_reject_route_rejects_too_long_reason():
    client = TestClient(app)

    response = client.post("/runs/run-123/final-approval/reject", json={
        "reason": "x" * 2001,
    })

    assert response.status_code == 422


def test_memory_conflict_reject_route_rejects_too_long_reason():
    client = TestClient(app)

    response = client.post("/runs/run-123/memory-conflict/reject", json={
        "reason": "x" * 2001,
    })

    assert response.status_code == 422


def test_execute_route_maps_project_lock_conflict_to_409(monkeypatch):
    async def fake_execute(run_id):
        raise ProjectRepoLockError(PROJECT_LOCK_CONFLICT_MESSAGE)

    monkeypatch.setattr("backend.routes.chunks.execute_approved_chunks", fake_execute)
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/execute")

    assert response.status_code == 409
    assert response.json()["detail"] == PROJECT_LOCK_CONFLICT_MESSAGE


def test_resume_route_maps_project_lock_conflict_to_409(monkeypatch):
    async def fake_resume(run_id):
        raise ProjectRepoLockError(PROJECT_LOCK_CONFLICT_MESSAGE)

    monkeypatch.setattr("backend.routes.chunks.resume_chunked_pipeline", fake_resume)
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/resume")

    assert response.status_code == 409
    assert response.json()["detail"] == PROJECT_LOCK_CONFLICT_MESSAGE


def test_approve_chunk_route_maps_project_lock_conflict_to_409(monkeypatch):
    def fake_approve(run_id, chunk_number):
        raise ProjectRepoLockError(PROJECT_LOCK_CONFLICT_MESSAGE)

    monkeypatch.setattr("backend.routes.chunks.approve_chunk_and_commit", fake_approve)
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/1/approve")

    assert response.status_code == 409
    assert response.json()["detail"] == PROJECT_LOCK_CONFLICT_MESSAGE


def test_reject_chunk_route_maps_project_lock_conflict_to_409(monkeypatch):
    def fake_reject(run_id, chunk_number, reason):
        raise ProjectRepoLockError(PROJECT_LOCK_CONFLICT_MESSAGE)

    monkeypatch.setattr("backend.routes.chunks.reject_chunk_and_rollback", fake_reject)
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/1/reject", json={
        "reason": "not safe",
    })

    assert response.status_code == 409
    assert response.json()["detail"] == PROJECT_LOCK_CONFLICT_MESSAGE


def test_final_approval_approve_route_updates_run(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Add route chunks",
        make_triage(run_id, project["id"]),
    )
    create_final_approval_gate(run_id, "final summary")
    client = TestClient(app)

    response = client.post(f"/runs/{run_id}/final-approval/approve")

    assert response.status_code == 200
    assert response.json()["status"] == "final_approved"
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT pr.status, ag.status
            FROM pipeline_runs pr
            JOIN approval_gates ag ON ag.run_id = pr.id
            WHERE pr.id = :run_id AND ag.approval_type = 'final'
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "final_approved"
    assert row[1] == "approved"


def test_final_approval_reject_route_updates_run_without_rollback(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Add route chunks",
        make_triage(run_id, project["id"]),
    )
    create_final_approval_gate(run_id, "final summary")
    rollback_called = {"value": False}

    def fake_rollback(*args, **kwargs):
        rollback_called["value"] = True

    monkeypatch.setattr("backend.pipeline.patch_applier.rollback_patch", fake_rollback)
    client = TestClient(app)

    response = client.post(f"/runs/{run_id}/final-approval/reject", json={
        "reason": "not ready",
    })

    assert response.status_code == 200
    assert response.json()["status"] == "final_rejected"
    assert rollback_called["value"] is False
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT pr.status, ag.status, ag.rejection_reason
            FROM pipeline_runs pr
            JOIN approval_gates ag ON ag.run_id = pr.id
            WHERE pr.id = :run_id AND ag.approval_type = 'final'
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "final_rejected"
    assert row[1] == "rejected"
    assert row[2] == "not ready"


def test_push_pr_route_calls_helper(monkeypatch):
    called = {"run_id": None}

    def fake_push(run_id):
        called["run_id"] = run_id
        return {
            "status": "complete",
            "run_id": run_id,
            "branch_name": "pipewright/run-123",
            "pr_url": "https://github.com/acme/demo/pull/1",
            "pr_number": 1,
        }

    monkeypatch.setattr("backend.routes.chunks.push_and_create_pr", fake_push)
    client = TestClient(app)

    response = client.post("/runs/run-123/push-pr")

    assert response.status_code == 200
    assert response.json()["status"] == "complete"
    assert called["run_id"] == "run-123"


def test_push_pr_route_maps_project_lock_conflict_to_409(monkeypatch):
    def fake_push(run_id):
        raise ProjectRepoLockError(PROJECT_LOCK_CONFLICT_MESSAGE)

    monkeypatch.setattr("backend.routes.chunks.push_and_create_pr", fake_push)
    client = TestClient(app)

    response = client.post("/runs/run-123/push-pr")

    assert response.status_code == 409
    assert response.json()["detail"] == PROJECT_LOCK_CONFLICT_MESSAGE


def test_post_runs_chunked_upgrades_low_risk_route_chunk_to_high(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)

    async def fake_triage(run_id, project_id, feature_description):
        return TriageResult(
            run_id=run_id,
            project_id=project_id,
            feature_description=feature_description,
            complexity="easy",
            total_chunks=1,
            reasoning="Adjust a route handler.",
            chunks=[ChunkDefinition(
                chunk_number=1,
                title="Tweak route",
                description="Adjust handler logic.",
                files_expected=["backend/routes/foo.py"],
                depends_on=[],
                risk_level="low",
                token_estimate=100,
                requires_human_review=False,
                rationale="Marked low by triage.",
            )],
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "Modify backend/routes/foo.py handler",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    chunk = data["chunks"][0]
    assert chunk["risk_level"] == "high"
    assert chunk["requires_human_review"] is True
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT risk_level, requires_human_review
            FROM chunks
            WHERE run_id = :run_id AND chunk_number = 1
        """), {"run_id": data["run_id"]}).fetchone()
    assert row[0] == "high"
    assert int(row[1]) == 1


# --------------------------------------------------------------------------
# PR #9B: repo-aware files_expected grounding (route/integration).
# --------------------------------------------------------------------------

def test_implementation_grounds_out_fake_paths(monkeypatch, tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    # Real frontend area uses .tsx.
    seed_file_index(project["id"], ["frontend/src/App.tsx"])

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage_with_files(
            run_id,
            project_id,
            files_expected=[
                "frontend/src/components/HomePage.js",  # .js in tsx area -> drop
                "backend/src/routes/home.py",           # backend/src absent -> drop
                "frontend/src/App.tsx",                 # real -> keep
            ],
            title="Home view",
            description="Adjust the home view layout.",
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "implement the home page",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    chunk = data["chunks"][0]
    assert chunk["files_expected"] == ["frontend/src/App.tsx"]
    assert chunk["risk_level"] == "high"
    assert chunk["requires_human_review"] is True
    # Persisted chunk row matches the grounded scope.
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT files_expected, risk_level, requires_human_review
            FROM chunks WHERE run_id = :rid AND chunk_number = 1
        """), {"rid": data["run_id"]}).fetchone()
    assert "HomePage.js" not in row[0]
    assert "backend/src" not in row[0]
    assert "frontend/src/App.tsx" in row[0]


def test_implementation_empty_when_all_paths_fake(monkeypatch, tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["frontend/src/App.tsx"])

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage_with_files(
            run_id,
            project_id,
            files_expected=[
                "src/features/extraordinary_feature.py",
                "backend/src/services/auth_service.py",
            ],
            title="New view",
            description="Build the view.",
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "implement the home page",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    chunk = data["chunks"][0]
    # No invented fallback path; empty and hardened for human review.
    assert chunk["files_expected"] == []
    assert chunk["risk_level"] == "high"
    assert chunk["requires_human_review"] is True


def test_implementation_preserves_indexed_readme(monkeypatch, tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["README.md"])

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage_with_files(
            run_id,
            project_id,
            files_expected=["README.md"],
            title="Docs fix",
            description="Fix a typo.",
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "fix typo in README",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    chunk = data["chunks"][0]
    assert chunk["files_expected"] == ["README.md"]


def test_plan_only_output_is_grounded(monkeypatch, tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/routes/users.py"])

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage_with_files(
            run_id,
            project_id,
            files_expected=[
                "src/models/user.py",       # fake -> drop
                "src/api/auth.py",          # fake -> drop
                "backend/routes/users.py",  # real -> keep
            ],
            title="Login plan",
            description="Plan the login work.",
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "give me a plan to add login",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    planned_chunk = data["triage"]["chunks"][0]
    assert planned_chunk["files_expected"] == ["backend/routes/users.py"]
    assert "src/models/user.py" not in planned_chunk["files_expected"]
    assert planned_chunk["risk_level"] == "high"
    # Stored plan JSON is grounded too: the fake path is gone from
    # files_expected (it may still be named in the rationale note).
    with engine.connect() as conn:
        chunk_plan = conn.execute(text("""
            SELECT chunk_plan FROM pipeline_runs WHERE id = :rid
        """), {"rid": data["run_id"]}).fetchone()[0]
    stored = json.loads(chunk_plan)
    stored_files = stored["chunks"][0]["files_expected"]
    assert stored_files == ["backend/routes/users.py"]
    assert "src/models/user.py" not in stored_files


def test_handoff_uses_grounded_plan(monkeypatch, tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/routes/users.py"])

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage_with_files(
            run_id,
            project_id,
            files_expected=[
                "frontend/src/components/LoginPage.js",  # fake -> drop
                "backend/routes/users.py",               # real -> keep
            ],
            title="Login plan",
            description="Plan the login work.",
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    # 1) Create a grounded plan_only run.
    plan_resp = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "give me a plan to add login",
    })
    assert plan_resp.status_code == 200
    plan_run_id = plan_resp.json()["run_id"]
    tracked_runs.append(plan_run_id)

    # 2) Hand off to implementation (PR #7).
    impl_resp = client.post(f"/runs/{plan_run_id}/start-implementation")
    assert impl_resp.status_code == 200
    impl_data = impl_resp.json()
    tracked_runs.append(impl_data["run_id"])

    chunk = impl_data["chunks"][0]
    assert chunk["files_expected"] == ["backend/routes/users.py"]
    assert "LoginPage.js" not in " ".join(chunk["files_expected"])


# --------------------------------------------------------------------------
# PR #17C: explicit-edit-target grounding wired into chunked run creation.
# --------------------------------------------------------------------------

def _forbid_triage(monkeypatch):
    """Assert run_triage is never reached (clarification short-circuits first)."""
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("run_triage must not be called")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.create_chunked_run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("create_chunked_run must not be called")
        ),
    )


def _assert_no_runs(project_id: str) -> None:
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs WHERE project_id = :pid
        """), {"pid": project_id}).fetchone()[0]
    assert count == 0


def test_grounded_readme_pins_files_expected(monkeypatch, tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["README.md", "backend/app.py"])

    async def fake_triage(run_id, project_id, feature_description):
        # Triage proposes a different path; the pin must override it.
        return make_triage_with_files(
            run_id, project_id, files_expected=["backend/app.py"],
            title="Docs edit", description="Edit docs.",
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add hello in the readme",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data.get("status") != "needs_clarification"
    chunk = data["chunks"][0]
    assert chunk["files_expected"] == ["README.md"]
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT files_expected FROM chunks
            WHERE run_id = :rid AND chunk_number = 1
        """), {"rid": data["run_id"]}).fetchone()
    assert "README.md" in row[0]


def test_grounded_pin_rescues_empty_files_expected(
    monkeypatch, tmp_repo, tracked_runs
):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["README.md"])

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage_with_files(
            run_id, project_id, files_expected=[],
            title="Docs edit", description="Edit docs.",
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add hello in the readme",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    # The pin produced a non-empty scope, so scope_guard never sees empty.
    assert data["chunks"][0]["files_expected"] == ["README.md"]


def test_readme_not_indexed_returns_specific_clarification(monkeypatch, tmp_repo):
    project = make_project(tmp_repo)
    # Indexed, but no README present.
    seed_file_index(project["id"], ["backend/app.py"])
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add hello in the readme",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["intent"] == "implementation"
    assert "README" in data["message"]
    assert "run_id" not in data
    _assert_no_runs(project["id"])


def test_multiple_readmes_returns_ambiguous_clarification(monkeypatch, tmp_repo):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["README.md", "docs/README.md"])
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add hello in the readme",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert "README.md" in data["message"]
    assert "docs/README.md" in data["message"]
    assert "run_id" not in data
    _assert_no_runs(project["id"])


def test_explicit_path_pins_files_expected(monkeypatch, tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["docs/usage.md", "backend/app.py"])

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage_with_files(
            run_id, project_id, files_expected=["backend/app.py"],
            title="Docs edit", description="Edit usage docs.",
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add a note to docs/usage.md",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunks"][0]["files_expected"] == ["docs/usage.md"]


def test_forbidden_target_returns_safe_refusal(monkeypatch, tmp_repo):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/app.py"])
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add a key to .env",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert "protected" in data["message"].lower()
    assert "run_id" not in data
    _assert_no_runs(project["id"])


def test_explicit_create_missing_file_clarifies_without_creating(
    monkeypatch, tmp_repo
):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/app.py"])
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "create README.md with a title",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert "automatically" in data["message"].lower()
    assert "run_id" not in data
    _assert_no_runs(project["id"])
    # We never created README.md on disk.
    assert not (tmp_repo / "README.md").exists()


def test_no_target_request_proceeds_unchanged(monkeypatch, tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/models/user.py"])
    calls = []

    async def fake_triage(run_id, project_id, feature_description):
        calls.append(run_id)
        return make_triage_with_files(
            run_id, project_id, files_expected=["backend/models/user.py"],
            title="Model edit", description="Edit the model.",
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add comment in user model",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data.get("status") != "needs_clarification"
    assert len(calls) == 1


def test_empty_index_skips_resolver_and_proceeds(
    monkeypatch, tmp_repo, tracked_runs
):
    project = make_project(tmp_repo)
    # No file_index seeded -> resolver is skipped. An explicit path request that
    # passes the specificity guard must proceed to triage unchanged (not be
    # blocked as a missing target).
    calls = []

    async def fake_triage(run_id, project_id, feature_description):
        calls.append(run_id)
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add a note to docs/usage.md",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data.get("status") != "needs_clarification"
    assert len(calls) == 1


def test_unindexed_readme_request_falls_back_to_existing_guard(
    monkeypatch, tmp_repo
):
    # Documents the index-gate limitation: with no index, the resolver cannot
    # ground "readme", so the request falls through to the existing specificity
    # guard, which blocks this particular phrasing as vague. Once the project is
    # indexed (see test_grounded_readme_pins_files_expected) it grounds instead.
    project = make_project(tmp_repo)
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add hello in the readme",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    _assert_no_runs(project["id"])


def test_vague_request_still_generic_clarification(monkeypatch, tmp_repo):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["README.md"])
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "fix it",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    # Generic guard message (no target named), not a README-specific one.
    assert "too vague" in data["message"].lower()
    _assert_no_runs(project["id"])
