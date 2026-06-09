# Memory Trust UI Smoke Checklist (M3E)

Manual smoke guide for the M3E frontend memory trust UI. This is a closeout
checklist for UI behavior only: it documents what to verify after the M3D
backend lifecycle work is surfaced in the frontend.

Related docs:

- [M3 trust lifecycle design](../design/memory-m3-trust-lifecycle.md)
- [Memory provenance smoke checklist](./memory-provenance-smoke.md)
- [Memory lifecycle smoke checklist](./memory-lifecycle-smoke.md)
- [README quick local setup](../../README.md#quick-local-setup)

## 1. Purpose

M3E closes the manual UI loop over the memory trust lifecycle:

- M3E1 adds Project Memory history visibility and plain-English status labels
  for in-use, possibly outdated, retired, and replaced memory.
- M3E2 adds the Run Detail "What the AI was given" viewer so operators can
  inspect exactly what approved memory was used during a run.
- M3E3 adds confirmation-modal UI for human lifecycle actions: mark memory as
  possibly outdated, replace one in-use memory with another, and approve a
  pending suggestion while replacing an old in-use memory.

This checklist verifies the UI surfaces and copy that make those actions
human-controlled, auditable, and explicit.

## 2. Safety Guarantees

Confirm these guarantees remain true throughout the smoke:

- The backend remains the source of truth for project ownership, active-only
  preconditions, duplicate safety, edited-content validation, and lifecycle
  reason validation.
- Server-side revalidation still happens after every UI confirmation. Frontend
  affordances are convenience, not authority.
- No route or UI automatically saves, approves, marks memory as outdated,
  retires, replaces, resolves, or applies latest-wins behavior.
- No LLM, vector, embedding, semantic memory, or truth-decision behavior is
  introduced by these UI surfaces.
- No prompt format, memory injection rule, pipeline, run, Git, or GitHub runtime
  behavior changes.
- Memory-use snapshots remain read-only. Loading or refreshing Run Detail
  memory details never changes memory or run state.
- The Run Detail memory details panel has no action buttons for marking
  outdated, retiring, replacing, approving, or rejecting memory.
- Replaced, possibly outdated, and retired memory is not given to the AI because
  prompt memory still uses only active, non-stale facts.

## 3. UI Surfaces Covered

Project Memory, implemented by `ProjectMemoryPanel`, covers:

- In-use, possibly outdated, retired, and replaced memory notes.
- Status labels and tooltips:
  - `active`: `In use`, available as background context for future AI runs.
  - `stale`: `Possibly outdated`, not shown to the AI and kept for review.
  - `archived`: `Retired`, manually retired and kept for the record.
  - `historical`: `Replaced`, not shown to the AI and preserved for history.
- Change history:
  - Replaced memory can show `Replaced by your approval -> ...` when the
    replacement memory is loaded.
  - Active replacement memory can show `Replaced earlier note: ...` for older
    memories that point to it.
- Lifecycle actions for active memory: `Mark as possibly outdated`,
  `Replace this memory`, and `Retire memory`.
- Pending suggestion actions: `Approve`, `Edit & approve`, and
  `Approve & replace an old memory...`.
- Existing create, edit, verify, retire, approve, reject, preview, and refresh
  flows.

Run Detail, implemented by `RunMemoryProvenancePanel`, covers:

- A `Memory Used` section with a collapsed `What the AI was given` panel.
- Lazy, read-only memory-use fetch after selecting `Load what the AI was given`.
- Included entries labeled `Used in this run.`
- Excluded entries under `Not shown - and why`, with plain-English reasons such
  as `Not enough room this run.` and `Not used by this role.`
- Advisory duplicate and possible replacement analysis from the stored memory
  snapshot.
- Explicit copy that observations are read-only and the system did not change
  memory.
- Explicit replacement caution: `Newer does not mean true. You decide which
  memory to use.`
- No mutation actions.

## 4. Manual Smoke Setup

1. Follow [README quick local setup](../../README.md#quick-local-setup) and
   start the backend and frontend. On Windows, use `npm.cmd` if PowerShell
   blocks `npm.ps1`.
2. Open the frontend and select a project that has Project Memory enabled.
3. Ensure the project has at least two active memory notes. Use a deliberately
   old/new pair so lineage is easy to recognize, for example `Backend uses
   Flask.` and `Backend uses FastAPI.`.
4. Ensure the project has at least one pending memory suggestion.
5. Optional, for provenance checks: run a tiny task that reaches at least the
   planner and coder stages, then note the `run_id`.
6. Open browser devtools Network before testing Run Detail memory details. The
   memory-use endpoints should not be called until the panel is opened.

## 5. Checklist - Status Labels And Lineage

- [ ] On Project Memory, set the status filter to `all`.
- [ ] Confirm active memory shows the `In use` badge and tooltip that it is
      available as background context.
- [ ] Confirm possibly outdated memory shows `Possibly outdated` and the tooltip says it is
      not shown to the AI and nothing was deleted.
- [ ] Confirm retired memory shows `Retired` and the tooltip says it is no
      longer shown to the AI and kept for the record.
- [ ] Confirm replaced memory shows `Replaced` and the tooltip says it is not
      shown to the AI and preserved to show what changed.
- [ ] Confirm the list ordering keeps active facts first when the status filter
      is `all`.
- [ ] For a replaced memory with `superseded_by_fact_id`, confirm the UI shows
      either `Replaced by your approval -> ...` or `Replaced by another approved
      memory.`
- [ ] For an active replacement memory, confirm the UI can show
      `Replaced earlier note: ...` for replaced memories pointing to it.
- [ ] Confirm these lineage displays are read-only labels, not action shortcuts.

## 6. Checklist - Mark As Possibly Outdated UI

- [ ] Choose an active memory and select `Mark as possibly outdated`.
- [ ] Confirm the modal title is `Mark as possibly outdated`.
- [ ] Confirm the modal shows the exact memory content.
- [ ] Confirm the status row shows current `In use` and resulting
      `Possibly outdated`.
- [ ] Confirm the modal states that future runs will not be shown this memory
      and that code wins when memory and code disagree.
- [ ] Confirm the `Reason` input requires at least 4 characters before the
      `Mark as possibly outdated` confirmation can run.
- [ ] Submit a valid human reason and confirm the memory returns as
      `Possibly outdated`.
- [ ] Confirm the memory is absent from `Preview what the AI sees` and visible
      when the `Possibly outdated` status filter is selected.
- [ ] Repeat with an invalid reason and confirm the UI surfaces the backend
      validation error without changing the memory.
- [ ] Confirm non-active memory does not expose `Mark as possibly outdated`.

## 7. Checklist - Replace UI

- [ ] Choose an active old memory and select `Replace this memory`.
- [ ] Confirm the modal title is `Replace an older memory`.
- [ ] Confirm the modal labels the selected memory as `Older memory`.
- [ ] Confirm the replacement selector lists other active memories only.
- [ ] Select a replacement and confirm the modal labels it as
      `Replacement memory`.
- [ ] Confirm the modal says the older memory moves from `In use` to
      `Replaced` and the replacement memory remains `In use`.
- [ ] Confirm the modal says the old memory stops being used and that replacing
      does not remove it.
- [ ] Confirm the `Reason` input requires at least 4 characters before
      `Replace old memory` can run.
- [ ] Submit a valid reason and confirm the old memory becomes `Replaced` while
      the new memory remains `In use`.
- [ ] Confirm the direction is explicit by choosing an older/newer pair where
      `created_at` would not imply the desired winner. Only the selected old
      memory should become replaced.
- [ ] Confirm self-replacement is impossible through the selector.
- [ ] Confirm backend errors for inactive, missing, duplicate, or invalid
      choices appear in the modal without partially changing memory.

## 8. Checklist - Approve-And-Replace UI

- [ ] Choose a pending suggestion and select
      `Approve & replace an old memory...`.
- [ ] Confirm the modal title is `Approve a suggestion that needs review`.
- [ ] Confirm the suggestion content is editable and shows a 400-character
      counter.
- [ ] Confirm the old-memory selector lists active memory.
- [ ] Select an old active memory and confirm its exact content is shown under
      `Selected old memory`.
- [ ] Confirm the modal says the suggestion will become a new active memory
      and the selected old memory will become `Replaced` and stop being used.
- [ ] Confirm the modal says nothing is deleted and backend validation remains
      the source of truth.
- [ ] Confirm `Reason` requires at least 4 characters and the edited content
      must be valid before `Approve & replace an old memory` can run.
- [ ] Submit without editing and confirm the suggestion becomes approved, a new
      active memory appears, and the old memory becomes `Replaced`.
- [ ] Repeat with edited content and confirm the new active memory uses the edited
      text.
- [ ] Try unsafe or duplicate edited content and confirm the UI surfaces the
      backend error while the suggestion remains pending and the old memory
      remains active.
- [ ] Confirm the ordinary `Approve` and `Edit & approve` flows still work and
      do not replace any old memory.

## 9. Checklist - Run Detail Memory-Use Viewer

- [ ] Open a run detail page and find the `Memory Used` section.
- [ ] Confirm the `What the AI was given` panel is collapsed by default.
- [ ] In devtools Network, confirm the memory endpoints are not called until
      `Load what the AI was given` is selected.
- [ ] Select `Load what the AI was given` and confirm the button changes to
      `Hide details`.
- [ ] Confirm role and chunk filters are available, plus a `Refresh` button.
- [ ] Confirm included entries are labeled `Used in this run.`
- [ ] If excluded entries exist, expand the details section and confirm the copy
      says they were not shown to the AI.
- [ ] Confirm event metadata includes role, chunk, attempt, memory space limit,
      allowed memory types, and `Snapshot fingerprint` when recorded.
- [ ] Confirm status badges reflect `status_at_injection`, not the memory's later
      current status.
- [ ] Confirm `Advisory observations` states that observations are read-only and
      the system did not change memory.
- [ ] Confirm duplicate candidates are labeled `Possible duplicate`.
- [ ] Confirm replacement candidates are labeled
      `Possible replacement candidate` and include
      `Newer does not mean true. You decide which memory to use.`
- [ ] Confirm the panel contains no mark-outdated, retire, replace, approve, or
      reject controls.
- [ ] After changing memory on the Project Memory page, refresh the Run Detail
      memory details panel and confirm past used content and fingerprints remain
      historical snapshots.

## 10. Regression Commands

Run frontend build and lint from `frontend`:

```powershell
cd frontend
npm.cmd run build
npm.cmd run lint
```

Run touched-file ESLint for the M3E UI files:

```powershell
npx.cmd eslint src\components\ProjectMemoryPanel.tsx src\components\RunMemoryProvenancePanel.tsx src\components\ui\dialog.tsx src\components\MemoryConflictPanel.tsx src\pages\MemoryPage.tsx src\pages\RunDetailPage.tsx src\utils\memoryReasonHumanize.ts src\utils\memoryStatusDisplay.ts
```

Return to the repo root and run the whitespace check:

```powershell
cd ..
git diff --check
```

Note: repo-wide lint may currently fail on pre-existing unrelated files. The
touched-file ESLint command above should be clean for the M3E UI files.

## 11. Known Limitations And Deferred Work

- No component test framework is in place yet for these UI flows.
- Repo-wide lint may have pre-existing unrelated failures outside the M3E files.
- No candidate-to-action shortcuts from the Run Detail memory details panel.
- No project-level memory-use aggregation.
- No automatic stale sweep UI.
- No restore or un-supersede UI.
- No append-only `memory_fact_lineage` table.
- No semantic or vector memory.
- Retire confirmation now uses the shared solid dialog pattern.

## 12. Closeout Criteria

M3E frontend memory trust UI can be considered closed when:

- [ ] `npm.cmd run build` passes.
- [ ] Touched-file ESLint passes for the frontend files listed in section 10.
- [ ] Project Memory status label and lineage smoke passes.
- [ ] Project Memory mark-outdated, replace, retire, and approve-and-replace
      smoke passes.
- [ ] Run Detail memory-use smoke passes.
- [ ] No backend, schema, route, prompt, memory-selection, pipeline, or frontend runtime
      behavior changed as part of this docs closeout.
- [ ] No auto-resolution, latest-wins, LLM truth, vector, or embedding behavior
      was introduced.
- [ ] Known limitations are documented before the next memory trust slice begins.
