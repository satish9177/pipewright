# Pipewright Redesign — Implementation Handoff Brief (rolling, per-phase)

**Date:** 2026-06-12
**What this file is:** the **rolling** implementation handoff brief for the redesign. Each phase's active spec lives here; when a slice lands, this file is repurposed for the next one. **Current contents: Phase 4, slice 2 (item 16 — phase/narrative read-model extending `operator_state`).** The previous occupant (item 15 — reviewer informed-approval soft gate + "steer this") landed via **PR #287** (`4a362c4`, merge `ee291c7`) on `develop` and is reviewed + accepted — do **not** re-review it. Its outcome is recorded in `PIPEWRIGHT_REDESIGN_WORKPLAN.md`.
**For:** the model implementing Phase 4 item 16. Re-verify every `file:line` against the live code before you cite or edit it; items 13–15 moved line numbers and the repo keeps moving.
**Source of record:** `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (§4.8 state & narrative model, §6 phasing item 16, §5.6 the display-only non-tension) and `docs/design/operator-state-attention-panel.md` (the existing `operator_state` contract this slice extends). This brief operationalizes them; if they disagree, the proposal wins and you flag the drift.
**Mode:** **Fable designs, implements, and tests this slice** — one item per PR. This is a **design brief, not a prescribed mechanism:** it fixes the *what* (scope, the headline invariant, decisions B1–B4, the safety-contract check, the acceptance tests) and points at the relevant code so you don't re-discover the repo — but **you own the design (the *how*): the exact phase-projection function, where the narrative fields attach, and how the legacy copy is preserved.** The acceptance tests are external criteria, not yours to relax. Since you also write the tests, **your design is reviewed by a human before you implement, and the PR is reviewed.** This slice is **item 16 only.** Do not build the trivial-task profile + prompt caching (item 17, the last Phase 4 slice), and do not build any chat/thread history feed.

---

## 0. The headline invariant (read before anything else)

Item 16 is the **lowest-risk slice in the redesign**: it is purely a **display-only derivation**. `operator_state` is already a read-only, never-persisted, never-authoritative model (`docs/design/operator-state-attention-panel.md` "Final Safety Invariants"; `operator_state.py:1-10` "performs no I/O … Routes remain the source of truth"). Item 16 adds two derived fields to it. The single non-negotiable invariant:

> The new `phase` and `narrative` are **pure derivations of the already-computed `OperatorState`**. They add **zero new authority**: no route reads them to make a decision, nothing is persisted, no DB migration occurs, and no existing eligibility/gate/commit path changes. A run's *behavior* must be **byte-identical** with and without this slice — only the read response grows two additive fields.

Three structural guarantees that make that true, and that your design must prove:

- **No new authority channel.** The narrative is "display-only derivation; like memory, never an authority channel for scope/approval/Git decisions" (proposal §5.6). `whats_next` **renders the actions `operator_state` already computed** — it must never advertise an action that isn't in `primary_action`/`neutral_actions`/`secondary_actions`, and never present a *blocked* action as available. It cannot become a nudge that contradicts the safety ledger (design-doc rules 10–11: risk decisions stay neutral; no generic "Continue").
- **Fail-closed stays fail-closed.** The wiring (`_augment_plan_with_operator_state`, `routes/chunks.py:2207`) already degrades any adaptation failure to `compute_operator_state(OperatorStateContext(unknown=True))`. The unknown/fallback state MUST project to a **safe phase** (`Needs attention`, never `Done`/`Working`) and a safe narrative (the existing unknown-state copy), so a degraded read never mislabels a stuck run as fine.
- **No correctness claims.** The narrative must never imply the generated code is correct (design-doc safety-ledger rule 12). "Strong tests" means the process gate passed, not that the code is right — the narrative's `why`/`whats_next` inherits that honesty from the existing copy.

This is display polish over a safety-critical read-model. The risk is **not** a scope/commit/Git regression (those paths are untouched). The only real risks are (a) accidentally letting the narrative assert an action or correctness the backend did not authorize, and (b) perturbing existing `operator_state` bytes that safety-critical copy/tests depend on.

### Decisions already fixed by the proposal (do not re-litigate — §4.8)

- **B1 — the six phases, exact names:** `Planning`, `Waiting for you`, `Working`, `Needs attention`, `Done`, `Stopped`. Every run/chunk read projects to exactly one. These are user-facing labels; do **not** rename the internal DB statuses or the `OutcomeClass`/`PatchFailureType`/verdict enums (proposal §4.8 "keep the current strings — renaming them buys nothing").
- **B2 — narrative is three structured parts:** *what happened* (outcome + short evidence summary), *why* (the classifier reason), *what's next* (the legal actions for that state — retry budget remaining, steer, approve scope expansion, fix env, etc.). Generated **from templates over the outcome taxonomy**, deterministically.
- **B3 — deterministic, not LLM, this slice.** "An LLM may **later** polish phrasing as display-only text, never as the source of state" (§4.8). This slice ships templates only; no LLM call is added. Phrasing polish is explicitly a *later* concern.
- **B4 — extend, don't fork.** This "extends the existing read-model direction (`operator_state.py`) rather than inventing a parallel one" (§4.8). Raw enums/fields remain in the API for compatibility (`title`, `explanation`, `waiting_on`, `decision_type`, actions, `safety_checks`); the UI gains `phase` + `narrative` on top. No second read-model, no new endpoint.

### Decisions that are YOURS to design (propose in your design doc; the human ratifies them in design review)

These are the *how*, and they are the substance of the design review. Bring a recommendation for each:

- **D1 — the phase-projection mapping**, especially the ambiguous boundaries. The coarse signals (`waiting_on`, `decision_type`, `is_terminal`, `terminal_status`) get you most of the way, but several states are judgment calls. Name your ruling on at least these:
  - Is a **retryable patch failure** `Waiting for you` (a clear retry action exists) or `Needs attention` (something failed)? (Recommend: `Needs attention` — *what happened* is a failure, even though a progress action exists.)
  - Are **risk-decision gates** (scope-expansion pending, memory conflict, weak-test ack, reviewer-finding ack) `Waiting for you` (expected gates) or `Needs attention` (exceptions)? (Recommend: expected gates → `Waiting for you`; only *failed/rejected* states → `Needs attention`.)
  - Is **stalled** `Working` or `Needs attention`? (Recommend: `Needs attention` — the doc says investigate before proceeding.)
  - **Planning vs Working** for system-busy states: split on lifecycle (Planning = pre-execution plan formation, e.g. `RunStatus.RUNNING` before chunk execution; Working = `RUNNING_CHUNKS`/executing). Confirm the run-status signal that distinguishes them is actually available at the read.
- **D2 — where the narrative copy comes from, and whether the legacy `explanation` is recomposed.** Two viable shapes:
  - **(D2-additive, recommended default — lowest risk):** keep `title` and `explanation` byte-identical; ADD a `narrative {what_happened, why, whats_next}` derived alongside. For failure states, source `what_happened`/`why` from the existing `_PatchFailureFamily` (which **already** carries the "What happened / Why / What next" split — `operator_state.py:708-739`); for the ~15 non-failure states, author concise `what_happened`/`why` (mostly already present as `title`/`explanation`), and derive `whats_next` from the computed action set everywhere. Pro: no existing copy churn, minimal test perturbation. Con: `explanation` and the triplet are two copies that could drift.
  - **(D2-recompose):** make the triplet the single source and compose `explanation` from it. Pro: one source of truth. Con: changes `explanation` bytes → touches the safety-critical copy the design doc says the backend owns, and re-baselines the existing operator_state tests. If you choose this, justify it and re-baseline explicitly.
  - Either way: **`whats_next` is derived from `primary_action`/`neutral_actions`/`secondary_actions` (+ blocked actions as "not yet, because …")** — one source of truth for actions, never re-authored.
- **D3 — where the projection lives.** A single pure `project_phase(state, context) -> RunPhase` table-driven function in `operator_state.py` (one testable place, with explicit per-state overrides for the D1 boundary cases) vs. attaching `phase` inside each of the ~20 `_state(...)` builders. (Recommend: the single pure function — fewer edits, one source of precedence, mirrors how `compute_operator_state` already centralizes precedence.)

## 1. Non-negotiable safety contract (from `CLAUDE.md`)

No change may weaken these. Item 16 framing in **bold**:

1. No implementation without an approved chunk plan; never bypass a gate. **Untouched — item 16 reads computed state; it adds no gate, no approval, no execution path.**
2. Never edit outside approved `files_expected`; `scope_guard` is the authority. **Untouched — no code-execution/scope path is involved.**
3. Never create empty / no-effective-change commits. (Untouched — no commit path.)
4. Never open PRs against `main`/`master`/`develop`; never auto-merge. (Untouched.)
5. Never write forbidden paths. (Untouched.)
6. Never expose or persist secrets/tokens/PII; sanitize errors. **The narrative is templated from outcome class + already-sanitized state copy. It must NOT introduce raw provider/Git errors, diffs, stack traces, test output blobs, or file contents into the read response — evidence summaries stay short and human-readable, the same discipline as `StageOutcome.evidence` (`stage_contract.py:163-168`) and the capped `technical_details` that stays off the panel.**
7. Memory/reviewer findings/narratives are **advisory**; source code, user instruction, tests, and safety rules win on conflict. **The phase/narrative is the canonical example: a display derivation with zero authority (proposal §5.6).**
8. AI-suggested memory stays pending until a human approves. (Untouched.)
9. Prefer failing safely with a clear, specific error over guessing. **The safe failure is the existing fail-closed `unknown` state → it must project to `Needs attention` + the unknown-state narrative, never to `Done`/`Working`.**

This PR is **low-risk** (display-only), but it sits on top of the safety-critical read-model, so carry the explicit safety-contract check (§16.5). The risk is misrepresentation, not mutation.

## 2. The ground you're designing on (grounding, not a prescribed mechanism)

Where the pieces live, so you design against real code. **How you use them is your call** — the only fixed points are §0, B1–B4, §16.5, and the §16.3 tests.

- **`backend/pipeline/operator_state.py` — the model you extend.** `OperatorState` (`:103`) already has `title`, `explanation`, `waiting_on` (`OperatorWaitingOn`: human/system/nobody, `:27`), `decision_type` (`OperatorDecisionType`: progress/risk_decision/none, `:33`), `primary_action`/`neutral_actions`/`secondary_actions`/`blocked_actions`, `safety_checks`, `trust_facts`, `out_of_app_instruction`, `is_terminal`, `unknown_state_warning`, `schema_version` (`:20`). `compute_operator_state` (`:192`) is the **single precedence authority** — ~20 `_state(...)` builders, pure, no I/O. **Bump `SCHEMA_VERSION` to 2** when you add fields so the frontend can evolve rendering safely (the field exists for exactly this).
- **The failure narrative is already half-built.** `_PatchFailureFamily` (`:661`) carries `title` + an `explanation` that for `TEST_REGRESSION`/`HARNESS_ERROR` is *already* "What happened: … Why: … What next: …" (`:708-739`). The family is keyed by `PatchFailureType` **string value** and fails safe to `_FAMILY_UNEXPECTED` for unknown/None (`:814-822`). This is your richest narrative source — restructuring the family into explicit `what_happened`/`why`/`whats_next` fields (and composing today's `explanation` from them) is the natural move if you pick D2-recompose for the failure family only.
- **The outcome taxonomy to template over.** `stage_contract.OutcomeClass` (`:97`): `SUCCESS` / `CODE_REJECTED` / `INFRA_ERROR` / `POLICY_BLOCKED` / `NEEDS_HUMAN`; `outcome_class_for_failure` (`:139`, total over `PatchFailureType`, never defaults). The §4.8 "templates over the outcome taxonomy" maps naturally: `CODE_REJECTED` → "the change is wrong" why-copy, `INFRA_ERROR` → "the world broke", `POLICY_BLOCKED` → "the rules said no", `NEEDS_HUMAN` → "a human must decide". Note `operator_state` currently keys failure copy off `failure_type` (the finer signal), not `OutcomeClass` — decide whether the narrative keys off the family (finer, what's wired today) or the outcome class (coarser, what §4.8 names) and keep it single-sourced.
- **The wiring — additive and fail-closed already.** `routes/chunks.py:_augment_plan_with_operator_state:2207` adapts loaded run/chunk rows into `OperatorStateContext` (`:2253`), calls `compute_operator_state(context).model_dump()` (`:2343`), and attaches it as `plan.operator_state` (`:2348`); any exception falls back to the `unknown=True` state (`:2344-2347`). **Because you add fields to the `OperatorState` dataclass and `.model_dump()` serializes them recursively (`:53-68`), every read path that dumps operator_state gets `phase`/`narrative` for free — you do NOT wire each call site.** Confirm the other surfacing points (around `:2400`, `:2455`, `:2515`) all go through the same `.model_dump()` so nothing is missed.
- **The frontend contract.** `frontend/src/api/client.ts:459` declares `interface OperatorState` mirroring the dump; `extends ExtraFields` (`:459`) means unknown fields already pass through, so adding `phase`/`narrative` to the type is additive. Consumers: `OperatorAttentionPanel.tsx`, `RunSafetyStrip.tsx`, `operatorPrimaryAction.ts`, `PatchFailureBanner.tsx`, `ChunkPlanPanel.tsx`, `RunDetailPage.tsx`. **Scope your frontend change to surfacing the phase + structured narrative; keep old controls/copy live (the design doc's "Control Consolidation" is a *later* slice, not this one).**
- **The precedence + matrix spec.** `docs/design/operator-state-attention-panel.md` is the authoritative description of every state, its `waiting_on`/`decision_type`, and the precedence order (its "State and Action Matrix" + "Precedence Rules"). Your phase projection should be consistent with that matrix — it is the closest thing to a phase oracle that already exists. Update that doc with the phase column when you finish (it is the home for this contract).

---

# ITEM 16 — phase / narrative read-model (extends `operator_state`)

## 16.1 Summary & scope

Add, as **additive, derived, display-only** fields on `OperatorState`: (1) a **`phase`** — exactly one of the six B1 values projected deterministically from the already-computed state; (2) a **`narrative`** — a structured `{what_happened, why, whats_next}` templated over the outcome taxonomy and the already-computed action set. Bump `schema_version` to 2. Surface both in the frontend attention panel without removing or changing existing controls. **No persistence, no migration, no new endpoint, no new authority, no LLM call, no behavior change** — a run executes byte-identically with and without this slice.

**Outcomes the design must deliver (the *what* is fixed; the *how* is yours to design and lay out in the design you submit for review):**
- A **pure phase projection** (D3): given an `OperatorState` (+ whatever minimal context it needs, e.g. the run-status that splits Planning/Working), return exactly one `RunPhase`. Total and defaulting **safe**: any unmapped/unknown/fallback state → `Needs attention` (never `Done`/`Working`). The ambiguous boundaries (D1) are ruled explicitly and pinned by tests.
- A **structured narrative** (B2/D2): `{what_happened, why, whats_next}` for every state. `whats_next` is **derived from the computed action set** (one source of truth for actions); `what_happened`/`why` are templated/authored deterministically, sourced from the `_PatchFailureFamily` split for failures. Never asserts an action the state didn't compute; never asserts correctness.
- **Additive serialization:** new fields flow through `OperatorState.model_dump()` so all existing read paths carry them with no per-call-site wiring. `schema_version` → 2.
- **Frontend surfacing:** the attention panel renders `phase` + the three narrative parts; old controls stay live; risk decisions stay neutral (no glowing CTA); no generic "Continue".
- **The fail-closed path projects safe:** `OperatorStateContext(unknown=True)` → phase `Needs attention` + the unknown-state narrative.

**Explicitly out of scope (name these in your PR):**
- **Item 17** — trivial-task stage profile + prompt caching (the last Phase 4 slice).
- **Any chat/thread/history feed** or `GET /runs/{id}/thread` — the design doc forbids a history feed in this work; the narrative is *current-state-only*, like `trust_facts`. (The Pass-3 thread endpoint is a separate, later slice.)
- **LLM phrasing polish** — deterministic templates only this slice (B3).
- **Renaming/migrating** any DB status string, `OutcomeClass`, `PatchFailureType`, verdict, or review/finding enum (B1/B4).
- **Control consolidation / removing old banners** — keep old controls live; consolidation is a later slice (design doc "Control Consolidation").
- **Any new persistence, table, column, migration, route, or authority** — `operator_state` stays computed-on-read and never persisted.
- **Changing eligibility predicates, the gate machinery, `scope_guard`, `patch_applier`, the commit/rollback path, or any `operator_state` *context-adaptation* logic** beyond what's needed to pass the run-status signal the Planning/Working split needs (if any).

## 16.2 Verified current behavior (re-confirm before editing)

- `operator_state.py`: `OperatorState` at `:103`; `compute_operator_state` precedence chain at `:200-288`; `SCHEMA_VERSION = 1` at `:20`; `_PatchFailureFamily` what/why/next prose at `:708-739`; the family map + safe fallback at `:797-822`; the fail-closed `_unknown_state()` at `:1334`.
- `stage_contract.py`: `OutcomeClass` at `:97`; `outcome_class_for_failure` total map at `:115-147`.
- `routes/chunks.py`: `_augment_plan_with_operator_state` at `:2207`, building `OperatorStateContext` at `:2253`, dumping at `:2343`, fail-closed fallback at `:2344-2347`, attach at `:2348`. Other operator_state read paths near `:2400`, `:2455`, `:2515` — confirm they all dump the same dataclass.
- `frontend/src/api/client.ts`: `OperatorState` interface at `:459`, `extends ExtraFields` pass-through, `operator_state?` on the plan response at `:485`.
- **Confirm the Planning/Working signal exists at read time.** The split needs to know whether a running run is forming the plan vs executing chunks. `OperatorStateContext` already carries `run_status` (`:131`) and `is_running` is derived from `{RUNNING, RUNNING_CHUNKS, PUSHING}` (`routes/chunks.py:2220-2224`). Verify `run_status` is enough to distinguish them; if the projection needs it, thread it (it's already on the context — likely no new field needed).
- **Confirm nothing reads `operator_state` for a decision.** Grep routes + frontend: `operator_state`/`operatorState` must be display-only (the design doc's frontend-responsibility split). If any route or mutating frontend path branches on it, that's a pre-existing issue to flag, and the new fields must not deepen it.

## 16.3 Tests that must exist and pass

- **§0 headline — display-only, zero behavior change:** a backend test (or reuse of an existing orchestrator/route test) proving a representative run's *behavior* and the *existing* operator_state fields are unchanged; the only delta in the read response is the two additive fields. Capture a pre-change parity snapshot of the operator_state suite + the chunk/final approval route suites before you touch `operator_state.py`.
- **Phase projection is total and safe (the test you cannot ship without):** every state `compute_operator_state` can produce maps to exactly one of the six phases; the `unknown=True`/fallback state → `Needs attention` (assert it is **never** `Done`/`Working`/`Planning`). Drive this by iterating the state builders or a representative context per branch — mirror how `stage_contract` tests iterate every `PatchFailureType`.
- **Phase boundary rulings (D1) are pinned:** one test per ruled boundary — retryable patch failure → (your ruling); each risk-decision gate (scope pending, memory conflict, weak-test ack, reviewer ack) → (your ruling); stalled → (your ruling); a planning-phase running run → `Planning` and an executing run → `Working`. These tests are the executable record of the D1 decisions.
- **Narrative structure + provenance:** for a failure state, `narrative.what_happened`/`why` match the `_PatchFailureFamily` source (no drift), and `whats_next` lists exactly the actions the state computed — assert it contains the `primary_action`/`neutral_actions` labels and **does not** advertise any `blocked_action` as available. For a success/strong-tests state, `whats_next` never claims an action the state didn't compute and the narrative never asserts code correctness.
- **`whats_next` ⊆ computed actions (no phantom action):** a property-style assertion across states that every action named in `whats_next` corresponds to an actual computed action id — this is the anti-nudge guard (§0).
- **No raw evidence leakage (contract §6):** assert the narrative carries no raw test output blob / diff / stack trace / provider error (evidence summaries are short; the capped `technical_details` stays off the panel).
- **`schema_version` bumped to 2** and the dump still round-trips through the frontend type (a frontend build with the extended `OperatorState` interface passes).
- **Parity:** the existing operator_state tests, the chunk/final approval route suites, and the item-15 ack/operator-state surfacing tests stay green (unmodified if you pick D2-additive; re-baselined with a named comment if you pick D2-recompose and change `explanation` bytes). Frontend `npm.cmd run build` clean; `ruff check` clean on changed backend files.

## 16.4 Traps

- **(a) The narrative advertising an action the state didn't authorize.** `whats_next` is a *projection of the computed actions*, not a fresh suggestion. If it can name an action that isn't in the action set — or present a blocked action as available — it has become a nudge that contradicts the safety ledger (§0; design-doc rules 10–11). Derive it; don't author it freehand.
- **(b) The fallback projecting to a "fine" phase.** The fail-closed `unknown` state is the *most* important one to get right: it MUST be `Needs attention`. A bug that maps it (or any unmapped state) to `Done`/`Working` silently tells a human a stuck run is healthy — the inverse of fail-safe (§0, contract §9).
- **(c) Claiming correctness.** "Strong tests" is a *process* signal. The narrative's `why`/`whats_next` must inherit the existing copy's honesty ("does not prove code correctness"); never let a template upgrade it to "the code is correct" (design-doc safety-ledger rule 12).
- **(d) Leaking raw evidence into the narrative.** Keep `what_happened`'s evidence summary short and human-readable (e.g. "2 new test failures in test_app.py; 3 pre-existing"). Raw output/diffs/errors stay on the capped `technical_details`, off the panel (contract §6).
- **(e) Drift between `explanation` and the triplet (if D2-additive).** Two copies of the same state's prose can diverge over time. Mitigate by sourcing both from one place where practical (the families already do this for failures) and by a test asserting they agree for the failure states.
- **(f) Renaming internal states to match phase names.** The six phases are a *derived* layer. Do not touch `RunStatus`/`ChunkStatusValue`/`OutcomeClass`/`PatchFailureType` strings (B1/B4) — the projection reads them, it does not rename them.
- **(g) Persisting or gating on the phase.** It is recomputed on every read like the rest of `operator_state` (design-doc invariant). No table, no column, no route branch on `phase`.
- **(h) Inventing a parallel read-model.** Extend `OperatorState` (B4). Do not add a second endpoint or a separate phase object the UI has to reconcile with `operator_state`.
- **(i) Building the history feed.** The narrative is current-state-only (like `trust_facts`). A timeline of prior attempts/turns is the Pass-3 thread endpoint — explicitly out of scope and forbidden here.

## 16.5 Safety-contract check (item 16)

- **§2.1 / §2.2 (gates / scope):** untouched. Item 16 reads computed state; it adds no gate, approval, execution, or scope path. Prove a run's behavior and the existing operator_state fields are unchanged (§16.3 §0 test).
- **§2.6 (no secrets/PII; sanitize):** the narrative is templated from outcome class + already-sanitized state copy; it carries no raw output/diff/error. Prove no evidence leakage.
- **§2.7-analog (advisory only):** the phase/narrative is a display derivation with **zero authority** (proposal §5.6). `whats_next` mirrors computed actions; nothing reads the new fields to decide. Prove `whats_next` ⊆ computed actions and no route/frontend branches on `phase`/`narrative`.
- **§2.9 (fail safe) + §0:** the fail-closed `unknown` state projects to `Needs attention` + the unknown narrative — never to a phase that implies health. Prove the fallback and every unmapped state default to `Needs attention`.
- **No-persistence / no-migration invariant:** `operator_state` stays computed-on-read; this slice adds no table, column, migration, or durable field. Prove there is no schema change.

---

## 3. Update these docs when you finish (part of "done")

1. **`PIPEWRIGHT_REDESIGN_WORKPLAN.md`** — mark item 16 done in the Phase 4 sequence + "How to resume" + the TL;DR "Where we are" bullet. Note the one remaining Phase 4 slice (item 17 — trivial-task profile + prompt caching).
2. **`docs/design/operator-state-attention-panel.md`** — add the `phase` projection to the state matrix (it is the home for this contract); record the D1 boundary rulings there.
3. **This file** — once item 16 lands + is reviewed, repurpose it for the **last Phase-4 slice (item 17)**.
4. A short per-item spec/changelog note if you follow the `specs/` convention.
5. These planning docs are **untracked**. Update content; **do not commit** them (or any code) unless the maintainer asks. If asked to commit: branch off `develop` first (never straight to `develop`/`main`), one item per commit, end with the repo's `Co-Authored-By` trailer.

## 4. Working discipline (this PR)

- **Design first, then get it reviewed, then implement.** Produce a short design — the `RunPhase` enum + the projection function and its precedence (with the D1 boundary rulings called out), the narrative shape and where `what_happened`/`why`/`whats_next` come from (your D2 choice + justification), where it attaches, the `schema_version` bump, and the frontend surfacing — and have a human review it **before** writing code. You own the design; because you also write the tests, the design review is the homework check.
- Read the real code first; re-verify every `file:line`; correct the brief's pointer if the live code drifted and say so.
- **Capture a parity snapshot** of the operator_state suite + the chunk/final approval route suites + the item-15 surfacing tests before you touch `operator_state.py` — "I didn't change behavior or existing copy" is only provable against a pre-change baseline.
- Smallest correct change; **item 16 only**; list what you deliberately did **not** change (item 17 trivial-profile/caching, chat/thread feed, LLM polish, control consolidation, status renames, persistence/migration, eligibility/gate/commit paths).
- Tests assert the **decided behavior** (B1 six phases, B2 three-part narrative, D1 boundary rulings, `whats_next` ⊆ computed actions) **and the §0 invariant** (display-only, fallback → `Needs attention`, no correctness claim, no evidence leak) — not just that code runs.
- Report on completion: changed files, tests run + results, manual validation, risks, what was intentionally left untouched.
- A human reviews this PR.
