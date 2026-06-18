# Design Brief — LLM Provenance Role-Coverage Patch

Status: **COMPLETE as of 2026-06-18 (PR-A + PR-B implemented).** Scope was a
thin, additive, metadata-only extension of the existing `llm_call_provenance`
write path (#33B; see `docs/design/multi-provider-modes.md`) so that every
*active invoked* pipeline LLM role records provenance, not just the coder.

Related: verification audit (Row 16b soak follow-up) confirmed the coverage gap is
**structural**, not sparse dev data: provenance is written from exactly one place.

---

## Implementation closeout (2026-06-18)

LLM provenance role coverage is complete for active invoked pipeline roles:

- `coder` already wrote metadata-only provenance before this patch family.
- `reviewer` was added in PR-A.
- `triage`, `planner`, and `summary` / `report_analyzer` were added in PR-B.

All new writes reuse the existing `try_record_llm_call_provenance` wrapper
unchanged. Writes remain best-effort and non-blocking: a store failure must never
fail triage, planner, reviewer, summary/report analysis, or coder execution.
Rows remain metadata-only (`run_id`, optional `chunk_number`, role,
provider/model, finish reason, token counts, timestamps, ids). They do not store
prompts, responses, diffs, raw provider errors, secrets, tokens, PII, memory
entries, or file contents. `selection_source` intentionally remains `None`.

PR-B records one row per real provider call, including correction/retry provider
calls. Coder semantics are intentionally unchanged: coder continues to record the
final effective output row rather than one row per parse-retry call.

Deliberate exclusions remain:

- Intent-suggestion LLM fallback is deferred because it has no `run_id`, while
  `llm_call_provenance.run_id` is `NOT NULL`.
- `architect` is excluded because it is enum-only / not actively invoked.
- Cache hit/miss fields remain out of scope; no `LLMResponse` or provider-adapter
  cache-token fields were added, and prompt cache was not activated.

---

## Verdict

**COMPLETE.** The patch was small, additive, isolated, and reused
`try_record_llm_call_provenance` - a write path already proven safe and
non-blocking by the coder. It changes no role behavior, no provider selection, no
prompt content, no cache behavior, and no runtime decision. The schema already had
every column needed.

Two clarifications change the premise and must be reflected (details below):

1. **`summary` does NOT retain `LLMResponse`.** `report_analyzer._call_llm`
   returns `response.text`, exactly like triage and planner. So `summary` belongs
   in PR-B, not PR-A. PR-A is **reviewer only**.
2. **The intent-suggestion path has no `run_id`** and is not tied to a persisted
   `pipeline_runs` row. `llm_call_provenance.run_id` is `NOT NULL` with an FK to
   `pipeline_runs`. That path is **deferred on safety/schema grounds**, not effort.

The row-semantics decision is closed as the minimal safe path: PR-B roles use
per-call rows, while coder semantics remain unchanged.

---

## 1. Exact current write path

Initial production writer: `backend/pipeline/coder.py:543`, calling
`try_record_llm_call_provenance(...)` **after** the coder's retry loop, recording
the **effective** (final successful) `coder_response`. Best-effort: the `try_*`
variant swallows and logs all failures and can never change the coder outcome.

The store (`backend/pipeline/llm_call_provenance_store.py`) is correct,
metadata-only CRUD. The table (`backend/db/schema.sql:383-396`) has columns:
`id, run_id, chunk_number, role, provider, model, selection_source, finish_reason,
input_tokens, output_tokens, created_at`. All other references to the table are
read-only or tests. PR-A/PR-B extended writers to reviewer, triage, planner, and
summary/report_analyzer without changing the store or schema.

---

## 2–3. Role coverage

| Role | Call site | Retains `LLMResponse`? | Scope | Decision |
|------|-----------|------------------------|-------|----------|
| **coder** | `coder.py:378` | yes | run+chunk | already covered (no change) |
| **reviewer** | `reviewer.py:353` | **yes** (`response`) | run+chunk in scope | **covered by PR-A** |
| **triage** | `triage.py:162` (via `_call_llm`) | no — returns `.text` | run-level (`chunk_number=None`) | **covered by PR-B** |
| **planner** | `planner.py:131` (via `_call_llm`) | no — returns `.text` | run+chunk (chunk_number lives on `run_planner`, not `_call_llm`) | **covered by PR-B** |
| **summary** | `report_analyzer.py:551` (via `_call_llm`) | no — returns `.text` | run-level (`chunk_number=None`) | **covered by PR-B** |
| **architect** | — none — | n/a | n/a | **EXCLUDE** (enum-only, never invoked) |
| intent-suggestion (triage role) | `intent.py:722` | yes locally | **no `run_id`** | **DEFER** (FK `NOT NULL`) |

Roles included: reviewer, triage, planner, summary. Excluded: architect (dead
role). Deferred: intent-suggestion path.

---

## 4–5. PR split

- **PR-A — reviewer.** Reviewer already holds `response` and has `run_id` +
  `chunk_number` in scope. One additive best-effort call after
  `log_token_usage` (`reviewer.py:354`). No signature changes anywhere. Lowest
  blast radius; shipped first.
- **PR-B — triage + planner + summary.** All three discard the response inside
  their own `_call_llm` (returning `response.text`). The provenance write goes
  **inside `_call_llm`**, where the response object is still in hand (see Q6).
  Planner additionally needed `chunk_number` threaded into its `_call_llm`.

The user's framing put summary in PR-A "because it retains LLMResponse." It does
not — `report_analyzer._call_llm:549` returns `.text`. Summary is mechanically
identical to triage/planner and belongs in PR-B.

---

## 6. Where triage/planner/summary record: inside `_call_llm`

**Record inside `_call_llm`, do NOT thread `LLMResponse` outward.**

Rationale:
- The response object is already local to `_call_llm`; recording there needs no
  return-type change and no ripple to callers (`run_triage`, `run_planner`,
  `run_report_analysis`).
- Threading the response out would change `_call_llm`'s return type from `str` to
  a tuple/response, touch every call site (including the correction-retry sites),
  and require a separate write at attempt-1 *and* attempt-2 — more surface, easy
  to miss the retry. Worse on every axis.
- `chunk_number`: triage and summary are run-level → constant `None`. Planner is
  chunk-scoped but `chunk_number` lives on `run_planner` (default `0`), not on
  `_call_llm`; add a `chunk_number: int = 0` parameter to `planner._call_llm` and
  pass it through from both call sites (`planner.py:199, 213`). This is the only
  signature change in the whole patch, and it is internal to `planner.py`.

---

## 7. Correction/retry calls → separate rows (per-call semantics)

**Yes — each real provider call produces its own row.** Recording inside
`_call_llm` fires once per invocation, and a correction-retry is a *distinct,
billed* provider call. For the token/cost/cache-opportunity accounting that
motivates this work, counting the real failed-parse call is the correct behavior,
not a bug.

**Accepted caveat:** the coder today records only its
**effective** output (one row, post-loop), so coder does *not* emit a row for its
own parse-retry calls. PR-B's new roles will use **per-call** semantics. That is a
deliberate divergence; see "Closed decisions / reading notes."

---

## 8. Intent-suggestion path: DEFER

`intent._llm_fallback_classify(feature_description)` and the public
`classify_intent_*` entry points take no `run_id` and are not bound to a persisted
`pipeline_runs` row (intent classification can run before/independently of a run).
`llm_call_provenance.run_id` is `NOT NULL` with `FOREIGN KEY (run_id) REFERENCES
pipeline_runs(id)`. Writing here would require inventing a run_id or relaxing the
FK — both out of scope and both a safety regression. Defer until/unless intent
classification is given a real run context. Document explicitly that this path
intentionally writes no provenance.

---

## 9. Metadata written

Per call, via `try_record_llm_call_provenance`:

- `run_id` (always present at each in-scope site)
- `chunk_number` (reviewer/planner: real chunk; triage/summary: `None`)
- `role` (`Role.<X>.value`)
- `provider` = `response.provider`
- `model` = `response.model`
- `finish_reason` = `response.finish_reason`
- `input_tokens` / `output_tokens` = `response.input_tokens` / `.output_tokens`
- `selection_source` = `None` (unchanged; deriving it would duplicate
  `resolve_role_config` precedence — explicitly out of scope)
- `id` / `created_at` auto-generated by the store

Provider-agnostic: all four real adapters (gemini, anthropic, openai, deepseek)
already populate provider/model/finish_reason/tokens.

---

## 10. Never written

No prompts, no response text, no diffs, no file contents, no provider/Git errors,
no API keys/tokens/auth headers, no PII, no memory entries. The record model has
no field capable of holding any of these (`llm_call_provenance_store.py:36-61`),
and write-failure messages are routed through `sanitize_for_log`. This patch adds
no new field, so the invariant holds unchanged.

Explicitly **not** in this slice: cache-read/cache-creation token fields (cache
hit/miss effectiveness). That requires extending `LLMResponse` and every provider
adapter and is a separate, higher-blast-radius slice. Out of scope here.

---

## 11. Exact code touch map

All writes are additive, best-effort (`try_record_llm_call_provenance`, never
raises), placed immediately after the existing `log_token_usage` line.

- **`reviewer.py`** — add import; after `log_token_usage` (`:354`) add one
  provenance write (`role=Role.REVIEWER.value`, `chunk_number=chunk_number`).
- **`triage.py`** — add import; in `_call_llm` (`:161-167`) add one write
  (`role=Role.TRIAGE.value`, `chunk_number=None`) before `return response.text`.
- **`planner.py`** — add import; add `chunk_number: int = 0` param to `_call_llm`
  (`:126`); pass it from `:199` and `:213`; add one write
  (`role=Role.PLANNER.value`, `chunk_number=chunk_number`) before
  `return response.text`.
- **`report_analyzer.py`** — add import; in `_call_llm` (`:549-554`) add one write
  (`role=Role.SUMMARY.value`, `chunk_number=None`) before `return response.text`.

No change to: `coder.py`, the store, the schema, any provider adapter,
`role_config.py`, `LLMResponse`, or any flag.

---

## 12. Must-not-touch list

- `resolve_role_config` precedence; `selection_source` derivation (keep `None`).
- Provider adapters and their response mapping — no cache-token fields here.
- `LLMResponse` model; `llm_call_provenance` schema (no new columns).
- `PIPEWRIGHT_PROMPT_CACHE_ENABLED` / any flag activation; cache behavior.
- Checkpoints / resume substrate; `scope_guard`; approval & final-approval gates;
  Git / PR behavior; `patch_applier`.
- Memory retrieval / injection / FTS (Row 19) / Row 23.
- The coder write (left as-is — see Closed decisions / reading notes).
- The intent path (deferred — must remain provenance-free this slice).
- No new event persistence beyond the existing table; writes stay best-effort and
  never raise into a role.

---

## 13. Validation plan

- New targeted tests mirroring `test_coder_provenance.py`, one per role
  (reviewer, triage, planner, summary):
  1. successful call writes exactly one row with correct
     role/provider/model/finish_reason/tokens and correct `chunk_number`;
  2. parse-failure correction path writes a row per real call (two rows for the
     per-call roles) — asserts retries are counted, not dropped;
  3. store failure (monkeypatch `record_llm_call_provenance` to raise) never
     changes the role's outcome (reuse the existing "boom" pattern).
- A guard test asserting the intent-suggestion path writes **no** provenance row.
- Run: `python -m pytest backend/tests -q -m unit` plus the new/targeted
  provenance + role tests; `ruff check`.
- No live-API tests required — every role is exercised through monkeypatched
  `complete_for_role`.

---

## 14. Closed decisions / reading notes

1. **Row semantics: per-call (new roles) vs. effective-output (coder today).**
   PR-B records one row per real provider call (including parse-retry calls);
   coder records only its final effective output. Section 9 queries use
   `COUNT(*)` and `SUM(input_tokens)`, so mixed semantics slightly skew per-role
   call counts and token totals (coder undercounts its own parse retries; new
   roles count theirs). **Accepted closeout:** keep coder semantics unchanged and
   document the mixed row semantics in metrics/read-model guidance. No coder path
   change is part of this closeout.

2. **Planner `chunk_number` source.** `run_planner`'s `chunk_number` parameter is
   the value recorded on planner provenance rows (the chunk being planned; default
   `0`).

3. **Cache effectiveness fields.** Cache hit/miss or cache-read/cache-creation
   token fields remain out of scope because they require `LLMResponse` and
   provider-adapter changes. `selection_source` remains `None`.
