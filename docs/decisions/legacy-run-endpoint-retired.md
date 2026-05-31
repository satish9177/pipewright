# Decision: Retire the legacy `POST /run` endpoint

Status: Accepted (Phase 2F, PR #11A)

## Context

Pipewright historically exposed two implementation entry points:

- `POST /run` — the original single-shot pipeline
  (`planner → coder → patch → test → one approval → auto PR`).
- `POST /runs/chunked` — the hardened Phase 2 flow.

A read-only audit found that `POST /run` was still reachable over HTTP even
though the frontend no longer called it. The legacy path bypassed the safety
guards that the chunked flow was built to enforce:

- no chunk-plan approval,
- no `files_expected` scope-drift guard,
- no deterministic high-risk gating,
- no ambiguous-implementation guard,
- no final chunked approval step,
- auto-created a GitHub PR after a single approval.

That made `/run` a way to run code generation and open a PR with weaker human
oversight than the supported path — a safety inconsistency, not a feature.

## Decision

- `POST /run` is permanently disabled. It returns **HTTP 410 Gone** with
  `"Legacy run endpoint is disabled. Use /runs/chunked."` and starts no
  pipeline work (it creates no run row, so no patch/test/commit/PR path can be
  reached).
- `POST /runs/chunked` is the **only supported implementation path**. Its
  behavior is unchanged by this decision.
- The unused frontend helpers (`runsApi.start`, `LegacyRunStartResponse`) were
  removed since nothing referenced them.

The legacy orchestrator module (`backend/pipeline/orchestrator.py`) has been
deleted (PR #15D). It was no longer wired into the app entry point and nothing
in production imported it, so removing it eliminates a latent footgun: a future
import could otherwise have re-enabled the old single-shot bypass. The only
supported implementation path remains `POST /runs/chunked`.

## Consequences

- All implementation runs go through the hardened gates: chunk-plan approval,
  scope guard, high-risk gating, ambiguous-implementation guard, and final
  approval.
- PR creation behavior is unchanged and still happens only after final human
  approval. There is no auto-merge.
- Reason for the choice: preserve the Phase 2 safety guarantees by having a
  single, audited implementation path.

## Tests

- `test_legacy_run_endpoint_is_disabled` (`backend/tests/test_project_routes.py`)
  asserts 410, the exact message, and that no `pipeline_runs` row is created.
- `test_legacy_run_route_is_disabled_even_when_project_locked`
  (`backend/tests/test_run_locks.py`) asserts the disabled route no longer
  participates in repo locking.
