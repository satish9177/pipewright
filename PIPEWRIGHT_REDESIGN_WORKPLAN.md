# Pipewright Redesign — Workplan & Handoff (START HERE)

**Last updated:** 2026-06-11
**Purpose:** Orientation and resume-point for the redesign effort. If you have no other context, read this first — it explains *what* we're doing, *how* we decided to execute it, *where we stopped*, and *what's next*. It is an index over the other planning docs, not a replacement for them.

---

## TL;DR

- **What:** executing the Pipewright pipeline + memory redesign described in `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (the authoritative, code-verified design).
- **How (the meta-decision):** *not* hand-written PR-by-PR. **Fable 5 owns the full implementation** — it reads the proposal, reasons about edge cases, writes the tests, and implements. A human reviews each completed PR before the next batch starts. Harness for this lives in `FABLE5_IMPL_SPEC_BRIEF.md`.
- **Where we are (2026-06-11):** **Phase 0 is complete and code-verified** (all items present + 105 targeted tests pass; P7 deferred to Phase 2 by design). **§5.1 and §5.2 are now DECIDED** (baseline gate + scoped option default-off; auto-retry budget 1). **Phase 1 is COMPLETE and code-verified** (308 targeted tests pass): item 7 Signal C execution-integrity classification is pure in `backend/pipeline/test_run_validation.py`; item 8 split post-apply test failures into `TEST_REGRESSION` vs. `HARNESS_ERROR` with one non-timeout harness auto-retry (`AUTO_RETRY_INFRA_BUDGET = 1`); item 9 baseline-aware verification (`run_baseline_tests` + `_ensure_verification_baseline` + baseline roll-forward + disclosure in the orchestrator) with the scoped-verification policy knob (`SCOPED_VERIFICATION_ENABLED = False`, default-off, not wired into execution). **Next is the Phase 1/2 boundary — STOP and reassess before starting Phase 2.** Do not start Phase 2.
- **CRITICAL BOUNDARY:** the parallel batch is endorsed **only for the independent Phase 0 items.** **Stop and reassess at the Phase 1/2 line** — the four §5 decisions *and* the strangler refactor of `_execute_single_chunk`. There, human judgment + a strong-model review gate become load-bearing again. Do not let momentum carry the loop across that line unreviewed.

---

## Why this workflow (and its limits)

The redesign's hard value is in *decisions and edge cases*, not typing. Fable owns both reasoning and implementation. This is sound **for Phase 0** because those items are independent of each other, of every later item, *and* of the §5 decisions — so parallel work doesn't conflict and no model is forced to guess a product decision.

It is **not** sound as a flat fan-out across the whole redesign, for three reasons:
1. **Items past Phase 0 are a dependency chain on shared hot files** (`chunked_orchestrator.py`, `patch_failures.py`, `tester.py`, the policy object). Parallel work would conflict at exactly the integration seams.
2. **Four §5 decisions are the user's, not a model's.** Implementing a dependent item before they're decided routes a product decision to a model — the exact anti-pattern Pipewright exists to prevent.
3. **Phase 2 is a behavior-preserving refactor of the apply/commit/rollback core** — the place a subtle miss becomes a safety regression. It needs a human review gate, not unsupervised implementation.

---

## The documents — which is canonical

| Doc | What it is | Status |
|---|---|---|
| **`PIPEWRIGHT_REDESIGN_PROPOSAL.md`** | The authoritative, code-verified design. Pass 1 = pipeline engine (E-items, §1–7), Pass 2 = memory (M-items, §8+), plus addenda. Contains the §5 decision points and §6 phasing. | **Canonical. Supersedes the earlier reviews.** |
| `ARCHITECTURE_REVIEW.md` | Older, code-*unverified* bug list (P1–P8 pipeline, M1–M7 memory) written for Fable. | Mostly **subsumed** by the proposal (which corrected several of its claims as stale). Keep only for the 3 gaps below. |
| **`FABLE5_IMPL_SPEC_BRIEF.md`** | **The active working doc.** Harness telling Fable how to approach per-task implementation; first task block = E2. | **In use.** |
| **`PIPEWRIGHT_REDESIGN_IMPL_BRIEF.md`** | **The rolling per-phase implementation handoff brief.** Repurposed each phase; current contents = **Phase 2** (items 10–12 + P7). Was previously the Phase-1 items 8 & 9 brief (now superseded; Phase 1 outcome lives in this workplan). | **Active for Phase 2.** |
| `FABLE5_DESIGN_BRIEF.md`, `FABLE5_ISOLATION_ADDENDUM.md`, `FABLE5_PASS3_BRIEF.md` | The design briefs that *produced* the proposal. | Historical input. |
| `PIPEWRIGHT_REDESIGN_BRIEF.md`, `PIPEWRIGHT_REDESIGN_UI_MOCKUP.svg` | The originating brief + a UI mock. | Input / reference. |

### What the proposal does NOT cover (don't lose these)

From the 2026-06-10 coverage check of `ARCHITECTURE_REVIEW.md` against the proposal, three items have no home in the proposal:
1. **P7** — branch-HEAD drift check on *resume* (only checked on fresh execution today). **DEFERRED to Phase 2 (2026-06-11 decision).** Investigation found `_commit_and_complete_chunk` records no per-chunk commit SHA, so a correct resume HEAD-drift check needs recorded state. A no-new-state heuristic risks false-positive dead-ends on the safety-critical resume path; the correct exact-SHA version overlaps Phase 2's attempt ledger (which records per-chunk HEADs natively). Resume already fails closed on missing commits (`_verify_completed_checkpoint_safe`), dirty tree, and missing branch — so the residual gap (foreign commits stacked on top) is the only uncovered case. Do P7 as part of Phase 2.
2. ~~**ARCH-M1**~~ ✓ **done (2026-06-11)** — the `project_id IS NULL` sweep is now a one-time startup migration (`migrate_unscoped_pre_m1_memory`), removed from `add_fact` / `load_hard_facts` / `list_all_facts`.
3. **P2 (outbox/saga)** — the proposal judged the double-commit risk "already mitigated" by `_verify_completed_checkpoint_safe` and declined the outbox table. Revisit only if you disagree with that call.

---

## Sequence (proposal §6)

- **Phase 0 — independent, decision-free, parallel-safe** *(the endorsed band)*:
  - ~~E2 `stdin=DEVNULL`~~ ✓ **done**
  - ~~E9 scope-intent parser~~ ✓ **done**
  - ~~E8 main-path symmetry (`files_expected` into planner + dry-run before apply)~~ ✓ **done**
  - ~~E4 shared retry executor (delete the 60s lock-held sleeps)~~ ✓ **done**
  - ~~§8b policy-module hygiene (dead constants → new policy module)~~ ✓ **done**
  - ~~test-command auto-detection~~ ✓ **done + wired** *(POST /projects/detect returns `suggested_test_command`; New Project form prefills it without overwriting user input)*
  - P7 branch-HEAD drift on resume *(ARCH-review item)* — **DEFERRED to Phase 2** (needs the attempt ledger's recorded HEADs; see "What the proposal does NOT cover")
  - ~~ARCH-M1 no-op table scan fix~~ ✓ **done** *(ARCH-review item; one-time startup migration)*
- ~~**Phase 1 — needs §5 decisions:** Signal-C infra classifier; split `TEST_FAILURE_AFTER_APPLY` → regression/harness; baseline-aware verification.~~ ✓ **done** (§5.1 + §5.2 decided; all three items implemented + 308 targeted tests pass)
- **Phase 2 — strangler refactor:** extract stages → driver replaces `_execute_single_chunk` → retry/resume collapse into entry modes + attempt ledger.
  - **Item 10 (stage contract): IMPLEMENTED 2026-06-11, reviewed & accepted 2026-06-11** (the Phase-2 per-PR gate passed). Golden snapshots captured **pre-refactor** in `backend/tests/test_golden_chunk_execution.py` (6 scenarios, run green before any change); `backend/pipeline/stage_contract.py` adds `OutcomeClass`, the total `PatchFailureType→OutcomeClass` map, `StageOutcome`, and thin adapters; the orchestrator's only change is importing `classify_test_failure`/`build_test_failure_report` from their new canonical home (aliased, behavior-identical). No caller/control-flow change; outcome class is carried, not acted on. **Item 11 is cleared to start, under the explicit constraint that item-10 golden behavior must remain unchanged** (the `test_golden_chunk_execution.py` snapshots stay green).
- **Phase 3 — needs Phase 2:** continuation / steer turns.
- **Phase 4:** reviewer ack soft-gate; phase/narrative read-model; trivial-task profile + prompt caching.

---

## Phase 0 implementation batching

Fable implements all remaining Phase 0 items. Because some touch shared files, they run in three batches — each batch merges cleanly before the next starts.

| Batch | Items | Key files touched | Can run in parallel? |
|---|---|---|---|
| ~~**A**~~ | ~~E9 scope-intent parser · test-command auto-detection~~ | ~~`file_scope_intent.py`, new detection module~~ | ✓ **done** |
| ~~**B**~~ | ~~E8 main-path symmetry · E4 retry executor~~ | ~~`planner.py`, `coder.py`, `chunked_orchestrator.py`~~ | ✓ **done** |
| ~~**C**~~ | ~~§8b policy-module hygiene~~ ✓ · ~~ARCH-M1~~ ✓ · P7 (deferred) | stage files + policy module + memory_store | §8b + ARCH-M1 done; P7 deferred to Phase 2 |

All of Phase 0 is now complete except P7, which is deliberately deferred to Phase 2 (it needs the attempt ledger's recorded per-chunk HEADs to be done without false-positive dead-ends on resume).

---

## The four §5 decisions (the user's call; they gate Phase 1+)

1. **§5.1 Verification semantics:** ✅ **DECIDED (2026-06-11): accept the regression-vs-baseline gate AND add scoped-verification as a policy option, default OFF.** Chunk gate fails only on *newly failing* tests vs. a recorded baseline; pre-existing failures are disclosed, never charged to the chunk; full suite still gates final approval. Gates Phase 1 item 9.
2. **§5.2 Auto-retry of `INFRA_ERROR`:** ✅ **DECIDED (2026-06-11): auto-retry budget = 1, then pause for a human** (conservative end of the proposal's 1–2 range). `INFRA_ERROR` class only — never code/scope failures. Gates Phase 1 item 8.
3. **§5.3 Steer-without-replan** inside unchanged scope: **PENDING** — gates Phase 3 (item 13), not Phase 1. Confirm; choose the conservative variant (any new-file mention forces re-confirm) or not.
4. **§5.4 Reviewer ack gate:** **PENDING** — gates Phase 4 (item 15), not Phase 1. Approve the ack-required severity set (proposal: `high` × {`requirement_mismatch`, `security`}).

Plus §7 open questions (attempt-budget defaults; whether post-success refinement ships in the first continuation slice; confirm test-command detection can ship immediately).

---

## How to resume (next actions)

1. ~~E2 `stdin=DEVNULL` — done.~~
2. ~~Batch A (E9 + test-command detection) — done.~~
3. ~~Batch B (E8 + E4) — done.~~
4. ~~§8b policy-module hygiene — done.~~
5. ~~ARCH-M1 (one-time startup migration) — done.~~
6. ~~Wire `detect_test_command` into the setup route + New Project form — done.~~
7. **P7 deferred to Phase 2** — fold the resume HEAD-drift check into the attempt-ledger work (it records per-chunk HEADs, making the check exact and false-positive-free).
8. ~~**Before Phase 1:** the user rules on the four §5 decisions.~~ **§5.1 + §5.2 decided 2026-06-11** (the two that gate Phase 1); §5.3/§5.4 still pending but gate Phases 3/4. Do **not** let the loop cross into Phase 2 (the strangler refactor) unreviewed (see CRITICAL BOUNDARY).
9. ~~**Phase 1 sequence (separate PRs, in order):** item 7 Signal C classifier → item 8 split `TEST_FAILURE_AFTER_APPLY` → regression/harness + auto-retry budget 1 → item 9 baseline-aware verification + scoped option default-off.~~ ✅ **all done.** item 7 = `classify_execution_integrity` in `test_run_validation.py`; item 8 = `TEST_REGRESSION`/`HARNESS_ERROR` in `patch_failures.py` (uses §5.2; `TEST_FAILURE_AFTER_APPLY` retained for backward compatibility); item 9 = `run_baseline_tests` + `_ensure_verification_baseline` + baseline roll-forward + `SCOPED_VERIFICATION_ENABLED = False` in `policy.py` (uses §5.1). 308 targeted tests pass. (Handoff brief was the rolling `PIPEWRIGHT_REDESIGN_IMPL_BRIEF.md`, now repurposed for Phase 2.)
10. ~~**Next: the Phase 1/2 boundary (CRITICAL BOUNDARY above).**~~ The user authorized **item 10 only** (2026-06-11); it is implemented with tests (golden snapshots captured pre-refactor; full unit suite run). **Implementation handoff brief: `PIPEWRIGHT_REDESIGN_IMPL_BRIEF.md` (Phase 2 = three serialized PRs, items 10 → 11 → 12, with P7 folded into item 12).**
11. ~~**Next: human review of item 10** (per-PR gate).~~ **Item 10 reviewed & accepted 2026-06-11.** Item 11 (the driver + rollback move — the highest-risk hunk of Phase 2) is now **cleared to start, under the explicit constraint that item-10 golden behavior must remain unchanged** (`test_golden_chunk_execution.py` snapshots stay green). Do not start item 12 unreviewed.

---

*Note: these planning docs are currently untracked (not committed). Commit them only when asked.*
