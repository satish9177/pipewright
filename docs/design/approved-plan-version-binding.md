# Approved-plan-version binding (Slice B) — design brief

Status: **Implemented in this slice — pending review.** §23 row 7b follow-up
(Slice B), successor to Slice A (plan-version lineage read-model, merged in #319).
At chunk-plan approval, records exactly which `plan_versions.version` a human
blessed, and exposes it as a top-level `approved_version` field on the existing
`GET /runs/{run_id}/plan-versions` endpoint **in the same PR**. Backend-only; one
small additive nullable column; no execution / final-approval / flag-default
change.

Scope owner: pipeline roadmap (plan-gate turns). Predecessor: Slice A
(`docs/design/plan-turn-lineage-read-model.md`). Precedes: optional frontend
lineage/approval display.

## Non-authority invariant (read first — this governs the whole slice)

> **`approved_plan_version` is audit/provenance only. It must never become an
> execution source, approval blocker, mismatch gate, or final-approval gate.**

Concretely, this slice must preserve all of the following exactly as they are
today:

- **Execution authority is unchanged.** `execute_approved_chunks` continues to read
  the live `pipeline_runs.chunk_plan` pointer (via `get_chunk_plan_status` →
  `TriageResult.model_validate_json(chunk_plan)`) and nothing else. It never reads
  `approved_plan_version`.
- **Approval is never blocked by the stamp.** Writing the version is best-effort and
  cannot fail an otherwise-valid human approval (see §5/§6).
- **No mismatch gate.** Nothing compares the live plan against the stamped version
  to gate approval, execution, push, PR, or merge.
- **Final approval is untouched.** This slice binds the *chunk-plan* approval gate
  only; `approve_final_approval_route` is out of scope.

The binding makes the approved version *observable and recorded*. It adds no new
authority channel. Memory/advisory-style provenance is never an authority on scope,
approval, Git, provider, execution, or merge — and `approved_plan_version` is
exactly that kind of provenance.

**Guardrails (inherited constraints):** one small PR; backend only; no execution
behavior change; no final-approval behavior change; no auto-execution; no
activation/default-on change; preserve current plan-turn safety invariants; avoid
broad refactors; targeted tests only.

---

## 1. Where to store the approved version

A new **nullable `approved_plan_version INTEGER` column on `pipeline_runs`.** The
approved version is a fact about *the run's approval event*, so it belongs on the
run row beside `chunk_plan` / `chunk_plan_status`, not on an append-only audit row.

Rejected alternatives:

- **Marking the `plan_versions` row "approved"** — violates that table's append-only
  invariant (`plan_version_store.py` docstring: "nothing ever updates or deletes").
  The binding is a run property, not a version property.
- **Stuffing it into `report_json`** — avoids a column but co-mingles an approval
  fact into a semi-structured grab-bag (read-modify-write JSON inside the approval
  transaction = clobber risk, no clean queryability). A typed nullable column is the
  boring, testable choice and matches how the run already records `pr_number`,
  `total_chunks`, etc.
- **Storing the `plan_versions.id` UUID** — the `(run_id, version)` pair is already
  unique (`database.py:631`), and the integer matches the GET response's `version`
  field. Integer wins on simplicity; no FK needed for a run-scoped binding.

## 2. Schema migration needed?

**No heavy migration — yes one small additive column via the project's existing
idempotent pattern.** Two one-line additions, exactly mirroring the ~15 prior
`pipeline_runs` columns added this way:

1. `schema.sql` — add `approved_plan_version INTEGER` to the `pipeline_runs`
   CREATE TABLE (fresh DBs).
2. `database.py` `_migrate_db` (`database.py:120-405`) — append
   `("pipeline_runs", "approved_plan_version",
   "ALTER TABLE pipeline_runs ADD COLUMN approved_plan_version INTEGER")` to the
   migrations list.

Nullable, no DEFAULT, no backfill, no index (the run PK already makes lookups
trivial). `_add_column_if_missing` (`database.py:90-105`) makes it idempotent and
safe on every startup.

**Pre-impl safety check:** confirm no consumer does `model_validate` on a raw
`SELECT *` `pipeline_runs` row with `extra="forbid"`. Existing readers
(`get_chunk_plan_status`, `plan_turn_engine._load_run`) map fields explicitly, so an
extra column is ignored — but verify before merge.

## 3. Exact approval path to touch

**Exactly one function: `approve_chunk_plan(run_id)` in `chunk_store.py:480-503`** —
inside its existing `engine.begin()` transaction, after `_require_awaiting_approval`,
add the version computation and one extra `SET approved_plan_version = :v` to the
existing `UPDATE pipeline_runs SET …`.

Do **not** touch:

- `approve_chunk_plan_route` (`chunks.py:2552`) — the route stays a thin wrapper.
- `reject_chunk_plan` (`chunk_store.py:506`) — rejection binds nothing.
- `approve_final_approval_route` — different, later gate; out of scope.

## 4. How to determine the approved version — MAX at approval time?

**Yes: `SELECT MAX(version) FROM plan_versions WHERE run_id = :run_id`, computed
inside the same approval transaction.** Correct by the append-discipline invariant:
the live `chunk_plan` always equals the latest `plan_versions` row (v1 at creation;
each plan turn appends `vN` *and* swaps `chunk_plan` atomically; no other
`chunk_plan` writer). The unique `(run_id, version)` index makes MAX unambiguous.

**Transaction placement is the one thing that must be right.** Computing MAX
*inside* `approve_chunk_plan`'s transaction (with the status flip) closes any TOCTOU
window. SQLite serializes writers, and plan turns are gated on
`status = AWAITING_APPROVAL`; so approval and a racing plan turn are mutually
exclusive — whichever commits first, the loser's conditional write rolls back, and
the stamped MAX always reflects the version actually being approved. Do **not**
compute MAX outside the transaction.

Deliberately *not* doing a JSON-equality check of `chunk_plan` against each
version's `triage_json` — blob comparison is brittle and the MAX invariant already
holds.

## 5. Legacy runs with zero plan_versions

`MAX(version)` over zero rows is `NULL` → store `approved_plan_version = NULL`.
Truthful "approved, recorded version unknown." **Approval proceeds unchanged.** No
backfill, no fabricated v1.

## 6. Should approval fail if chunk_plan has no plan_versions row?

**No — emphatically.** Blocking approval on a missing audit row would:

- introduce a *new failure mode into the approval gate* (a behavior change the
  constraints forbid, and a direct violation of the non-authority invariant above),
- break legitimate approval of legacy / pre-store runs, and
- invert "fail safe" — refusing a human's safety-gated approval over missing
  provenance is failing *unsafe*.

The stamp is **best-effort and strictly non-blocking**: compute MAX; if NULL, store
NULL; never raise.

## 7. Interaction with plan turns being flag-off / default-off

**Orthogonal — do not flag-gate the stamp.** With the flag off, no plan turns can
occur, so every post-store run has MAX(version)=1 → `approved_plan_version = 1`,
which is correct and harmless. Gating the stamp on `PLAN_TURNS_ENABLED` would yield
less-useful NULLs for no benefit. Consistent with Slice A: the flag governs the
*mutating revise capability*, not audit/binding facts.

## 8. Expose `approved_version` in GET /plan-versions — DECIDED: yes, same PR

**Settled decision:** add one top-level field to the Slice A response —
`"approved_version": <int|null>` — **in this same Slice B PR.** It is the whole
point of binding (making the approved version observable), it is ~3 lines, and Slice
A already built the endpoint. Shipping the write without the read would persist a
fact with no way to see it.

Implementation: extend the endpoint's existing lightweight run probe — replace the
`SELECT 1 … pipeline_runs` existence check (`_run_exists`) with
`SELECT approved_plan_version … pipeline_runs` (row `None` → 404; else use the
value). Still one cheap single-row read, still no `chunk_plan` parse, still
decoupled from `get_chunk_plan_status`.

```jsonc
{ "run_id": "abc123", "approved_version": 2, "versions": [ … ] }
// approved_version is null until approved, and null for legacy runs
```

Deferred (non-goals this slice): per-entry `is_approved` flags, `approved_at`,
approver identity. Top-level `approved_version` is sufficient for the audit lens.

## 9. Tests required (targeted; no live API)

- **v1-only approve** → `approved_plan_version == 1` in DB *and* surfaced as
  `approved_version: 1`.
- **Approve after plan turns to v3** → stamped `3` (MAX at approval time); matches
  the live plan.
- **Legacy (chunk_plan present, zero plan_versions)** → approval **succeeds**, stamp
  is `NULL`, GET shows `approved_version: null`. *(The key safety test — approval
  must not fail.)*
- **Explicit "approval does not raise"** assertion for the zero-version case.
- **Flag-off approve** → stamped `1` (not gated).
- **Reject path** → `approved_plan_version` stays `NULL` (reject binds nothing).
- **Re-approve guard unchanged** → second approve still raises via
  `_require_awaiting_approval`; stamp written exactly once.
- **Execution unaffected (non-authority)** → existing execution tests still pass;
  assert execution still reads live `chunk_plan` and ignores `approved_plan_version`
  (no new gate, no mismatch check).
- **GET /chunks byte-unchanged** → mirror Slice A's regression guard (the new column
  must not leak into `ChunkPlanResponse`).

## 10. Explicit non-goals

- No frontend (backend field only; UI display deferred).
- **No execution behavior change** — execution never reads `approved_plan_version`;
  no mismatch gate between live plan and stamp.
- **No approval blocker / mismatch gate / final-approval gate** built on the stamp
  (the non-authority invariant).
- No final-approval binding/change; only chunk-plan approval.
- No backfill of historical approved runs (legacy stays NULL).
- No change to the `plan_versions` append-only invariant.
- No per-entry approved flags / `approved_at` / approver identity.
- No flag-gating of the stamp or the read field; no activation/default-on change.
- No new indexes; no broad refactor.

## Files touched

- `backend/db/schema.sql` — `approved_plan_version INTEGER` on `pipeline_runs`.
- `backend/db/database.py` — one entry in the `_migrate_db` migrations list.
- `backend/pipeline/chunk_store.py` — `approve_chunk_plan`: MAX(version) read + one
  `SET` clause, same transaction.
- `backend/routes/chunks.py` — `get_plan_versions_route`: swap the existence probe to
  return `approved_plan_version`; add the top-level `approved_version` field.
- `backend/tests/test_plan_versions_read.py` and/or a new
  `test_approved_plan_version_binding.py` — the matrix above.
- **Untouched:** approval route, reject path, final approval, execution, the
  `plan_versions` store, the flag default.
