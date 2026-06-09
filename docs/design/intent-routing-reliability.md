# Intent Routing Reliability (#42)

Status: design / audit only (#42A). No backend, frontend, schema, API, test, or
runtime change lands with this document. It defines the problem, the root cause,
the desired intent policy, the product UX, the backend/API contract, the conflict
policy, the safety invariants, the test matrix, and a small follow-up PR roadmap
so the implementation PRs (#42B–#42G) stay scoped and safe.

## 1. Why this exists

A manual smoke run surfaced explicit implementation requests being classified as
read-only reports. The user asks Pipewright to write code; Pipewright instead
produces a `REPORT_READY` analysis and changes nothing.

### Bad example 1

```
Create a small helper function for the calculator.

Only modify src/app.py and tests/test_app.py.
Do not modify or create any other files.
Add tests for the helper.
```

Expected: implementation / chunk plan. Actual: `REPORT_READY`.

### Bad example 2

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

Expected: implementation / chunk plan. Actual: `REPORT_READY`.

This is a trust problem, not a cosmetic one. The user stated, in plain language,
that this was an implementation request — and even pre-empted the failure ("Do
not run as a read-only report") — yet the product silently chose the opposite
path. When the only thing deciding the outcome is a classifier and the classifier
is wrong, the user has no recourse and the product feels useless.

## 2. Current flow

### Frontend run creation

`frontend/src/pages/ProjectDashboard.tsx` renders a single free-text request box
(a `Textarea` bound to `feature`) and submits it via
`runsApi.createChunkedRun(projectId, feature)`. There is **no mode control** — the
user types a request and Pipewright decides what to do with it.

### Request payload

`frontend/src/api/client.ts` defines `ChunkedRunRequest` as exactly:

```ts
{ project_id: string; feature_description: string }
```

`createChunkedRun` posts those two fields to `POST /runs/chunked`. The backend
`ChunkedRunRequest` Pydantic model (`backend/routes/chunks.py`) carries the same
two fields and nothing else. **There is no field for the user's intended mode.**

### Backend classification and branching

`_create_chunked_run_core` (`backend/routes/chunks.py`) is the single entry point.
It runs a non-actionable guard, then calls `classify_intent_details_async`
(`backend/pipeline/intent.py`), then branches on the result:

- `decision.uncertain` → `_non_actionable_response` (clarification).
- `report_only` → `run_report_analysis` →
  `_create_read_only_run(status=RunStatus.REPORT_READY)`.
- `plan_only` → triage → `_create_read_only_run(status=RunStatus.PLAN_READY)`.
- `implementation` → start-branch guard, index freshness, specificity guard,
  triage, then `create_chunked_run` (the only path that can lead to edits, and
  only after chunk-plan approval).

Read-only and plan-only runs are kept non-executing by the `_load_run_intent`
gate (`backend/routes/chunks.py`): when a run's stored intent is `report_only`
or `plan_only`, implementation actions are blocked.

## 3. Current risk

- **The classifier is the only router.** A single classification fully determines
  whether the user gets a report, a plan, or an implementation. There is no human
  override and no record of what the user actually wanted.
- **The wrong guess happens at maximum confidence.** Both bad examples trip the
  *deterministic* read-only blocker — they are not low-confidence LLM guesses.
  Any design that only surfaces a choice "when the classifier is unsure" would
  **not** have caught these failures, because the classifier was certain and
  wrong. This is the decisive constraint on the fix.
- **A misroute feels irreversible.** The run becomes `REPORT_READY` with no edits,
  no tests, no plan to approve. The user must notice, discard, and re-phrase —
  and they have no lever that says "I meant implement."

## 4. Root cause

The deterministic read-only blocker in `backend/pipeline/intent.py` is too broad
and uses naive substring matching. It runs as an absolute first layer: if it
fires, the function returns `report_only` (or `plan_only`) **before**
implementation verbs are ever considered.

Two distinct misreads produce the smoke failures:

1. **Scope constraints misread as global read-only.** Phrases like
   "Do not modify or create any other files" or "Do not create or modify any
   other files" are *scope-narrowing* instructions — they say *implement, but only
   touch these files*. The blocker matches substrings such as `do not modify` /
   `do not create` and treats them as a global "change nothing" instruction.
2. **Anti-report meta text misread as report-only.** "Do not run as a read-only
   report" is an instruction *against* the report path. Because the blocker
   substring-matches `read-only`, the very sentence meant to prevent a report
   *causes* one.

The underlying defect is that the blocker reasons about **substrings**, not about
**negation and scope**. Adding more keywords cannot fix this class of bug — the
same word ("read-only", "do not modify") legitimately appears in both
report-intent and implementation-intent requests. The fix must distinguish
*global no-change* from *local scope constraint*, and must respect negation
("do not run as a report" ≠ "run as a report").

## 5. Desired intent policy

| Request shape | Correct intent |
|---|---|
| Implementation verbs + scope constraint ("implement X, only touch these files") | `implementation` |
| Genuine global read-only ("analyze and explain, do not change anything") | `report_only` |
| Implementation verbs + global no-change contradiction ("implement X but do not change any code") | `needs clarification` |
| Plan request ("plan how you would do X, don't implement yet") | `plan_only` |
| Vague / non-actionable | existing specificity / clarification path |

The key distinctions the classifier must make:

- **Scope constraint ≠ global read-only.** "Only modify A and B; do not modify
  other files" is an implementation request with a scope, not a read-only request.
- **Negation must be respected.** "Do not run as a read-only report" is *not* a
  read-only signal.
- **Selecting a less powerful mode than the text implies is always safe;**
  selecting a more powerful mode than the text supports needs confirmation
  (see §7).

## 6. Product UX

Add a **mode selector at run creation, always visible**, with the classifier's
suggestion pre-selected as the default:

```
What should Pipewright do?
( ) Read-only report          Analyze & explain only. No code, no tests, no commits, no PR.
( ) Plan only                 Produce a chunk plan to review. No code changes.
(•) Implement with approval   Create a chunk plan, then write code only after you approve.
        ↳ Suggested · Nothing is committed or pushed without your approval.

[ Describe your request … ]                                       [ Start run ]
```

- **Always shown, not confidence-gated.** The smoke failures fired at the
  classifier's highest confidence, so "only show a choice when unsure" would not
  have prevented them. A consistent, always-present selector is predictable,
  teaches the three modes, and makes mode a deliberate choice.
- **Classifier suggests / preselects the default.** When the classifier is
  confident, its suggested mode is pre-selected so the happy path stays one click.
  When the request is uncertain, no mode is pre-selected and the user is asked to
  pick.
- **The visible, user-selected mode is the source of truth.** Whatever the
  classifier thinks, the run is created in the mode the user can see selected. The
  classifier informs the default and the warnings; it no longer silently decides.

Mode copy (honest, no overclaiming):

- **Read-only report** — "Analyze & explain only. No code changes, no tests, no
  commits, no PR."
- **Plan only** — "Produce a chunk plan to review. No code changes."
- **Implement with approval** — "Create a chunk plan, then write code only after
  you approve. Nothing is committed or pushed without your approval."

## 7. Backend / API proposal

Additive and backward-compatible. The selected mode resolves to the existing
stored `intent`, so **no schema change is required**; the conflict confirmation is
an ephemeral request flag, not persisted.

```jsonc
// POST /runs/chunked
{
  "project_id": "…",
  "feature_description": "…",
  "requested_mode": "report_only" | "plan_only" | "implementation" | "auto", // optional; default "auto"
  "confirm_conflict": false   // optional; set true on the re-submit after the user acknowledges a conflict
}
```

Resolution in the route:

- **`requested_mode` omitted or `"auto"`** → run the classifier exactly as today
  (with the §4 fix from #42B applied). This keeps **old clients fully
  backward-compatible**: a client that omits the field behaves identically to the
  current product, just with a more accurate classifier.
- **`requested_mode` concrete** → that mode is authoritative for routing. The
  classifier still runs, but only to (a) have produced the pre-selected default
  and (b) detect a possible conflict for the warning.
- **Conflict detected and `confirm_conflict` is false** → return a new
  `ModeConflictResponse` (a sibling of the existing `NeedsClarificationResponse`)
  describing the conflict and **create no run**. The frontend shows the warning;
  on confirmation it re-submits with `confirm_conflict=true` and the run proceeds
  in the selected mode. This reuses the existing clarification round-trip shape
  rather than inventing a new protocol.

`ChunkedRunResult` gains `ModeConflictResponse` as a union member alongside
`ChunkPlanResponse`, `NeedsClarificationResponse`, and `StaleIndexResponse`.

## 8. Conflict policy

Principle: **a safer (less powerful) selected mode than the text implies is always
honored; a more powerful selected mode than the text supports requires one
explicit confirmation.** The user's visible mode is the source of truth, but a
safety-relevant contradiction is surfaced once — never silently turned into edits.

| Selected mode | Text signal | Direction | Action |
|---|---|---|---|
| Read-only report | implementation-like ("implement add()") | Safer than text | **Honor read-only.** Optional soft note: "Read-only will only analyze — switch to Implement?" Never blocks. |
| Plan only | "just implement, don't plan" | Safer than text | **Honor plan-only.** Optional soft note. |
| Implement with approval | "do not change code" / global no-change | More powerful than text | **`ModeConflictResponse`, require confirmation.** "You picked Implement, but your text says 'do not change code.' Implement creates a chunk plan; nothing is committed until you approve. Continue as Implement, or switch to Read-only?" |
| Implement with approval | vague / non-actionable | More powerful than text | **Existing specificity / clarification path** (no new mechanism). |
| Auto | (any) | n/a | Classifier routes; uncertain → existing clarification. |

Even after a user confirms Implement on a contradictory request, **nothing
executes**: the run only enters the chunk-plan-awaiting-approval path. The
confirmation guards *surprise*, not *safety* — safety remains the approval gate.

## 9. Classifier role after the selector

The classifier keeps three jobs, none of which is "final authority" once a
concrete mode is selected:

1. **Suggestion engine** — computes the pre-selected default mode and confidence.
2. **Conflict detector** — compares detected intent against the selected mode to
   drive the §8 warnings.
3. **Router** — **only** when `requested_mode` is `auto` or omitted (old clients,
   "let Pipewright decide"). Same deterministic + LLM stack, with the §4 fix.

Because the classifier still powers the default and the warnings, the
deterministic scope/negation fix (#42B) is required **even with** the selector: a
broken classifier produces wrong defaults and misfiring warnings (for example,
warning "your text says read-only" on bad example 2). The selector is the
authority backstop; the §4 fix makes the suggestions and warnings trustworthy.

## 10. ML / vector / sklearn decision

- **Defer runtime ML and vector search.** The selector reduces the classifier's
  job from "be correct" to "produce a reasonable default the user can override,"
  which removes the pressure that pushed toward ML.
- **No `sqlite-vec` / embedding store now.** Pipewright is deliberately a
  zero-embedding stack (`backend/repo/repo_indexer.py` states it does not use
  embeddings). Vectors are also negation-blind, so they would not fix the actual
  §4 bug, and they are overkill at the current scale.
- **Constraints honored.** Open-source users must not be required to train models,
  and heavy dependencies are avoided. A free, local, deterministic scope/negation
  classifier beats ML on cost, determinism, and on the specific bug.
- **Optional offline experiment (#42G), deferred.** If the *suggestion* quality is
  ever *measured* to be insufficient, a small pre-trained sklearn artifact may be
  committed to the repo (users never train), run offline, and used to improve
  *suggestions only* — never as the safety authority. This is the last and
  optional item in the roadmap.

## 11. Safety invariants

These must hold for every implementation slice:

1. **Read-only is truly read-only.** A read-only run routes through
   `_create_read_only_run(REPORT_READY)`; the `_load_run_intent` gate keeps
   blocking all implementation actions. No code, no tests, no git, no PR.
2. **Plan-only makes no code changes** (`PLAN_READY`, same gate).
3. **Implementation still requires chunk-plan approval.** Selecting (or confirming)
   Implement grants no execution authority — it only creates the chunk plan.
4. **No auto-execution.** No path runs code, tests, commits, or PRs without the
   existing approval gates.
5. **Scope guard, final approval, and PR safety are unchanged.** The mode selector
   changes only *which path is entered*, never what a path is permitted to do.
6. **Auto / uncertain fails safe** to clarification, never to silent
   implementation.
7. **No schema, memory, retry, or patch behavior change.** `requested_mode`
   resolves to the existing `intent`; `confirm_conflict` is ephemeral. Triage,
   patch apply, rollback, retry eligibility, and memory behavior are untouched.

## 12. Test matrix

### #42B — classifier (`backend/tests/test_intent.py`)

- Bad example 1 → `implementation` (scope constraint, not global read-only).
- Bad example 2 → `implementation` (anti-report meta text, not report-only).
- Genuine global read-only ("analyze and explain, change nothing") → `report_only`
  (must not regress).
- Implementation verbs + true global no-change contradiction → needs clarification.
- Plan request → `plan_only`.

### #42C — API `requested_mode` (`backend/tests/test_chunk_routes.py`)

- `report_only` selected + implementation-like text → `REPORT_READY`, no triage.
- `plan_only` selected → `PLAN_READY`.
- `implementation` selected + clear text → chunk plan awaiting approval (not
  `REPORT_READY`).
- `implementation` selected + "do not change code" + `confirm_conflict=false` →
  `ModeConflictResponse`, no run created.
- same + `confirm_conflict=true` → chunk plan awaiting approval.
- `auto` + bad examples → chunk plan (after the #42B fix).
- payload with `requested_mode` omitted (old client) → identical to current
  behavior.
- `implementation` selected → run still requires chunk-plan approval (no
  execution).

### #42D — frontend selector smoke (build/lint + manual; no FE test runner)

- Selector renders all three modes; classifier suggestion pre-selected when
  confident; none pre-selected when uncertain.
- Submitting carries `requested_mode`; omitting the selector still works.

### #42E — conflict confirmation (`backend/tests/test_chunk_routes.py` + FE manual)

- Implement + contradiction → warning blocks submit until confirmed; confirm
  re-submits with `confirm_conflict=true`.
- Safe-direction mismatch (read-only selected on implementation text) → soft note
  only, never blocks.

## 13. Roadmap

| PR | Scope | Type |
|---|---|---|
| **#42A** | This design / audit document. | Docs-only |
| **#42B** | Deterministic scope/negation classifier fix in `intent.py` + tests. Fixes the reported bug for `auto`/old clients and makes future suggestions/warnings trustworthy. | Backend, no schema |
| **#42C** | API contract: add `requested_mode` (enum incl. `auto`, default `auto`) + `confirm_conflict`; add `ModeConflictResponse`; route honors explicit mode, `auto` → classifier. | Backend, no schema |
| **#42D** | Frontend always-visible mode selector wired to `requested_mode`, with classifier-suggested default and the §6 copy. | Frontend |
| **#42E** | Conflict warning / confirm UI using `ModeConflictResponse` and the `confirm_conflict` re-submit. | Frontend |
| **#42F** | Smoke documentation / closeout and the manual validation matrix. | Docs-only |
| **#42G** | *(Optional, deferred)* offline pre-trained sklearn experiment to improve suggestions only — built only if a measured need appears. | Backend, gated |

Recommended first build: **#42B**. It fixes the exact smoke bug immediately for
every client (including `auto` and old clients), is small/deterministic/testable
with no new dependencies and no schema change, and is a prerequisite for the
selector's trustworthiness. #42C–#42E then make the user-selected mode the durable
source of truth, ending the "one wrong guess, no recourse" failure class. #42G
(ML) stays deferred unless suggestion quality is later measured to be
insufficient.
