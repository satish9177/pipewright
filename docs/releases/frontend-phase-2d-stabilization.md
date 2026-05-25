# Frontend Phase 2D Stabilization Release Notes

Phase: Frontend stabilization after Phase 2D backend hardening

## Summary

This frontend stabilization pass aligned the React UI with the current
chunked-run backend flow and made the MVP operator experience usable end to
end: project setup, chunked run creation, chunk plan review, chunk execution,
high-risk approvals, final approval, push/PR, status visibility, and live log
readability.

No backend behavior, API routes, database schema, WebSocket payloads, or
pipeline execution semantics were changed by this frontend work.

## Completed Frontend Items

- FE-0: Audited frontend API usage, response shapes, missing backend feature
  support, and design gaps.
- FE-1: Aligned frontend API client types and helpers with current backend
  routes.
- FE-2: Added RunDetail chunk plan review and plan approve/reject UI.
- FE-3: Added chunk execution, resume, chunk status, and high-risk chunk
  approve/reject UI.
- FE-4: Added final approval and push/create PR UI.
- FE-4 fix: Made final approval gate-aware so buttons are disabled when no
  pending final gate exists.
- FE-5: Switched ProjectDashboard new work creation to chunked runs.
- FE-6: Expanded status badges and classified Approval Queue entries.
- FE-7: Added project edit and GitHub credential settings UI.
- FE-8: Polished RunDetail, ProjectDashboard, Projects, and Live Log
  readability.

## API Alignment Improvements

- Project responses no longer expect or display `github_token`.
- Project UI uses `has_github_token` for token configured state.
- `ProjectCreateRequest` and `ProjectUpdateRequest` may still send
  `github_token` when entered by the user.
- The default ProjectDashboard run creation flow now uses
  `POST /runs/chunked`.
- RunDetail uses current chunk, final approval, and push/PR route helpers from
  `frontend/src/api/client.ts`.
- WebSocket live logs continue to use `/ws/runs/{run_id}/events`; polling
  remains the source of truth for persisted run and chunk state.

## RunDetail Improvements

- Run summary section shows current status, step, chunk counts, and feature
  description.
- Chunk plan panel displays plan status, feature description, total chunks,
  current chunk, and chunk definitions.
- Chunk rows show status, risk, expected files, dependencies, token estimate,
  human review requirement, rationale, completion summary, and errors when
  available.
- Chunk plan approve/reject actions are available only when the plan is awaiting
  approval.
- Execute and resume controls are available after plan approval.
- High-risk chunk approve/reject controls appear for chunks awaiting chunk
  approval.

## Approval and PR Flow Improvements

- Final approval panel appears when a run is awaiting final approval.
- Final approval approve/reject buttons require a matching pending final gate:
  `approval_type = "final"`, `chunk_number = 0`, and `status = "pending"`.
- Push PR panel appears for final-approved, pushing, push-failed, or existing
  PR data states.
- PR panel shows branch name, PR number, PR URL, push errors, and retry affordance
  after `push_failed`.
- Approval Queue classifies pending gates as Chunk Approval, Final Approval, or
  Legacy/Pre-merge Approval.

## GitHub Credential UI Safety

- Project settings can update `github_owner`, `github_repo`,
  `github_base_branch`, and `github_token`.
- Stored GitHub tokens are never displayed.
- Token input is always blank.
- Leaving the token field blank preserves the existing stored token.
- `github_token` is sent in PATCH only when the user enters a non-empty value.
- Project and PR panels warn when token/owner/repo configuration appears
  incomplete.

## Live Log and Design Polish

- Live Log has clearer connection state, timestamp, event label, message, and
  run/chunk source tags.
- Live Log has a compact scrollable panel and an explicit no-events empty state.
- RunDetail is organized into run summary, chunk lifecycle, final approval/PR,
  and timeline sections.
- ProjectDashboard has clearer settings, GitHub token warnings, feature request
  feedback, and no-runs empty state.
- Projects list distinguishes connected GitHub repos from repos that still need
  a token.
- Status badges now cover current backend run, chunk, approval, and push states,
  with unknown statuses rendered safely as neutral raw labels.

## Known Remaining Frontend Limitations

- Completion Summary is still shown as raw JSON in some places.
- No full mobile-first responsive pass has been completed.
- There is no durable frontend event history beyond current backend data and
  in-memory live-log replay.
- Approval Queue still depends on the backend gate data shape and uses the
  existing gate approve/reject endpoints.
- The current design polish is good enough for MVP use, but it is not a final
  product-grade design system.
- Frontend API and WebSocket base URLs are still local-development oriented.

## Validation Commands

Run from `frontend/`:

```powershell
npm.cmd run build
npx.cmd tsc --noEmit
```

Optional backend unit validation from repo root:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit
```
