# Phase 2D - Production Readiness + Code Quality Hardening

## Why Phase 2D Exists

Phase 2A through 2C made Pipewright capable: repo indexing, chunked execution,
resume/recovery, approvals, safe local Git, PR creation, and live logs are in
place. Phase 2D exists to harden that system before adding larger product
features. The goal is safer operations, clearer code paths, less duplicated
state handling, and fewer production footguns.

Phase 2D should not add major new product capability. It should reduce risk.

## Completion Note

Phase 2D is complete. The final release notes are in
`docs/releases/phase-2d-production-readiness.md`.

## P0 Priorities

1. Stop returning `github_token` from project API responses.
   - Keep internal access where needed for push/PR.
   - Redact or omit the token in all API responses and frontend types.
   - Add regression tests proving the token is not exposed.

2. Add project/repo-level execution locking.
   - Prevent two runs from mutating the same target repo at once.
   - Fail or reject concurrent execution clearly.
   - Do not rely on WebSocket state or frontend behavior for locking.

3. Fix planner/coder retry sleep bugs.
   - Ensure async code uses `await asyncio.sleep()`.
   - Avoid blocking the event loop.
   - Add focused retry tests.

4. Add request validation for large inputs.
   - Cap feature descriptions and rejection reasons.
   - Return clear 400-level errors.
   - Avoid sending oversized prompts to Gemini.

## P1 Priorities

1. Logging foundation.
   - Introduce a small logging convention or wrapper.
   - Keep Windows-safe ASCII output.

2. Print audit.
   - Inventory existing `print()` calls.
   - Convert only where useful and safe.

3. Status constants/enums.
   - Centralize run, chunk, approval, and gate status strings.
   - Do not rename statuses during the first pass.

4. Centralized run/chunk status service.
   - Stop scattering raw status updates.
   - Keep event publishing best-effort.

5. Split `main.py` routes.
   - Move legacy project/run/gate routes into route modules.
   - Preserve public API paths.

6. Config hygiene.
   - Review settings naming and defaults.
   - Do not move secrets into committed files.

7. WebSocket origin config.
   - Replace hardcoded origins with settings.
   - Keep local defaults for development.

8. Pre-deployment checklist.
   - Document required env vars, ports, GitHub config, test commands, and smoke checks.

## P2 Priorities

1. Repository layer for raw SQL.
2. Checkpoint semantic cleanup.
3. Route error mapping.
4. Test helper deduplication.
5. Gradual orchestrator extraction.
6. Worker queue later.
7. Alembic later.
8. Approval audit table later.

## Recommended Order

1. P0 token redaction.
2. P0 execution locking.
3. P0 retry sleep fixes.
4. P0 request size validation.
5. P1 logging foundation and print audit.
6. P1 status constants and centralized status service.
7. P1 route split and config hygiene.
8. P1 WebSocket origin config and deployment checklist.
9. P2 cleanup work only after P0/P1 are stable.

## What Not To Do Yet

- Do not add a worker queue until execution locking and status services are stable.
- Do not introduce Alembic until schema churn is understood.
- Do not redesign chunk execution or approvals during hardening tasks.
- Do not replace polling or WebSockets.
- Do not add new frontend product features.
- Do not add semantic memory or multi-agent routing.
- Do not merge broad refactors with security fixes.
- Do not change public API behavior unless the task explicitly requires it.
