# Failure-State UX Cleanup (#40)

Status: design / audit only (#40A). No backend, frontend, schema, API, test, or
runtime change lands with this document. It defines the problem, the failure
taxonomy, the copy contract, and a small follow-up PR roadmap so the
implementation PRs (#40B–#40F) stay scoped and safe.

## 1. Why this exists

A smoke run surfaced misleading copy on the Run Detail page. The top-level
operator panel said:

> Code change could not be applied — retry unavailable

But the actual recorded failure was `TEST_FAILURE_AFTER_APPLY`, which means:

1. The patch **applied** cleanly.
2. The project's tests **ran**.
3. The tests **failed**.
4. The patch was **rolled back**.
5. **Nothing was committed.**

So the top-level copy was wrong on two independent counts: it implied the change
*could not be applied* (it was), and it implied (in its explanation) that tests
*did not run* (they ran and failed). The retry-availability verdict was also
promoted into the headline, framing the failure by what the user *cannot do*
rather than by *what happened*.

This is a trust problem, not a cosmetic one. Pipewright's product promise is a
truthful, auditable UI. Copy that misstates whether code applied or whether
tests ran erodes exactly the guarantee the rest of the safety machinery is built
to protect.

## 2. Root cause

There are two failure surfaces on Run Detail and they do not agree.

### Chunk-level surface (already accurate)

`frontend/src/components/PatchFailureBanner.tsx` renders the per-chunk failure.
It calls `patchFailurePlainCopy(report.failure_type)` from
`frontend/src/utils/patchFailure.ts`, which maps each `PatchFailureType` to a
type-specific headline, detail, and an honest test note. For
`TEST_FAILURE_AFTER_APPLY` it correctly says:

- headline: "Tests failed after the change was applied"
- detail: "Pipewright applied the change and ran tests, but the tests failed, so
  the change was rolled back."
- test note: "Tests ran and failed, so the change was not kept."

This surface is correct and type-aware. It is **not** the source of the bug and
should not be regressed.

### Run-level surface (failure-type-blind — the bug)

`backend/pipeline/operator_state.py` computes the top operator-attention panel.
`_patch_failure_state()` emits exactly one of two hardcoded titles for **all 11**
`PatchFailureType` values:

- retry eligible → `"Code change could not be applied"`
- retry ineligible → `"Code change could not be applied — retry unavailable"`

Its explanation text likewise hardcodes "could not apply that change… tests did
not run."

The structural cause: `OperatorStateContext` carries `patch_failure_present:
bool` and `patch_retry_decision`, but **no `failure_type`**. The read model that
produces the top-level copy literally cannot distinguish "patch never applied"
from "patch applied, tests failed, rolled back." Because
`TEST_FAILURE_AFTER_APPLY` is (correctly) excluded from
`_HUMAN_RETRYABLE_FAILURE_TYPES`, it lands in the ineligible branch and inherits
the "could not be applied — retry unavailable" title.

The fix is to make the run-level read model failure-type-aware using the same
family grouping the frontend already encodes — **not** to change retry
eligibility.

## 3. Failure taxonomy (user-facing families)

The backend `PatchFailureType` enum is correct and closed; it does not change.
This document defines a **presentation grouping** on top of it. Families are
chosen on the two axes a user actually cares about: *did the change apply?* and
*did tests run?* This mirrors the `tests: 'not_run' | 'failed' | 'unknown'`
field already present in `patchFailure.ts`.

| Family | Backend `PatchFailureType` values | Applied? | Tests ran? |
|--------|-----------------------------------|----------|-----------|
| **Change could not be applied** | `PATCH_DOES_NOT_APPLY`, `PATCH_MALFORMED`, `PATCH_PARTIAL_APPLY_BLOCKED`, `TARGET_MISSING`, `STALE_INDEX_OR_FILE_CHANGED` | No | No |
| **Tests failed after the change was applied** | `TEST_FAILURE_AFTER_APPLY` | Yes (then rolled back) | Yes — failed |
| **Blocked for safety** | `SCOPE_VIOLATION`, `FORBIDDEN_FILE` | No (refused) | No |
| **No change was produced** | `NO_CHANGES` | No (empty) | No |
| **Repo was not ready** | `DIRTY_WORKTREE` | No | No |
| **Unexpected failure** | `UNKNOWN_PATCH_FAILURE` | Unknown | Unknown |

Notes:

- **Blocked for safety** is deliberately separated from "could not be applied."
  A scope/forbidden refusal is the safety gate doing its job, not a mechanical
  failure to produce code. Conflating them undersells the product.
- **No change was produced** is frequently benign (the change may already be
  present). It must not read as breakage.
- **Repo was not ready** (`DIRTY_WORKTREE`) is actionable by the user (commit or
  stash), distinct from an AI-side failure.

## 4. Copy matrix

For each family: the run-level title is the headline; the run-level explanation
is one sentence; the test note states honestly what we can say about tests. The
chunk-level `PatchFailureBanner` detail copy already matches this intent and is
kept as the detailed view.

Every family's copy ends with the always-true assurance **"Nothing was
committed."** Retry-availability is **never** part of the title; when retry is
unavailable it appears as a separate sub-line (see §5).

| Family | Run-level title | Run-level explanation | Test note |
|--------|-----------------|-----------------------|-----------|
| Change could not be applied | **Code change couldn't be applied** | "Pipewright generated a change, but it didn't match the current files, so nothing was applied. Nothing was committed." | "Tests didn't run because the change was never applied." |
| Tests failed after the change was applied | **Tests failed after the change was applied** | "Pipewright applied the change and ran your tests, but the tests failed, so the change was rolled back. Nothing was committed." | "Tests ran and failed, so the change wasn't kept." |
| Blocked for safety — scope | **Change was blocked — outside approved scope** | "The change tried to edit files outside this chunk's approved scope, so Pipewright refused it. This is a safety stop. Nothing was committed." | "Tests didn't run because the change was blocked before applying." |
| Blocked for safety — protected file | **Change was blocked — protected file** | "The change tried to modify a protected file, so Pipewright refused it. Nothing was committed." | "Tests didn't run because the change was blocked before applying." |
| No change was produced | **No change was produced** | "Pipewright's change produced no edits — it may already be present in the repo. Nothing was committed." | "Tests didn't run because there was no change to test." |
| Repo was not ready | **Repo wasn't ready for this change** | "Your working tree had uncommitted changes, so Pipewright didn't apply anything (it needs a clean tree to roll back safely). Commit or stash, then retry. Nothing was committed." | "Tests didn't run because the change was never applied." |
| Unexpected failure | **Something went wrong applying this change** | "An unexpected error stopped this change. It was rolled back and nothing was committed. See diagnostic details below." | *(none — we do not know; stay silent rather than guess)* |

The "Unexpected failure" row deliberately emits **no** test note, matching the
existing `tests: 'unknown'` behavior (return `null`). We never guess about test
execution.

## 5. Retry-unavailable framing

Retry-eligibility is an action property, not an account of what happened. The
headline must always describe *what happened*. When the backend reports retry is
unavailable, surface that as a **separate explanatory sub-line**, e.g.:

> This kind of failure can't be retried automatically — review the details to
> decide the next step.

The run-level panel must never render a retry affordance the backend gate would
reject. The single source of truth stays the operator-state primary action
(`retry_patch`) derived from `evaluate_patch_retry_eligibility`; the UI only
mirrors it.

## 6. Test output surfacing design

For `TEST_FAILURE_AFTER_APPLY`, the user's first question is "what failed?" The
design goal is to answer that without dumping a wall of pytest output.

Layering:

1. **Headline + test note** (§4): tests ran and failed; change rolled back.
2. **Short summary line** when available: e.g. "3 tests failed." The API already
   exposes `test_validation.failed_tests` (`frontend/src/api/client.ts`); prefer
   wiring that through over inventing a new field. A first-failing-test name may
   be added if it is cheaply available, but is optional.
3. **Collapsed raw output**: the existing `technical_details` field (sanitized
   and capped at `MAX_TECHNICAL_DETAILS_CHARS`) stays behind the existing "View
   details" toggle in `PatchFailureBanner`. It is audit-grade, never primary.

Constraints for the implementing PR (#40D):

- Raw pytest text must stay behind a disclosure, not in the headline.
- It must remain sanitized (no secrets) and length-capped — do not raise the
  cap.
- Absence of a structured count must degrade gracefully to the honest test note,
  never to a fabricated number.

## 7. Weak / no / unknown test guidance

Three honest states, none of which may ever imply tests passed:

- **No tests detected / no tests run**: say so plainly — "No tests ran for this
  change." Do not imply success.
- **Weak validation** (tests ran but did not meaningfully exercise the change):
  the existing acknowledgement flow already states "Tests did not meaningfully
  run, or no tests were configured." Keep that language; it must remain a
  required acknowledgement before final approval, not a silent pass.
- **Unknown / unverified** (`UNKNOWN_PATCH_FAILURE`, or evidence we cannot
  classify): stay silent about test outcome rather than guessing — emit no test
  claim at all.

This guidance is a copy/clarity polish (#40E). It must not relax the
final-approval acknowledgement gate or downgrade a weak/unknown verdict to a
strong one.

## 8. Backend / read-model needs to verify (for #40B)

The run-level title is server-computed, so the core change is backend. Before
implementing, verify:

- `OperatorStateContext` (`backend/pipeline/operator_state.py`) gains a
  `failure_type: str | None` field, threaded from the route that already loads
  the `PatchFailureReport` out of `chunks.completion_summary`
  (`patch_failure_report_from_completion_summary` already parses it). No schema
  change is required — the failure report already lives in the existing
  `completion_summary` JSON.
- `_patch_failure_state()` branches its title, explanation, and the `patch`
  safety-check detail on the family grouping in §3.
- The explanation for `TEST_FAILURE_AFTER_APPLY` is corrected: it must say tests
  **ran and failed**, never "tests did not run." (Truthfulness fix, not
  cosmetic.)
- `_decision_reason(...)` output that feeds a safety-check `detail` is mapped to
  human copy; no stable reason identifier (e.g. `disallowed_failure_type`) leaks
  as user-facing prose. The partial humanizer map in `RunDetailPage.tsx` must be
  completed to cover every `evaluate_patch_retry_eligibility` reason (#40C).

## 9. Frontend-only opportunities

These need no backend change and can ship independently:

- Demote the `failure_type` badge and the retry-availability text to secondary
  styling so they never read as the headline.
- Keep raw `technical_details` and attempt history collapsed by default
  (existing "View details" pattern).
- Render the humanized retry-unavailable reason from the existing map.

Anything that changes the **run-level operator title** is not frontend-only,
because that title originates in `operator_state.py`.

## 10. Safety invariants (must hold across all #40 PRs)

- Never imply code was committed when a rollback happened. Every failure family's
  copy states "Nothing was committed."
- Never imply tests passed when they failed, were weak, or were unknown.
- Never imply tests did not run when they ran and failed
  (`TEST_FAILURE_AFTER_APPLY`).
- Do **not** loosen retry eligibility. Do **not** add `TEST_FAILURE_AFTER_APPLY`
  (or scope/forbidden) to `_HUMAN_RETRYABLE_FAILURE_TYPES`. The honest fix is
  copy, not a new Retry button.
- Never offer a retry affordance the backend gate would reject; the
  operator-state `retry_patch` action stays the single source of truth.
- Never expose raw enum names as primary user-facing copy. Enums and raw output
  stay as demoted, collapsible diagnostics — visible for audit, never hidden
  completely.
- Never bypass chunk-plan approval, final approval, or scope guard. Never create
  a PR from a failed or unapproved state. Never auto-commit or auto-merge.

## 11. Smoke scenarios (for #40F)

Each verifies: run-level title matches the family, the test note is honest, no
committed implication, no retry affordance unless the backend gate allows it,
and raw enum/output appears only in collapsed diagnostics.

1. `TEST_FAILURE_AFTER_APPLY` → "Tests failed after the change was applied";
   "Tests ran and failed"; no Retry button (ineligible); enum only in badge.
2. `PATCH_DOES_NOT_APPLY` → "Code change couldn't be applied"; "Tests didn't
   run"; Retry enabled (eligible) and Re-index offered.
3. `TARGET_MISSING` → could-not-be-applied family; Re-index + Retry eligible.
4. `PATCH_PARTIAL_APPLY_BLOCKED` → could-not-be-applied family; "nothing applied
   to keep the repo consistent."
5. `STALE_INDEX_OR_FILE_CHANGED` → could-not-be-applied family; stale-index hint
   shown.
6. `SCOPE_VIOLATION` → "Change was blocked — outside approved scope"; framed as a
   safety stop; no plain Retry. (Scope-expansion path applies only when a pending
   request exists, via `AWAITING_SCOPE_APPROVAL`.)
7. `FORBIDDEN_FILE` → "Change was blocked — protected file"; no Retry.
8. `NO_CHANGES` → "No change was produced"; benign framing; no Retry.
9. `DIRTY_WORKTREE` → "Repo wasn't ready for this change"; commit/stash guidance.
10. `UNKNOWN_PATCH_FAILURE` → "Something went wrong applying this change"; no test
    note; details link present.
11. Retry-cap-exhausted on an otherwise-eligible type → title still describes
    what happened; retry-unavailable reason appears as a sub-line.
12. Rollback left the tree dirty (`manual_intervention_needed`) → "Manual
    intervention needed" stays prominent regardless of family.

## 12. Follow-up PR roadmap

- **#40A — Failure-state UX audit/design doc.** This document. Docs-only.
- **#40B — Failure-type-aware operator_state / read-model copy.** Add
  `failure_type` to `OperatorStateContext`; branch `_patch_failure_state` title,
  explanation, and safety-check detail on the family grouping; fix the
  `TEST_FAILURE_AFTER_APPLY` "tests did not run" claim. Pure read-model change +
  unit tests for all 11 types. No eligibility change. This PR alone fixes the
  smoke bug at its source.
- **#40C — Retry-unavailable reason humanization / enum cleanup.** Map every
  `evaluate_patch_retry_eligibility` reason to human copy; complete the
  `RunDetailPage` humanizer; demote the `failure_type` badge. Add a test that no
  raw identifier reaches a title or detail.
- **#40D — Test failure output surfacing.** Thread `test_validation.failed_tests`
  into the report; show "N tests failed" above the collapsed raw output. Keep raw
  output sanitized, capped, and behind the disclosure.
- **#40E — Weak / no / unknown test guidance polish.** Clarify the no-tests,
  weak-validation, and unknown-evidence copy without relaxing the acknowledgement
  gate.
- **#40F — Failure-state smoke docs / closeout.** Add the §11 scenarios to
  `docs/testing/`, mirroring the existing `patch-failure-recovery-smoke.md`
  style, and record the closeout.

Sequence rationale: #40B removes the active falsehood and is the smallest
high-value change; #40C closes the enum-leak invariant; #40D–#40E are
progressive trust polish that depend on #40B's type-awareness; #40F locks
behavior with repeatable smokes.
