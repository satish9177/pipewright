# Patch Retry Smoke Checklist

## Purpose

This document records manual smoke validation for Patch Failure Recovery v2. It
has two parts: the backend/API retry path (through #26D3b) and the frontend retry
UI added in #26E (Retry button, recovery attempt history, recovered_patch_review
marker). The backend smoke is below; the frontend smoke is in its own section.

## Completed Backend Phases

- #26A design doc
- #26B shared dry-run/apply evaluator and diagnostics
- #26C attempt history and failure_report_id
- #26D1 retry eligibility/data helpers
- #26D2 internal retry execution helper
- #26D3a branch verify-only pre-check
- #26D3b public retry route and HTTP mapping

## Manual Smoke Results

- A PATCH_DOES_NOT_APPLY failure produced a failure_report_id.
- POST /runs/{run_id}/chunks/{chunk_number}/retry accepted the current failure_report_id.
- Retry executed through the backend route.
- Safe re-failure produced a new failure_report_id.
- Second retry on disallowed failure type returned retry_ineligible 422.
- A fresh retry smoke succeeded through the intended path.
- Successful recovery paused at awaiting_chunk_approval.
- Recovered code did not commit automatically.
- Existing approval path remains responsible for commit.

## Manual Retry Command Example

```powershell
$runId = "<RUN_ID>"
$failureReportId = "<FAILURE_REPORT_ID>"

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8001/runs/$runId/chunks/1/retry" `
  -ContentType "application/json" `
  -Body (@{
    failure_report_id = $failureReportId
  } | ConvertTo-Json)
```

If backend is mounted under /api/v1 in a given environment, use that prefix.
Current manual smoke used the unprefixed route.

## Expected Results

- retry_ineligible 409 for wrong branch, stale id, or dirty tree.
- retry_ineligible 422 for disallowed failure type, cap, unmet dependency, or malformed report.
- status failed plus a new failure_report_id when retry executes but safely fails.
- status awaiting_chunk_approval when retry succeeds.

## Safety Invariants Verified

- No auto retry.
- No frontend action yet.
- No auto checkout.
- No scope expansion.
- No files_expected mutation.
- No commit on retry success.
- Recovered chunks pause at awaiting_chunk_approval.
- Dependent chunks remain blocked until recovered chunk is approved/committed.
- Disallowed failure types remain blocked.
- Stale/disallowed retries are rejected safely.

## Frontend Smoke Checklist

The steps above validate the backend/API retry path. This section validates the
frontend UI added in #26E (Retry button, recovery attempt history, and the
recovered_patch_review marker). It is manual and complements — does not replace —
the backend smoke above.

### Setup

1. Start the backend (`http://localhost:8001`) and the frontend dev server.
2. Create or reuse a run whose chunk fails with `PATCH_DOES_NOT_APPLY` and has a
   `failure_report_id` (any human-retryable failure type works; the manual
   backend smoke above produces one). Open the run in the UI and locate the
   failed chunk in the Chunk Plan panel.

### Retry button + attempt history

3. Confirm the patch failure banner (`PatchFailureBanner`) shows a **Retry**
   button. It appears only when the chunk status is `failed`, the report has a
   `failure_report_id`, and `suggested_actions` includes `retry` or
   `retry_with_instruction`.
4. Confirm the **Recovery attempts** section lists attempt #1 (the initial
   failed apply) with its mode/outcome.
5. Click **Retry**.
6. Confirm the button disables and its label changes to **Retrying…** while the
   request is pending.

### Outcome A — retry safely fails again

7. If the retry executes but the patch fails again:
   - The banner refreshes with the **new** `failure_report_id` (the run/runChunks
     queries are invalidated automatically).
   - **Recovery attempts** shows an appended `human` attempt.
   - The inline message reads "Retry ran but the patch failed again."

### Outcome B — retry succeeds

8. If the retry succeeds:
   - The chunk moves to `awaiting_chunk_approval`.
   - A recovered_patch_review marker shows **"Recovered patch ready for
     review"** with the supporting line "Retry applied and tests passed. Review
     the recovered patch before committing."
   - Attempt history is shown (initial + recovered attempt).
   - If `weak_test_warning` is set, an amber warning appears.
   - The existing chunk approval UI still renders and remains responsible for
     approve/commit — there are **no** new approve/commit controls in the marker.
   - No commit happens automatically on retry success.

### Rejection messages

9. Confirm safe backend rejections surface a clear inline error (no raw JSON):
   - wrong branch → "Checkout the run branch and try again." (or the backend
     detail).
   - stale `failure_report_id` → "This failure report is stale. Refresh the run
     and try again."
   - dirty working tree → "The working tree is dirty. Clean it before retrying."
   - disallowed failure type → "This failure type cannot be retried
     automatically."

### Display guarantees

10. Confirm a `recovered_patch_review` completion summary renders the marker and
    **never** dumps raw JSON.
11. Confirm there is **no** retry-with-instruction UI (no instruction input /
    button) yet.
12. Confirm there is **no** scope-expansion UI (no `files_expected` editing or
    scope-widening controls).

### Frontend validation commands

```powershell
cd frontend
npm.cmd run build
npm.cmd run lint
```

`npm.cmd run build` runs `tsc -b` (type-check) then `vite build`. Lint may report
pre-existing errors in untouched files (current baseline: 5 errors in
`Layout.tsx`, `ProjectSettingsPanel.tsx`, `ui/badge.tsx`, `ui/button.tsx`,
`useRunEvents.ts`); changed files should add no new lint errors.

## Deferred Work

- Optional retry-with-instruction later
- Auto retry remains deferred
- Scope expansion recovery remains #27
- Stronger test validation remains #28
