# Memory M1.5 — DB Memory Conflict Run-Scope Gate (Design)

**Status:** Design only. No pipeline code, no memory-store change, no API, no UI, no
prompt change, no schema change, and **no run blocking** is implemented in PR #16D-1.
**Phase:** M1.5, continuation of [`memory-repo-reality-conflicts.md`](./memory-repo-reality-conflicts.md).
**Mode:** Adversarial. The goal is to gate the *right* runs and never silently block the
wrong ones.

---

## 0. Problem

#16C made stale/conflicting DB memory stop being **injected** into prompts (the existing
`is_stale=0` filter in `build_project_memory_block` excludes it). It deliberately added
**no run blocking**.

But exclusion alone is not enough when the conflict is *relevant to what the user is
doing right now*:

- Memory says **MongoDB**, repo fingerprint says **PostgreSQL**.
- User: *"fix typo in README"* → the conflict is irrelevant → **do not block**.
- User: *"update the DB models"* → the run will write DB/model code against a wrong
  mental model → **pause and make a human resolve the stale memory first.**

#16D designs **when** a clear DB conflict should **block**, **warn**, or **do nothing**,
based on the run's scope. It implements nothing here; it is the contract for #16D-2…4.

Core rule, unchanged: **current repository state > project memory.** The gate never
edits or archives memory; it only pauses a run so a human decides.

---

## 1. Where the gate runs

**Recommended: once, at the start of `_execute_approved_chunks_locked` (and the resume
path `_resume_chunked_pipeline_locked`) in `backend/pipeline/chunked_orchestrator.py` —
i.e. "before executing approved chunks."**

Why this point:

- The chunk plan is already **approved** (`chunk_plan_status == "approved"`), so every
  chunk's `files_expected` is populated (`_definition_by_number`, `chunk.files_expected`)
  — that is the deterministic scope signal the gate needs.
- It runs **once per run**, not once per chunk.
- It sits **before** any branch creation, patch, or commit — a blocked run has written
  nothing.
- Plans that are never executed are never gated.

Rejected alternatives:

| Candidate | Why not |
|---|---|
| Before chunk planning | `files_expected` does not exist yet — no scope signal. |
| Per chunk, before each chunk | Repeats work, noisy, and can pause mid-run after commits. The gate is a per-run decision. |
| Before prompt injection | #16C already excludes stale facts from prompts; nothing left to gate there. |
| Startup hook | Surprising, slow, touches resumable runs. Same conclusion as the backup-cleanup review. |
| Manual only | That is exactly #16C (`verify-repo`). #16D is about automatic, scope-aware gating. |

---

## 2. What determines run scope

A **pure, deterministic** classifier over the **union of all pending chunks'
`files_expected`**:

```
is_db_sensitive_run(files_expected: list[str]) -> bool
```

> **Implemented in #16D-2** as `backend/memory/conflict_scope.py`:
> `is_db_sensitive_run(files_expected)` plus the debug helper
> `get_db_sensitivity_reason(path) -> str | None`. Pure (no I/O, no side effects),
> reuses `repo_indexer.classify_file` for indicator A, and is not yet wired into the
> pipeline.

`db_sensitive = True` only on a **strong** indicator — any expected file where:

1. `backend/repo/repo_indexer.py:classify_file(path)` returns `model` or `migration`; or
2. the normalized path contains a DB token:
   `models/`, `migrations/`, `alembic/`, `prisma/`, `db/`, `repositories/`, `schemas/`,
   `entities/`, `queries/`; or
3. it is a dependency/manifest file that defines the DB stack:
   `requirements.txt`, `pyproject.toml`, `package.json`, `docker-compose.*`,
   `prisma/schema.prisma`.

If no strong indicator is present → **uncertain → warn, never block.**

**Free-text feature/request text is not a blocking input.** It is too fuzzy to justify
pausing a run. It may only *enrich the warning message*. Blocking always requires
file-path / `classify_file` evidence. This keeps the gate conservative: a false block is
a worse experience than a visible warning, and the conflicting fact is already excluded
from prompts regardless.

---

## 3. Statuses and gate mechanism

**Recommended: reuse the existing `approval_gates` mechanism, plus one distinct run
status. No schema change.**

- **Gate row** (`backend/pipeline/approval_gate.py` + `approval_gates` table): a new
  `approval_type = 'memory_conflict'`. The `approval_type`, `status`, `risk_level`,
  `ai_summary`, and `chunk_number` columns already exist, so this is purely a new value
  in a free-text column — no migration. `risk_level='high'`, `chunk_number=0`,
  `ai_summary` = the block message (§7).
- **Run status:** add a code-only string constant in `backend/core/statuses.py`:
  `RunStatus.AWAITING_MEMORY_CONFLICT_APPROVAL = "awaiting_memory_conflict_approval"`.
  Statuses are plain strings (no enum/migration), so this is not a schema change. A
  distinct status keeps the conflict pause from being confused with chunk approval.
- **Decisions** reuse the proven `approve_gate` / `reject_gate` plumbing via thin new
  endpoints (#16D-4), mirroring the chunk-gate decision pattern
  (`_decide_pending_chunk_gate`).

Options considered:

| Option | Verdict |
|---|---|
| A. Reuse high-risk approval-gate mechanism | **Chosen** (gate row + decision plumbing). |
| B. Whole new status/state machine | Overkill; the gate table already models pending→approved/rejected. We take only the one new status string from it. |
| C. Hard-fail the run | Rejected — too blunt, loses the override path, and reads as a crash. |
| D. Warn-only, defer blocking | Adopted as the **first shipped slice** (#16D-3) before blocking (#16D-4). |

> **Implemented in #16D-3 (warning-only):** the read-only evaluator
> `evaluate_db_memory_conflicts(project_id, repo_path, statuses=("active","stale"))`
> in `backend/memory/repo_reality.py` returns a `ConflictReport` without mutating
> memory. `chunked_orchestrator._emit_db_conflict_warning(...)` runs it **once** at
> execute/resume start (before any chunk runs) and, on a conflict, publishes a single
> run `log` event (`level="warning"` when `is_db_sensitive_run(files_expected)` is true,
> else `"info"`; `data.type="memory_db_conflict"`). It **never blocks, pauses, changes
> status, marks memory stale, or creates a gate** — those land in #16D-4. The manual
> `verify-repo` action (#16C) now also calls this evaluator and remains the only path
> that mutates memory.

Non-negotiables: **never silently fail; never auto-edit or auto-archive memory.** At the
gate the human always sees: the stale/conflicting fact, the repo DB signal, the evidence
path, why the run is blocked, and the options — **verify / update / archive / override
once.**

---

## 4. Block / warn / no-action policy

**Block** — create a `memory_conflict` gate and pause the run — only when **all** hold:

- a clear DB conflict exists *now*: the repo `db` signal is present and **not ambiguous**,
  and an **active or stale** `category='db'` memory fact names a **different** engine; AND
- the run is **db-sensitive** (§2 strong indicator).

**Warn** — proceed, surface a non-blocking notice in run detail / the execute response —
when:

- a clear DB conflict exists but run scope is **uncertain**; or
- the repo DB signal is **ambiguous** (multiple engines detected); or
- a DB conflict exists but the run is clearly **unrelated** (docs/non-code); or
- a DB fact is stale for an **unknown / non-conflict** reason.

**No action** when:

- there is no active/stale DB memory; or
- the repo DB signal is **unknown**; or
- it is an unrelated README/doc/typo run with no DB conflict.

Note the asymmetry: a conflict on an unrelated run still **warns** (and the fact stays
excluded from prompts), but only a db-sensitive run is **blocked**.

---

## 5. Knowing a conflict is repo-caused — without schema

**Recommended: recompute at gate time. Do not parse `archived_reason` text** (brittle and
couples the gate to #16C's wording).

Refactor the comparison core of #16C into a **pure, read-only** evaluator:

```
evaluate_db_memory_conflicts(project_id, repo_path) -> ConflictReport
```

It:

- builds the fingerprint (`build_repo_fingerprint`), loads **active and stale**
  `category='db'` facts, maps each fact's content via the existing
  `_extract_db_values_from_content` / `_DB_VALUE_TOKENS`, and
- returns conflicts / ambiguity / evidence **without mutating anything** — no `verify`,
  no `mark_fact_stale`. Mutation stays in #16C's manual action.

`verify_project_db_memory_against_repo` (#16C) is then rewritten to call this evaluator
and apply its mutations, so detection logic lives in exactly one place. The gate calls
the evaluator **read-only**. This makes the gate decision robust, stateless, and
schema-free, and it correctly handles a fact that was staled by a *prior* manual
verification (the conflict still exists in the repo, so the evaluator re-derives it).

`ConflictReport` (shape, not a DB table) carries per-conflict: `fact_id`, `memory_value`,
`repo_value`, `evidence_path`, `evidence_excerpt` (the fingerprint's fixed human string —
never raw file content, never `.env` values), plus `repo_db_signal` and `ambiguous`.

---

## 6. Override-once flow

- **"Override once" = approving the `memory_conflict` gate** (reuse `approve_gate`).
  Because gates are keyed by `run_id`, the override is inherently **run-scoped**.
- **Stored** as the gate row's `status='approved'` + `decided_at` (and approver). No new
  table.
- **Resume:** `_resume_chunked_pipeline_locked` re-evaluates. If an **approved**
  `memory_conflict` gate exists for this run *and the current conflict is unchanged*,
  honor it (do not re-block). If the repo has changed so the conflict differs (or is
  gone), create a fresh gate / proceed accordingly.
- **Expiry:** none by clock; it expires by **scope** — a *new* run re-evaluates from
  scratch and will gate again if still relevant.
- **Visibility:** the gate record appears in run detail (same surface as chunk/final
  gates).
- The override **never** edits or archives memory. Resolving the underlying staleness is
  a separate, explicit human action (#16C `verify-repo`, edit, or archive).

---

## 7. UX copy

**Block** (gate `ai_summary`):

```
Pipewright found a DB memory conflict relevant to this run.
Memory says: MongoDB
Repo evidence says: PostgreSQL (from docker-compose.yml)
This run modifies DB/model/migration files, so it was paused.
Resolve the stale memory (verify / update / archive) or override once to continue.
```

**Warn** (run detail / execute response, non-blocking):

```
Note: a DB memory conflict was detected (memory: MongoDB, repo: PostgreSQL from
docker-compose.yml) but this run does not appear to touch DB files, so it was not
blocked. Conflicting memory is excluded from prompts.
```

Both messages name the memory value, the repo value, and the evidence path. Neither
contains file content or secrets — the evidence excerpt is the fingerprint's fixed
string.

---

## 8. Tests for the implementation

For #16D-2…4 (deterministic, temp repos, isolated DB; mirrors the existing memory suite):

1. DB conflict + README-only `files_expected` → **no block** (warn).
2. DB conflict + a `models/` file expected → **block**.
3. DB conflict + a `migrations/` file expected → **block**.
4. Ambiguous repo signal → **warn, no block**.
5. Unknown repo signal → **no block**.
6. Override once → run continues; a different run re-evaluates (override is run-scoped).
7. Project isolation: a conflict in project A never gates project B.
8. No conflict → no gate, normal execution.
9. Stale-but-unrelated memory (non-DB-sensitive run) → **no block**.
10. Gate evaluation runs **once** per run (call-count assertion at execute/resume).
11. `evaluate_db_memory_conflicts` is **pure** — DB facts are byte-identical before/after
    the call (no `verify`, no `stale` side effects).
12. `is_db_sensitive_run` unit matrix: model/migration/manifest paths → True; docs/route/
    frontend-only → False.

---

## 9. Recommended PR split (split, not combine)

| PR | Scope | Runtime behavior change |
|---|---|---|
| **#16D-1 (this doc)** | Design only | None |
| **#16D-2** | Pure `is_db_sensitive_run` scope classifier + unit tests | None (pure helper) |
| **#16D-3** | Read-only `evaluate_db_memory_conflicts` + **non-blocking warning** surfaced in run-detail / execute response | Adds a warning field; no gating |
| **#16D-4** | Blocking `memory_conflict` gate at execute/resume start + override-once decision endpoints + `AWAITING_MEMORY_CONFLICT_APPROVAL` status | DB-sensitive conflicts pause the run (human-gated, loud) |

Splitting keeps the safe-by-default ethos: ship detection and a visible warning first,
prove the scope classifier and evaluator in real use, then turn on blocking. Each slice
is independently testable and revertible.

---

## 10. What NOT to build (strict list)

- `memory_conflicts` table — defer to #16E (the durable conflict record / resolution UI).
- Full conflict UI / dashboard, frontend changes.
- pgvector, embeddings, semantic memory, PostgreSQL migration.
- Automatic memory rewrite or auto-archive on conflict.
- Cross-project or org memory.
- Per-chunk gating; startup hooks; every-run full repo scans.
- Feature-text-based blocking (free text may only enrich a warning).

---

## 11. Risks and open questions

- **False blocks vs. false warns.** A wrong block is worse than a wrong warn. Mitigation:
  block only on `classify_file`/path evidence + non-ambiguous repo signal; default to warn
  on any uncertainty; always offer override-once.
- **`files_expected` accuracy.** The gate's scope signal is only as good as the planner's
  `files_expected`. If a db-sensitive run lists no DB files, it will only warn. Acceptable
  for M1.5; #16E/telemetry can measure miss rate.
- **Resume + changed repo.** Re-evaluation on resume must compare the *current* conflict,
  not the one captured at first execute, so an approved override is not honored after the
  repo changed underneath it. Covered by §6 and test #6.
- **Status proliferation.** One new run status is the minimum that keeps the conflict
  pause distinct from chunk approval. Resisting a bespoke state machine (Option B) keeps
  the surface small.
- **Stale reason provenance.** Because #16D recomputes rather than trusting
  `archived_reason`, a fact staled for a *non-repo* reason will not be treated as a repo
  conflict unless the repo actually conflicts now — which is the correct, conservative
  behavior.

---

## 12. Confirmation

PR #16D-1 is **design and documentation only**: this doc plus a one-line pointer in
`memory-repo-reality-conflicts.md`. It changes **no** runtime behavior — no gate, no run
blocking, no prompt-format change, no memory-store/API/UI/schema change. The integration
points, gate mechanism, and reuse targets described here are referenced, not modified.

---

## 13. #16D-4 implemented

PR #16D-4 implements the blocking run-scope DB memory conflict gate.

- A clear DB memory conflict on a DB-sensitive run now pauses execution with run status
  `awaiting_memory_conflict_approval` before branch creation, patching, testing, commit,
  push, or PR work.
- The gate uses the existing `approval_gates` table with
  `approval_type = "memory_conflict"`, `risk_level = "high"`, and `chunk_number = 0`;
  there is no schema change and no `memory_conflicts` table.
- Decision endpoints are:
  `POST /runs/{run_id}/memory-conflict/approve` and
  `POST /runs/{run_id}/memory-conflict/reject`.
- Approving the gate is an override-once action scoped to that run. Execute/resume
  re-evaluates the current conflict and honors the approved gate only when it still
  matches the conflict that was approved.
- The conflict signature is stored in the existing gate `test_results` field so the
  override can be compared without adding new columns.
- Approving the gate does not update, verify, archive, or otherwise resolve memory. It
  only lets this run continue; resolving stale memory remains an explicit human action.
