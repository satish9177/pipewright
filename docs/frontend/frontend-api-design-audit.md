# Frontend API and Design Audit

Phase: 2D-FE-0

This audit compares the current React frontend with the Phase 2D backend route
surface and the current Claude-inspired visual direction. It is documentation
only. No backend behavior or frontend implementation was changed in this step.

## FE-1 Update

Phase 2D-FE-1 aligned `frontend/src/api/client.ts` with the current backend
routes and response shapes. The frontend now has typed helpers for chunked run
creation, chunk plan approval/rejection, chunk execution/resume, per-chunk
approval/rejection, final approval/rejection, and push/PR. WebSocket live logs
already exist and remain scoped to the EventLog; polling remains the source of
truth for run/chunk status.

## FE-2 Update

Phase 2D-FE-2 added a focused chunk plan review panel to `RunDetailPage`.
Run detail now fetches chunk plan data through `runsApi.getRunChunks`, displays
the plan status, feature description, total chunks, current chunk number, and
chunk definitions, and wires plan approval/rejection to
`runsApi.approveChunkPlan` and `runsApi.rejectChunkPlan`.

Execute/resume controls, high-risk per-chunk approval UI, final approval, and
push/PR actions remain future frontend work.

## FE-3 Update

Phase 2D-FE-3 extended the RunDetail chunk plan panel with approved-plan
execution controls, resume controls, chunk status details, and high-risk
per-chunk approval/rejection actions. The UI now uses `runsApi.executeChunks`,
`runsApi.resumeChunks`, `runsApi.approveChunk`, and `runsApi.rejectChunk`, then
refetches run and chunk plan state after each successful action.

Final approval and push/PR controls remain future frontend work.

## Current Frontend API Functions and Backend Route Mapping

| Backend route | Frontend mapping | Status |
| --- | --- | --- |
| `GET /health` | No API helper or UI health check | Missing |
| `POST /projects` | `projectsApi.create` | Present |
| `GET /projects` | `projectsApi.list` | Present |
| `GET /projects/{project_id}` | `projectsApi.get` | Present |
| `PATCH /projects/{project_id}` | `projectsApi.update` | Present API helper, no edit UI found |
| `POST /run` | `runsApi.start` | Present, legacy single-run flow |
| `GET /runs` | `runsApi.list` | Present |
| `GET /runs/{run_id}` | `runsApi.get` | Present |
| `GET /gates` | `gatesApi.list` | Present |
| `GET /gates/{gate_id}` | `gatesApi.get` | Present API helper, not clearly used |
| `POST /gates/{gate_id}/approve` | `gatesApi.approve` | Present legacy approval flow |
| `POST /gates/{gate_id}/reject` | `gatesApi.reject` | Present legacy approval flow |
| `POST /runs/chunked` | `runsApi.createChunkedRun` | API helper present; UI flow missing |
| `GET /runs/{run_id}/chunks` | `runsApi.getRunChunks` | Present in RunDetail chunk plan panel |
| `POST /runs/{run_id}/chunks/approve` | `runsApi.approveChunkPlan` | Present in RunDetail chunk plan panel |
| `POST /runs/{run_id}/chunks/reject` | `runsApi.rejectChunkPlan` | Present in RunDetail chunk plan panel |
| `POST /runs/{run_id}/chunks/execute` | `runsApi.executeChunks` | Present in RunDetail chunk plan panel |
| `POST /runs/{run_id}/chunks/resume` | `runsApi.resumeChunks` | Present in RunDetail chunk plan panel |
| `POST /runs/{run_id}/chunks/{chunk_number}/approve` | `runsApi.approveChunk` | Present in RunDetail high-risk chunk controls |
| `POST /runs/{run_id}/chunks/{chunk_number}/reject` | `runsApi.rejectChunk` | Present in RunDetail high-risk chunk controls |
| `POST /runs/{run_id}/final-approval/approve` | `runsApi.approveFinalApproval` | API helper present; UI flow missing |
| `POST /runs/{run_id}/final-approval/reject` | `runsApi.rejectFinalApproval` | API helper present; UI flow missing |
| `POST /runs/{run_id}/push-pr` | `runsApi.pushPr` | API helper present; UI flow missing |
| `WS /ws/runs/{run_id}/events` | `useRunEvents` | Present and path matches backend |

## Mismatched or Outdated API Calls

- `ProjectDashboard` still starts work through `runsApi.start`, which calls the
  legacy `POST /run` endpoint. Current backend capability centers on chunked
  planning via `POST /runs/chunked`, chunk plan approval, chunk execution,
  final approval, and push/PR.
- `RunDetailPage` uses legacy gate endpoints for approvals. It does not use the
  chunk plan approval/rejection routes, per-chunk approval/rejection routes, or
  final approval routes.
- `ApprovalQueuePage` also uses only legacy gate approve/reject endpoints and
  cannot distinguish plan approval, chunk approval, and final approval actions.
- `projectsApi.delete` calls `DELETE /projects/{id}`. That route exists in the
  older AGENTS route inventory, but it is not in the current Phase 2D route
  verification list and is not shown in the current route scan. Treat it as
  stale until backend support is confirmed.
- There is no frontend API helper for `GET /health`, so the sidebar hardcoded
  `localhost:8001` indicator is not based on backend health.
- WebSocket path matches the backend: `/ws/runs/{run_id}/events`. The base URL
  is hardcoded to `ws://localhost:8001`, so deployment will need a configurable
  frontend API/WS base URL later.

## Mismatched or Outdated TypeScript Types

- `Project` should not include `github_token`. It currently does not, which is
  correct.
- Fixed in FE-1: `Project` includes `has_github_token: boolean`.
- Fixed in FE-1: stale `is_active` was removed from `Project`; backend project
  responses expose `status` and optional `updated_at`.
- `ProjectCreate` includes `github_token`, which is correct for create/update
  requests. Create/update forms should not expect `github_token` in responses;
  current code does not read it from responses.
- Fixed in FE-1: `PipelineRun` now aliases the broader `Run` type.
  Backend run rows
  can include fields such as `chunk_plan_status`, `total_chunks`,
  `current_chunk_number`, `pr_url`, `pr_number`, `branch_name`, `push_error`,
  and timestamps for push/PR.
- Fixed in FE-1: `ApprovalGate` now aliases the broader `Gate` type.
  Backend gates can
  include `step`, `plain_english_summary`, `chunk_number`, `approval_type`,
  `rejection_reason`, and `decided_at`.
- Fixed in FE-1: TypeScript types now exist for `ChunkPlanResponse`,
  `ChunkStatus`, `TriageResult`, chunk action responses, final approval
  responses, and push/PR responses.
- `RunEvent.kind` and `RunEvent.stage` are typed as broad strings. This builds,
  but it gives no compile-time help for known backend event kinds/stages.

## Missing UI Support for Backend Features

- No chunked run creation UI. The primary project dashboard still offers the
  legacy single-run submit path.
- Fixed in FE-2: `RunDetailPage` now shows chunk plan review details, including
  chunk definitions, dependency order, risk level, token estimates, files
  expected, and plan approve/reject controls.
- Fixed in FE-3: `RunDetailPage` now exposes chunk execution and resume
  controls after plan approval.
- Fixed in FE-3: `RunDetailPage` now exposes high-risk per-chunk approve/reject
  controls for chunks with `awaiting_chunk_approval` status.
- Remaining: high-risk chunk approval could still be enriched with changed
  files, checkpoint state, and rollback consequence once the backend exposes or
  links those details.
- No final approval action using `/runs/{run_id}/final-approval/approve` or
  `/runs/{run_id}/final-approval/reject`.
- No push/create PR control using `/runs/{run_id}/push-pr`.
- No PR result display for `pr_url`, `pr_number`, `branch_name`, or
  `push_error`.
- No project edit screen for adding/updating GitHub credentials after project
  creation, even though `projectsApi.update` exists.
- No token configured indicator based on `has_github_token`.
- No explicit lock-conflict UI for HTTP 409 responses from repo-mutating routes.
- No request validation feedback polish for 422 errors from Pydantic limits.

## Status Coverage Gaps

`RunStatusBadge` explicitly styles only:

- `running`
- `paused`
- `complete`
- `failed`
- `rejected`

Current backend statuses also include:

- `started`
- `running_chunks`
- `awaiting_chunk_plan_approval`
- `chunk_plan_approved`
- `awaiting_chunk_approval`
- `chunk_approved`
- `awaiting_final_approval`
- `final_approved`
- `final_rejected`
- `pushing`
- `push_failed`

Chunk and plan statuses that need UI treatment:

- `pending`
- `running`
- `completed`
- `failed`
- `rejected`
- `awaiting_chunk_approval`
- `awaiting_approval`
- `approved`
- `none`

The fallback `status.toUpperCase()` prevents crashes, but important states do
not receive useful colors, labels, or action affordances.

`RunDetailPage` polling only continues for `running` and `paused`. It may stop
polling too early for chunked statuses such as `running_chunks`, `pushing`,
`awaiting_chunk_plan_approval`, or `awaiting_final_approval`.

## Design and UX Gaps

- The app has a promising visual direction, but implementation is inconsistent:
  some pages use shadcn components while others use large inline style blocks.
- Several visible strings show encoding artifacts where separator dots, dashes,
  and arrows should appear. These should be cleaned up with plain ASCII or
  correct UTF-8 handling.
- Layout hierarchy is thin on `RunDetailPage`; chunk plan, execution state,
  approvals, logs, and PR state need distinct sections with clear order.
- Approval actions are not specific enough. Users should know whether they are
  approving a plan, a high-risk chunk commit, final merge readiness, or legacy
  gate.
- Live Log is functional but dense. It needs stronger visual grouping by stage,
  severity, chunk number, and terminal events.
- Project setup form needs clearer GitHub credential state: not configured,
  token stored, owner/repo missing, ready for PR.
- Empty, loading, and error states exist in places but are not consistent across
  projects, runs, approvals, and logs.
- Long paths, test commands, feature descriptions, and event messages need
  stronger responsive treatment.
- Sidebar label `Runs` links to `/projects`, which is conceptually confusing.
- There is no deployment-aware backend connection state, despite configurable
  backend CORS/WS origins.

## Claude-design-inspired Recommendations

- Keep the restrained Pipewright palette, but make the system more coherent:
  use shared components for page headers, status badges, action bars, and empty
  states.
- Use a dense operator-console layout for run detail rather than a marketing
  page. Prioritize scanability and repeated operational actions.
- Rework `RunDetailPage` into sections:
  run summary, chunk plan, current action, chunk progress, live log, final
  approval, PR result.
- Use calm status color semantics:
  neutral for pending/awaiting, blue for running, amber for approval needed,
  green for approved/completed, red for failed/rejected.
- Add explicit primary actions only when the backend state permits them:
  approve plan, execute chunks, resume, approve chunk, reject chunk, final
  approve, push PR.
- Improve approval cards with risk level, chunk number, files expected/changed,
  and consequences of reject/rollback.
- Add a GitHub configuration indicator on project cards using
  `has_github_token`, `github_owner`, and `github_repo`.
- Make Live Log easier to parse with compact rows, severity marks, chunk tags,
  sticky connection status, and empty/replay states.
- Replace inline styling gradually with shared Tailwind/shadcn-compatible
  components rather than a broad one-shot rewrite.

## Recommended Implementation Order

1. Add status coverage:
   expand `RunStatusBadge`, add chunk/plan badge helpers, and update polling
   conditions for current backend statuses.
2. Add chunked run creation from `ProjectDashboard` using `POST /runs/chunked`.
   Keep the legacy `/run` flow only if intentionally still supported.
3. Continue the `RunDetailPage` chunked lifecycle work:
   final approval and push PR.
4. Update `ApprovalQueuePage` to classify approval types and route users to the
   right run/chunk action.
5. Add project edit/GitHub credential UI and display `has_github_token`.
6. Clean up encoding artifacts and consolidate shared visual components.
7. Polish Live Log readability and deployment-aware connection state.

## Validation Results

Commands run from `frontend/`:

```powershell
npm.cmd run build
npx.cmd tsc --noEmit
```

Results:

- `npm.cmd run build` passed.
- `npx.cmd tsc --noEmit` passed.

No tiny frontend code fix was required for TypeScript/build correctness during
this audit.
