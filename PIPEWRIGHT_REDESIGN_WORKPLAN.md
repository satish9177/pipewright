# Pipewright Redesign — Workplan & Handoff (START HERE)

**Last updated:** 2026-06-11
**Purpose:** Orientation and resume-point for the redesign effort. If you have no other context, read this first — it explains *what* we're doing, *how* we decided to execute it, *where we stopped*, and *what's next*. It is an index over the other planning docs, not a replacement for them.

---

## TL;DR

- **What:** executing the Pipewright pipeline + memory redesign described in `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (the authoritative, code-verified design).
- **How (the meta-decision):** *not* hand-written PR-by-PR. **Fable 5 owns the full implementation** — it reads the proposal, reasons about edge cases, writes the tests, and implements. A human reviews each completed PR before the next batch starts. Harness for this lives in `FABLE5_IMPL_SPEC_BRIEF.md`.
- **Where we are (2026-06-11):** **E2 (`stdin=DEVNULL`) is done** (`backend/pipeline/tester.py` + `backend/tests/test_tester.py`; spec in `specs/E2-stdin-devnull.md`). Next batch: **5 remaining Phase 0 items in parallel** (see batching below).
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
| `FABLE5_DESIGN_BRIEF.md`, `FABLE5_ISOLATION_ADDENDUM.md`, `FABLE5_PASS3_BRIEF.md` | The design briefs that *produced* the proposal. | Historical input. |
| `PIPEWRIGHT_REDESIGN_BRIEF.md`, `PIPEWRIGHT_REDESIGN_UI_MOCKUP.svg` | The originating brief + a UI mock. | Input / reference. |

### What the proposal does NOT cover (don't lose these)

From the 2026-06-10 coverage check of `ARCHITECTURE_REVIEW.md` against the proposal, three items have no home in the proposal:
1. **P7** — branch-HEAD drift check on *resume* (only checked on fresh execution today).
2. **ARCH-M1** — `_archive_unscoped_pre_m1_memory` runs a no-op `WHERE project_id IS NULL` table scan on *every* memory read.
3. **P2 (outbox/saga)** — the proposal judged the double-commit risk "already mitigated" by `_verify_completed_checkpoint_safe` and declined the outbox table. Revisit only if you disagree with that call.

1 and 2 are good candidates for two small standalone Phase-0-style PRs. Recorded here so they don't silently vanish.

---

## Sequence (proposal §6)

- **Phase 0 — independent, decision-free, parallel-safe** *(the endorsed band)*:
  - ~~E2 `stdin=DEVNULL`~~ ✓ **done**
  - E9 scope-intent parser
  - E8 main-path symmetry (`files_expected` into planner + dry-run before apply)
  - E4 shared retry executor (delete the 60s lock-held sleeps)
  - §8b policy-module hygiene (dead constants → new policy module)
  - test-command auto-detection
  - P7 branch-HEAD drift on resume *(ARCH-review item)*
  - ARCH-M1 no-op table scan fix *(ARCH-review item)*
- **Phase 1 — needs §5 decisions:** Signal-C infra classifier; split `TEST_FAILURE_AFTER_APPLY` → regression/harness; baseline-aware verification.
- **Phase 2 — strangler refactor:** extract stages → driver replaces `_execute_single_chunk` → retry/resume collapse into entry modes + attempt ledger.
- **Phase 3 — needs Phase 2:** continuation / steer turns.
- **Phase 4:** reviewer ack soft-gate; phase/narrative read-model; trivial-task profile + prompt caching.

---

## Phase 0 implementation batching

Fable implements all remaining Phase 0 items. Because some touch shared files, they run in three batches — each batch merges cleanly before the next starts.

| Batch | Items | Key files touched | Can run in parallel? |
|---|---|---|---|
| **A** | E9 scope-intent parser · test-command auto-detection | `file_scope_intent.py`, new detection module | Yes — no overlap |
| **B** | E8 main-path symmetry · E4 retry executor | `planner.py`, `coder.py`, `chunked_orchestrator.py` | Sequence: E8 first, then E4 (both touch `planner.py`) |
| **C** | §8b policy-module hygiene · P7 · ARCH-M1 | sweeps stage files + new policy module | After B merges; P7 + ARCH-M1 independent of §8b |

Fable can **write and implement all 5 (or all 8) in one session** if given the full batch list and the sequencing constraint above. The human reviews + merges batch A, then hands batch B, then batch C.

---

## The four §5 decisions — PENDING (the user's call; they gate Phase 1+)

1. **§5.1 Verification semantics:** accept "no new failures vs. baseline" (+ disclose pre-existing) replacing "whole suite green"? Accept scoped-verification as a policy option?
2. **§5.2 Auto-retry of `INFRA_ERROR`:** confirm scope + default budget (proposal: 1–2).
3. **§5.3 Steer-without-replan** inside unchanged scope: confirm; choose the conservative variant (any new-file mention forces re-confirm) or not.
4. **§5.4 Reviewer ack gate:** approve the ack-required severity set (proposal: `high` × {`requirement_mismatch`, `security`}).

Plus §7 open questions (attempt-budget defaults; whether post-success refinement ships in the first continuation slice; confirm test-command detection can ship immediately).

---

## How to resume (next actions)

1. ~~E2 `stdin=DEVNULL` — done.~~
2. **Hand Fable 5 `FABLE5_IMPL_SPEC_BRIEF.md`** with the Batch A task list (E9 + test-command detection). Fable implements both, runs `python -m pytest backend/tests -q -m unit` + `ruff check`, reports.
3. Human reviews + merges Batch A PRs.
4. Hand Fable Batch B (E8, then E4). Merge in order.
5. Hand Fable Batch C (§8b + P7 + ARCH-M1).
6. **Before Phase 1:** the user rules on the four §5 decisions. Do **not** let the loop cross into Phase 1/2 unreviewed (see CRITICAL BOUNDARY).

---

*Note: these planning docs are currently untracked (not committed). Commit them only when asked.*
