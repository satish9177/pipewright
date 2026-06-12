# Pipewright Redesign — Implementation Handoff Brief (rolling, per-phase)

**Date:** 2026-06-12
**What this file is:** the **rolling** implementation handoff brief for the redesign. Each phase's active spec lives here; when a slice lands, this file is repurposed for the next one. **Current contents: Phase 4, slice 3 — item 17 (trivial-task stage profile + provider prompt caching), the LAST Phase-4 slice and the last Area-A Pass-1 slice. 17a (trivial-task stage profile) is DONE — merged via PR #288 into `develop` (`221cc9c` / `24e24b8`); do NOT re-review or re-implement it (outcome recorded in `PIPEWRIGHT_REDESIGN_WORKPLAN.md`). The active remaining work in this brief is 17b (provider prompt caching) — see §17.4 (caching) + `PIPEWRIGHT_ITEM17_DESIGN.md` §7–8.** The previous occupant (item 16 — phase/narrative read-model extending `operator_state`) landed on `develop` (`c7bfbf4` / `d3deb65`), reviewed before push — do **not** re-review it. Its outcome is recorded in `PIPEWRIGHT_REDESIGN_WORKPLAN.md`.
**For:** the model implementing Phase 4 item **17b** (17a has landed). Re-verify every `file:line` against the live code before you cite or edit it; items 13–17a moved line numbers and the repo keeps moving.
**Source of record:** `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (§4.7 policy/retry/call-structure/caching, §5.6 the display-only/advisory non-tension, §6 phasing item 17, §18.2 the ledger `stage_profile` field, §18.3 the trivial-profile soak + decision D12). This brief operationalizes them; if they disagree, the proposal wins and you flag the drift.
**Mode:** **Fable designs, implements, and tests this slice** — one *purpose* per PR (see E0: this slice likely wants to be **two** PRs). This is a **design brief, not a prescribed mechanism:** it fixes the *what* (scope, the headline invariant, the decisions for the maintainer, the safety-contract check, the acceptance tests) and points at the real code so you don't re-discover the repo — but **you own the design (the *how*): the exact eligibility predicate, how the planner call is removed, where the profile branch attaches in the driver, and how cacheable segments are marked and translated per provider.** The acceptance tests are external criteria, not yours to relax. Since you also write the tests, **your design is reviewed by a human before you implement, and the PR(s) are reviewed.** This slice is **item 17 only.** Do not build any chat/thread history feed, the layered per-project policy framework, a metrics endpoint, or any Area-B (memory) work.

> **Status: 17a DONE (merged via PR #288); 17b NOT yet implemented.** Decisions E0–E5 were ratified 2026-06-12 and the as-built design lives in `PIPEWRIGHT_ITEM17_DESIGN.md`. **Item 17a (trivial-task stage profile) is merged — do not re-implement it.** The remaining work is **17b (provider prompt caching)**: implement per `PIPEWRIGHT_ITEM17_DESIGN.md` §7–8 (E4/E5 — a typed `Message.cache` marker, Anthropic `cache_control` active, OpenAI/DeepSeek passive, Gemini a flagged seam), behind `PROMPT_CACHE_ENABLED` (default off ⇒ byte-identical), with the §17.4 (caching) tests, then have it reviewed.

---

## 0. The headline invariant (read before anything else)

Item 17 is a **cost/latency optimization with ZERO quality loss and ZERO authority change.** It is the one slice whose entire justification is "cheaper, never worse" — and the project's first principle is that *"cheaper but worse" is not a trade this product accepts* (`CLAUDE.md`, Engineering principles). So the bar is not "it saved tokens"; it is "it saved tokens **and** an auditor cannot find a single quality or safety byte that moved." The single non-negotiable invariant:

> A run's **observable behavior** — which of triage/plan/code/review executes, every approval gate, `scope_guard`, baseline verification, reviewer independence, commit/rollback, and PR rules — is **identical with the optimization on or off**, with exactly two permitted deltas: (1) a **provably-trivial** chunk skips the **redundant planner LLM call** (the planner stage that demonstrably re-derives what triage already decided), and (2) **byte-identical** prompt context is billed once instead of twice. Both deltas are **reversible by a single policy flag with no code change** (`merged_profile_sample_pct → 0`; `prompt_cache_enabled → False`). Neither the profile decision nor a cache hit/miss is ever an **authority channel**: it cannot change scope, approval, which memory is injected, reviewer independence, or Git/merge behavior.

The two halves restate it:

- **Trivial-task stage profile (the call-structure half).** Removing the planner is permitted **only** inside a **deterministic eligibility envelope** where the planner provably adds nothing over triage (single chunk, `complexity=easy`, low risk, grounded non-empty `files_expected`). Outside the envelope the standard two-stage path is unchanged. **Triage is kept always** (it is the approval artifact). **The coder is kept always.** **The reviewer is kept always, including on trivial chunks** — E10's evidence is that review matters *most* exactly when everything else says green; cutting the quality stage to save a call is the trade the brief forbids (proposal §4.7). The eligibility predicate reads only the **already-approved** triage + chunk; it never edits scope, risk, or the approval requirement.

- **Provider prompt caching (the billing half).** Caching avoids paying twice for **byte-identical** context (static system prompts; later, per-run-stable repo file-lists). It must **never change a single byte** sent to the model, **never freeze or override per-request memory selection** (relevance beats cache — the memory block is recomputed per stage/context and is explicitly *not* force-cached; proposal §4.7 / M9), **never cross project or run boundaries**, and **never gate**. A cache hit and a cache miss must yield identical model output and identical downstream behavior; the response is always the model's, never the cache's authority.

Three structural guarantees your design must prove:

- **Reversibility is real.** With `merged_profile_sample_pct = 0` and `prompt_cache_enabled = False`, the system is byte-for-byte today's system. Prove it against a pre-change parity snapshot. The flags are the soak/rollback lever (§18.3 D12) — a config flip, never a code revert.
- **Eligibility is deterministic and total.** No LLM decides triviality. The predicate is a pure function over the approved triage + chunk, total and conservative: any doubt → `standard` profile. A bug that mis-classifies a *non*-trivial chunk as trivial silently drops the planner on work that needed it — so the predicate fails **toward `standard`**, never toward `merged`.
- **Caching cannot leak or pin.** The cacheable set is byte-stable, request-independent context only. The memory block — which varies by request and whose relevance is a safety property — is never marked cacheable. No cache handle is keyed in a way that could serve one project's/run's bytes to another.

This is a cost slice sitting on top of safety-critical execution and provider plumbing. The risks are **not** scope/commit/Git regressions (those paths are untouched). The real risks are (a) dropping the planner on a chunk that wasn't actually trivial, (b) the reviewer or a gate being silently skipped "because it's trivial," and (c) caching mutating bytes, pinning memory, or leaking context across boundaries.

---

## 17.0 Decisions for the maintainer (ratify BEFORE any code)

These are the substance of the design review. Bring a recommendation for each; the maintainer rules. **Code starts only after these are settled.**

- **E0 — One slice or two PRs?** The two halves have different blast radii: the profile touches the driver + a deterministic eligibility predicate + the ledger; caching touches the provider layer + policy flags only. The proposal itself sizes caching as "one provider-layer PR + per-provider flags in policy" (§4.7). **Recommendation: split into 17a (trivial profile) and 17b (prompt caching), reviewed and merged independently** — one clear purpose per PR (`CLAUDE.md` PR discipline). They share nothing but `policy.py`. *(If the maintainer prefers one PR, the §0 invariant and tests still apply per-half.)*

- **E1 — The eligibility predicate (profile).** Fix the exact deterministic criteria over the **approved** `TriageResult` + `ChunkDefinition`. Recommended set, all required:
  - `TriageResult.total_chunks == 1` (single chunk)
  - `TriageResult.complexity == "easy"`
  - `chunk.risk_level == "low"` **and** `chunk.requires_human_review is False`
  - `chunk.files_expected` non-empty **and grounded** — every path resolves to an existing repo file (so the coder is fully grounded; this is the "fully grounded `files_expected`" clause)
  - `chunk.depends_on == []` (trivially true for a single chunk, but assert it)
  - **The proposal's "no security/db flags" clause has NO backing field** on `ChunkDefinition` (verified: `backend/models/chunk.py:14-49` has no security/db flag). **Decide:** (a) drop it — `risk_level`/`requires_human_review` already force review on the dangerous cases; (b) add a **deterministic** path-pattern denylist that forces `standard` (e.g. paths matching migrations/auth/secrets/`schema.sql`), single-sourced in policy. **Recommendation: (a) + a conservative (b) denylist** — keep the safety margin without inventing an LLM signal. Whatever you choose, eligibility stays a pure, table-tested function; never an LLM call.

- **E2 — How the planner call is removed (profile).** Two shapes:
  - **(E2-synthesize, recommended — lowest risk):** for an eligible chunk, **synthesize the `PlannerHandoff` deterministically from triage** (goal/steps/files from the chunk title/description/`files_expected`), make **zero** new LLM call, and run the **coder unchanged (byte-identical prompt)**. Net: the planner's redundant LLM call → 0; nothing else moves. "Zero quality loss by construction" becomes literally true because the coder's input is unchanged. Constraint to honor: `PlannerHandoff` requires `steps ≥ 2` (`planner.py` schema) — the synthesis must produce a valid handoff; the synthesized plan-summary is still persisted for the audit trail.
  - **(E2-merge):** a single merged plan+code **LLM call** that emits plan-summary + coder handoff together (matches the proposal's literal "produces plan-summary + handoff in one call", §4.7). Pro: one call does both. Con: a **new coder-side prompt** → real quality risk, must be soaked, and "zero quality loss by construction" is weaker (the prompt changed). If you choose this, justify it and treat the merged prompt as the soak's subject.
  - **Recommendation: E2-synthesize.** It removes a redundant call without touching the prompt that actually writes code. Either way: the plan-summary stays in the persisted `completion_summary`/audit output (proposal §4.7 "the plan-summary remains in the output for the audit trail").

- **E3 — Sampling & cohort (profile / §18.3 soak).** Deterministic eligibility first, then a randomized cohort gate `merged_profile_sample_pct` (policy, **default 50** during soak per §18.3; eligible-but-unsampled chunks are the `standard` control). **Decide:** the default (50 recommended), and that the randomization is **stable per chunk** (seed from run_id+chunk_number) so a re-read/replay classifies identically for audit. The soak *analysis* (finding-rate comparison, the D12 rollback trigger) is **documented SQL over the ledger, not code** (§18.3 "Instrumentation is the ledger… No new telemetry framework").

- **E4 — Caching scope: segments & providers (caching).** Cache only **byte-stable, request-independent** segments: the role **system prompts** now; per-run-stable repo file-lists later (the engine exposes the hook; Pass-2/M9 fills it). **Never** the memory block (relevance over cache). Providers: **Anthropic `cache_control` (ephemeral)** is the clean first target. **Gemini explicit context caching is materially heavier** (a `CachedContent` create/delete lifecycle, minimum-token thresholds, TTLs) and the default provider is Gemini (`role_config.DEFAULT_MODEL = gemini-2.5-flash-lite`). **Recommendation: implement Anthropic caching; leave Gemini a flagged seam, default off** — and never wire a Gemini cache handle that could outlive or cross a run/project. Confirm.

- **E5 — The caching marker mechanism (caching).** How a cacheable segment is marked **provider-agnostically** at the `LLMRequest`/`Message` layer (a typed optional field vs. the existing `extras` dict), how providers translate it (Anthropic → a system content block with `cache_control`; others → graceful no-op identical to today), and the **per-provider policy flag(s)**. **Recommendation: a typed optional marker** (absent → today's exact string path, so every non-marked call and every non-caching provider is byte-identical) plus a `prompt_cache_enabled` policy flag (per-provider seam). Confirm the default flag state (recommend **on for Anthropic system prompts** — zero quality impact by definition; flips off with no code change).

---

## 17.1 Non-negotiable safety contract (from `CLAUDE.md`)

No change may weaken these. Item 17 framing in **bold**:

1. No implementation without an approved chunk plan; never bypass a gate. **Untouched — the profile only removes a *post-approval* execution-stage LLM call (the planner runs after triage approval). Triage, the approval artifact, is unchanged; the eligibility predicate reads the *approved* plan, it does not produce or alter one.**
2. Never edit outside approved `files_expected`; `scope_guard` is the authority. **Untouched — preflight (`assert_files_in_scope`) + dry-run + apply run identically in both profiles; the merged/synthesized path still feeds the same `files_expected` to the same `scope_guard`.**
3. Never create empty / no-effective-change commits. **Untouched — same `code_stage` NO_CHANGES guard, same `_commit_and_complete_chunk`.**
4. Never open PRs against `main`/`master`/`develop`; never auto-merge. **Untouched — no PR/merge path is involved.**
5. Never write forbidden paths. **Untouched — and E1's optional denylist *adds* a margin, never removes one.**
6. Never expose or persist secrets/tokens/PII; sanitize errors. **The new `stage_profile` ledger value is a closed enum (`standard`/`merged_plan_code`) — metadata only, same discipline as the rest of `chunk_attempts`. Caching adds NO new persisted data and must never log prompt bytes.**
7. Memory/reviewer findings/narratives are **advisory**; source code, user instruction, tests, and safety rules win on conflict. **The profile decision and caching are the strongest form of "advisory-not-authoritative": neither changes scope, approval, memory selection, or Git. Caching especially must never pin memory (relevance over cache, §4.7/M9).**
8. AI-suggested memory stays pending until a human approves. **Untouched — no memory write path.**
9. Prefer failing safely with a clear, specific error over guessing. **The eligibility predicate fails toward `standard` on any doubt (never drops the planner on ambiguity); a caching-translation error must fall back to the uncached byte-identical path, never to a wrong-bytes request.**

This slice is **low-risk** (cost optimization) but it sits on execution + provider plumbing, so carry the explicit safety-contract check (§17.6). The risk is *under-doing safety* ("it's trivial, skip the reviewer/gate") and *caching mutating or pinning context* — not mutation of the commit path.

## 17.2 The ground you're designing on (grounding, not a prescribed mechanism)

Where the pieces live, so you design against real code. **How you use them is your call** — the only fixed points are §0, §17.0's ratified decisions, §17.6, and the §17.4 tests.

- **`backend/pipeline/chunk_driver.py` — the profile attaches here.** `_drive_stages` (`:392`) runs, for `fresh`/`resume`, `plan_stage` (`:512`) then the `while True` apply/verify loop starting at `code_stage` (`:531`); human/steered modes skip planning via `orch._retry_plan_for_chunk` (`:505`). The profile branch is a `fresh`-mode decision *before* `plan_stage`: eligible → synthesize/merge (E2) instead of calling the planner. **Everything below `code_stage` — preflight, apply, verify+rollback, reviewer (`review_stage` `:711`), gates, commit — must stay on the exact same path.** The driver already records one `chunk_attempts` row per pass via `_record_attempt` (`:220`); thread the chosen `stage_profile` into that write.
- **`backend/pipeline/stage_contract.py` — the stage adapters.** `plan_stage` (`:254`) wraps `run_planner`; `code_stage` (`:275`) wraps `run_coder` and already carries the NO_CHANGES guard. If E2-synthesize, you likely produce a `PlannerHandoff` without `plan_stage` and feed `code_stage` unchanged; if E2-merge, you add a merged adapter. Keep the `StageOutcome` contract intact — it is what the ledger and the driver read.
- **`backend/models/chunk.py` — the eligibility inputs.** `TriageResult` (`:52`) carries `complexity: easy|medium|hard` and `total_chunks`; `ChunkDefinition` (`:14`) carries `risk_level`, `requires_human_review`, `files_expected`, `depends_on`, `token_estimate`. **There is no security/db flag (E1).** Confirm where the *approved* triage is loaded at execution time so the predicate can read `complexity`/`total_chunks` at the driver (it is run-level, not on the chunk row) — this is the one real plumbing question of the profile half; verify the load path before designing the thread-through.
- **`backend/pipeline/policy.py` — where the new knobs live.** Flat defaults, no per-project layer yet (`:11-14` says so explicitly). Add: the profile sampling pct, the E1 denylist (if chosen), and the caching flag(s) — each a single-sourced, commented constant with the same discipline as `AUTO_RETRY_INFRA_BUDGET`/`REVIEW_ACK_REQUIRED_*`. **Model selection stays in `role_config.py` (`:16-73`), NOT here** (`policy.py:16-18` is explicit) — caching does not touch model selection.
- **`backend/pipeline/chunk_attempt_store.py` + `backend/db/schema.sql` + `backend/db/database.py` — the ledger.** `record_chunk_attempt` (`chunk_attempt_store.py:32`) inserts the row; `chunk_attempts` is defined in `schema.sql:205` and shape-ensured idempotently by `_ensure_chunk_attempts_shape` (`database.py:537`). **`stage_profile` does not exist yet** (grep clean). Add it as an **additive, nullable** column in both places (mirror how item 12/13/14 added columns) plus the insert param; `NULL` for legacy rows and for passes where the concept doesn't apply. Metadata only.
- **`backend/llm/base.py` — the caching seam.** `LLMRequest` (`:16`) has `messages: list[Message]` (string content) + an `extras: dict` already present (`:23`); `Message` (`:11`) is `{role, content}`. The cacheable-segment marker (E5) attaches here. `LLMResponse` (`:26`) carries `input_tokens`/`output_tokens`/`raw` — a good place to surface cache hit/usage for the soak without changing behavior.
- **`backend/llm/providers/anthropic.py` — the first caching target.** `complete` (`:63`) and `_translate_messages` (`:92`) currently flatten all system messages into one **string** `kwargs["system"]` (`:80-81`). Anthropic `cache_control` requires the system block to be a **list of content blocks** with `{"type": "text", "text": ..., "cache_control": {"type": "ephemeral"}}`. The translation change is local and must be **a strict no-op when the marker/flag is absent** (still emits the identical string today). The other providers (`gemini.py`, `openai.py`, `deepseek.py`, `fake.py`) must ignore the marker gracefully (E4/E5) — `fake.py` especially, since the test fakes run through it.
- **`backend/pipeline/planner.py` / `coder.py` — the prompts + the call.** `planner.SYSTEM_PROMPT` (`:36`) and `coder.CODER_SYSTEM_PROMPT` (`coder.py:43`) are the byte-stable static segments caching targets. The memory block is built into the **user** prompt (`planner._build_user_prompt:66`) — it is request-varying and **not** a cache target. Both call the provider via `complete_for_role` through `call_with_rate_limit_retry` (`planner.py:130`). `run_planner` (`:149`) is the call the profile removes for eligible chunks; `PlannerHandoff` (the synthesis target for E2) must validate (`steps ≥ 2`).
- **The proposal's measurement design.** §18.2 fixes the ledger field (`stage_profile TEXT NULL` = `standard | merged_plan_code`); §18.3 fixes the cohort/soak/rollback (sample 50%; window ≥30 profiled chunks or 30 days; rollback trigger = profiled high-severity finding rate >5pp over control, or chunk-gate rejection rate doubles → set `merged_profile_sample_pct = 0`). Your code emits the ledger field; the analysis is SQL.

---

## 17.3 Summary & scope

**Item 17 = two cost optimizations behind policy, zero quality loss, zero new authority:**

1. **Trivial-task stage profile** — for chunks that pass a **deterministic** eligibility predicate (E1), skip the **redundant planner LLM call** (E2: synthesize the handoff from triage, recommended; or a merged plan+code call) and record `stage_profile = merged_plan_code` in the `chunk_attempts` ledger. Eligible-but-unsampled and all ineligible chunks run the unchanged two-stage path and record `standard`. **Triage, coder, reviewer, scope_guard, preflight, baseline verification, all gates, commit/rollback — unchanged.** Behind `merged_profile_sample_pct` (default 50 during soak); `0` ⇒ feature off, byte-identical to today.

2. **Provider prompt caching** — mark byte-stable system prompts (E4) as cacheable at the `LLMRequest` layer (E5) and translate to provider caching (Anthropic `cache_control` first; Gemini a flagged seam). **Identical bytes, zero quality impact by definition.** Behind `prompt_cache_enabled`; off ⇒ byte-identical to today. **Never caches the memory block; never crosses project/run boundaries; never gates.**

**Outcomes the design must deliver (the *what* is fixed; the *how* is yours to design and submit for review):**
- A **pure, total eligibility predicate** (E1) over the approved triage + chunk, failing toward `standard` on any doubt; pinned by a fixture table.
- The **planner LLM call removed** for eligible+sampled chunks (E2) with the coder path unchanged (E2-synthesize) or a reviewed merged prompt (E2-merge); the **plan-summary still persisted** for audit.
- The **reviewer still runs on every trivial chunk** (explicit test) and reviewer independence (#33C) is unchanged.
- **`stage_profile`** additively recorded in the ledger (`standard | merged_plan_code`), nullable, metadata-only.
- **Provider prompt caching** that is a strict no-op when off/unmarked, byte-identical bytes to the model when on, Anthropic-first, never caching the memory block.
- **Both halves flag-reversible** with no code change; proven against a pre-change parity snapshot.

**Explicitly out of scope (name these in your PR):**
- **Reducing or removing the reviewer** for any task, trivial or not (forbidden — the reviewer is the quality stage; proposal §4.7).
- **Removing or merging triage**, or touching triage chunking / the chunk-plan approval gate.
- **An LLM-based eligibility decision** — eligibility is deterministic.
- **The layered per-project policy-override framework** — `policy.py` stays flat defaults; only add the new constants (proposal §4.7 names the layered framework as a *later* phase).
- **Gemini explicit context caching** beyond a flagged, default-off seam (E4) — unless the maintainer rules otherwise.
- **The §18.3 soak automation** — the rollback trigger is operational (documented SQL + a config flip), not a code controller; no metrics endpoint, no telemetry framework.
- **Caching the memory block or any request-varying context**, and any cache handle keyed across runs/projects.
- **Changing model selection / `role_config`**, temperature, max-tokens, streaming, or token-counting semantics (caching reads usage; it does not change the model contract).
- **Renaming/migrating** any `RunStatus` / `ChunkStatusValue` / `OutcomeClass` / `PatchFailureType` / verdict string.
- **Any new route, endpoint, gate, approval, or authority channel.**
- **Any chat/thread/history feed** and any Area-B (memory) change.

## 17.4 Tests that must exist and pass

**Profile (17a):**
- **Eligibility predicate is pure & total:** a fixture table of (triage, chunk) → eligible/ineligible that pins **each** criterion boundary: multi-chunk → `standard`; `complexity` medium/hard → `standard`; `risk_level` medium/high → `standard`; `requires_human_review` true → `standard`; empty `files_expected` → `standard`; an ungrounded path → `standard`; non-empty `depends_on` → `standard`; (if E1(b)) a denylisted path → `standard`. The all-pass case → eligible. Assert the function **defaults to `standard`** for any unmapped/partial input.
- **§0 parity — feature off is byte-identical:** with `merged_profile_sample_pct = 0`, an eligible chunk runs the **standard** path; capture a pre-change golden/parity snapshot of the driver/orchestrator suites and prove no behavioral or `completion_summary` byte changed.
- **Profile taken removes exactly the planner call:** when eligible+sampled, assert `run_planner` is **not** invoked (call-count/spy), the **coder call is unchanged** (E2-synthesize: identical `LLMRequest`), and preflight/apply/verify/**review**/gates/commit all still run (the stage sequence is identical minus the planner LLM). Assert a **plan-summary artifact is still produced and persisted**.
- **Reviewer always runs on a trivial chunk** (the test you cannot ship without): an eligible+sampled chunk still reaches `review_stage`; the quality stage is never skipped.
- **Ledger:** `stage_profile == "merged_plan_code"` on a profiled pass, `"standard"` otherwise; column is additive/nullable; `_ensure_chunk_attempts_shape` is idempotent (run twice, no error, no dup). Legacy rows read `NULL` without breaking resume / `get_latest_completed_attempt_head`.
- **Sampling cohort:** eligible-but-unsampled → `standard` (control); eligible-and-sampled → `merged_plan_code`; classification is **stable** for a given run_id+chunk_number (re-read yields the same cohort).
- **No authority leak:** a profiled chunk does not change `files_expected`, `risk_level`, `requires_human_review`, or the approval requirement; the chunk/final gates fire identically.

**Caching (17b):**
- **Off / unmarked is byte-identical:** with `prompt_cache_enabled = False` (or no segment marked), the provider request bytes equal today's — Anthropic `_translate_messages` still emits the identical `system` **string** (regression-pin its output).
- **On is byte-identical to the model:** with caching on, the **text content** sent to Anthropic is identical; only `cache_control` metadata is added (assert the concatenated system text equals the uncached string; assert the block carries `cache_control: {type: ephemeral}`).
- **Memory block is never cached:** assert the request-varying memory/user segment is **not** marked cacheable.
- **No cross-boundary leak:** no cache handle/key is shared across run or project; (if Gemini seam touched) any `CachedContent` is per-run and cleaned up — but default-off means the test asserts it's *not* created.
- **Cache hit/miss is behavior-neutral:** the `LLMResponse.text` and all downstream behavior are identical whether the provider reports a cache hit or miss (the response is the model's).
- **Other providers & fakes ignore the marker gracefully:** `FakeProvider` and the non-caching providers produce identical output with the marker present (the test fakes must keep working unchanged).

**Both:**
- **Parity:** the existing driver/orchestrator/stage-contract/ledger/provider suites stay green (unmodified where the feature is off). `ruff check` clean on changed files; `cd frontend; npm.cmd run build` only if any frontend type changed (likely none — this slice is backend-only). Run the unit set: `python -m pytest backend/tests -q -m unit`.

## 17.5 Traps

- **(a) Dropping the planner on a non-trivial chunk.** The predicate must fail **toward `standard`**. Any missing/ambiguous signal (e.g. triage not loadable, `complexity` absent) → `standard`, never `merged`. A false-positive silently removes planning from work that needed it — the inverse of fail-safe (§0, contract §9).
- **(b) "It's trivial, so skip the reviewer/gate/baseline."** The *only* thing the profile removes is the redundant planner LLM call. The reviewer, the baseline verification, the ack gates (item 15), and the chunk/final approval gates are **identical**. Pin the reviewer-always test explicitly.
- **(c) Caching mutating bytes.** `cache_control` is metadata; the text must be identical. A translation that reorders, trims, or re-joins the system text differently when caching is on is a silent prompt change → potential quality drift. Pin byte-equality of the text.
- **(d) Caching the memory block.** The memory block's relevance is a safety property (the right facts for *this* request). Force-caching it to save tokens trades relevance for cache hits — forbidden (§4.7/M9). Cache only request-independent bytes.
- **(e) Cross-run / cross-project cache leak.** Especially if the Gemini explicit-cache seam is ever wired: a `CachedContent` handle that outlives or crosses a run/project could serve one context's bytes to another. Default-off, per-run lifecycle, and a test that the default path creates no shared handle.
- **(f) The plan-summary disappearing from the audit trail.** The proposal requires the plan-summary to remain in the output even when the separate planner call is gone (§4.7). E2-synthesize must still persist a valid plan-summary in `completion_summary`; don't let the merge erase the audit artifact.
- **(g) Burying the new knobs.** `merged_profile_sample_pct`, the E1 denylist, and the cache flag are behavior-affecting policy — single-source them in `policy.py` with comments, never as scattered literals in the driver/provider (`CLAUDE.md`: no buried magic numbers; one source of truth).
- **(h) `stage_profile` breaking the ledger contract.** It is **metadata** — a closed enum, nullable, never a prompt/diff/secret, never read as authority for retry/eligibility (those stay with the `patch_failures.py` frozensets + `ExecutionIntegrity`). Additive column only; legacy `NULL` rows must not perturb resume.
- **(i) Eligibility reading un-approved state.** The predicate reads the **approved** triage + chunk. It must not re-trigger triage, re-plan, or read a draft/pending plan; it is a post-approval execution decision.
- **(j) Touching model selection.** Caching is orthogonal to which model runs. Do not move or duplicate model selection out of `role_config.py` (one source of truth; `policy.py:16-18`).

## 17.6 Safety-contract check (item 17)

- **§2.1 / §2.2 (gates / scope):** untouched. The profile removes only a *post-approval* planner LLM call; triage (the approval artifact), `scope_guard`, preflight, and every gate run identically. Prove the §0 parity test and the no-authority-leak test.
- **§2.3 / §2.9 (no empty commits; fail safe):** the eligibility predicate fails toward `standard`; caching errors fall back to the uncached byte-identical request. Prove the predicate default and the off/unmarked byte-identity.
- **§2.6 (no secrets/PII; sanitize):** `stage_profile` is a closed enum; caching adds no persisted data and logs no prompt bytes. Prove the column is metadata-only and no prompt content is logged.
- **§2.7 (advisory, never authority):** neither the profile decision nor a cache hit/miss changes scope, approval, memory selection, reviewer independence, or Git. Prove cache hit/miss behavior-neutrality and that nothing branches on `stage_profile` for a decision.
- **Reviewer kept (proposal §4.7 / E10):** the reviewer runs on every trivial chunk; reviewer independence (#33C) is unchanged. Prove the reviewer-always test.
- **No-migration-of-meaning invariant:** `stage_profile` is additive; no status/enum string is renamed; model selection is untouched. Prove the additive schema and the unchanged enums.

---

## 3. Update these docs when you finish (part of "done")

**Status (2026-06-12):** the 17a slice did items 1 (partial — its landing is recorded; the "Phase 4 COMPLETE" stamp waits on 17b) and 4. Items 2 (cache flags), 3 (soak note/SQL), and 5 (retire the brief) finish with **17b**.

1. **`PIPEWRIGHT_REDESIGN_WORKPLAN.md`** — mark item 17 done in the Phase 4 sequence + "How to resume" + the TL;DR. **Note that Phase 4 — and Area A Pass 1 — is COMPLETE.** *(17a's landing is already recorded there; make this final "Phase 4 COMPLETE" stamp only when **17b** also lands.)*
2. **`backend/pipeline/policy.py`** docstring + the new constants — the profile sampling knob (`MERGED_PROFILE_SAMPLE_PCT`) + the denylist (`TRIVIAL_PROFILE_DENYLIST_PATTERNS`) landed commented in 17a; **17b still adds the cache flag(s)** (`PROMPT_CACHE_ENABLED`, `GEMINI_EXPLICIT_CACHE_ENABLED`). Restate that model selection stays in `role_config.py`.
3. ~~**A short `docs/design/` note (or the proposal §18.3 section)** — the trivial-profile eligibility predicate as-built and the **documented SQL** for the §18.3 soak + the D12 rollback trigger (the measurement is SQL over the ledger, not code).~~ ✅ **done for 17a:** `docs/design/trivial-task-profile-soak.md` (operator-facing — `stage_profile` values, cohort-comparison SQL over `chunk_attempts`, and the `MERGED_PROFILE_SAMPLE_PCT = 0` rollback). 17b extends it with the caching-usage view if useful.
4. ~~**The `chunk_attempts` ledger comment** (`schema.sql` / `chunk_attempt_store.py` docstring) — record the new `stage_profile` column and its closed enum.~~ ✅ **done in 17a (PR #288).**
5. **This file** — once item **17b** lands + is reviewed, this rolling brief has **no further Phase-4 occupant**: note that Area A Pass 1 is complete and the brief is dormant until the next pass (e.g. Area B / Pass 2 memory work) repurposes it.
6. These planning docs are now **tracked** (they landed with PR #288 alongside the 17a code). Update content; **do not commit** doc or code changes unless the maintainer asks. If asked to commit: branch off `develop` first (never straight to `develop`/`main`), one item per commit, end with the repo's `Co-Authored-By` trailer.

## 4. Working discipline (this slice)

- **Design first, then get it reviewed, then implement.** Produce a short design answering **E0–E5** — the eligibility predicate (the exact criteria + the E1 ruling), how the planner call is removed (E2 + justification), where the profile branch attaches in the driver, the `stage_profile` ledger add, and the caching marker + per-provider translation + flags — and have the maintainer review it **before** writing code. You own the design; because you also write the tests, the design review is the homework check.
- Read the real code first; re-verify every `file:line`; correct this brief's pointers if the live code drifted and say so.
- **Capture a parity snapshot** of the driver/orchestrator/stage-contract/ledger/provider suites before you touch anything — "feature off is byte-identical" is only provable against a pre-change baseline.
- **Strongly prefer E0's two-PR split**: 17a (profile) then 17b (caching), each smallest-correct and independently reviewed.
- Smallest correct change; **item 17 only**; list what you deliberately did **not** change (the reviewer, triage, gates, scope_guard, commit/rollback, PR rules, model selection, the memory block, the layered-policy framework, Gemini explicit caching beyond a seam).
- Tests assert the **decided behavior** (deterministic eligibility, planner-call removed, `stage_profile` recorded, caching byte-identity) **and the §0 invariant** (feature off ⇒ byte-identical; reviewer always; no authority leak; no memory caching) — not just that code runs.
- Report on completion: changed files, tests run + results, manual validation, risks, what was intentionally left untouched.
- A human reviews this/these PR(s).
