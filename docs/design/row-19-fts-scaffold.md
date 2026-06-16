# Row 19 FTS — PR-A scaffold — design brief

Status: **Design-only. No code in this slice.** §23 row 19 ("Retriever interface
+ FTS rung 1 + §5 conformance suite", Memory; depends on Row 12, which is
complete). This brief scopes **PR-A only**: a dormant, derived SQLite FTS5 index
over approved memory-fact content, wired to nothing. It is the first of three
slices; PR-B (retriever interface + §5 conformance suite) and PR-C (BM25 fusion
into selection) are framed at the end but explicitly out of PR-A scope.

Scope owner: memory roadmap (retrieval ladder). Predecessor design:
`docs/design/sqlite-vector-memory-readiness.md` (#32G — the adopted readiness
contract). Successor: Row 23 vector rung 2 (out of scope here).

## Non-authority invariant (read first — this governs the whole slice)

> **The FTS index is derived, rebuildable data over `memory_facts`. It is never a
> source of truth, never an authority channel, and in PR-A it has no runtime
> reader at all.**

Concretely, PR-A must preserve all of the following exactly as they are today:

- **Injection is unchanged.** `prompt_builder.build_project_memory_block` /
  `_detailed` and `memory_store.load_hard_facts` continue to select memory exactly
  as today. Nothing in PR-A reads the FTS index. The injected block is
  byte-identical before and after this slice.
- **No retrieval, ranking, or fusion.** No BM25, no rung-0/rung-1 fusion, no
  reordering. That is rung-1 retrieval (PR-C), not the scaffold.
- **No approval / execution / final-approval / Git / PR behavior change.** The
  index touches none of these paths. The §16D conflict gate and all approval gates
  are untouched.
- **The #32G §5 retrieval safety contract is adopted verbatim, not edited.** Even
  with no reader, the build query mirrors the canonical injection filter so the
  index can never become a place where excluded memory accumulates.

## 1. What Row 19 FTS means in the current architecture

Row 19 is **rung 1 of the #32G retrieval ladder**: a SQLite **FTS5** keyword index
over **approved memory-fact content**, eventually fused with the deterministic
rung-0 signals (path/token overlap, role policy, mandatory tier) behind a
`MemoryRetriever` seam between "load candidate rows" and "select tiers" in
`prompt_builder`. It is **memory retrieval** — not repo/code search (that is the
separate `file_index`), not the run/chunk/turn thread log, and not vectors (Row 23
is rung 2). Per #32G §3–§4 and §10, the index is **a derived index over
`memory_facts`, never the source of truth**: it is rebuildable from canonical
facts, exactly like the existing disposable `file_index`.

The full row (interface + FTS + conformance suite + fusion) is too large for one
PR. PR-A is the smallest piece that de-risks the FTS-specific unknowns —
FTS5 availability, table shape, and deterministic rebuild-from-canonical — without
touching the live prompt path.

## 2. PR-A scope

A **dormant derived FTS5 index scaffold with an explicit rebuild lifecycle and
zero readers.** Concretely, during implementation:

- A capability-guarded `CREATE VIRTUAL TABLE IF NOT EXISTS … USING fts5(...)`,
  created only when FTS5 is available (see §5). Standalone / own-content table
  keyed by `fact_id`, with `project_id` and `content_hash` stored UNINDEXED and
  `content` as the only tokenized column.
- A single `rebuild_memory_fts(project_id)` function that DELETE+INSERTs rows from
  canonical `memory_facts` using the same filter as injection (active,
  project-scoped, non-stale). **Called only by tests in PR-A** — no runtime path
  invokes it.
- A `_ensure_memory_fts_shape(conn)` helper in `database.py`, called from
  `_migrate_db`, following the existing idempotent `_ensure_*_shape` pattern.

Nothing reads the index. `prompt_builder` and `memory_store` selection paths are
untouched. The slice proves table shape, the FTS5 capability probe, and
deterministic rebuild — the genuinely failure-prone parts — while remaining inert
and fully reversible (drop + rebuild).

## 3. Why this is not a search endpoint yet

A `GET` memory-search endpoint would add, in slice one: a new route; parsing of an
**untrusted user query string into an FTS5 `MATCH` expression** (a syntax/abuse
surface); and a response that **returns memory content** to a caller. None of that
is needed before the index exists and before a `MemoryRetriever` seam enforces the
§5 contract (project-scope-before-rank, status exclusion, advisory-only). Shipping
the endpoint first would put a read surface in front of an unproven index. The
endpoint, if it is ever wanted, lands after the retriever and its conformance
suite — never in PR-A.

## 4. Why no runtime flag in PR-A

The index is **dormant by construction**: no runtime reader exists, so there is
nothing to toggle. A default-off flag gating a no-op would be buried magic — a
constant that controls nothing observable. The first meaningful default-off flag
belongs in **PR-B**, where the retriever becomes selectable; rung 1 only becomes
the default much later (per the row, after the M5 suggestion-quality gate /
order-row 7 has soaked). PR-A therefore adds **no policy flag**. Index population
is not a runtime behavior to gate; it is an explicit `rebuild_memory_fts` call
exercised only by tests.

## 5. FTS5 capability / guarding decision

**The FTS5 DDL must not live in `backend/db/schema.sql`.** `schema.sql` is executed
unconditionally via `executescript` in `init_db`. A `CREATE VIRTUAL TABLE … USING
fts5` statement there raises `no such module: fts5` on any CPython/SQLite build
without FTS5 compiled in — **breaking startup for every user on that build.**

Decision: the virtual-table DDL lives in a **capability-guarded
`_ensure_memory_fts_shape(conn)` in `database.py`**, following the existing
`_ensure_*_shape` pattern. Implementation probes FTS5 once (e.g. attempt a
throwaway `CREATE VIRTUAL TABLE … USING fts5` in a savepoint, or inspect
`PRAGMA compile_options` / `sqlite_compile_options`); if FTS5 is absent, the helper
**creates nothing and no-ops** — `init_db` must never crash. The local development
environment has FTS5 (SQLite 3.45.1), but availability is treated as not
guaranteed. When FTS5 is unavailable, the future retriever falls back to rung 0
(deterministic); PR-A simply has no index.

## 6. Standalone explicit-rebuild table; no triggers

Decision: a **standalone (own-content) FTS5 table populated by explicit rebuild** —
**not** an external-content (`content='memory_facts'`) table and **not** SQLite
triggers on the `memory_facts` write path.

Rationale:

- **No write-path blast radius.** Triggers or external-content coupling would touch
  the hot mutation functions in `memory_store.py` (add / update / supersede /
  archive / mark-stale). PR-A keeps those byte-identical. Incremental maintenance
  is a PR-B concern, shipped alongside the reader that needs it.
- **Decoupled and trivially recoverable.** A standalone table is rebuilt with a
  plain DELETE+INSERT from canonical facts; there is no rowid coupling to
  `memory_facts` and no trigger ordering to reason about.
- **`content_hash` stored UNINDEXED** to enable drift detection in PR-B without any
  PR-A reader.

Virtual tables cannot be `ALTER`ed; a future shape change is drop+recreate. That is
acceptable precisely because the index is derived/rebuildable — the same posture as
the existing `file_index` (treated as disposable cache, rebuilt on shape mismatch).
FTS5's internal shadow tables (`*_data`, `*_idx`, `*_content`, `*_docsize`,
`*_config`) are name-distinct and do not collide with the existing name-based
`_table_exists` / `PRAGMA table_info` / `list_*` queries.

## 7. What is indexed / what is not

**Indexed (searchable content first):** only `memory_facts.content` of **active,
project-scoped, non-stale** facts — the exact filter `load_hard_facts` already
enforces (`project_id = ?`, `status = 'active'`, `is_stale = 0`). `category`,
`scope`, and `content_hash` are stored UNINDEXED as future filter/drift metadata;
`content` is the only tokenized text. Content is already secret/PII-gated at
approval time by the `memory_store` write gate, so the index inherits a gated
corpus.

**Not indexed (in PR-A or as a standing rule for this index):**

- `memory_suggestions` (pending / rejected / approved-pending) — only approved
  active facts, mirroring "suggestions are never embedded" (#32G §11.2).
- Stale, archived, and historical facts.
- Anything cross-project.
- `evidence_excerpt`, `rationale`, diffs, stack traces, run-outcome trivia.
- Run / chunk / turn / thread conversational content.
- Repo source / `file_index` content (that is a different index).
- Embeddings / vectors (Row 23).

## 8. Risks

- **FTS5 availability.** Not guaranteed on every CPython/SQLite build (present
  locally). Mitigation: capability probe + no-op fallback; DDL out of `schema.sql`;
  `init_db` must never crash (§5).
- **Migrations.** Additive + idempotent only, via `_ensure_memory_fts_shape`.
  Virtual tables can't be `ALTER`ed → future shape change is drop+recreate, safe
  because the index is derived/rebuildable. Shadow tables are name-distinct and do
  not collide with existing inspection queries (§6).
- **Stale index.** The core FTS hazard, **neutralized in PR-A by construction**:
  nothing reads the index, and rebuild-from-canonical is the recovery path.
  `content_hash` is stored to enable PR-B drift detection. All incremental
  write-path maintenance is deferred.
- **Privacy / secrets.** Only already-gated canonical `content` is indexed; never
  suggestions, evidence, diffs, or stack traces. The index is derived data and is
  already inside the #32G §10 "rebuildable, do not back up / commit" bucket; shadow
  tables live inside the existing DB file, so no new gitignore entry is required.
- **Performance.** Corpus is tens of facts; build is negligible and off the hot
  path. The migration only *ensures the table*; it does not rebuild the corpus on
  every boot.
- **UI scope.** None — backend-only, no route, no frontend.

## 9. Likely files touched (during implementation, not this brief)

- `backend/db/database.py` — new capability-guarded `_ensure_memory_fts_shape(conn)`
  + a call site in `_migrate_db`; FTS5 capability probe.
- `backend/memory/memory_fts.py` *(new)* — capability check, `rebuild_memory_fts`,
  the canonical-mirror build query, an internal scoped count/probe helper. Index
  lifecycle only; no ranking / retrieval.
- `backend/tests/test_memory_fts_scaffold.py` *(new)* — targeted tests (§10).
- **Not** `backend/db/schema.sql` (§5). **Not** `backend/memory/prompt_builder.py`
  or `backend/memory/memory_store.py` selection paths.

## 10. Targeted tests (for PR-A implementation)

- Capability probe returns true locally; simulated-absent path → no table created,
  no crash in `init_db`.
- `_ensure_memory_fts_shape` is idempotent (run twice → no error, one table).
- Rebuild populates **exactly** active / project-scoped / non-stale facts; excludes
  stale, archived, historical, cross-project facts, and all suggestions.
- Rebuild is deterministic / idempotent (twice → identical rows); after
  archive / supersede + rebuild, the dropped fact is absent (reflects canonical
  lifecycle).
- Internal scoped `MATCH` (test-only) never returns another project's fact —
  project-scope-before-rank, even with no runtime reader.
- `init_db` succeeds on both a fresh DB and a pre-existing DB.
- **Dormancy guard:** `build_project_memory_block(...)` output is byte-identical
  with and without the FTS table present — proves zero behavior change.

## 11. Explicit non-goals

- No `MemoryRetriever` interface; no wiring into `prompt_builder`.
- No effect on memory selection, ordering, or the injected block.
- No search route / endpoint.
- No frontend.
- No BM25 / ranking / fusion.
- No write-path triggers or incremental index maintenance.
- No runtime population except the explicit `rebuild_memory_fts` callable used by
  tests; no runtime flag.
- No vectors / embeddings (Row 23).
- No cross-project / suggestion / evidence / diff / thread / repo-file indexing.
- No default-on activation; no change to approval / chunk-plan / final-approval /
  execution / Git / PR behavior; scope and path safety unchanged.
- No edit to the #32G §5 contract (adopted verbatim).
- No FTS DDL in `schema.sql`.

## 12. Sequel framing (so PR-A stays single-purpose)

- **PR-B** — `MemoryRetriever` interface + the #32G §5 **conformance test suite** +
  rung-0 (existing deterministic selection) behind the seam, **byte-identical** +
  incremental index maintenance on the write path. Introduces the first default-off
  flag (the retriever is selectable but off by default).
- **PR-C** — rung-1 BM25 keyword relevance **fused with rung-0 signals**, wired into
  selection behind the default-off flag; deterministic given the corpus; provenance
  records rung identity and score per included entry. Rung 1 only becomes the
  default after the M5 suggestion-quality gate (order-row 7) has soaked.

PR-A smuggles none of PR-B/PR-C. It is the inert foundation they build on.
