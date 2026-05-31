# Print Audit

Phase: 2D-1B

This audit records current backend `print()` usage after the logging foundation
was added. The goal is not to remove all prints yet. Many messages are useful
operator-visible pipeline output and should move gradually after status and
logging conventions are clearer.

## Converted Now

- `backend/main.py`
  - `Pipewright started.` was already converted in Phase 2D-1A to
    `logger.info("Pipewright started.")`.
- `backend/projects/project_store.py`
  - `[PROJECTS] Updated project | id=...` converted to `logger.info(...)`.
  - Reason: project CRUD logging is outside the core pipeline execution path,
    not part of approval prompts, WebSocket payloads, rollback behavior, or PR
    creation semantics.

## Keep For Now

These are operator-visible pipeline messages. Keep their current text and
`print()` behavior until a focused print conversion pass can preserve the run
experience end to end.

- `backend/pipeline/planner.py`
  - `[PLANNER]` stage progress, token usage, retry, and checkpoint messages.
- `backend/pipeline/coder.py`
  - `[CODER]` file-read warnings, provider calls, retry, checkpoint, and
    completion messages.
- `backend/pipeline/patch_applier.py`
  - `[PATCH]` path validation, backups, file application, diff, checkpoint, and
    rollback messages.
- `backend/pipeline/tester.py`
  - `[TESTER]` command, timing, result, rollback, timeout, and parse-warning
    messages.
- `backend/pipeline/chunked_orchestrator.py`
  - `[CHUNKED]` execution, resume, chunk approval, final approval, and failure
    messages.
- `backend/pipeline/approval_gate.py`
  - `[APPROVAL]` gate creation, CLI-style approval instructions, polling,
    timeout, approve, and reject messages.
- `backend/github/github_client.py`
  - `[GITHUB]` legacy PR creation progress and file operation messages.
- `backend/pipeline/chunk_store.py`
  - `[CHUNKS]` chunk plan creation, save, approve, and reject messages.
- `backend/pipeline/triage.py`
  - `[TRIAGE]` provider call, token usage, retry, validation, and completion
    messages.

## Future Conversion Candidates

These are good candidates for later conversion once logging levels and status
messages are centralized:

- `backend/db/database.py`
  - Database initialization CLI messages. Convert carefully because
    `python -m backend.db.database` is used as an operator command.
- `backend/checkpoint/checkpoint_store.py`
  - Checkpoint save and checkpoint load failure diagnostics. These should likely
    become `logger.info(...)` and `logger.warning(...)`.
- `backend/memory/memory_store.py`
  - Memory load/list stale failure diagnostics. These should likely become
    `logger.warning(...)`, but the current silent-fallback behavior should be
    reviewed first.
- `backend/repo/repo_indexer.py`
  - `[REPO_INDEXER]` scan progress and warnings. Convert after deciding whether
    indexing output should be operator-visible or debug-level.
- `backend/routes/ws_events.py`
  - `[WS] run event stream failed...` should likely become `logger.warning(...)`
    after confirming no tests or manual workflows rely on stdout.
- `backend/events/event_bus.py`
  - `[EVENT_BUS] publish failed, ignored...` should likely become
    `logger.warning(...)`, matching its best-effort semantics.
- `backend/pipeline/chunked_orchestrator.py`
  - `[EVENT_BUS] publish raised, ignored...` warnings can move with the event bus
    conversion.

## Test And Debug Only

- `backend/tests/conftest.py`
  - Test cleanup messages are pytest-only. They can remain as prints or be
    converted later with no product behavior impact.
- `backend/tests/test_chunked_orchestrator.py`
  - `print(...)` appears inside test fixture file contents, not as executable
    Pipewright logging.
- `backend/tests/test_local_git.py`
  - `print(...)` appears inside test fixture file contents, not as executable
    Pipewright logging.

## Notes For Later Passes

- Preserve plain ASCII output for Windows compatibility.
- Preserve existing stage prefixes such as `[PLANNER]`, `[CODER]`, `[PATCH]`,
  `[CHUNKED]`, `[TESTER]`, `[APPROVAL]`, and `[PIPELINE]` until a deliberate
  logging convention replaces them.
- Avoid broad mechanical conversion until operational docs and status constants
  are in place.
