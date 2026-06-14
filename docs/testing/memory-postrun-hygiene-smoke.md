# Memory Post-Run Hygiene Smoke

Manual smoke checklist for Row 16 PR-B read-only digest / observability.

## Setup

1. Keep `MEMORY_POSTRUN_HYGIENE_ENABLED=False`.
2. Use the existing manual "Generate memory suggestions from this run" action on
   Run Detail, or call `POST /api/v1/runs/{run_id}/memory-suggestions/generate`,
   to create pending suggestions.
3. Reload the Run Detail page for that run.

## Expected

1. The read-only digest appears only when the run has pending suggestions.
2. The digest shows the correct pending count.
3. The copy includes:
   - pending
   - review
   - not used by future AI runs unless approved
   - Nothing was added to memory automatically
4. The copy does not say:
   - added to memory
   - auto-saved
   - learned automatically
5. Styling is neutral/slate, not warning/error.
6. "Review in Project Memory →" navigates to
   `/memory?projectId=<project_id>`.
7. Viewing or reloading the digest does not create new suggestions, active facts,
   or approval gates.
8. A run with zero pending suggestions shows no digest card.

## Non-Goals

- Do not enable `MEMORY_POSTRUN_HYGIENE_ENABLED`.
- Do not approve or reject suggestions from Run Detail.
- Do not expect generated/skipped/blocked/floored/capped counts in the digest;
  those transient counts are only returned by the manual generate response.
