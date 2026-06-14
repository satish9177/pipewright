# Pipewright Redesign — Rolling Implementation Brief

**Date:** 2026-06-14
**Status:** **ACTIVE — Row 12 PR-A design opening (docs-only).** This is the
design-first brief for the next active memory slice. **No code has been written
and none may be written from this brief until a human reviews and approves it**
(the established design-first discipline). The previous occupant — the Row 11
closeout — is complete; its record lives in `PIPEWRIGHT_REDESIGN_WORKPLAN.md`.

---

## Maintainer decision (2026-06-14)

Row 11 (detection rules-as-data) is complete and closed. The recommended and
accepted next memory row is **§23 order-row 12 — request-aware selection +
mandatory tier + adaptive guardrail (D5/B1; Pass 2 §11.1)**. Row 12 is opened
**one sub-slice at a time**, safest first. **This brief opens Row 12 PR-A only.**

## Row 12 sub-slice split

- **PR-A — scaffolding, no-op by default — NEXT ACTIVE SLICE (this brief).**
  Decision-free. Introduces the structure and single-sources the policy numbers
  without changing what memory is injected.
- **PR-B — relevance *ordering* only — DEFERRED.** When a `request_context` is
  supplied, order the relevance tier by a deterministic rung-0 signal (lexical
  overlap on chunk title / description / `files_expected`). Same set injected
  (no omission). Plumbs `request_context` from the orchestrator. Not opened yet.
- **PR-C — relevance *omission* + human-pinning + per-project off-switch —
  DEFERRED, and gated on D5.** This is the first slice that actually *acts* on
  D5; it must not be implemented until D5 is explicitly confirmed (see the D5
  wording tension below). Not opened yet.

## Row 12 PR-A scope (no-op-by-default scaffolding)

PR-A makes the structure exist and pays down the buried-magic-number debt
(§14 / §8b) **without changing the injected memory block for current stores.**

1. **`request_context` as a dormant, default-`None` concept.** Introduce (or, at
   this docs stage, design) an optional `request_context` parameter on the memory
   block builder (`backend/memory/prompt_builder.py:build_project_memory_block_detailed`).
   **`request_context=None` must preserve today's injected block byte-for-byte**
   (golden-locked). PR-A does not plumb a non-`None` value from anywhere — the
   orchestrator call sites are untouched; threading a real `request_context` is
   PR-B.
2. **Mandatory safety tier scaffolding — `security` and `forbidden_paths` only.**
   These two categories become structurally un-droppable: they live outside the
   token-budget loop and can never appear as a `budget_dropped` exclusion. Human
   pinning is **not** part of PR-A (pinning is a D5 concern, deferred to PR-C).
   Loud-fail discipline: if the mandatory tier *alone* exceeds the cap, return a
   `NEEDS_HUMAN`-style outcome rather than silently shedding a guardrail.
3. **Policy single-sourcing for memory injection.** Relocate the buried numbers
   in `prompt_builder.py` — `ROLE_TOKEN_BUDGETS` and the `(len + 3) // 4` token
   estimator — into the policy spine (`backend/pipeline/policy.py`), and express
   the **adaptive guardrail** there: cap = clamp(policy floor, role share ×
   resolved model context window, policy ceiling), with the model resolved from
   `backend/llm/role_config.py` (one source of truth) and the estimator's safety
   margin stated in policy. PR-A must keep the *effective* cap for current roles
   equal to today's so the block is unchanged.
4. **Dormant `not_relevant_to_request` provenance reason.** The reason name may
   be **documented/defined** in the exclusion vocabulary alongside the existing
   `budget_dropped` and `category_not_allowed_for_role`, but **PR-A must never
   emit it** (no fact is excluded for relevance in PR-A). It activates in PR-C.

## Row 12 PR-A non-goals (explicit)

- No relevance omission.
- No request-aware **ordering** behavior change (that is PR-B).
- No orchestrator `request_context` plumbing (kept `None`; plumbing is PR-B).
- No human-pinning mechanism or UI (D5 / PR-C).
- No D5 behavior activation of any kind.
- No retriever / FTS / vector / embeddings (rows 19 / 23).
- No memory mutation, auto-approval, active memory creation, stale/archive, or
  `last_verified_at` bump.
- No schema change (any provenance additions are in-memory dataclass fields, not
  persisted columns).
- No frontend / UI / thread work.
- No gate / scope / Git / PR / model-selection behavior change.
- No reviewer memory wiring (M8 stays off, per §11.5).

## Row 12 PR-A safety invariants

- **Never evict a safety fact.** `security` + `forbidden_paths` are structurally
  outside the droppable set; never listed as `budget_dropped`.
- **`request_context=None` ⇒ byte-identical block.** Golden-locked parity.
- **Loud-fail, never silently shed** a guardrail to fit budget.
- **Memory stays advisory.** PR-A changes structure only — never grants scope,
  gates, or touches Git/PR/authority (B5).
- **Read-only + project-scoped.** Selection performs no writes; the query stays
  `project_id`-filtered, `status='active' AND is_stale=0`; never cross-project.
- **Provenance stays complete and observable.** Existing exclusion reasons are
  preserved; the new reason is defined-but-dormant.
- **No buried magic numbers.** The guardrail/threshold/margin live in policy,
  single-sourced — do not trade one buried constant for three.

## Likely files (when PR-A is implemented, after review of this brief)

- `backend/memory/prompt_builder.py` — mandatory safety tier, adaptive guardrail,
  optional `request_context` (default `None`), dormant `not_relevant_to_request`.
- `backend/pipeline/policy.py` — single-sourced budgets / estimator / guardrail.
- `backend/llm/role_config.py` — **read-only** model → context-window resolution.
- `backend/tests/test_prompt_builder*.py` (+ a new parity/golden test).
- Docs: this brief; a smoke doc; the reconciled Appendix E.1/E.2.
- **Untouched:** orchestrator call sites, schema, routes, gates, `scope_guard`,
  Git/PR, `bootstrap.py`, `detection_rules.py`, `suggestion_quality.py`.

## Suggested targeted tests + manual smoke (for the PR-A implementation step)

- **Parity/golden:** block byte-identical across representative stores with
  `request_context=None`.
- **Mandatory tier:** under an artificially tiny budget, `security` /
  `forbidden_paths` facts are always included, never in `excluded_entries`.
- **Loud-fail:** mandatory tier alone > cap ⇒ `NEEDS_HUMAN` outcome.
- **Adaptive guardrail:** cap = clamp(floor, role share × model window, ceiling),
  sourced from policy / `role_config`; assert no duplicated constant.
- **Dormancy:** `not_relevant_to_request` is defined but never emitted in PR-A.
- **Regression:** existing `injection_analysis` / provenance tests stay green.
- **Manual smoke:** run triage/planner/coder on a real project; diff injected
  block bytes vs. pre-change (expect identical); confirm provenance still renders.

## D5 wording tension (resolve before PR-C, not before PR-A)

There is a soft contradiction in the planning docs that must be settled before
**PR-C** (the omission/pinning slice), but **not** before PR-A (which never acts
on D5):

- **Appendix E.2** records §24's recommended defaults as "accepted as written,"
  including **D5**.
- **§12 / B1** still flags an open sub-question — "confirm pin mechanism
  (priority-based vs. explicit flag)" — and **§24's gating map** still lists
  "12 needs D5."
- **§11.1** recommends the pin mechanism as priority-based (pinned = `priority ≤
  pin_threshold`, policy default 10, no schema change).

Row 12-PR-C is the first slice that actually exercises D5, so the maintainer
should **affirmatively confirm** (a) relevance-omission semantics + grace
threshold (default 12) and (b) the pin mechanism — rather than relying on the
blanket "accepted as written" — before PR-C is implemented.

## Deferred (do not start from this brief)

- **Row 12 PR-B** (relevance ordering) and **PR-C** (omission / pinning / D5).
- **Row 16** — post-run hygiene (auto-analysis + generation, D7/B2).
- **Row 19** — retriever interface + FTS rung 1.
- **Row 23** — vector / embedding rung 2 (D6/B4).
- **§21 thread UI** (rows 22b–22e, D13).

## Canonical pointers

- Current status snapshot: `docs/status/current-state.md`
- Sequence and decisions: `PIPEWRIGHT_REDESIGN_WORKPLAN.md`
- Source proposal: `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (§11.1, §23, §24,
  Appendix E.1/E.2)
