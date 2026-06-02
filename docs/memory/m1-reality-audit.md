# Pipewright Memory M1 Reality Audit

This document records a code-backed audit of the Pipewright Memory M1
implementation. It corrects several stale architecture assumptions from a
previous review and defines the M2 sequencing and safety invariants going
forward.

Scope note: this is a documentation-only record of what the current repository
actually contains. It does not change runtime behavior, schemas, or tests.

## Executive verdict

| Area                           | Status                         | Notes                                                                                                       |
| ------------------------------ | ------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| Project-level memory isolation | Implemented                    | project_id exists; load_hard_facts filters by project_id/status/staleness                                   |
| Role-aware injection           | Partially implemented          | triage/planner/coder found; reviewer runtime not found                                                      |
| Write-path hard blockers       | Partially implemented          | many blockers exist; control-plane/path/stack-trace/code-block blockers incomplete                          |
| Suggestions workflow           | Partially implemented          | bootstrap suggestions exist; run-outcome suggestions not implemented                                        |
| Frontend memory UI             | Partially implemented but real | facts, filters, archive/edit/verify, bootstrap suggestions, prompt preview                                  |
| M2 readiness                   | Not fully ready                | needs hard blockers, structured run suggestions, source/evidence fields, atomic approval, conflict handling |

## What previous architecture review got wrong

The previous architecture review made several claims that do not match the
current repository. They are corrected here so future planning does not repeat
them:

* **Claim: `memory_facts` had no `project_id`.** Incorrect for the current
  repo. The column exists and is used throughout the memory store. Project
  isolation is real.
* **Claim: `load_hard_facts()` was global.** Incorrect for the current repo.
  `load_hard_facts()` is project-scoped and filters by `project_id`, active
  status, and staleness. A missing or blank `project_id` deliberately returns
  empty (fails closed) rather than leaking facts across projects.
* **Claim: no suggestions table or workflow existed.** Only partially correct.
  A `memory_suggestions` table exists, and bootstrap suggestions have a
  pending/approve/reject workflow. What is genuinely missing is persistence of
  post-run planner/coder `suggested_memory_entries` into that table.
* **Claim: no memory UI exists.** Incorrect. The memory UI is real, not a
  placeholder. It supports facts, filters, archive/edit/verify, bootstrap
  suggestions, and a prompt preview.

## Verified implemented

The following items were verified against the current codebase. File
references point to where each behavior lives; no line numbers are asserted
beyond what the audit established.

### Schema / database

* `memory_facts` table with a `project_id` column (`backend/db/schema.sql`).
* `memory_suggestions` table exists (`backend/db/schema.sql`).
* Duplicate active memory facts are blocked per project via `content_hash`
  (`backend/memory/memory_store.py`, `backend/db/schema.sql`).
* Migration/init logic archives/stales previously unscoped active facts
  (`backend/db/database.py`).

### Memory store

* `load_hard_facts()` is project-scoped: it filters by `project_id`, active
  status, and staleness (`backend/memory/memory_store.py`).
* `_validate_project_id()` requires a non-blank `project_id`; missing or blank
  values fail closed and return empty (`backend/memory/memory_store.py`).
* `compute_content_hash()` underpins per-project duplicate blocking
  (`backend/memory/memory_store.py`).

### Prompt builder / injection

* The prompt builder defines role categories and per-role budgets
  (`backend/memory/prompt_builder.py`).
* Memory is injected into triage, planner, and coder roles
  (`backend/pipeline/intent.py`, `backend/pipeline/planner.py`,
  `backend/pipeline/` coder path; covered by
  `backend/tests/test_triage.py`, `backend/tests/test_planner.py`,
  `backend/tests/test_coder.py`).

### API

* The memory API is mounted (`backend/routes/memory.py`), covered by
  `backend/tests/test_memory_api.py`.

### Frontend

* The memory UI is real, not a placeholder. It supports listing facts,
  filtering, archive/edit/verify, bootstrap suggestion review, and a prompt
  preview.

### Bootstrap suggestions

* Bootstrap suggestions exist with a pending/approve/reject workflow
  (`backend/memory/bootstrap.py`), covered by
  `backend/tests/test_memory_bootstrap.py`.

### Patch failure enums

* Patch failures are modeled as first-class enum values (patch failure modes
  are explicit enum cases rather than ad-hoc strings).

## Verified missing or weak

The following gaps are real and must be addressed before M2 is considered
ready:

* **Nullable `project_id` at schema level.** `memory_facts.project_id` is
  nullable in the schema and is only required at the application layer
  (`_validate_project_id`). The database does not enforce non-null.
* **Incomplete hard blockers.** Several write-path hard blockers are missing,
  notably Pipewright control-plane bypass phrases:
  * `skip approval`
  * `bypass approval`
  * `auto-merge`
  * `ignore tests`
  * `disable tests`
  * `bypass scope guard`
  * `force push`
  * `commit directly to main`
  * `edit .env`

  No explicit blockers were found for absolute local paths, raw stack traces,
  or large raw code blocks beyond the generic content-length cap.
* **No durable source/run/chunk/evidence fields on `memory_facts`.** The table
  only carries generic `source` / `added_by` / `approved_by`. There is no
  durable linkage to the originating run, chunk, or evidence.
* **No generic conflict lifecycle.** There is no generic `memory_conflicts`
  table or conflict lifecycle.
* **Approval not atomic.** During suggestion approval, fact insertion and
  suggestion status update happen separately rather than in one atomic
  operation.
* **Planner/coder `suggested_memory_entries` not persisted.** These run-derived
  suggestions are not written into `memory_suggestions`.
* **No runtime reviewer injection found.** Memory injection was confirmed for
  triage/planner/coder but not for the reviewer role at runtime.
* **No edit-then-approve flow for suggestions.** A suggestion cannot be edited
  and then approved as a single curated action.
* **No run-outcome suggestion generator.** Post-run / run-outcome suggestions
  are not implemented; only bootstrap suggestions exist.

## Updated M2 sequencing

Recommended PR sequence:

1. **#21A — Memory M1 reality audit doc** (this document).
2. **#21B — Hard blockers and validation tests.** Add the missing write-path
   blockers (control-plane phrases, absolute paths, raw stack traces, large raw
   code blocks) with validation tests.
3. **#21C — Suggestion schema hardening and atomic approval/edit/reject
   lifecycle.** Make approval atomic and add an edit-then-approve flow.
4. **#21D — Deterministic run-outcome suggestion generator.** Persist
   planner/coder `suggested_memory_entries` as pending suggestions.
5. **#21E — Frontend run-derived suggestion review UX.**
6. **#21F — Role-specific injection hardening / reviewer path / budgets /
   preview.**
7. **#21G — Docs and smoke tests.**

## Safety invariants going forward

These invariants must hold across all M2 work:

* Memory reads must remain project-scoped.
* Missing `project_id` must fail closed.
* No auto-save of run-derived memory.
* Hard blockers must be unbypassable.
* Memory must not become a control channel.
* Source code, the current user request, tests, and safety rules override
  memory.
* Failed-run lessons must not become durable injected facts from a single
  failure.
* Run-outcome suggestions must be pending-only until human approval.
* Approval must call the same validation as manual memory creation.
* Duplicates and conflicts must be surfaced before injection.

## Immediate next implementation recommendation

The next code PR should be **#21B**, focused only on missing hard blockers and
validation tests. Do not start run-outcome suggestions until the write-path
blockers are complete.
