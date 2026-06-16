# Phase 2F — Pipewright UX 1.0: Thread UI, Run Timeline & Approval Experience

**Status:** **COMPLETE & MERGED through PR-0..PR-4 (plus review fixes
PR-A/PR-B/PR-C), 2026-06-17.** This is the originating design brief; the
implementation closeout and per-PR delta notes are **§13**. **PR-5 (fine-grained
event persistence) is deferred / not started** and remains the only unshipped slice.
**Scope:** Frontend Run Detail experience + one read-only backend read-model.
**Out of scope (do not touch):** FTS activation, Row 23 vectors, memory retrieval,
execution/approval/Git/PR *behavior*. The shipped PRs are read-only (a single GET +
presentation): no backend writes, no schema/event table, no POST lifecycle handler
changes.

---

## 0. The one-paragraph thesis

Pipewright already emits a structured event stream, already computes a plain-English
"what's happening / what's next" narrative (`operator_state`), already has a stage rail
and a status header, and already persists every run milestone with a timestamp. What it
*lacks* is a **single chronological spine** that ties these together into a readable story,
and a **read-only, restart-surviving source** for that spine. Phase 2F is therefore
**90% recomposition of existing parts + one new read-only read-model** — not a greenfield
build. This is the safest possible framing: the risky parts (approvals, Git, scope) keep
their existing, enforced controls untouched; we only change how the run is *presented*.

---

## 1. Current state (verified against code)

### Frontend
- `frontend/src/pages/RunDetailPage.tsx` (~1,956 lines) — the monolith. Already contains:
  - Header: `feature_description`, short run id, `chunkSummaryText`, `RunStatusBadge`.
  - `PipelineRail` — display-only stage rail (`plan→code→patch→test→approval→github_pr`,
    labelled Plan/Code/Patch/Test/Review/Ship), keyed off `run.current_step`/`run.status`.
  - `RunSafetyStrip` — compact read-only safety overview.
  - `OperatorAttentionPanel` — **the existing next-action engine.** Renders the backend
    `operator_state` read-model: `phase`, `waiting_on`, `narrative.{what_happened, why,
    whats_next}`, `primary_action`/`neutral_actions`/`secondary_actions`/`blocked_actions`,
    `trust_facts`, `out_of_app_instruction`, plus a per-action "effects ledger." It is
    already wired to the **real** mutations via `resolvePrimaryAction`/`resolveCoEqualAction`.
  - "Finish & ship" vertical stepper (final approval → push/PR → checks).
  - `ChunkPlanPanel` (collapses to "Chunk history" when terminal).
  - Pending-gate "Human Approval Required" card; `MemoryConflictPanel`; terminal cards.
  - `EventLog` — **buried inside a default-collapsed "Details & audit" `<details>`**, plus
    memory provenance and provider diagnostics.
- `frontend/src/components/EventLog.tsx` — renders `RunEvent[]` as a monospace, developer-
  oriented live log (`time | source-chip | stage/kind | message`). Not selectable, no detail
  view, no plain-English layer.
- `frontend/src/hooks/useRunEvents.ts` — WebSocket client. Dedupes by `event.id`, replays via
  `?since=`, falls back to polling, caps at `EVENT_LIMIT = 500`.
- `frontend/src/types/events.ts` — `RunEvent` and the kind/stage vocab (mirrors backend).
- Plain-English primitives already present: `utils/statusDisplay.ts`
  (`friendlyStatusLabels` vs raw `formatStatusLabel`), `lib/runFlowCopy.ts`,
  `utils/memoryReasonHumanize.ts`, `lib/operatorPrimaryAction.ts`.
- Routing (`App.tsx`): `runs/:runId → RunDetailPage`. One route; this stays.

### Backend
- `backend/events/event_bus.py` — **process-local, in-memory, `BUFFER_LIMIT = 500`,
  not persisted.** `publish()` is fail-safe (never raises). Lost on restart.
- `backend/routes/ws_events.py` — `GET /ws/runs/{run_id}/events` (WS). Replays the in-memory
  buffer, then live-tails, heartbeats every 15s, closes on `terminal`. **This is the only
  event transport. There is no REST timeline/events endpoint.**
- `backend/events/schema.py` — `Event` (id, ts, run_id, chunk_number, kind, stage, level,
  message≤500 chars, data≤4KB). Already size-capped and PII-conscious.
- `backend/pipeline/operator_state.py` — computes `operator_state` + `narrative` on chunk
  reads (never persisted). **This is the plain-English source of truth; reuse it, do not
  reinvent it.**
- Persisted, timestamped tables (`backend/db/schema.sql`) — the timeline backbone:
  `pipeline_runs` (`created_at`, `pr_created_at`, status, current_step, pr_url/number),
  `plan_versions` (`created_at`), `approval_gates` (`created_at`, `decided_at`, status,
  approval_type, chunk_number, risk_level, ai_summary), `chunks` (`created_at`, status),
  `chunk_attempts` (`created_at`), `scope_expansion_requests` (`created_at`, `decided_at`),
  `test_validation_acknowledgements` / `review_finding_acknowledgements` (`created_at`),
  `chunk_reviews` (`created_at`), `memory_injection_events` (`created_at`).
- Run REST surface (`backend/routes/chunks.py`): `GET /runs/{id}/chunks` (returns
  `operator_state`), `/runs/{id}/plan-versions`, `/runs/{id}/pr-status`, plus the POST
  lifecycle (approve/reject/execute/resume/retry/steer/final/memory-conflict/push-pr).

---

## 2. Ideal Run Detail / Thread Timeline UX (the target)

One screen that reads as a **story of the run**, top to bottom:

```
┌──────────────────────────────────────────────────────────────────────┐
│  RUN HEADER (always visible)                                           │
│  "Add rate-limit guard to the upload route"        [Needs your review]│
│  run a1b2c3d8 · Chunk 2 of 3 · started 14:02 · github_cli              │
│  Plan ──●── Code ──●── Patch ──○── Test ──○── Review ──○── Ship        │
├──────────────────────────────────────────────────────────────────────┤
│  ⟶ STICKY NEXT-ACTION BANNER  (only when waiting_on = human)          │
│    "Approve the plan to begin coding."   [Approve plan] [Reject]      │
│    Writes files: No · Commits: No · Merges: Never                     │
├───────────────────────────────┬──────────────────────────────────────┤
│  TIMELINE (spine)             │  DETAIL PANEL (selected event)        │
│  ● 14:02 Run created          │  Plan approved · 14:05                │
│  ● 14:03 Triaged: feature     │  ───────────────────────────────────│
│  ● 14:04 Plan created (v1)    │  Plain English:                       │
│  ▸ 14:05 Plan approved  ◀sel  │   You approved the 3-chunk plan.      │
│  ● 14:06 Chunk 1 coding…      │   Coding can now begin. No files were │
│  ● 14:08 Chunk 1 tests passed │   written by approving.               │
│  ▸ 14:09 Chunk 1 awaiting…    │  Why it's safe: approval only unlocks │
│  …                            │   the next step; nothing is committed │
│                               │   without your review.                │
│                               │  ▸ Technical details (expand)         │
└───────────────────────────────┴──────────────────────────────────────┘
│  Details & audit (collapsed): raw EventLog, memory used, environment   │
└──────────────────────────────────────────────────────────────────────┘
```

Principles:
- **The timeline is the spine**, not a buried log. It is the default reading order.
- **Master-detail**: selecting a timeline event opens a detail panel with a plain-English
  summary, a "why this is safe / what it unlocks" line, and an expandable technical block.
- **The next action is never hunted for**: a sticky banner (the existing
  `OperatorAttentionPanel`, made sticky) carries the current decision and the real controls.
- **Safety copy is first-class**: every approval-class event states what changed, what it
  unlocks, and the standing guarantee ("Pipewright never merges automatically").
- **Live for in-progress, complete for terminal/reopened**: in-progress runs get live tail
  on top of the persisted backbone; terminal/reopened runs render the full backbone from the
  REST read-model (no dependence on the ephemeral buffer).

---

## 3. Event model — what appears in the thread, and in what order

### 3.1 Unified timeline entry (read-model shape)
The new REST read-model returns a list of **TimelineEntry** objects, deliberately shaped to be
compatible with the existing `RunEvent` so the frontend can merge persisted + live cleanly:

```
TimelineEntry {
  id: string            # STABLE, deterministic (see §3.4) — NOT a random uuid
  ts: string            # ISO; the persisted timestamp of the fact
  kind: RunEventKind    # reuse existing vocab (approval_required, chunk_status_changed, …)
  stage: RunEventStage | null
  chunk_number: number | null
  level: 'info' | 'warn' | 'error'
  category: 'lifecycle' | 'plan' | 'execution' | 'approval' | 'review' | 'memory' | 'ship'
  title: string         # plain-English headline ("Plan approved")
  detail: string        # plain-English body ("You approved the 3-chunk plan…")
  source: 'persisted' | 'live'   # provenance; persisted = restart-safe backbone
  data: Record<string, unknown>  # technical payload for the expandable block
}
```

### 3.2 The canonical story order (categories)
1. **lifecycle** — run created; triaged (intent/mode); run status transitions; terminal
   (complete / failed / rejected).
2. **plan** — chunk plan created (v1); plan revised (vN); plan approved / rejected.
3. **execution** — per chunk: coding started; patch applied; tests ran (+verdict);
   chunk awaiting approval; chunk approved / rejected; retry / steer; scope-expansion
   requested / decided.
4. **review** — adversarial review findings; test-validation ack; review-finding ack.
5. **approval** — every approval gate: required (created) and decided, with risk + summary.
6. **memory** — memory injected into this run (count + provenance); memory conflict gate;
   post-run memory suggestions generated (terminal).
7. **ship** — final approval; push; PR created (url/number); PR checks (on explicit refresh).

Within a run the entries are **strictly ordered by `ts`, then by a stable category/kind
tiebreak** so equal-timestamp rows (e.g. status change + gate created in the same tick) never
reorder between fetches.

### 3.3 Source-of-truth rule (persisted vs live)
- **Persisted backbone** (REST read-model) owns every milestone in §3.2. It is authoritative,
  restart-safe, and the only source for terminal/reopened runs.
- **Live tail** (existing WS) contributes only the **fine-grained, non-persisted progress**
  (`stage_started`, `stage_completed`, `log`, heartbeats) for *in-progress* runs.
- **Reconciliation**: the client keys on `id`. Persisted entries use stable ids (§3.4); live
  WS milestone events that duplicate a persisted fact are de-duped by mapping to the same
  stable id where feasible, otherwise the persisted entry wins on refetch. The existing
  `useRunEvents` dedupe-by-id already gives us half of this for free.

### 3.4 Stable id scheme (so refetch is idempotent)
`run:created` · `run:terminal:<status>` · `triage` · `plan:v<n>` · `plan:v<n>:approved` ·
`gate:<gate_id>:created` · `gate:<gate_id>:decided` · `chunk:<n>:status:<status>:<ts>` ·
`chunk:<n>:attempt:<attempt_id>` · `scope:<request_id>:created` ·
`scope:<request_id>:decided` · `memory:injection:<event_id>` · `review:<chunk>:<review_id>` ·
`pr:created`. (Live WS events keep their uuid `id`.)

---

## 4. Data inventory — exists vs. missing

| Timeline need | Status | Source |
|---|---|---|
| Run created / status transitions / terminal | **Exists (persisted)** | `pipeline_runs` |
| Triage intent / mode | **Exists (persisted)** | `pipeline_runs` (intent/mode fields) |
| Plan created / revised / approved | **Exists (persisted)** | `plan_versions`, `approval_gates` |
| Approval gate required + decided (+risk, summary) | **Exists (persisted)** | `approval_gates` |
| Chunk status transitions | **Exists (persisted)** | `chunks` |
| Chunk attempts (retry/steer history) | **Exists (persisted)** | `chunk_attempts` |
| Scope-expansion requested / decided | **Exists (persisted)** | `scope_expansion_requests` |
| Test-validation / review-finding acks | **Exists (persisted)** | `*_acknowledgements` |
| Adversarial review findings | **Exists (persisted)** | `chunk_reviews` |
| Memory injected into the run | **Exists (persisted)** | `memory_injection_events` |
| Final approval / push / PR created | **Exists (persisted)** | `pipeline_runs`, `approval_gates` |
| Plain-English narrative & next action | **Exists (derived)** | `operator_state.py` |
| Fine-grained stage/log progress | **Exists but EPHEMERAL** | in-memory bus (lost on restart) |
| **A single ordered REST timeline read-model** | **MISSING** | *(PR-0 builds it)* |
| **Per-event plain-English title/detail + "why safe"** | **MISSING (assembly)** | *(PR-0/PR-2: map persisted rows → copy)* |
| Persisted fine-grained progress for terminal runs | **MISSING** | *(PR-5, optional, only non-read-only PR)* |

**Conclusion:** the backbone is entirely derivable from existing persisted tables. We do **not**
need a new events table or any execution-path write to ship the core timeline. The only thing
that needs a write is *fine-grained progress replay for already-finished runs* — explicitly
deferred to an optional, flagged PR.

---

## 5. Smallest safe PR split

Ordering principle: **read-only first; recompose before restructure; no approval/Git behavior
change until the read-only spine is trusted.** Each PR is independently shippable and revertible.

### PR-0 — Backend: read-only timeline read-model *(read-only; no schema change)*
- **What:** new `backend/pipeline/run_timeline.py` (deriver) + `GET /runs/{run_id}/timeline`
  in `chunks.py`. Queries existing tables, returns ordered `TimelineEntry[]` (§3). Pure read.
  Plain-English `title`/`detail` mapping lives here (single source), reusing `statusDisplay`/
  `operator_state` vocabulary where natural. Sanitizes — never emit secrets, tokens, diffs,
  stack traces, or raw provider/Git errors into `data`.
- **Tests:** `backend/tests/test_run_timeline.py` (unit) — seed rows across all categories;
  assert ordering, stable ids, idempotent re-derivation, category mapping, empty-run case,
  unknown-run 404, and a redaction test (no secrets/diffs in payload).
- **Touches:** `backend/pipeline/run_timeline.py` (new), `backend/routes/chunks.py` (one GET),
  response model. **Must not touch:** any POST handler, `scope_guard`, `patch_applier`,
  `path_safety`, `chunked_orchestrator`, `event_bus`, schema.sql.

### PR-1 — Frontend: timeline data hook + read-only `RunTimeline` component *(additive)*
- **What:** `useRunTimeline(runId)` (React Query against PR-0) + `components/RunTimeline.tsx`
  (chronological, plain-English rows, selectable). Merge with live `useRunEvents` per §3.3.
  Render it **inside the existing "Details & audit" section first** (additive, nothing removed),
  next to the existing `EventLog`. No page restructure yet.
- **Tests:** component tests — renders ordered entries; empty/loading/error states; selection
  highlight; merge dedupe (persisted + live with same stable id renders once).
- **Touches:** new hook + component, small addition in `RunDetailPage.tsx`. **Must not touch:**
  `EventLog.tsx` (keep it), `useRunEvents.ts`, any mutation/approval wiring.

### PR-2 — Frontend: event detail panel (master-detail) *(read-only)*
- **What:** `components/RunTimelineDetail.tsx`. Selecting a timeline row shows plain-English
  summary + "what it unlocks / why it's safe" + expandable **Technical details** (raw `data`,
  kind/stage, ids). Reuse `runFlowCopy`, `memoryReasonHumanize`, `statusDisplay`.
- **Tests:** select → detail renders; expand reveals technical block; approval-class entries
  render safety copy and the "never merges" guarantee; no secrets in technical block.
- **Touches:** new component + wiring in `RunTimeline`. **Must not touch:** approval controls.

### PR-3 — Frontend: promote timeline to primary layout + sticky next-action banner *(recompose; no behavior change)*
- **What:** Restructure `RunDetailPage` so the **timeline is the spine** and the status header
  + `PipelineRail` sit on top. Make the **existing `OperatorAttentionPanel` sticky** as the
  next-action/approval banner (it is *already wired to the real mutations* — we change position
  and prominence, not logic). The real controls (`ChunkPlanPanel`, "Finish & ship", gate card,
  `MemoryConflictPanel`) stay exactly as-is, reachable from the timeline/detail. Demote the raw
  `EventLog` into developer view (PR-4) / "Details & audit".
- **Tests:** render per run status (planning, awaiting_*, running, terminal, failed, rejected);
  assert the sticky banner exposes the **same** actions/handlers as today (the mutation
  functions are unchanged — assert wiring identity, not new behavior); assert no approval gate
  can be reached that wasn't reachable before.
- **Touches:** `RunDetailPage.tsx` (layout/composition), light CSS. **Must not touch:** any
  mutation, any `runsApi`/`gatesApi` call signature, gate-derivation predicates, `operator_state`.

### PR-4 — Frontend: plain-English ⇄ developer view toggle *(read-only)*
- **What:** global toggle (persisted in `localStorage`), **plain-English default.** Developer
  mode reveals raw `kind/stage` labels, the raw `EventLog`, ids, and `data` payloads; reuses
  `formatStatusLabel` (dev) vs `friendlyStatusLabels` (plain). Affects presentation only.
- **Tests:** default is plain; toggle flips labels and reveals technical fields; preference
  persists; no data fetched differently between modes.
- **Touches:** small shared context/util + consuming components. **Must not touch:** data layer.

### PR-5 — *(Optional, deferred)* Persist fine-grained progress for terminal-run replay
- **What:** the **only** PR that adds an execution-path write: persist live `stage_*`/`log`
  events so finished runs can replay fine-grained progress after restart. Additive, fail-safe
  (mirror `event_bus.publish`'s "never affect execution" rule), behind a default-off flag
  (e.g. `RUN_EVENT_PERSISTENCE_ENABLED`). **Recommend deferring** unless milestone-level replay
  proves insufficient in self-use. Needs its own one-page brief before any prompt.
- **Touches:** `event_bus` (additive sink), a new table (schema migration), the timeline
  read-model (merge persisted progress). **Must not touch:** execution control flow, approvals.

---

## 6. Component structure (frontend)

```
pages/RunDetailPage.tsx           # orchestrates; shrinks as logic moves into pieces
  components/run/
    RunHeader.tsx                 # title, status badge, meta, PipelineRail (extracted)
    NextActionBanner.tsx          # sticky wrapper around existing OperatorAttentionPanel
    RunTimeline.tsx               # the spine: ordered TimelineEntry rows, selection
    RunTimelineRow.tsx            # one entry (icon by category, plain title, time)
    RunTimelineDetail.tsx         # master-detail panel + expandable technical block
  hooks/
    useRunTimeline.ts             # React Query → GET /runs/{id}/timeline
    useRunEvents.ts               # UNCHANGED (live tail)
  context/
    ViewModeContext.tsx           # plain (default) ⇄ developer (PR-4)
  lib/timelineCopy.ts             # plain-English titles/details + safety copy (mirrors backend)
```
Reuse, do not duplicate: `OperatorAttentionPanel`, `ChunkPlanPanel`, `FinalApprovalPanel`,
`PushPrPanel`, `PrStatusPanel`, `RunSafetyStrip`, `RunStatusBadge`, `statusDisplay`,
`runFlowCopy`, `operatorPrimaryAction`. The approval/exec controls are **moved/reframed, never
rewritten.**

---

## 7. States (loading / empty / error / blocked / approval)

For every read-only surface, define all five — failing safe and never implying a write:

- **Loading:** skeleton timeline rows + header placeholder. Live badge shows
  `connecting`/`reconnecting` from `useRunEvents` (already exists). Never blank.
- **Empty:** brand-new run with no events yet → "Run is starting — steps will appear here."
  (Mirror `EventLog`'s existing empty copy; never look broken.)
- **Error:** timeline fetch fails → inline, non-destructive error with Retry; the run header
  still renders from the `run` query. WS drop → degrade to "Polling" (already handled). A
  failed run renders its terminal lifecycle entry plus "Nothing was pushed; no merge performed."
- **Blocked:** reuse `operator_state.blocked_actions` → "Can't do yet: <label> — <reason>"
  (the panel already renders this). Blocked actions are **informational, never clickable.**
- **Approval (waiting_on = human):** sticky banner is prominent; the matching timeline entry is
  marked "awaiting your review"; the real approve/reject controls are the existing, enforced
  ones. Approval copy always states: what changed, what approval unlocks, and "Pipewright never
  merges automatically."

---

## 8. Developer view vs. plain-English view

- **Default = plain-English.** Audience: a human supervising the run. Uses `friendlyStatusLabels`,
  `title`/`detail`, category icons, safety copy. Hides ids, raw kind/stage, payloads.
- **Developer view (toggle, persisted):** adds raw `kind/stage` labels, `formatStatusLabel`,
  the existing monospace `EventLog`, event ids, `data` payloads, provider/model diagnostics.
- The toggle is **presentation only** — same data, same fetches, same controls. It never unlocks
  an action or changes an approval. The per-event "Technical details" expander (PR-2) works in
  both modes so a plain-mode user can still drill into one event without flipping global mode.

---

## 9. Tests (summary; details per-PR in §5)
- **PR-0:** backend unit — ordering, stable/idempotent ids, category mapping, empty + unknown
  run, **redaction** (no secrets/diffs/tokens/stack traces in payload).
- **PR-1:** timeline render, empty/loading/error, selection, persisted+live dedupe.
- **PR-2:** detail render, technical expand, approval safety copy + "never merges," redaction.
- **PR-3:** layout per status; sticky banner exposes the **same** handlers (wiring identity);
  no newly-reachable approval path.
- **PR-4:** plain default; toggle flips labels/reveals technical; preference persists; identical
  data layer across modes.
- **PR-5 (if taken):** persistence is additive + fail-safe (publish failure never breaks a run);
  flag default-off; terminal-run replay equals live order.

---

## 10. Likely touched / must-not-touch files

**Likely touched (by PR):**
- Backend: `backend/pipeline/run_timeline.py` (new), `backend/routes/chunks.py` (one GET),
  response models; *(PR-5 only)* `backend/events/*` + a new table.
- Frontend: `pages/RunDetailPage.tsx`, new `components/run/*`, `hooks/useRunTimeline.ts`,
  `context/ViewModeContext.tsx`, `lib/timelineCopy.ts`, `api/client.ts` (one typed GET).

**Must NOT touch (enforced safety surface + this brief's guardrails):**
- `backend/pipeline/scope_guard.py`, `patch_applier.py`, `path_safety.py`,
  `chunked_orchestrator.py` (execution control flow), `tester.py`, `reviewer.py`.
- Any **POST** lifecycle handler in `backend/routes/chunks.py` (approve/reject/execute/resume/
  retry/steer/final-approval/memory-conflict/push-pr) — read-only PRs add a GET only.
- `backend/events/event_bus.py` publishing on the execution path (untouched until PR-5).
- Memory retrieval/injection: `backend/memory/prompt_builder.py`, `memory_store.py`,
  `injection_*`, `bootstrap.py`. **No memory retrieval changes.**
- `backend/llm/role_config.py` and provider selection.
- FTS (`row-19*`), Row 23 vectors, `sqlite-vector-memory-readiness` — **untouched.**
- Frontend: `useRunEvents.ts` (PR-1..4), the real mutation wiring in `RunDetailPage`,
  `ChunkPlanPanel`/`FinalApprovalPanel`/`PushPrPanel` control logic.

---

## 11. Safety analysis (against the safety contract)
- **No implementation without approved plan / no scope grant:** PRs 0–4 add only a GET and
  presentation; they cannot grant scope, approve, commit, push, or merge.
- **Approval gates unchanged:** the sticky banner reuses the *existing* wired controls; PR-3
  asserts wiring identity and no newly-reachable gate. Final-approval bypass is impossible —
  no approval code changes.
- **Git/PR untouched:** no push/PR/merge code is modified; ship copy keeps "never merges."
- **Secrets/PII:** the timeline read-model sanitizes; redaction is a required test in PR-0/PR-2.
  We never put diffs, tokens, stack traces, or raw provider/Git errors into `data`.
- **Memory advisory & untouched:** no retrieval/injection/promotion change; memory appears in
  the timeline as read-only provenance only.
- **Fail safe:** every state (§7) degrades to a clear read-only message; the live bus staying
  fail-safe is preserved (PR-5 keeps the "never affect execution" rule).

---

## 12. Open decisions to confirm before PR-0

> **Resolved 2026-06-17 (see §13).** All four were taken as recommended: (1) derive
> the timeline from persisted tables — no events table; (2) defer PR-5 fine-grained
> persistence; (3) two-view model = plain default + per-event technical expander +
> global developer toggle; (4) doc location/cadence as here, with per-PR delta notes
> in §13.

1. **Timeline source (recommended: derive from persisted tables).** Confirm we build the
   read-only read-model rather than adding an events table now. *(Recommended — it's read-only,
   restart-safe, and avoids any execution-path write.)*
2. **Persisted fine-grained progress (PR-5).** Defer unless milestone-level replay proves
   insufficient in self-use? *(Recommended: defer.)*
3. **Two-view model (recommended: plain default + per-event technical expander + global dev
   toggle).** Confirm this rather than a hard split.
4. **Doc location / PR cadence.** This brief lives at `docs/design/phase-2f-thread-ui.md`;
   each PR gets a short delta note here on merge (matches existing row-closeout convention).
```

---

## 13. Implementation closeout (2026-06-17)

**Phase 2F Thread UI / Run Timeline is COMPLETE & MERGED** through PR-0..PR-4, plus
review-fix follow-ups PR-A/PR-B/PR-C. It shipped exactly as framed in §0–§11: a
read-only timeline spine derived from existing persisted tables, plus presentation
recomposition of the Run Detail page — **no execution-path write, no schema change,
no new events table.** §12's four open decisions were all resolved as recommended.

### Delta notes (per the §12.4 convention)

- **PR-0 — backend read-only `GET /runs/{run_id}/timeline`.** New
  `backend/pipeline/run_timeline.py` deriver + one GET in `chunks.py`; queries the
  existing persisted tables only and returns ordered `TimelineEntry[]` with stable,
  idempotent ids and sanitized `data`. Pure read — no schema change, no write.
- **PR-1 — frontend `useRunTimeline` + additive `RunTimeline`.** React Query hook
  against PR-0 plus a read-only chronological component, merged with the live
  `useRunEvents` tail by stable id. Additive; `EventLog` / `useRunEvents` untouched.
- **PR-2 — read-only `RunTimelineDetail`.** Master-detail panel: plain-English
  summary + "what it unlocks / why it's safe" + an expandable technical block. No
  approval-control change.
- **PR-3 — timeline promoted to the primary Run Detail layout.** The timeline is now
  the spine; the existing `OperatorAttentionPanel` was made sticky/prominent as the
  next-action banner. The real approval/exec controls were moved/reframed, not
  rewritten — same wired mutations, no newly-reachable gate.
- **PR-4 — Plain English / Developer view toggle.** Presentation-only, plain default,
  persisted in `localStorage`; same data, fetches, and controls in both modes.
- **PR-A — backend timeline correctness/redaction fixes.** Hardened the read-model
  correctness and redaction tests; no write / POST / schema change.
- **PR-B — persisted/live dedupe + timeline refresh fixes.** Fixed persisted-vs-live
  reconciliation (dedupe by stable id) and timeline refresh; no data-layer authority
  change.
- **PR-C — redaction polish, sticky height, `localStorage` guard, a11y.**
  Presentation/robustness polish only.

### Invariants held

- **No PR-5 / event persistence started.** No new schema or event table; the
  in-memory `event_bus` execution-path publish is untouched.
- **No backend writes and no POST lifecycle handler changes.** The only backend
  surface added is the single read-only `GET /runs/{run_id}/timeline`.
- **No approval / final-approval / Git / PR behavior change.** The sticky banner
  reuses the existing wired controls; no gate became newly reachable.
- **No memory-retrieval / injection change**; memory appears in the timeline as
  read-only provenance only.
- **No FTS / Row 19 activation and no Row 23 / vector work.**

### Deferred (unchanged)

- **PR-5 — fine-grained event persistence** for terminal-run replay. It is the only
  unshipped slice of this brief and needs its own one-page brief before any prompt.
