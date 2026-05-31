"""
test_db_memory_conflict_gate.py
Tests for the DB memory conflict BLOCKING gate (Memory M1.5 PR #16D-4).

Covers backend/pipeline/chunked_orchestrator's blocking policy and the
backend/routes/chunks decision endpoints:

  - A. Blocking: a clear DB conflict on a DB-sensitive run pauses execution behind a
       `memory_conflict` approval gate, before any branch/patch/commit.
  - B. Non-blocking: README-only / ambiguous / unknown signal / no conflict proceed and
       preserve the #16D-3 warning.
  - C. Override-once: approving the gate lets THIS run continue; a different run still
       gates; a changed conflict re-blocks.
  - D. Rejection: rejecting the gate rejects the run with nothing applied.
  - E. Safety: gate evaluation never mutates memory, never crosses projects, never leaks
       secrets, runs once per run, and the decision core is pure.

Deterministic: temp repos, isolated DB rows, mocked pipeline/git. No real AI, no push.
"""

import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.core.statuses import ApprovalStatus, RunStatus
from backend.db.database import engine
from backend.events.event_bus import clear_all_events_for_tests, get_buffered_events
from backend.main import app
from backend.memory.memory_store import add_fact, list_facts
from backend.memory.repo_reality import ConflictEntry, ConflictReport
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import chunked_orchestrator
from backend.pipeline.approval_gate import (
    get_approved_memory_conflict_gate,
    get_pending_memory_conflict_gate,
)
from backend.pipeline.chunk_store import (
    approve_chunk_plan,
    create_chunked_run,
)
from backend.pipeline.chunked_orchestrator import (
    _apply_db_memory_conflict_policy,
    _conflict_signature,
    _db_conflict_block_decision,
    execute_approved_chunks,
)
from backend.projects.project_store import create_project
from backend.routes.chunks import _decide_memory_conflict_gate

# Reuse the proven pipeline/git mocks from the orchestrator suite so an allowed run
# never touches real AI or git, and every planner/coder/patch/test/branch/commit call
# is recorded in a shared ``calls`` list.
from backend.tests.test_chunked_orchestrator import (
    patch_git_preflight,
    patch_success_pipeline,
)

pytestmark = pytest.mark.unit

client = TestClient(app)

LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"

# DB-sensitive expected files. Includes the fake pipeline's changed files so the scope
# guard passes when a run is allowed through (override / non-blocking end-to-end).
PIPELINE_FILES = ["created_1.py", "modified_1.py", "deleted_1.py"]
DB_SENSITIVE_FILES = ["backend/models/user.py", *PIPELINE_FILES]
NON_SENSITIVE_FILES = ["README.md"]


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture()
def repo():
    root = LOCAL_TMP / str(uuid.uuid4())
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def tracked():
    """Track created projects and runs; clean up all their rows afterward."""
    project_ids: list[str] = []
    run_ids: list[str] = []

    yield project_ids, run_ids

    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM approval_gates WHERE run_id = :r"), {"r": run_id})
            conn.execute(text("DELETE FROM chunks WHERE run_id = :r"), {"r": run_id})
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :r"), {"r": run_id})
        for project_id in project_ids:
            conn.execute(text("DELETE FROM memory_facts WHERE project_id = :p"), {"p": project_id})


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _postgres_repo(root: Path) -> None:
    """Repo whose fingerprint resolves to a clear, non-ambiguous PostgreSQL signal."""
    _write(root, "requirements.txt", "psycopg2\n")


def _make_project(root: Path, tracked) -> str:
    project_ids, _ = tracked
    project = create_project(
        name=f"GateProj {uuid.uuid4()}",
        repo_path=str(root),
        test_command="python --version",
    )
    project_ids.append(project["id"])
    return project["id"]


def _add_db_fact(project_id: str, content: str) -> str:
    fact = add_fact(
        project_id=project_id, content=content, category="db", scope="backend",
        source="manual", added_by="test", approved_by="test",
    )
    return fact["id"]


def _make_run(project_id: str, tracked, files: list[str]) -> str:
    """Create an approved single-chunk run whose chunk lists ``files`` as expected."""
    _, run_ids = tracked
    run_id = str(uuid.uuid4())
    run_ids.append(run_id)
    triage = TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description="Change the data layer",
        complexity="easy",
        total_chunks=1,
        chunks=[ChunkDefinition(
            chunk_number=1,
            title="Chunk 1",
            description="Do chunk 1",
            files_expected=list(files),
            depends_on=[],
            risk_level="low",
            token_estimate=100,
            requires_human_review=False,
            rationale="test",
        )],
        reasoning="gate test",
    )
    create_chunked_run(run_id, project_id, "Change the data layer", triage)
    approve_chunk_plan(run_id)
    return run_id


def _mock_pipeline_and_git(monkeypatch, run_id: str, calls: list) -> None:
    """Mock the AI pipeline and all git side effects. ``calls`` records every
    planner/coder/patch/test plus branch/commit, so a blocked run can be proven to have
    invoked none of them."""
    patch_success_pipeline(monkeypatch, run_id, calls)
    patch_git_preflight(monkeypatch, calls)


def _fact_snapshot(project_id: str, fact_id: str) -> dict:
    fact = next(f for f in list_facts(project_id) if f["id"] == fact_id)
    return dict(fact)


def _conflict_events(run_id):
    return [
        e for e in get_buffered_events(run_id)
        if e.kind == "log" and isinstance(e.data, dict)
        and str(e.data.get("type", "")).startswith("memory_db_conflict")
    ]


# --------------------------------------------------------------------------- #
# A. Blocking
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_db_conflict_db_sensitive_run_blocks_before_any_work(repo, tracked, monkeypatch):
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    calls: list = []
    _mock_pipeline_and_git(monkeypatch, run_id, calls)

    result = await execute_approved_chunks(run_id)

    # Paused on the conflict gate.
    assert result["status"] == "awaiting_memory_conflict_approval"
    assert result["approval_required"] is True

    gate = get_pending_memory_conflict_gate(run_id)
    assert gate is not None
    assert gate["approval_type"] == "memory_conflict"
    assert gate["risk_level"] == "high"
    assert gate["chunk_number"] == 0
    assert "MongoDB" in gate["ai_summary"]
    assert "PostgreSQL" in gate["ai_summary"]

    # Run status / current_step reflect the pause.
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, current_step FROM pipeline_runs WHERE id = :r"),
            {"r": run_id},
        ).fetchone()
    assert row._mapping["status"] == "awaiting_memory_conflict_approval"
    assert row._mapping["current_step"] == "memory_conflict_approval"

    # Nothing was executed: no planner/coder/patch/tester, no branch/commit.
    assert calls == []


# --------------------------------------------------------------------------- #
# B. Non-blocking (policy returns None, no gate, #16D-3 warning preserved)
# --------------------------------------------------------------------------- #

def test_readme_only_run_does_not_block_but_warns(repo, tracked):
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, NON_SENSITIVE_FILES)

    pause = _apply_db_memory_conflict_policy(run_id, project_id, str(repo), NON_SENSITIVE_FILES)

    assert pause is None
    assert get_pending_memory_conflict_gate(run_id) is None
    # #16D-3 warning preserved (non-sensitive => info level).
    events = _conflict_events(run_id)
    assert len(events) == 1
    assert events[0].level == "info"


def test_ambiguous_repo_signal_does_not_block(repo, tracked):
    clear_all_events_for_tests()
    _write(repo, "requirements.txt", "psycopg2\n")
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    pause = _apply_db_memory_conflict_policy(run_id, project_id, str(repo), DB_SENSITIVE_FILES)

    assert pause is None
    assert get_pending_memory_conflict_gate(run_id) is None


def test_unknown_repo_signal_does_not_block(repo, tracked):
    clear_all_events_for_tests()
    _write(repo, "README.md", "# docs only")  # no DB manifest -> unknown signal
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    pause = _apply_db_memory_conflict_policy(run_id, project_id, str(repo), DB_SENSITIVE_FILES)

    assert pause is None
    assert get_pending_memory_conflict_gate(run_id) is None


def test_no_conflict_does_not_block(repo, tracked):
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses PostgreSQL.")  # memory agrees with repo
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    pause = _apply_db_memory_conflict_policy(run_id, project_id, str(repo), DB_SENSITIVE_FILES)

    assert pause is None
    assert get_pending_memory_conflict_gate(run_id) is None
    assert _conflict_events(run_id) == []


# --------------------------------------------------------------------------- #
# C. Override-once
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_approving_gate_lets_this_run_continue(repo, tracked, monkeypatch):
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    calls: list = []
    _mock_pipeline_and_git(monkeypatch, run_id, calls)

    # 1) First execute blocks.
    first = await execute_approved_chunks(run_id)
    assert first["status"] == "awaiting_memory_conflict_approval"
    assert calls == []

    # 2) Approve the gate (override once) via the route decision helper.
    _decide_memory_conflict_gate(run_id, ApprovalStatus.APPROVED, RunStatus.CHUNK_PLAN_APPROVED)
    assert get_approved_memory_conflict_gate(run_id) is not None
    assert get_pending_memory_conflict_gate(run_id) is None

    # 3) Re-execute: the matching approved override is honored, run proceeds.
    second = await execute_approved_chunks(run_id)
    assert second["status"] == "awaiting_final_approval"
    assert any(c[0] == "planner" for c in calls)
    assert any(c[0] == "branch" for c in calls)


@pytest.mark.asyncio
async def test_override_is_run_scoped(repo, tracked, monkeypatch):
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")

    run_a = _make_run(project_id, tracked, DB_SENSITIVE_FILES)
    run_b = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    calls: list = []
    _mock_pipeline_and_git(monkeypatch, run_a, calls)

    # Run A blocks and is overridden.
    await execute_approved_chunks(run_a)
    _decide_memory_conflict_gate(run_a, ApprovalStatus.APPROVED, RunStatus.CHUNK_PLAN_APPROVED)

    # Run B (same conflict) still gates — the override does not leak across runs.
    result_b = await execute_approved_chunks(run_b)
    assert result_b["status"] == "awaiting_memory_conflict_approval"
    assert get_pending_memory_conflict_gate(run_b) is not None


def test_resume_honors_matching_approved_override(repo, tracked):
    """Both execute and resume call _apply_db_memory_conflict_policy; an approved gate
    whose signature matches the current conflict is honored (no re-block)."""
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    # Block, then approve in DB (signature preserved in test_results).
    pause = _apply_db_memory_conflict_policy(run_id, project_id, str(repo), DB_SENSITIVE_FILES)
    assert pause is not None
    _decide_memory_conflict_gate(run_id, ApprovalStatus.APPROVED, RunStatus.CHUNK_PLAN_APPROVED)

    # Resume re-evaluates: matching override honored.
    again = _apply_db_memory_conflict_policy(run_id, project_id, str(repo), DB_SENSITIVE_FILES)
    assert again is None


def test_changed_conflict_re_blocks_despite_approved_override(repo, tracked):
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    gate = chunked_orchestrator.create_memory_conflict_gate_and_mark_run(
        run_id, "summary", "stale-signature-that-no-longer-matches"
    )
    _decide_memory_conflict_gate(run_id, ApprovalStatus.APPROVED, RunStatus.CHUNK_PLAN_APPROVED)

    # Current conflict signature differs from the approved one -> re-block.
    pause = _apply_db_memory_conflict_policy(run_id, project_id, str(repo), DB_SENSITIVE_FILES)
    assert pause is not None
    assert pause["status"] == "awaiting_memory_conflict_approval"


# --------------------------------------------------------------------------- #
# D. Rejection
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_rejecting_gate_rejects_run_with_nothing_applied(repo, tracked, monkeypatch):
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    calls: list = []
    _mock_pipeline_and_git(monkeypatch, run_id, calls)

    blocked = await execute_approved_chunks(run_id)
    assert blocked["status"] == "awaiting_memory_conflict_approval"

    _decide_memory_conflict_gate(
        run_id, ApprovalStatus.REJECTED, RunStatus.REJECTED, "stale memory"
    )

    with engine.connect() as conn:
        run_status = conn.execute(
            text("SELECT status FROM pipeline_runs WHERE id = :r"), {"r": run_id},
        ).scalar()
        gate_status = conn.execute(
            text("SELECT status FROM approval_gates WHERE run_id = :r "
                 "AND approval_type = 'memory_conflict'"),
            {"r": run_id},
        ).scalar()
    assert run_status == "rejected"
    assert gate_status == "rejected"
    # Nothing was applied: no pipeline work, no branch/commit.
    assert calls == []


# --------------------------------------------------------------------------- #
# E. Safety
# --------------------------------------------------------------------------- #

def test_gate_evaluation_never_mutates_memory(repo, tracked):
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    fact_id = _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    before = _fact_snapshot(project_id, fact_id)
    pause = _apply_db_memory_conflict_policy(run_id, project_id, str(repo), DB_SENSITIVE_FILES)
    after = _fact_snapshot(project_id, fact_id)

    assert pause is not None  # it did block (so evaluation definitely ran)
    assert before == after    # ...yet the fact is byte-identical: no verify, no stale


def test_conflict_in_project_a_never_gates_project_b(repo, tracked):
    clear_all_events_for_tests()
    # Shared repo => PostgreSQL. Project A conflicts (MongoDB); Project B agrees.
    _postgres_repo(repo)
    project_a = _make_project(repo, tracked)
    _add_db_fact(project_a, "Project uses MongoDB.")
    project_b = _make_project(repo, tracked)
    _add_db_fact(project_b, "Project uses PostgreSQL.")

    run_b = _make_run(project_b, tracked, DB_SENSITIVE_FILES)
    pause = _apply_db_memory_conflict_policy(run_b, project_b, str(repo), DB_SENSITIVE_FILES)

    assert pause is None
    assert get_pending_memory_conflict_gate(run_b) is None


def test_gate_and_events_contain_no_secret(repo, tracked):
    clear_all_events_for_tests()
    secret = "sk-thisisaverylongsecretkeyvalue"
    _write(repo, ".env", f"DATABASE_URL=postgres://user:{secret}@host/db\n")
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    pause = _apply_db_memory_conflict_policy(run_id, project_id, str(repo), DB_SENSITIVE_FILES)
    assert pause is not None

    gate = get_pending_memory_conflict_gate(run_id)
    assert secret not in (gate["ai_summary"] or "")
    assert secret not in (gate["test_results"] or "")
    for event in get_buffered_events(run_id):
        assert secret not in event.message
        assert secret not in str(event.data)


@pytest.mark.asyncio
async def test_evaluator_called_once_per_execute(repo, tracked, monkeypatch):
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    calls: list = []
    _mock_pipeline_and_git(monkeypatch, run_id, calls)

    real = chunked_orchestrator.evaluate_db_memory_conflicts
    counter = {"n": 0}

    def counting(*a, **k):
        counter["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(chunked_orchestrator, "evaluate_db_memory_conflicts", counting)

    await execute_approved_chunks(run_id)
    assert counter["n"] == 1


def test_calling_policy_twice_reuses_single_gate(repo, tracked):
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    _apply_db_memory_conflict_policy(run_id, project_id, str(repo), DB_SENSITIVE_FILES)
    _apply_db_memory_conflict_policy(run_id, project_id, str(repo), DB_SENSITIVE_FILES)

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM approval_gates WHERE run_id = :r "
                 "AND approval_type = 'memory_conflict'"),
            {"r": run_id},
        ).scalar()
    assert count == 1


# --------------------------------------------------------------------------- #
# Route endpoints (sync; TestClient)
# --------------------------------------------------------------------------- #

def test_approve_endpoint_decides_gate_and_sets_run_status(repo, tracked):
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    # Block via the sync policy (creates the pending gate + sets run status).
    assert _apply_db_memory_conflict_policy(run_id, project_id, str(repo), DB_SENSITIVE_FILES) is not None

    resp = client.post(f"/runs/{run_id}/memory-conflict/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == RunStatus.CHUNK_PLAN_APPROVED
    assert get_approved_memory_conflict_gate(run_id) is not None
    assert get_pending_memory_conflict_gate(run_id) is None


def test_reject_endpoint_rejects_run(repo, tracked):
    clear_all_events_for_tests()
    _postgres_repo(repo)
    project_id = _make_project(repo, tracked)
    _add_db_fact(project_id, "Project uses MongoDB.")
    run_id = _make_run(project_id, tracked, DB_SENSITIVE_FILES)

    assert _apply_db_memory_conflict_policy(run_id, project_id, str(repo), DB_SENSITIVE_FILES) is not None

    resp = client.post(f"/runs/{run_id}/memory-conflict/reject", json={"reason": "stale"})
    assert resp.status_code == 200
    assert resp.json()["status"] == RunStatus.REJECTED

    with engine.connect() as conn:
        run_status = conn.execute(
            text("SELECT status FROM pipeline_runs WHERE id = :r"), {"r": run_id},
        ).scalar()
    assert run_status == "rejected"


def test_approve_endpoint_404_when_run_missing():
    resp = client.post(f"/runs/{uuid.uuid4()}/memory-conflict/approve")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Pure decision-core matrix
# --------------------------------------------------------------------------- #

def _entry(memory="mongodb", repo_value="postgresql"):
    return ConflictEntry(
        fact_id="f1", memory_value=memory, repo_value=repo_value,
        evidence_path="requirements.txt", evidence_excerpt="postgres", status="active",
    )


def _report(conflicts=(), ambiguous=False, signal="postgresql"):
    return ConflictReport(
        repo_db_signal=signal, ambiguous=ambiguous, repo_path_missing=False,
        checked_count=len(conflicts), conflicts=tuple(conflicts), matches=(),
        skipped=(), multi_engine=(), warnings=(),
    )


def test_decision_blocks_on_clear_conflict_sensitive_no_override():
    report = _report(conflicts=(_entry(),))
    decision = _db_conflict_block_decision(report, DB_SENSITIVE_FILES, approved_signature=None)
    assert decision is not None
    summary, signature = decision
    assert signature == _conflict_signature(report)
    assert "MongoDB" in summary and "PostgreSQL" in summary


@pytest.mark.parametrize("report, files, approved, why", [
    (_report(conflicts=()), DB_SENSITIVE_FILES, None, "no conflict"),
    (_report(conflicts=(_entry(),), ambiguous=True, signal=None), DB_SENSITIVE_FILES, None, "ambiguous"),
    (_report(conflicts=(_entry(),), signal=None), DB_SENSITIVE_FILES, None, "unknown signal"),
    (_report(conflicts=(_entry(),)), NON_SENSITIVE_FILES, None, "not sensitive"),
])
def test_decision_returns_none_when_any_condition_fails(report, files, approved, why):
    assert _db_conflict_block_decision(report, files, approved_signature=approved) is None, why


def test_decision_honors_matching_approved_signature():
    report = _report(conflicts=(_entry(),))
    signature = _conflict_signature(report)
    assert _db_conflict_block_decision(report, DB_SENSITIVE_FILES, approved_signature=signature) is None
    # A non-matching approved signature does not suppress the block.
    assert _db_conflict_block_decision(report, DB_SENSITIVE_FILES, approved_signature="other") is not None
