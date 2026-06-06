# Memory Provenance Smoke & Closeout Checklist (M3C)

Manual smoke validation and closeout record for **memory injection provenance**
(M3C). This is a checklist, not an automated suite: it complements the focused
backend tests that already cover the persisted snapshots (M3C1) and the
read-only analysis (M3C2). There is no frontend for this phase yet, so all UI
steps are intentionally absent — M3C is backend/API only.

Related docs:

- Design / as-built audit: [`docs/design/memory-m3-trust-lifecycle.md`](../design/memory-m3-trust-lifecycle.md)
  (§14 M3B helpers, §15 M3C1 provenance, §16 M3C2 analysis)
- Memory M2 smoke: [`docs/testing/memory-m2-smoke-checklist.md`](./memory-m2-smoke-checklist.md)
- Local setup: [README → Quick local setup](../../README.md#quick-local-setup)

## Completed Work (this phase)

- M3A — `docs/design/memory-m3-trust-lifecycle.md` as-built audit — merged
- M3B — pure memory trust helper foundations + stale docs reconciliation — merged
- M3C1 — persisted memory injection provenance snapshots + read-only endpoint — merged
- M3C2 — read-only, compute-on-read provenance analysis + read-only endpoint — merged
- M3C3 — this smoke / closeout checklist (docs only)

## 1. Purpose

M3C makes memory **influence auditable** without changing how memory behaves:

- **M3C1 records what memory was injected** into the triage, planner, and coder
  prompts for a run (append-only `memory_injection_events` snapshots), so a human
  can later see the exact approved facts a role received — even after those facts
  are edited, archived, marked stale, or superseded.
- **M3C2 analyzes those snapshots read-only** for advisory duplicate and
  supersession candidates, computed on read using the pure M3B trust helpers.
- It **does not change** memory, prompts, run behavior, approvals, retries, PRs,
  or injection rules. It is visibility-only.

## 2. Safety guarantees preserved

M3C changes none of the existing safety invariants. Confirm all still hold:

- No auto-save of memory.
- No auto-approval.
- No auto-stale.
- No auto-archive.
- No auto-resolve.
- No mutation / resolution routes (M3C is read-only).
- No frontend actions (no UI in this phase).
- No LLM, embeddings, vector search, or pgvector.
- No repo scan or git calls from analysis.
- No default Run Detail payload bloat (provenance and analysis are dedicated
  endpoints, not added to the default run/chunk read model).
- No prompt / injection behavior change (the block string is byte-identical;
  capture is best-effort and never alters the prompt or run outcome).
- Reviewer and summary are **not** newly wired to memory — they still do not
  receive injected memory, so no provenance is recorded for them.

## 3. Endpoints

Both endpoints are **read-only**, project-scoped to the run's owning project,
and return empty results for pre-M3C / no-provenance runs.

### Provenance (M3C1)

```
GET /api/v1/runs/{run_id}/memory-injections
```

- Optional filters: `chunk_number`, `role`.
- Returns the recorded injection events (newest first).
- Returns `{ "run_id": ..., "events": [] }` for runs with no provenance.
- `404` for an unknown run.

### Analysis (M3C2)

```
GET /api/v1/runs/{run_id}/memory-injections/analysis
```

- Optional filters: `chunk_number`, `role`.
- Returns summary counts plus **advisory-only** `duplicate_candidates` and
  `supersession_candidates`, computed on read (nothing persisted).
- Returns empty analysis (`total_events: 0`, empty candidate lists) for runs with
  no provenance.
- `404` for an unknown run.

## 4. Manual smoke setup

Use the standard local setup — do not invent new commands:

- Start the backend and frontend per
  [README → Quick local setup](../../README.md#quick-local-setup).
  (Windows note: if PowerShell blocks `npm.ps1`, use `npm.cmd`.)
- Ensure the project has **at least one approved, active memory fact** relevant to
  triage/planner/coder categories (e.g. a `test`, `stack`, or `structure` fact).
  Create/approve facts via the existing memory routes / UI as in the
  [M2 smoke checklist](./memory-m2-smoke-checklist.md).
- Run a **tiny feature** through Pipewright (chunked execution) that reaches at
  least the planner and coder stages.
- Note the `run_id` — it keys every check below.

> Tip: the local SQLite DB is `backend/db/pipewright.db`; the SQL snippets below
> read from it directly to corroborate the API responses.

## 5. Smoke checklist — provenance capture (M3C1)

- [ ] After a run that injected memory, `memory_injection_events` rows exist:

  ```sql
  SELECT role, chunk_number, included_count, excluded_count, entries_hash
  FROM memory_injection_events
  WHERE run_id = '<your-run-id>'
  ORDER BY created_at;
  ```

- [ ] Triage, planner, and coder each have rows **if** that role actually built a
      non-empty memory block (a role with no in-policy active facts builds an empty
      block and may record an event with `included_count = 0`).
- [ ] Each row includes `run_id`, `project_id`, `role`, `chunk_number` (when
      applicable — triage is run-level and may be `NULL`), `token_budget`,
      `category_policy`, `included_count`, and `entries_hash`.
- [ ] `entries_json` contains **memory-entry data only** (fact id, content,
      category, scope, priority, status) — **never** the full prompt, repo files,
      handoff contracts, or source code.
- [ ] A **failed run still preserves** any injections already recorded for role
      invocations that happened before the failure (provenance is append-only and
      best-effort capture never rolls back on later failure).
- [ ] A **pre-M3C run** (or any run with no provenance) returns empty provenance
      from the endpoint in §6 — no rows, no error.

## 6. Smoke checklist — read endpoint (M3C1)

- [ ] `GET /api/v1/runs/{run_id}/memory-injections` returns the recorded events.
- [ ] Filtering by `role` works:
      `GET /api/v1/runs/{run_id}/memory-injections?role=coder`.
- [ ] Filtering by `chunk_number` works:
      `GET /api/v1/runs/{run_id}/memory-injections?chunk_number=1`.
- [ ] An unknown run returns **404**:
      `GET /api/v1/runs/does-not-exist/memory-injections`.
- [ ] Calling the endpoint **does not mutate** memory (re-list the project's
      memory facts before/after; they are unchanged).
- [ ] Per-entry `content_hash` is **not exposed** (current behavior — stripped for
      parity with the rest of the memory API); the event-level `entries_hash`
      digest **is** retained.

## 7. Smoke checklist — analysis endpoint (M3C2)

- [ ] `GET /api/v1/runs/{run_id}/memory-injections/analysis` returns summary
      counts: `total_events`, `total_included_entries`, `distinct_fact_count`,
      `duplicate_candidate_count`, `supersession_candidate_count`.
- [ ] `duplicate_candidates` and `supersession_candidates` are **advisory only**
      (each carries `advisory_only: true`; supersession uses
      `relation: "possible_supersession"`).
- [ ] The **same fact injected into multiple roles is not flagged against itself**
      (distinct facts are keyed by fact id → content hash → normalized content, so
      a fact reused across planner and coder collapses to one distinct entry).
- [ ] `recency_implies_truth` is **false** for every supersession candidate
      (direction is undecided; a newer fact is never automatically correct).
- [ ] **No analysis is persisted** (re-run the call; it recomputes from the
      immutable snapshots — there is no analysis table or stored verdict).
- [ ] The endpoint makes **no repo / git / LLM / vector** calls (it reads only the
      already-stored snapshots; the analysis module imports none of those — see the
      import-purity guard in `test_memory_injection_analysis.py`).

## 8. Regression commands (PowerShell)

```powershell
# Focused M3C tests
python -m pytest backend/tests/test_memory_injection_provenance.py -q
python -m pytest backend/tests/test_memory_injection_analysis.py -q

# Memory trust helpers + memory API subset
python -m pytest backend/tests/test_memory_trust.py `
  backend/tests/test_memory.py `
  backend/tests/test_memory_api.py -q

# Lint (repo enforces `ruff check`, NOT `ruff format`) on the M3C files
python -m ruff check backend/memory/injection_store.py `
  backend/memory/injection_analysis.py `
  backend/memory/prompt_builder.py `
  backend/routes/memory.py

# Whitespace / conflict-marker hygiene
git diff --check
```

Expected: all listed tests pass; `ruff check` reports no issues on these files;
`git diff --check` is clean. Some `@pytest.mark.api` tests elsewhere need live keys
+ a target repo and are deselected with `-m unit` — none of the M3C tests above
require live keys.

## 9. Known limitations / deferred work

- No **frontend UI** yet (no memory-provenance panel).
- **Supersession / approve-and-supersede** routes shipped in M3D2 as human-controlled
  backend-only mutation routes. They mark old active facts `historical` via
  `superseded_by_fact_id` and leave recorded provenance snapshots immutable.
  There is still no frontend UI for this path.
- No **reality-check analysis** against live repo signals yet — M3C2 must not scan
  the repo, so `check_fact_against_signal` is not surfaced until a later slice can
  pass an already-computed signal in safely.
- No **automatic exclusion** of risky non-DB memory yet (the §8 poisoning surface
  in the audit remains; M3C only makes it *visible*).
- No **retention / pruning** of `memory_injection_events`.
- `attempt_id` / `repo_head_sha` may be **nullable** depending on the capture path
  (not yet wired to the patch-failure attempt machinery).
- **Reviewer / summary memory injection** is still intentionally **not wired**.

## 10. Closeout criteria

M3C can be considered **closed** when:

- [ ] M3C1 / M3C2 unit + API tests pass (`test_memory_injection_provenance.py`,
      `test_memory_injection_analysis.py`, plus the memory trust / API subset).
- [ ] Manual endpoint smoke passes (§5–§7): provenance rows recorded, read
      endpoint filters/404 behave, analysis returns advisory-only candidates.
- [ ] **No prompt behavior change** is observed (the injected memory block is
      unchanged; runs behave identically with capture on).
- [ ] **No mutation / auto-resolution** behavior was introduced (memory is not
      written, approved, staled, archived, or resolved by anything in M3C).
- [ ] Known limitations (§9) are recorded before starting M3D.

## 11. Final phase result

**M3C — memory injection provenance + read-only analysis is complete for its
visibility-only scope** once this doc is merged and the smoke above is run. Memory
influence is now auditable (M3C1) and advisory candidates are surfaced on read
(M3C2), with every existing safety invariant preserved and all mutation/resolution
work deferred to **M3D**.
