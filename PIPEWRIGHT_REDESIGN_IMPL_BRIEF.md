# Pipewright Redesign — Rolling Implementation Brief

**Date:** 2026-06-17
**Status:** **Phase 2G Run Detail Product UI is COMPLETE & MERGED (frontend
presentation/composition only).** The design spec is complete in
`docs/design/phase-2g-run-detail-product-ui.md`. PR-1 merged the two-column
cockpit/context shell; PR-2 merged Running and Needs-review context rail trust
facts; PR-4 merged Done-state PR de-duplication and the authoritative PR rail;
PR-5 merged the Failed-state failure rail; PR-3 merged decision evidence near the
approval cockpit; PR-6 merged visual/register polish plus Plain/Developer mode
cleanup. Final state: Run Detail is organized around the cockpit, safety overview,
context rail, decision evidence, timeline, and collapsed audit/details. **No
backend behavior changes, no mutation handler changes, no approval/final
approval/Git/PR behavior changes, no new actions, no event persistence / Phase 2F
PR-5 work, no memory retrieval changes, no FTS/Row 19 changes, and no Row 23
work.** Validation recorded: build/lint/diff checks passed per slice; PR-3
demo-smoke passed all 10 checks; PR-6 SSR smoke passed
running/final-approval/done/failed across Plain and Developer modes; protected
path checks confirmed frontend/docs-only scope per slice.

Previous status remains true: **Phase 2F Thread UI / Run Timeline is COMPLETE &
MERGED (read-only) through PR-0..PR-4 plus review fixes PR-A/PR-B/PR-C.** Design
brief + closeout: `docs/design/phase-2f-thread-ui.md` (§13). The Phase 2F
closeout section is below. PR-5 (fine-grained event persistence) remains the only
deferred Phase 2F slice.

Previous status remains true: **§23 row 7b plan-gate turns plus plan-version lineage
are COMPLETE & MERGED, end-to-end but default-off.** PR-A/PR-B/PR-C/PR-D landed the
internal engine, backend route, frontend "Revise plan" affordance, and
`PIPEWRIGHT_PLAN_TURNS_ENABLED` env wiring. Stabilization Slices A/B/C then
landed the backend `GET /runs/{run_id}/plan-versions` read model,
`approved_plan_version` approval binding, and frontend lineage display.
Backend/API/DB env-var smoke passed with process-local
`PIPEWRIGHT_PLAN_TURNS_ENABLED=true`; later manual smoke after Slice C confirmed
flag-off disabled behavior and flag-on revise/history/approval behavior. The
revised plan still requires explicit approval, lineage/stamping never blocks
approval, and no execution or final-approval bypass is introduced. Default remains
off.

Previous status remains true: **ROW 12 COMPLETE — PR-A, PR-B, and PR-C are all
merged.** D5 was affirmatively confirmed by the maintainer on 2026-06-14 (the D5
wording tension below is now **RESOLVED**), and Row 12 **PR-C** — relevance
*omission* + priority-based human-pinning + a single global off-switch — has been
**merged** behind one default-off policy flag
(`MEMORY_RELEVANCE_OMISSION_ENABLED=False`). The flag ships `False`, so default
behavior is byte-identical to PR-B; omission and priority pinning are **dormant**
until the flag is explicitly flipped later (an operational soak decision, not a
code change — and out of scope for this closeout). Stage 2 readiness is complete:
backend tests and a manual smoke doc now prove the future flag-on omission path
without changing the shipped default. This brief retains the PR-B closeout record
below (still accurate history), the PR-C summary, the Row 7b closeout note, and
the Row 19 closeout note. Row 19 and its FTS populate/soak follow-up are complete
and merged, but FTS retrieval remains default-off and dormant. Row 16 PR-C
activation and Row 23 remain deferred; the §21 thread UI shipped as Phase 2F's
read-only Run Timeline (2026-06-17), with only PR-5 event persistence deferred.
**Next step: a maintainer / Claude roadmap review before opening any new row or
activating a default.** Full records live in `PIPEWRIGHT_REDESIGN_WORKPLAN.md`
and the proposal's §24 + Appendix E.1/E.2.

## Phase 2G Run Detail Product UI closeout (frontend presentation, 2026-06-17)

COMPLETE & MERGED through PR-1, PR-2, PR-4, PR-5, PR-3, and PR-6. Canonical
design spec + closeout: `docs/design/phase-2g-run-detail-product-ui.md`.

- **PR-1:** two-column cockpit/context shell.
- **PR-2:** Running and Needs-review context rail trust facts.
- **PR-4:** Done-state PR de-duplication and authoritative PR rail.
- **PR-5:** Failed-state failure rail and trimmed failed banner.
- **PR-3:** decision evidence near the approval cockpit.
- **PR-6:** visual/register polish and Plain/Developer mode cleanup.

**Final state:** Run Detail is now organized around the cockpit, safety overview,
context rail, decision evidence, timeline, and collapsed audit/details.

**Invariants held:** no backend behavior changes; no mutation handler changes; no
approval/final approval/Git/PR behavior changes; no new actions; no event
persistence / Phase 2F PR-5 work; no memory retrieval changes; no FTS/Row 19
changes; no Row 23 work.

**Validation recorded:** build/lint/diff checks passed per slice; PR-3 demo-smoke
passed all 10 checks; PR-6 SSR smoke passed running/final-approval/done/failed
across Plain and Developer modes; protected-path checks confirmed frontend/docs-only
scope per slice.

## Phase 2F Thread UI / Run Timeline closeout (read-only, 2026-06-17)

COMPLETE & MERGED through PR-0..PR-4 plus review fixes PR-A/PR-B/PR-C. Canonical
design brief + per-PR delta notes: `docs/design/phase-2f-thread-ui.md` (§13).

- **PR-0:** backend read-only `GET /runs/{run_id}/timeline` deriver
  (`backend/pipeline/run_timeline.py`) over existing persisted tables; ordered
  `TimelineEntry[]` with stable, idempotent ids and sanitized `data`. No schema
  change, no write.
- **PR-1:** frontend `useRunTimeline` hook + additive read-only `RunTimeline`
  component, merged with the live `useRunEvents` tail by stable id. `EventLog` /
  `useRunEvents` untouched.
- **PR-2:** read-only `RunTimelineDetail` master-detail panel (plain-English summary
  + "what it unlocks / why it's safe" + expandable technical block).
- **PR-3:** timeline promoted to the primary Run Detail layout; the existing
  `OperatorAttentionPanel` made sticky/prominent as the next-action banner. Real
  controls moved/reframed, not rewritten — same wired mutations, no newly-reachable
  gate.
- **PR-4:** Plain English / Developer view toggle (presentation-only, plain default,
  `localStorage`-persisted; same data/fetches/controls in both modes).
- **PR-A:** backend timeline correctness/redaction test fixes.
- **PR-B:** persisted/live dedupe (by stable id) + timeline refresh fixes.
- **PR-C:** redaction polish, sticky height, `localStorage` guard, a11y.

**Invariants held:** no PR-5 / event persistence started; no schema or event table;
no backend writes; no POST lifecycle handler changes; no approval/final-approval/
Git/PR behavior change; no memory-retrieval change; no FTS/Row 19 activation; no
Row 23/vector work. The only backend surface added is the single read-only GET.

**Deferred:** PR-5 (fine-grained event persistence) is the only unshipped slice and
needs its own one-page brief before any prompt.

## §23 row 7b closeout (plan-gate turns + lineage, default-off)

- **PR-A:** internal plan-turn engine scaffold.
- **PR-B:** backend `POST /runs/{run_id}/plan-turns`, hidden as 404 while
  `PLAN_TURNS_ENABLED` is false.
- **PR-C:** collapsed Run Detail "Revise plan" affordance at the chunk-plan
  approval gate.
- **PR-D:** `PIPEWRIGHT_PLAN_TURNS_ENABLED` env wiring; unset/false-ish/invalid
  keeps the feature disabled, truthy enables it locally.
- **Slice A:** backend `GET /runs/{run_id}/plan-versions` read-only lineage/audit
  endpoint.
- **Slice B:** nullable `approved_plan_version` binding on chunk-plan approval,
  surfaced as top-level `approved_version` in the lineage endpoint.
- **Slice C:** frontend Run Detail plan-version lineage display.
- **Smoke:** backend/API/DB env-var activation passed on project `proj-4d529cfb`,
  run `dcacba8c-a993-44b6-bc50-e3ba0c57bea1`: `POST /runs/{run_id}/plan-turns`
  returned 200 (`plan_version=2`, `total_chunks=3`,
  `chunk_plan_status=awaiting_approval`), DB persisted v2
  `plan_versions.source='plan_turn'`, live `pipeline_runs.chunk_plan` changed from
  2 to 3 chunks, and pending chunk rows were replaced.
- **Approval invariant:** the revised plan still required explicit approval; the
  existing approve route approved it, chunks stayed pending, and no execution route
  was called / auto-triggered.
- **Flag-off invariant:** restart without the env var returned 404 for a valid
  plan-turn request; frontend source maps that 404 to "Plan revision is not
  enabled for this run."
- **Post-Slice-C smoke:** manual smoke confirmed flag-off disabled behavior and
  flag-on revise/history/approval behavior. Revisions update lineage, approval
  stamps/displays the approved version, and the revised plan still requires
  explicit approval.
- **Safety invariants:** no auto-execution, no approval blocker from lineage or
  stamping, and no final-approval bypass.
- **Known smoke gap:** direct browser visual smoke remains pending because local
  Windows automation failed (`CreateProcessAsUserW failed: 5`) and the Vite dev
  process exited under automated launch. Production frontend build passed. No
  newer uncompleted browser/manual limitation was recorded in this closeout; the
  earlier automation issue is not a known product bug.
- **Default:** remains off.

## §23 Row 19 closeout (MemoryRetriever/FTS, default-off)

Row 19 is complete across PR-A, PR-B, and PR-C.

- **PR-A:** inert SQLite FTS scaffold: guarded FTS5 table shape, derived/rebuildable
  index infrastructure, explicit rebuild lifecycle, and no runtime reader.
- **PR-B:** `MemoryRetriever` seam: deterministic rung-0 candidate loading moved
  behind the retriever interface with byte-identical prompt/memory behavior.
- **PR-C:** FTS rung-1 retrieval behind
  `PIPEWRIGHT_MEMORY_FTS_RETRIEVAL_ENABLED`; the flag defaults off.
- **Flag-off invariant:** byte-identical to deterministic rung-0/current behavior.
- **Flag-on invariant:** FTS is advisory ordering signal only over the canonical
  rung-0 candidate set; it never adds, drops, caps, or cross-projects candidates.
- **Safety invariant:** rung-0 remains the canonical safety spine, and
  mandatory/safety facts are never scored, demoted, omitted, or dropped.
- **Lifecycle:** explicit rebuild/population is required for flag-on to have
  effect. There is no rebuild-on-write and no lazy rebuild-on-read.
- **Non-goals held:** no endpoint, no frontend, no `schema.sql` FTS DDL, no
  approval/execution/final-approval/Git/PR behavior change, and no Row 23 vector
  memory started.

## Row 19 FTS populate/soak follow-up

Complete and merged. PR-1 added the explicit guarded manual FTS rebuild CLI. PR-2
added the read-only compare/seed soak harness and
`docs/design/row-19-fts-soak-results.md`.

- **Seeded soak:** passed with identical included set, identical mandatory tier,
  only relevance-tier order changed, no cross-project facts, and deterministic
  output.
- **Real-project soak:** safely fell back with zero ordering delta.
- **State after soak:** FTS retrieval remains default-off/dormant.
- **Non-goals held:** no activation trigger, no rebuild-on-write, no lazy
  rebuild-on-read, no default-on flip, no endpoint/frontend/`schema.sql`/boot-
  migration populate, and no Row 23 vector/embedding work.
- **Deferred:** the later approval-write-path rebuild trigger remains deferred and
  should not be started unless future soak shows real value.

## Row 12 PR-C summary (request-aware omission + pinning, dormant-by-default)

D5 confirmed defaults, single-sourced in `backend/pipeline/policy.py`:
`MEMORY_RELEVANCE_OMISSION_ENABLED = False` (one global switch gating BOTH omission
and pinning), `MEMORY_SMALL_STORE_GRACE_THRESHOLD = 12`, `MEMORY_PIN_PRIORITY_THRESHOLD = 10`.

- **Flag off (shipped default) ⇒ PR-B byte-for-byte.** No omission, no pinning;
  the relevance tier is ordered exactly as PR-B and exclusions keep their existing
  reasons (`budget_dropped` / `category_not_allowed_for_role`).
- **Pinning (flag on).** A fact with `priority ≤ 10` joins the mandatory tier
  (never scored, omitted, or budget-dropped). Request-context-independent, so it
  applies to every caller; the mandatory-overflow error message generalizes to
  "mandatory tier (safety + pinned)". Rides the existing `priority` field — no
  schema, no Pin button.
- **Omission (flag on).** A zero-signal relevance fact (zero path-overlap **and**
  zero token-overlap) is excluded with `not_relevant_to_request` — but only when
  the request carries signal, the relevance-candidate count exceeds the grace
  threshold, and at least one relevance fact carries signal. If no relevance fact
  carries signal, nothing is omitted (all-zero degeneracy guard preserved). A
  kept fact may still be `budget_dropped`. Omission is coder-only in practice.
- **Files:** `backend/pipeline/policy.py` (3 constants), `backend/memory/prompt_builder.py`
  (pinning into the mandatory tier + omission in the relevance tier, both flag-gated),
  `backend/tests/test_memory_selection_scaffolding.py` (coverage). `coder.py` needed
  no change — omission rides the `request_context` it already passes.
- **Rollback:** set `MEMORY_RELEVANCE_OMISSION_ENABLED = False` (the shipped value);
  selection-time only, nothing persisted to unwind.

---

## Maintainer decision (2026-06-14)

Row 12 opened **one sub-slice at a time, safest first.** PR-A (decision-free
scaffolding) landed first, was reviewed, and closed. **PR-B (relevance ordering
only) was reviewed and merged.** **PR-C (relevance omission + priority-based
human-pinning + global off-switch) was reviewed and merged after D5 was
affirmatively confirmed** — shipped dormant (`MEMORY_RELEVANCE_OMISSION_ENABLED=False`).
Row 12 is now complete.

## Row 12 sub-slice split

- **PR-A — scaffolding, no-op by default — COMPLETE.** Decision-free. Introduced
  the structure (dormant `request_context`, mandatory safety tier, policy
  single-sourcing of budgets / token estimator / adaptive guardrail) without
  changing normal memory injection behavior.
- **PR-B — relevance *ordering* only — COMPLETE.** When a `request_context` is
  supplied, it orders the non-mandatory relevance tier by a deterministic rung-0
  signal (lexical overlap on chunk title / description / `files_expected` /
  steer). Same set considered (no omission); `request_context` is plumbed from the
  **coder** only. Details below.
- **PR-C — relevance *omission* + priority-based human-pinning + global
  off-switch — COMPLETE (merged, dormant-by-default).** The slice that acts on D5,
  behind one default-off flag (`MEMORY_RELEVANCE_OMISSION_ENABLED=False`): when the
  flag is on it emits `not_relevant_to_request` for zero-signal relevance facts
  above the small-store grace threshold (12, over relevance-tier facts) and routes
  priority-pinned facts (`priority ≤ 10`) into the mandatory tier. The off-switch
  is the global flag (no per-project setting). See the PR-C summary above.

## Row 12 PR-B closeout (request-aware relevance ordering)

PR-B turned the dormant PR-A scaffolding into observable, auditable request-aware
**ordering** — with no omission and no new decision (D5 governs PR-C, not PR-B).

1. **Coder-only `request_context` plumbing.** `run_coder`
   (`backend/pipeline/coder.py`) builds a `RequestContext` from data it already
   holds — `plan.goal` (title), `plan.feature_description` (description),
   `files_to_modify + files_to_create` (`files_expected`), and
   `continuation_context` (steer text) — and passes it to
   `build_project_memory_block_detailed`. The planner, triage, and prompt-preview
   call sites are **untouched** (they still pass no `request_context`, so their
   behavior is unchanged). The `files_expected` carried here is advisory scoring
   input only: it is a fresh tuple, never mutates the plan, and **never reaches
   `scope_guard` or the write scope**.
2. **Relevance ordering of the non-mandatory tier only.**
   `backend/memory/prompt_builder.py` scores each relevance-tier fact by a
   two-tier deterministic key — path-token overlap (primary) then content-token
   Jaccard (secondary) — and orders by it, falling back to the existing
   `(category, scope, priority, created_at)` key as the tie-break. The scorer
   **reuses the existing `memory_trust` helpers** (`_content_tokens`, `_jaccard`);
   no second tokenizer was introduced, and no new policy constant / threshold was
   added.
3. **`request_context=None` ⇒ byte-identical block (golden-locked).** With a
   `None` context the relevance tier is not scored and the block is produced by the
   exact legacy path.
4. **All-zero overlap ⇒ legacy order preserved.** When a `request_context` is
   supplied but no fact carries any path or token signal, the order is identical to
   today's — the no-signal degeneracy guard.
5. **Mandatory safety tier untouched.** `security` / `forbidden_paths` facts remain
   first and are **never scored, never reordered, and never budget-dropped**; only
   the non-mandatory relevance rows are reordered.
6. **No omission.** PR-B only reorders. Under a binding budget, ordering changes
   *which* relevance facts fit, but every exclusion keeps an existing reason
   (`budget_dropped` / `category_not_allowed_for_role`). `not_relevant_to_request`
   stays **defined-but-dormant and is never emitted.**

## Row 12 PR-B non-goals (explicit — all held)

- No relevance omission; no `not_relevant_to_request` emission.
- No D5 activation; no human-pinning; no per-project off-switch.
- No grace threshold, relevance floor, or any new policy constant / threshold.
- No adaptive-budget activation (the flag stays off).
- No `MemoryRetriever` interface / FTS / vector / embedding work (rows 19 / 23).
- No planner / triage / prompt-preview plumbing.
- No schema / frontend / gate / scope / Git / PR / model-selection change.
- No memory mutation: no auto-approval, no active-memory creation, no
  stale/archive, no `last_verified_at` bump.
- Not rows 16 / 19 / 23; not the thread UI.

## Row 12 PR-B safety invariants (preserved)

- **`request_context=None` ⇒ byte-identical block.** Golden-locked parity.
- **Never evict or reorder a safety fact.** The `security` / `forbidden_paths`
  tier is never scored, reordered, or dropped — always injected first.
- **Ordering only, never omission while budget remains.** Exclusions keep existing
  reasons; `not_relevant_to_request` is never emitted.
- **Memory stays advisory.** Ordering changes ranking only — never scope, gates,
  Git/PR, or authority (B5). The scoring `files_expected` never reaches
  `scope_guard`.
- **Read-only + project-scoped.** Selection performs no writes; the query is
  unchanged (`status='active' AND is_stale=0`, project-filtered); never
  cross-project.
- **No buried magic numbers / one source of truth.** Reuses the `memory_trust`
  tokenizer; introduces no policy threshold.

## Files changed by PR-B

- `backend/memory/prompt_builder.py` — rung-0 relevance scorer + non-mandatory
  tier ordering; extracted the shared sort key; reuses the `memory_trust` helpers.
- `backend/pipeline/coder.py` — builds and passes the coder `RequestContext`.
- `backend/tests/test_memory_selection_scaffolding.py`,
  `backend/tests/test_coder.py` — ordering / no-omission / mandatory-tier /
  determinism / coder-plumb coverage.
- **Untouched:** `backend/pipeline/policy.py`, schema, gates, `scope_guard`,
  Git/PR, routes (incl. prompt-preview), frontend, planner/triage call sites,
  `bootstrap.py`, `detection_rules.py`.

## Tests / validation recorded for PR-B

- **Parity/golden:** `request_context=None` byte-identical (legacy golden kept).
- **No-signal:** all-zero overlap ⇒ legacy order (block byte-identical, equal
  included/excluded entries).
- **Ordering, no omission:** a populated context reorders the relevance tier with
  the **same set**; `excluded_entries == ()` when all facts fit.
- **Binding budget:** the high-relevance fact is kept and the zero-overlap fact is
  `budget_dropped` (never `not_relevant_to_request`).
- **Mandatory tier:** safety facts stay first even when a request favors a
  non-mandatory fact.
- **Determinism:** identical inputs ⇒ identical order; ties fall back to the
  legacy `(category, scope, priority, created_at)` key.
- **Coder plumb:** `run_coder` builds the `RequestContext` from the plan fields +
  steer and passes it; verified advisory (no `scopes` kwarg, no scope coupling).
- **Suite:** targeted suites green under `-m unit`. The two unmarked
  `test_coder.py` failures (`test_coder_returns_valid_handoff`,
  `test_coder_handles_missing_files_gracefully`) are **pre-existing live/integration
  tests** that need a configured target repo and fail identically without this
  change.

## D5 wording tension — RESOLVED (2026-06-14)

The soft contradiction in the planning docs (Appendix E.2 "accepted as written"
vs. §12/B1 "confirm pin mechanism" vs. §24's gating map "12 needs D5") is now
settled by an **affirmative maintainer confirmation**, recorded as the
single-source "D5 confirmed" note in **§24**:

- **Relevance-omission semantics + grace threshold:** confirmed — omit zero-signal
  relevance facts above `MEMORY_SMALL_STORE_GRACE_THRESHOLD` (default 12, counted
  over non-mandatory in-policy relevance facts), preserving the all-zero
  degeneracy guard.
- **Pin mechanism:** confirmed **priority-based** (pinned = `priority ≤
  MEMORY_PIN_PRIORITY_THRESHOLD`, default 10, no schema change) — resolving the
  §12/B1 "priority-based vs. explicit flag" sub-question in favour of priority.
- **Off-switch:** confirmed as the single global flag (no per-project setting).

PR-C ships dormant (`MEMORY_RELEVANCE_OMISSION_ENABLED=False`), so the
confirmation activates nothing by default.

## Deferred (do not start from this brief)

- **Activating `MEMORY_RELEVANCE_OMISSION_ENABLED`** in any environment — an
  operational soak decision, not a code change (it turns on omission + pinning).
  - When flipping it on, also update the prompt-preview 422 detail from "Memory
    safety tier exceeds the requested token budget" to "Memory mandatory tier
    exceeds the requested token budget" and update its API test (the route string
    is intentionally left unchanged in PR-C to preserve flag-off parity).
- **Row 16** — PR-A dormant default-off post-run hygiene trigger, PR-B
  read-only digest, and default-false env-gated soak complete; default-on PR-C
  activation remains later (D7/B2).
- **Row 23** — vector / embedding rung 2 (D6/B4).
- **§21 thread UI / Phase 2F** — the read-only Run Timeline shipped 2026-06-17
  (PR-0..PR-4 + review fixes PR-A/PR-B/PR-C; closeout above). Only **PR-5**
  (fine-grained event persistence) remains deferred and needs its own one-page brief.
  (Rows 22b–22e, D13.)

## Canonical pointers

- Current status snapshot: `docs/status/current-state.md`
- Sequence and decisions: `PIPEWRIGHT_REDESIGN_WORKPLAN.md`
- Source proposal: `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (§11.1, §23, §24,
  Appendix E.1/E.2)
