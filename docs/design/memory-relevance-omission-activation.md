# Memory Relevance Omission — Activation Design (Row 12 follow-up)

**Status:** Docs-only design. No code, schema, route, policy, prompt, UI, or test change.
The flag `MEMORY_RELEVANCE_OMISSION_ENABLED` stays `False`. **This document activates nothing.**
**Mode:** Adversarial / evidence-based; behavioral claims cite a `file:line` or function.
**Audience:** maintainer / Claude planning a future activation of request-aware memory omission.
**Author intent:** define the preconditions, observability bar, and staged plan to *safely* turn on
request-aware relevance omission + priority pinning later — so a future session does not flip the flag
without the user-facing visibility and tests that make omission trustworthy.

Related docs:

- Roadmap sequence + decisions: `../../PIPEWRIGHT_REDESIGN_WORKPLAN.md`, proposal §23/§24
  (`../../PIPEWRIGHT_REDESIGN_PROPOSAL.md`), Row 12 closeout in `../../PIPEWRIGHT_REDESIGN_IMPL_BRIEF.md`
- Injection observability as-built: [`memory-injection-discipline.md`](./memory-injection-discipline.md),
  [`memory-m3-trust-lifecycle.md`](./memory-m3-trust-lifecycle.md)
- Soak/rollback pattern this doc mirrors: [`trivial-task-profile-soak.md`](./trivial-task-profile-soak.md)
- Current status snapshot: `../status/current-state.md`

---

## 1. Purpose

This document defines the **preconditions and staged plan for safely activating
`MEMORY_RELEVANCE_OMISSION_ENABLED` in the future.** It records *what must be true* before the flag can
move and *in what order* the work lands, so activation is a deliberate, reviewed step rather than a
config flip someone makes in isolation.

**This PR does not activate the flag.** It changes no runtime behavior. The shipped default
(`backend/pipeline/policy.py:215` — `MEMORY_RELEVANCE_OMISSION_ENABLED = False`) is unchanged, and with
the flag off the system remains byte-identical to Row 12 PR-B (relevance *ordering* only, no omission, no
pinning). Activation is a separate, later, explicitly-approved change governed by §4–§6 below.

Why this matters: omission is the first selection behavior that can cause a human-approved fact to *not*
reach the model. The product invariant is **human control, auditability, and explicit approval**. A
fact silently dropped with no visible, plain-language reason is exactly the failure mode this design
exists to prevent. **System detects; human decides** — and the human can only decide if omission is
observable.

---

## 2. Current shipped state

As of Row 12 close (all three PRs merged; flag dormant):

- **Flag off.** `MEMORY_RELEVANCE_OMISSION_ENABLED = False` (`policy.py:215`). Companion knobs ship but
  are inert while the flag is off: `MEMORY_SMALL_STORE_GRACE_THRESHOLD = 12` (`policy.py:221`) and
  `MEMORY_PIN_PRIORITY_THRESHOLD = 10` (`policy.py:226`).
- **Relevance ordering is live — for the coder only.** `_partition_relevance_rows`
  (`prompt_builder.py:215`) reorders the non-mandatory relevance tier by request signal whenever a
  `RequestContext` is present; this is **not** flag-gated and the coder path passes one (`run_coder`
  builds `RequestContext` from the plan goal / feature description / files-to-modify+create / steer).
  See the §2.1 note — it is byte-safe.
- **Omission dormant.** With the flag off, `_partition_relevance_rows` returns `(rows, [])` — nothing is
  omitted. The exclusion reason `EXCLUSION_NOT_RELEVANT_TO_REQUEST = "not_relevant_to_request"`
  (`prompt_builder.py:41`) is **defined but never emitted**.
- **Pinning dormant.** `_is_mandatory_row` (`prompt_builder.py:157`) only routes a human-pinned fact
  (priority ≤ `MEMORY_PIN_PRIORITY_THRESHOLD`) into the mandatory tier **when the flag is on**. With the
  flag off, the mandatory tier is exactly the safety-only tier from PR-B.
- **`security` / `forbidden_paths` are mandatory and non-droppable — flag-independent.**
  `_is_mandatory_row` returns `True` for these categories regardless of the flag; the mandatory tier is
  rendered first and an overflow raises `MandatoryMemoryBudgetExceeded` (`prompt_builder.py:331`, raised
  at `:565`) rather than dropping a safety fact. They are never scored, reordered, omitted, or
  budget-dropped. This invariant holds today and **must hold after activation.**
- **All-zero degeneracy preserved.** If no relevance fact carries signal, nothing is omitted even with
  the flag on (`has_relevance_signal` guard, `prompt_builder.py:256`).
- **prompt-preview 422 wording still deferred.** The `MandatoryMemoryBudgetExceeded` → HTTP 422 mapping
  exists, but its user-facing wording is **not yet** updated to say the mandatory tier includes *pinned*
  facts (today it is safety-only). That copy change is intentionally deferred to the activation PR
  (Stage 3), because pinned facts cannot enter the mandatory tier until the flag is on.

### 2.1 Note: PR-B relevance ordering is already live and can be soaked

PR-B relevance *ordering* is in production on the coder path right now, independent of the flag. It is
**byte-safe**: it reorders the same set of relevance facts by request signal and **omits nothing** — the
included set is identical to pre-PR-B, only its order within the block changes. This is an asset for
activation readiness: the `RequestContext` plumbing and the path/token overlap scoring are already
exercised on every coder run, so the request-signal machinery omission depends on is not cold.

Before activating omission, the ordering behavior can be observed on real coder runs (the provenance
panel already records the injected order). No metric gate is defined for ordering — it is byte-safe — but
it is the de-facto soak of the scoring inputs omission will reuse.

---

## 3. Risk assessment

The dominant risks, in priority order:

1. **Hidden omission / silent context loss (top trust risk).** Omission removes a human-approved fact
   from the model prompt. Today the omitted fact *is* persisted with its reason
   (`injection_store.py:291` records `excluded_entries`) and rendered per-entry in the provenance panel
   (`RunMemoryProvenancePanel.tsx`), but `not_relevant_to_request` has **no curated label** in
   `REASON_LABELS` (`frontend/src/utils/memoryReasonHumanize.ts:1`) and **no prominent aggregate
   surface** the way budget drops do (`RunMemoryProvenancePanel.tsx:232`). Activating before §4's
   observability is built means omission is technically logged but practically hidden. This is the gate.
2. **Memory poisoning × relevance scoring.** Omission keys off deterministic path-overlap + token-Jaccard
   signal. A correct-but-poorly-worded fact can score zero and be dropped, while a confidently-worded
   wrong fact scores high and is kept. Scoring is a *relevance* heuristic, not a *truth* signal — it must
   never be read as one. The pin escape hatch (risk 4 below; precondition in §4) is the mitigation, which
   is why pinning and omission ship and activate together under one flag.
3. **Over-pinning → mandatory-tier overflow → 422.** `MEMORY_PIN_PRIORITY_THRESHOLD = 10` is a **priority
   threshold, not a count cap.** Nothing limits how many facts a project can pin (priority ≤ 10). With
   the flag on, enough pins push the mandatory tier over budget and raise
   `MandatoryMemoryBudgetExceeded` → prompt-preview 422. The loud fail is *correct* (a safety fact is
   never silently dropped for a pin), but activation can turn a previously-working project into a 422
   with no upstream warning. This needs a guard or a high-pin-count warning before activation (§4).
4. **Pin affordance is not discoverable.** Pinning rides the existing priority field; there is no "Pin"
   button and no in-context "this was left out — pin to force-include" path. If omission can hide a fact
   but the user has no obvious recourse, the escape hatch is not real. Discoverability is a trust gate,
   not a nicety.
5. **Grace threshold 12 is cautious but unproven.** Omission only engages above 12 non-mandatory
   relevance facts (`policy.py:221`). The value is a deliberate small-store guard, but it has not been
   validated against real store sizes; the first activation should confirm it behaves on the target
   project before widening.
6. **Small stores see no omission at all.** A corollary of (5): projects at/below 12 relevance facts get
   *zero* omission even with the flag on. That is safe, but it means activation is a no-op for small
   stores — useful for a cautious first flip, and a reason not to over-read an uneventful first soak.

---

## 4. Required preconditions before activation

All of the following must hold before `MEMORY_RELEVANCE_OMISSION_ENABLED` may move to `True` in any
environment. None are implemented by this PR.

**Observability (closes risk 1):**

- [ ] Curated `REASON_LABELS` entry for `not_relevant_to_request` in
      `frontend/src/utils/memoryReasonHumanize.ts` (no raw auto-prettified fallback string).
- [ ] Curated `summarizeExclusions` copy for `not_relevant_to_request`
      (`RunMemoryProvenancePanel.tsx:73`), distinct from the budget/category copy.
- [ ] Neutral aggregate UI surface: **"N memories left out as not relevant to this request."** It must be
      visually distinct from the budget-drop banner (`RunMemoryProvenancePanel.tsx:232`) and the safety
      banner (`:245`) — omission is **intended behavior, not a failure**, so it must not reuse the
      amber/red alarm styling.
- [ ] Per-entry UI label: **"Left out — not relevant to this request."**

**Pin escape hatch (closes risks 2 & 4):**

- [ ] Clear **"Pin to force-include"** affordance and help text, reachable from where a user sees an
      omitted fact (so the recourse is in-context, not buried in a priority field).
- [ ] High-pin-count **warning or guard** before the mandatory tier can overflow and surprise users with
      a 422 (closes risk 3). Decide guard vs. warning explicitly in Stage 1.

**Correctness / safety proof (re-proves the invariants under the flag on):**

- [ ] Explicit **flag-on** safety tests proving `security` / `forbidden_paths` are **never** omitted and
      **never** budget-dropped, even when they would score zero overlap.
- [ ] Coder-path **integration proof** with **more than 12** relevance-tier facts: omission actually
      engages.
- [ ] A **pinned fact survives** omission (joins the mandatory tier; never `not_relevant_to_request`).
- [ ] Omitted facts **persist** with `not_relevant_to_request` and **render** in the provenance panel.
- [ ] **Rollback proof:** flag back to `False` restores PR-B behavior with **no omission** (byte-identical
      block for the same inputs).

**Process / rollout:**

- [ ] prompt-preview 422 wording updated **only in the activation PR** to state the mandatory tier is
      *safety + pinned* facts (deferred until pinning is live — see §2).
- [ ] Local / single-project rollout scope documented first (do not flip globally on the first activation).

---

## 5. Proposed staged roadmap

Each stage is a separate, independently-reviewable change. No stage auto-starts the next.

**Stage 0 — this design doc only.** Docs-only. No code, no flag move. (This PR.)

**Stage 1 — omission observability slice** (first implementation slice; **no flag activation**). Branch
`feature/memory-omission-observability`. Builds the §4 observability items so omission is explainable
*before* it can ever happen:

- Curated `REASON_LABELS` + `summarizeExclusions` copy for `not_relevant_to_request`.
- Neutral aggregate surface ("N left out as not relevant to this request") + per-entry label.
- Pin affordance / "Pin to force-include" help text.
- Decide and implement the high-pin-count warning vs. hard guard.
- Flag stays `False` throughout. Because omission never fires with the flag off, this slice is read-only
  display work over an already-persisted field — no runtime selection behavior changes.

**Stage 2 — activation readiness tests / smoke** (still **no default activation**). Branch
`feature/memory-relevance-omission-readiness`. Lands the §6 unit + integration + UI/manual coverage,
exercised with the flag toggled *in tests only* (monkeypatched on), never as the shipped default.
Activation does not happen here.

**Stage 3 — controlled flag activation** (only after Stages 1–2 land and §4 is fully checked). Branch
`feature/memory-relevance-omission-activate`:

- **Local-only / single-project first**, per §4 rollout scope.
- Includes the prompt-preview 422 wording update (mandatory tier = safety + pinned).
- Rollback documented and proven: set `MEMORY_RELEVANCE_OMISSION_ENABLED = False` (selection-time only;
  nothing persisted to unwind), restart, behavior returns to PR-B — mirroring the single-flag rollback in
  [`trivial-task-profile-soak.md`](./trivial-task-profile-soak.md).

**Stage 4 — post-activation observation.** Watch, before widening rollout:

- **Omitted-then-pinned facts** — the proposal's **T10 guardrail** ("memory relevance omission audit",
  `PIPEWRIGHT_REDESIGN_PROPOSAL.md:705`): a fact omitted as not-relevant that a human later pins/asks for
  is the signal that omission cut too aggressively. Data is already joinable (`fact_id` +
  `not_relevant_to_request` persist per injection event).
- **prompt-preview 422 rates** — a rise signals over-pinning / mandatory-tier overflow (risk 3).
- **User trust / UX issues** — confusion about why a fact was left out, or omission perceived as silent.

---

## 6. Minimum smoke checklist for future activation

Required before Stage 3 flips the flag. (Restates §4's test items as an executable checklist.)

**Unit tests** (`backend/tests/test_memory_selection_scaffolding.py`, flag toggled per-test):

- [ ] flag-off parity (PR-B byte-identical)
- [ ] omission above grace
- [ ] below-grace no-op
- [ ] all-zero degeneracy (no signal ⇒ no omission)
- [ ] pin into mandatory tier
- [ ] pin overflow → clean `MandatoryMemoryBudgetExceeded` (loud fail)
- [ ] `security` / `forbidden_paths` never omitted or budget-dropped **under flag on**

**Integration** (coder path):

- [ ] `run_coder` path with **>12** relevance facts engages omission
- [ ] omitted fact **persisted** (`injection_store`) and **rendered** in provenance
- [ ] **pinned fact survives** (stays included)

**Manual UI** ("What Pipewright told the AI" — `RunMemoryProvenancePanel.tsx:456`):

- [ ] omitted facts shown clearly under "Left out in this run"
- [ ] omitted reason is **curated**, not the raw prettified string
- [ ] aggregate count is **visible and non-alarming** (neutral styling)
- [ ] pinning an omitted fact **returns it to included memory**

**Rollback:**

- [ ] flag `False` restores no-omission behavior (PR-B), verified on the same inputs

**Negative:**

- [ ] enough pinned facts trigger a **clean 422** with the future (safety + pinned) wording — not a
      partial or garbled block

---

## 7. Explicit non-goals

This document and the Stage 0 PR do **not**:

- Activate `MEMORY_RELEVANCE_OMISSION_ENABLED` (stays `False`).
- Modify any policy constant (`MEMORY_SMALL_STORE_GRACE_THRESHOLD`, `MEMORY_PIN_PRIORITY_THRESHOLD`, the
  flag).
- Start Rows 16 / 19 / 23 (post-run hygiene, retriever + FTS, vector/embedding).
- Build the thread UI (rows 22b–22e).
- Add semantic / vector / embedding memory.
- Change any runtime behavior (selection, ordering, omission, pinning, budgets).
- Change any backend or frontend code, schema, route, test, or package.
- Change the prompt-preview 422 wording (deferred to Stage 3).

---

## 8. Suggested future branch names

- `feature/memory-omission-observability` — Stage 1 (observability slice, flag stays off).
- `feature/memory-relevance-omission-readiness` — Stage 2 (tests / smoke, no default activation).
- `feature/memory-relevance-omission-activate` — Stage 3 (controlled activation + 422 wording update).

---

## Appendix: invariants that must survive activation

These hold today and are the non-negotiable acceptance bar for Stage 3. None may regress:

- `security` / `forbidden_paths` are mandatory, flag-independent, never omitted or budget-dropped
  (`_is_mandatory_row`, `prompt_builder.py:157`).
- All-zero degeneracy: no relevance signal ⇒ no omission (`prompt_builder.py:256`).
- Omission is **selection-time only and deterministic** — it is never an authority channel for scope,
  approval, Git, provider, or merge, and nothing it does is persisted as a memory mutation.
- Flag off ⇒ byte-identical to Row 12 PR-B; rollback is a single constant flip with nothing to unwind.
- Omission is **observable**: every omitted fact carries `not_relevant_to_request` in the persisted
  injection event and renders in the provenance panel. Visibility is a precondition of the behavior, not
  an afterthought.
