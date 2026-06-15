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

## Controlled Env-Gated Soak

Default behavior remains disabled. For a local/dev soak only, set the env var
before starting the backend/test process:

```powershell
$env:PIPEWRIGHT_MEMORY_POSTRUN_HYGIENE_ENABLED = "true"
```

Then run one successful `local_only` or PR success flow and confirm:

1. Pending suggestions are created automatically after successful completion.
2. The Run Detail digest appears with the correct pending count.
3. Project Memory shows the pending suggestions for human review.
4. Approve/reject still works through Project Memory only.
5. Reloading Run Detail does not create new suggestions.
6. Repeating an idempotent complete/PR action does not create duplicates.

Unset the flag after the soak:

```powershell
Remove-Item Env:PIPEWRIGHT_MEMORY_POSTRUN_HYGIENE_ENABLED
```

Unset, false-ish, and invalid values keep automatic post-run hygiene disabled.
Do not use this as default-on activation; it is a controlled local/dev soak path.

Before running the normal unit suite, unset
`PIPEWRIGHT_MEMORY_POSTRUN_HYGIENE_ENABLED`. The env-gated soak flag is read at
policy import time and is intended only for controlled local/dev smoke.

## Activation Smoke Result — 2026-06-15

This smoke used a throwaway SQLite database via `PIPEWRIGHT_DB_PATH` under
`.pytest_tmp/row16_activation_smoke/`. The flag was enabled only in-process by
setting `policy.MEMORY_POSTRUN_HYGIENE_ENABLED = True` in the smoke script; no
source file was edited and no default activation was committed.

Run mode: `local_only`, through the real `push_and_create_pr()` completion path.
Local git probes were patched inside the smoke process to avoid push/GitHub
activity while still exercising the post-lock success completion path.

Observed:

- Successful completion returned `status="complete"`.
- Automatic post-run trigger created 2 pending suggestions with
  `suggested_by="postrun_auto"`.
- Before review, active memory facts remained 0.
- Before review, approval gates remained 0.
- `GET /api/v1/runs/{run_id}/memory-suggestions` returned 200 with
  `pending_count=2`.
- Repeating the digest read kept `pending_count=2` and did not create new rows.
- The Project Memory suggestions endpoint returned the same 2 pending
  suggestions for review.
- Repeating the idempotent complete action returned `status="complete"` and did
  not create duplicates; total suggestions remained 2.
- Explicit Project Memory review worked: approving one suggestion and rejecting
  one suggestion succeeded. After review, the run digest returned
  `pending_count=0`; one active fact existed due to explicit approval, not
  automatic promotion; approval gates remained 0.

UI note: this smoke did not launch a browser. The Run Detail digest API returned
the non-empty payload that `RunMemorySuggestionsDigest` renders, and the component
copy/CTA were inspected in source. A browser smoke remains useful before any
default-on activation.

Rejected-content reappearance:

- Tested in a separate throwaway DB.
- A handoff suggestion rejected from run A reappeared as a pending suggestion
  from run B when the later run proposed the same content.
- This confirms the known behavior: rejection suppression is scoped to the same
  `source_run_id`; it is not project-wide.

Recommendation after smoke: keep the shipped default `False`. If the maintainer
wants activation next, prefer an env/config override with default `False` for a
controlled local/dev soak before considering default-on PR-C.
