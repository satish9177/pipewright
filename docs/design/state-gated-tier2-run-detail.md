# State-Gated Tier 2 — Run Detail design / implementation plan (#37D)

> Status: design / planning (docs-only). No frontend, backend, API, schema,
> route, or package changes are introduced by this document. It is the
> source-of-truth input for the #37D implementation slices that follow. It builds
> on [`docs/design/run-detail-guided-ux.md`](./run-detail-guided-ux.md) (#35) and
> [`docs/design/active-chunk-guided-ux.md`](./active-chunk-guided-ux.md) (#36) and
> reuses their tier vocabulary.

## 1. Purpose

#37D is **planning for the final major Claude Design structural gap: state-gated
Tier 2.** After #35, #36, and #37A–#37C, the Run Detail page is structurally much
closer to the Claude Design "guided cockpit," but one gap remains: the design's
**Tier 2 renders only the evidence/context for the current decision**, whereas the
live app still renders the full `ChunkPlanPanel` in almost every state — including
"finished" states where no chunk decision is pending.

**This document is not an implementation.** It inspects the current code, names
the gap precisely, lays out a state-by-state target, evaluates the smallest safe
first slice, analyzes risk, and gives a single honest recommendation about whether
to implement state-gated Tier 2 now, defer it, or ship only a small safe slice.

Nothing here changes runtime behavior. The only deliverable is this Markdown file.

## 2. Current state after #35 / #36 / #37A–#37C

The page has steadily improved. Current render order in `RunDetailPage.tsx`
(top → bottom):

1. **Header** — eyebrow + feature title + run-id/chunk meta row (#37A).
2. **Pipeline rail** — calm `plan → code → patch → test → review → ship`
   visualization; replaced the old Run Summary card + `StepIndicator` (#37A).
3. **Safety overview** — single consolidated `RunSafetyStrip`: at-a-glance chips
   (Scope/Tests/Review/PR) + the moved `operator_state` process-gate checks +
   unknown-state banner (#37B).
4. **Operator spine** — `OperatorAttentionPanel`: waiting/decision framing, title,
   explanation, blocked actions, trust facts, out-of-app step, wired primary /
   co-equal actions, and the new per-action **effects ledger** (#37C).
5. **Finish & ship** — stepper card (final approval → push/PR → checks), rendered
   only in final-stage states; hosts `TestValidationAckPanel`,
   `FinalApprovalPanel`, `PushPrPanel`, `PrStatusPanel` (#35H).
6. **Memory suggestions** — terminal states only.
7. **Chunk Plan Details** — `ChunkPlanPanel`, rendered **whenever `chunkPlan`
   exists** (i.e. nearly always after planning).
8. **Human Approval Required** — `pendingGate` card with a raw `<pre>` diff
   (chunk/high-risk approval), rendered below the panel when a gate is pending.
9. **Memory Conflict** — `MemoryConflictPanel`, conditional.
10. **Terminal cards** — complete / failed / rejected.
11. **Details & audit** — default-closed `<details>` (timeline / memory provenance
    / provider diagnostics) (#35D).

What #36 already solved **inside** `ChunkPlanPanel` (chunk-level gating):

- `selectActiveChunk(plan)` derives the one active chunk from existing data.
- `ActiveChunkCard` shows a read-only "current chunk at a glance."
- `chunkNeedsAttention(chunk, activeChunkNumber)` keeps attention/decision chunks
  expanded and collapses the rest into `CollapsibleChunkCard` rows.
- `ChunkEvidenceSection`, `ScopePermissionContext`, `PatchRecoveryContext` frame
  the evidence/recovery banners.

**What is still always-on (panel-level):** the `ChunkPlanPanel` `Card` itself —
its header ("Chunk Plan"), `ChunkPlanSummary`, `ExecutionControls` (when
approved), and the whole chunk list — renders at full weight in every state where
`chunkPlan` is present, including `complete`, `local_only_complete`, `rejected`,
and `final_approved`, where the current decision (if any) has already moved up
into the spine or Finish & ship. The chunk-level gating from #36 is good; the
**panel-level** gating the design implies does not exist yet.

## 3. Claude Design target

In the Claude Design prototype, **Tier 2 is state-gated**: a single
`Tier2({ st })` switch renders exactly one context cluster for the current
decision, as discrete dark-paned context cards under one "Context for this
decision" eyebrow. It never renders evidence for a decision that isn't active.

Intended state clusters (design vocabulary):

- **plan** — plan summary + chunk list (compact, for review) + plan approve/reject.
- **running** — the running chunk + a live log; no decision.
- **patch_failure** — the failure context + retry (when eligible); "nothing
  committed."
- **scope** — approved-vs-attempted file comparison (file-permission framing) +
  scope approve/reject.
- **weak_test** — the weak/none test verdict + the unverified change; the ack gate
  itself stays in Finish & ship.
- **chunk_review** — the active chunk's diff + test verdict + advisory review +
  reviewer independence + chunk approve/reject.
- **finish_ship** — the final-approval → push → PR stepper.
- **terminal / local-only** — a calm "done" summary; chunk detail recedes into
  history/audit.

The contrast with the live app: the design picks **one** cluster; the live app
renders the **whole** panel and lets #36's chunk-level collapse thin it out.

## 4. Gap analysis

What still differs from the design target:

1. **`ChunkPlanPanel` is always visible.** It renders whenever `chunkPlan` exists,
   regardless of whether a chunk decision is pending.
2. **All chunks/details render even when not the current decision.** #36 collapses
   non-attention chunks to compact rows, but the panel (summary, execution
   controls, list shell) is still present and expanded in "done" states.
3. **Final / terminal states still show chunk history at full weight.** After
   `final_approved` or `complete`/`rejected`, Finish & ship (or a terminal card)
   is the real content, yet the full chunk panel sits below it adding noise.
4. **Duplicate evidence in some states (by design, but amplified).** A test
   verdict can appear in the Safety overview chip, the `ActiveChunkCard` chip, the
   `ChunkEvidenceSection` chip, and the full `RuntimeTestValidationBanner`. #36
   accepted this as a scan aid; state-gating would reduce how often all four
   co-render.
5. **Approval diff is separate from active-chunk context.** The "Human Approval
   Required" `pendingGate` card (raw `<pre>` diff + approve/reject) renders as its
   own block **below** `ChunkPlanPanel`, not co-located with the active chunk's
   evidence. The design folds the diff into the `chunk_review` cluster.
6. **Discrete context cards vs dense reusable panels.** The design uses one
   context card per decision; the live app uses one dense, multi-responsibility
   `ChunkPlanPanel` (~1370 lines) that owns plan approval, execution, the active
   card, the list, and recovery framing simultaneously.

Net: the **information is right and safe**; the remaining gap is **panel-level
state-gating and placement**, not missing evidence or missing decomposition.

## 5. Safety constraints (binding for every #37D slice)

These are non-negotiable. Any slice that cannot satisfy all of them is out of
scope until it can.

- **Do not hide approval controls** — plan approve/reject, chunk approve/reject,
  final approve/reject must always be reachable when their gate is pending.
- **Do not hide retry / scope / acknowledgement controls** — `PatchFailureBanner`
  retry, `ScopeExpansionBanner` approve/reject, `TestValidationAckPanel`
  acknowledge stay reachable when active.
- **Do not hide weak-test warnings** — `RuntimeTestValidationBanner` weak/none
  stays amber and visible; the ack gate stays in Finish & ship.
- **Do not hide advisory warnings** — `AdvisoryReviewPanel` stays visible and
  advisory-only; never rendered as blocking.
- **Do not move chunk-specific controls unless IDs/targets/handlers are exact** —
  retry/scope/chunk-approve need a chunk number / request id / failure-report id;
  they stay where that context lives (carried over from #35/#36).
- **Do not make scope expansion look like code approval** — keep the
  file-permission framing and the "not code approval" copy.
- **Do not make `local_only` look like an in-app PR** — keep the manual/out-of-app
  guidance; never imply Pipewright pushes/opens a PR for `local_only`.
- **Fail open, never fail closed on visibility** — when a predicate is uncertain
  whether an action is pending, the panel stays **expanded**. Hiding a gate is a
  safety regression; showing slightly more than necessary is not.
- **No backend/API changes** unless a later, explicit slice proves the need and is
  approved separately.

## 6. State-by-state plan

For each state: what Tier 2 should show **first** (primary), and what can move
lower / collapse. "Active chunk" derives from existing data (`selectActiveChunk`);
"attention" reuses `chunkNeedsAttention`. No control moves unless its handler/ids
are already local to the new location.

| State | Tier 2 shows first | Can move lower / collapse |
| --- | --- | --- |
| **awaiting chunk plan approval** | Plan summary + chunk list (review) + `PlanApprovalControls` | Nothing — this *is* the decision; keep expanded |
| **ready to execute** (approved, not run) | `ExecutionControls` (Execute/Resume) + active/next chunk | Completed/future chunks already compact (#36) |
| **running** | Active (running) chunk; "waiting on the system" | Other chunks compact; no decision controls needed |
| **patch failure** | The failed chunk + `PatchRecoveryContext` + retry (if eligible) | Non-failed chunks compact; keep failed expanded |
| **scope expansion** | `ScopePermissionContext` + `ScopeExpansionBanner` (approve/reject) | Suppress competing retry (already done); other chunks compact |
| **weak / no-test acknowledgement** | Weak verdict visible on chunk; **ack gate stays in Finish & ship** | Tier 2 points to the gate; do not duplicate it |
| **awaiting chunk approval** | Active chunk + evidence + `InlineChunkApprovalControls` (and/or the gate diff) | Other chunks compact |
| **final approval** | **Finish & ship** (above) is primary | `ChunkPlanPanel` becomes secondary; chunk detail can collapse to history |
| **final_approved / ready to push** | **Finish & ship** push step is primary | Chunk panel is history → collapse |
| **PR open / checks** | **Finish & ship** PR/checks step | Chunk panel is history → collapse |
| **local_only complete** | Terminal "completed locally" + manual-push guidance | Chunk panel is history → collapse; no in-app PR anywhere |
| **terminal failed** | Gentle failure card + pointer to Details & audit; **failed chunk context if retry-eligible** | If no pending action, chunk panel → collapse; keep an attention/failed chunk expanded |
| **rejected / final_rejected** | "Rejected and rolled back" card | Chunk panel is history → collapse |

The recurring pattern: **states with a pending chunk/plan decision keep the panel
expanded; states whose real content has moved into the spine, Finish & ship, or a
terminal card can let the panel recede into history.**

## 7. First safe implementation candidate — terminal/final-state chunk-detail collapse (#37D2)

**Candidate:** In states where **no chunk action is pending**, make
`ChunkPlanPanel` secondary — wrap it in a default-collapsed "Chunk history /
details" disclosure (mirroring the existing #35D Details & audit `<details>`
pattern) instead of rendering it expanded. When any chunk action exists, render
exactly as today.

**Why this is the right first slice:**

- It is **panel-level visibility only** — it wraps the existing
  `ChunkPlanPanel` in a disclosure; it moves no controls, changes no props, and
  alters no handlers. Expanded, it is byte-for-byte today's panel.
- It targets the highest-noise, lowest-risk states (`complete`,
  `local_only_complete`, `rejected`, `final_rejected`, and `final_approved` once
  Finish & ship owns the decision), which is exactly where the design says chunk
  detail should recede.
- The "is an action pending?" predicate can be built **entirely from existing
  data** and reuse #36's `chunkNeedsAttention`, so it never needs a backend field.

**Proposed predicate (collapse only when ALL are true — otherwise stay expanded):**

- `plan.chunk_plan_status !== 'awaiting_approval'` (no plan approve/reject pending), **and**
- the plan is not approved-but-unexecuted (no pending `ExecutionControls` decision), **and**
- no chunk satisfies `chunkNeedsAttention(...)` (no running / failed / awaiting
  chunk approval / pending scope / error / patch-failure / recovered chunk), **and**
- no pending approval gate for this run (`pendingGate` / `pendingChunkGates`), **and**
- no pending memory-conflict gate, **and**
- the run status is terminal-or-finished (`complete`, `local_only_complete`,
  `rejected`, `final_rejected`, or a `final_approved`/PR state where Finish & ship
  is the live surface).

If **any** condition is unmet, the panel renders expanded as today (fail open).

**Keep visible even when collapsed:** the disclosure header should still show a
one-line summary (e.g. "Chunk plan — N chunks, all complete") and remain one click
from the full, unchanged panel. A failed/attention chunk forces expansion (the
predicate's `chunkNeedsAttention` term guarantees this), so a retry-eligible
failure is never hidden.

**Explicitly out of scope for #37D2:** touching any active approval/retry/scope
state, relocating the approval diff, or decomposing `ChunkPlanPanel`.

## 8. Risk analysis

**Low risk to collapse (no action pending in normal flow):**

- `complete` / `local_only_complete` — terminal; decision already done.
- `rejected` / `final_rejected` — terminal; rolled back.
- `final_approved` and PR-open states — the live decision is in Finish & ship; the
  chunk panel is history. (Still gated by the predicate, not status alone.)

**High risk — must remain expanded:**

- `awaiting_chunk_plan_approval` — the plan list + approve/reject *is* the decision.
- `awaiting_chunk_approval` — chunk approve/reject pending.
- patch failure with retry available — retry must stay reachable.
- `awaiting_scope_approval` / pending scope expansion — scope approve/reject.
- `running` / ready-to-execute — execution controls / live context.
- `awaiting_memory_conflict_approval` — conflict approve/reject.
- weak/no-test acknowledgement outstanding — handled in Finish & ship, but the
  panel's weak verdict must not be hidden while the ack is outstanding.

**What could accidentally hide a safety gate:**

- A predicate keyed on **run status alone** (e.g. "collapse when `failed`") would
  hide a retry-eligible failed chunk. Mitigation: the predicate must include the
  per-chunk `chunkNeedsAttention` term and **fail open**.
- A `final_approved` run that still has an outstanding weak-test acknowledgement —
  the ack lives in Finish & ship (kept expanded), but the predicate must confirm
  `acknowledgementBlocking` is false before treating the panel as pure history.
- A pending `pendingGate`/`pendingChunkGates` not reflected in chunk status —
  include the gate checks in the predicate.

**Manual smoke required (feeds #37E):**

- Drive a run to `complete`, `local_only_complete`, and `rejected`: confirm the
  chunk panel is collapsed by default and one click reveals the unchanged panel.
- Drive a `failed` run **with a retry-eligible chunk**: confirm the panel stays
  expanded and the retry button is reachable.
- `final_approved` with an outstanding weak-test ack: confirm Finish & ship + ack
  gate stay expanded and reachable; confirm the panel does not collapse while the
  ack is outstanding.
- `awaiting_chunk_approval`, scope expansion pending, patch failure: confirm no
  collapse (all stay expanded).
- Confirm Details & audit and the consolidated Safety overview are unchanged.

## 9. Suggested roadmap

Small, independently shippable, each preserving behavior and audit surfaces:

- **#37D1 — docs-only plan** (this document).
- **#37D2 — terminal/final-state chunk-history collapse.** Wrap `ChunkPlanPanel`
  in a default-collapsed disclosure when the Section 7 predicate says no action is
  pending; fail open otherwise. Frontend-only, display-only. **The recommended
  next slice.**
- **#37D3 — approval-diff / context-placement audit (docs or display-only).**
  Decide whether (and how) to co-locate the `pendingGate` diff with the active
  chunk's `chunk_review` context. Because this touches a live approval surface
  (`approveMutation`/`rejectMutation` bound to the gate), the first step is an
  **audit/plan**, not a move. Any actual relocation is a separate, smoke-heavy
  slice that preserves the exact gate binding.
- **#37D4 — optional dark evidence-pane polish.** Restyle the approval diff,
  scope-compare, and test-verdict presentation toward the design's dark panes /
  big-number verdict **in place** (no markup/control changes, no global token
  migration). Optional; lowest priority.
- **#37E — smoke / docs closeout.** Manual smoke checklist mirroring
  `docs/testing/active-chunk-guided-ux-smoke.md`, validating #37A–#37D and
  recording known limitations.

## 10. Recommendation

**Do #37D2 only, then close #37 after #37E smoke. Defer #37D3 and #37D4 behind
explicit, separately-approved slices.**

Honest value-vs-risk reasoning:

- **#37D2 is high value, low risk.** It removes the most visible remaining "admin
  dashboard" smell (full chunk history shouting in finished states) with a
  display-only disclosure and a fail-open predicate. It moves no controls and is
  trivially reversible. This is the slice worth doing.
- **A full state-gated Tier 2 reorg (discrete context cards per decision,
  relocating the approval diff, decomposing `ChunkPlanPanel`) is high regression
  risk for diminishing returns.** #35/#36 already delivered the guided spine,
  consolidated safety, the effects ledger, the active-chunk card, and chunk-level
  collapse. The remaining delta is mostly cosmetic placement, and the riskiest
  piece (the approval diff) touches a live gate. The cost/benefit does not justify
  a large phase right now, especially against the current local-first / demo
  priority.
- **#37D3/#37D4 are legitimate but optional.** Keep #37D3 as an audit first;
  treat #37D4 as polish. Neither should gate closing #37.

So: ship #37D2, run #37E smoke, and **close the #37 Run Detail Tier-2 work** with
#37D3/#37D4 recorded as deferred follow-ups — not as a larger state-gated Tier 2
phase.

## 11. Non-goals

- **No backend behavior changes.**
- **No new action wiring** — no new mutations; no wiring of the still-preview
  chunk-specific actions (retry/scope/ack/chunk-approve) without exact ids/targets.
- **No global design-system migration** (no paper/ink/copper token swap, no IBM
  Plex migration).
- **No hidden warnings** — weak-test, advisory, scope, patch-failure, and
  unknown-state signals stay visible; collapse applies only to non-attention chunk
  *history*.
- **No full `ChunkPlanPanel` rewrite in one PR** — decomposition, if ever done, is
  incremental and parity-gated.
- **No automatic approvals / retries / pushes** — the UI never acts without an
  explicit human click.

## 12. Closeout criteria

This document is complete when:

- A clear state-gated Tier 2 plan exists (this file).
- The first safe slice is identified (**#37D2 — terminal/final-state chunk-history
  collapse**) with an explicit, fail-open predicate built from existing data.
- The larger state-gated Tier 2 reorg is explicitly **deferred** with a stated
  value-vs-risk rationale (Section 10).
- **#37E** can validate the current design and the known limitations via manual
  smoke (Sections 6–8 provide the state matrix and the required smoke cases).

## Related docs

- [`docs/design/run-detail-guided-ux.md`](./run-detail-guided-ux.md) — #35 Tier 1/3.
- [`docs/design/active-chunk-guided-ux.md`](./active-chunk-guided-ux.md) — #36 Tier 2.
- [`docs/testing/run-detail-guided-ux-smoke.md`](../testing/run-detail-guided-ux-smoke.md)
  — #35 smoke checklist.
- [`docs/testing/active-chunk-guided-ux-smoke.md`](../testing/active-chunk-guided-ux-smoke.md)
  — #36 smoke checklist (model for #37E).

### Docs lint / build

No docs linter or docs build is configured in this repo (the `docs/` tree is plain
Markdown). For this docs-only slice, run `git diff --check` only; there is no docs
build/lint step to run.
