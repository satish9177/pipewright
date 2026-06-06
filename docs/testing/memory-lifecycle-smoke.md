# Memory Lifecycle Smoke Checklist (M3D)

Manual smoke guide for M3D human-controlled memory lifecycle behavior. This is
backend-only coverage for mark-stale, supersession lineage, and
approve-and-supersede. It intentionally does not add frontend, prompt, runtime,
LLM, vector, or auto-resolution behavior.

Related docs:

- [M3 trust lifecycle design](../design/memory-m3-trust-lifecycle.md)
- [Memory provenance smoke checklist](./memory-provenance-smoke.md)
- [README quick local setup](../../README.md#quick-local-setup)

## 1. Purpose

M3D lets a human operator explicitly move memory facts through lifecycle states:
mark a fact stale, supersede one active fact with another active fact, or approve
a pending suggestion and supersede an existing active fact in one transaction.

The system may surface advisory candidates elsewhere, but it does not decide
truth and does not resolve conflicts automatically. The operator supplies the
exact fact IDs and reason. Supersession direction is explicit: the old fact is
the path parameter or `old_fact_id`, and the new fact is the supplied
`new_fact_id` or newly approved suggestion fact.

## 2. Safety Guarantees

- No route automatically saves, approves, marks stale, archives, supersedes, or
  resolves memory.
- No latest-wins behavior. `created_at` recency never decides which fact wins.
- No LLM truth. Generated suggestions remain pending until a human approves
  them.
- No embeddings, vector database, pgvector, or similarity store is introduced.
- No prompt format, injection, pipeline, run, Git, GitHub, or runtime behavior is
  changed by lifecycle routes.
- Provenance is immutable. Existing `memory_injection_events` rows, including
  `entries_json`, `entries_hash`, `status_at_injection`, and captured content,
  stay unchanged after stale/archive/supersede actions.
- Historical, stale, and archived facts are excluded from prompt memory because
  injection reads only active facts with `is_stale = 0`.
- The M3D mutation routes/helpers are guarded so they do not call M3B trust or
  M3C analysis helpers.

## 3. Backend Routes Covered

All routes below are under `/api/v1/projects/{project_id}/memory`.

| Route | Purpose | Body | Success behavior | Expected failures | Creates active memory? |
| --- | --- | --- | --- | --- | --- |
| `PATCH /{memory_id}` | Existing fact edit. Relevant as the pre-M3D direct-edit path, not supersession lineage. | Any of `content`, `category`, `scope`, `priority`. | Mutates the existing fact and returns the sanitized fact response, including `superseded_by_fact_id` and excluding `content_hash`. | `404` missing/cross-project fact, `409` active duplicate content, `422` invalid fields or empty body. | No new fact; may update an existing active fact in place. |
| `POST /{memory_id}/archive` | Human archive of a fact. | `{ "reason": "..." }` | Sets `status = "archived"`, `is_stale = 1`, stores `archived_reason`, updates `updated_at`, and returns the sanitized fact. | `404` missing/cross-project fact, `422` invalid reason. | No. Lifecycle-only. |
| `POST /{memory_id}/verify` | Human verification timestamp. | None. | Updates `last_verified_at` and `updated_at`; returns `id`, `project_id`, and `last_verified_at`. | `404` missing/cross-project fact. | No. Metadata-only. |
| `POST /{memory_id}/stale` | M3D1 human mark-stale. | `{ "reason": "..." }` | Requires an active fact, then sets `status = "stale"`, `is_stale = 1`, stores the reason in `archived_reason`, and returns the sanitized fact. | `404` missing/cross-project fact, `409` non-active fact, `422` blank/short/control-plane reason. | No. Lifecycle-only. |
| `POST /facts/{old_fact_id}/supersede` | M3D2 existing-fact supersession. | `{ "new_fact_id": "...", "reason": "..." }` | Requires old and new facts in the same project, both active and non-stale. Updates only the old fact to `status = "historical"`, `is_stale = 1`, sets `archived_reason`, and sets `superseded_by_fact_id` to the new fact ID. Returns `{ old_fact, new_fact }`. | `404` missing/cross-project old or new fact, `409` non-active old/new fact, `422` blank IDs, self-supersede, or invalid reason. | No. Lifecycle-only; the new fact is unchanged. |
| `POST /suggestions/{suggestion_id}/approve-and-supersede` | M3D2 atomic approval plus supersession. | `{ "old_fact_id": "...", "reason": "...", "edited_content": "optional", "approved_by": "optional" }` | In one transaction, validates and approves the pending suggestion through the normal approval path, creates one active fact, then marks the old fact historical with `superseded_by_fact_id` pointing to the new fact. Returns `{ suggestion, fact, superseded_fact }`. | `404` missing/cross-project old fact or suggestion, `409` old fact not active, suggestion not pending, duplicate active content, or supersession precondition failure, `422` unsafe edited content or invalid reason. | Yes. Creates the approved active fact, then lifecycle-updates the old fact. |

Useful read routes for smoke checks:

- `GET /api/v1/projects/{project_id}/memory`
- `GET /api/v1/projects/{project_id}/memory?status=stale`
- `GET /api/v1/projects/{project_id}/memory?status=historical`
- `GET /api/v1/projects/{project_id}/memory/prompt-preview`
- `GET /api/v1/runs/{run_id}/memory-injections`

## 4. Manual Smoke Setup

1. Follow [README quick local setup](../../README.md#quick-local-setup) and
   start the backend. The default local backend URL is
   `http://127.0.0.1:8001`.
2. Create or reuse a project whose `repo_path` points at a small local checkout.
3. Create two active facts in that project with
   `POST /api/v1/projects/{project_id}/memory`. Use a deliberately old/new pair,
   for example `Backend uses Flask.` and `Backend uses FastAPI.`.
4. Create or find one pending suggestion. Use the existing bootstrap suggestion
   route, run-scoped suggestion generation, or a test fixture. Do not directly
   create an active fact when testing approve-and-supersede.
5. Optional provenance setup: run the memory provenance smoke or a tiny run that
   injects one active fact, then capture the run ID for the provenance
   immutability checks below.

## 5. Mark-Stale Checklist

- Confirm the target fact appears in `GET /memory` and in
  `GET /memory/prompt-preview`.
- Call `POST /{memory_id}/stale` with a human reason of at least four
  characters.
- Confirm the response and stored fact have `status = "stale"`, `is_stale = 1`,
  and `archived_reason` equal to the supplied reason.
- Confirm the stale fact is absent from `GET /memory/prompt-preview`.
- Confirm `GET /memory?status=stale` includes the fact.
- Try the same route against stale, archived, and historical facts. Each should
  fail with `409` and leave the fact unchanged.
- Try a fact from another project and a random fact ID. Each should return
  `404`.
- Try a missing, short, blank, or control-plane-style reason. It should return
  `422` and leave the fact active.
- If provenance was captured before the stale action, confirm the provenance
  event still shows the original content and `status_at_injection = "active"`.

## 6. Existing-Fact Supersession Checklist

- Create or identify two active, non-stale facts in the same project.
- Call `POST /facts/{old_fact_id}/supersede` with the new fact ID in the body.
- Confirm the response includes both facts and no `content_hash`.
- Confirm the old fact has `status = "historical"`, `is_stale = 1`,
  `archived_reason` set to the reason, and `superseded_by_fact_id` equal to the
  new fact ID.
- Confirm the new fact remains active, non-stale, and otherwise unchanged.
- Confirm active prompt preview excludes the old fact and still may include the
  new fact.
- Confirm `GET /memory?status=historical` includes the old fact.
- Verify direction is explicit by superseding an older/newer pair regardless of
  `created_at`; only the path `old_fact_id` should become historical.
- Try self-supersession. It should return `422` and leave the fact active.
- Try a cross-project or missing old/new fact. It should return `404`.
- Try stale, archived, or historical old and new facts. Each should return
  `409`.
- Try invalid reasons, including blank/short/control-plane text. They should
  return `422`.

## 7. Approve-And-Supersede Checklist

- Start with one active old fact and one pending suggestion in the same project.
- Call `POST /suggestions/{suggestion_id}/approve-and-supersede` with
  `old_fact_id` and a human reason.
- Confirm the returned `fact` is active and contains the suggestion content.
- Confirm the returned `suggestion` is approved and points at the new approved
  fact.
- Confirm the returned `superseded_fact` is historical, stale, has the supplied
  reason, and has `superseded_by_fact_id` equal to the new fact ID.
- Repeat with `edited_content` and confirm the approved fact and suggestion use
  the edited text.
- Try unsafe edited content, such as control-plane instructions or sensitive
  local paths. The request should fail and no active fact or historical old fact
  should be created.
- Try an old fact that is already stale, archived, or historical. The request
  should fail with `409`; the suggestion should remain pending and no new fact
  should exist.
- Try a suggestion whose content duplicates an existing active fact. The request
  should fail safely, leave the suggestion pending or unapproved, and leave the
  old fact active.
- If testing with monkeypatch or a fixture, force a post-insert supersession
  failure and confirm the whole transaction rolls back.
- Confirm the existing `POST /suggestions/{suggestion_id}/approve` route still
  approves a suggestion without superseding any old fact.

## 8. Provenance Immutability Checklist

- Capture a memory injection event before lifecycle mutation.
- Record the event ID, `entries_hash`, included entry content, and
  `status_at_injection`.
- Supersede the captured fact after the run.
- Confirm `GET /api/v1/runs/{run_id}/memory-injections` still returns the same
  captured content, same `status_at_injection`, and same `entries_hash`.
- If checking SQLite directly, confirm raw `entries_json` and `entries_hash` are
  byte-for-byte unchanged before and after supersession.
- Confirm no lifecycle route creates, updates, or deletes provenance rows.
- Confirm historical facts are excluded from future prompt previews, while past
  provenance remains a snapshot of what the role actually received.

## 9. Regression Commands

Use focused unit coverage when backend files change:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\test_memory_api.py backend\tests\test_memory_bootstrap.py backend\tests\test_memory_prompt_builder.py backend\tests\test_memory_injection_provenance.py backend\tests\test_memory_injection_analysis.py backend\tests\test_memory_trust.py -v -m unit
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit -k "supersede or supersession"
```

Run backend style checks only for changed backend files:

```powershell
venv\Scripts\python.exe -m ruff check backend\memory\memory_store.py backend\memory\bootstrap.py backend\routes\memory.py backend\db\database.py backend\tests\test_memory.py backend\tests\test_memory_api.py backend\tests\test_memory_bootstrap.py backend\tests\test_memory_injection_provenance.py
```

Always finish with the whitespace check:

```powershell
git diff --check
```

## 10. Known Limitations And Deferred Work

- No frontend UI is included for stale or supersession actions.
- No lineage table, audit table, new lifecycle status, or new index is added for
  M3D.
- `archived_reason` is reused as the human reason field for stale, archived, and
  historical facts.
- Candidate detection, duplicate analysis, and supersession analysis remain
  advisory read models. They do not perform mutation.
- Prompt filtering remains the existing active and non-stale read path.
- Provenance is a historical snapshot, not a live pointer to current fact state.

## 11. Closeout Criteria

- The stale route can move only an active fact to stale and excludes it from
  prompt preview.
- Existing-fact supersession changes only the explicitly chosen old fact to
  historical and preserves the new fact.
- Approve-and-supersede creates the approved active fact and historicizes the old
  fact atomically, with rollback on validation, duplicate, inactive-old, or
  post-insert failure.
- API responses include `superseded_by_fact_id` where fact read models are
  returned and continue to exclude `content_hash`.
- Past provenance remains immutable after stale, archive, or supersession.
- No prompt, pipeline, runtime, frontend, vector, LLM, or auto-resolution
  behavior changes are introduced.
