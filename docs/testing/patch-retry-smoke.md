# Patch Retry Backend Smoke Checklist

## Purpose

This document records backend/API smoke validation for Patch Failure Recovery v2
through #26D3b. It is focused on the backend retry path and does not imply that
frontend retry controls are available yet.

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

## Deferred Work

- #26E frontend retry UI / patch failure banner action
- Display attempt history in UI
- Show recovered_patch_review marker in UI
- Optional retry-with-instruction later
- Auto retry remains deferred
- Scope expansion recovery remains #27
- Stronger test validation remains #28
