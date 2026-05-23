# Pipewright Console — UI Kit

Proposed Phase 2 React web UI for Pipewright. Interactive click-through demo. Designed against the existing backend's data model (no API calls — uses mock data shaped like the real Pydantic contracts).

## Files

| File                  | Purpose                                                        |
| --------------------- | -------------------------------------------------------------- |
| `index.html`          | Mount point. Loads React, Babel, Lucide, and all JSX modules.  |
| `data.js`             | Mock data shaped like `pipeline_runs`, `approval_gates`, `memory_facts`. |
| `components.jsx`      | Atomic primitives — `Button`, `Tag`, `StatusPill`, `RiskBadge`, `Mono`, `Eyebrow`, `Card`, `Rule`. |
| `AppShell.jsx`        | Fixed sidebar + top bar + content slot. Keyboard-friendly nav. |
| `RunsList.jsx`        | Main dashboard — pending-gates banner, filters, runs table.    |
| `RunDetail.jsx`       | Pipeline-stages strip, plan/files/tests panels, terminal log.  |
| `GateInspector.jsx`   | The marquee approval screen — tabbed diff/summary/plan/log, sticky decision bar, reject-with-reason modal. |
| `Memory.jsx`          | Hard-fact editor backed by `memory_facts`.                     |
| `StartRun.jsx`        | Launch form — wraps `POST /run`.                               |
| `app.jsx`             | Root — simple in-memory routing across the five screens.       |

## Screens demonstrated

1. **Runs** — list view, opens detail on row click.
2. **Run detail** — pipeline stages, plan/files/tests cards, terminal log.
3. **Approval queue** — list of paused runs needing decision.
4. **Gate inspector** — full diff, summary, plan, log; approve / reject with reason. The single most important screen — Pipewright's core promise is "never autonomous, human always in control," and this is where that promise gets enforced.
5. **Memory** — hard-fact list with stale flags.
6. **Start a run** — minimal launch form.

## Click-through paths to verify

- Runs → click any row → **Run detail**.
- Runs → click **Review oldest →** banner → **Gate inspector** (paused run).
- Gate inspector → **Approve and merge** → row updates to COMPLETE in the runs list.
- Gate inspector → **Reject** → modal opens → enter reason → confirm → row updates to REJECTED.
- Top bar → **Start a run** → minimal form, validates length, adds a synthetic paused run.

## What this kit does NOT include

- Authentication (Pipewright is single-user / CLI today).
- Real API wiring (a real build would call `/runs`, `/gates/{id}/approve`, etc.).
- Checkpoint browsing / rollback UI (backend supports it, design is open).
- Live log streaming (the log here is a static dump from the gate fixture).

## Where the design came from

Every value, label, and contract field in this UI maps to something concrete in the Pipewright backend at commit `22e3e7ff`:

- Run status values (`running`, `paused`, `complete`, `failed`, `rejected`) → `backend/db/schema.sql` `pipeline_runs.status`
- Gate states (`pending`, `approved`, `rejected`, `timeout`) → `approval_gates.status`
- Pipeline stages → `backend/pipeline/orchestrator.py`
- Risk levels → `ApprovalRequest.risk_level` in `backend/models/handoff.py`
- Terminal log tags & casing → `[PIPELINE]`, `[APPROVAL]`, etc. throughout the backend (Windows-safe ASCII rule from `AGENTS.md`)

Iterating? Cross-reference the source: <https://github.com/satish9177/pipewright>.
