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

- M3E1 adds Project Memory lineage visibility and plain-English status labels
  for active, stale, archived, and historical facts.
- M3E2 adds the Run Detail memory provenance viewer so operators can inspect
  exactly what approved memory was injected during a run.
- M3E3 adds confirmation-modal UI for human lifecycle actions: mark stale,
  supersede one active fact with another active fact, and approve a pending
  suggestion while superseding an old active fact.

This checklist verifies the UI surfaces and copy that make those actions
human-controlled, auditable, and explicit.

## 2. Safety Guarantees

Confirm these guarantees remain true throughout the smoke:

- The backend remains the source of truth for project ownership, active-only
  preconditions, duplicate safety, edited-content validation, and lifecycle
  reason validation.
- Server-side revalidation still happens after every UI confirmation. Frontend
  affordances are convenience, not authority.
- No route or UI automatically saves, approves, marks stale, archives,
  supersedes, resolves, or applies latest-wins behavior.
- No LLM, vector, embedding, semantic memory, or truth-decision behavior is
  introduced by these UI surfaces.
- No prompt format, memory injection rule, pipeline, run, Git, or GitHub runtime
  behavior changes.
- Provenance remains read-only. Loading or refreshing Run Detail memory
  provenance never changes memory or run state.
- The Run Detail provenance panel has no action buttons for mark stale, archive,
  supersede, approve, reject, or approve-and-supersede.
- Historical, stale, and archived facts are not injected because prompt memory
  still uses only active, non-stale facts.

## 3. UI Surfaces Covered

Project Memory, implemented by `ProjectMemoryPanel`, covers:

- Active, stale, archived, and historical memory facts.
- Status labels and tooltips:
  - `active`: `In use`, injected into AI prompts for this project.
  - `stale`: `Possibly outdated`, not injected and kept for review.
  - `archived`: `Retired`, manually retired and kept for the record.
  - `historical`: `Replaced`, not injected and preserved for lineage.
- Supersession lineage:
  - Historical facts can show `Replaced by your approval -> ...` when the
    replacement fact is loaded.
  - Active facts can show `Supersedes <- ...` historical facts that point to
    them.
- Lifecycle actions for active facts: `Mark stale` and `Supersede`.
- Pending suggestion actions: `Approve`, `Edit & approve`, and
  `Approve & supersede...`.
- Existing create, edit, verify, archive, approve, reject, prompt-preview, and
  refresh flows.

Run Detail, implemented by `RunMemoryProvenancePanel`, covers:

- A `Memory Diagnostics` section with a collapsed `Memory Provenance` panel.
- Lazy, read-only provenance fetch after selecting `Load provenance`.
- Included entries labeled `as injected during this run`.
- Excluded entries under `In-policy but not injected because the role memory
  budget was full`, with copy that they did not influence the run.
- Advisory duplicate and possible replacement analysis from stored provenance.
- Explicit copy that observations are read-only and the system did not change
  memory.
- Explicit supersession caution: `Newer does not mean true. Human decides.`
- No mutation actions.

## 4. Manual Smoke Setup

1. Follow [README quick local setup](../../README.md#quick-local-setup) and
   start the backend and frontend. On Windows, use `npm.cmd` if PowerShell
   blocks `npm.ps1`.
2. Open the frontend and select a project that has Project Memory enabled.
3. Ensure the project has at least two active memory facts. Use a deliberately
   old/new pair so lineage is easy to recognize, for example `Backend uses
   Flask.` and `Backend uses FastAPI.`.
4. Ensure the project has at least one pending memory suggestion.
5. Optional, for provenance checks: run a tiny task that reaches at least the
   planner and coder stages, then note the `run_id`.
6. Open browser devtools Network before testing Run Detail provenance. The
   memory provenance endpoints should not be called until the panel is opened.

## 5. Checklist - Status Labels And Lineage

- [ ] On Project Memory, set the status filter to `all`.
- [ ] Confirm active facts show the `In use` badge and tooltip that they are
      injected into AI prompts.
- [ ] Confirm stale facts show `Possibly outdated` and the tooltip says they are
      not injected and nothing was deleted.
- [ ] Confirm archived facts show `Retired` and the tooltip says they are no
      longer injected and kept for the record.
- [ ] Confirm historical facts show `Replaced` and the tooltip says they are not
      injected and preserved to show what changed.
- [ ] Confirm the list ordering keeps active facts first when the status filter
      is `all`.
- [ ] For a historical fact with `superseded_by_fact_id`, confirm the UI shows
      either `Replaced by your approval -> ...` or `Replaced by another approved
      fact.`
- [ ] For an active replacement fact, confirm the UI can show
      `Supersedes <- ...` for historical facts pointing to it.
- [ ] Confirm these lineage displays are read-only labels, not action shortcuts.

## 6. Checklist - Mark Stale UI

- [ ] Choose an active fact and select `Mark stale`.
- [ ] Confirm the modal title is `Mark memory fact stale`.
- [ ] Confirm the modal shows the exact fact content.
- [ ] Confirm the status row shows current `In use` and resulting
      `Possibly outdated / stale`.
- [ ] Confirm the modal states that the fact will stop being injected into
      future AI prompts and that nothing is deleted.
- [ ] Confirm the `Reason` input requires at least 4 characters before the
      `Mark stale` confirmation can run.
- [ ] Submit a valid human reason and confirm the fact returns as
      `Possibly outdated`.
- [ ] Confirm the fact is absent from active prompt preview and visible when the
      stale status filter is selected.
- [ ] Repeat with an invalid reason and confirm the UI surfaces the backend
      validation error without changing the fact.
- [ ] Confirm non-active facts do not expose `Mark stale`.

## 7. Checklist - Supersede UI

- [ ] Choose an active old fact and select `Supersede`.
- [ ] Confirm the modal title is `Supersede memory fact`.
- [ ] Confirm the modal labels the selected fact as `OLD FACT`.
- [ ] Confirm the replacement selector lists other active facts only.
- [ ] Select a replacement and confirm the modal labels it as `NEW FACT`.
- [ ] Confirm the modal says OLD FACT moves from `In use` to `Replaced` and NEW
      FACT remains `In use`.
- [ ] Confirm the modal says the old fact will no longer be injected into future
      prompts and that nothing is deleted.
- [ ] Confirm the `Reason` input requires at least 4 characters before
      `Supersede old fact` can run.
- [ ] Submit a valid reason and confirm the old fact becomes `Replaced` while
      the new fact remains `In use`.
- [ ] Confirm the direction is explicit by choosing an older/newer pair where
      `created_at` would not imply the desired winner. Only the selected OLD
      FACT should become historical.
- [ ] Confirm self-supersession is impossible through the selector.
- [ ] Confirm backend errors for inactive, missing, duplicate, or invalid
      choices appear in the modal without partially changing memory.

## 8. Checklist - Approve-And-Supersede UI

- [ ] Choose a pending suggestion and select `Approve & supersede...`.
- [ ] Confirm the modal title is `Approve suggestion and supersede`.
- [ ] Confirm the suggestion content is editable and shows a 400-character
      counter.
- [ ] Confirm the old-fact selector lists active facts.
- [ ] Select an old active fact and confirm its exact content is shown under
      `Selected old fact`.
- [ ] Confirm the modal says the suggestion will become a new active memory
      fact and the selected old fact will become `Replaced` and stop being
      injected.
- [ ] Confirm the modal says nothing is deleted and backend validation remains
      the source of truth.
- [ ] Confirm `Reason` requires at least 4 characters and the edited content
      must be valid before `Approve and supersede` can run.
- [ ] Submit without editing and confirm the suggestion becomes approved, a new
      active fact appears, and the old fact becomes `Replaced`.
- [ ] Repeat with edited content and confirm the new active fact uses the edited
      text.
- [ ] Try unsafe or duplicate edited content and confirm the UI surfaces the
      backend error while the suggestion remains pending and the old fact
      remains active.
- [ ] Confirm the ordinary `Approve` and `Edit & approve` flows still work and
      do not supersede any old fact.

## 9. Checklist - Run Detail Provenance Viewer

- [ ] Open a run detail page and find the `Memory Diagnostics` section.
- [ ] Confirm the `Memory Provenance` panel is collapsed by default.
- [ ] In devtools Network, confirm the provenance endpoints are not called until
      `Load provenance` is selected.
- [ ] Select `Load provenance` and confirm the button changes to
      `Hide provenance`.
- [ ] Confirm role and chunk filters are available, plus a `Refresh` button.
- [ ] Confirm injected entries are labeled `as injected during this run`.
- [ ] If excluded entries exist, expand the details section and confirm the copy
      says they did not influence this run.
- [ ] Confirm event metadata includes role, chunk, attempt, token budget,
      category policy, and `entries hash` when recorded.
- [ ] Confirm status badges reflect `status_at_injection`, not the fact's later
      current status.
- [ ] Confirm `Advisory observations` states that observations are read-only and
      the system did not change memory.
- [ ] Confirm duplicate candidates are labeled `Possible duplicate`.
- [ ] Confirm replacement candidates are labeled
      `Possible replacement candidate` and include
      `Newer does not mean true. Human decides.`
- [ ] Confirm the panel contains no mark-stale, archive, supersede, approve,
      reject, or approve-and-supersede controls.
- [ ] After changing memory on the Project Memory page, refresh the Run Detail
      provenance panel and confirm past injected content and hashes remain
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
npx.cmd eslint src\components\ProjectMemoryPanel.tsx src\components\RunMemoryProvenancePanel.tsx src\api\client.ts src\utils\memoryStatusDisplay.ts src\pages\RunDetailPage.tsx
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
- No candidate-to-action shortcuts from the Run Detail provenance panel.
- No project-level provenance aggregation.
- No automatic stale sweep UI.
- No restore or un-supersede UI.
- No append-only `memory_fact_lineage` table.
- No semantic or vector memory.
- Archive confirmation still uses the older inline archive reason flow if it has
  not yet been harmonized with the M3E3 confirmation modal pattern.

## 12. Closeout Criteria

M3E frontend memory trust UI can be considered closed when:

- [ ] `npm.cmd run build` passes.
- [ ] Touched-file ESLint passes for `ProjectMemoryPanel.tsx`,
      `RunMemoryProvenancePanel.tsx`, `client.ts`,
      `memoryStatusDisplay.ts`, and `RunDetailPage.tsx`.
- [ ] Project Memory status label and lineage smoke passes.
- [ ] Project Memory mark-stale, supersede, and approve-and-supersede smoke
      passes.
- [ ] Run Detail provenance smoke passes.
- [ ] No backend, schema, route, prompt, injection, pipeline, or frontend runtime
      behavior changed as part of this docs closeout.
- [ ] No auto-resolution, latest-wins, LLM truth, vector, or embedding behavior
      was introduced.
- [ ] Known limitations are documented before the next memory trust slice begins.
