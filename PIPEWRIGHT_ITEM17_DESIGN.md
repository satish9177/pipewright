# Item 17 — Design doc (trivial-task stage profile + provider prompt caching)

**Date:** 2026-06-12 · **Status:** COMPLETE (both PRs landed 2026-06-12). **PR 17a (trivial-task stage profile) merged via PR #288** (commits `221cc9c` / `24e24b8`). **PR 17b (provider prompt caching) merged via PR #290** (commit `e2b7cc9`). Both had a pre-merge working-tree review with no must-fix; 17b cache flags ship OFF (byte-identical on merge). This completes Phase 4 and Area A (Pipeline) Pass 1. The §1–8 spec below is the as-built record.
**Brief:** `PIPEWRIGHT_REDESIGN_IMPL_BRIEF.md` (item-17 section). **Proposal source:** §4.7, §5.6, §6 item 17, §18.2, §18.3.
**Rulings applied:** E0–E5 as approved 2026-06-12 (two PRs; conservative deterministic eligibility + denylist; synthesize-from-triage; sample 50% stable; Anthropic-active/OpenAI-DeepSeek-passive/Gemini-seam caching; typed cache marker).

This is the *how*. Code-shaped snippets below are **specification, not implementation** — they pin names/shapes for review; nothing is written to `backend/` until the maintainer signs off.

---

## §0 Headline invariant (corrected per E2)

- **Feature OFF ⇒ byte-identical to today.** With `merged_profile_sample_pct = 0` (17a) and `prompt_cache_enabled = False` (17b), the system is byte-for-byte the current system. This is the primary parity test for both PRs.
- **17a, feature ON, the ONLY permitted delta:** for a *provably trivial, eligible, sampled* chunk, a **deterministic `PlannerHandoff` synthesized from triage** replaces the **planner LLM output**. The coder template, `scope_guard`, preflight/dry-run, baseline verification, the reviewer (always), all gates, commit/rollback, and PR behavior are **unchanged**. A **plan-summary still exists and is persisted** for audit (the synthesized handoff flows through the same `_surface_files_expected_for_edit` → coder → `_commit_and_complete_chunk` path). *We do NOT claim the profiled coder request equals any standard planner output — only that the substituted input is a deterministic synthesis and nothing downstream of it moves.*
- **17b, feature ON:** identical prompt **bytes** to the model; only provider cache **metadata** differs (Anthropic `cache_control`). Never caches the memory block or any request-varying context; never crosses run/project boundaries; never gates. A cache hit and miss produce identical model output.

Two PRs (E0): **17a first** (profile; no caching), **17b after 17a lands** (caching; no profile changes). They share only `policy.py`.

---

# PR 17a — trivial-task stage profile  ✅ MERGED (PR #288, 2026-06-12)

## 1. Exact eligibility function (E1)

New pure module `backend/pipeline/stage_profile.py`. Eligibility is a total predicate over the **already-approved** triage + chunk; any missing/ambiguous signal returns `STANDARD`.

```python
class StageProfile(StrEnum):
    STANDARD = "standard"
    MERGED_PLAN_CODE = "merged_plan_code"

def is_trivial_eligible(
    triage: TriageResult | None,
    chunk: ChunkDefinition,
    target_repo_path: str,
    *,
    path_exists=_default_repo_file_exists,   # injectable for tests
) -> bool:
    if triage is None:                                   # missing signal -> standard
        return False
    if triage.total_chunks != 1:                         # single chunk only
        return False
    if triage.complexity != "easy":
        return False
    if chunk.risk_level != "low":
        return False
    if chunk.requires_human_review:
        return False
    if chunk.depends_on:                                 # must be []
        return False
    if not chunk.files_expected:                         # non-empty
        return False
    for rel in chunk.files_expected:
        if _is_denylisted(rel):                          # dangerous path -> standard
            return False
        if not path_exists(target_repo_path, rel):       # every path must exist (modify-only)
            return False
    return True
```

- **`path_exists`** resolves `rel` under `target_repo_path` via the existing `path_safety` safe-join, and is true only for an existing **regular file** inside the repo. Consequence (deliberate, called out): a chunk that **creates** a new file is **not** eligible (the file doesn't exist yet) and runs `standard` — conservative and intended, since file creation is where the planner most plausibly adds value.
- **Denylist** (`_is_denylisted`, case-insensitive on the normalized relative path), single-sourced in `policy.py` as `TRIVIAL_PROFILE_DENYLIST_PATTERNS`. Forces `STANDARD` for dangerous edits even when all other signals say trivial. Categories and seed patterns:
  - **migrations / DB schema:** `*/migrations/*`, `*/alembic/*`, `schema.sql`, `*.sql`
  - **auth / security:** path segments `auth`, `security`, `permission(s)`, `login`, `password`, `crypto`, `jwt`, `oauth`, `session`
  - **secrets / env:** `.env*`, `*secrets*`, `*credentials*`, `*.pem`, `*.key`, `id_rsa*`
  - **dependency manifests:** `requirements*.txt`, `pyproject.toml`, `poetry.lock`, `Pipfile*`, `setup.py`, `setup.cfg`, `package.json`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `go.mod`, `go.sum`, `Cargo.toml`, `Cargo.lock`, `Gemfile*`
  - **CI / build config:** `.github/*`, `.gitlab-ci.yml`, `.circleci/*`, `Jenkinsfile`, `Dockerfile*`, `docker-compose*`, `Makefile`, `tox.ini`, `*.config.js`/`*.config.ts` (e.g. `vite`, `webpack`), `tsconfig.json`
  - This denylist is a *force-standard* margin, distinct from (and never a replacement for) the hard write-forbidden set in `path_safety` (`.env`, `.git/`, secrets) — `scope_guard`/`path_safety` remain the write authority regardless of profile.

## 2. Sampling plan (E3)

```python
def resolve_stage_profile(
    triage, chunk, target_repo_path, *, run_id, sample_pct, path_exists=...
) -> StageProfile:
    if sample_pct <= 0:                                  # 0 fully disables (parity)
        return StageProfile.STANDARD
    if not is_trivial_eligible(triage, chunk, target_repo_path, path_exists=path_exists):
        return StageProfile.STANDARD
    bucket = int(sha256(f"{run_id}:{chunk.chunk_number}".encode()).hexdigest(), 16) % 100
    return StageProfile.MERGED_PLAN_CODE if bucket < sample_pct else StageProfile.STANDARD
```

- `policy.MERGED_PROFILE_SAMPLE_PCT = 50` (soak default; single-sourced, commented like `AUTO_RETRY_INFRA_BUDGET`).
- Sampling is **stable per `run_id + chunk_number`** (hash bucket, no PRNG state) — a re-read/replay classifies identically, which the soak audit needs.
- `sample_pct = 0` short-circuits **before** eligibility, so the off state is a guaranteed no-op.

## 3. Driver attachment point

The profile is a **`fresh`-mode-only** concept — `human_retry`/`steered`/`refinement` already skip the planner via `_retry_plan_for_chunk`, and `resume` skips stages; none touch this. The decision is resolved where the triage is in hand and passed down; the driver only acts on it.

- **Resolve** in `_execute_approved_chunks_locked` (`chunked_orchestrator.py:1953-1966`): it has `plan_status.triage` (→ `complexity`, `total_chunks`), the `chunk`, and `target_repo_path`. Per chunk: `profile = resolve_stage_profile(plan_status.triage, chunk, target_repo_path, run_id=run_id, sample_pct=policy.MERGED_PROFILE_SAMPLE_PCT)`.
- **Thread** through `_execute_single_chunk` → `chunk_driver.drive_chunk(EntryMode.FRESH, …, stage_profile=profile)` → `_drive_stages`. New optional param `stage_profile: StageProfile = StageProfile.STANDARD`; all non-fresh callers keep the default → unchanged.
- **Branch** in `_drive_stages` (`chunk_driver.py:506-522`), the fresh `else` block that currently calls `plan_stage`:
  - `STANDARD` → today's `plan_stage` (LLM planner) — unchanged.
  - `MERGED_PLAN_CODE` → **no planner call**; build the synthesized handoff (an orchestrator helper, resolved via `_orch()`, e.g. `orch._synthesize_trivial_plan(run_id, enriched_description, chunk)`), and append a plan `StageOutcome(stage="plan", outcome_class=SUCCESS, payload=synth, evidence=("synthesized_from_triage",))` so the ledger's stage list keeps its plan→code shape.
  - Both paths then go through the **same** `plan = orch._surface_files_expected_for_edit(payload, chunk.files_expected)` and the identical `while True` apply/verify/review/gate/commit loop. Nothing below the plan stage is aware of the profile.

## 4. Synthesized `PlannerHandoff` shape (E2)

Pure, deterministic, no LLM. `PlannerHandoff` has no hard `min_items` on `steps`, but we honor the planner contract's spirit (≥2 steps) and keep the coder fully grounded:

```python
def synthesize_trivial_plan(run_id, feature_description, chunk) -> PlannerHandoff:
    return PlannerHandoff(
        handoff_from="planner",          # provenance unchanged: downstream sees a normal handoff
        handoff_to="coder",
        run_id=run_id,
        feature_description=feature_description,   # the same enriched description the planner would receive
        goal=chunk.title,                          # one sentence
        steps=[
            f"Implement chunk {chunk.chunk_number}: {chunk.title}",
            chunk.description,
        ],
        files_to_create=[],                        # eligibility guarantees all files_expected exist -> modify-only
        files_to_modify=list(chunk.files_expected),
        files_to_read=list(chunk.files_expected),  # coder grounds edits on these
        out_of_scope=[],
        risks=[],
        suggested_memory_entries=[],
    )
```

- `feature_description` = the **same** `enriched_description` from `_build_enriched_feature_description` that the planner stage would have received, so the coder's input is built from the identical run context (minus the planner's LLM elaboration, which is exactly the redundant work we're removing).
- This handoff **is** the plan-summary; it persists for audit via the unchanged `plan` argument to `_commit_and_complete_chunk` (`chunk_driver.py:780-789`).

## 5. `stage_profile` ledger plan (§18.2)

Additive, nullable, metadata-only — same pattern as the item-12/13/14 column adds.

- **`schema.sql`** (`chunk_attempts`, near `:205`): add `stage_profile TEXT` (nullable). Closed value set `standard | merged_plan_code`; `NULL` for legacy rows and any non-fresh attempt where the concept doesn't apply.
- **`database.py` `_ensure_chunk_attempts_shape`** (`:537`): add the column idempotently for existing DB files (the helper already does additive `ALTER`/`CREATE IF NOT EXISTS` work).
- **`chunk_attempt_store.record_chunk_attempt`** (`:32`): new `stage_profile: str | None = None` param → INSERT column.
- **`chunk_driver._record_attempt` / `_drive_stages`**: thread the active `stage_profile.value` into every `_record_attempt` call for the fresh pass (so each recorded attempt of a profiled chunk carries `merged_plan_code`; standard passes carry `standard`).
- **Never** read `stage_profile` as authority for retry/eligibility — auto-retry stays keyed on failure type + `ExecutionIntegrity`; human-retry eligibility stays in the `patch_failures.py` frozensets. It is audit metadata only.

## 6. 17a tests (`backend/tests/test_stage_profile.py` + driver/ledger additions)

- **Eligibility table (pure):** one case per boundary returns `STANDARD` — `triage is None`; `total_chunks != 1`; `complexity in {medium, hard}`; `risk_level in {medium, high}`; `requires_human_review`; `depends_on != []`; empty `files_expected`; an ungrounded (non-existent) path; each denylist category (migration, schema.sql, auth, `.env`, `requirements.txt`, `.github/…`). The all-pass case returns eligible. Plus a `path_exists` injection so the test is deterministic.
- **Sampling:** `sample_pct = 0` ⇒ `STANDARD` even when eligible; stability ⇒ same `run_id+chunk_number` yields the same bucket across calls; a known bucket near the 50 boundary lands on the expected side.
- **§0 parity (feature off):** with `MERGED_PROFILE_SAMPLE_PCT = 0`, an otherwise-eligible chunk runs `plan_stage` and the run is byte-identical to a captured pre-change golden of the driver/orchestrator suites.
- **Profile taken removes exactly the planner call:** eligible+sampled ⇒ `run_planner` spy **not** called; the synthesized handoff drives the coder; preflight/apply/verify/**review**/gate/commit all still run; `completion_summary` still contains a plan-summary.
- **Reviewer always (cannot ship without):** an eligible+sampled chunk still reaches `review_stage`.
- **Ledger:** profiled pass records `stage_profile = "merged_plan_code"`, standard pass records `"standard"`; `_ensure_chunk_attempts_shape` is idempotent; legacy `NULL` rows don't perturb resume / `get_latest_completed_attempt_head`.
- **No authority leak:** a profiled chunk doesn't change `files_expected`, `risk_level`, `requires_human_review`, or the approval requirement; chunk/final gates fire identically.

---

# PR 17b — provider prompt caching  ✅ MERGED (PR #290, 2026-06-12)

## 7. Cache marker + provider translation plan (E4/E5)

- **Typed marker at the LLM layer (E5).** Add `cache: bool = False` to `Message` (`backend/llm/base.py:11`). Default `False` ⇒ today's exact bytes. Planner/coder mark **only** their static system message: `Message(role="system", content=SYSTEM_PROMPT, cache=True)` (`planner.py:_build_llm_request`, `coder.py:_build_llm_request`). The memory-bearing **user** message is never marked.
- **Policy flags (`policy.py`).** `PROMPT_CACHE_ENABLED = False` initially; flipped to `True` (Anthropic) only after 17b tests pass. `GEMINI_EXPLICIT_CACHE_ENABLED = False` — a named, disabled seam (TODO), no `CachedContent` created.
- **Per-provider translation:**
  - **Anthropic (active).** In `_translate_messages`/`complete` (`anthropic.py:92-104, 80-81`): when `PROMPT_CACHE_ENABLED` **and** a system message has `cache=True`, emit `system` as a **content-block list** — the marked stable text as `{"type": "text", "text": <stable>, "cache_control": {"type": "ephemeral"}}`, any unmarked system text as plain blocks (no `cache_control`). When the flag is off or nothing is marked, emit the **identical string** as today. The concatenated text is byte-identical either way; only metadata is added.
  - **OpenAI / DeepSeek (passive).** No marker handling, no translation change. They already pass messages through in stable order (`openai.py:85-90`) with the system prompt as a stable prefix, which is what provider-side automatic caching keys on. 17b's only obligation here is **don't break the stable prefix** (no reordering, no per-call mutation of the system prompt). A regression test pins message order/prefix stability.
  - **Gemini (seam only).** Marker ignored; `_translate_messages` unchanged. The disabled `GEMINI_EXPLICIT_CACHE_ENABLED` flag documents the future explicit-cache path; no handle is created (default provider is Gemini, so this stays inert until deliberately built).
  - **Fake / others.** Ignore the marker (read `.content` only) ⇒ byte-identical; the test fakes keep working unchanged.
- **No new persistence; no logged prompt bytes.** Caching may surface hit/usage via the existing `LLMResponse.input_tokens`/`raw` for the soak, but stores nothing new and logs no content.

## 8. 17b tests (`backend/tests/test_prompt_cache.py` + provider tests)

- **Off / unmarked byte-identical:** with `PROMPT_CACHE_ENABLED = False` (or no marked message), Anthropic `_translate_messages` emits the identical `system` **string** as today (regression-pinned); OpenAI/DeepSeek/Gemini/Fake outputs unchanged.
- **On byte-identical text:** with the flag on and the system message marked, the Anthropic system payload's concatenated text equals the uncached string; the first block carries `cache_control: {type: ephemeral}`; no other bytes differ.
- **Memory never cached:** the user/memory message is never marked `cache=True`; assert the request builder marks only the system message.
- **Hit/miss behavior-neutral:** a faked provider response with vs. without a cache-hit indicator yields identical `LLMResponse.text` and identical downstream behavior.
- **Passive providers:** OpenAI/DeepSeek message order + system-prefix stability pinned; marker present ⇒ identical translated messages.
- **Gemini seam inert:** with `GEMINI_EXPLICIT_CACHE_ENABLED = False`, no `CachedContent` is created and translation is unchanged.
- **Parity:** existing provider/llm suites green; `ruff check` clean.

---

## 9. Safety-contract checks (item 17)

- **§2.1 / §2.2 (gates / scope):** the profile removes only a *post-approval* planner LLM call; triage (the approval artifact), `scope_guard`, preflight, and every gate run identically. Proven by §0 parity + no-authority-leak tests.
- **§2.3 / §2.9 (no empty commits; fail safe):** eligibility fails toward `STANDARD` on any missing/ambiguous signal; a caching-translation error falls back to the uncached byte-identical request. Proven by the eligibility table + off/unmarked byte-identity.
- **§2.6 (no secrets/PII; sanitize):** `stage_profile` is a closed enum, metadata-only; caching adds no persisted data and logs no prompt bytes.
- **§2.7 (advisory, never authority):** neither the profile decision nor a cache hit/miss changes scope, approval, memory selection, reviewer independence, or Git. Proven by hit/miss neutrality + nothing branching on `stage_profile`.
- **Reviewer kept (proposal §4.7 / E10):** the reviewer runs on every trivial chunk; reviewer independence (#33C) untouched. Proven by the reviewer-always test.
- **No-migration-of-meaning:** `stage_profile` is additive; no status/enum string renamed; model selection stays in `role_config.py`; the memory block is never cached.

## 10. Out of scope (both PRs)

Reducing/removing the reviewer for any task · removing/merging triage or touching the chunk-plan approval gate · any LLM-based eligibility · the layered per-project policy framework (only the new flat constants are added) · Gemini `CachedContent` (disabled seam only) · §18.3 soak automation / metrics endpoint (the rollback trigger is documented SQL + a config flip) · caching the memory block or any request-varying context · any cross-run/cross-project cache handle · changing model selection / temperature / max-tokens / streaming / token-count semantics · renaming any `RunStatus`/`ChunkStatusValue`/`OutcomeClass`/`PatchFailureType`/verdict string · any new route/endpoint/gate/authority · any chat/thread/history feed · any Area-B (memory) change.

---

## 11. Files touched (planned)

**17a:** `backend/pipeline/stage_profile.py` (new) · `backend/pipeline/policy.py` (sample pct + denylist) · `backend/pipeline/chunked_orchestrator.py` (resolve + thread; `_synthesize_trivial_plan`) · `backend/pipeline/chunk_driver.py` (`stage_profile` param + fresh-mode branch + ledger thread) · `backend/pipeline/chunk_attempt_store.py` (param) · `backend/db/schema.sql` + `backend/db/database.py` (additive column) · new `backend/tests/test_stage_profile.py` + driver/ledger test additions.

**17b:** `backend/llm/base.py` (`Message.cache`) · `backend/llm/providers/anthropic.py` (active translation) · `backend/pipeline/planner.py` + `backend/pipeline/coder.py` (mark system message) · `backend/pipeline/policy.py` (cache flags) · new `backend/tests/test_prompt_cache.py` + provider-test additions. (OpenAI/DeepSeek/Gemini/Fake providers: no behavior change; covered by regression assertions.)

**Docs after landing:** `PIPEWRIGHT_REDESIGN_WORKPLAN.md` (item 17 done; Phase 4 + Area A Pass 1 COMPLETE), `policy.py` docstring, a `docs/design/` note + the §18.3 soak SQL, the `chunk_attempts` ledger comment, and this rolling brief retired. Planning docs are untracked; commit only when the maintainer asks.
