# Row 19 FTS — populate / soak trigger — design brief

Status: **Design-only. No code in this slice.** Follow-up to §23 row 19 ("Retriever
interface + FTS rung 1 + §5 conformance suite", Memory). Predecessors, all merged:
`docs/design/row-19-fts-scaffold.md` (PR-A — dormant FTS5 index),
`docs/design/row-19-pr-b-retriever-seam.md` (PR-B — `MemoryRetriever` seam, rung-0
byte-identical), `docs/design/row-19-pr-c-fts-rung1.md` (PR-C — rung-1 fusion
behind `PIPEWRIGHT_MEMORY_FTS_RETRIEVAL_ENABLED`, default-off). Contract source:
`docs/design/sqlite-vector-memory-readiness.md` (#32G §5, adopted verbatim, not
edited).

Scope owner: memory roadmap (retrieval ladder). This brief scopes **only** the
explicit, default-off way to *populate / refresh* the SQLite FTS index and to
*measure* rung-0 vs rung-1 advisory ordering for a soak. It builds the tooling
PR-C §13 sequence step (b) ("soak rung-1 fusion behind the flag in dev with an
explicit rebuild") needs, and deliberately stops short of PR-C §13's
production-activation trigger.

## Non-authority invariant (read first — governs the whole slice)

> Populating the FTS index changes **nothing** about memory retrieval. The index
> is derived, rebuildable data over `memory_facts`; it is read only when
> `MEMORY_FTS_RETRIEVAL_ENABLED` is on, and even then only as an **advisory
> ordering signal over the non-mandatory relevance tier** (PR-C). This slice adds
> an **explicit operator/test entrypoint** that calls the *existing*
> `rebuild_memory_fts(project_id)` and a **read-only comparison harness**. It adds
> no runtime trigger, no write-path hook, no read-path rebuild, no endpoint, no
> frontend, no `schema.sql` DDL. Flag-off output stays byte-identical to today;
> the candidate set is never replaced, only (optionally, flag-on) re-ordered
> within the relevance tier. Memory stays advisory: source, user instruction,
> tests, and safety rules win on conflict.

## Two grounding facts from the as-built code (read before designing)

1. **The populate mechanism already exists.** `rebuild_memory_fts(project_id)` in
   `backend/memory/memory_fts.py` is a complete, project-scoped DELETE+INSERT from
   canonical facts (active / project-scoped / non-stale only), capability-guarded
   (returns `0` when FTS5 is absent), validated via `_validate_project_id`, and
   wrapped in its own `engine.begin()` transaction. It is invoked **only by tests**
   today. This slice therefore adds an **entrypoint and a soak harness, not a new
   index-builder.** Duplicating the build query is a non-goal.

2. **Rung-1 affects only ordering, and only of the relevance tier.** Per PR-C §4
   grounding fact #1, `prompt_builder` re-sorts whatever the retriever returns;
   FTS reaches the rendered block solely through `relevance_scores` consumed by
   `_partition_relevance_rows` (`prompt_builder.py:218,237`). The mandatory tier
   (`security` / `forbidden_paths`, plus pinned facts when omission is on) is split
   off *before* any scoring and is never scored, reordered, or dropped. So the only
   thing a soak can legitimately observe differing between flag-off and flag-on is
   **the order of the non-mandatory relevance entries** in
   `MemoryBlockBuildResult.included_entries`. The included *set* and the mandatory
   tier must be identical. A soak that reports any other delta is measuring a bug.

---

## 1. What this is, and how it relates to PR-C §13

PR-C shipped the rung-1 *mechanism* default-off and honestly noted (§9) that with
explicit-rebuild-only, **flag-on is a production no-op until an index is
populated.** PR-C §13 then named a *production-activation* follow-up whose
lowest-blast-radius candidate was rebuilding at the **memory-approval write path**
(an automatic trigger on an existing route).

This brief is the **step before that** — PR-C §13 sequence (b). Its job is to let
us *populate on demand* and *measure whether rung-1 ordering is worth anything*
before we commit to any automatic trigger or a default-on flip. It is intentionally
**more conservative** than §13: explicit/manual only, no route, no automatic
freshness. The §13 approval-write-path trigger remains a **separate, later** slice
and must not be smuggled in here (see §10).

## 2. Why a soak is non-trivial here (honest expected outcome)

The as-built dev stores are tiny: the Row 12 reality check recorded a max
non-mandatory relevance count of **3 « grace 12**, with **0** security /
forbidden / pinned facts, and `MEMORY_RELEVANCE_OMISSION_ENABLED` is off. With
omission off and ≤3 relevance facts that all fit the role budget, reordering the
relevance tier changes the *order* of at most a handful of entries and **drops
nothing** — so on a real dev store the soak will very likely show a **near-zero
rendered delta.** That is itself a finding (it tells us rung-1 activation is
low-value until stores grow), and it is exactly the silent-no-op failure mode PR-C
grounding fact #1 warns about. Therefore the soak must run in **two modes** (§8):
over a real project (measures real-world delta, expected ~0) **and** over a seeded
synthetic corpus large enough to actually exercise BM25 ordering (validates the
mechanism orders sensibly). A soak that only runs the first mode can "pass" while
proving nothing.

## 3. (Q1) What triggers FTS population

**An explicit, human-invoked command — nothing else.** Population happens only
when an operator runs the rebuild script (or a test calls the existing function).
Specifically **not**:

- not a `memory_store` write-path trigger (rebuild-on-write) — see §6;
- not a `prompt_builder` / retriever read-path rebuild (lazy rebuild-on-read) — §6;
- not a boot/migration-time populate — `_ensure_memory_fts_shape` keeps only
  *ensuring the empty table shape* in `_migrate_db`; it must not gain a populate
  call;
- not the PR-C §13 approval-write-path trigger — that is the later activation slice.

Refresh is just re-running the same command; `rebuild_memory_fts` is already a
DELETE+INSERT, so a second run is an idempotent refresh, scoped to the named
project.

## 4. (Q2) Entrypoint form — recommendation: explicit `scripts/` CLI

**Recommended: a thin, explicit CLI in `scripts/` that wraps the existing
`rebuild_memory_fts` backend function, plus a read-only compare mode.** Rationale,
strongest first:

- **Reuse, don't duplicate.** The builder already exists as a backend function;
  the gap is an operator-runnable entrypoint and a measurement harness.
- **`scripts/` is the established home** for guarded operator helpers
  (`find_runs.py`, `reset_smoke_repo.py`) and is runnable against a live dev DB
  **across sessions** — which a multi-day soak needs.
- **Not an endpoint/route.** A route adds an HTTP surface, auth concerns, and a
  rebuild reachable at runtime — rejected by PR-A §3 and by the user's direction;
  unjustified for a soak tool. (If automatic freshness is ever wanted, that is the
  §13 approval-write-path slice, not this one.)
- **Not test-helper-only.** Tests already exercise `rebuild_memory_fts`, but a
  pytest fixture cannot drive a longitudinal dev soak or let an operator refresh on
  demand. Tests still cover the script (§9).
- **Not a new admin backend function.** The backend function that matters already
  exists; the script is its CLI shell.

Shape (matches `find_runs.py` / `reset_smoke_repo.py`): `argparse`, `ROOT` on
`sys.path`, `init_db()`, returns an int exit code, `raise SystemExit(main())`.
Two modes:

- `rebuild` — `--project-id <id>` (required; or `--all-projects`, which loops and
  rebuilds **each project independently**, never a cross-project mix). Prints the
  per-project row count `rebuild_memory_fts` returns. Refuses without explicit
  confirmation (`--yes`) when not interactive, mirroring `reset_smoke_repo.py`.
- `compare` — **read-only** rung-0-vs-rung-1 soak comparison (§8). Performs no
  writes in real-project mode; in `--seed` mode it builds a throwaway synthetic
  project, compares, and cleans it up.

(Two scripts vs. one script with two subcommands is an implementation detail; one
guarded script with subcommands keeps the surface minimal.)

## 5. (Q3) Flags / config that guard it — recommendation: no new env flag

**Recommended: no new behavioral env flag for the populate action.** Guard with
explicit CLI ergonomics instead:

- a **required** `--project-id` (no implicit "rebuild everything" default);
- a confirmation gate (`--yes` / interactive prompt) for the mutating `rebuild`
  mode, exactly like `reset_smoke_repo.py`;
- an FTS5-availability check up front: if `_sqlite_fts5_available` is false, print
  a clear "FTS5 unavailable — nothing to populate" and exit non-fatally (the
  rebuild already returns `0` in that case).

Why no env flag: a default-off env flag gating an *explicit operator command* is
the buried-magic anti-pattern PR-A §4 and PR-B §11 already rejected — it would gate
nothing observable. The command is **dormant by construction** (it runs only when
invoked), and what it populates is **read only when the existing
`MEMORY_FTS_RETRIEVAL_ENABLED` flag is on.** That retrieval flag stays the single
behavioral switch; the soak `compare` mode flips it **in-process** to produce the
off/on pair. Populating while retrieval is off is harmless: nothing reads the
index, and the corpus is the same already-gated canonical `content` the scaffold
indexes.

Alternative considered and rejected: a dedicated `PIPEWRIGHT_MEMORY_FTS_REBUILD_
ENABLED` guard. It adds a constant that controls nothing the explicit confirmation
+ project-scope requirement doesn't already cover, and defense-in-depth here is
better served by those (you cannot run the command by accident; a populated-but-
unread index is inert).

## 6. (Q4) Guaranteeing no rebuild-on-write and no lazy rebuild-on-read

Both are preserved **by not adding a call**, and asserted by tests:

- **No rebuild-on-write.** The only new caller of `rebuild_memory_fts` is the
  script (and tests). No `memory_store` mutation (`add` / `update` / `supersede` /
  `archive` / `mark_stale`) gains a rebuild call; they stay byte-identical.
  *Test:* run each mutation, then assert the FTS rows are **unchanged** until an
  explicit rebuild (the index legitimately goes stale — and the read path tolerates
  that, §8 / PR-C §8).
- **No lazy rebuild-on-read.** The retriever read path
  (`FTSMemoryRetriever.retrieve_candidates` → `_rank_memory_fts_for_project`) uses
  `engine.connect()` and only ever *reads*; it must not gain a populate. *Test:*
  with the flag on over an **empty or stale** index, `build_project_memory_block*`
  / `retrieve_candidates` returns (degrading to rung-0 ordering) and leaves the FTS
  rows **unchanged** — proving the read did not populate. `_ensure_memory_fts_shape`
  stays shape-only and is not given a populate call site.

The single, auditable populate path is the explicit command. That is the whole
point of the slice.

## 7. (Q5) Guaranteeing project isolation

`rebuild_memory_fts` is already `DELETE … WHERE project_id = :p` then `INSERT …
SELECT … WHERE project_id = :p` inside one transaction, and the rank/MATCH helpers
enforce `AND project_id = :project_id` alongside the match (PR-C §6). The script
preserves this by:

- **requiring an explicit `--project-id`** for `rebuild`, and looping
  **per-project** for `--all-projects` (each project a separate scoped rebuild — no
  global statement that could span projects);
- never issuing an unscoped DELETE/INSERT against `MEMORY_FTS_TABLE`.

*Tests:* rebuilding project A leaves project B's FTS rows **byte-identical**;
`--all-projects` over {A, B} yields exactly each project's own active facts;
the compare harness never surfaces a cross-project fact (reuses the standing
`test_internal_match_helper_is_project_scoped` guarantee).

## 8. (Q6) What to measure during soak

For each (project, role, sampled `RequestContext`), call
`build_project_memory_block_detailed(...)` **flag-off** then **flag-on** and diff
the structured `included_entries` (never scrape the rendered string):

**Safety assertions (must hold every sample — a violation is a stop-the-soak bug):**

- **Included set identical** off vs on (same `fact_id`s) — proves FTS never adds or
  drops a candidate.
- **Mandatory tier identical** in contents *and* order — proves no demotion /
  omission / reordering of safety/pinned facts.
- **No cross-project fact** ever appears.
- **Flag-off bytes == current** behavior (reuse the PR-A/PR-B/PR-C dormancy guard +
  `FrozenDateTime`).

**Quality signal (the actual measurement):**

- **Relevance-tier order delta** — the only legitimate difference. Report it as
  e.g. count of reordered adjacent pairs / Kendall-tau between the off and on
  relevance-entry sequences.
- **FTS coverage** — how often `relevance_scores` is non-empty (request produced
  usable tokens *and* FTS matched something) vs. fell back to rung-0.
- **Fallback rate** — FTS5-unavailable, empty/missing index, empty-after-
  sanitization, and `MATCH`-error fallbacks (all should degrade to exact rung-0).
- **Staleness behavior** — after a canonical edit/supersede/archive **without**
  rebuild, confirm hash-mismatch hits are **ignored** (no up-rank, no inject) — i.e.
  the worst case is degraded ordering, never a wrong/dropped fact (PR-C §8).
- **Performance** — per-project rebuild time; added rank-query latency on the read
  path (expected negligible at tens of facts).

**Two corpora (per §2):** (a) a **real** project — expected near-zero delta, which
is the finding that informs whether activation is worthwhile; (b) a **seeded
synthetic** project with enough facts to make BM25 ordering meaningful — validates
the mechanism orders sensibly and that the delta is non-trivial when the corpus is.

Report format: a short results doc (`docs/design/row-19-fts-soak-results.md` or
similar), matching the `trivial-task-profile-soak.md` precedent.

## 9. (Q8) Tests required for the later implementation PR

Targeted, `pytest.mark.unit`; FTS-dependent tests skip when FTS5 is unavailable,
mirroring `test_memory_fts_scaffold.py::_require_fts5`.

- **Rebuild correctness (reuse scaffold assertions):** the script populates
  **exactly** the project's active / scoped / non-stale facts; excludes stale,
  archived, historical, cross-project, and all suggestions.
- **CLI contract:** `rebuild` requires `--project-id`; refuses to mutate without
  `--yes` in non-interactive mode; `--all-projects` rebuilds each project
  independently; exit codes are sane (`0` success; non-fatal, clearly messaged when
  FTS5 is unavailable).
- **Project isolation:** rebuild A leaves B unchanged (§7).
- **No rebuild-on-write:** mutations do not change FTS rows until explicit rebuild
  (§6).
- **No lazy rebuild-on-read:** flag-on read over empty/stale index does not populate
  and does not crash; degrades to rung-0 (§6).
- **Compare harness (read-only):** off vs on → identical included set + identical
  mandatory tier; only relevance-tier order may differ; deterministic given the
  corpus; never a cross-project fact; flag-off bytes == current.
- **Staleness:** a fact archived/superseded/edited after rebuild (or with a
  `content_hash` mismatch) is not injected and not up-ranked — canonical spine wins.
- **Seed mode hygiene:** the synthetic project is fully cleaned up (facts +
  suggestions + FTS rows), leaving no residue (reuse the `memory_project_ids`
  teardown fixture pattern).

## 10. (Q7) Explicitly out of scope

- **No automatic freshness of any kind:** no rebuild-on-write trigger, no lazy
  rebuild-on-read, no boot/migration-time populate.
- **The PR-C §13 approval-write-path activation trigger** — the separate, later
  production-activation slice. This brief is the soak *prerequisite*, not the
  trigger.
- **No default-on flip** of `MEMORY_FTS_RETRIEVAL_ENABLED`; no change to its
  default. The soak `compare` mode flips it in-process only.
- **No endpoint / route / public search API; no frontend; no thread UI.**
- **No `schema.sql` FTS DDL;** no change to `_ensure_memory_fts_shape` semantics
  (it stays shape-only) or to its `_migrate_db` call.
- **No change to the retriever / fusion logic** — PR-C is frozen. No new ranking,
  no new fusion weight behavior, no provenance schema change.
- **No `memory_store` mutation-path change;** no new public memory API; ideally no
  new public function in `memory_fts.py` (reuse `rebuild_memory_fts`; if a row
  count for reporting is needed, prefer a small *private* read-only helper or reuse
  the test pattern, not a widened surface).
- **No vectors / embeddings (Row 23).**
- **No cross-project rebuild;** no edit to the #32G §5 contract.
- **No approval / chunk-plan / final-approval / execution / Git / PR behavior
  change;** scope and path safety unchanged.

## 11. (Q9) Files touched / must not touch

**Likely touched (during implementation, not this brief):**

- `scripts/rebuild_memory_fts.py` *(new)* — explicit guarded CLI: `rebuild` +
  read-only `compare` subcommands, wrapping the existing `rebuild_memory_fts`.
- `backend/tests/test_fts_populate_soak.py` *(new)* — CLI contract, isolation,
  no-rebuild-on-write/read, compare-harness invariants, staleness, seed hygiene
  (§9).
- `docs/design/row-19-fts-populate-soak.md` *(this brief)* + a soak **results** doc
  + the `MEMORY.md` pointer.

**Must not touch:**

- `backend/db/schema.sql` — no FTS DDL (standing invariant).
- `backend/db/database.py` — `_ensure_memory_fts_shape` / `_migrate_db` stay
  shape-only; **no populate call on the boot/migrate path.**
- `backend/memory/memory_store.py` — all mutation paths byte-identical; **no
  rebuild-on-write.**
- `backend/memory/memory_retriever.py` — read path unchanged; **no lazy
  rebuild-on-read;** PR-C logic frozen.
- `backend/memory/prompt_builder.py` — selection / ordering / budget / render
  unchanged.
- `backend/memory/memory_fts.py` — reuse `rebuild_memory_fts`; do not change the
  build query or widen the public API.
- `backend/pipeline/policy.py` — **no new flag** (recommendation, §5); the
  retrieval flag already exists.
- `backend/routes/*` — no endpoint.
- `frontend/*` — none.

## 12. (Q10) Smallest safe PR split

Both pieces are small; the recommended split keeps each single-purpose and
independently reviewable:

- **PR-1 — explicit populate CLI.** `scripts/rebuild_memory_fts.py` `rebuild` mode
  (project-scoped, guarded, `--all-projects` loop) wrapping the existing function,
  plus the rebuild-correctness / isolation / no-rebuild-on-write/read tests. Changes
  **no** runtime behavior; independently useful (lets an operator populate for any
  manual rung-1 check).
- **PR-2 — read-only soak compare harness.** The `compare` subcommand
  (real-project + `--seed` synthetic), the off-vs-on invariant + ordering-delta
  tests, and the soak **results** doc. Pure measurement; depends on PR-1 to have
  something populated.

PR-1 is independently shippable; PR-2 has no value without it. They are small
enough that a reviewer may reasonably take them together — but the split makes the
mutating tool and the measurement harness separately auditable, which is the safer
default. Neither PR flips a flag or changes retrieval.

## 13. Sequel framing (so this slice stays single-purpose)

- **After the soak:** if the seeded-corpus measurement shows rung-1 ordering is
  worthwhile *and* the safety invariants held, the next slice is PR-C §13's
  **production-activation trigger** (rebuild at the memory-approval write path,
  best-effort/wrapped/logged). Only after *that* soaks does a default-on flip of
  `MEMORY_FTS_RETRIEVAL_ENABLED` become a candidate — and only after the M5
  order-row-7 suggestion-quality gate has soaked, per the row.
- If the real-store delta is ~0 (the likely §2 outcome) and stores stay small,
  the honest conclusion may be to **leave rung-1 dormant** and revisit when project
  memory grows — the soak exists precisely to make that call on evidence.
- **Row 23** later reaches vector rung-2 by swapping the retriever behind the same
  seam — unaffected by this tooling.

This slice smuggles none of the activation work. It is the explicit, default-off
populate + measurement tooling the activation decision depends on.
