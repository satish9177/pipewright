# Pipewright Redesign — Rolling Implementation Brief

**Date:** 2026-06-14
**Status:** **D5 CONFIRMED — Row 12 PR-C implemented dormant-by-default.** D5 was
affirmatively confirmed by the maintainer on 2026-06-14 (the D5 wording tension
below is now **RESOLVED**), and Row 12 **PR-C** — relevance *omission* +
priority-based human-pinning + a single global off-switch — has been implemented
behind one default-off policy flag (`MEMORY_RELEVANCE_OMISSION_ENABLED=False`).
The flag ships `False`, so default behavior is byte-identical to PR-B; flipping it
on is an operational soak decision, not a code change. This brief retains the PR-B
closeout record below (still accurate history) and adds the PR-C summary. Rows 16
/ 19 / 23 and the §21 thread UI remain deferred. Full records live in
`PIPEWRIGHT_REDESIGN_WORKPLAN.md` and the proposal's §24 + Appendix E.1/E.2.

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
only) has now been reviewed and merged.** PR-C (relevance omission + human-pinning
+ per-project off-switch) remains deferred and **requires explicit D5
confirmation** before it is opened.

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
- **PR-C — relevance *omission* + human-pinning + per-project off-switch —
  DEFERRED, gated on D5.** The first slice that actually *acts* on D5: it would
  emit `not_relevant_to_request`, activate omission above the small-store grace
  threshold, and add human-pinned facts to the mandatory tier. Must not be
  implemented until D5 is explicitly confirmed.

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
- **Row 16** — post-run hygiene (auto-analysis + generation, D7/B2).
- **Row 19** — retriever interface + FTS rung 1.
- **Row 23** — vector / embedding rung 2 (D6/B4).
- **§21 thread UI** (rows 22b–22e, D13).

## Canonical pointers

- Current status snapshot: `docs/status/current-state.md`
- Sequence and decisions: `PIPEWRIGHT_REDESIGN_WORKPLAN.md`
- Source proposal: `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (§11.1, §23, §24,
  Appendix E.1/E.2)
