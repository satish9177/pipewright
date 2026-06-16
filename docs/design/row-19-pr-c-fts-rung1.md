# Row 19 FTS — PR-C: rung-1 retrieval behind a default-off flag — design brief

Status: **Design-only. No code in this slice.** §23 row 19 ("Retriever interface
+ FTS rung 1 + §5 conformance suite", Memory; depends on Row 12, which is
complete). This brief scopes **PR-C only**: introduce a **rung-1 SQLite FTS5
keyword relevance signal**, fused into memory selection behind a real
**default-off** flag, while preserving every safety contract. It is the third and
final slice. Predecessors: `docs/design/row-19-fts-scaffold.md` (PR-A, merged —
the dormant FTS5 index) and `docs/design/row-19-pr-b-retriever-seam.md` (PR-B,
merged — the `MemoryRetriever` seam with a byte-identical rung-0).

Scope owner: memory roadmap (retrieval ladder). Contract source:
`docs/design/sqlite-vector-memory-readiness.md` (#32G §5, adopted verbatim, not
edited).

## Accepted decisions (govern this whole slice)

- Add `PIPEWRIGHT_MEMORY_FTS_RETRIEVAL_ENABLED`, **default false**.
- Flag resolution lives in `backend/pipeline/policy.py`.
- **Flag-off is byte-identical** to current rung-0 behavior.
- Flag-on chooses the FTS **rung-1 retriever, but rung-1 wraps deterministic
  rung-0 as the canonical safety spine.**
- FTS is an **advisory ordering signal only.**
- FTS must **never add / drop / cap / cross-project** candidates.
- Mandatory / safety facts must **never be scored, omitted, demoted, or dropped.**
- No endpoint. No frontend.
- No rebuild-on-write. No lazy rebuild-on-read. No `memory_store` mutation-path
  changes.
- No `schema.sql` FTS DDL.
- No Row 23 vector memory.
- No approval / execution / final-approval / Git / PR behavior changes.
- PR-C uses **explicit rebuild / test setup only**; automatic freshness is a
  **named follow-up** after rung-1 soaks.
- **D-PRC-1 resolved: explicit-rebuild-only in PR-C.**

## Non-authority invariant (read first — this governs the whole slice)

> Rung-1 never replaces the canonical candidate set; it only attaches an
> **advisory ordering signal** over the **non-mandatory relevance tier**. The
> canonical active / project-scoped / non-stale load (rung-0) is the permanent
> spine and the permanent fallback. FTS can never add, drop, cap, cross-project,
> or reorder a mandatory (safety / pinned) fact. **Flag-off is byte-identical to
> today.** Memory remains advisory: source code, explicit user instruction,
> tests, and safety rules win on conflict. FTS is never an authority channel for
> scope, approval, Git, merge, provider, or safety.

## Two grounding facts from the as-built code (read before designing)

These two facts from the live code shape the entire design; designing without
them produces either a silent no-op or a broken test suite.

1. **`prompt_builder` re-sorts whatever the retriever returns.**
   `build_project_memory_block_detailed` calls `in_policy_rows.sort(...)`
   (`prompt_builder.py:497`) and then `_partition_relevance_rows` re-orders the
   relevance tier by path-overlap → Jaccard → base key. **Any row order imposed
   inside the retriever is discarded downstream.** Therefore the *only* channel
   by which FTS can affect the rendered block is an **additive relevance signal
   consumed by the downstream relevance step** — not row reordering in the
   retriever. An implementation that "returns rows in BM25 order" will appear to
   work and change nothing.

2. **`test_rung_zero_ignores_request_context` is deliberately rung-0-only.** It
   asserts deterministic candidates are identical with and without
   `request_context`. Rung-1 *uses*
   `request_context`, so it **cannot** be added to that test's params. It must
   stay rung-0-only. Every other §5 contract test calls with
   `request_context=None`, where rung-1 ≡ rung-0, so those parametrize over both
   rungs safely.

---

## 1. Non-authority invariant

Stated above (read-first block). In one line: **rung-1 is an advisory ordering
overlay on the relevance tier; rung-0 is the canonical spine and fallback; the
mandatory tier and the flag-off path are untouchable.** This is the #32G §5
contract restated for keyword retrieval: *filter by project before rank; exclude
archived / rejected / stale; never cross-project; apply safety filters before or
together with ranking, never after; memory never overrides source / user /
tests / safety.*

## 2. Flag name and policy location

- **Flag:** `MEMORY_FTS_RETRIEVAL_ENABLED`, env
  `PIPEWRIGHT_MEMORY_FTS_RETRIEVAL_ENABLED`, **default `False`**. Parallels the
  existing `MEMORY_RELEVANCE_OMISSION_ENABLED` / `MEMORY_POSTRUN_HYGIENE_ENABLED`
  naming.
- **Location:** `backend/pipeline/policy.py`, the single source of truth for
  behavioral flags, via the existing `_env_flag_enabled(...)` helper. Add a
  `# --- Memory FTS rung-1 retrieval (memory_retriever.py, Row 19 PR-C) ---`
  block with `MEMORY_FTS_RETRIEVAL_ENV` + `MEMORY_FTS_RETRIEVAL_ENABLED`, and any
  fusion weight / query-token cap as **named policy constants** (no buried magic
  numbers).
- **Import:** `memory_retriever.py` adds `from backend.pipeline import policy as
  pipeline_policy`. No cycle — `policy.py` imports nothing from the backend, and
  `prompt_builder` already imports it the same way.

## 3. Resolver behavior

`default_memory_retriever()` reads the flag **at call time** (attribute access,
monkeypatchable exactly like `MEMORY_RELEVANCE_OMISSION_ENABLED` is today):

```python
def default_memory_retriever() -> MemoryRetriever:
    if pipeline_policy.MEMORY_FTS_RETRIEVAL_ENABLED:
        return _FTS_MEMORY_RETRIEVER       # rung-1, composes rung-0
    return _DEFAULT_MEMORY_RETRIEVER        # rung-0 (unchanged)
```

- Reading the flag at call time (not import time) means a test or operator can
  flip `pipeline_policy.MEMORY_FTS_RETRIEVAL_ENABLED` and the next call observes
  it. Both rungs are module-level singletons (no per-call allocation).
- `FTSMemoryRetriever` **composes** `DeterministicMemoryRetriever` — it calls
  rung-0 for the spine — so rung-0 remains the one definition of the safe
  candidate load. There is no second copy of the candidate SQL.

## 4. Rung-1 combination strategy

Rung-1 produces candidates in three steps, of which only the third is new
behavior, and only when the flag is on:

1. **Spine (rung-0).** Call `DeterministicMemoryRetriever.retrieve_candidates`
   for the full in-policy / out-of-policy set. This is the complete, safe,
   project-scoped, active-only candidate load — never narrowed by FTS.
2. **Signal (FTS).** If FTS5 is available *and* `request_context` carries usable
   signal, derive sanitized query tokens (§7), run project-scoped `MATCH`
   probes, and collect **summed `-bm25` scores keyed by `fact_id`**. As
   implemented, the sanitized query is a capped tuple of bareword tokens; the
   rank helper runs one safe `MATCH` per token and sums `-bm25` scores per fact.
3. **Attach (additive).** Attach those scores to a new **optional** field on
   `RetrievedCandidates`:

   ```python
   @dataclass(frozen=True)
   class RetrievedCandidates:
       in_policy_rows: list[dict]
       out_of_policy_rows: list[dict]
       relevance_scores: dict[str, float] | None = None  # fact_id -> BM25; None = rung-0
   ```

   The candidate **lists are unchanged and complete.** `relevance_scores` is
   `None` for rung-0 (and whenever FTS yields nothing), which keeps the field
   inert.

**Where fusion takes effect:** `prompt_builder._partition_relevance_rows` (the
single place relevance ordering lives) consumes `relevance_scores` **only when
present**, blending BM25 as a **co-signal** with the existing path-overlap →
Jaccard signal into one fused relevance value. Path overlap, Jaccard, and the
existing `_memory_row_sort_key` remain deterministic tiebreakers. The FTS
contribution is controlled by a single named policy weight. When
`relevance_scores is None`, the existing code
path is taken unchanged → **byte-identical**.

**Co-signal, not replacement**, is deliberate: FTS may be empty/unavailable and
must degrade to *exactly* rung-0, and path-overlap already serves file-scoped
requests well. BM25 is deterministic given the corpus, so fused ordering stays
deterministic (§5 conformance).

## 5. Mandatory / safety protection

- The mandatory tier (`_is_mandatory_row`: `security` / `forbidden_paths`
  always; pinned facts when omission is on) is **split off before any relevance
  scoring** (`prompt_builder.py:509-510`) and is never scored, omitted,
  reordered by relevance, or budget-dropped. FTS scores apply **only** to the
  non-mandatory relevance tier.
- The retriever returns the **full** candidate set (no top-k, no cap). FTS
  contributes a score map, never a filter.
- A safety fact that does not match the FTS query is still mandatory and still
  injected; a safety fact that *does* match cannot be promoted out of, or
  demoted within, the mandatory tier — scoring never touches it.
- **Standing guard:** the existing
  `test_retriever_does_not_cap_or_drop_mandatory_candidates` is extended to run
  against rung-1, asserting the full in-policy set (safety + pinned + relevance)
  is returned.

## 6. Project-scope-before-rank guarantee

- The spine load is `WHERE project_id = ?` (rung-0) — scope before anything.
- The FTS rank probe goes through `_rank_memory_fts_for_project(project_id, …)`,
  which enforces `AND project_id = :project_id` **alongside** each `MATCH`
  (SQL-level scope filter applied *with* the rank query, never after a blind
  top-k — exactly #32G §5's vector clause applied to keyword search).
- Fusion attaches scores only by **intersection on `fact_id`** with the
  canonical set; a `fact_id` not already in this project's active set cannot
  enter (and cross-project rows never reach the match in the first place).
- Blank / whitespace `project_id` → empty result (guarded both in rung-0 and in
  `_validate_project_id` inside the FTS helper); contract-tested over both rungs.

## 7. Safe FTS query construction

- **Request-context only.** `_fts_query_tokens` builds the query from
  `RequestContext` (`title`, `description`, `steer_text`, and
  `files_expected`). It does **not** use `memory_trust._content_tokens`; PR-C
  keeps a small local tokenizer so FTS query construction stays explicit and
  cycle-free.
- **Sanitize to barewords.** Strip column-filter-shaped fragments first, then
  extract lowercase `[a-z0-9_]+` tokens. Drop FTS operators (`NEAR`, `AND`,
  `OR`, `NOT`), very short tokens, punctuation, quotes, stars, dashes, colons,
  and duplicates. Cap the token count with `MEMORY_FTS_QUERY_TOKEN_CAP`. Empty after
  sanitization → no `MATCH` → fall back to rung-0 (no scores).
- **One safe `MATCH` per token.** `_rank_memory_fts_for_project` executes one
  parameterized, project-scoped `MATCH` per sanitized bareword token and sums
  `-bm25(memory_facts_fts)` per `fact_id`. It does **not** build one OR-joined
  query string.
- **Raw request text never reaches `MATCH`.** This neutralizes the
  "untrusted query string → FTS5 `MATCH` expression" abuse surface that PR-A §3
  flagged as the reason not to ship a search endpoint. Any residual `MATCH`
  error is caught and treated as "no FTS signal" → rung-0 fallback, never
  surfaced.

## 8. FTS unavailable / empty index / stale index behavior

- **FTS5 unavailable** (`_sqlite_fts5_available` is false): the match helper
  returns `[]` → no scores → output **identical to rung-0**, even with the flag
  on. Rung-0 is the permanent fallback (per PR-A §5 / PR-B).
- **Empty / missing index table**: already guarded
  (`_memory_fts_table_exists`, empty-query short-circuit) → `[]` → rung-0
  ordering, no crash.
- **Stale index** (canonical `memory_facts` changed since the last rebuild) — the
  core FTS hazard, neutralized because FTS is overlay-only: fusion **intersects
  FTS hits with the canonical active set by `fact_id` and verifies the stored
  UNINDEXED `content_hash` against the canonical row.** A hit for a fact that was
  archived / superseded / edited since the rebuild is **ignored** (no score). The
  canonical spine is always authoritative for *which* facts exist and *what they
  say*; FTS only orders among them. **Worst case of staleness is degraded
  ordering, never a wrong, dropped, or up-ranked fact** — which makes index
  freshness a *quality* property, not a *safety* one (the premise for §9).

## 9. Explicit-rebuild-only decision (D-PRC-1)

**Decision: explicit-rebuild-only in PR-C.** No rebuild-on-write triggers in
`memory_store`; **no lazy rebuild-on-read.**

Rationale:

- **Smallest safe change.** `memory_store` mutation functions (add / update /
  supersede / archive / mark-stale) stay byte-identical, matching the PR-A/PR-B
  posture and the safety-contract preference against touching hot write paths.
- **No write on the read path.** `build_project_memory_block_detailed` documents
  itself as performing **no writes**; a lazy rebuild-on-read would violate that
  contract and risk "database is locked" contention on the prompt path. So the
  reader stays pure.
- **Staleness is already safe.** The hash-verified overlay (§8) means a stale or
  empty index can only *weaken ordering*, never inject or drop a fact. Guaranteed
  freshness is therefore not required for a correct, safe rung-1.

**Honest consequence (stated, not hidden):** with explicit-rebuild-only, the
index is populated only by the explicit `rebuild_memory_fts(project_id)` callable
(exercised by tests / an operator), so **flag-on is a production no-op until a
populate trigger ships** — consistent with this project's ship-dormant-then-
activate pattern (17a/17b prompt cache, Row 12 relevance omission). PR-C delivers
the rung-1 *mechanism* (flag, fusion, sanitized query, staleness tolerance,
conformance); runtime *activation* is the named follow-up in §13.

## 10. Tests required

Targeted only; `pytest.mark.unit`. FTS-dependent tests skip when FTS5 is
unavailable, mirroring `test_memory_fts_scaffold.py::_require_fts5`.

- **Resolver:** flag off → `default_memory_retriever()` is rung-0; flag on →
  rung-1.
- **Byte-identity off (headline):** with the flag off, block bytes **and**
  included / excluded provenance entries are identical to current behavior,
  across roles / budgets / scopes, with and without `request_context` (reuse the
  `FrozenDateTime` approach + the PR-A/PR-B dormancy guard).
- **§5 conformance, parametrized over [rung-0, rung-1]:** project-scope
  isolation; stale / archived / historical excluded; suggestions never returned;
  blank `project_id` → empty; **mandatory candidates never capped or dropped**
  (rung-1 returns the full set).
- **Rung-0-only (unchanged):** `test_rung_zero_ignores_request_context` stays on
  a rung-0 fixture (see grounding fact #2).
- **Rung-1-specific:**
  - request signal changes relevance **order** but not the candidate **set**;
    mandatory facts still present and un-demoted;
  - FTS5 unavailable → rung-1 output ≡ rung-0;
  - empty / missing index → rung-1 output ≡ rung-0, no crash;
  - **stale index** — a fact archived / superseded after rebuild, or with a
    `content_hash` mismatch, is not injected and not up-ranked (canonical spine
    wins);
  - **query sanitization** — `request_context` containing FTS5 operators /
    quotes / `*` / `NEAR` / `:` / `-` does not raise and does not MATCH-inject; a
    benign token still matches;
  - a mandatory fact with zero FTS score is still injected, still in the
    mandatory tier.

## 11. Likely files touched

- `backend/pipeline/policy.py` — flag + fusion-weight / query-token-cap
  constants.
- `backend/memory/memory_retriever.py` — `FTSMemoryRetriever` (rung-1),
  flag-aware resolver, additive `relevance_scores` field, safe query builder,
  fallback logic.
- `backend/memory/memory_fts.py` — a **private**, project-scoped, rank-returning
  helper (`_rank_memory_fts_for_project`) that runs one safe `MATCH` per token,
  sums `-bm25(...)`, and returns `content_hash` for staleness verification.
- `backend/memory/prompt_builder.py` — thread and consume `relevance_scores` in
  `_partition_relevance_rows`, gated so `None` → byte-identical.
- `backend/tests/test_memory_retriever.py` — extend the conformance params to
  both rungs; scope the rung-0-only test; new
  `backend/tests/test_memory_fts_retrieval.py` for fusion / sanitization /
  staleness.
- `docs/design/row-19-pr-c-fts-rung1.md` (this brief) + the `MEMORY.md` pointer.

## 12. Explicit non-goals

- No endpoint / route; no public search API.
- No frontend; no search UI; no thread UI.
- No vectors / embeddings (Row 23).
- No `schema.sql` FTS DDL.
- **No default-on** — the flag ships `False`; rung-1 becomes the default only
  after the M5 suggestion-quality gate (order-row 7) has soaked, per the row.
- No rebuild-on-write; no lazy rebuild-on-read; no `memory_store` mutation-path
  change.
- No change to the mandatory tier, relevance-omission semantics, token budgets,
  approval / chunk-plan / final-approval, execution, Git, PR, scope, or path
  safety.
- No change to rung-0 behavior; no `prompt_builder` behavior change when the flag
  is off.
- No edit to the #32G §5 contract (adopted verbatim).
- No cross-project / suggestion / evidence / rationale / diff / stack-trace /
  thread / repo-file indexing.
- Optional and out of the minimal slice: surfacing rung id / score into
  `InjectedMemoryEntry` provenance (PR-B foreshadowed it). It can be added
  additively later without behavior change; PR-C does not require it.

## 13. Follow-up note — future rebuild / populate trigger

Because PR-C is explicit-rebuild-only (§9), **flag-on stays inert in production
until an index-populate trigger lands.** That activation is a deliberately
separate, named follow-up to be designed and soaked on its own:

- **Lowest-blast-radius candidate:** rebuild a project's index at the
  **memory-approval write path** (an *existing* route in `backend/routes/memory.py`
  — not a new endpoint), since approval is the low-frequency, human-gated moment
  when a project's canonical facts actually change. A rebuild failure there must
  never break the approval (best-effort, wrapped, logged).
- **Sequence:** (a) ship PR-C dormant; (b) soak rung-1 fusion behind the flag in
  dev with an explicit rebuild; (c) add the populate trigger as its own slice;
  (d) flip `MEMORY_FTS_RETRIEVAL_ENABLED` to default-on only after the M5
  order-row-7 suggestion-quality gate has soaked.
- **Row 23** then reaches vector rung-2 by swapping the retriever implementation
  behind the same seam — no change to this contract.

PR-C smuggles none of the activation work. It is the safe, default-off rung-1
mechanism the activation slice will switch on.
