# Phase 2D Release Notes - Production Readiness and Code Quality Hardening

## Phase Summary

Phase 2D hardened Pipewright after the Phase 2A through 2C capability work.
It focused on security, repo mutation safety, request validation, logging,
status consistency, route organization, config hygiene, deployment notes, and
test marker clarity.

This phase did not add major product capability. It reduced operational risk
before deployment and before larger product work resumes.

## Completed Items

- Refreshed agent context docs for the current Pipewright architecture.
- Sanitized project API responses so `github_token` is never returned by
  project create/list/get/update endpoints.
- Added in-process project/repo execution locks for repo-mutating operations.
- Fixed planner/coder provider retry sleep behavior so async flows do not use
  blocking sleeps.
- Added focused request validation for high-risk request payloads.
- Added centralized backend logging configuration.
- Audited `print()` usage and converted one safe print path.
- Added centralized plain string status constants.
- Added a centralized run/chunk status service foundation.
- Split project routes out of `backend/main.py`.
- Centralized safe non-secret config values, including CORS and WebSocket
  origins.
- Added a pre-deployment checklist.
- Marked live Gemini tests as API-only so unit runs remain mocked and fast.

## Security and Safety Improvements

- Project API responses remove `github_token` and expose only
  `has_github_token`.
- Internal project storage still supports GitHub token access for PR creation.
- Project/repo locks reject concurrent repo-mutating operations for the same
  project in a single API process.
- Request validation caps large or invalid input fields before planner/coder
  work begins.
- WebSocket and CORS origins can now be configured through environment
  variables while preserving local defaults.
- Live Gemini tests are clearly separated from unit tests to reduce accidental
  external calls.

## Reliability Improvements

- Planner retry handling now uses async sleep in async flow.
- Coder retry handling cannot fail due to a missing sleep import path.
- Repo-mutating execution, resume, approval rollback, and push/PR paths are
  protected by lightweight project locks.
- Run/chunk status updates have a shared service path for future consistency.
- Event publishing around status updates remains best-effort and does not fail
  the pipeline.

## Code Quality Improvements

- `backend/core/logging_config.py` centralizes backend logging setup.
- `backend/core/statuses.py` centralizes existing status string values without
  changing persisted/API values.
- `backend/core/status_service.py` provides focused helpers for run/chunk
  status updates.
- `backend/core/config.py` centralizes safe non-secret app config.
- `backend/routes/projects.py` owns project CRUD routes, reducing `main.py`
  surface area.
- `docs/ops/print-audit.md` documents current print usage and future migration
  candidates.

## Testing Improvements

- Added regression tests for project response token redaction.
- Added tests for project/repo lock behavior and release-on-exception paths.
- Added planner/coder retry tests for rate-limit behavior.
- Added request validation tests for project, run, chunk, and rejection inputs.
- Added logging config idempotency and log-level tests.
- Added status constants and status service tests.
- Added config parsing tests.
- Marked live planner/coder Gemini tests as `api` only, leaving mocked retry
  tests as `unit`.

## Deployment Readiness Notes

- Use the pre-deployment checklist in
  `docs/ops/pre-deployment-checklist.md`.
- Required provider secret: `GEMINI_API_KEY`.
- Safe config values include `APP_ENV`, `APP_NAME`, `LOG_LEVEL`,
  `CORS_ALLOWED_ORIGINS`, and `WS_ALLOWED_ORIGINS`.
- Project GitHub credentials are project-level values set through the project
  API, not global environment variables.
- Run Pipewright on port `8001` for local pipeline work.
- Avoid `uvicorn --reload` during active pipeline runs.

## Final Validation Commands

Backend unit tests:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit
```

Frontend validation is optional unless frontend code changed:

```powershell
cd frontend
npm.cmd run build
npx.cmd tsc --noEmit
```

Live/API tests call external providers and should be run intentionally:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m api -s
```

## Known Remaining Limitations

- The project/repo lock is in-process only and protects a single API process.
- SQLite is still local and single-instance oriented.
- The event bus and Live Log buffer are in-memory and non-durable.
- Project GitHub tokens are still stored internally and are not encrypted yet,
  although they are no longer returned by project APIs.
- The broader route split is incomplete; run and gate routes still live in
  `main.py`.
- A raw SQL repository layer is not done.
- Alembic is not introduced yet.
- A worker queue is not introduced yet.
- Full print-to-logger migration is not complete.

## Recommended Next Phase

Recommended options:

- A. GCP/local deployment hardening: Cloud Run shape, single-instance config,
  deployment smoke tests, observability, and operational docs.
- B. Phase 2E deeper backend cleanup: route split completion, repository layer,
  error mapping, test helper deduplication, and checkpoint naming cleanup.
- C. Phase 3 product capability: multi-LLM provider routing, Slack/email
  approvals, memory improvements, or other product-facing workflow upgrades.

Recommended first choice: GCP/local deployment hardening, because Phase 2D was
explicitly aimed at making the current system safer to deploy before expanding
product capability.
