# Memory M2 — Run Outcome Suggestions

## Overview

Memory M2 lets a user generate memory suggestions from a run that has reached a
terminal status (completed, failed, or rejected). It turns the run's
**structured** outcome into candidate memory entries so useful lessons are not
lost between runs.

Key properties:

- Generated suggestions are **pending only**. Generation never writes an active
  memory fact.
- A human must approve, edit-and-approve, or reject each suggestion.
- Only **approved** suggestions become active memory facts.
- Memory remains **project-scoped** and **advisory**. It never overrides source
  code, the current user request, the project's tests, or Pipewright's safety
  rules.

A single failed run can therefore never become a durable injected rule on its
own. Everything stays pending until a human approves it.

## End-to-end flow

1. A run reaches a terminal status:
   - `complete`
   - `failed`
   - `rejected`
2. The user opens **Run Detail** for that run.
3. The user clicks **"Generate memory suggestions from this run."**
4. The backend reads the run's structured outcome fields (run status, chunk
   completion summaries, patch-failure reports, rejected approval reasons,
   project test command).
5. The backend creates **pending** `memory_suggestions` for each safe candidate.
6. Run Detail shows `generated` / `skipped` / `blocked` counts and a compact
   read-only mini-list of what was generated.
7. The user opens **Project Memory**.
8. The user **approves**, **edit-and-approves**, or **rejects** each pending
   suggestion.
9. Approval creates an active memory fact **atomically** (the suggestion is
   marked approved and the fact is created in the same operation).
10. Later, the role-specific prompt builder *may* inject the approved memory into
    triage/planner/coder prompts (advisory only).

## What sources M2 uses

M2 reads only safe, structured fields that the pipeline already produces:

- `pipeline_runs.status` and `pipeline_runs.project_id`.
- `chunks.completion_summary` structured fields.
- Planner/coder `suggested_memory_entries` from a chunk's success summary, when
  they pass deterministic validation (non-empty, length-bounded, string).
- Patch-failure reports parsed from completion summaries, keyed by first-class
  `PatchFailureType` enum values.
- Rejected approval reasons from `approval_gates` (sanitized and truncated).
- The project's `test_command` (for a successful run).

## What sources M2 does not use

M2 deliberately does **not** read or extract from:

- Raw repository files.
- Raw logs.
- Stack traces.
- Raw diffs.
- Arbitrary PR text.
- LLM extraction or phrasing.
- pgvector / embeddings / semantic retrieval.

## Suggestion categories and examples

Generation is deterministic. The current categories produced are `test`,
`security`, and `other`. Examples:

- **Test command suggestion** (from a successful run) — `test` category:
  `Project test command: <project test_command>`.
- **Run handoff suggestion** (from planner/coder structured
  `suggested_memory_entries`) — `other` category, preserved verbatim if it
  passes validation.
- **Dirty worktree operational note** — `other`, low risk.
- **Stale index / changed file operational note** — `other`, medium risk.
- **Target missing note** — `other`, medium risk.
- **Patch does not apply note** — `other`, medium risk.
- **Scope violation / forbidden file high-risk note** — `security`, high risk.
- **Test failure after apply note** — `test`, medium risk; never includes the
  raw stack trace, only an instruction to review failing test names.
- **Rejected approach note** (from a rejected approval reason) — `other`, medium
  risk; the reason is sanitized and truncated.

Note: rejected-approach and patch-failure lessons currently persist under the
existing `other` / `security` / `test` categories. There are no dedicated
`rejected_approach` / `patch_failure_lesson` categories yet (see Deferred work).

## Safety model

- **No auto-save.** Generation only ever creates pending suggestions.
- **Pending-only generation.** No active fact is created during generation.
- **Approval is atomic.** The suggestion is marked approved and the active fact
  is created in a single operation.
- **Edit-and-approve revalidates content.** Edited content goes through the same
  content validation as a manual fact.
- The **#21B hard blockers** apply to both manual facts and suggestion approval:
  control-plane bypass phrases (e.g. skip approval, auto-merge), absolute local
  paths, raw stack traces, and large raw code blocks are rejected.
- **Blocked generated content is not approvable.** Blocked candidates are
  counted in `blocked_count` and never stored, so they cannot be approved.
- **Active duplicate prevention.** Per-project content-hash dedupe prevents
  creating an active duplicate fact.
- **Idempotent generation.** Re-generating for the same run skips candidates
  that already exist as pending suggestions or active facts, so it does not spam
  suggestions.
- **Source code / current user request / tests / safety rules override memory.**
  The advisory wrapper states this explicitly.
- **Memory is advisory, not authoritative.**

## Role-specific injection

- Triage, planner, and coder currently inject project memory (advisory).
- A reviewer prompt builder and preview exist, but there is **no reviewer
  runtime stage yet**, so reviewer memory is preview-only.
- Approximate per-role token budgets (`ROLE_TOKEN_BUDGETS` in
  `backend/memory/prompt_builder.py`):

  - triage: 400
  - planner: 1200
  - coder: 1200
  - reviewer: 800

  (architect and summary roles also exist at 1200 and 800 respectively.)

- Every role always includes the safety categories `security` and
  `forbidden_paths`. The exact per-role category sets are defined by
  `ROLE_CATEGORIES` in `backend/memory/prompt_builder.py`. As currently
  implemented:

  - **triage**: `security`, `forbidden_paths`, `stack`, `structure`, `test`,
    `db`.
  - **planner / coder**: safety categories plus `stack`, `db`, `test`,
    `structure`, `architecture`, `style`, `deploy`, `reviewer_pref`, `other`
    (which is where approved run-outcome lessons land).
  - **reviewer**: focused set — `security`, `forbidden_paths`, `architecture`,
    `test`, `deploy`, `style`, `reviewer_pref`, `other`.

  If you reference exact category names elsewhere, match the current
  implementation rather than this summary.

- The injected block is wrapped with an advisory header
  (`=== PROJECT MEMORY (advisory; source code wins on conflict) ===`) and a
  trailing note that source code, the user's instruction, tests, and safety
  rules win on conflict.

## API reference

Generation route (run-scoped, keyed by `run_id`):

- `POST /api/v1/runs/{run_id}/memory-suggestions/generate`
- Optional body: `{ "requested_by": "<string>" }`.
- Response fields:
  - `run_id`
  - `project_id`
  - `generated_count`
  - `skipped_count`
  - `blocked_count`
  - `suggestions[]` — the newly generated pending suggestions.

Suggestion lifecycle routes (project-scoped, under the project memory router):

- `POST /api/v1/projects/{project_id}/memory/suggestions/{suggestion_id}/approve`
  - Optional body: `{ "edited_content": "<string>", "approved_by": "<string>" }`.
  - `edited_content` supports edit-and-approve; it is revalidated before the
    active fact is created.
  - Returns the approved `suggestion` and the created `fact`.
- `POST /api/v1/projects/{project_id}/memory/suggestions/{suggestion_id}/reject`
  - Body: `{ "reason": "<string>" }` (required, min length 4).
- `GET /api/v1/projects/{project_id}/memory/suggestions`
  - Optional `status` query filter.

(Route names above reflect `backend/routes/memory.py`. Do not invent additional
endpoints; inspect that file if anything is unclear.)

## Frontend behavior

- The Run Detail **"Generate memory suggestions"** button appears **only for
  terminal runs** (complete / failed / rejected).
- Non-terminal (in-progress) runs hide the button.
- A run with no project disables generation with an explanatory note.
- The generation result shows `generated` / `skipped` / `blocked` counts, a
  compact read-only mini-list, and a CTA to Project Memory.
- The Project Memory page shows provenance for each suggestion
  (`source_type`, `source_run_id`, `risk_level`, `rationale`, etc.).
- Edit-and-approve is supported from Project Memory.
- Blocked suggestions are represented in the `blocked_count` only — they are
  never shown as approvable rows because they were never stored.

## Deferred work

- pgvector / semantic retrieval.
- LLM-assisted suggestion phrasing or classification.
- A dedicated `memory_conflicts` table and conflict lifecycle.
- Dedicated `rejected_approach` / `patch_failure_lesson` categories instead of
  the current `other` / `security` / `test` fallback categories.
- Reviewer runtime injection (once a reviewer stage actually exists).
- Richer analytics / usage history.
- Slack / email approval integration.
- Multi-project / team permission model.
