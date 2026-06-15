"""
Tests for the §23 row 7b PR-B plan-turn HTTP route.

POST /runs/{run_id}/plan-turns revises a chunk plan that is still awaiting
approval. It is dormant by default (PLAN_TURNS_ENABLED is false), enforces the
plan-turn cap, refuses every non-awaiting run state, hardens the final write
against an approval/rejection landing mid re-triage, and never approves or
executes anything — the revised plan still goes through the unchanged approval
route.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.core.statuses import ChunkPlanStatus, RunStatus
from backend.db.database import engine
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import policy
from backend.pipeline.chunk_store import create_chunked_run
from backend.pipeline.plan_version_store import (
    PLAN_VERSION_SOURCE_PLAN_TURN,
    list_plan_versions,
)
from backend.pipeline.run_turn_store import (
    RUN_TURN_TARGET_PLAN,
    list_run_turns,
    record_plan_turn,
)
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in ("run_turns", "plan_versions", "chunks"):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE run_id = :run_id"),
                    {"run_id": run_id},
                )
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :run_id"),
                {"run_id": run_id},
            )


@pytest.fixture()
def enable_plan_turns(monkeypatch):
    """Flip the default-off capability flag on for the in-band route tests."""
    monkeypatch.setattr(policy, "PLAN_TURNS_ENABLED", True)


def _make_project(tmp_repo):
    return create_project(
        name=f"Plan Turn Route Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def _chunk(number: int, title: str) -> ChunkDefinition:
    return ChunkDefinition(
        chunk_number=number,
        title=title,
        description=f"Do {title}.",
        files_expected=[],
        depends_on=[] if number == 1 else [number - 1],
        risk_level="high",
        token_estimate=100 * number,
        requires_human_review=True,
        rationale="Exact files require human confirmation.",
    )


def _triage(
    run_id: str,
    project_id: str,
    feature: str,
    titles: list[str],
) -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description=feature,
        complexity="easy" if len(titles) == 1 else "medium",
        total_chunks=len(titles),
        reasoning="Test plan.",
        chunks=[_chunk(index, title) for index, title in enumerate(titles, start=1)],
    )


def _create_awaiting_run(tmp_repo, tracked_runs):
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Update the dashboard",
        _triage(run_id, project["id"], "Update the dashboard", ["Initial chunk"]),
    )
    return run_id, project


def _load_run(run_id: str) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM pipeline_runs WHERE id = :run_id"),
            {"run_id": run_id},
        ).mappings().fetchone()
    return dict(row) if row is not None else {}


def _chunk_titles(run_id: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT title
            FROM chunks
            WHERE run_id = :run_id
            ORDER BY chunk_number
        """), {"run_id": run_id}).fetchall()
    return [row[0] for row in rows]


def _set_run_state(run_id: str, *, status: str, chunk_plan_status: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE pipeline_runs
            SET status = :status,
                current_step = :status,
                chunk_plan_status = :chunk_plan_status
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "status": status,
            "chunk_plan_status": chunk_plan_status,
        })


def _patch_two_chunk_triage(monkeypatch, captured: dict | None = None):
    async def fake_run_triage(run_id, project_id, feature_description):
        if captured is not None:
            captured["feature_description"] = feature_description
        return _triage(
            run_id,
            project_id,
            "temporary revision context",
            ["API chunk", "UI chunk"],
        )

    monkeypatch.setattr(
        "backend.pipeline.plan_turn_engine.run_triage",
        fake_run_triage,
    )


def _patch_producer_must_not_run(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise AssertionError("produce_next_plan_version must not be called")

    monkeypatch.setattr(
        "backend.routes.chunks.produce_next_plan_version",
        _boom,
    )


# --- 1. Flag off ----------------------------------------------------------

def test_flag_off_hides_route_and_skips_producer(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    monkeypatch.setattr(policy, "PLAN_TURNS_ENABLED", False)
    _patch_producer_must_not_run(monkeypatch)
    client = TestClient(app)

    response = client.post(
        f"/runs/{run_id}/plan-turns",
        json={"message": "Split chunk 1 into backend and frontend chunks"},
    )

    # Disabled: a valid request is hidden as a 404.
    assert response.status_code == 404
    assert list_plan_versions(run_id) and len(list_plan_versions(run_id)) == 1
    assert list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN) == []


def test_flag_off_with_invalid_body_is_422_not_404(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    """
    Honesty pin for the disabled route: it is not byte-for-byte identical to an
    unmounted path. Pydantic validates PlanTurnRequest before the capability
    gate runs, so a blank/missing body is rejected as 422 even while disabled —
    only a *valid* request is masked as a 404. Either way the producer never
    runs and no revision lands.
    """
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    monkeypatch.setattr(policy, "PLAN_TURNS_ENABLED", False)
    _patch_producer_must_not_run(monkeypatch)
    client = TestClient(app)

    blank = client.post(f"/runs/{run_id}/plan-turns", json={"message": "   "})
    missing = client.post(f"/runs/{run_id}/plan-turns", json={})

    assert blank.status_code == 422
    assert missing.status_code == 422
    assert len(list_plan_versions(run_id)) == 1
    assert list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN) == []


# --- 2/3/4. Message validation -> 422 -------------------------------------

@pytest.mark.parametrize(
    "message",
    [
        "",  # blank
        "   \n\t  ",  # whitespace only
        "x" * (policy.PLAN_TURN_MESSAGE_MAX_CHARS + 1),  # over cap
    ],
)
def test_invalid_message_is_422_and_skips_producer(
    monkeypatch,
    tmp_repo,
    tracked_runs,
    enable_plan_turns,
    message,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    _patch_producer_must_not_run(monkeypatch)
    client = TestClient(app)

    response = client.post(
        f"/runs/{run_id}/plan-turns",
        json={"message": message},
    )

    assert response.status_code == 422
    assert len(list_plan_versions(run_id)) == 1
    assert list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN) == []


# --- 5. Wrong status -> 409 ------------------------------------------------

@pytest.mark.parametrize(
    ("status", "chunk_plan_status"),
    [
        (RunStatus.CHUNK_PLAN_APPROVED, ChunkPlanStatus.APPROVED),
        (RunStatus.REJECTED, ChunkPlanStatus.REJECTED),
        (RunStatus.RUNNING_CHUNKS, ChunkPlanStatus.APPROVED),
        (RunStatus.COMPLETE, ChunkPlanStatus.APPROVED),
        # status wrong even though the plan-status column still says awaiting.
        (RunStatus.FAILED, ChunkPlanStatus.AWAITING_APPROVAL),
        (RunStatus.REPORT_READY, ChunkPlanStatus.NONE),
        (RunStatus.PLAN_READY, ChunkPlanStatus.NONE),
    ],
)
def test_wrong_status_is_409(
    monkeypatch,
    tmp_repo,
    tracked_runs,
    enable_plan_turns,
    status,
    chunk_plan_status,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    _set_run_state(run_id, status=status, chunk_plan_status=chunk_plan_status)

    async def fail_triage(*_args, **_kwargs):
        raise AssertionError("wrong-status plan turns must not run triage")

    monkeypatch.setattr(
        "backend.pipeline.plan_turn_engine.run_triage",
        fail_triage,
    )
    client = TestClient(app)

    response = client.post(
        f"/runs/{run_id}/plan-turns",
        json={"message": "Revise this plan."},
    )

    assert response.status_code == 409
    # No turn recorded and no new version on a wrong-status refusal.
    assert list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN) == []
    assert len(list_plan_versions(run_id)) == 1


def test_unknown_run_is_404(monkeypatch, enable_plan_turns):
    _patch_two_chunk_triage(monkeypatch)
    client = TestClient(app)

    response = client.post(
        f"/runs/{uuid.uuid4()}/plan-turns",
        json={"message": "Revise a run that does not exist."},
    )

    assert response.status_code == 404


# --- 6. Cap -> 409 ---------------------------------------------------------

def test_cap_rejects_sixth_turn(
    monkeypatch,
    tmp_repo,
    tracked_runs,
    enable_plan_turns,
):
    run_id, project = _create_awaiting_run(tmp_repo, tracked_runs)
    for index in range(policy.PLAN_TURN_CAP):
        record_plan_turn(
            run_id=run_id,
            project_id=project["id"],
            message=f"Plan turn {index + 1}",
        )

    async def fail_triage(*_args, **_kwargs):
        raise AssertionError("over-cap plan turn must not run triage")

    monkeypatch.setattr(
        "backend.pipeline.plan_turn_engine.run_triage",
        fail_triage,
    )
    client = TestClient(app)

    response = client.post(
        f"/runs/{run_id}/plan-turns",
        json={"message": "One more revision."},
    )

    assert response.status_code == 409
    # Copy must point the user at the decision they have to make instead.
    detail = response.json()["detail"].lower()
    assert "approve or reject" in detail
    # No v(N+1) lands; the prior turns stay at exactly the cap.
    assert len(list_plan_versions(run_id)) == 1
    assert len(list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN)) == (
        policy.PLAN_TURN_CAP
    )


# --- 7. Success ------------------------------------------------------------

def test_success_revises_plan_and_keeps_awaiting_approval(
    monkeypatch,
    tmp_repo,
    tracked_runs,
    enable_plan_turns,
):
    run_id, project = _create_awaiting_run(tmp_repo, tracked_runs)
    original_plan_json = _load_run(run_id)["chunk_plan"]
    captured: dict = {}
    _patch_two_chunk_triage(monkeypatch, captured)
    client = TestClient(app)

    response = client.post(
        f"/runs/{run_id}/plan-turns",
        json={"message": "Split API and UI work into separate chunks."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ok": True,
        "run_id": run_id,
        "plan_version": 2,
        "total_chunks": 2,
        "chunk_plan_status": ChunkPlanStatus.AWAITING_APPROVAL,
        "next_action": "approve_or_reject_plan",
    }

    # Producer ran with the human message + original request as context.
    assert "Split API and UI work into separate chunks." in (
        captured["feature_description"]
    )
    assert "Update the dashboard" in captured["feature_description"]

    # A v2 plan version landed, sourced as a plan turn.
    versions = list_plan_versions(run_id)
    assert len(versions) == 2
    assert versions[1]["version"] == 2
    assert versions[1]["source"] == PLAN_VERSION_SOURCE_PLAN_TURN

    # The live pointer + pending chunk rows were swapped to the new plan.
    run = _load_run(run_id)
    assert run["chunk_plan"] == versions[1]["triage_json"]
    assert run["chunk_plan"] != original_plan_json
    assert run["total_chunks"] == 2
    assert _chunk_titles(run_id) == ["API chunk", "UI chunk"]

    # No approval bypass: the run is still awaiting human approval.
    assert run["status"] == RunStatus.AWAITING_CHUNK_PLAN_APPROVAL
    assert run["chunk_plan_status"] == ChunkPlanStatus.AWAITING_APPROVAL
    assert len(list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN)) == 1


def test_revised_plan_still_requires_manual_approval(
    monkeypatch,
    tmp_repo,
    tracked_runs,
    enable_plan_turns,
):
    """A plan turn never auto-approves; the unchanged approve route still works."""
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    _patch_two_chunk_triage(monkeypatch)
    client = TestClient(app)

    turn = client.post(
        f"/runs/{run_id}/plan-turns",
        json={"message": "Split API and UI work into separate chunks."},
    )
    assert turn.status_code == 200
    # Still pending immediately after the revision.
    run = _load_run(run_id)
    assert run["status"] == RunStatus.AWAITING_CHUNK_PLAN_APPROVAL
    assert run["chunk_plan_status"] == ChunkPlanStatus.AWAITING_APPROVAL

    approve = client.post(f"/runs/{run_id}/chunks/approve")
    assert approve.status_code == 200
    decided = _load_run(run_id)
    assert decided["status"] == RunStatus.CHUNK_PLAN_APPROVED
    assert decided["chunk_plan_status"] == ChunkPlanStatus.APPROVED


# --- 8. TOCTOU / concurrency ----------------------------------------------

def test_approval_during_retriage_discards_plan_with_409(
    monkeypatch,
    tmp_repo,
    tracked_runs,
    enable_plan_turns,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    before_plan_json = _load_run(run_id)["chunk_plan"]
    before_titles = _chunk_titles(run_id)

    async def fake_run_triage(run_id, project_id, feature_description):
        # Simulate an approval committing while re-triage is in flight.
        _set_run_state(
            run_id,
            status=RunStatus.CHUNK_PLAN_APPROVED,
            chunk_plan_status=ChunkPlanStatus.APPROVED,
        )
        return _triage(run_id, project_id, feature_description, ["Too late"])

    monkeypatch.setattr(
        "backend.pipeline.plan_turn_engine.run_triage",
        fake_run_triage,
    )
    client = TestClient(app)

    response = client.post(
        f"/runs/{run_id}/plan-turns",
        json={"message": "Revise after approval landed."},
    )

    assert response.status_code == 409
    # No v2 appended; the live plan and chunk rows remain v1.
    assert len(list_plan_versions(run_id)) == 1
    assert _load_run(run_id)["chunk_plan"] == before_plan_json
    assert _chunk_titles(run_id) == before_titles
    # The append-only audit row from before re-triage is preserved (not deleted).
    assert len(list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN)) == 1
