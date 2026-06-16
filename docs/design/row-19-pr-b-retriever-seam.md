# Row 19 FTS — PR-B `MemoryRetriever` seam — design brief

Status: **Design-only. No code in this slice.** §23 row 19 ("Retriever interface
+ FTS rung 1 + §5 conformance suite", Memory; depends on Row 12, which is
complete). This brief scopes **PR-B only**: introduce the `MemoryRetriever` seam
and move the existing deterministic candidate load behind it, **byte-identical**,
with the §5 retriever-contract conformance suite. It is the second of three
slices. Predecessor: `docs/design/row-19-fts-scaffold.md` (PR-A, the dormant FTS5
index). Successor: PR-C (rung-1 FTS fused into selection behind a real default-off
flag) — explicitly out of PR-B scope.

Scope owner: memory roadmap (retrieval ladder). Contract source:
`docs/design/sqlite-vector-memory-readiness.md` (#32G §5, adopted verbatim).

## Accepted decisions (govern this whole slice)

- PR-B introduces the **`MemoryRetriever` seam only**.
- PR-B keeps the injected block **byte-identical**.
- PR-B **does not use FTS** (no read of the PR-A index).
- PR-B **does not add a runtime flag**.
- PR-B **does not add write-path FTS maintenance**.
- PR-B does **not** change prompt text, ordering, token budgets, mandatory-memory
  behavior, relevance omission, or request-aware selection.
- PR-B keeps **sorting, tiering, omission, and budget logic downstream** in
  `prompt_builder`.
- PR-B **includes the §5 conformance / retriever-contract tests**.
- **PR-C** will be the first slice that uses FTS, behind a real **default-off**
  flag.

## Non-authority invariant (read first)

> The seam relocates *where* candidate rows are loaded; it changes **nothing**
> about which rows are selected, in what order, or how they are rendered. The
> retriever loads candidates; `prompt_builder` still owns selection. With one
> rung (deterministic) and no flag, the seam is a pure structural refactor whose
> output is byte-identical to today.

## 1. Interface shape

A small Protocol plus one rung-0 implementation and a resolver:

```python
@dataclass(frozen=True)
class RetrievedCandidates:
    in_policy_rows: list[dict]
    out_of_policy_rows: list[dict]
    # PR-C extends this ADDITIVELY (per-fact score, rung id). Absent in PR-B so
    # there is nothing for downstream selection to consume or drift on.

class MemoryRetriever(Protocol):
    def retrieve_candidates(
        self,
        project_id: str,
        categories: set[str],
        request_context: "RequestContext | None",
    ) -> RetrievedCandidates: ...

class DeterministicMemoryRetriever:
    """Rung-0. The current candidate load, verbatim."""
    def retrieve_candidates(self, project_id, categories, request_context=None):
        ...  # body moved verbatim from prompt_builder._load_active_memory_rows

def default_memory_retriever() -> MemoryRetriever:
    """Returns the rung-0 singleton. No flag; rung-0 is the only rung in PR-B."""
```

Notes:

- The rung-0 implementation **ignores `request_context`**. Rung-0's request
  signal (path-token overlap, content Jaccard) is applied *downstream* in
  `_partition_relevance_rows`, which is unchanged. `request_context` is in the
  signature only because it is rung-1's reason to exist (it becomes the FTS
  `MATCH` query in PR-C); accepting it now prevents an interface break in PR-C.
- `RetrievedCandidates` mirrors the exact `(in_policy_rows, out_of_policy_rows)`
  tuple that `_load_active_memory_rows` returns today — same shape, same row
  dicts, same category partition.

## 2. Where the interface lives

New module `backend/memory/memory_retriever.py`, sibling to the PR-A
`backend/memory/memory_fts.py`. It owns the candidate-load SQL + role
category-policy partition and the rung-0 implementation. This keeps
`prompt_builder` thin and gives PR-C a single home for rung-1 (FTS) without
touching the selection logic again.

## 3. Exact caller boundary

`build_project_memory_block_detailed` in `prompt_builder.py` is the **only**
caller. The seam replaces exactly the existing candidate-load call site
(`prompt_builder.py:507`):

```python
# before
in_policy_rows, out_of_policy_rows = _load_active_memory_rows(
    project_id, categories
)

# after
candidates = default_memory_retriever().retrieve_candidates(
    project_id, categories, request_context=request_context
)
in_policy_rows = candidates.in_policy_rows
out_of_policy_rows = candidates.out_of_policy_rows
```

Everything before this point (`role_key`, `categories`, `_resolve_budget`,
`preferred_scopes`, empty-`project_id` guard) and everything after it (sort,
tiering, omission, budget drop, render, provenance result) is unchanged. Triage,
planner, coder, and the `/memory/prompt-preview` route are untouched — they keep
calling the same `build_project_memory_block*` API.

## 4. Byte-identity strategy

- **Move, do not rewrite.** Rung-0 is the verbatim body of
  `_load_active_memory_rows`: identical SQL (no added `ORDER BY`), identical
  active/non-stale filter, identical `dict(row._mapping)` materialization,
  identical category partition into in-policy / out-of-policy.
- **Selection stays put.** The deterministic `_memory_row_sort_key` sort,
  mandatory split, `_partition_relevance_rows` (ordering + omission), budget-drop
  loop, and the block render remain in `prompt_builder`, unedited. Output cannot
  drift because the bytes are produced by the same downstream code over the same
  rows.
- **Order safety.** The load SQL is intentionally left unordered, exactly as
  today; row order does not matter because `prompt_builder` re-sorts
  deterministically after retrieval. Adding an `ORDER BY` "for cleanliness" is a
  behavior change and is out of scope.
- **One rung, no toggle.** `default_memory_retriever()` always returns rung-0;
  there is no alternate path and no flag, so there is exactly one possible
  output — the current one.

## 5. Definition of rung-0 (current implementation)

Rung-0 is the full existing deterministic selection, composed of:

1. **Candidate load** — `SELECT ... FROM memory_facts WHERE project_id = ? AND
   is_stale = 0 AND status = 'active'`.
2. **Role category-policy partition** — `ROLE_CATEGORIES[role]` → in-policy vs.
   out-of-policy.
3. **Base sort** — `_memory_row_sort_key` = (category_rank, scope_rank, priority,
   created_at).
4. **Mandatory split** — `_is_mandatory_row`: safety categories always; pinned
   facts when `MEMORY_RELEVANCE_OMISSION_ENABLED` is on.
5. **Relevance order** — `_partition_relevance_rows`: path-token overlap →
   content-token Jaccard → base key; omission only when
   `MEMORY_RELEVANCE_OMISSION_ENABLED`.
6. **Budget drop** — render-order token budgeting.

**PR-B moves only steps 1–2 behind the seam; steps 3–6 stay in `prompt_builder`.**
PR-C adds rung-1 (FTS/BM25) fused into the step-5 signal, behind a default-off
flag; rung-0 remains the permanent fallback (and the only rung when FTS5 is
unavailable).

## 6. Tests required

- **Existing suites pass unchanged** (the primary byte-identity proof — they
  already assert exact block bytes and exclusion sets across roles, budgets,
  scopes, with/without `request_context`, and omission on/off):
  `test_memory_prompt_builder.py`, `test_memory_selection_scaffolding.py`,
  `test_memory_free_exclusions.py`, `test_memory_injection_provenance.py`.
- **New `backend/tests/test_memory_retriever.py`:**
  - *Move-equivalence:* rung-0 `retrieve_candidates` returns exactly the rows the
    legacy `_load_active_memory_rows` returned for the same project/categories.
  - *§5 conformance suite*, parametrized over retriever implementations (rung-0
    only in PR-B, so PR-C's rung-1 must pass the same harness):
    - **project-scope before anything** — never returns another project's rows;
    - **status / staleness exclusion** — no stale, archived, or historical facts;
    - **suggestions never returned** — only approved active facts;
    - **empty / whitespace `project_id`** → empty result;
    - **mandatory candidates never dropped by the retriever** — the full
      in-policy set (safety + pinned + relevance) is returned; tiering/budget
      happen downstream (standing contract so PR-C cannot regress it);
    - **rung-0 ignores `request_context`** — identical candidates with and
      without it.
- **Dormancy guard (reuse PR-A):** injected block byte-identical with/without the
  FTS table present; assert the retriever performs **no FTS read** in PR-B.

## 7. Likely files touched

- `backend/memory/memory_retriever.py` *(new)* — `MemoryRetriever` Protocol,
  `RetrievedCandidates`, `DeterministicMemoryRetriever` (rung-0), resolver.
- `backend/memory/prompt_builder.py` — remove `_load_active_memory_rows`; call
  the retriever at the one boundary in `build_project_memory_block_detailed`.
- `backend/tests/test_memory_retriever.py` *(new)* — move-equivalence + §5
  conformance + dormancy.

## 8. Files / logic that must remain untouched

- `backend/memory/memory_fts.py` — no FTS read; PR-A index stays dormant.
- `backend/memory/memory_store.py` — all mutation / write paths (add / update /
  supersede / archive / mark-stale) byte-identical; **no write-path FTS
  maintenance**.
- `backend/db/database.py` and `backend/db/schema.sql` — **no schema change**.
- `backend/pipeline/policy.py` — **no new flag**.
- `backend/pipeline/triage.py`, `planner.py`, `coder.py`, and
  `backend/routes/memory.py` — unchanged; same `build_project_memory_block*` API.
- In `prompt_builder.py`: `_partition_relevance_rows`, `_is_mandatory_row`,
  `_memory_row_sort_key`, `_resolve_budget`, the budget-drop loop, and the block
  render — unchanged.
- Frontend — none (backend-only; no endpoint).

## 9. Risks

- **Circular import.** `prompt_builder` imports the retriever; the retriever wants
  `RequestContext` (defined in `prompt_builder`) for typing. Break the cycle with
  `from __future__ import annotations` + a `TYPE_CHECKING`-guarded import in the
  retriever module. Do **not** move `RequestContext` (it would ripple into
  `coder.py` and widen the diff).
- **Mandatory facts (safety-critical).** The retriever must return the *full*
  in-policy candidate set; the mandatory tier and budget run downstream. A future
  rung that ranks or caps inside the retriever could drop a safety fact —
  violating §5 ("safety filters applied *with*, never after, ranking"). Rung-0
  satisfies this trivially; the conformance suite asserts it as a standing
  contract so PR-C cannot regress it.
- **Ordering / token budget.** Byte-identity depends on the sort and budget loop
  running *after* retrieval. Keep them in `prompt_builder`; resist moving sorting
  or capping into the retriever "for cleanliness."
- **Relevance vs. retrieval conflation.** `_partition_relevance_rows` and
  omission stay downstream and untouched; the retriever does no relevance
  omission.
- **request-aware selection.** Rung-0 must genuinely ignore the passed
  `request_context`; test with and without it to prove no drift.

## 10. Explicit non-goals

- No FTS read; no BM25 / ranking / fusion; no scores or rung id in provenance.
- No write-path / incremental FTS maintenance; no triggers.
- No runtime flag.
- No search route / endpoint; no frontend.
- No vectors / embeddings (Row 23).
- No change to mandatory-tier, relevance-omission, budget, ordering, or
  request-aware-selection semantics.
- No schema change; no `prompt_builder` selection logic change beyond relocating
  the candidate load.
- No change to triage / planner / coder / preview-route behavior.
- No edit to the #32G §5 contract (adopted verbatim).
- No change to approval / chunk-plan / final-approval / execution / Git / PR
  behavior; scope and path safety unchanged.

## 11. Deliberate PR-B decisions: no flag, no write-path FTS maintenance

Both omissions are intentional, not oversights:

- **No runtime flag.** A flag selects between implementations. In PR-B the only
  rung is deterministic rung-0, so a flag would select rung-0-vs-rung-0 — gating
  nothing observable. That is the "buried magic / constant that controls nothing"
  anti-pattern the PR-A brief (§4) used to justify no flag in PR-A; the same logic
  applies here. The first **meaningful** default-off flag lands in **PR-C**, where
  rung-1 (FTS) is a real alternative for it to select. The retriever
  registry/resolver already provides the forward-compat seam without a dead
  toggle.
- **No write-path FTS maintenance.** Incremental index maintenance *is* FTS use
  and would touch the hot `memory_store` mutation functions — both excluded by the
  PR-B scope. PR-B stays a pure, byte-identical structural seam. The reader that
  needs a fresh index (rung-1) and the maintenance that keeps it fresh ship
  together in **PR-C**, where they can be designed and tested as one concern.

## 12. Sequel framing (so PR-B stays single-purpose)

- **PR-C** — rung-1 SQLite FTS5 keyword relevance **fused with rung-0 signals**,
  wired into selection behind a real **default-off** flag; deterministic given the
  corpus; provenance records rung identity and score per included entry; ships the
  write-path / incremental index maintenance the reader needs. Rung-1 becomes the
  default only after the M5 suggestion-quality gate (order-row 7) has soaked.
- **Row 23** — vector rung 2 (opt-in), reached by swapping the retriever
  implementation behind the same seam.

PR-B smuggles none of PR-C. It is the byte-identical seam they build on.
