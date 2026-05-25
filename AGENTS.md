# Pipewright Agent Context

## What Pipewright Is

Pipewright is an AI engineering pipeline orchestrator. It coordinates planning,
coding, patching, testing, review, approvals, local Git, GitHub PR creation, and
live run observability. It is not autonomous: humans approve plans, high-risk
chunks, final results, and merges.

## Current Architecture

Backend lives in `backend/`.

- `backend/main.py` - FastAPI app and legacy route entry point.
- `backend/routes/` - newer route modules, including chunked runs and WebSocket events.
- `backend/pipeline/` - orchestration, planner/coder/patch/test stages, chunk store, PR flow.
- `backend/checkpoint/` - checkpoint persistence and chunk-aware checkpoint loading.
- `backend/git/local_git.py` - safe local Git subprocess wrapper.
- `backend/repo/repo_indexer.py` - deterministic zero-AI repo indexing.
- `backend/events/` - process-local event bus and event schema.
- `backend/projects/` - project CRUD and active runtime project context.
- `backend/db/` - SQLite schema and idempotent migration helpers.
- `backend/tests/` - backend unit tests.

Frontend lives in `frontend/`.

- Vite/React/TypeScript app.
- `frontend/src/api/client.ts` - Axios API client and frontend API types.
- `frontend/src/pages/` - page components.
- `frontend/src/components/` - UI and app components.
- `frontend/src/hooks/useRunEvents.ts` - RunDetail WebSocket event hook.
- `frontend/src/components/EventLog.tsx` - RunDetail live log panel.

Runtime docs and helper scripts live in `docs/` and `scripts/`.

## Current Stack

- Backend: Python, FastAPI, synchronous SQLAlchemy, SQLite.
- Validation: Pydantic v2.
- Frontend: Vite, React, TypeScript, Tailwind, shadcn/ui.
- AI provider: Google Gemini via `GEMINI_API_KEY`.
- GitHub: PyGithub.
- WebSocket live logs: FastAPI WebSocket plus in-memory process-local event bus.
- Tests: Pytest for backend, TypeScript/Vite build for frontend.

## Completed Phases

Phase 2A - Repo indexing complete.

- Zero-AI deterministic repo indexing.
- `project_id` isolation in `file_index`.
- Path normalization, file classification, import extraction, token estimates.

Phase 2B - Chunked execution, approvals, Git, and PR complete.

- Chunk-safe checkpoints and rollback paths.
- Safe local Git helper.
- Chunk planning and persistence.
- Sequential chunk execution.
- Resume/recovery from chunk boundaries.
- Final approval gate.
- High-risk per-chunk approval before commit.
- Push final-approved Pipewright branch and create one GitHub PR.
- Stale `pipewright/*` branch safety guard.
- Operator docs and helper scripts.

Phase 2C - Live logs complete.

- In-memory backend event bus.
- Run/chunk status event publishing.
- Backend WebSocket route: `/ws/runs/{run_id}/events`.
- Frontend `useRunEvents` hook and RunDetail `EventLog` panel.
- Manual UI verified.
- Backend tests passed.
- Frontend build and TypeScript passed.

## Critical Engineering Rules

- Keep changes small, scoped, and behavior-preserving unless the task explicitly asks otherwise.
- Do not refactor unrelated code.
- Add regression tests for safety, security, rollback, approval, Git, or API exposure fixes.
- Do not change DB schema unless explicitly asked.
- SQLAlchemy usage is synchronous only.
- Do not touch chunk execution, approval, WebSocket, or PR behavior unless the task explicitly says so.
- Coder output must be applied through `patch_applier`; coder must not write directly to disk.
- Patch application must back up first and rollback on failed tests.
- Never use `git reset --hard`, force push, or branch deletion as automatic recovery.
- Never run Git operations outside the target repo; use `backend/git/local_git.py`.
- Never print or expose real API keys or GitHub tokens.
- Internal `project_store` may still load `github_token`; after Phase 2D-0A, API responses must not expose it.
- Tests share the app SQLite database. Test project repo paths must use `.pytest_tmp` so cleanup can remove them.
- Do not name Pydantic models starting with `Test*`; pytest may collect them.
- Use exact model names and explicit temperatures for AI calls. Do not use `"latest"` model names.
- Use plain ASCII in logs/docs where possible for Windows compatibility.

## Current Known Risks

- Project GitHub tokens are still stored internally and are not encrypted yet,
  though project APIs no longer return them.
- Project/repo execution locking is in-process only and does not protect
  multiple API processes or instances.
- SQLite is still local/single-instance oriented.
- Event bus and Live Log buffers are in-memory and non-durable.
- Logging still has many pipeline/operator-visible `print()` calls.
- `main.py` still owns run and gate routes.
- Raw SQL is spread across modules.
- Checkpoint naming is historical and needs semantic cleanup later.

## Phase 2D Priorities

Phase 2D is Production Readiness + Code Quality Hardening.

P0:

1. Stop returning `github_token` from project API responses.
2. Add project/repo-level execution locking.
3. Fix planner/coder retry sleep bugs.
4. Add request validation for large inputs.

P1:

1. Logging foundation.
2. Print audit.
3. Status constants/enums.
4. Centralized run/chunk status service.
5. Split `main.py` routes.
6. Config hygiene.
7. WebSocket origin config.
8. Pre-deployment checklist.

P2:

1. Repository layer for raw SQL.
2. Checkpoint semantic cleanup.
3. Route error mapping.
4. Test helper deduplication.
5. Gradual orchestrator extraction.
6. Worker queue later.
7. Alembic later.
8. Approval audit table later.

## Run Backend Tests

Run from repo root:

```powershell
venv\Scripts\python.exe -m backend.db.database
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit
venv\Scripts\python.exe -m pytest tests\test_repo_indexer.py -v
```

API tests may call Gemini and should be run intentionally:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m api -s
```

## Run Frontend Build

Run from `frontend/`.

PowerShell may block `npm.ps1`; use `npm.cmd` and `npx.cmd` when needed:

```powershell
cd frontend
npm.cmd run build
npx.cmd tsc --noEmit
```

## Codex Working Rules

- Read this file before making changes.
- Prefer `rg`/`rg --files` for search.
- Inspect the existing implementation before editing.
- Use `apply_patch` for manual edits.
- Do not overwrite user changes in a dirty worktree.
- Do not modify application code for documentation-only tasks.
- Do not add new dependencies unless the task explicitly asks.
- Do not add new architecture when a small local fix is sufficient.
- Preserve React Query polling unless a task explicitly replaces it.
- Preserve WebSocket live logs unless a task explicitly changes them.
- Preserve chunked execution, approvals, rollback, local Git, and PR semantics unless explicitly in scope.
- Keep docs practical for future coding agents.
