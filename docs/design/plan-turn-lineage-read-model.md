# Plan-turn lineage read-model / audit endpoint (design brief)

Status: **Implemented in this slice — pending review.** §23 row 7b follow-up
(Slice A). Plan-turn engine, the `POST /runs/{run_id}/plan-turns` route, the
frontend "Revise plan" affordance, and `PIPEWRIGHT_PLAN_TURNS_ENABLED` env wiring
are already merged and dormant by default. This slice adds a **read-only** audit
endpoint so the plan-version lineage that the engine already writes becomes
observable. Read-only; no schema migration; no frontend; no approval/execution
change; flag default unchanged.

Scope owner: pipeline roadmap (plan-gate turns). Predecessor: §23 row 7b PR-A..PR-D
(plan-turn engine + route + frontend + env wiring). Precedes: approved-plan-version
stamping (Slice B, which touches the approval gate and is kept separate), then the
frontend lineage display.

**Guardrails (inherited constraints):** read-only only; no schema migration; no
frontend in this slice; no approval/execution changes; no `approved_plan_version`
stamping yet; no activation/default-on change; no mutation locks or pipeline
triggers; preserve the existing `GET /chunks` response byte-for-byte;
sanitize/redaction safety asserted in tests; targeted tests only.

---

## 0. Why this slice, and why now

The engine writes a full plan-version lineage but **nothing reads it.** Every run
gets a `plan_versions` v1 at plan creation (`chunk_store.py:371`,
`routes/chunks.py:1136`); each plan turn appends `vN` and atomically swaps the live
`pipeline_runs.chunk_plan` pointer while still awaiting approval
(`plan_turn_engine.py:245-289`). But no route exposes `list_plan_versions`, and the
frontend `RevisePlanPanel` is fire-and-forget — it submits a message, shows a
success line, and refetches `GET /chunks`, which carries no version number, no
history, and no message lineage. A user revising a plan cannot see "this is
revision 2 of 3" or which message produced each version.

This slice closes the **observability** gap only. It does **not** touch the
approval binding, which already holds structurally: plan turns can mutate
`chunk_plan` only while the run is awaiting approval (status-gated conditional
`UPDATE ... WHERE status = AWAITING_CHUNK_PLAN_APPROVAL`, with a `rowcount != 1`
rollback, `plan_turn_engine.py:263-283`); approval merely flips status
(`chunk_store.py:480-503`); execution reads the live `chunk_plan` pointer and
nothing else (`execute_approved_chunks` → `get_chunk_plan_status` →
`TriageResult.model_validate_json(chunk_plan)`). So "execute exactly the approved
plan" is already enforced; this slice adds the lens, not a new authority.

## 1. Endpoint contract

`GET /runs/{run_id}/plan-versions`

- **Auth/lock:** none. Pure read; mirrors the read-only `get_pr_status_route`
  discipline (`routes/chunks.py:2519`) — no `_ensure_mutating_run`, no repo lock,
  never gates or triggers approval, push, execution, or merge.
- **Existence rule (the 404-vs-empty split):** a cheap
  `SELECT 1 FROM pipeline_runs WHERE id = :run_id`. Run **absent → 404**; run
  **present → 200** even with zero recorded versions. Do **not** route through
  `get_chunk_plan_status` — that parses the plan and runs scope overlays; keep this
  endpoint decoupled and light.
- **Errors:** `ValueError` (unknown run) → 404; any other `Exception` → 500. No
  400/409/422 — there is nothing to validate and no state to conflict with.
- **Returns:** a plain dict (matches the read-endpoint convention —
  `get_pr_status_route` and `POST /plan-turns` both return plain dicts; no new
  pydantic `response_model`).
- **Idempotent / side-effect-free:** identical bytes on repeated calls; provably no
  writes.

## 2. Response shape

```jsonc
{
  "run_id": "abc123",
  "versions": [
    { "version": 1, "source": "initial",   "created_at": "…", "created_from_turn": null },
    { "version": 2, "source": "plan_turn", "created_at": "…",
      "created_from_turn": { "turn_number": 1, "created_at": "…",
                             "message": "<sanitized steer_text>" } }
  ]
}
```

- Ordered `version ASC` (already the order `list_plan_versions` returns).
- **`triage_json` is deliberately excluded** — it is the full plan blob, already
  served by `GET /chunks`; echoing it per-version bloats the audit response and
  duplicates the live plan. (If a lightweight "3→5 chunks" signal is wanted later,
  derive `total_chunks` server-side — optional, not in this slice.)
- Internal `id` UUIDs omitted (not useful to clients; minimal surface).
  `created_from_turn` is `null` for `initial`/`seeded` versions.

## 3. Flag-gating: No

Do **not** gate the GET on `PLAN_TURNS_ENABLED`.

- Nothing to hide: while the flag is off the POST 404s, so **no `plan_turn` row can
  ever exist** — every run's lineage is exactly `[v1]`. The `plan_turn` source enum
  cannot leak, and "1 version" reveals no dormant capability.
- PR-B hides a *mutating capability*; a read of historical audit reveals no
  capability.
- It is the lens needed to review activation later **without** first flipping the
  flag, and it aligns with the "provenance is observable" principle. Re-gating later
  is trivial if posture changes.

## 4. Composition (`plan_versions` ⋈ sanitized `run_turns`) — route composition

**Decision (settled): compose in the route. Do not add a public
`list_plan_version_lineage` store helper in this slice.** If the join later reads
awkwardly for a future consumer, extract a helper then. This keeps the store
append-only/pure and the new surface minimal.

- `list_plan_versions(run_id)` → ordered version rows (`plan_version_store.py:106`).
- `list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN)` → plan-turn messages
  (`run_turn_store.py:166`).
- In the handler, index the plan turns by `id`, then attach to each version via
  `plan_versions.created_from_turn_id → run_turns.id`. **LEFT-join semantics:** a
  missing/dangling turn id degrades to `created_from_turn: null` — never raise.
- `message` is `run_turns.steer_text`, **already redacted at insert** via
  `sanitize_for_log` (`run_turn_store.py:49`). The read introduces no new raw text.

## 5. Behavior matrix

| Case | Result |
|---|---|
| Only initial **v1** | 200 · one entry · `source:"initial"` · `created_from_turn:null` |
| **v2/v3** from plan turns | 200 · ordered v1..vN · plan-turn entries carry `turn_number` + sanitized `message` |
| **Unknown run** | 404 (run id absent) |
| **Legacy/early run** (exists, `chunk_plan` present or null, but **zero `plan_versions` rows**) | 200 · `versions: []` — truthful "no recorded lineage," **not** a 404 and **not** a synthesized v1 (do not fabricate provenance) |

Legacy is real: v1 is written only since the store shipped and only
`if chunk_plan is not None` (`routes/chunks.py:1135`). Pre-existing runs and
pre-plan runs legitimately have no rows. The endpoint reports *recorded* lineage;
absence of rows is a truthful empty result, not an error.

## 6. Test matrix (targeted; no live API)

- Unknown run → **404**.
- Run exists, no `plan_versions` → **200**, `versions: []`.
- v1-only → one `initial` entry, `created_from_turn: null`.
- Drive `produce_next_plan_version` (engine tests already stub triage) for v2/v3 →
  ordered lineage; plan-turn entries link the correct `turn_number` + message.
- **Redaction:** a plan-turn message containing a secret-shaped token → response
  contains only the sanitized form, **never** the raw token (the explicit redaction
  assertion required by the guardrails).
- **Ordering:** versions returned `version ASC`.
- **Flag-off parity:** with `PLAN_TURNS_ENABLED` off, the GET still 200s with the v1
  lineage (locks in the ungated decision from §3).
- **Read-only:** run status / `chunk_plan_status` / row counts unchanged after the
  call.
- **`GET /chunks` unaffected:** assert `get_chunk_plan_route` body is unchanged
  (regression guard that the new endpoint and route composition did not perturb the
  hot path).
- Run: `python -m pytest backend/tests/test_plan_versions_read.py -q` plus the
  existing `test_plan_turns_route.py` / `test_plan_turns_engine.py`; `ruff check`
  (never `ruff format`).

## 7. Files touched

- `backend/routes/chunks.py` — new `@router.get("/runs/{run_id}/plan-versions")`;
  import `list_plan_versions` (and `list_run_turns`, `RUN_TURN_TARGET_PLAN` — already
  public). Compose the lineage in the handler (§4).
- `backend/tests/test_plan_versions_read.py` — **new**.
- **Untouched:** `backend/pipeline/plan_version_store.py` (no new helper this slice),
  `schema.sql`, `chunk_store.py`, `chunked_orchestrator.py`, all frontend, and the
  flag default.

## 8. Explicit non-goals

- No frontend display of the lineage (next slice).
- No `approved_plan_version` stamping (Slice B; touches the approval gate — kept
  separate).
- No schema/migration; no `triage_json` in the response.
- No new public store helper (`list_plan_version_lineage`) — route composition only.
- No change to `POST /plan-turns`, approval, execution, scope, or the flag default.
- No activation of `PLAN_TURNS_ENABLED`; no mutation locks or pipeline triggers.
- `GET /chunks` response unchanged by construction (separate endpoint; hot path
  untouched).
