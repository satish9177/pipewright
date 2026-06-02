# Memory M2 Smoke Checklist

## Preconditions

- Backend running.
- Frontend running.
- On latest develop with #21A–#21F merged.
- A project exists and has a repo path.
- At least one terminal run exists, or create one.

## Backend test commands

These are the commands already used while building the M2 PRs:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\test_memory.py -v
venv\Scripts\python.exe -m pytest backend\tests\test_memory_api.py -v
venv\Scripts\python.exe -m pytest backend\tests\test_memory_bootstrap.py -v
venv\Scripts\python.exe -m pytest backend\tests\test_memory_run_outcome_suggestions.py -v
venv\Scripts\python.exe -m pytest backend\tests\test_memory_prompt_builder.py -v
venv\Scripts\python.exe -m pytest backend\tests\ -v -k "memory"
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit
```

## Frontend test command

```powershell
cd frontend
npm.cmd run build
```

Optionally:

```powershell
npx.cmd tsc --noEmit
```

## Manual smoke 1 — completed run

Steps:

1. Open a completed run.
2. Confirm the generate memory suggestions section appears.
3. Click generate.
4. Confirm counts appear.
5. Confirm the mini-list appears if suggestions were generated.
6. Click Review in Project Memory.
7. Confirm pending suggestions appear.
8. Approve one suggestion.
9. Confirm an active memory fact appears.
10. Generate again for the same run.
11. Confirm an idempotent result, usually generated 0 / skipped > 0.

Expected:

- No active fact before approval.
- Suggestions remain pending until approved.
- Duplicate generation does not spam suggestions.

## Manual smoke 2 — edit-and-approve

Steps:

1. Open Project Memory suggestions.
2. Click Edit & approve.
3. Modify the text.
4. Approve the edited content.
5. Confirm the active fact uses the edited text.
6. Confirm the original suggestion content is preserved if visible through the
   API/UI.

Expected:

- Edited content is saved as the active fact.
- Backend validation still applies to the edited content.

## Manual smoke 3 — reject

Steps:

1. Pick a pending suggestion.
2. Reject with a reason.
3. Confirm the status is rejected.
4. Confirm the rejected suggestion does not become active memory.
5. Confirm the rejected suggestion cannot be approved later.

## Manual smoke 4 — failed run / patch failure

Steps:

1. Open a failed run with a patch failure.
2. Generate memory suggestions.
3. Confirm an operational pending suggestion appears.
4. Confirm the suggestion does not include a raw stack trace or logs.
5. Confirm `source_type` / `source_run_id` / `risk_level` / `rationale` are
   visible.

Expected:

- Failed run suggestions are pending-only.
- No raw logs or stack traces.

## Manual smoke 5 — rejected run / rejected approval

Steps:

1. Open a rejected run, or a run with a rejected approval reason.
2. Generate memory suggestions.
3. Confirm a rejected-approach style suggestion is pending.
4. Approve or reject it manually.

Expected:

- The rejection reason is sanitized / truncated.
- Unsafe rejection content is blocked or skipped.

## Manual smoke 6 — hard blocker

Steps:

1. Try to create or approve memory containing:
   - skip approval
   - auto-merge
   - `C:\Users\satish\secret`
   - a Python traceback
   - a large fenced code block
2. Confirm the backend rejects it.

Expected:

- Unsafe memory cannot become active.
- The UI surfaces the validation error.

## Manual smoke 7 — role preview

Steps:

1. Create approved facts in different categories if possible.
2. Open the prompt preview for triage / planner / coder / reviewer.
3. Confirm categories differ by role.
4. Confirm the advisory wrapper is present.
5. Confirm stale / archived facts do not appear.

Expected:

- Role-specific memory blocks differ.
- Memory says it is advisory.
- Source / user / tests / safety override memory.

## Manual smoke 8 — terminal visibility

Steps:

1. Open an in-progress run.
2. Confirm the generate button is hidden.
3. Open a complete / failed / rejected run.
4. Confirm the generate button appears.

Expected:

- Only terminal runs can generate suggestions.

## Known limitations

- No pgvector.
- No LLM extraction.
- No generic conflict lifecycle.
- Reviewer runtime does not exist yet.
- Some run-outcome lessons use the current fallback category because dedicated
  categories do not exist.
- Blocked suggestions are counted, not shown as approvable rows.
- Generation is deterministic and conservative, so it may skip useful but
  ambiguous memories.

## Done criteria

- Backend memory tests pass.
- Frontend build passes.
- Manual smoke confirms generation / review / approve / edit / reject.
- No unsafe memory can be saved.
- No active memory is created without human approval.
