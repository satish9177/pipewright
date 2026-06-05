"""
test_async_execution_responsiveness.py
Tests for #32C: blocking patch/test work is offloaded off the asyncio event
loop so the API/UI stays responsive during a long run, WITHOUT weakening the
per-project repo lock (one active mutating operation per project).

No real AI calls, no real GitHub, no push. Reuses the chunked-orchestrator test
harness for planner/coder/patch/git fakes and overrides only `run_tests` with a
deliberately blocking fake.
"""

import asyncio
import threading
import time

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.pipeline import chunked_orchestrator
from backend.pipeline.run_locks import ProjectRepoLockError, is_project_locked
from backend.tests.test_chunked_orchestrator import (
    create_run,
    make_test_result,
    patch_git_preflight,
    patch_success_pipeline,
)

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


async def test_blocking_test_run_does_not_freeze_event_loop(
    monkeypatch, tmp_repo, tracked_runs
):
    """
    A blocking, synchronous run_tests must NOT freeze the event loop. We run a
    lightweight ticker coroutine concurrently and assert it keeps advancing
    *while* the blocking test call is in flight. If run_tests still ran on the
    loop (pre-#32C), the ticker could not advance during that window.
    """
    run_id, _project = create_run(tmp_repo, tracked_runs)
    patch_git_preflight(monkeypatch)
    patch_success_pipeline(monkeypatch, run_id)

    counter = {"ticks": 0}
    observed = {}

    def blocking_tests(patch, run_id, chunk_number=0):
        # Runs in the to_thread worker. Sample the loop-driven ticker before and
        # after a real (blocking) sleep; a responsive loop advances it meanwhile.
        observed["start"] = counter["ticks"]
        time.sleep(0.25)
        observed["end"] = counter["ticks"]
        return make_test_result(run_id, True)

    monkeypatch.setattr(chunked_orchestrator, "run_tests", blocking_tests)

    stop = asyncio.Event()

    async def ticker():
        while not stop.is_set():
            counter["ticks"] += 1
            await asyncio.sleep(0.005)

    ticker_task = asyncio.create_task(ticker())
    try:
        result = await chunked_orchestrator.execute_approved_chunks(run_id)
    finally:
        stop.set()
        await ticker_task

    # The chunk still executed normally (offload preserves behavior).
    assert result.get("completed_chunks") == 1
    # The loop ran the ticker while the blocking test call was offloaded.
    assert "start" in observed and "end" in observed
    assert observed["end"] - observed["start"] >= 3


async def test_project_lock_held_across_offloaded_blocking_work(
    monkeypatch, tmp_repo, tracked_runs
):
    """
    While one project operation is mid-flight inside the offloaded blocking test
    call, a second mutating operation for the SAME project must still hit the
    existing lock conflict. The offload must not move work outside the lock.
    """
    run_id, project = create_run(tmp_repo, tracked_runs)
    patch_git_preflight(monkeypatch)
    patch_success_pipeline(monkeypatch, run_id)

    entered = threading.Event()
    release = threading.Event()

    def blocking_tests(patch, run_id, chunk_number=0):
        # Signal we are inside the locked critical section, then hold until the
        # test releases us. This keeps the project lock held by the awaiting
        # coroutine the whole time.
        entered.set()
        release.wait(timeout=5)
        return make_test_result(run_id, True)

    monkeypatch.setattr(chunked_orchestrator, "run_tests", blocking_tests)

    first = asyncio.create_task(
        chunked_orchestrator.execute_approved_chunks(run_id)
    )
    try:
        # Wait until the first op is parked inside the offloaded blocking call.
        while not entered.is_set():
            await asyncio.sleep(0.01)

        # The lock is held and a second mutating op for the same project is
        # refused with the existing conflict behavior.
        assert is_project_locked(project["id"])
        with pytest.raises(ProjectRepoLockError):
            await chunked_orchestrator.execute_approved_chunks(run_id)
    finally:
        release.set()
        result = await first

    # First op completed normally and the lock was released afterward.
    assert result.get("completed_chunks") == 1
    assert not is_project_locked(project["id"])
