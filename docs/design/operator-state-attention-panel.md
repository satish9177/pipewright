# Operator State Attention Panel Design

> Status: **Design only.** This document defines a proposed read-only
> `operator_state` model and a top-level attention panel for Pipewright's run UI.
> It does not ship backend code, frontend code, schema changes, route changes,
> migrations, package changes, tests, or runtime behavior changes.
>
> The design is an additive read-model overlay on top of the existing run,
> chunk, patch-recovery, scope-expansion, test-validation, memory, Git, and PR
> states. Existing routes remain the source of truth and must continue to
> revalidate every mutating action.

---

## Purpose

Pipewright's backend safety model now has several human gates and recovery
paths:

- chunk plan approval;
- approved chunk execution and resume;
- patch retry;
- scope expansion approval or rejection;
- weak/no-test validation acknowledgement;
- chunk approval;
- final approval;
- PR creation;
- memory conflict approval or rejection;
- generic failure and recovery states.

Those gates are valuable, but the operator experience is hard to read. The UI
currently exposes many actions and banners, and a user may need to understand
internal statuses to know what is safe or expected next.

This design introduces a computed `operator_state` read model and a top-level
attention panel that answers, in one place:

- what Pipewright is waiting for;
- why it is waiting;
- what action is available;
- which actions are blocked, and why;
- which process safety checks passed, failed, are weak, or have not run;
- what the user should do next without understanding backend status internals.

The attention panel is not a new authority. It is a display surface for current
state that the backend already knows how to validate.

---

## Non-Negotiable Safety Rules

1. `operator_state` is computed/read-only and never persisted.
2. `operator_state` is additive. It must not replace existing run, chunk,
   approval, retry, test-validation, memory, Git, or PR data.
3. No durable audit/history table is introduced by this work.
4. No history feed is introduced by this work.
5. Only current-state-derived trust facts are allowed.
6. Frontend code must not infer dangerous eligibility for retry, scope
   expansion, chunk approval, final approval, PR creation, or memory decisions.
7. Backend routes remain the source of truth and always revalidate before
   mutating anything.
8. Backend code owns the state explanation and safety-critical copy for
   safety-critical states.
9. `enabled` and `blocked_reason` in `operator_state` are display hints. Routes
   must still reject stale, unsafe, or invalid requests.
10. Risk decisions must not visually nudge the user with a glowing primary CTA.
11. Avoid a generic "Continue" action. Execute, resume, retry, approve scope,
    acknowledge validation risk, approve final, and create PR have different
    meanings and risks.
12. The safety ledger describes process gates only. It must never imply that the
    generated code is definitely correct.

---

## Decision Types

`decision_type` tells the frontend how to lay out the attention panel. The
frontend may use the value for presentation, but the backend still owns which
actions are available and why.

| Value | Meaning | UI treatment |
| --- | --- | --- |
| `progress` | One ordinary workflow step is available and safe to present as the next step. | One clear primary action is acceptable. |
| `risk_decision` | The user is being asked to accept or reject a safety or trust tradeoff. | Render neutral/co-equal actions. Do not make approve, acknowledge, or accept look pre-blessed. |
| `none` | No in-app action is available, or the state is informational/instruction-only. | No primary CTA. Show instruction, blocked reasons, or terminal/unknown copy. |

Examples:

- `progress`: approve plan, execute chunks, approve chunk after strong tests,
  approve final when all gates are satisfied, create PR.
- `risk_decision`: approve/reject scope expansion, acknowledge weak/no-test
  validation, memory conflict approve/reject.
- `none`: running, wrong branch, terminal, unknown, stalled.

---

## Safety Check Statuses

Each safety ledger row uses one of these statuses:

| Value | Meaning |
| --- | --- |
| `passed` | The process gate ran and satisfied its rule. |
| `failed` | The process gate ran and failed, or a required safe condition is absent. |
| `weak` | The gate produced a known limited signal that requires human attention or acknowledgement. |
| `not_evaluated` | The gate has not run yet, or the relevant evidence is not available yet. |
| `not_applicable` | The gate does not apply to this run state. |

The ledger must be honest about scope. For example, "Tests: strong" means a
meaningful test command ran and passed according to Pipewright's process rules.
It does not mean the implementation is certainly correct.

---

## Proposed `operator_state` Shape

The shape below is descriptive, not implementation code. Exact model names and
module placement are implementation-slice decisions.

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | integer | Starts at `1`; lets the frontend safely evolve rendering. |
| `title` | string | Short top-line state, owned by the backend. |
| `explanation` | string | User-facing reason for the state, owned by the backend. |
| `waiting_on` | `human` / `system` / `nobody` | Who must act before the run can move. |
| `decision_type` | `progress` / `risk_decision` / `none` | Drives layout treatment. |
| `primary_action` | action or null | Only for `progress` states where one clear action is appropriate. |
| `next_action` | action or null | Optional alias/name if the implementation prefers this over `primary_action`; do not expose both with conflicting meaning. |
| `neutral_actions` | list of actions | Co-equal risk-decision actions, such as approve/reject. |
| `secondary_actions` | list of actions | Non-primary, non-risk actions such as refresh or view detail. |
| `blocked_actions` | list of actions | Actions the operator might expect but cannot safely take now. Must include reasons. |
| `safety_checks` | list of safety checks | Current process-gate ledger. |
| `trust_facts` | list of facts | Current-state-derived facts only; no historical feed. |
| `out_of_app_instruction` | string or null | Manual instruction when no route action can safely proceed. |
| `is_terminal` | boolean | True for completed/failed terminal states. |
| `unknown_state_warning` | string or null | Present when the backend cannot map the state safely. |

### Action Shape

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | Stable action identifier, such as `approve_plan`, `retry_patch`, `approve_scope_expansion`, `acknowledge_test_validation`, `approve_final`, `create_pr`. |
| `label` | string | User-facing button/menu label. Avoid generic "Continue." |
| `intent` | string | Backend-owned short explanation of what this action does. |
| `severity` | `normal` / `caution` / `danger` | Display hint only. Does not control authorization. |
| `enabled` | boolean | Display hint based on current read state. Route still revalidates. |
| `blocked_reason` | string or null | Required when `enabled` is false. |

### Safety Check Shape

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | Stable check identifier, such as `branch`, `scope`, `tests`, `test_ack`, `chunk_approval`, `final_approval`, `pr`. |
| `label` | string | Short display label. |
| `status` | `passed` / `failed` / `weak` / `not_evaluated` / `not_applicable` | Process-gate status. |
| `detail` | string | Backend-owned explanation of what the status means now. |

### Trust Facts

Trust facts are short, current-state-derived statements. They are not a history
feed and not an audit trail.

Allowed examples:

- `Current branch matches the expected Pipewright branch.`
- `Working tree is clean.`
- `A weak-test acknowledgement matches the current diff hash.`
- `The current scope expansion request is pending human decision.`
- `A PR already exists for this run.`

Disallowed examples:

- A timeline of prior retries.
- A durable audit summary.
- Claims that code correctness has been proven.
- Inferences from stale artifacts that are not current-state-derived.

---

## State and Action Matrix

This matrix defines the intended user-facing state for common run conditions.
The backend computes the selected row and the frontend renders it. The frontend
must not choose precedence between rows.

| State | Title | Waiting on | Decision type | Available action(s) | Blocked action(s) | Safety ledger highlights | User-facing explanation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Chunk plan awaiting approval | Review the chunk plan | human | `progress` | Approve chunk plan | Execute chunks, approve final, create PR | Plan: `not_evaluated`; Tests: `not_evaluated`; PR: `not_applicable` | Pipewright needs human approval of the proposed chunk plan before it can edit files. |
| Plan approved but chunks not executed | Execute approved chunks | human | `progress` | Execute approved chunks | Approve chunk, approve final, create PR | Plan: `passed`; Patch: `not_evaluated`; Tests: `not_evaluated` | The plan is approved. Pipewright is waiting for the operator to start chunk execution. |
| System currently running | Pipewright is running | system | `none` | None | Approvals, retries, final approval, PR creation | Active stage: `not_evaluated` or current; Branch: current check if known | A pipeline step is currently running. Wait for it to finish before taking another action. |
| Patch failure retry available | Patch retry is available | human | `progress` | Retry patch | Approve chunk, approve final, create PR | Branch: `passed`; Working tree: `passed`; Patch: `failed`; Scope expansion: `not_applicable` | The patch failed in a retryable way and the current repository state is safe for a human-triggered retry. |
| Patch failure retry blocked because wrong branch/detached/unverifiable HEAD | Retry is blocked by branch state | human | `none` | None | Retry patch, approve chunk, approve final | Branch: `failed`; Patch: `failed`; Working tree: current if known | Pipewright cannot verify that HEAD is on the expected branch. Return to the expected branch before retrying. |
| Pending scope expansion | Scope expansion needs review | human | `risk_decision` | Approve scope expansion and retry; reject scope expansion | Normal retry, approve chunk, approve final, create PR | Scope: `failed`; Branch: `passed` if verified; Working tree: `passed`; Tests: `not_evaluated` | The previous attempt tried to touch files outside the approved chunk scope. Approving scope only allows a retry under the expanded allowlist; it does not approve code. |
| Scope expansion rejected | Scope expansion was rejected | human | `none` | None, or out-of-app manual intervention instruction | Approve scope expansion, normal retry, approve chunk, approve final | Scope: `failed`; Patch: `failed`; Tests: `not_evaluated` | The requested expanded scope was rejected. The chunk remains failed until the operator resolves it manually or starts a new safe path. |
| Wrong branch during scope approval | Scope approval is blocked by branch state | human | `none` | None | Approve scope expansion and retry; reject may remain available only if route can safely record it without touching repo state | Branch: `failed`; Scope: `failed`; Working tree: unknown or current | Pipewright cannot safely approve-and-retry scope expansion because the repository branch is wrong, detached, or unverifiable. |
| Scope expansion approve-and-retry succeeds and pauses at chunk approval | Review recovered scoped change | human | `progress` | Approve chunk | Execute next chunk, approve final, create PR | Scope: `passed`; Patch: `passed`; Tests: `passed`/`weak`/`not_evaluated` based on verdict; Chunk approval: `not_evaluated` | The expanded-scope retry produced a change. Review the actual code before it is committed. |
| Weak/no-test acknowledgement missing | Acknowledge weak validation | human | `risk_decision` | Acknowledge weak/no-test validation | Approve final, create PR | Tests: `weak`; Test acknowledgement: `failed`; Final approval: `failed` | Tests did not meaningfully run, or no tests were configured. The operator must acknowledge this before final approval can proceed. |
| Weak/no-test acknowledgement current | Weak validation acknowledged | human | `progress` | Approve final, if all other gates are satisfied | Create PR until final approval completes | Tests: `weak`; Test acknowledgement: `passed`; Final approval: `not_evaluated` | The weak/no-test acknowledgement matches the current diff hash. Final approval may proceed if no other gate is blocked. |
| Stale acknowledgement | Test acknowledgement is stale | human | `risk_decision` | Re-acknowledge weak/no-test validation | Approve final, create PR | Tests: `weak`; Test acknowledgement: `failed`; Diff hash: changed | The previous acknowledgement was made for a different diff. The current change needs a fresh acknowledgement before final approval. |
| Strong tests | Tests passed with strong validation | human | `progress` | Next eligible approval action, often approve chunk or approve final | PR creation until final approval completes | Tests: `passed`; Test acknowledgement: `not_applicable` | Meaningful tests ran and passed according to Pipewright's process rules. This does not prove code correctness. |
| Chunk awaiting approval | Review chunk change | human | `progress` | Approve chunk | Execute dependent chunks, approve final, create PR | Patch: `passed`; Tests: current verdict; Chunk approval: `not_evaluated`; Scope: `passed` or `not_applicable` | The chunk produced a change and is waiting for human review before commit. |
| Final approval blocked | Final approval is blocked | human | `none` or `risk_decision` depending on blocker | Only the blocker-specific action, if any | Approve final, create PR | Chunk approvals: `failed` or `not_evaluated`; Tests/test ack: current; Branch: current | Final approval cannot proceed until all required chunk, branch, scope, memory, and validation gates are satisfied. |
| Final approval available | Review final result | human | `progress` | Approve final | Create PR until final approval completes | Chunk approvals: `passed`; Tests/test ack: `passed`/`not_applicable`; Branch: `passed`; Final approval: `not_evaluated` | All required gates for final approval are satisfied. The operator may approve the final result. |
| Memory conflict pending | Resolve memory conflict | human | `risk_decision` | Approve memory change; reject memory change | Final approval, create PR | Memory conflict: `failed`; Final approval: `failed`; Tests: current | Pipewright detected a memory conflict that requires a human decision before the run can safely finish. |
| `local_only` completion/manual push state | Manual push required | nobody | `none` | None | Create PR in Pipewright | Final approval: `passed`; PR mode: `not_applicable`; Push/PR: `not_applicable` | The run completed locally. This project mode requires the operator to push or create a PR outside Pipewright. |
| `github_cli` PR not yet created | Create pull request | human | `progress` | Create PR | Approve final | Final approval: `passed`; Branch: `passed`; PR: `not_evaluated` | Final approval is complete and the branch is ready for Pipewright to push/create a PR through the configured GitHub path. |
| PR created or reused | Pull request is ready | nobody | `none` | None | Create duplicate PR | Final approval: `passed`; PR: `passed` | A pull request already exists or was reused for this run. No further in-app action is required. |
| Terminal failed/completed state | Run is complete | nobody | `none` | None | Mutating run actions | Terminal: `passed`; Other checks: final known statuses | The run has reached a terminal state. No further Pipewright action is available for this run. |
| Unknown/unmapped state | Next safe action is unknown | human | `none` | None | All mutating actions | Unknown state: `failed`; Other checks: current if known | Pipewright cannot determine the next safe action. No action is available. Investigate before proceeding. |
| Stalled/running-too-long state | Run may be stalled | human | `none` | None, or out-of-app investigation instruction | Approvals, retries, final approval, PR creation | Active stage: `not_evaluated`; Stalled check: `failed` | Pipewright has been in a running state longer than expected based on persisted timestamps. Investigate before proceeding. |

---

## Precedence Rules

When multiple conditions could produce different attention states, the backend
must choose the displayed `operator_state`. The frontend must never choose
precedence.

Recommended precedence:

1. Wrong branch, detached HEAD, or unverifiable HEAD where the relevant action
   would touch the working tree or commit/push state.
2. Pending scope expansion.
3. Memory conflict.
4. Patch failure / retry state.
5. Test-validation acknowledgement gate.
6. Chunk approval.
7. Final approval.
8. PR creation.
9. Terminal, stalled, or unknown fallback.

### Named Bug Case: Scope Expansion Beats Stale Patch Failure Copy

If a pending scope expansion exists and an old/stale patch failure summary also
exists, the pending scope expansion wins.

Required behavior:

- render the scope expansion risk decision;
- block or suppress normal retry;
- do not render stale generic patch-failure copy underneath the scope expansion
  UI;
- keep approve/reject scope expansion as neutral/co-equal actions;
- still let the approve-and-retry route revalidate branch, working tree,
  failure identity, request state, and scope eligibility before mutating
  anything.

Rationale: normal retry inside the old scope is the wrong mental model once the
current safe path is a human decision about expanded scope.

---

## Run Phase Projection (item 16)

`operator_state` carries two additive, derived, display-only fields (proposal
§4.8): a user-facing `phase` and a structured `narrative`. Both are recomputed
on every read, never persisted, and grant no authority — the same contract as
the rest of `operator_state`.

`phase` is one of six buckets, projected from the **already-selected** state by
its visible properties (so the phase can never disagree with what the operator
sees) plus two run-status splits:

| Phase | When |
| --- | --- |
| `planning` | system busy forming the plan — `waiting_on == system` and `run_status == running` (pre-execution pipeline) |
| `working` | system busy executing/pushing — `waiting_on == system` otherwise (`running_chunks`, `pushing`, strong-tests fallback) |
| `waiting_for_you` | an expected human gate / acknowledgement / decision — `waiting_on == human` with an available action and not a failure (chunk-plan/chunk/final approval, execute, weak/review acks, scope-expansion **pending**, memory conflict, create-PR) |
| `needs_attention` | a failure / anomaly / blocked state needing a human — `unknown_state_warning`, `decision_type == none`, `push_failed`, a **retryable patch failure** (a failure first, despite offering retry), `final_approval_blocked`, `stalled`, wrong branch, scope-expansion **rejected**, and terminal `failed`/`rejected`/`final_rejected` |
| `done` | terminal `complete`, PR created/reused, local-only completion (`waiting_on == nobody`) |
| `stopped` | reserved for an explicit user cancel/stop terminal status — **no such status exists today**, so no current state reaches it; failed/rejected map to `needs_attention`, not `stopped` |

The fail-safe rule (proposal §0 analog): the unknown/unmapped fallback and every
failure/anomaly map to `needs_attention` — a phase is never the optimistic guess,
so a degraded or stuck read never reads as healthy.

`narrative` is `{what_happened, why, whats_next}`: `what_happened`/`why` reuse
the backend-owned `title`/`explanation` verbatim (no new prose, so no correctness
claim and no raw-evidence leak), and `whats_next` is derived ONLY from the
available actions (primary → neutral → secondary), so it can never present a
blocked or nonexistent action as available.

## Backend and Frontend Responsibility Split

### Backend Responsibilities

- Compute `operator_state` from current persisted run/chunk/project/repo state.
- Own safety-critical labels, explanations, blocked reasons, and out-of-app
  instructions.
- Evaluate action availability for display using the same eligibility predicates
  or carefully shared behavior-preserving equivalents used by routes.
- Include blocked actions where helpful, with precise reasons.
- Expose `operator_state` additively on existing read APIs.
- Recompute `operator_state` on every read; do not persist it.
- Keep routes authoritative. Every route must revalidate under the appropriate
  lock before mutating state.

### Frontend Responsibilities

- Render `operator_state`.
- Use `decision_type` only for layout treatment.
- Use action `enabled`, `severity`, and `blocked_reason` only as display hints.
- Avoid generic "Continue" copy.
- Render `risk_decision` actions neutrally and co-equally.
- Avoid duplicating or inventing safety-critical eligibility logic.
- Avoid owning safety-critical copy for retry, scope expansion, final approval,
  PR creation, memory conflict, or weak/no-test acknowledgement states.

### Eligibility Consistency Rule

In stable test fixtures with no intervening state change, `operator_state`
eligibility should match route eligibility.

At runtime, a route may still reject even when `operator_state` showed
`enabled=true`, because the state can change between read and mutation. Expected
rejection causes include:

- branch changed;
- detached or unverifiable HEAD;
- dirty working tree;
- stale request token or diff hash;
- lock contention or concurrent state transition;
- changed run or chunk status;
- PR already created by another actor.

These route rejections are correct. `operator_state` is not a bypass.

---

## Staleness and Stalled Handling

The existing weak/no-test acknowledgement staleness rule remains required:

- acknowledgement must be bound to the current diff/test checkpoint hash;
- stale acknowledgement must block final approval;
- stale acknowledgement must be explained as stale, not silently ignored.

`operator_state` itself is always recomputed, so it does not need a durable
staleness marker.

This design does not implement universal artifact staleness. It should not
introduce broad stale/current tracking for checkpoints, patch summaries, plans,
or PR artifacts. Future reviewer or audit work may reuse a vocabulary such as
`current`, `stale`, `missing`, and `not_required`, but that is out of scope for
this design.

Stalled detection must be based on durable persisted timestamps and statuses,
not the in-memory event bus. The event bus is useful for live logs, but it is
process-local and non-durable, so it cannot be the authority for whether a run
is stalled.

Unknown/unmapped states must fail closed with this message:

> Pipewright cannot determine the next safe action. No action is available.
> Investigate before proceeding.

---

## Safety Ledger Guidance

The attention panel should include a compact ledger of process checks. The
ledger should be useful without becoming a dashboard.

Recommended check ids:

- `branch`
- `working_tree`
- `plan_approval`
- `scope`
- `patch`
- `tests`
- `test_acknowledgement`
- `chunk_approval`
- `memory_conflict`
- `final_approval`
- `pr`

Rules:

- Do not display "all safe" as a blanket claim.
- Do not present strong tests as correctness proof.
- Use `not_evaluated` when a stage has not run yet.
- Use `not_applicable` instead of hiding important non-applicable checks when
  their absence could otherwise confuse the operator.
- Use `weak` for weak/no-test validation and similar process signals that are
  known limited signals rather than failures.
- Pair every `failed` or `weak` check with a backend-owned detail string.

---

## Non-Goals

This design explicitly does not include:

- backend code;
- frontend code;
- schema or migration changes;
- route changes;
- durable audit table;
- history feed;
- reviewer stage;
- dashboards or PM views;
- multi-model routing;
- Memory M3;
- auto-approval;
- auto-commit;
- auto-retry;
- final approval bypass;
- a generic "Continue" action that hides different semantics.

---

## Future Implementation Slices

These are safe next steps after this design document. Names are descriptive on
purpose; do not treat them as roadmap labels.

### Shared Eligibility Audit

Carefully identify the existing route eligibility predicates for retry, scope
expansion, chunk approval, final approval, memory conflict decisions, and PR
creation. Extract or share only where behavior-preserving tests can prove the
route behavior stays unchanged.

### Pure Operator State Models

Add pure backend models/helpers for `operator_state`, actions, blocked actions,
safety checks, and trust facts. Keep the helper deterministic and side-effect
free. Do not persist its output.

### Additive API Surfacing

Expose `operator_state` additively on existing run/read responses. Do not remove
old fields or old controls in this slice. Add fixtures proving eligibility
matches route behavior when no state changes between read and mutation.

### Attention Panel Frontend

Add a top-level "What needs your attention?" panel that renders the backend
state. Use `decision_type` to choose primary vs neutral action layout. Keep old
controls visible until the new panel is smoke-tested.

### Control Consolidation

Only after manual smoke testing, consolidate or hide confusing old banners and
controls that duplicate `operator_state`. Do not remove route revalidation or
existing safety checks.

### Smoke Documentation

Add manual smoke docs for the flows below, including screenshots or exact state
expectations where useful.

---

## Future Smoke Checklist

Manual smoke coverage for implementation should include:

- strong test happy path;
- weak/no-test final approval blocked, then acknowledged;
- stale acknowledgement blocks final approval;
- patch retry available;
- patch retry blocked on wrong branch;
- scope expansion approve path;
- scope expansion reject path;
- wrong-branch scope approval conflict;
- scope expansion success pauses at chunk approval;
- memory conflict state renders correctly;
- PR created/reused state;
- `local_only` manual push state;
- unknown/stalled state;
- precedence case: pending scope expansion plus old patch failure summary.

For each smoke, verify:

- the attention panel title and explanation are understandable without internal
  backend status knowledge;
- `decision_type` produces the correct layout treatment;
- risk decisions have neutral/co-equal actions;
- blocked actions show precise reasons;
- safety ledger rows use honest statuses;
- route rejection still behaves safely if state changes after the page read.

---

## Final Safety Invariants

- `operator_state` is a read-only current-state explanation, not authority.
- The backend computes it; the frontend renders it.
- Routes always revalidate before mutation.
- Risk decisions are visually neutral.
- There is no generic "Continue" action.
- Strong tests do not prove correctness.
- Weak/no-test acknowledgement staleness remains enforced.
- Pending scope expansion takes precedence over stale generic patch-failure
  retry copy.
- Unknown and stalled states fail closed with no mutating action available.
