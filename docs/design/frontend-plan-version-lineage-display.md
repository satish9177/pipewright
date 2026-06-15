# Frontend plan-version lineage display (Slice C) — design brief

Status: **Design only — not implemented.** §23 row 7b follow-up (Slice C),
successor to Slice A (plan-version lineage read-model, merged in #319) and Slice B
(approved-plan-version binding, `docs/design/approved-plan-version-binding.md`).
This slice surfaces the lineage the backend already records — it adds a small,
presentational `PlanVersionLineage` component to the Run Detail page that consumes
the existing `GET /runs/{run_id}/plan-versions` endpoint. **Frontend-only.** No
backend, schema, route, approval, or execution change.

Scope owner: pipeline roadmap (plan-gate turns). Predecessors: Slice A (read-model
endpoint), Slice B (`approved_version` field). This is the terminal slice of the
plan-version-lineage thread; there is no planned follow-on UI.

## Non-authority invariant (read first — this governs the whole slice)

> **The plan-version lineage display is advisory/provenance only. It must never
> block, gate, or alter approval, revision, execution, push, PR, or merge. It is a
> read of an audit endpoint rendered as text.**

Concretely, this slice must preserve all of the following exactly as they are
today:

- **No approval/execution behavior changes.** The component renders data; it owns
  no mutations and no controls. The Revise/Approve cluster keeps its existing
  components, props, conditions, and handlers.
- **No new request authority.** The lineage is read from one additive GET. It never
  feeds a decision, a disabled state, a mismatch warning, or a gate.
- **No new polling.** The page already polls `['run']` and `['runChunks']`; this
  slice adds a query that refetches only on explicit invalidation, never on an
  interval.
- **Honest absence over fabrication.** Zero recorded versions renders nothing (or a
  muted "not recorded" line for an approved legacy run) — never a synthesized v1,
  never "approved: unknown".

Slice B already established that `approved_plan_version` is provenance, not an
authority channel. This slice is the *view* of that provenance and inherits the
same rule: memory/advisory-style provenance is never an authority on scope,
approval, Git, provider, execution, or merge.

**Guardrails (inherited constraints):** one small PR; frontend only; no backend /
schema / route change; no approval, revision, or execution behavior change; no
activation/default-on change; no mismatch gate; reuse existing query/invalidation
conventions; render sanitized text as plain React children only.

---

## 1. Component placement decision

Mount a new presentational `PlanVersionLineage` inside `ChunkPlanPanel`
(`frontend/src/components/ChunkPlanPanel.tsx`), in the panel's `CardContent`,
**immediately above the Revise/Approve cluster but OUTSIDE the
`isAwaitingApproval` block.**

The Revise/Approve cluster today is the trailing `{isAwaitingApproval && ( … )}`
fragment (`ChunkPlanPanel.tsx:1717-1738`): a `<Separator/>`, `RevisePlanPanel`, and
`PlanApprovalControls`. That entire block unmounts the moment the plan is approved.

- Placing lineage *outside* that block means it **survives after approval** — the
  user can still see "Approved: v2" once the controls are gone. This is the whole
  point of showing the binding from Slice B.
- Placing it *immediately above* the cluster keeps the natural reading order while a
  decision is pending: chunk list → "this is revision N of M" lineage → Revise /
  Approve. The user sees which version they are about to approve right next to the
  approve button, without the lineage being part of the approval control flow.
- It renders below the per-chunk list and the chunk action message/error block, so
  it never displaces a pending chunk decision.

Rejected placements:

- **Inside the `isAwaitingApproval` block** — would vanish after approval, hiding
  the approved-version badge exactly when it becomes most useful (audit lens on a
  finished/terminal run).
- **In the card header / plan summary** — too prominent for what is provenance; it
  would compete with the live plan status badge and the "Current chunk at a glance"
  summary.
- **A new page-level card in `RunDetailPage`** — splits lineage from the plan it
  describes and from the Revise affordance that produces new versions; the panel is
  the cohesive home.

`PlanVersionLineage` is **purely presentational**: it fetches nothing, owns no
state beyond a local collapse toggle, and takes its data via props. `ChunkPlanPanel`
forwards the page-owned lineage data plus the `plan.chunk_plan_status` it already
holds.

## 2. Always-on vs read-data-gated decision

**Always render when lineage data exists; do NOT gate on
`PIPEWRIGHT_PLAN_TURNS_ENABLED`.**

This mirrors the Slice A/B decision precisely: the flag governs the *mutating
revise capability* (the `POST /plan-turns` route), not audit/provenance reads.

- With the flag off, no plan turns can occur, so every post-store run's lineage is
  exactly `[v1]` and `approved_version` is `1` once approved. Showing "Version 1 ·
  original plan" reveals no dormant capability — it is a true statement about a run
  that has exactly one plan version.
- The `plan_turn` source value cannot leak while the flag is off (no `plan_turn`
  rows can exist), so there is nothing flag-sensitive to hide.
- Gating the display on the flag would hide truthful history for no safety benefit
  and would diverge from "provenance is observable."

The *only* gate is "is there data to show?" — derived from the response, not from a
capability flag (see §3).

## 3. Display states (v1 / v2-v3 / approved / null / legacy)

Inputs the component reasons over:

- `versions: PlanVersion[]` — ordered `version ASC`, each `{ version, source,
  created_at, created_from_turn }` (`created_from_turn` is `null` for
  `initial`/`seeded`; for `plan_turn` it carries `turn_number` + sanitized
  `message`).
- `approved_version: number | null` — top-level field added in Slice B.
- `chunk_plan_status` — already on `plan` inside `ChunkPlanPanel`
  (`awaiting_approval` | `approved` | `rejected` | `none` | …).

Decision matrix (the component is a pure function of these three):

| `versions` | `chunk_plan_status` | `approved_version` | Render |
|---|---|---|---|
| `[]` | not `approved` | `null` | **Nothing.** No recorded lineage, nothing approved — stay silent. |
| `[]` | `approved` | `null` | Muted single line: **"Version history not recorded."** (legacy run approved before the store shipped). |
| `[v1]` | any | `null` (awaiting) or `1` (approved) | Quiet inline line: **"Version 1 · original plan."** If `approved_version === 1`, append an **"Approved: v1"** badge on that line. |
| `[v1, v2(, v3…)]` | any | `null` or `N` | **Collapsed "Plan history" disclosure** (see §4). Each row shows its version, source label, and (for plan-turn rows) the sanitized message. The row whose `version === approved_version` carries an **"Approved: vN"** badge. |
| any non-empty | `awaiting_approval` | `null` | Render the lineage **without** any "approved" badge. Do **not** render "approved: unknown" — null-while-awaiting means *not yet approved* (see §10 warning). |

Source-label vocabulary (display strings, derived from `version.source`):
`initial`/`seeded` → "original plan"; `plan_turn` → "revision" (paired with its
`turn_number` and sanitized `message`). Unknown future sources degrade to a neutral
label, never an error.

The approved badge is attached to a **row** (the matching version), not shown as a
standalone claim, so an `approved_version` that does not match any listed version
(should never happen given the MAX invariant) simply shows no badge rather than a
dangling "Approved: v?".

## 4. Collapsed-by-default behavior

- **v1-only** and the **legacy "not recorded"** cases are a single quiet inline
  line — no disclosure, nothing to expand.
- **v2/v3+** render inside a **collapsed-by-default** `<details>`/disclosure
  labelled "Plan history" (matching the existing `CollapsibleChunkCard` /
  "Show raw completion summary" disclosure idiom in this panel). The summary line
  states the headline ("Revision N of M" / "N plan versions"); expanding reveals the
  ordered rows.
- Collapsed by default keeps provenance unobtrusive: a multi-revision plan does not
  push the Approve button down a long history list. The approved-version badge, when
  present, is surfaced in the collapsed summary line too (e.g. "Plan history · 3
  versions · Approved: v3") so the audit fact is visible without expanding.
- Local `useState` collapse toggle only — same pattern as `CollapsibleChunkCard`.
  No URL state, no persistence.

## 5. Eager page-owned query decision

`RunDetailPage` owns a **new, eager `useQuery`** keyed `['planVersions', runId]`,
fetched via a new `runsApi.getPlanVersions(runId)` client method:

- **Page-owned, not component-owned**, matching how `['run']` and `['runChunks']`
  are owned by the page and passed down. `PlanVersionLineage` stays presentational
  and trivially testable; `ChunkPlanPanel` receives the lineage as props alongside
  the `plan` it already gets.
- **Eager** (`enabled: !!runId`), like the existing `['runChunks']` query — the
  endpoint is a cheap single-row + small join read with no lock and no plan parse
  (Slice A), so there is no cost reason to lazy-load it, and eager fetch means the
  approved badge is present immediately on a finished run.
- **No `refetchInterval`.** This is the explicit "no new polling loop" decision
  (§10). Lineage only changes on two discrete user actions (revise, approve), both
  of which already trigger invalidation (§6). It does not need to track running/
  terminal status the way `['run']`/`['runChunks']` do.
- `retry: false` is reasonable (a 404 on an unknown run should not retry-storm),
  mirroring the `['runChunks']` query.

Data threading: page → `ChunkPlanPanel` (new optional `planVersions` /
`approvedVersion` props) → `PlanVersionLineage`. Optional props keep every existing
`ChunkPlanPanel` caller/test valid; when absent (data still loading or query
disabled) the lineage renders nothing.

## 6. Invalidation points after revise and approval

Refetch `['planVersions', runId]` at exactly the two points where a new version is
created or the approved version is stamped — reusing the page's existing
invalidation conventions, **not** a poll:

1. **After a successful plan revision.** `RevisePlanPanel.onRevised` → the page's
   `onPlanRevised` handler (currently `refreshRunDecisionState`, which invalidates
   `['run']`, `['runChunks']`, `['gates']`). Add `['planVersions', runId]` to that
   refresh path so a newly appended `vN` appears. (A revise can also fire on a 409
   to self-correct a stale view — the same invalidation covers it.)
2. **After a successful chunk-plan approval.** `approveChunkPlanMutation.onSuccess`
   (`RunDetailPage.tsx:762-772`) already invalidates run/chunks/gates; add
   `['planVersions', runId]` so the freshly stamped `approved_version` (Slice B)
   shows its "Approved: vN" badge without a manual refresh.

The cleanest single-sourced approach is to add the `['planVersions', runId]`
invalidation inside `refreshRunDecisionState()` (already used by revise/steer/scope
paths) and to the `approveChunkPlanMutation.onSuccess` handler. No other mutation
changes the lineage, so no other handler needs touching. Rejecting a plan binds
nothing (Slice B §3) and creates no version, so reject paths need no lineage
invalidation — but invalidating there is harmless if it falls out of reusing
`refreshRunDecisionState`.

## 7. Likely frontend files touched

- `frontend/src/api/client.ts` — add a `PlanVersion` type (and a
  `PlanVersionLineageResponse` type: `{ run_id, approved_version, versions }`) and a
  `runsApi.getPlanVersions(runId)` method (`GET /runs/${runId}/plan-versions`),
  mirroring the existing `getRunChunks` / `createPlanTurn` shape.
- `frontend/src/components/PlanVersionLineage.tsx` — **new** presentational
  component implementing §3/§4. No fetching, no mutations; local collapse state only.
- `frontend/src/components/ChunkPlanPanel.tsx` — accept new optional
  `planVersions` / `approvedVersion` props; render `<PlanVersionLineage>` above the
  `isAwaitingApproval` cluster (passing `plan.chunk_plan_status`).
- `frontend/src/pages/RunDetailPage.tsx` — add the `['planVersions', runId]`
  `useQuery`; thread results into `ChunkPlanPanel`; add the lineage key to the
  revise + chunk-plan-approval invalidation paths (§6).
- **Untouched:** all backend, `schema.sql`, routes, the `plan_versions` store, the
  flag default, `RevisePlanPanel` internals, and every other panel/component.

## 8. Build / lint / manual smoke plan

This frontend has **no unit-test runner configured** (no `vitest`, no `.test.tsx`;
`package.json` scripts are `dev` / `build` / `lint` / `preview`). Verification is
therefore type-check/build + lint + manual smoke. Standing up a component-test
harness is out of scope for this slice (see §9).

- **Type-check + build:** `cd frontend; npm.cmd run build` (`tsc -b && vite build`)
  — must pass with the new component, props, and client types.
- **Lint:** `cd frontend; npm.cmd run lint` (`eslint .`) — no new warnings/errors.
- **Manual smoke** (drive the pipeline via the established API + headless-Chrome
  screenshot method on Windows — see the project demo-smoke notes; no Playwright):
  1. v1-only run awaiting approval → quiet "Version 1 · original plan", no
     disclosure, no approved badge.
  2. Approve it → lineage **survives** the controls unmounting; "Approved: v1"
     badge appears (no manual refresh).
  3. With `PIPEWRIGHT_PLAN_TURNS_ENABLED` on, revise twice → "Plan history"
     disclosure collapsed by default; expand shows v1/v2/v3 with sanitized
     messages; approve → "Approved: v3" on the v3 row and in the summary line.
  4. Legacy run (chunk_plan present, zero `plan_versions`) approved → muted
     "Version history not recorded", never a fabricated v1, never "Approved:
     unknown".
  5. Confirm the network panel shows `['planVersions']` refetching only on
     revise/approve — never on an interval.

## 9. Explicit non-goals

- No backend, schema, route, or store change — consumes the existing Slice A/B GET
  only.
- No approval, revision, push, PR, merge, or execution behavior change.
- No gating on `PIPEWRIGHT_PLAN_TURNS_ENABLED`; no activation/default-on change.
- **No approval blockers** and **no mismatch warnings** — the display never compares
  the live plan to a stamped version and never disables a control.
- No new polling loop; refetch is invalidation-driven only (§6).
- No full plan-turn thread UI (no per-version diffs, no message threads beyond the
  single sanitized `message` Slice A already returns); no approver identity /
  `approved_at` (deferred in Slice B).
- No Row 19 FTS / Row 23 vector-memory work.
- No new frontend test harness (none exists); verification is build + lint + manual
  smoke.

## 10. Warnings (must hold in implementation)

- **`approved_version: null` must be interpreted with `chunk_plan_status`.** Null is
  ambiguous on its own: `awaiting_approval` + null = *not yet approved* (show no
  approved badge, no "unknown"); `approved` + null = *legacy run, version not
  recorded* (the muted "Version history not recorded" line). Never render "approved:
  unknown" for the awaiting case, and never treat a legacy null as "not approved".
- **The lineage is advisory/provenance only and must never block approval or
  execution.** It owns no mutations, no disabled states, and no gates. If lineage
  fetch fails or is empty, approval/execution/revise must behave exactly as today
  (render nothing, degrade silently).
- **No new polling loop.** Do not set `refetchInterval` on `['planVersions']`; rely
  solely on the revise/approval invalidation points. Adding a poll would be a
  behavior change and a (small) cost regression for zero quality gain.
- **Render sanitized messages as plain React text.** The `message` field is already
  redacted at insert by the backend (`sanitize_for_log`), but it must still be
  rendered as plain React children — **never** via `dangerouslySetInnerHTML` and
  never interpolated into markup. Treat all server strings as untrusted text.
