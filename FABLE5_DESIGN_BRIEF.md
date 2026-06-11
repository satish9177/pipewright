# Design Brief for Claude Fable 5: Pipeline Execution Engine & Memory Redesign

**Date:** 2026-06-10
**Mode:** DESIGN ONLY — do not implement. Produce a written design/architecture proposal.
**Companion doc:** `ARCHITECTURE_REVIEW.md` (specific bugs/fixes — different exercise from this one)

---

## Why This Brief Exists

The previous review (`ARCHITECTURE_REVIEW.md`) found and proposed fixes for 15 specific bugs. Fixing those bugs will not, by itself, make Pipewright feel trustworthy to a new user.

The deeper problem is two-fold:

1. **The execution engine is brittle on the happy path.** Even small, low-risk tasks fail at a noticeable rate. Each individual failure mode (scope violation, stale index, patch malformed, test failure, dirty worktree, rate limit) is *handled* — but "handled" currently means "stop and show an error." There is no recovery ladder, no self-correction, no graceful degradation.

2. **The memory system is a flat fact list with keyword/category matching.** It cannot answer "what do we already know that's relevant to this request?" with any nuance — it can only filter by category and sort by priority. As the codebase and run history grow, this gets worse, not better.

We want Fable 5 to think about both of these as **design problems**, not bug lists. The deliverable is a written proposal (architecture doc, diagrams, sequence flows, trade-off analysis) — not code.

---

## Part A: Pipeline Execution Engine — "Why does this fail on small tasks, and how do we make it feel solid?"

### Current reality

A "small task" (e.g. "add a new field to a Pydantic model and a test for it") goes through:

```
triage (LLM #1) → planner (LLM #2) → coder (LLM #3) → scope_guard → patch_apply → tests → reviewer (LLM #4)
```

Four LLM calls, each of which can independently fail in ways that abort the whole chunk:

- Triage hallucinates a file path → caught at scope_guard, chunk fails, full re-triage needed
- Planner plans a file outside scope → caught at scope_guard after coder already ran
- Coder's `old_string` doesn't match → `STALE_INDEX_OR_FILE_CHANGED`, chunk fails
- Patch is malformed JSON → 1 retry, then fail
- Tests fail after a *correct* apply → rollback, chunk fails entirely (even if 90% of the change was right)

Every one of these failure types has a `_DEFAULT_MESSAGES` string (see `patch_failures.py`) shown to the user. But the user experience is: **"Chunk 2 failed: STALE_INDEX_OR_FILE_CHANGED"** — and then... what? Does the user retry? Does Pipewright retry? Is the whole run dead? Can they fix it themselves and continue?

### Current run/chunk status model

Pipewright has ~20 status strings across `RunStatus`, `ChunkStatusValue`, `ChunkPlanStatus`, `ApprovalStatus`, `GateStatus` (see `core/statuses.py`). This is a lot of states for a user to mentally map. When something goes wrong, the user lands on one of these strings plus a failure-type message, in `RunDetailPage.tsx` / `RunStatusBadge.tsx` / `RuntimeTestValidationBanner.tsx`.

### Open design questions for Fable 5

1. **Conversational continuation after failure (highest priority — this is the #1 user pain).** Today a run is single-shot: when a chunk fails (e.g. `TEST_FAILURE_AFTER_APPLY`), the patch is rolled back, the chunk is marked `failed`, the UI says "cannot retry," and the user's only real option is to start a brand-new run from scratch. The work and context are thrown away. Users experience this as *"it's just one chat I can't continue."*

   Design a **continuation loop**: after a failure, the user supplies a short natural-language steer (*"the test it wrote is wrong — fix the test, not the helper"* / *"ignore that failing test, it's pre-existing"* / *"the helper is fine, only `tests/test_app.py` needs changing"*) and Pipewright re-attempts the SAME chunk against the current state — carrying forward the prior plan/code/diff and the failure evidence as context — without re-triaging from zero and without discarding what was already correct.

   Constraints to design within (do NOT relax these):
   - The continuation stays inside the chunk's already-approved `files_expected` scope (scope_guard still enforces).
   - It must not bypass chunk-level or final approval gates.
   - It must not commit a no-effective-change patch.
   - User-supplied steers are advisory text, never an instruction to touch forbidden paths or skip tests.

   Propose: where this loop lives relative to the existing `_execute_single_chunk` / human-retry machinery (`_persist_retry_patch_failure`, the `#26D` retry path), how many continuation rounds are allowed before the run is declared terminal (token-spend ceiling), and how prior attempt history is surfaced so the user steers with full context, not blind.

2. **Failure taxonomy → recovery taxonomy.** For each `PatchFailureType`, is the failure:
   - (a) **Auto-recoverable** — Pipewright itself should retry with corrected context (e.g. `STALE_INDEX_OR_FILE_CHANGED` → re-read the file, re-prompt the coder with the new content, retry once automatically before surfacing to the user)
   - (b) **User-actionable** — the user needs to do something (e.g. `DIRTY_WORKTREE` → "commit or stash, then click Resume")
   - (c) **Terminal** — genuinely needs a human decision (e.g. `TEST_FAILURE_AFTER_APPLY` on a chunk that's fundamentally wrong)

   Today almost everything falls into bucket (c) from the user's perspective, even when it's actually (a) or (b). Propose a taxonomy and an automatic-retry policy per failure type, with limits (e.g. max 1 auto-retry per failure type per chunk, to avoid silent loops and runaway token spend).

3. **"What happened, why, what's next" as a first-class object.** Instead of a status string + failure-type string, propose a structured `RunNarrative` (or similar) that the UI renders as a human story:
   - What was attempted (in plain language, not "chunk 2")
   - What went wrong (in plain language, not an enum name)
   - What Pipewright already tried (if auto-retry happened)
   - What the user can do next, as concrete buttons/actions — not just "Resume" / "Reject"

   This should be generated *deterministically from structured data* (failure type, attempt history, chunk description) — not another LLM call, to avoid adding latency/cost/hallucination risk to the failure path itself.

4. **Partial success within a chunk.** Currently a chunk is all-or-nothing: if 3 of 4 file changes apply cleanly and the 4th has a `STALE_INDEX_OR_FILE_CHANGED`, the whole chunk rolls back. Is there a design where:
   - The 3 clean changes commit
   - The 4th becomes its own micro-chunk that retries with fresh context
   - Dependencies are still respected

   Propose whether this is worth the complexity, or whether smaller chunk granularity (more, smaller chunks from triage) is a simpler lever to pull instead.

5. **Pre-flight validation before spending LLM calls.** Many failures (scope violations, stale `old_string`, hallucinated file paths) are *detectable from data Pipewright already has* before or immediately after each LLM call, without needing another LLM call. Propose a "pre-flight" layer that runs cheap deterministic checks between every stage transition (triage→planner, planner→coder, coder→apply) and either blocks early with a clear message or triggers a same-stage correction loop — so failures happen *before* the next expensive LLM call, not after.

6. **Run-level health signal.** Propose a simple, visible "confidence" or "health" indicator computed from: chunk complexity vs. actual file sizes, number of auto-retries so far, reviewer verdicts so far, memory injection completeness. This isn't a hard gate — it's a UX signal so the user isn't surprised by a late-stage failure on a run that was "quietly struggling" the whole time.

### What "good" looks like (target experience)

A new open-source contributor runs a small feature request. If it succeeds: clean, fast, one approval click. If it hits a recoverable hiccup: they see "Pipewright noticed the file changed and re-read it — continuing" with no action needed. If it genuinely fails: they see what was tried, why it didn't work, and a specific next step ("This file has grown to 1800 lines — try splitting your request into two smaller features, or manually edit this file before re-running").

---

## Part B: Memory System Redesign — "From a fact list to something that actually helps"

### Current reality

`memory_facts` is a flat table: `category`, `scope`, `priority`, `content` (≤400 chars), `status`. Retrieval (`prompt_builder.py`) is:

1. Filter by role-allowed categories
2. Sort by `(category_rank, scope_rank, priority, created_at)`
3. Greedily pack into a token budget

This is **categorical + recency + manual priority** — there is no semantic relevance to the *current request*. A fact about "the auth module uses JWT with RS256" is equally likely to be injected into a prompt about CSS styling as one about login flow, as long as both are tagged `category=stack, scope=backend` and fit the budget.

### The SQLite vector idea

You mentioned wanting to explore `sqlite-vec` (or similar embedding-based retrieval) for memory. This is the right instinct — the question is *where* it fits relative to the existing categorical system, not whether to replace it.

### Open design questions for Fable 5

1. **Hybrid retrieval design.** Propose how categorical filtering (security/forbidden_paths must ALWAYS be included regardless of relevance — these are safety facts, not "context") and semantic retrieval (stack/structure/test facts ranked by relevance to the current `feature_description`) should combine. A reasonable starting hypothesis: safety-critical categories stay rule-based and mandatory; everything else becomes a vector similarity search over `feature_description` + `chunk.description` against fact embeddings, then the existing token-budget packer applies to the *ranked* result instead of the *category-sorted* result.

2. **Embedding generation and storage.** Where do embeddings get computed — at fact insertion time (one embedding call per approved fact/suggestion, infrequent) — and stored as a BLOB column or a `sqlite-vec` virtual table alongside `memory_facts`? What embedding model/provider, and what's the fallback if that provider is unavailable (the categorical system should never be *blocked* by an embedding service being down — propose a degrade-to-categorical-only path)?

3. **Run history as memory, not just curated facts.** Right now memory is only human-approved facts + suggestions. But the *injection_analysis* and *run_outcome_suggestions* modules already produce rich structured data about what happened in past runs (which files commonly fail, which patterns of feature request map to which chunk strategies that worked). Propose whether/how past run outcomes (not just curated facts) should become a separate, lower-trust retrieval layer — e.g. "3 past runs touching `backend/pipeline/coder.py` hit `STALE_INDEX_OR_FILE_CHANGED` — consider reading this file fresh" — surfaced to the planner/coder as *advisory* context, distinct from the trusted fact layer.

4. **Memory quality over time (ties to M5/M6 in the architecture review).** With semantic retrieval, near-duplicate or contradictory facts become more dangerous (two similar facts could both score high and both get injected, doubling token cost and potentially conflicting). Propose how embedding similarity could *also* power the duplicate/contradiction detection in `injection_analysis.py` — i.e., the same vector infrastructure serves both retrieval AND quality control.

5. **Schema migration path.** `memory_facts` is accessed via raw SQL across many modules (`memory_store.py`, `prompt_builder.py`, `bootstrap.py`, `run_outcome_suggestions.py`, `injection_analysis.py`). Propose a migration strategy that introduces vector columns/tables without a flag-day rewrite — e.g. embeddings populated lazily/in background for existing facts, with retrieval falling back to categorical for facts that don't yet have an embedding.

6. **Will this actually fix the "memory is not good" feeling?** Be honest in the proposal about what semantic retrieval *won't* fix. If the underlying problem is that bootstrap-generated facts are low-quality or run-outcome suggestions are noisy (M5, M7 from the architecture review), better retrieval over bad facts is still bad context. Recommend whether vector retrieval should be sequenced *after* suggestion-quality improvements, or whether it's independent and can proceed in parallel.

   **Maintainer's steer (do not treat as neutral):** the current hypothesis is that the "memory is not good" feeling is driven more by *fact quality* (low-signal bootstrap facts, noisy run-outcome suggestions — M5/M7 from the architecture review) than by retrieval *mechanism*. Vector search over low-quality facts is still bad context, and it's the larger, riskier build. Unless your code reading contradicts this, default to recommending **fact-quality improvements first, vector retrieval second**, and justify explicitly if you recommend otherwise.

### What "good" looks like (target experience)

When Pipewright plans a chunk, the memory injected into the prompt feels like it was hand-picked by someone who knows this specific codebase and has watched it being worked on for months — not like a generic "here are some facts tagged 'backend'" dump. A returning user notices Pipewright "remembers" not just static facts about the stack, but patterns from how previous runs on this repo went.

---

## Deliverable Format Requested From Fable 5

For both Part A and Part B, produce:

1. **Problem framing** — in your own words, confirm or refine the framing above based on reading the actual code (don't take this brief's framing as gospel — verify against `chunked_orchestrator.py`, `memory_store.py`, `prompt_builder.py`, `injection_analysis.py`, `run_outcome_suggestions.py`, `bootstrap.py`).
2. **2-3 candidate architectures per part**, with trade-offs (complexity, latency/cost impact, migration risk, how it interacts with the existing safety rules in `CLAUDE.md`).
3. **A recommended architecture per part**, with reasoning.
4. **Sequencing recommendation**: given `ARCHITECTURE_REVIEW.md`'s existing priority list, where does this redesign work fit — before, after, or interleaved with those bug fixes?
5. **Explicitly flag anything that would require relaxing a safety rule from `CLAUDE.md`** (e.g. auto-retry policies must not bypass approval gates; partial-chunk commits must not weaken the no-empty-commit guard). If a proposed design requires a safety rule change, call it out as a separate decision point rather than assuming it.

This is a design exercise. No code changes. The output should be a markdown document Pipewright's maintainer can review and decide which direction to commit to before any implementation PRs are scoped.

---

## Appendix: Real Failed Run (Evidence for Part A)

A verbatim example of the failure experience Part A must fix. Ground the design against this, not a hypothetical.

- **Run:** `c459bcde` — Chunk 1 of 1. Complexity `easy`, risk `low`, human review not required.
- **Request:** "create a small helper fun for the calculator"
- **Files expected:** `src/app.py`, `tests/test_app.py`
- **Outcome:** `TEST_FAILURE_AFTER_APPLY`. The change applied cleanly; the project test command exited non-zero `[1]`; Pipewright rolled the patch back; nothing was committed; chunk marked `failed`.
- **Test evidence:** classified *unverified* — "Test command exited non-zero [1]; the failure path owns this, so it cannot be treated as strong validation."
- **Dead-end state shown to the user** (every next action disabled):
  - "Retry code change — This kind of failure cannot be retried automatically."
  - "Approve chunk — The chunk is failed."
  - "Approve final — The change was rolled back after tests failed, so there is nothing to approve."

**What the user needed and could not do:** understand *why* the test failed (was it the generated test itself, or a pre-existing/unrelated suite failure?), and continue from here with a one-line steer instead of starting over. A brand-new low-risk helper should not be a terminal dead-end.

**The hard question Part A must answer:** was the full rollback even the right call here? For a low-risk chunk, should a test failure offer *"keep the applied change, let me fix the test"* rather than discard everything? (This must still respect the no-empty-commit guard and approval gates — see Part A Q1 constraints.)

> The precise root cause of THIS run (generated-test bug vs. whole-suite test command vs. scoped test command) was not separately diagnosed. The design must not depend on which it was — it should handle all three gracefully. A follow-up can capture the actual test output if Fable 5 needs it.
