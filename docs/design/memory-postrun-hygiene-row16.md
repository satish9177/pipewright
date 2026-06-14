# Row 16 — Memory Post-Run Hygiene (design brief)

Status: **PR-A and PR-B implemented.** The dormant, default-off,
success-terminal-only trigger is wired behind
`MEMORY_POSTRUN_HYGIENE_ENABLED=False`; PR-B adds read-only digest/observability
for pending run suggestions. PR-C activation remains deferred. There is no active
auto-generation by default.

Scope owner: memory roadmap. Predecessor: Row 12 (relevance omission) shipped its
scaffolding dormant behind a default-off flag and activated later only after proof.
Row 16 mirrors that discipline.

---

## 1. Purpose

Post-run memory hygiene = the moment, after a run reaches a terminal state, when the
system *proposes* memory suggestions derived from that run's structured artifacts, so a
human can review and approve them. The suggestions still go through the existing
human-approval lifecycle; nothing becomes an active memory fact automatically.

The engine already exists and is good. `backend/memory/run_outcome_suggestions.py`
`generate_run_memory_suggestions(run_id, *, requested_by="user") -> RunSuggestionResult`
is deterministic (no LLM, no embeddings, no repo/log/stack-trace reading), reads only
structured DB fields the pipeline already produced (`pipeline_runs`, `chunks`,
`approval_gates`), inserts **pending suggestions only** via `insert_pending_suggestion`,
is **content-gated** (`validate_memory_content`; a blocked candidate raises `ValueError`
→ counted in `blocked_count`, never stored), **idempotent** (per-project content-hash
dedupe across pending + active facts → repeat calls create no duplicates), **capped**
(`HANDOFF_SUGGESTION_CAP=5`, `RUN_SUGGESTION_TOTAL_CAP=8` in `policy.py`), and
quality-scored. A manual route already exists:
`POST /api/v1/runs/{run_id}/memory-suggestions/generate` (`routes/memory.py`).

**The gap is not a new subsystem.** The gap is (a) a dormant automatic trigger and
(b) observability over the persisted pending suggestions produced by that path or
the existing manual route.

### Reality check that reshapes the attach point (read before §5)

The generator's *highest-value* output is produced **only for `RunStatus.COMPLETE`
runs**: `_completed_run_candidates` requires `run["status"] == COMPLETE` plus a project
`test_command`, and the planner/coder handoff entries come from *completed* chunks'
`suggested_memory_entries`. Failed/rejected runs yield only patch-failure operational
notes and rejected-approach notes. Any attach point that does **not** see the success
terminal misses the point of post-run hygiene.

---

## 2. Non-negotiable safety model (invariants)

Flag off ⇒ **byte-identical current behavior.** When on, every one of these still holds:

1. Pending suggestions only — never an active memory fact.
2. No auto-approval; no approval row auto-created.
3. No prompt-injection change (`prompt_builder` untouched; no selection change).
4. No memory lifecycle mutation (no archive, no supersede, no stale-mark).
5. No stale sweep, no auto-archive.
6. No `last_verified_at` auto-bump; no repo-reality verification side effects.
7. No new LLM call, no embeddings, no repo/log/stack-trace reading (generator is
   already deterministic; the trigger adds nothing).
8. No schema change. (See §5 — fire-once is achieved by the existing content-hash
   dedupe, not a new column. A schema change is *not* proven required, so it is out.)
9. No Row 19 (retriever/FTS), Row 23 (vector), or thread/run UI work.
10. The manual route remains valid and is the **only active path** until activation.
11. Best-effort: a generator error is logged and swallowed; it never fails, stalls, or
    changes a run's terminal status, and never holds a DB transaction or repo lock.
12. `MEMORY_RELEVANCE_OMISSION_ENABLED` is untouched.

---

## 3. D7 decision — dormant default-off trigger

We choose a dormant, default-off policy flag in `backend/pipeline/policy.py`, slotting in
beside the existing `*_ENABLED = False` seams (`MEMORY_RELEVANCE_OMISSION_ENABLED`,
`PROMPT_CACHE_ENABLED`, `SCOPED_VERIFICATION_ENABLED`):

```python
MEMORY_POSTRUN_HYGIENE_ENABLED = False
```

Meaning:
- **Off = byte-identical current behavior.** The trigger short-circuits before doing
  anything; the only added work is a boolean check.
- The manual button remains the only active generation path.
- Future activation is a **separate maintainer decision + smoke**, exactly as with Row 12.
- Build safe scaffolding first; activate later only after proof. Rollback = set `False`
  (selection-time/trigger-time only; nothing persisted to unwind).

---

## 4. Proposed PR split

**PR-A — dormant trigger + flag — implemented.** Added
`MEMORY_POSTRUN_HYGIENE_ENABLED = False` and a thin best-effort `pr_orchestrator`
helper that returns immediately while the flag is off; when monkeypatched on, it calls
`generate_run_memory_suggestions(..., requested_by="postrun_auto")` inside `try/except`
(log + swallow). It runs only after a successful `complete` result and after the repo
lock releases. It never fails/stalls a run. No digest UI. No lifecycle mutation.

**PR-B — housekeeping digest / observability — implemented.** Added a read-only
run suggestion digest over persisted pending suggestions from the run:
`GET /api/v1/runs/{run_id}/memory-suggestions`. It resolves the run's project,
lists pending project suggestions, filters by `source_run_id`, returns
`pending_count` and the safe `MemorySuggestionResponse` shape, and never calls the
generator. The Run Detail page renders a neutral card only when pending
suggestions exist and links to Project Memory for review. Full
`generated/skipped/blocked/floored/capped` breakdowns remain available only in
the existing manual `POST .../generate` response because those counts are
transient and not persisted. Persisting a fuller digest would require schema and
is deferred out of PR-B.

**PR-C — activation decision.** Only after PR-A/PR-B soak. Decide whether to flip the
default or keep manual. May add a "review suggestions now" affordance. Still pending-only.

---

## 5. Attach point — recommendation (this is the load-bearing section)

**The roadmap premise ("attach at the `_update_run_status` terminal funnel") does not
match the code. There is no single terminal funnel.** Verified terminal write map:

| Terminal status | Written by | Mechanism | Publishes `run_status_changed`? |
|---|---|---|---|
| `failed` | `chunked_orchestrator._update_run_status` → `status_service.update_run_status` | SQLAlchemy | yes |
| `final_approved` / `final_rejected` | `routes/chunks.py::_decide_final_gate` | direct SQL inside `engine.begin()` | no |
| `rejected` (memory conflict) | `routes/chunks.py::_decide_memory_conflict_gate` | direct SQL inside `engine.begin()` | no |
| `complete` (success) | `pr_orchestrator._save_pr_metadata` (remote PR) + `_mark_local_only_complete` (local) | direct SQL during/after push (repo lock held) | no |
| `push_failed` | `pr_orchestrator._mark_push_failed` | direct SQL | no |

Consequences:
- `_update_run_status` only ever writes `failed` as a terminal state **and** writes many
  non-terminal states (`running`, `resume`, `chunk_approved`). Attaching there alone would
  fire hygiene *only on failed runs* and would **never** capture the success test command
  or handoff entries — i.e. it attaches at the one place that misses the value.
- The `run_status_changed` **event bus is not a complete terminal signal** either (the
  success/final/push terminals don't publish it), so an event subscriber is not a clean
  single attach point today.
- `_decide_final_gate` / `_decide_memory_conflict_gate` write **inside** an open
  `engine.begin()` transaction, and `pr_orchestrator` writes `complete` while holding the
  **project repo lock**. A synchronous hygiene call nested in either would risk lock/
  transaction contention on SQLite. The call must run **after commit and after lock
  release**.

**Enabling property:** because the generator is idempotent (content-hash dedupe),
"fire once per run" is a *cleanliness* goal, not a correctness requirement — double-firing
produces no duplicates. This is why **no `hygiene_fired_at` schema column is needed**
(invariant §2.8 holds).

**Decision (locked, 2026-06-14):** PR-A's trigger is scoped to the **success terminal
only** — a per-site, best-effort call (`maybe_generate_postrun_suggestions(run_id)`) from
`pr_orchestrator` right after `complete` is persisted and the repo lock is released,
covering **both** `complete` writers (`_save_pr_metadata` for remote PR and
`_mark_local_only_complete` for local-only). This is the most contained funnel (two
adjacent helpers in one module), captures the highest-value output, and holds every
invariant.

Explicitly rejected for PR-A:
- **Attaching to `_update_run_status`** — it is not a true terminal funnel (it writes only
  `failed` plus non-terminal states) and would miss every successful `complete` run.
- **A shared terminal-settle refactor** (a new `settle_run_terminal()` seam all writers
  funnel through) — heavier, touches Git/PR + approval paths; not warranted for a dormant
  trigger.
- **Failed / `final_rejected` / memory-conflict `rejected` / `push_failed` coverage** —
  deferred to a possible later Row 16 follow-up if it proves useful; not in PR-A.

---

## 6. Tests required for PR-A

Built on the existing `backend/tests/test_memory_run_outcome_suggestions.py` harness
(`pytest.mark.unit`; already builds projects/runs/chunks/gates and asserts via
`list_suggestions` / `list_facts`):

1. **Flag-off parity:** terminal transition with `MEMORY_POSTRUN_HYGIENE_ENABLED=False`
   creates zero suggestions (no auto-generation).
2. **Flag-on trigger:** terminal transition with the flag on generates the expected
   pending suggestions for that run.
3. **Fire-once / idempotency:** driving the terminal path (or the entry point) twice
   yields no duplicate suggestions (relies on existing content-hash dedupe).
4. **Generator exception is contained:** monkeypatch the generator to raise; assert the
   run keeps its terminal status, the call does not raise, and the error is logged.
5. **Pending-only invariant:** generated rows are all `pending`.
6. **No active fact created:** `list_facts` shows no new active fact attributable to the
   trigger.
7. **No approval auto-created.**
8. **No prompt-injection change:** the injection block for the project is unchanged.
9. **Manual route still works:** the existing `POST .../memory-suggestions/generate`
   behavior is unchanged.

---

## 7. Tests / smoke for PR-B

1. Read-only route returns 404 for missing runs and empty digest for runs with no
   pending suggestions.
2. Digest filters to the target `source_run_id` and excludes non-pending rows.
3. Repeated reads do not create suggestions, active facts, or approval gates, and
   never call `generate_run_memory_suggestions`.
4. Response uses the existing safe suggestion shape and does not expose
   `content_hash`.
5. Manual smoke confirms neutral copy: pending/review framing, no
   "added to memory" / "auto-saved" / "learned automatically" wording.

---

## 8. Explicit out of scope

Row 19 retriever/FTS; Row 23 vector/embedding; thread/run UI; stale-memory lifecycle
automation; repo-verification `last_verified_at` auto-bump; auto-archive; auto-approval;
prompt-injection changes; schema changes (unproven ⇒ excluded); enabling
`MEMORY_RELEVANCE_OMISSION_ENABLED`; enabling `MEMORY_POSTRUN_HYGIENE_ENABLED` by default.

---

## 9. PR-A implementation files

- `backend/pipeline/policy.py` — adds
  `MEMORY_POSTRUN_HYGIENE_ENABLED = False`.
- `backend/pipeline/pr_orchestrator.py` — adds the success-terminal, post-lock,
  best-effort trigger helper and calls it after successful PR/local-only completion.
  No edits to `chunked_orchestrator.py` or `routes/chunks.py`.
- `backend/tests/test_memory_postrun_hygiene_trigger.py` — trigger, flag-off,
  best-effort, pending-only, idempotency, and no-`_update_run_status` coverage.
- `docs/status/current-state.md` and this design doc — status updates.

---

## 10. PR-B implementation files

- `backend/routes/memory.py` — adds the read-only run suggestion digest route.
- `backend/tests/test_memory_run_suggestions_readmodel.py` — route isolation,
  pending-only, non-mutating, and response-shape coverage.
- `frontend/src/api/client.ts` — typed `getRunMemorySuggestions` client.
- `frontend/src/components/RunMemorySuggestionsDigest.tsx` — neutral read-only
  Run Detail digest card.
- `frontend/src/pages/RunDetailPage.tsx` — renders the digest above the existing
  manual generator panel on terminal runs.
- `docs/testing/memory-postrun-hygiene-smoke.md`, `docs/status/current-state.md`,
  and this design doc — status and manual smoke coverage.

---

## 11. Decisions (locked) and remaining detail

**Decided (2026-06-14):**
1. **Attach breadth — success terminal only.** PR-A covers the successful `complete`
   terminal and nothing else. Failed, `final_rejected`, memory-conflict `rejected`, and
   `push_failed` are explicitly out of PR-A (a possible later Row 16 follow-up).
2. **Attach mechanism — per-site best-effort call in `pr_orchestrator`**, after `complete`
   commits and the repo lock releases. No shared terminal-settle refactor in PR-A. Do not
   attach to `_update_run_status`.
3. **Both success writers in scope.** `_save_pr_metadata` (remote PR) and
   `_mark_local_only_complete` (local-only) each get the post-commit, post-lock call.

**Settled at implementation:**
4. **`requested_by` provenance string.** The automatic trigger uses
   `"postrun_auto"`; the manual route is unchanged.

---

## Final recommendation

PR-A and PR-B are implemented and remain dormant/read-only by default. The blocking
decisions are preserved (§11: success-terminal only; best-effort call in
`pr_orchestrator` after `complete` commits + lock release; **not**
`_update_run_status`; no shared terminal-settle refactor), and the provenance string is
`"postrun_auto"`. Next work requires a maintainer decision before PR-C activation.
