# Phase 2D Release Notes - Production Readiness

## Executive Summary

Phase 2D is complete across backend and frontend. The backend hardening work
reduced security, concurrency, validation, logging, config, and test-risk
footguns. The frontend stabilization work aligned the React app with the
current chunked-run backend flow and made the MVP operator path usable from
project setup through chunk planning, execution, final approval, live logs, and
push/create PR.

Phase 2D did not change Pipewright's core execution model. It made the current
system safer, clearer, and more deployment-ready before GCP/local deployment
hardening or larger product capability work.

## Backend Improvements

- Refreshed agent and project context docs.
- Sanitized project API responses so `github_token` is never returned by
  project create/list/get/update endpoints.
- Added lightweight in-process project/repo execution locks around
  repo-mutating operations.
- Fixed planner/coder provider retry sleep behavior and removed async blocking
  sleeps from retry paths.
- Added focused Pydantic request validation for high-risk fields.
- Added centralized backend logging configuration.
- Audited `print()` usage and converted one safe print path.
- Added centralized plain string status constants without changing persisted
  values.
- Added a small run/chunk status service foundation.
- Split project CRUD routes out of `backend/main.py`.
- Centralized safe non-secret config, including CORS and WebSocket origins.
- Added a pre-deployment checklist.
- Marked live Gemini tests as API/integration only so unit tests stay mocked
  and fast.

## Frontend Improvements

- Audited frontend API usage, response shapes, feature gaps, and design gaps.
- Aligned frontend API client types and helpers with current backend routes.
- Added RunDetail chunk plan review and chunk plan approve/reject UI.
- Added chunk execution, resume, status display, and high-risk chunk
  approve/reject UI.
- Added final approval and push/create PR UI.
- Made final approval gate-aware so approve/reject buttons are disabled when no
  pending final gate exists.
- Switched ProjectDashboard new work creation to `POST /runs/chunked`.
- Expanded status badge coverage and classified Approval Queue entries.
- Added project edit and GitHub credential settings UI.
- Polished RunDetail, ProjectDashboard, Projects, and Live Log readability.
- Added frontend smoke checklist and frontend stabilization release notes.

## Security Improvements

- Project API responses omit `github_token` and expose only
  `has_github_token`.
- Frontend types and UI no longer expect project responses to include
  `github_token`.
- Project settings never display stored GitHub tokens.
- The GitHub token input is always blank and only sends a token in PATCH when a
  user enters a non-empty replacement.
- Internal project storage still supports GitHub token access for PR creation.
- Request validation caps high-risk inputs before expensive provider calls.
- CORS and WebSocket allowed origins are configurable with safe local defaults.

## Reliability Improvements

- Repo-mutating operations for the same project are protected by an in-process
  lock in the current single-instance MVP.
- Locks release on success and exception paths.
- Planner and coder retry behavior no longer blocks the event loop in async
  retry paths.
- Status updates have a shared service foundation and preserve existing event
  publishing behavior.
- Event publishing remains best-effort and does not fail the main pipeline.
- RunDetail now refetches run, chunk plan, and gate data after relevant user
  actions.

## UX Improvements

- ProjectDashboard creates chunked runs by default and navigates directly to
  RunDetail.
- RunDetail is organized around run summary, chunk plan/execution, final
  approval/PR, and timeline sections.
- Chunk plan and chunk status details are visible before and during execution.
- High-risk chunk approval, final approval, and PR push flows are available in
  the UI.
- Approval Queue distinguishes Chunk Approval, Final Approval, and
  Legacy/Pre-merge Approval.
- Status badges cover current backend run, chunk, approval, and push states.
- Live Log has clearer timestamps, source tags, event labels, empty state, and
  connection state.
- Projects and ProjectDashboard show clearer GitHub configuration/token state.

## Testing and Validation Improvements

- Added backend regression tests for project response token redaction.
- Added project/repo lock tests, including release-on-exception coverage.
- Added planner/coder retry tests for 429/rate-limit behavior.
- Added request validation tests for project, run, chunk, and rejection inputs.
- Added logging config, config parsing, status constants, and status service
  tests.
- Marked live Gemini tests as API-only to keep unit runs fast and local.
- Frontend build and TypeScript validation were run after each frontend
  implementation chunk.
- Added manual frontend smoke checklist for the chunked-run UI flow.

## Manual Smoke Result Summary

Manual smoke coverage is documented in
`docs/frontend/frontend-smoke-checklist.md` and
`docs/ops/pre-deployment-checklist.md`.

The intended smoke path now covers:

- backend/frontend startup
- project creation and token response safety
- project GitHub configuration
- chunked run creation
- chunk plan review and approval/rejection
- chunk execution and resume
- Live Log event visibility
- high-risk chunk approval/rejection
- final approval/rejection
- push/create PR and PR URL display
- status badge readability
- Approval Queue classification
- frontend build and typecheck

## Validation Commands

Backend unit tests:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit
```

Frontend build and typecheck:

```powershell
cd frontend
npm.cmd run build
npx.cmd tsc --noEmit
```

Live/API tests call Gemini and should be run intentionally:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m api -s
```

## Known Remaining Limitations

- In-process repo locks protect only a single API process/instance.
- SQLite/local DB remains local and single-instance oriented.
- The in-memory event bus and Live Log buffers are non-durable.
- GitHub tokens are still stored internally and are not encrypted yet, although
  they are no longer returned by project APIs.
- Raw SQL repository layer is not done.
- Alembic is not introduced yet.
- Worker queue is not introduced yet.
- Full print-to-logger migration is incomplete.
- The broader route split is incomplete; run and gate routes still need future
  route-module cleanup.
- Completion Summary still renders raw JSON in some UI places.
- No full mobile-first responsive pass has been completed.
- The current frontend polish is MVP-grade, not a final product-grade design
  system.
- Frontend API and WebSocket base URLs are still local-development oriented.

## Recommended Next Phase

Recommended next phase: Phase 2E - Deployment Readiness / GCP MVP.

Phase 2E should come before Phase 3 unless there is a strong product reason to
jump ahead. Pipewright now has the backend hardening and frontend operator flow
needed to validate deployment shape, single-instance operation, environment
configuration, startup commands, smoke checks, observability, and failure
recovery in a deployment-like environment.

Phase 3 remains a good follow-up after deployment readiness. Good Phase 3
candidates include multi-LLM provider routing, Slack/email approvals, memory
improvements, or other product-facing workflow upgrades.
