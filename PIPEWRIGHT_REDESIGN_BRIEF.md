# Pipewright — Consolidated Issues, Bugs & Gaps + Redesign Mandate

**Date:** 2026-06-10
**Audience:** Claude Fable 5 (via Claude Code)
**Mode:** DESIGN-FIRST. Produce a written redesign proposal. Do **not** implement code from this document.
**Status:** This file is the single source of truth. It consolidates and **supersedes** `ARCHITECTURE_REVIEW.md` and `FABLE5_DESIGN_BRIEF.md` — everything actionable from both is folded in here, with accuracy corrections applied after a fresh code read on 2026-06-10. You do not need to read the other two files.

---

## 0. How to use this document

This is **not** a bug-fix checklist. The two prior reviews proposed fixes *inside the current architecture*. The conclusion after deeper exploration is that several of the felt problems are **architectural**, not bug-level — they cannot be fixed well without rethinking the execution model and the memory model.

So you are explicitly authorized to **redesign**, not just patch:

- You may propose a **new pipeline execution model** (state machine, conversational run, partial-commit, etc.). You are **not** bound to the current `triage → planner → coder → scope_guard → patch_apply → test → reviewer` chain or the current ~20-state status model.
- You may propose a **new memory model** (retrieval, storage, lifecycle). You are **not** bound to the flat `memory_facts` table or categorical retrieval.
- Pick the **best** design, then justify it. Where the current design is already good, keep it and say why.

**The one thing you may not redesign: the safety contract in §2.** Those invariants survive any architecture. If a proposed design would weaken one, call it out as an explicit, separate decision point — never assume it.

**Guiding principle — quality first.** Output quality (correct triage, plan, code, review, and *relevant* memory) is the primary objective. **Latency and token cost are secondary and must never be optimized at the expense of quality.** Do not adopt any fixed token budget, stage-skipping, caching, truncation, or retry shortcut that degrades the result. Where cost can be cut with **no** quality loss (e.g. prompt-caching an identical system prompt, skipping a provably redundant call), do it; otherwise quality wins. "Cheaper but worse" is not an acceptable trade for this product. In particular: a fixed cap that silently drops relevant — or *safety* — context to save tokens (see §8b, M10) is a quality/safety regression, not an optimization.

**Deliverable:** a markdown design proposal — problem framing (verified against the code yourself), 2–3 candidate architectures per area with trade-offs, a recommended architecture, a sequencing plan, and an explicit list of any safety-contract tensions. No code.

---

## 1. What Pipewright is (grounding)

Pipewright is an **AI engineering workflow tool with memory for existing codebases.** A user request → intent classification → chunk plan → **human approval** → scoped code changes → patch apply → verification (tests) → advisory review → local commit → optional GitHub PR. The product promise is **safety**: never edit outside approved scope, never commit empty, never push/PR without the correct approval state, never auto-merge, prefer safe failure over risky automation.

Current happy path (per chunk):

```
POST /runs/chunked
  → triage.py        [LLM]   → chunk plan + files_expected   (run-level, once)
  → human approves plan
  → chunked_orchestrator.py   (per chunk, sequential):
       planner.py    [LLM]    → PlannerHandoff
       coder.py      [LLM]    → CoderHandoff (create/edit/modify/delete)
       scope_guard            → assert changed files ⊆ files_expected
       patch_applier          → apply to disk (guarded)
       tester.py              → run project test_command (subprocess)
       reviewer.py   [LLM]    → advisory review (display-only, never gates)
       commit / or pause for chunk approval
  → final approval gate [human]
  → PR creation (optional, never auto-merge)
```

Key modules: `backend/pipeline/{chunked_orchestrator,triage,planner,coder,patch_applier,scope_guard,tester,reviewer,test_command_quality,file_scope_intent}.py`; memory `backend/memory/{memory_store,prompt_builder,bootstrap,injection_store,injection_analysis,run_outcome_suggestions,memory_trust}.py`; LLM `backend/llm/{role_config,registry,base}.py`; projects `backend/projects/{project_store,project_context}.py`; routes `backend/routes/chunks.py`.

---

## 2. Non-negotiable safety contract (survives any redesign)

These are enforced in code today and must hold in any new design. (Source of truth: the code itself — `scope_guard.py`, `approval_gate.py`, `patch_applier.py`, `path_safety.py` — and the memory safety contract in `docs/design/sqlite-vector-memory-readiness.md` §5.)

1. No implementation work without an approved chunk plan. Chunk-plan approval and final approval gates must not be bypassable.
2. No edits outside approved `files_expected` scope. `scope_guard` is the authority; memory/planner/coder prose may **request** scope, never **grant** it.
3. No empty / no-effective-change commits. No pushing zero-commit branches.
4. No PRs against `main`/`master`/`develop`; default PR base is the staging branch; never auto-merge.
5. Forbidden paths (`.env`, `.git`, secrets, keys) are never written.
6. Secrets/tokens/PII never returned in API responses, stored in memory, or embedded.
7. Memory is **advisory**: current code + explicit user instruction + safety rules always beat memory. Memory must never become an authority channel for scope, approval, Git, provider, or merge.
8. AI-suggested memory stays pending until human approval. Rejected suggestions don't silently return.
9. Prefer failing safely with a clear error over guessing.

Any auto-retry / continuation / partial-commit / auto-detect feature you design must remain inside these lines.

---

## 3. Product-level gaps (the *felt* problems)

These are what the user actually experiences. The numbered engine/memory issues in §4–§8 are the causes.

- **G1 — Single-message runs; no conversational continuation.** A run is bound to one immutable `feature_description` (`chunks.py:1026-1051`). There is no turn/conversation model and no way to add a message to a live or failed run. Unlike Claude/Cursor/Codex, the user cannot say "the test it wrote is wrong, fix the test" and have the run continue — they must start over. The only existing follow-up is a narrow signed-context reply for *ambiguous-file clarification* (`chunks.py:582-607`), not general iteration. **This is the #1 pain and is architectural.**
- **G2 — Fails on small, low-risk tasks.** A "create a small helper" request rolls back and dead-ends (see §9 evidence). Root causes are mostly infrastructure/heuristics, not bad code.
- **G3 — Failures aren't understandable and dead-end the run.** On failure the user gets a status string + an enum (`TEST_FAILURE_AFTER_APPLY`) and disabled buttons ("cannot retry"). No plain-language "what happened / why / what's next."
- **G4 — User must hand-type the test command.** Required field, no detection, no default (`project_store.py:49`). Most users don't know it; the tools they compare against auto-detect.
- **G5 — Memory doesn't feel smart.** Retrieval is categorical, not relevant to the request; it never self-heals; quality of auto-generated facts is unscored.
- **G6 — Latency and token cost are high for small work.** 4 LLM calls per trivial task, no prompt caching, 60s retry stalls.

---

## 4. Execution engine issues (verified, with accuracy corrections)

Severity legend: **HIGH** = breaks correctness/UX on normal use · **MED** = real but narrower · **LOW** = maintainability/opinion. "Mitigation" notes where existing code already softens the original review's claim.

### E1 — A scoped change is gated on an *unscoped* full-suite test run **(HIGH — primary cause of G2)**
`tester.py:141` runs the project's whole `test_command` (e.g. `pytest`, `npm test`). When the coder adds one helper + one test, the **entire** suite runs. Any pre-existing, unrelated, or flaky red test → non-zero exit → rollback of the *correct* change. Pipewright never scopes verification to the changed files. **Design question:** should verification be scoped (run only impacted tests) with the full suite as a separate gate? How does that interact with the "strong validation" guarantee?

### E2 — "Test process crashed" is indistinguishable from "tests failed" **(HIGH — cause of intermittent G2)**
`tester.py:164` is the *entire* verdict: `passed = completed.returncode == 0`. A non-zero exit from a crashed interpreter (observed: `Fatal Python error: init_sys_streams: can't initialize sys standard streams` + `KeyboardInterrupt`) is treated as a real test failure → rollback + dead-end. The subprocess is launched with no `stdin` (`tester.py:150`, `shell=True, capture_output=True`), the likely cause of that Windows crash. The runtime classifier already labels this "unverified" but that signal is display-only and doesn't change the rollback. **Design question:** distinguish *harness/infra error* (collection error, 0 tests, interpreter crash, timeout) from *assertion failure*, and make the former auto-retryable, not terminal.

### E3 — Four LLM calls for a one-line change **(MED — cause of G6)**
A single trivial chunk runs triage + planner + coder + reviewer (`chunked_orchestrator.py:1396-1486`); the reviewer runs on *every* success. Triage already chose the files, the planner re-derives a file list, the coder reads the files again — **three stages reason about the same file set.** For small tasks the planner adds little. **Design question:** collapse/skip stages by task size; is planner separable from coder for trivial chunks?

### E4 — Retry/backoff can silently multiply cost and stall **(MED)**
Planner/coder retry once on parse failure *and* `asyncio.sleep(60)` + retry once on rate limit (`planner.py:224`, `coder.py:486`). Correction: `asyncio.sleep` does **not** block the event loop (the prior review said it did) — but the coroutine holds the **project repo lock** for the full 60s, so other runs on the same project do stall. No exponential backoff, no `Retry-After` parsing. **Design:** centralized retry with backoff + jitter + `Retry-After`, bounded total attempts.

### E5 — `_execute_single_chunk` is a ~147-line monolith **(LOW — maintainability)**
`chunked_orchestrator.py:1346-1492` does dependency check → dirty-tree → plan → code → scope → apply → test → review → commit/pause in one function, with a near-duplicate human-retry path. A stage machine would make stages testable, resumable, and extensible. (Original review P1.)

### E6 — Git+DB divergence / double commit **(LOW — largely already mitigated; original review overstated this)**
The prior review (P2) claimed a commit-then-DB-fail race causes duplicate commits. **Correction:** the resume path already prevents this — `_resume_chunked_pipeline_locked` uses the `test` checkpoint plus `_verify_completed_checkpoint_safe` (which checks `commit_message_exists` + `completion_summary`) before re-running a chunk (`chunked_orchestrator.py:1662-1696`). Treat as defense-in-depth, not a confirmed bug. An outbox/saga is optional, not urgent.

### E7 — Triage file paths aren't validated against the index **(MED — wasted calls, fails safe)**
`triage.py` relies on prompt rules (A–D) to keep `files_expected` grounded but never programmatically validates the returned paths against the repo index. A hallucinated path is only caught later at `scope_guard`/apply — after planner+coder already ran. Fails safe (no scope violation), but wastes calls. (Original review P3.)

### E8 — Planner is unaware of `files_expected`; `old_string` validity checked only at apply **(MED — wasted calls, fails safe)**
`run_planner` never receives `files_expected` (`chunked_orchestrator.py:1396`), so it can plan out-of-scope files caught only at `scope_guard`. The coder's `action="edit"` `old_string` is validated only at `apply_patch`, so a stale `old_string` burns a full planner+coder round-trip before failing. Both are cheap deterministic pre-flight checks today. (Original review P4+P5.)

### E9 — Over-aggressive scope heuristics turn trivial tasks into high-risk dead-ends **(MED — cause of G2/G3)**
`file_scope_intent.py` is deterministic constraint parsing. On a bulleted request ("Only modify:\n- src/app.py\n- tests/test_app.py") the `- ` bullet markers break file collection (`_collect_files`, lines 187-206), so the user's explicit allowlist isn't recognized; files fall to "uncertain," the plan-consistency check (lines 414-426) emits a confusing `[SCOPE] ... not in files_expected: tests/test_app.py` note **for a file that is in scope**, and `harden=True` (line 426) bumps a two-function helper to **risk=high, requires_human_review=true.** **Design question:** does deterministic prose parsing earn its false-positive cost, or should scope intent be derived differently?

### E10 — Reviewer is advisory-only, so wrong output can "succeed" **(MED — quality/UX)**
The reviewer never gates (`reviewer.py` docstring; `_run_advisory_review_safe` swallows everything). Observed: a README run reached "Ready to push" with the *wrong sentence*; the reviewer flagged a high-severity `requirement_mismatch` but it didn't block. So "it worked" can ship content that doesn't meet the request. Diff cap keeps the head (`reviewer.py:58,108-114`) — debatable but low-impact since it gates nothing. **Design question:** should some reviewer findings (e.g. requirement_mismatch on a low-risk chunk) become a soft gate or a continuation trigger rather than silent advice?

### E11 — Status model is large **(LOW)**
~20 status strings across `RunStatus`/`ChunkStatusValue`/`ChunkPlanStatus`/`ApprovalStatus`/`GateStatus` (`core/statuses.py`). A redesign should consider whether a smaller, user-facing state set + a structured "what happened / what's next" narrative replaces raw enum exposure (ties to G3).

---

## 5. The conversational-continuation gap (G1) — design this as a first-class capability

This is the headline. Today: one run = one immutable `feature_description`; failure = rollback + dead-end + "start over." Design a **continuation model** where, after a failure (or even a success the user wants to refine), the user supplies a short natural-language steer and Pipewright re-attempts **carrying forward** the prior plan/code/diff and the failure evidence — without re-triaging from zero and without discarding what was already correct.

Constraints (from §2): continuation stays within the chunk's already-approved `files_expected`; it must not bypass approval gates; it must not commit a no-effective-change patch; user steers are advisory text, never authority to touch forbidden paths or skip tests.

Open questions: Is the right primitive a *conversation over a run* (turns appended to a durable run) or *cheap re-runs that inherit context*? How many continuation rounds before terminal (token ceiling)? How is prior attempt history surfaced so the user steers with full context? Where does this live relative to the existing human-retry machinery (`_persist_retry_patch_failure`, the `#26D` retry path) and checkpoints? (Relevant prior art: `docs/architecture/durable-agent-runtime.md`.)

---

## 6. Memory issues (verified, with accuracy corrections)

### M1 — Retrieval has no relevance to the request **(HIGH — primary cause of G5)**
`prompt_builder.py:353-377` ranks by `(category, scope, priority, created_at)` then greedily packs a token budget. There is **no semantic match** to `feature_description` — a JWT/auth fact is as likely to be injected into a CSS task as a login task. This is the core of "memory doesn't feel smart." (Vector/FTS/hybrid retrieval is the candidate fix; see §10.)

### M2 — Memory never self-heals; it only advises, and only on demand **(MED)**
`injection_analysis.py` is pure/read-only and explicitly never mutates (lines 12-21). It finds duplicate/supersession/reality-mismatch **candidates**, but only when a caller invokes it, and a human must act. It is not run automatically after a run. Contradictions accumulate silently. Reality checks only fire when the caller pre-computes a `repo_signals` map (lines 290-298) — today essentially only the DB-engine dimension.

### M3 — Provenance capture: original "fire-and-forget" claim was wrong **(LOW)**
Correction: `capture_memory_injection` (`injection_store.py:267`) is a **synchronous** function called directly in triage/planner/coder — not `asyncio.create_task` as the prior review (M3) claimed — and it already swallows + logs failures with sanitization. The real (minor) gap is no `failed_injections` audit table, not silent loss. Low priority.

### M4 — Greedy budget packing can crowd out lower-ranked categories **(LOW — milder than claimed)**
`prompt_builder.py:364-377` uses `continue`, not `break`, so smaller later facts still fit, and dropped facts are tracked in `budget_excluded_entries` (so it's not silent). Security/forbidden sort first and are protected. A category-budget allocation would help but this is not urgent. **Note:** M4 is only about packing *order*. The bigger, HIGH-severity concern is the fixed cap + crude token estimator that does the dropping in the first place — see **M10**. Treat M10 as the load-bearing budget issue; M4 is a refinement on top of it.

### M5 — LLM-suggested facts have no quality gate **(MED — cause of review-queue noise)**
`run_outcome_suggestions.py` accepts coder/planner `suggested_memory_entries` through content-safety + dedup but **no quality scoring**, so low-signal facts ("uses Python") become pending items a human must triage. Over many runs the queue fills with noise. **This likely matters more to the "memory is bad" feeling than retrieval mechanism does — better retrieval over low-quality facts is still bad context.**

### M6 — No staleness TTL **(LOW)**
Facts are `active` indefinitely; only manual `mark_fact_stale` (`memory_store.py:674`) or on-demand analysis removes them. No `run_bounded`/`ttl_days` policy. `last_verified_at` exists but isn't enforced.

### M7 — Detection is a hardcoded `if/elif` substring chain **(LOW — extensibility)**
`bootstrap.py:200-452` is `if "fastapi" in content` / `if "django" in content` …, plus a hardcoded self-detect at line 590. New frameworks require code edits. Detection-rules-as-data would make it extensible — and feeds §7.

### M8 — Reviewer/summary roles have memory budgets defined but unused **(LOW)**
`prompt_builder.py:42-130` defines `ROLE_TOKEN_BUDGETS`/`ROLE_CATEGORIES` for `reviewer` and `summary`, but `reviewer.py` never injects a memory block. Quick wire-up if a redesign keeps role-based memory.

### M9 — Memory is recomputed and re-persisted every stage **(LOW — minor cost)**
`build_project_memory_block_detailed` + `capture_memory_injection` run separately in triage, planner, and coder (3× read + 3× provenance write per chunk).

### M10 — Fixed token budget + crude estimator can evict relevant or *safety* facts **(HIGH — quality/safety; ties to §8b)**
Injection is bounded by hardcoded per-role budgets (`prompt_builder.py:42-49`) measured with a `(len+3)//4` heuristic, not a real tokenizer. On a tight budget, relevant facts — and even `security`/`forbidden_paths` facts — can be silently dropped (`docs/design/memory-injection-discipline.md` §3, §6). **The redesign must make memory quality-first: inject the *best, most relevant, minimal* set — never a long dump, never padding to fill a budget, and never evicting a safety fact to save tokens.** Budget should be adaptive to the model's real context window, not a fixed cap that trades quality for tokens. The goal is *the right few facts*, not *as many facts as fit*.

**Build on existing thinking — read these before redesigning memory:**
- `docs/architecture/memory-architecture.md` — the original three-layer model (Run / Project-State / Semantic), the M1→M2→M4 roadmap, the adversarial failure matrix (§5), and the candid "the cap will be hit; safety facts can be crowded out" notes (§15).
- `docs/design/memory-injection-discipline.md` — the as-built injection path, the "surface before suppress / system detects, human decides" discipline, and the explicit risk that budget-dropped safety facts are silently lost (§3, §6).
- `docs/design/memory-m3-trust-lifecycle.md` — the trust/staleness/provenance lifecycle (the current meaning of "M3").
- `docs/design/sqlite-vector-memory-readiness.md` (#32G) — memory modes (SQLite quickstart → SQLite FTS/vector → pgvector hosted), a derived `memory_embeddings` index that is never the source of truth, a retriever-interface migration path, and a hard retrieval safety contract (§5).

Adopt the §5 retrieval safety contract verbatim, build on (don't reinvent) the layered model and the future M2/M4 scope, and decide where the redesign lands on the mode ladder. **The north star: memory that injects the smallest set of genuinely relevant, high-quality facts for *this* request — quality over quantity, relevance over recency, never a long unwanted dump.**

---

## 7. Test-command auto-detection (G4) — the capability already exists, it's just unwired

**Current state:** `test_command` is a **required, hand-typed** field (`project_store.py:49-50`). The New Project "detect" endpoint (`routes/projects.py:64` → `repo_inspect.detect_repo`) only detects git/GitHub/`pr_mode` — **not** language, framework, or test command. At run time, missing command = `RuntimeError` (`project_context.py:55`).

**The opportunity:** Pipewright already contains the detection knowledge, twice, both **deterministic and zero-token**:
- `bootstrap.py:242-452` already detects pytest / jest / vitest / npm test / JUnit / go / cargo from manifests — but emits *memory suggestions*, not a `test_command`.
- `test_command_quality.py:52-125` already encodes the canonical command forms for every major runner — but only to *warn*, not to *suggest*.

**Design question:** a deterministic resolver that reads manifests → proposes a `test_command` → **prefills** the field for one-click confirm. **Safety:** detect-and-*prefill* (human confirms) is safe; detect-and-*silently-execute* is not — an auto-detected command is still "run an arbitrary repo string." A detected *weak* command (build-only, version probe) must not count as strong validation (`test_command_quality` already distinguishes this). **Cost:** unlike semantic memory, this adds **zero latency and zero tokens** — likely the highest-impact, cheapest win in this entire document.

---

## 8. Latency & token cost (G6)

- **No prompt caching anywhere.** Confirmed: zero `cache`/`cache_control` usage in `backend/llm/*` and the providers (Anthropic/Gemini/OpenAI/DeepSeek). Each stage re-sends its full system prompt + memory block uncached on every call. Anthropic prompt caching and Gemini context caching are both available and unused — a direct cost lever for the repeated system prompts and per-run memory blocks.
- **4 calls per trivial task** (E3) and **60s lock-holding retry stalls** (E4).
- **Per-stage memory recompute** (M9).
- **Whole-suite test runs** (E1) also cost wall-clock time on every chunk.

**Design question:** where does caching fit (provider-level prompt cache vs. application-level), and what's the cost model the redesign should target for a "small task" (ideal: 1–2 calls, cached system prompt, scoped test run)?

---

## 8b. Unreliable / hardcoded values & fixed budgets **(quality risk — you flagged this)**

A recurring theme across the engine and memory: behavior is governed by **scattered hardcoded magic numbers**, several of which are unreliable, misleading, or actively harmful to output quality. None are per-project configurable or adaptive. A redesign should treat these as **policy, not constants.**

- **Dead/misleading per-stage model constants.** `triage.py:28` (`TRIAGE_MODEL`), `planner.py:32` (`PLANNER_MODEL`), `coder.py:37` (`CODER_MODEL`) all hardcode `"gemini-2.5-flash-lite"` and pass it as `LLMRequest.model` — but `complete_for_role` overrides it (`backend/llm/__init__.py:76`: `request.model_copy(update={"model": <role-config model>})`). The constants are **ignored**; the real model comes from role config/env (your runs used DeepSeek). They are dead code that lies about which model runs. **One source of truth for model selection, please.**

- **Fixed token budgets as hard caps that silently drop facts.** `prompt_builder.py:42-49` hardcodes `triage=400, planner=1200, coder=1200, reviewer=800, summary=800` (default 1500), enforced as **hard ceilings** — facts that don't fit are dropped (`prompt_builder.py:370-374`). The memory architecture doc itself admits "the 1500-token cap will be hit faster than expected" (`docs/architecture/memory-architecture.md` §15), and the injection-discipline doc flags that **even `security`/`forbidden_paths` facts can be budget-dropped** on a tight budget (`docs/design/memory-injection-discipline.md` §3, §6). A fixed budget that evicts relevant — or safety — context to save tokens is a direct quality/safety risk, not an optimization.

- **The token "estimate" is not a tokenizer.** Budgeting uses `(len+3)//4` (`prompt_builder.py:133-134`), a crude char/4 heuristic, not the model's real tokenizer — so the budget it enforces is itself unreliable and can drop the wrong facts.

- **Other scattered magic numbers**, none configurable/adaptive: `REVIEWER_MAX_DIFF_CHARS=6000` (`reviewer.py:58`), `MAX_FILE_LINES=200` / `LARGE_FILE_CONTEXT_LINE_CAP=1500` (`coder.py:41,46`), `TESTER_TIMEOUT_SECONDS=300` / `MAX_OUTPUT_CHARS=10000` (`tester.py:16-17`), the `asyncio.sleep(60)` retry (`planner.py:225`, `coder.py:486`), per-stage `temperature`/`max_output_tokens`, the 400-char memory content cap, the closed 11-category / 5-scope memory enums, and the future similarity threshold `0.72` (`memory-architecture.md` §4.3).

- **Brittle string parsing presented as fact.** Test pass/fail counts are scraped by regex (`tester.py:80-101`: `\d+ passed` / `\d+ failed`) — fragile across runners and output formats. The secret-detection hex pattern is documented to false-positive on commit hashes (`memory-architecture.md` §5.1). Framework detection is hardcoded substring matching (`bootstrap.py:200-452`, the M7 if/elif chain).

**Design direction:** caps that can evict relevant or safety context must be **adaptive to the model's real context window** and must never silently drop a safety fact; budgeting should use the real tokenizer (or be generous enough that quality is never the casualty); model/temperature/timeout/threshold values should be per-project/per-model policy with sane defaults, not buried constants. **This is a direct expression of the quality-first principle in §0.**

---

## 9. Appendix: real failed runs (evidence — design against these, not hypotheticals)

**Run `c459bcde`** — "create a small helper fun for the calculator". Files `src/app.py`, `tests/test_app.py`. Outcome `TEST_FAILURE_AFTER_APPLY`: change applied, test command exited non-zero, rolled back, dead-ended ("cannot retry", "chunk is failed", "nothing to approve").

**Run `a584f251`** — `validate_number` + `add`, "Only modify: src/app.py, tests/test_app.py". First attempt: `SCOPE_VIOLATION` (tests not run). Second attempt: `TEST_FAILURE_AFTER_APPLY` with `Fatal Python error: init_sys_streams: can't initialize sys standard streams` / `KeyboardInterrupt` (interpreter crash, not an assertion failure — see E2). The chunk was hardened to **risk=high, human review required** by the scope heuristic (see E9) for a trivial helper. **The same request later succeeded, 12/12 tests passed** — proving the failures were infrastructure/heuristics (E1, E2, E9), not bad code.

**Run `415a7669`** — "Add a README section with the exact sentence '…demo smoke run.'". Reached "Ready to push" but the coder wrote a *different* sentence; the advisory reviewer flagged `requirement_mismatch` (high) but it didn't block (E10). "Worked" but shipped the wrong content.

Through-line: the failures the user feels are dominated by **E1 (unscoped test gate)**, **E2 (crash = fail)**, **E9 (scope over-firing)**, and the absence of **G1 (continuation)** — not by a broken core engine.

---

## 10. Redesign mandate for Fable 5

Produce a design proposal covering **two areas**. For each: (a) confirm/refine the framing by reading the code yourself; (b) give 2–3 candidate architectures with trade-offs (complexity, latency/cost, migration risk, interaction with §2); (c) recommend one with reasoning; (d) flag any §2 safety tension explicitly.

**Area A — Pipeline execution engine.** You may propose a fundamentally new execution model. Must address: G1 (conversational continuation), G2/G3 (small-task robustness + understandable, non-dead-end failures), E1 (scoped vs. full-suite verification), E2 (infra-failure ≠ test failure), E3/E4/G6 (cost: fewer calls, caching, bounded retries), E9 (scope-intent without false positives), E10 (advisory vs. soft-gating review), and a simpler user-facing state/narrative model (E11). E5–E8 are smaller and may be folded into whatever structure you choose.

**Area B — Memory system.** You may propose a new retrieval + storage + lifecycle model. Must address: M1 (relevance-aware retrieval), **M10 (quality-first injection — best/minimal/relevant set, adaptive budget, never drop a safety fact, never a long dump)**, M5 (suggestion quality — likely sequence this *before* vector retrieval; justify), M2/M6 (self-healing / staleness lifecycle), and adopt the safety contract from `docs/design/sqlite-vector-memory-readiness.md` §5. First read the four memory docs listed at the end of §6 and build on their layered model + M2/M4 future scope rather than reinventing them. M3/M4/M7/M8/M9 are lower priority; fold in as the design allows. **Be honest** about what semantic retrieval will *not* fix (better retrieval over low-quality facts is still bad context).

**Cross-cutting — quality-first & de-hardcoding (§0, §8b).** Both areas must honor the quality-first principle: do not trade output quality for latency/tokens. Explicitly address the §8b hardcoded/unreliable values — one source of truth for model selection, adaptive (not fixed) budgets measured with a real tokenizer, and configurable policy instead of buried magic numbers — wherever they affect quality.

**Cross-cutting quick win — Test-command auto-detection (§7).** Independent of both redesigns, zero-token. Recommend whether it ships first as a standalone improvement.

**Sequencing.** Recommend an order across: the cheap deterministic wins (test-command detection, E2 stdin/infra-classification, E9 scope parsing), the cost levers (prompt caching, fewer calls), the architectural items (G1 continuation, scoped verification, memory retrieval). Note that implementation must be **split into small, separately-tested PRs** — a combined "execution + memory rewrite" PR is exactly the kind this project forbids.

**Two-pass option.** This brief is intentionally large. If covering both areas in one pass would stretch your context or dilute depth, do it in two passes against this same brief: **Pass 1 = Area A (execution engine)**, **Pass 2 = Area B (memory)**, then a short combined section reconciling cross-cutting concerns (quality-first, §8b de-hardcoding, sequencing). Depth per area beats a shallow single pass — prefer two focused passes over one rushed one.

**Output:** a single markdown design document — write it as `PIPEWRIGHT_REDESIGN_PROPOSAL.md` — that a maintainer can review and approve before any implementation PR is scoped. (If you use the two-pass option, append Pass 2 and the reconciliation to the same file rather than creating a second file.) No code changes in this exercise.
