# Design Brief for Claude Fable 5 (extra-high effort): Pipewright Redesign — Pass 3 + Proposal Amendments

**Date:** 2026-06-10
**Mode:** DESIGN ONLY — do not implement. Produce written design appended to `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (as **Pass 3** plus an **Amendments** section to Passes 1–2), in the same evidence-first style: verify every claim against the code, cite `file:line`, keep a verification ledger, name safety-contract tensions as explicit decision points.
**Input docs:** `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (Passes 1–2, accepted as the baseline), `PIPEWRIGHT_REDESIGN_BRIEF.md`, `FABLE5_DESIGN_BRIEF.md`, `ARCHITECTURE_REVIEW.md`.
**Existing UX docs to read first:** `docs/design/run-detail-guided-ux.md`, `docs/design/active-chunk-guided-ux.md`, `docs/design/state-gated-tier2-run-detail.md`, `docs/design/failure-state-ux-cleanup.md`, `docs/design/operator-state-attention-panel.md`.

The proposal was reviewed and rated strong; this brief covers (1) the specific gaps that review found in Passes 1–2, and (2) the missing frontend/UX pass. Everything below was discussed against the actual code on 2026-06-10; verify it again before designing — do not inherit this brief's claims unverified.

---

## Part 1 — Amendments to Pass 1 (gaps to close before Phase 2 sign-off)

These came out of a critical review of the proposal. Each needs a designed answer, not a hand-wave.

### 1.1 Flaky tests break the baseline design (§4.4)

The baseline-aware verification handles *stably* pre-existing failures, but a flaky test that is green at baseline and red after chunk N gets charged to chunk N as `CODE_REJECTED` — exactly the false-blame failure mode the redesign exists to kill. Design a policy: re-run-on-new-failure (confirm a new failure is deterministic before charging it), a quarantine/known-flaky list, or both. State the cost (extra test runs) and where the knob lives (the policy module, §4.7).

### 1.2 The attempt ledger and turn log have no data model

The proposal asserts "additive tables/columns" but never sketches the schema. Design the actual tables: attempts (entry mode, steer text, per-stage outcomes, evidence refs, final classified outcome) and the run turn log (see Part 3/4 below — one design must serve both). Follow the existing append-only patterns (`memory_injection_events`, `llm_call_provenance`, `checkpoints` in `backend/db/schema.sql`). Constraint: `init_db` splits `schema.sql` on `;`, so no semicolons inside SQL comments.

### 1.3 "Zero quality loss by construction" (§4.7 trivial profile) needs a measurement plan

The merged plan+code stage for trivial chunks claims zero quality loss by construction of the profile. Design the empirical check: e.g., reviewer-finding rates on profiled vs. unprofiled trivial chunks over a soak period, with a defined rollback trigger. The always-on reviewer is a backstop, not a proof.

### 1.4 Success metrics for the redesign targets

T1–T16 are unquantified. Define a small set of measurable acceptance signals (e.g., chunk dead-end rate, runs abandoned after failure, human-retry success rate, time-to-first-relief per phase) and where they are recorded (the attempt ledger should make most of them derivable queries, not new instrumentation).

### 1.5 Dependency guard must be named a driver invariant

Chunk dependencies are well handled today at three layers: plan-time Pydantic validation (forward-only `depends_on`, exactly 1..N numbering, references must exist — `backend/models/chunk.py:32-37, 61-80`) and the #24A execution guard (`_unmet_dependencies`, `backend/pipeline/chunked_orchestrator.py:409-425`, enforced at `:1356-1362`, fails safe on any non-`completed` dependency status). §4.1's list of invariants "enforced identically in every mode" does not name this guard. The new `steered` and `auto_retry` entry modes re-enter mid-run; the dependency precondition must be on that invariant list explicitly.

### 1.6 Phase 2 extraction needs failure-path tests, not just golden paths

`tester.py` has rollback welded into it (`:201, 226, 247`); moving rollback to the driver is a behavior change on *failure* paths that golden-path "identical behavior" tests will not cover. Design the failure-injection test matrix for the extraction.

### 1.7 Chunk sizing: deterministic post-triage advisory

Chunk *division* quality is currently prompt-only (`triage.py:41-51`: easy=1 / medium=2–3 / hard=3–6, "must fit context window using token_estimate") — `token_estimate` is the LLM's own guess, validated only as `>= 0` (`models/chunk.py:21`), never checked against indexed file sizes or the resolved model's context window. Design a deterministic post-triage validator: compare summed indexed token estimates of each chunk's `files_expected` against a policy budget; surface "this chunk looks oversized/undersized" as an *advisory* on the plan-approval screen (same pattern as the existing grounding and consistency checks — never auto-mutate the plan, the human decides). Do **not** redesign dependency handling (see 1.5 — it is already correct); this item is sizing only.

---

## Part 2 — Plan-gate turns (conversational plan refinement before approval)

The §4.3 turn primitive starts at execution time. The plan gate is still approve-or-reject: if the plan is wrong ("split chunk 2", "do it in the service layer, not the route"), the only move today is reject and start a new run. §4.6's editable constraint fields cover *structured* scope edits only — there is no free-text "revise the plan" turn.

Design **plan-gate turns** as a named Phase 3 slice:

- User message at the chunk-plan approval gate → re-triage/re-plan with the previous plan + message as carried context → revised plan → **approval still required** (the gate never weakens; iteration produces a *better* approval artifact).
- Reuse the same turn-log primitive as §4.3 (one schema, two targets: plan gate and chunk attempts).
- Precedent for carrying context into re-planning: the signed clarification flow at `routes/chunks.py:582-607`.
- This is the cheapest, safest turn class — nothing has executed, zero scope/rollback risk. Sequence it accordingly (it may be shippable before execution-time steering; say whether it should be).
- Plan version history: decide whether superseded plans are kept (recommend yes — append-only, audit) and how the diff between plan versions is shown at the gate.

---

## Part 3 — Thread/run boundary semantics (confirm and write down)

The thread **is** the run. Make this an explicit design statement in Pass 3:

- Within a run the thread never breaks; every steer/attempt/gate lives in that run's turn log.
- A new run is a new thread. Old threads' messages are **never** injected into new runs. Only three things cross the run boundary: (1) committed code (re-indexed repo state), (2) human-approved project memory — including the post-run hygiene → pending suggestion → human approval bridge from Pass 2 §11.4, (3) read-only audit history in the UI.
- Raw transcript carryover across runs is **deliberately rejected**: it would be an unaudited knowledge channel bypassing the memory trust spine (contract §2.7/§2.8), it is mostly stale by construction, and it is token noise against the "best minimal relevant set" rule. Record this as a rejected alternative with reasons, like §5.5 did for keeping failed diffs applied.
- A "reference old run N" linking feature is out of scope; if something from an old run matters durably, the channel is approved memory.

---

## Part 4 — Pass 3: Frontend/UX design (the main deliverable)

The proposal ships UI-ready backend read-models but no frontend design. Design the run experience as **one conversation thread with state-gated actions** — the goal is the conversational feel of modern agent tools while keeping every Pipewright gate intact.

### 4.1 What already exists — build on it, do not reinvent

- `backend/pipeline/operator_state.py` already computes a per-state action surface: `compute_operator_state` returns `primary_action`, `neutral_actions`, `secondary_actions`, and `blocked_actions` (with block reasons — e.g. the wrong-branch state blocks mutating actions and explains why). The "which buttons should show right now" question is already answered server-side; today's frontend renders too much of the surface at once.
- Pass 1 §4.8's phase + narrative read-model (six phases; narrative = what happened / why / what's next, where "what's next" is the legal actions for the outcome class) is the contract Pass 3 designs against.
- The five `docs/design/*ux*.md` docs are the existing direction; Pass 3 extends them, it does not start a parallel model.

### 4.2 The thread UI

- Run detail becomes a conversation: turn log (user messages, attempt outcomes, narratives) rendered chronologically; gates rendered **as inline action cards in the thread** — a message-shaped card with its explicit button(s). The conversation is the container; the gate is a card in it.
- Show only `primary_action` prominently; secondary/neutral collapse behind a single "more" affordance; blocked actions appear only with their reason when relevant. One primary action at a time, placed in thread context, is the whole point.
- Attempt history per chunk (entry mode, outcome class, narrative) must be inspectable from the thread without leaving it — the user steers with full context, not blind.
- Raw statuses/enums never render; phases + narratives only (raw values stay in the API for compatibility per §4.8).

### 4.3 Safety rules for the chat input (non-negotiable)

- **Free text never approves anything.** Approval is always an explicit structured action on a gate card. A typed "yes go ahead" is advisory text, not an approval. (Contract: steers are advisory; approval gates cannot be bypassed.)
- **Free text never grants scope.** Out-of-scope steers route to the existing scope-expansion approval flow; the narrative says so (Pass 1 §4.3 already establishes this — keep it).
- **The thread UI ships with steered attempts, not before.** A chat box that accepts messages the backend cannot act on (the historical `retry_with_instruction` situation — action surfaced in UI, no execution path) makes trust worse than buttons. State this sequencing dependency explicitly.

### 4.4 Message queueing semantics

The chat input stays available while the engine is busy; messages queue:

- A message sent mid-attempt lands as a `pending` turn (append-only turn log), consumed at the next legal entry point. Execution is already serialized (project repo lock; the driver is single-writer-per-chunk), so send-while-busy is safe by construction.
- **Queued messages hold behind gates.** If the run pauses at an approval gate, pending turns wait until the human acts on the gate — a queued steer must never be what un-pauses a gated run.
- Decide and record: batching (recommend: all pending messages batch into the next attempt's context — matches how people type), queue-depth cap (policy), edit/cancel of a pending turn before consumption, and what the UI shows for queued state.

### 4.5 Persistence

Turn log + attempt ledger schema is Part 1 item 1.2 — design once, serve both the engine and the UI. Append-only, additive, following `memory_injection_events` / `llm_call_provenance` patterns. The read side is a derived endpoint joining turns + attempts + gates + narratives in time order; no new state machine.

### 4.6 Scope of Pass 3

In scope: the thread/card UI model for run detail, plan-gate rendering (incl. Part 1 item 1.7's sizing advisory and §4.6's editable constraints), queueing, the reviewer-ack panel + "steer this" placement (Pass 1 §4.5), and the memory housekeeping digest placement (Pass 2 §11.4). Out of scope: visual design/styling, the memory management pages beyond the digest, and anything that changes gate *semantics* — Pass 3 changes how gates are presented, never what they require.

---

## Constraints (carried verbatim)

- All nine safety invariants from `PIPEWRIGHT_REDESIGN_BRIEF.md` §2 / CLAUDE.md's safety contract. AI never gates, approves, or merges; memory stays advisory; scope_guard stays the authority.
- Quality-first (§0): no UX simplification that hides information a human needs at an approval gate; gates inform, never nag, never get bypassed for smoothness.
- Design only. Small, separately-testable PR sequencing at the end, slotted into the proposal's existing unified sequence (§15) with explicit dependency edges.
- Where the current design is already good, keep it and say why. Where this brief is wrong, correct it with evidence — the proposal earned trust by auditing its own brief; do the same to this one.

## Deliverables

1. **Amendments section** appended to the proposal answering Part 1 items 1.1–1.7 (each with evidence, design, and tests).
2. **Plan-gate turns** design (Part 2) as a named slice with its decision points.
3. **Pass 3 — Frontend/UX** (Parts 3–4): thread/card model, action gating against `operator_state`, queueing semantics, thread-boundary statement, persistence schema (shared with 1.2), sequencing.
4. Updated consolidated decision points (§16) and unified sequencing (§15) reflecting all of the above.
5. Verification ledger for every claim this brief made that you checked.
