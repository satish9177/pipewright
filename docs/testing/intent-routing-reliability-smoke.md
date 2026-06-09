# Intent Routing Reliability Smoke Checklist & Closeout (#42)

## Purpose

This document is the manual smoke checklist and closeout record for #42 Intent
Routing Reliability. It verifies that the user's **visible selected mode is the
source of truth** for how a run starts, that the deterministic classifier no
longer misreads scope constraints or anti-report meta as read-only, that
selecting implementation over a read-only/no-code request surfaces an honest
confirmation instead of silently guessing, and that every read-only / plan-only
/ implementation safety guarantee is preserved.

Design source of truth:
[`intent-routing-reliability.md`](../design/intent-routing-reliability.md).

Docs-only: this file changes no backend, frontend, schema, API, test, or runtime
behavior.

## Completed Phases (#42A–#42F)

- **#42A** — intent routing reliability audit/design doc.
- **#42B** — deterministic scope/negation classifier fix in
  `backend/pipeline/intent.py` (scope constraints and anti-report meta are no
  longer read as global read-only; an "implement but change nothing"
  contradiction becomes uncertain → clarification).
- **#42C** — backend/API `requested_mode` + `confirm_conflict` +
  `ModeConflictResponse` in `backend/routes/chunks.py`. `auto`/omitted keeps the
  classifier as router (backward compatible); a concrete mode is the source of
  truth.
- **#42D** — frontend always-visible mode selector and `requested_mode` API
  wiring in `ProjectDashboard.tsx` / `client.ts`.
- **#42E** — frontend conflict warning / confirm UI (Confirm run mode box with
  Continue / Switch / Cancel actions).
- **#42F** — this smoke checklist / closeout.

## What #42 Fixed

- **Explicit implementation requests are no longer misclassified as read-only.**
  The deterministic blocker previously substring-matched scope constraints
  ("do not modify or create any **other** files") and anti-report meta ("do not
  run as a **read-only** report") and routed real implementation requests to
  `REPORT_READY`. #42B distinguishes scope/meta from genuine global read-only.
- **The user can choose the start mode and that choice wins.** A visible selector
  offers Read-only report / Plan only / Implement with approval. The classifier
  is reduced to a suggestion/conflict signal for a concrete selection, and is the
  router only for `auto`/legacy clients.
- **Implementation-over-read-only is confirmed, never guessed.** When the user
  selects implementation but the text reads as read-only / a no-code
  contradiction, the backend returns `mode_conflict` (no run) and the UI asks for
  one explicit confirmation. A safer selection (report_only / plan_only) over
  implementation-like text is honored without a block.

## Manual Smoke Prerequisites

- Backend running locally with a configured LLM provider key for the role used by
  triage/report analysis (see project README / role-based LLM config). The #42B
  classifier paths exercised below are deterministic and do not require a key;
  the report/plan/chunk-plan generation steps do.
- A project registered in Pipewright pointing at a clean Git checkout on a normal
  branch (not a `pipewright/...` run branch, not detached HEAD), with a fresh repo
  index.
- Frontend dev server running and the project open on its dashboard.
- `pr_mode = local_only` is sufficient; #42 does not touch push/PR behavior.

## Backend Validation Commands

```powershell
# Deterministic intent classifier (#42B scope/negation matrix + helpers)
python -m pytest backend/tests/test_intent.py -q

# requested_mode / confirm_conflict / ModeConflictResponse routing (#42C)
python -m pytest backend/tests/test_chunk_routes_mode.py -q

# Existing chunk route behavior (old-client regression coverage)
python -m pytest backend/tests/test_chunk_routes.py -q

# Lint the touched backend files
python -m ruff check backend/pipeline/intent.py backend/routes/chunks.py
```

All three suites must pass and ruff must report no findings.

## Frontend Validation Commands

```powershell
cd frontend

# TypeScript + production build
npm.cmd run build

# Targeted eslint on the touched frontend files
npx.cmd eslint src/api/client.ts src/pages/ProjectDashboard.tsx
```

The build's pre-existing 500 kB chunk-size warning is benign and unrelated to
#42.

## Manual UI Smoke Matrix

For each row: open the project dashboard, set the **Mode** selector, type the
**Request**, click **Create Chunked Run**, and confirm the **Expected** outcome.

| # | Mode selected | Request | Expected |
|---|---|---|---|
| A | Read-only report | "Add a login endpoint to the API." | Navigates to a `report_ready` run. No chunk plan, no triage, no code change. |
| B | Plan only | "Add a login endpoint to the API." | Navigates to a `plan_ready` run. A plan is shown; no code change, no execution. |
| C | Implement with approval | "Add a GET /ping endpoint that returns status ok." | Chunk plan **awaiting approval**. Nothing executes until the plan is approved. |
| D | Implement with approval | "Implement add(a, b) in src/app.py but do not change any code." | **No run created.** "Confirm run mode" warning appears with `message`, Continue / Switch / Cancel actions. |
| E | (from D) click **Continue with Implement with approval** | — | Re-submits with `confirm_conflict=true`; chunk plan **awaiting approval**. No execution until plan approval. |
| F | (from D) click **Switch to Read-only report** | — | Selector visibly moves to Read-only; creates a `report_ready` run. No code change. |
| G | (from D) click **Cancel** | — | Warning clears. **No run created**, no request re-sent. |
| H | Implement with approval (or `auto` via API) | The two original bad examples below | Routes to **implementation / chunk plan**, not `report_ready` (#42B). |

### Stale-conflict clearing (verify during D–G)

- Editing the request text clears the conflict warning.
- Changing the Mode selector clears the conflict warning.
- Clicking Create Chunked Run starts a fresh submission (clears the prior
  conflict verdict).

### Original bad examples for row H

Bad example 1:

```
Create a small helper function for the calculator.

Only modify src/app.py and tests/test_app.py.
Do not modify or create any other files.
Add tests for the helper.
```

Bad example 2:

```
Implement this code change.

Create validate_number in src/app.py.
Create add(a, b) in src/app.py and make it use validate_number.
Add tests in tests/test_app.py.

Only modify:
- src/app.py
- tests/test_app.py

Do not create or modify any other files.
Do not run as a read-only report.
This is an implementation request.
```

Both must produce an implementation/chunk-plan outcome, never `report_ready`.
(For the pure-`auto`/old-client behavior, send these via
`POST /runs/chunked` with `requested_mode` omitted.)

## Safety Invariants (must remain true)

- **Read-only is truly read-only.** A report_only run (selected or classified)
  routes through `_create_read_only_run(REPORT_READY)`; the `_load_run_intent`
  gate keeps blocking all implementation actions. No code, tests, commits, or PR.
- **Plan-only makes no code changes.** A plan_only run is `PLAN_READY` behind the
  same gate; `create_chunked_run` (the execution path) is never reached.
- **Implementation still requires chunk-plan approval.** Selecting — or confirming
  a conflict for — implementation only creates the chunk plan awaiting approval.
  Nothing auto-executes.
- **`confirm_conflict` does not bypass any gate.** It acknowledges the warning and
  proceeds in the selected mode; chunk-plan approval, the specificity guard, scope
  guard, index-freshness checks, final approval, and PR safety are unchanged.
- **No ML / vector / sklearn / embedding runtime dependency** was added. The
  classifier remains deterministic rules + the existing LLM fallback.
- **No schema, memory, retry, or patch-recovery change.** `requested_mode`
  resolves to the existing `intent`; `confirm_conflict` is ephemeral (never
  stored).

## Known Limitations / Deferred

- **No classifier-suggestion endpoint.** The frontend default mode is a static
  "Implement with approval"; it does not yet call a backend suggestion. A
  classifier-suggested default is future work, not part of #42A–#42F.
- **No ML / sklearn runtime.** Per the #42A decision, runtime ML and vector search
  are deferred; the selector removes the pressure that motivated them.
- **#42G is optional.** A small, offline, pre-trained sklearn suggestion model
  (users never train) would only be built if suggestion quality is *measured* to
  be insufficient. It would improve suggestions only — never the safety authority.

## Closeout Criteria

#42 is closed out when all of the following hold:

- [ ] `test_intent.py`, `test_chunk_routes_mode.py`, and `test_chunk_routes.py`
      all pass.
- [ ] `ruff check` on the touched backend files reports no findings.
- [ ] `npm.cmd run build` succeeds (only the benign chunk-size warning).
- [ ] `eslint` on the touched frontend files is clean.
- [ ] The manual UI smoke matrix (rows A–H) passes, including stale-conflict
      clearing.
- [ ] This docs PR changes **no** backend, frontend, API, schema, package, test,
      or runtime files.
