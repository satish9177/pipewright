# Phase 2G — Run Detail Product UI

> **Status: COMPLETE & MERGED (2026-06-17).** The Phase 2G design/spec is
> complete. This docs closeout records the merged frontend presentation slices;
> the document itself introduces no frontend, backend, API, route, schema, or
> behavior changes.
>
> **Source material:** the Claude Design handoff bundle
> `Pipewright-handoff (2).zip`, primary file
> `pipewright/project/Run Detail - Canonical States.html` and its imports under
> `pipewright/project/canonical/` (`app.jsx`, `shell.jsx`, `decision.jsx`,
> `timeline.jsx`, `primitives.jsx`, `data.js`, `pw.css`). The
> `uploads/Screenshot 2026-06-17 02:16xx.png` images are screenshots of the
> **current** running frontend (the "Done" run at `localhost:8001`) and are used
> here as the UI audit baseline — i.e. what we have today vs. the canonical
> target. (The `.pptx` is an export of the same prototype; the HTML/JSX is the
> canonical source and was read directly per the bundle README.)

---

## Status closeout

Phase 2G Run Detail Product UI is complete and merged. The implementation landed
as frontend presentation/composition slices only:

- **PR-1:** two-column cockpit/context shell merged.
- **PR-2:** Running and Needs-review context rail trust facts merged.
- **PR-4:** Done-state PR de-duplication and authoritative PR rail merged.
- **PR-5:** Failed-state failure rail and quieter failed banner merged.
- **PR-3:** decision evidence near the approval cockpit merged.
- **PR-6:** visual/register polish and Plain/Developer mode cleanup merged.

Final state: Run Detail is organized around the cockpit, safety overview, context
rail, decision evidence, timeline, and collapsed audit/details.

Safety invariants preserved: no backend behavior changes; no mutation handler
changes; no approval/final approval/Git/PR behavior changes; no new actions; no
event persistence / Phase 2F PR-5 work; no memory retrieval changes; no FTS/Row
19 changes; and no Row 23 work.

Validation recorded across the slices: build/lint/diff checks passed per slice;
PR-3 demo-smoke passed all 10 checks; PR-6 SSR smoke passed running/final
approval/done/failed across Plain and Developer modes; protected-path checks
confirmed frontend/docs-only scope per slice.

---

## 0. What this is, in one paragraph

The Run Detail page (`frontend/src/pages/RunDetailPage.tsx`) is functionally
complete and already shipped the #35 guided-UX redesign, the #37/#38 calm-chrome
and cockpit work, and the Phase 2F two-pane Run Timeline. **Phase 2G is a
visual/IA finishing pass, not new capability.** It takes the canonical-states
prototype as the target look-and-feel and resolves the one structural weakness
the prototype itself still carries into the current build: the page is a single
long column, and in the **Done** state the pull request is described four-to-five
times. Phase 2G introduces the prototype's two-column "cockpit + context rail"
layout, pulls the decision evidence up next to the approval action, gives Failed
a real failure surface, and collapses PR information to one authoritative place.

Everything below preserves every safety guarantee, approval gate, mutation
handler, and audit surface unchanged. **No backend, no mutation wiring, no
approval/Git/PR behavior, no event persistence (PR-5), no memory retrieval, no
FTS/Row 19, no Row 23.** See §8.

---

## 1. What from the prototype is accepted as the target direction

Accepted (this is the Phase 2G target):

1. **App shell with a fixed left workspace sidebar** (`shell.jsx` `Sidebar`) and a
   scrolling main column. Pipewright already has `components/Layout.tsx`; we adopt
   the prototype's *spacing, brand mark, and "This run" affordance* within the
   existing Layout, not a new shell.
2. **A single guided "cockpit" as the hero** — one calm instrument at the top that
   says who the run is waiting on, what happened, why, and the one safe next
   action. This is the existing `OperatorAttentionPanel`; the prototype calls it
   the *guided spine* and gives it a `mood`/`moodbar` accent that we already ship
   (#38A).
3. **A two-column body: primary cockpit column + a right "context rail."** The
   rail holds the state-specific *evidence/identity* for the run — PR identity
   (Done), failure detail (Failed), "what you're approving" trust facts (Needs
   review), or "while you wait" reassurance (Running). This is the prototype's
   `runwrap` → `primary-col` + `ctx-rail` and is the single biggest accepted
   change.
4. **Decision evidence promoted next to the action** (`decision.jsx`
   `DecisionEvidence`): the diff, the test verdict, and the advisory review render
   in the primary column adjacent to the approval CTA, instead of only living deep
   inside the chunk panel.
5. **Safety overview as compact honest chips + process gates** (`shell.jsx`
   `SafetyOverview`). Already shipped as `RunSafetyStrip`; we keep it and align
   wording/placement.
6. **Two-pane Run Timeline (list + detail), read-only** (`timeline.jsx`
   `RunTimeline`). Already shipped in Phase 2F; accepted as-is, no rework.
7. **Plain English / Developer mode toggle** in the header, persisted, with
   per-field plain-vs-dev copy across the page (`shell.jsx` `ModeToggle`,
   `data.js` `{ plain, dev }`). Toggle already exists; Phase 2G *broadens* what
   the toggle actually changes (see §4).
8. **Details & audit collapsed at the bottom** (memory-used, environment/provider
   diagnostics, raw log in dev). Already shipped; accepted as-is.
9. **The honest framing tokens**: "process gates, not code correctness", "read-
   only summary of existing signals", "Pipewright never merges automatically",
   "Can't do yet: …" blocked actions, "preview / display-only" disclaimers,
   effects ledgers ("what happens if you click"). These are core to the product's
   trust contract and are all kept.

Accepted as *visual language* only (tokens, type scale, spacing, color roles)
from `pw.css` / the design-system bundle — **not** as literal CSS to copy.
Pipewright is Tailwind + a small token layer (`pw-tokens.css`, `index.css`); the
prototype's `--pw-*` tokens already align with `pw-tokens.css`. Map intent, not
selectors.

Explicitly **not** adopted from the prototype:

- The **DEMO state switcher** (`shell.jsx` `StateSwitcher`) — it is a prototype
  affordance to flip between fixtures; the real page derives state from the run.
- The **annotation pins** (`primitives.jsx` `Pin`) and the "Design rationale"
  view — prototype documentation chrome.
- The prototype's **hard-coded fixtures** (`data.js`) — the real page is data-
  driven from `run`, `chunkPlan.operator_state`, `gates`, `project`, and the
  timeline read model.
- Any **new buttons or new actions.** Phase 2G adds zero controls.

---

## 2. The four canonical states

The prototype models four states the user "actually lands on." The real backend
has more granular statuses; Phase 2G treats the four as **presentation
archetypes** and maps every real status into one. The mood/archetype is already
derivable today from `operator_state.waiting_on`, `operator_state.phase`, and
`run.status` — Phase 2G changes presentation, not derivation.

| Canonical state | Mood / accent | `waiting_on` | Backend `run.status` family | Existing derivation |
| --- | --- | --- | --- | --- |
| **Running / Working** | system (blue) | `system` | `running`, `running_chunks`, `pushing` | `WORKING_STATUSES`, `PHASE.working`, mood bar `bg-blue-400` |
| **Needs review / final approval** | human (amber) | `human` | `awaiting_final_approval` (hero case), plus the sibling "needs you" gates: `awaiting_chunk_plan_approval`, `awaiting_chunk_approval`, `awaiting_scope_approval`, `awaiting_memory_conflict_approval`, `paused` | `PHASE.waiting_for_you`, pending-gate logic, mood bar `bg-amber-400` |
| **Done / PR open** | nobody (green) | `nobody` | `complete` (hero case), plus transitional `final_approved` | `PHASE.done`, mood bar `bg-emerald-400` |
| **Failed** | fail (red) | n/a | `failed` (hero case), plus `rejected`, `final_rejected`, `push_failed` | `PHASE.stopped` / `needs_attention`, `run.status === 'failed'` banner |

Per-state target behavior:

### 2.1 Running / Working
- **Cockpit:** system mood bar; "Working" tag; headline = "Pipewright is writing
  the change" (plain) / stage-level dev line; a spinner working line; **no primary
  action**; blocked actions explain why Approve/Create-PR aren't available yet.
- **Context rail:** "While you wait" — trust facts (`Waiting on`, `Will pause
  before commit`, `Pushed so far: nothing`) + reassurance copy.
- **Safety overview:** scope/tests/review pending (`not_evaluated`), PR n/a.
- **No decision evidence, no Finish & ship, no PR card.**

### 2.2 Needs review / final approval
- **Cockpit:** human mood bar; "It's your move" + "Final approval"; headline =
  "Approve this change before it leaves your machine"; **one primary CTA**
  ("Approve & allow push") with its effects ledger (writes: no, commits: yes,
  pushes: on approve, merges: never) + secondary Reject / Open diff; blocked "Create
  PR" explained.
- **Primary column also shows Decision evidence** (the change diff, test verdict
  "14 passed / 0 failed · Strong", advisory review verdict + findings +
  independence). This is the core "what you're approving" content.
- **Context rail:** "Before you approve" trust facts (scope, tests, review,
  pushed-so-far) + the no-merge reassurance.
- Risk decisions stay **co-equal** (no recommended primary) exactly as the
  cockpit already renders them.

### 2.3 Done / PR open
- **Cockpit:** green mood bar; "Done" + "Nothing needed"; headline = "Pull request
  is ready"; **no primary action**; secondary "Open on GitHub" + "Refresh PR
  checks"; blocked "Create PR" explained ("already exists").
- **Context rail: the single authoritative PR card** — state, PR #, branch, link,
  checks ("not fetched"), and the manual-refresh note.
- **Finish & ship** stays as the ordered record of what happened (final approval
  ✓, push/PR ✓) **but stops repeating PR identity** — see §5.1.
- Success banner is trimmed to a one-line confirmation (no second PR URL).

### 2.4 Failed
- **Cockpit:** red mood bar; "Stopped" + "Nothing pushed"; headline = "The run
  stopped — a test failed"; explanation points to the timeline; **no primary
  action**; secondary View timeline / Open diff; an out-of-app terminal hint to
  start a fresh run; blocked Approve/Create-PR explained.
- **Context rail: a real Failure card** — failing stage, one-line summary, the
  specific failure detail (e.g. failing test id), and the "working tree is
  unchanged / patch rolled back" reassurance — plus trust facts (committed:
  nothing, pushed: nothing, repo: unchanged).
- This replaces today's thin one-paragraph red banner.

---

## 3. Final target layout

Top-to-bottom, with the two-column body. Widths/spacing follow the prototype
(`pw.css`: `max-width` ≈ 1160–1180px content, primary column ≈ 760px, rail ≈
320–360px, single column under ~960px).

```
┌ Layout sidebar (existing) ─┬───────────────────────────────────────────────┐
│ pipewright                 │  HEADER                                        │
│ Projects                   │   eyebrow "Pipeline run"                       │
│ Approval queue             │   <task title>                                 │
│ Memory                     │   meta: run <id> · Chunk N of M [· project dev]│
│ ─ This run                 │   right: <RunStatusBadge>  [Plain | Developer] │
│   Run detail (active)      │                                                │
│                            │  PIPELINE RAIL  Plan·Code·Patch·Test·Review·Ship│
│                            │                                                │
│                            │  SAFETY OVERVIEW  chips + process gates + note │
│                            │                                                │
│ localhost:8001 ●           │  ── BODY: two columns ──────────────────────── │
└────────────────────────────┤  ┌ PRIMARY (cockpit) ──┐ ┌ CONTEXT RAIL ────┐ │
                             │  │ COCKPIT (hero)       │ │ state-specific:  │ │
                             │  │  mood bar            │ │  • Running: trust│ │
                             │  │  phase + waiting pill│ │    facts + wait  │ │
                             │  │  headline + why      │ │  • Review: "before│ │
                             │  │  [working spinner]   │ │    you approve"  │ │
                             │  │  primary CTA+effects │ │    trust facts   │ │
                             │  │  secondary actions   │ │  • Done: PR card │ │
                             │  │  blocked actions     │ │    (authoritative)│ │
                             │  │  out-of-app terminal │ │  • Failed:       │ │
                             │  │                      │ │    Failure card  │ │
                             │  │ DECISION EVIDENCE    │ │    + trust facts │ │
                             │  │  (review state only):│ │                  │ │
                             │  │   diff               │ └──────────────────┘ │
                             │  │   test verdict       │                      │
                             │  │   advisory review    │                      │
                             │  │                      │                      │
                             │  │ FINISH & SHIP        │  (final-stage states │
                             │  │  (final approval →   │   only; spans/below  │
                             │  │   push/PR → checks)  │   the columns)       │
                             │  └──────────────────────┘                      │
                             │                                                │
                             │  RUN TIMELINE  (two-pane list + detail, RO)    │
                             │                                                │
                             │  MEMORY SUGGESTIONS  (terminal states)         │
                             │  CHUNK HISTORY  (collapsed disclosure, finished)│
                             │                                                │
                             │  DETAILS & AUDIT  (collapsed: memory · env ·   │
                             │                    raw log[dev])               │
                             │  [Back]                                        │
                             └────────────────────────────────────────────────┘
```

Section-by-section target:

- **Header.** Eyebrow + task title (`run.feature_description`), meta row (`run`
  short id, `Chunk N of M`, project id in dev), right side `RunStatusBadge` +
  Plain/Developer toggle. Unchanged in content; aligned to prototype type scale.
- **Pipeline rail.** Six stages Plan→Ship, done/current/failed states. Already
  matches the prototype closely (`PipelineRail` in `RunDetailPage.tsx`).
- **Safety overview.** `RunSafetyStrip` chips (Scope/Tests/Review/PR) + process
  gates + "summarizes signals below" note. A FAIL/WEAK chip must remain
  impossible to hide (the prototype's critical-chip banner idea is satisfied today
  by the chip tone + the cockpit/failure surfaces; keep that invariant).
- **Primary cockpit (hero).** `OperatorAttentionPanel`, unchanged wiring.
- **Decision evidence.** Diff + test verdict + advisory review, shown in the
  primary column **only in the needs-review archetype**. Sourced from the existing
  per-chunk evidence (`ChunkPlanPanel` / `AdvisoryReviewPanel`); see §6 for the
  relocation note and §7 PR-3 for the conservative approach.
- **Safety overview / cockpit own the FAIL surfacing**; rail never the only place
  a critical signal appears.
- **Context rail.** New right column; content per §2. Reads run/chunk/project
  fields only.
- **Finish & ship.** Existing `FinishStep` stepper for `awaiting_final_approval`
  / push / checks. Stays, but PR identity is de-duplicated (§5.1). It renders
  full-width below the two columns (it is its own decision context in final-stage
  states).
- **Run timeline.** `RunTimeline` two-pane, read-only. No change.
- **Memory suggestions.** `RunMemorySuggestionsDigest` + `RunMemorySuggestions`
  in terminal states. No change (generation is an existing action; black box).
- **Chunk history.** Existing collapsed disclosure for finished runs. No change.
- **Details & audit.** Existing collapsed disclosure (memory provenance, provider
  diagnostics, dev-only raw log). No change.

> Layout note: the cockpit + decision evidence live in the primary column; the
> rail is *supplementary context*, never the only home of an action or a critical
> safety signal. On narrow viewports the rail stacks **below** the cockpit so the
> guided action is always first.

---

## 4. Plain English vs Developer mode

Today the toggle exists (`RunViewMode` in `types/viewMode.ts`, persisted in
`RunDetailPage`) but changes relatively little: it adds the raw event log in
Details & audit and switches the timeline detail to show technical JSON. The
backend `operator_state.narrative` already supplies plain-language headline/why,
so the cockpit reads "plain" regardless.

The prototype treats Plain/Developer as a **page-wide register switch**, with
`{ plain, dev }` copy on nearly every field. Phase 2G adopts the prototype's
*intent* (consistent register) while respecting the source-of-truth rule:
**plain copy comes from the backend narrative; dev copy is an additive, display-
only technical overlay — never a different claim.**

What differs by mode in the Phase 2G target:

| Surface | Plain English | Developer |
| --- | --- | --- |
| Header meta | `run 84a2a15a · Chunk 1 of 1` | also full run id (on hover today) + project id |
| Run status badge | friendly ("Done", "Needs your review") | raw status token (`COMPLETE`, `AWAITING_FINAL_APPROVAL`) |
| Cockpit headline / why | `narrative.what_happened` / `why` | stage/state-level technical phrasing (e.g. `operator_state.primary_action = approve_final…`) |
| Effects ledger | "pushes: on approve" | `approve_final → push + create_pr` style |
| Safety chips | "Tests: Strong" | "Tests: strong" / `passed` enum-aligned values |
| Process gates note | "detailed cards remain the source of truth" | "summarize operator_state.safety_checks …" |
| Trust facts (rail) | "Pushed so far: nothing" | "pushes = 0" |
| PR card note (Done) | "Checks refresh only when you ask" | "checks_status = not_fetched · refresh = manual" |
| Failure card (Failed) | "1 test failed when Pipewright ran the suite" | "pytest: 1 failed, 13 passed · exit=1" + working_tree=clean |
| Timeline detail | "Why this matters" prose | + technical JSON block (already shipped) |
| Details & audit | memory · env | + raw live event log (already shipped) |

Rules:

1. **Developer mode is additive and honest.** It may expose ids, enums, counts,
   and provider/model strings; it must never weaken or contradict the plain claim
   (the "never understate an effect" rule from `OperatorAttentionPanel` effects).
2. **Where the backend already supplies dev-flavored text** (narrative,
   safety_checks `detail`, `out_of_app_instruction`), use it; do **not** invent a
   parallel dev string in the frontend that could drift.
3. **No mode gates a safety signal.** A FAIL/WEAK chip, a blocked action, or the
   no-merge guarantee shows in both modes identically.
4. Mode is persisted (existing `pipewright.runDetail.viewMode` localStorage key,
   with the existing fail-safe to `plain`).

Phase 2G is *not* required to wire dev copy on every row in one slice; the table
is the target. Slices may land it surface-by-surface, but each surface must keep
the additive-only contract.

---

## 5. Prototype ideas that must be adjusted before implementation

### 5.1 Remove the remaining Done-state PR duplication (primary cleanup)

This is the single most important correction. **Both the current build and the
prototype describe the pull request too many times in the Done state.**

Current build (from the audit screenshots), the PR appears **four** times in
Done:

1. `RunSafetyStrip` → "PR: PR open" chip.
2. Finish & ship step 2 "Push / create PR" → `PushPrPanel` "GitHub PR" card
   (COMPLETE) with Branch, PR number, full PR URL, and a "Pull request created:
   <url>" result line.
3. Finish & ship step 3 "Pull request & checks" → `PrStatusPanel` "PR Status"
   card (PR open) with Branch, PR number, "Open on GitHub", "Refresh PR checks".
4. The green `run.status === 'complete'` banner → "Your pull request is open on
   GitHub: <url>".

The prototype would make it **worse** (five+): it *adds* a context-rail `PrMini`
**and keeps both ship-stepper PR cards** (`app.jsx` `ShipStepper` →
`ShipCard kind:"complete"` and `kind:"pr"`) **and** the success `RunBanner` PR
line.

**Target for Phase 2G — exactly one authoritative PR identity surface in Done,
plus exactly one PR action:**

- **Authoritative PR identity = the context-rail PR card** (state, PR #, branch,
  "Open on GitHub" link, checks status, manual-refresh note). This is the single
  place a user reads "what/where is the PR."
- **The one PR action = "Refresh PR checks"**, owned by `PrStatusPanel`, kept in
  exactly one location. Decision: keep the refresh affordance **inside the rail PR
  card** (preferred) so identity and its only action sit together; if that is too
  invasive for `PrStatusPanel` as a black box, keep `PrStatusPanel` as the single
  PR surface and **drop the rail card's duplicate fields** instead. Either way the
  end state is: **PR identity once, refresh once.**
- **Finish & ship** keeps step rows as a *record of completion* ("Final approval
  ✓", "Push / create PR ✓") but **stops re-printing branch/PR#/URL** in the
  completed step — a one-line "Pull request opened — see PR panel" is enough.
- **The completed-run banner** is reduced to a single confirmation line and **must
  not repeat the PR URL** (the rail card has it). Keep the "Pipewright never
  merges automatically" sentence — that is a trust statement, not duplication.
- The `RunSafetyStrip` "PR: PR open" chip **stays** — it is a one-word status
  summary, not a duplicate description, and removing it would break the four-chip
  safety symmetry.

Net: one PR card (identity + checks), one chip (status word), one stepper tick
(history), one trust line. No URL printed twice.

### 5.2 Don't ship the DEMO scaffolding

`StateSwitcher`, annotation `Pin`s, the "Design rationale" route, and the
`[DEMO] Approved/Rejected` resolved-action feedback in `decision.jsx` are
prototype-only. The real cockpit's primary CTA runs the real mutation via the
existing resolver; there is no demo echo.

### 5.3 Decision evidence must stay data-driven and single-chunk-aware

The prototype's `DecisionEvidence` is a fixed single-chunk diff. The real run can
be multi-chunk. Surface evidence for **the chunk the pending gate belongs to**
(the page already computes `pendingGate.chunk_number` and the matching
`test_validation.verdict`); do not assume one chunk. For multi-chunk runs the
full per-chunk evidence stays in `ChunkPlanPanel`; the cockpit-adjacent evidence
is the *current decision's* chunk only. (See §7 PR-3 for the conservative path.)

### 5.4 Effects ledger / trust facts are already richer in code — keep the code's version

The prototype hard-codes effect strings. The shipped `OperatorAttentionPanel`
already has a vetted `ACTION_EFFECTS` table keyed off wired handlers and a
"never understate" rule. **Keep the code's ledger and the backend trust_facts**;
treat the prototype's strings as copy reference only.

### 5.5 "Open diff" / "View timeline" secondary actions

The prototype shows quiet secondary buttons ("Open diff", "View timeline"). These
should be in-page anchors/scrolls to the existing diff (decision evidence /
chunk panel) and the existing timeline — **not** new modals or new data. If an
anchor target isn't cleanly addressable, omit the button rather than add chrome.

### 5.6 Rail must not become a second action column

The context rail is **evidence and identity**, not a second place to act. The
only interactive element the rail may host is the single PR "Open on GitHub" link
and (per §5.1 decision) the one "Refresh PR checks" control. Everything else in
the rail is read-only.

---

## 6. Mapping: prototype pieces → current frontend

Treat every "mutation/action" component as a **black box** — Phase 2G composes
and restyles them, it does not change their props, conditions, handlers, or
behavior.

| Prototype piece (file) | Current frontend home | Phase 2G action |
| --- | --- | --- |
| `shell.jsx` `Sidebar` | `components/Layout.tsx` | Align spacing/brand/"This run" affordance within existing Layout. No new shell. |
| `shell.jsx` `StateSwitcher` | — (none) | **Do not build.** Demo-only. |
| `shell.jsx` `ModeToggle` | header toggle in `RunDetailPage.tsx` + `types/viewMode.ts` | Keep; broaden what it switches (§4). |
| `shell.jsx` `RunHeader` / `BigBadge` | header block in `RunDetailPage.tsx` + `RunStatusBadge` | Restyle to prototype type scale; badge already supports friendly/raw. |
| `shell.jsx` `PipelineRail` | `PipelineRail` (inline in `RunDetailPage.tsx`) | Keep; minor visual alignment only. |
| `shell.jsx` `SafetyOverview` | `components/RunSafetyStrip.tsx` | Keep; align wording/placement. Owns process gates + unknown-state banner already. |
| `decision.jsx` `GuidedSpine` (cockpit) | `components/OperatorAttentionPanel.tsx` | Keep wiring; it is the hero. Mood bar, blocked actions, trust facts, effects ledger, out-of-app already present. |
| `decision.jsx` `Blocked` / `Terminal` | inside `OperatorAttentionPanel` (`BlockedActions`, `out_of_app_instruction`) | Keep. |
| `decision.jsx` `TrustFacts` | inside `OperatorAttentionPanel` (`TrustFacts`) + backend `operator_state.trust_facts` | **Relocate/duplicate read-only** into the context rail per state (§2). Source stays `operator_state.trust_facts`. |
| `decision.jsx` `DecisionEvidence` (diff + test verdict + advisory review) | currently rendered per-chunk inside `components/ChunkPlanPanel.tsx` (which renders `components/AdvisoryReviewPanel.tsx`, test validation, and the diff) | Surface the *current-decision chunk's* evidence in the primary column for the review archetype. Conservative options in §7 PR-3. Black-box `AdvisoryReviewPanel`. |
| `decision.jsx` `DiffView` | diff in `ChunkPlanPanel` and the legacy "Human Approval Required" card (`pendingGate.diff`) | Reuse existing diff rendering; do not introduce a new diff component. |
| `decision.jsx` `PrMini` (context PR card) | `components/PrStatusPanel.tsx` + `components/PushPrPanel.tsx` (+ run fields `pr_url`/`pr_number`/`branch_name`) | Build the rail PR card as the single PR identity surface (§5.1). Reuse `PrStatusPanel` for the refresh action; do not add a push action. |
| `decision.jsx` `FailureCard` | — (only a thin red banner today in `RunDetailPage.tsx`) | **New read-only rail card** for Failed, from existing run fields + chunk/patch-failure data already on the read model. |
| `app.jsx` `ShipStepper` / `ShipCard` | `FinishStep`/`FinishStepBadge` (inline) + `PushPrPanel` + `PrStatusPanel` in `RunDetailPage.tsx` | Keep stepper as completion record; **stop reprinting PR identity** (§5.1). |
| `app.jsx` `RunBanner` (success/fail) | the `run.status === 'complete' / 'failed' / 'rejected'` banners in `RunDetailPage.tsx` | Trim success banner to one line (no second PR URL). Failed banner's content largely moves into the rail Failure card. |
| `timeline.jsx` `RunTimeline` (two-pane) | `components/RunTimeline.tsx` + `RunTimelineDetail.tsx` + `hooks/useRunTimeline.ts` | **No change** — shipped in Phase 2F. |
| `timeline.jsx` `Disclosure` (Chunk history) | the collapsed `<details>` "Chunk history" in `RunDetailPage.tsx` | No change. |
| `timeline.jsx` `MemorySuggestions` | `components/RunMemorySuggestions*`/`RunMemorySuggestionsDigest.tsx` | No change (generation is an existing action; black box). |
| `timeline.jsx` `DetailsAudit` / `MemoryTold` / `ProviderDiagnostics` | "Details & audit" `<details>` + `RunMemoryProvenancePanel.tsx` + `ProviderDiagnosticsPanel.tsx` + `EventLog.tsx` | No change. |
| `primitives.jsx` `Btn`/`Eyebrow`/`Mono`/`Pin` etc. | `components/ui/*` (button, badge, card, separator) + Tailwind | Use existing `ui/*`. Do not port `Pin` (demo). |
| `pw.css` tokens | `pw-tokens.css` / `index.css` / Tailwind theme | Map token intent; do not copy selectors. |
| `data.js` fixtures | live `run`, `chunkPlan.operator_state`, `gates`, `project`, timeline read model | The page is data-driven; fixtures are reference only. |

---

## 7. Safe frontend-only PR split

Each slice is frontend-only, independently shippable, behind no flag, and **gated
by parity**: existing run-detail tests/smokes must pass, and every slice must
prove "same actions reachable, same handlers wired, nothing auto-pushes/merges/
refreshes." Mutation/action components are not modified.

**PR-1 — Two-column cockpit shell (layout scaffold).**
Introduce the responsive `primary-col` + `context-rail` grid in
`RunDetailPage.tsx`. Move the cockpit (`OperatorAttentionPanel`) into the primary
column; the rail starts empty (or holds nothing new). Header, rail, safety
overview, timeline, finish & ship, disclosures stay exactly where they are
otherwise. Pure structure/CSS. Risk: low. Proof: visual + existing tests; rail
stacks below cockpit under the breakpoint.

**PR-2 — Context rail: trust facts + Running/Review states.**
Populate the rail for the Running and Needs-review archetypes: relocate the
read-only `trust_facts` rendering into the rail and add the "while you wait" /
"before you approve" framing. Source stays `operator_state.trust_facts`. No PR or
failure content yet. Risk: low (read-only relocation). Proof: trust facts render
identically; cockpit still shows blocked/out-of-app.

**PR-3 — Decision evidence in the primary column (review archetype).**
Surface the current-decision chunk's diff + test verdict + advisory review next
to the approval CTA. **Conservative approach:** do not fork rendering — either
(a) hoist the existing evidence block out of `ChunkPlanPanel` into a shared
read-only presentational component used in both places, or (b) if (a) is too
invasive, render a compact evidence summary in the primary column that *anchors/
scrolls* to the existing full evidence in `ChunkPlanPanel`. Black-box
`AdvisoryReviewPanel`. Multi-chunk: show only the pending gate's chunk (§5.3).
Risk: medium (touches where evidence renders) — keep it presentational, prove
the advisory review verdict/independence and the test verdict are byte-identical
to today.

**PR-4 — Done-state PR de-duplication + authoritative rail PR card.**
The headline cleanup (§5.1). Build the rail PR card as the single identity
surface; keep one refresh action; trim the Finish & ship completed step to stop
reprinting branch/PR#/URL; reduce the success banner to one line (no second URL).
`PushPrPanel`/`PrStatusPanel` stay black boxes — choose one as the single PR
surface. Risk: medium (composition of three PR surfaces) — prove: refresh still
works in exactly one place, push action still reachable in pre-Done states, no PR
URL printed twice, `local_only` path unchanged.

**PR-5 — Failed-state failure card + trimmed banner.**
Add the read-only rail Failure card (stage, summary, detail, working-tree-clean
reassurance) + failed trust facts, from existing run/chunk fields. Trim the
failed banner accordingly. Add the out-of-app "start a new run" hint if cleanly
derivable; otherwise omit. Risk: low–medium (new read-only card). Proof: nothing
implies a commit/push happened; matches `failed` and degrades safely for
`rejected`/`final_rejected`/`push_failed`.

**PR-6 — Mode register polish + visual token alignment (optional, last).**
Broaden Plain/Developer per §4 surface-by-surface under the additive-only
contract; align type scale/spacing/color roles to the prototype tokens. Risk:
low. Proof: no safety signal gated by mode; persisted preference intact.

Suggested order: **PR-1 → PR-2 → PR-4 → PR-5 → PR-3 → PR-6.** (PR-4 and PR-5 are
the highest user value and lower risk than PR-3; do the evidence relocation once
the columns are stable.)

---

## 8. Must-not-touch list (non-negotiable for Phase 2G)

Phase 2G is **frontend presentation/composition only.** It must not:

- **No backend behavior.** No changes to `backend/` — no pipeline, orchestrator,
  scope_guard, patch_applier, gates, or read-model computation. `operator_state`,
  `trust_facts`, `safety_checks`, `narrative`, and the timeline read model are
  consumed as-is.
- **No mutation handler changes.** `approve*`, `reject*`, `executeChunks`,
  `resumeChunks`, `retryChunk`, `steerChunk`, `pushPr`, `approveFinalApproval`,
  `generateRunMemorySuggestions`, and the resolver functions
  (`resolvePrimaryAction`, `resolveCoEqualAction`, `primaryActionHandlerKey`,
  `coEqualActionHandlerKey`) keep their current wiring, conditions, and
  invalidations. Action components are black boxes.
- **No approval / final-approval / Git / PR behavior change.** No new way to
  approve, reject, push, create, or merge. Nothing auto-pushes, auto-merges, or
  auto-refreshes checks. The "Pipewright never merges automatically" guarantee
  and all gate semantics are untouched. `local_only` manual-ship behavior is
  preserved.
- **No PR-5 / event persistence.** No new event table, no event writes, no
  fine-grained persistence; the timeline stays the existing read-only GET.
- **No memory retrieval changes.** No injection/retrieval/prompt-builder work;
  Memory Used / provenance stays display-only.
- **No FTS / Row 19.** No FTS scaffold, retriever, rebuild, or activation.
- **No Row 23.** No vector/embedding memory work.

When any slice appears to require touching one of the above, that is the signal to
**stop and re-scope** — Phase 2G has not been delivered correctly if a backend,
mutation, gate, Git/PR, persistence, or memory-retrieval surface changed.

---

## 9. Acceptance checklist (per slice and overall)

- [x] Same set of actions reachable as before; same handlers; same
      enabled/disabled conditions.
- [x] Running shows no action; Review shows one primary CTA (or co-equal for risk)
      + evidence; Done shows PR identity **once**; Failed shows a real failure
      surface.
- [x] PR URL is printed in exactly one place in Done; "Refresh PR checks" exists
      in exactly one place.
- [x] A FAIL/WEAK safety chip and any blocked action are visible in **both**
      Plain and Developer modes.
- [x] Developer mode only **adds** technical detail; it never makes a different or
      weaker claim than Plain.
- [x] Context rail hosts no action other than the single PR open/refresh; it is
      never the only home of a critical safety signal.
- [x] Two-pane timeline, chunk history, memory suggestions, and details & audit
      are unchanged.
- [x] `local_only` runs render manual-ship guidance, not an implied in-app push.
- [x] No `backend/` diff; no mutation/gate/Git/PR/persistence/memory-retrieval
      change.
