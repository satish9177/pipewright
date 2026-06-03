# Pipewright Stabilization Closeout

## Purpose

This document closes the current stabilization cycle. It is the source of
truth for what was completed, manually verified, and intentionally deferred
before starting **patch failure recovery v2 (#26)**.

Patch failure recovery v2 must not begin until this closeout document is
merged. Use this document to confirm the stabilization scope is settled and
that no in-flight stabilization work remains open.

## Completed Stabilization Work

### 1. Memory M2 v1

- Project-scoped memory confirmed.
- Hard blockers for unsafe memory writes.
- Pending memory suggestions.
- Atomic approve / edit-approve / reject lifecycle.
- Deterministic run-outcome memory suggestions.
- Frontend generation/review UX.
- Role-specific advisory injection.
- Docs + smoke checklist.
- Rejected run-outcome suggestion regeneration bug fixed.

### 2. File-Scope Contract Hardening

- Explicit multi-file hard allowlists handled.
- Example fixed: "using only src/app.py and tests/test_app.py" preserves both
  files in `files_expected`.
- Forbidden/reference/preferred file mentions handled conservatively.
- Planner prose mismatch creates a `[SCOPE]` warning and human review, not
  silent scope expansion.
- `scope_guard` remains strict and unchanged.
- Approval UI shows visible amber `[SCOPE]` warnings.
- Files Expected is highlighted in the review UI.

### 3. Test Command Quality Guard

- Backend classifier added with `weak` / `likely_test` / `unknown`.
- Weak examples like `python --version` are detected.
- `likely_test` examples like `python -m pytest` do not warn.
- UI shows the warning in Project Settings and near run/final approval review.
- No execution/checkpoint/rollback semantics changed yet.

### 4. Chunk Dependency Execution Enforcement

- `depends_on` is enforced at runtime.
- A chunk can execute only if all dependency chunks are completed.
- Resume/checkpoint skip path also checks dependencies.
- `DEPENDENCY_NOT_MET` error added through the existing chunk/run failure path.
- Normal sequential behavior remains unchanged.

## Manual Verification

- Calculator run completed successfully with Files Expected:
  - `src/app.py`
  - `tests/test_app.py`
- This confirmed the file-scope bug is fixed.
- Memory suggestions from completed/failed runs work and remain pending.
- Weak test command warning works:
  - `python --version` shows a warning.
  - `python -m pytest` does not warn.
- Chunk dependency enforcement worked:
  - Chunk 1 failed with `PATCH_DOES_NOT_APPLY`.
  - Chunk 2 depended on Chunk 1.
  - Chunk 2 was blocked with `DEPENDENCY_NOT_MET`.

## Intentionally Deferred

### Scope Expansion Recovery

Deferred intentionally.

Future behavior:

- Pause on `SCOPE_VIOLATION`.
- Show requested extra files.
- Require human approval to amend `files_expected`.
- Retry the chunk.
- Never auto-expand scope.
- Never weaken `scope_guard`.

### Patch Failure Recovery v2

Deferred until after this closeout doc is merged.

The next phase should start with a design audit and focus on
`PATCH_DOES_NOT_APPLY` re-index/retry recovery.

### Stronger Test Validation

Deferred.

Future possibilities:

- Weak-command acknowledgement.
- Suggested test command detection.
- Test-count awareness.
- Optional policy.

### Memory M3

Deferred.

Future possibilities:

- Conflict lifecycle.
- Dedicated memory categories.
- Memory usage tracking.
- Constrained LLM-assisted memory later.
- pgvector only when scale justifies.

## Current Roadmap

- **#25 — Stabilization closeout/status docs**
- **#26 — Patch failure recovery v2**
  - start with design audit
  - focus on `PATCH_DOES_NOT_APPLY` re-index/retry recovery
- **#27 — Scope expansion recovery**
  - pause on `SCOPE_VIOLATION`
  - show extra files
  - human approval to amend `files_expected`
  - retry chunk
  - never auto-expand or weaken `scope_guard`
- **#28 — Stronger test validation**
  - weak-command acknowledgement
  - suggested test command detection
  - test-count awareness
  - optional policy
- **#29 — Memory M3**
  - conflict lifecycle
  - dedicated memory categories
  - memory usage tracking
  - constrained LLM-assisted memory later
  - pgvector only when scale justifies
- **#30 — Optional reviewer stage**
- **#31 — GitHub/PR robustness and checks integration**
- **#32 — Production hardening**
  - DB locks
  - durable events
  - secrets encryption
  - Postgres/Alembic path
- **#33 — Multi-LLM/provider modes**
  - fast/standard/deep
  - per-role model config
  - fallback
  - token/cost tracking

Later:

- Slack/email/GitHub comment approvals

## Guardrails For Next Work

- Do not start patch failure recovery until this closeout doc is merged.
- Do not weaken `scope_guard`.
- Do not auto-expand `files_expected`.
- Do not change runtime behavior in this documentation PR.
- Do not change execution/checkpoint/rollback semantics.
- Do not add new product features in this PR.

## Recommended Next Step After Merge

After this doc is merged, start **#26 — Patch failure recovery v2** with a
design audit focused on `PATCH_DOES_NOT_APPLY` re-index/retry recovery.

## Acceptance Status

- Documentation-only closeout completed.
- No runtime behavior changed.
- No test, checkpoint, rollback, memory, scope, or execution semantics changed.
- Stabilization cycle is ready to close after this document is merged.