# Failure-State UX Smoke Checklist & Closeout (#40)

## Purpose

This document is the manual smoke checklist and closeout record for #40
Failure-state UX cleanup. It verifies that the run-level failure copy is
truthful and failure-type-aware, that retry-unavailable reasons read as plain
language, that parsed failed-test counts surface when (and only when) real
evidence exists, and that weak/no/unknown test guidance is clear and honest.

Design source of truth: [`failure-state-ux-cleanup.md`](../design/failure-state-ux-cleanup.md).

Docs-only: this file changes no backend, frontend, schema, API, test, or runtime
behavior.

## Completed Phases

- **#40A** — failure-state UX audit/design doc.
- **#40B** — failure-type-aware `operator_state` / read-model copy.
- **#40C** — retry-unavailable reason humanization / enum cleanup.
- **#40D** — test-failure output surfacing (parsed failed-test counts).
- **#40E** — weak/no/unknown test guidance polish.
- **#40F** — this smoke checklist / closeout.

## What #40 Fixed

- **Top-level Run Detail copy distinguishes failure families.** The Operator
  Attention panel was failure-type-blind and showed "Code change could not be
  applied — retry unavailable" for every patch failure, including
  `TEST_FAILURE_AFTER_APPLY` (where the patch *did* apply, tests ran, tests
  failed, and the patch was rolled back). Titles now describe what actually
  happened, grouped into user-facing families
  (see [taxonomy](#failure-family-reference)).
- **Retry-unavailable reasons are humanized.** Stable identifiers such as
  `disallowed_failure_type`, `human_retry_cap_exhausted`, `chunk_not_failed`,
  and `dirty_worktree` no longer appear as primary user copy; they are mapped to
  plain sentences. The raw identifiers remain machine-stable for branching and
  audit.
- **Parsed failed-test counts are surfaced for `TEST_FAILURE_AFTER_APPLY`.** When
  the persisted test validation already parsed counts, the patch-failure banner
  shows a compact "N of M tests failed." line. No new pytest parsing was added;
  it reads only already-persisted evidence and degrades to the plain sentence
  when no reliable count exists.
- **Weak/no/unknown test guidance is clearer and truthful.** The runtime
  test-validation banner now gives action-oriented, honest copy per verdict and
  correctly states that weak/none verdicts must be acknowledged before final
  approval (replacing the stale "informational only / does not block approval"
  line).

## Safety Invariants (must remain true)

- `TEST_FAILURE_AFTER_APPLY` is **still not retryable**. #40 changed copy, never
  eligibility.
- **No retry eligibility change.** `_HUMAN_RETRYABLE_FAILURE_TYPES` and
  `evaluate_patch_retry_eligibility` are unchanged.
- **No classifier or test-execution change.** `classify_test_run` and the tester
  path are untouched; persisted verdict semantics are unchanged.
- **No final-approval gate change.** The weak/none acknowledgement requirement
  (#28F/#28G) and final-approval blocking are unchanged. Acknowledgement copy is
  display-only and enforces nothing.
- **No auto-commit, no auto-merge, no PR from a failed or unapproved state.** A
  failed/rolled-back chunk never commits, and no PR is created from it.
- **Raw diagnostics remain available but are never primary user copy.** The
  `failure_type` enum (badge), `technical_details`, attempt history, and the raw
  retry reason stay reachable for audit — demoted behind clearer explanations or
  disclosures, never hidden completely and never the headline.

## Failure Family Reference

User-facing family (run-level title) per backend `PatchFailureType`:

| `PatchFailureType` | Family / Operator title | Tests ran? |
|--------------------|-------------------------|-----------|
| `PATCH_DOES_NOT_APPLY` | Code change couldn't be applied | No |
| `PATCH_MALFORMED` | Code change couldn't be applied | No |
| `PATCH_PARTIAL_APPLY_BLOCKED` | Code change couldn't be applied | No |
| `TARGET_MISSING` | Code change couldn't be applied | No |
| `STALE_INDEX_OR_FILE_CHANGED` | Code change couldn't be applied | No |
| `TEST_FAILURE_AFTER_APPLY` | Tests failed after the change was applied | Yes — failed |
| `SCOPE_VIOLATION` | Change was blocked — outside approved scope | No |
| `FORBIDDEN_FILE` | Change was blocked — protected file | No |
| `NO_CHANGES` | No change was produced | No |
| `DIRTY_WORKTREE` | Repo wasn't ready for this change | No |
| `UNKNOWN_PATCH_FAILURE` | Something went wrong applying this change | Unknown |

## Manual Smoke Scenarios

For each scenario, verify the four columns. "Operator title" is the run-level
Operator Attention headline. "Test-honesty line" is the sentence shown about
test execution. "Retry?" is whether an enabled Retry affordance should appear.
"Raw enum/details" confirms the `failure_type` and technical output stay
diagnostic (badge / disclosure), never the primary explanation.

### Patch failure scenarios

| # | Scenario | Operator title | Test-honesty line | Retry? | Raw enum/details |
|---|----------|----------------|-------------------|--------|------------------|
| 1 | `TEST_FAILURE_AFTER_APPLY` | "Tests failed after the change was applied" | "Tests ran and failed, so the change wasn't kept." Must **never** say tests did not run. Compact "N of M tests failed." appears when counts parsed. | **No** (not human-retryable) | Diagnostic only (badge + View details) |
| 2 | `PATCH_DOES_NOT_APPLY` | "Code change couldn't be applied" | "Tests didn't run because the change was never applied." | **Yes** (eligible) + Re-index offered | Diagnostic only |
| 3 | `TARGET_MISSING` | "Code change couldn't be applied" | "Tests didn't run because the change was never applied." | **Yes** (eligible) + Re-index offered | Diagnostic only |
| 4 | `PATCH_PARTIAL_APPLY_BLOCKED` | "Code change couldn't be applied" | "Tests didn't run because the change was never applied." | **Yes** (eligible) | Diagnostic only |
| 5 | `SCOPE_VIOLATION` | "Change was blocked — outside approved scope" | "Tests didn't run because the change was blocked before applying." | **No** plain retry (safety stop; scope-expansion path applies only when a pending request exists) | Diagnostic only |
| 6 | `FORBIDDEN_FILE` | "Change was blocked — protected file" | "Tests didn't run because the change was blocked before applying." | **No** | Diagnostic only |
| 7 | `NO_CHANGES` | "No change was produced" | "Tests didn't run because there was no change to test." | **No** | Diagnostic only |
| 8 | `DIRTY_WORKTREE` | "Repo wasn't ready for this change" | "Tests didn't run because the change was never applied." Guidance: commit/stash then retry. | **No** automatic retry from operator panel | Diagnostic only |
| 9 | `UNKNOWN_PATCH_FAILURE` | "Something went wrong applying this change" | No test claim (honest silence — we don't know). | **No** | Diagnostic only; "See details" present |
| 10 | Retry cap exhausted (e.g. `PATCH_DOES_NOT_APPLY` after retries used) | Title still describes what happened (e.g. "Code change couldn't be applied") — **not** "retry unavailable" | As per the underlying family | **No** | Retry-unavailable reason shown as a humanized sub-line, not the title; raw reason stays diagnostic |

Notes:

- The retry-unavailable reason in scenarios 1, 5–10 must be **plain language**
  (e.g. "This kind of failure cannot be retried automatically.", "The retry
  limit for this failure has already been reached."). A raw identifier such as
  `disallowed_failure_type` must never appear as the blocked-action or
  safety-check copy.
- In scenario 1, the "N of M tests failed." line appears **only** when the
  persisted `test_validation` has `counts_parsed = true` and a failed count
  ≥ 1. With no parsed count (e.g. truncated output, collection error,
  `failed_tests = 0`), it must degrade to the plain sentence — never a
  fabricated "0 tests failed."

### Test-validation guidance scenarios

| # | Scenario (verdict) | Banner headline | Guidance / honesty | Final-approval note |
|---|--------------------|-----------------|--------------------|---------------------|
| 11 | Weak test validation (`weak`) | "Tests ran, but they may not prove this change works." | "Review the command and output before final approval — you may need a stronger, project-specific test command." Never implies tests passed. | "Final approval requires acknowledging this limitation first." (gate unchanged) |
| 12 | No meaningful tests (`none`) | "No meaningful tests ran for this change." | "Pipewright cannot verify this change from tests. Continue only if you intentionally accept that limitation; adding tests or a real test command would give stronger evidence." | "Final approval requires acknowledging this limitation first." |
| 13 | Unknown/unverified (`unknown`) | "Test evidence could not be classified." | "Tests may have run, but Pipewright could not determine how much confidence they provide. Review the output manually." | **No** acknowledgement note (unknown does not require ack and does not block) |

Notes:

- The acknowledgement note appears only when
  `test_validation.requires_acknowledgement === true` (weak/none), matching the
  existing #28F/#28G gate. `unknown` never shows it.
- `strong` is unaffected by the failure surfaces but should read as meaningful
  evidence, not proof of correctness.
- The acknowledgement panel (`TestValidationAckPanel`) behavior is unchanged —
  the same checkbox/button flow and the same gate own enforcement.

## Suggested Validation Commands

Backend (read-model copy & humanization — #40B/#40C):

```powershell
python -m pytest backend/tests/test_operator_state.py backend/tests/test_patch_failures.py backend/tests/test_chunk_ack_read_model.py -q
```

Targeted retry-route sanity (eligibility unchanged):

```powershell
python -m pytest backend/tests/test_chunk_retry_route.py -q
```

Frontend (count surfacing & guidance copy — #40D/#40E), if doing manual
verification:

```powershell
cd frontend
npm.cmd run build
npx.cmd eslint src/components/PatchFailureBanner.tsx src/components/RuntimeTestValidationBanner.tsx src/utils/patchFailure.ts
```

Whitespace / conflict-marker check for any doc edits:

```powershell
git diff --check
```

## Closeout Criteria

#40 is considered complete when all of the following hold:

- [ ] No misleading "tests did not run" copy for `TEST_FAILURE_AFTER_APPLY`; the
      run-level title is test-failure-specific.
- [ ] No raw retry reason identifier appears as primary user-facing copy; all
      retry-unavailable reasons are humanized.
- [ ] The failed-test count line appears **only** when parsed evidence exists and
      degrades gracefully otherwise.
- [ ] Weak/none final-approval limitation is stated truthfully, and the
      acknowledgement gate behavior is unchanged.
- [ ] Safety invariants above all still hold (no eligibility, classifier,
      execution, gate, commit, merge, or schema change).

After this doc merges and the optional manual smoke above passes, #40
Failure-state UX cleanup can be marked complete.
