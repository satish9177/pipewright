# Memory UX Guided Smoke Checklist (#37A-#37G)

Manual closeout checklist for the local-first/demo Memory UI guided UX phase.
This document covers frontend UX only. It does not describe backend behavior,
schema changes, prompt injection changes, or new memory lifecycle semantics.

Related docs:

- [M3 trust lifecycle design](../design/memory-m3-trust-lifecycle.md)
- [Memory provenance smoke checklist](./memory-provenance-smoke.md)
- [Memory lifecycle smoke checklist](./memory-lifecycle-smoke.md)
- [README quick local setup](../../README.md#quick-local-setup)

## Completed Slices

- **#37A - Terminology and modal solidity**
  Humanized Memory UI terms, humanized visible reason strings, and fixed memory
  confirmation dialogs so they render on a solid readable background.
- **#37B - Guided top section**
  Added `MemoryTrustStrip` and `MemoryAttentionPanel` so `/memory` explains
  what Pipewright remembers, what needs review, and what not to do blindly.
- **#37C - Suggested memories review**
  Redesigned pending suggested memory cards so each suggestion leads with the
  suggested note, clear source/rationale/evidence, safety copy, and existing
  review actions.
- **#37D - Memory Notes grouping and Manage actions**
  Grouped saved notes into `In use`, `Possibly outdated`,
  `History: Retired / Replaced`, and `Needs review` when applicable. Moved
  lifecycle controls into per-card `Manage` disclosures.
- **#37E - Stale/conflict/repo-reality compare UI**
  Replaced raw repo-reality lifecycle strings with a clear compare block that
  says what memory claims, what the repo appears to show, and why review matters.
- **#37F - Run Detail memory provenance polish**
  Reframed Run Detail memory-use details as `Recent AI behavior` /
  `What Pipewright told the AI`, with role-first cards, included and left-out
  memories, warning blocks, and raw technical details behind disclosures.
- **#37G - Smoke closeout**
  This checklist documents the final manual validation for the Memory UI guided
  UX phase.

## Safety Invariants

Confirm these remain true throughout smoke testing:

- Memory is advisory context only.
- Source code and explicit user instructions win over memory.
- Nothing is used until approved where suggestions are concerned.
- Memory UI does not auto-retire, auto-replace, auto-stale, or auto-resolve
  conflicts.
- No backend, API, schema, route, package, runtime, or prompt-injection behavior
  changed in #37A-#37G.
- No new memory mutation routes were added.
- No auto-resolution, confidence scoring, vector search, semantic retrieval, or
  automatic approval was introduced.
- Normal UI does not use `supersede`, `delete`, or `latest wins` wording.

## Manual Setup

1. Start the backend and frontend using the normal local setup.
2. Open `/memory` and select a project with Memory data.
3. For fuller coverage, use a project with:
   - at least one active memory note,
   - at least one pending suggested memory,
   - at least one possibly outdated memory,
   - at least one retired or replaced memory,
   - at least one repo-reality conflict reason if available.
4. For Run Detail provenance checks, open a run that recorded memory-use
   snapshots.

## 1. `/memory` Guided Overview

- [ ] `/memory` loads successfully.
- [ ] The Project knowledge / memory trust summary appears near the top.
- [ ] `MemoryTrustStrip` counts are visible for in-use memory, suggested
      memories, possibly outdated memory, retired/replaced history, and review
      needed.
- [ ] Changing Status / Category / Scope filters does not make the top summary
      misleading; counts still summarize the loaded project memory state.
- [ ] Loading, error, missing, or unfamiliar states fail closed and never imply
      "all clear."
- [ ] `MemoryAttentionPanel` explains the safest next thing to inspect without
      adding mutation buttons.

## 2. Suggested Memories Review Queue

- [ ] The Suggested memories section remains in the same place on `/memory`.
- [ ] Each pending card leads with the suggested memory sentence.
- [ ] Metadata is quiet and scannable: pending state, category, scope, priority,
      source, `Found in`, and `Why suggested` when available.
- [ ] Safety framing is clear: nothing is used until approved, and approving
      inaccurate memory can mislead future runs.
- [ ] Existing actions remain present and map only to existing behavior:
      - `Approve & start using`
      - `Edit, then approve`
      - `Approve & replace an old memory`
      - `Reject suggestion`
- [ ] Reject still uses the existing rejection reason flow.
- [ ] There is no bulk approve.
- [ ] No normal UI wording uses `supersede`, `delete`, or `latest wins`.

## 3. Memory Notes

- [ ] Memory Notes are grouped into:
      - `In use`
      - `Possibly outdated`
      - `History: Retired / Replaced`
      - `Needs review` when applicable
- [ ] In-use notes are easy to identify and appear visually first.
- [ ] Each card shows the memory sentence, status chip, category/scope/priority,
      created/updated/last-checked metadata, and history note when present.
- [ ] Possibly outdated notes clearly say they are not shown to the AI while
      marked possibly outdated.
- [ ] Retired/Replaced notes clearly say they are kept in history and not shown
      to the AI.
- [ ] Manage actions are behind the per-card `Manage` disclosure.
- [ ] Existing handlers are preserved:
      - `Confirm still accurate`
      - `Mark as possibly outdated`
      - `Replace this memory`
      - `Edit memory note`
      - `Retire memory`
- [ ] Retire and mark-outdated reason validation still uses the existing flows.
- [ ] Retired, replaced, and outdated notes remain visible and understandable.

## 4. Stale / Conflict / Repo-Reality Warnings

- [ ] Raw reason strings such as
      `repo reality conflict: repo=postgresql, memory=mongodb` do not appear in
      normal UI.
- [ ] Repo-reality conflict reasons are humanized, for example:
      `The current repo appears to use PostgreSQL, while this memory says MongoDB.`
- [ ] A compare block appears for clear repo-reality conflicts with:
      - `Memory says`
      - `Current repo appears to show`
      - `Why this matters`
      - code/source repo wins and review-before-using copy
- [ ] Normal stale/outdated reasons still render as simple humanized text.
- [ ] No one-click fix, automatic replacement, auto-resolution, or automatic
      memory mutation appears.

## 5. Run Detail Memory Provenance

- [ ] In Run Detail, open `Details & audit` and find `Memory Used`.
- [ ] The panel shows the `Recent AI behavior` eyebrow.
- [ ] The panel title is `What Pipewright told the AI`.
- [ ] The panel is read-only and lazy-loaded; opening it does not change memory
      or run state.
- [ ] Role-first cards appear for Planner, Coder, Reviewer, Triage, or any role
      present in the stored snapshot.
- [ ] Included memories are marked with `+`.
- [ ] Left-out memories are marked with `-`.
- [ ] Memory left out because of budget shows a clear warning block.
- [ ] Safety memory left out for space remains prominent when present.
- [ ] Raw technical details are behind disclosures, not leading the card.

## 6. Validation Commands

For this docs-only closeout, frontend and backend tests are not required.

Run from the repository root:

```powershell
git diff --check
git status --short
```

If any frontend file is touched accidentally, also run from `frontend`:

```powershell
npm.cmd run build
npx.cmd eslint <touched frontend files>
```

If any backend file is touched accidentally, stop and re-scope before merging;
#37G is intended to remain docs-only.

## 7. Known Limitations / Deferred Work

- Bad active memory can still be injected until the user marks it possibly
  outdated, retires it, or replaces it.
- Repo reality checks may be unavailable for some runs.
- There is no vector or semantic memory retrieval yet.
- There is no gated injection tightening yet.
- There is no automatic stale-memory suppression yet.
- There is no run selector on `/memory` for `What Pipewright told the AI`; that
  view remains run-specific in Run Detail.
- Project-level memory-use aggregation remains deferred.

## Closeout Statement

Memory UI guided UX is complete for the local-first/demo phase after this
checklist is added and reviewed. Further memory behavior changes should be
separately scoped.
