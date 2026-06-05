# Local-First Hardening Design Audit

> Audit ID: **#32A**. Companion roadmap owner for **#32B–#32G**.

## Status

Docs-only audit. **No runtime changes.** No backend, frontend, schema, or
package files are modified by this slice. Every mitigation named here is
explicitly deferred to a later, separately-approved slice. This document
captures findings and a roadmap; it does **not** implement WAL, event-loop
offload, crash recovery, or SQLite vector memory.

---

## Executive Verdict

Pipewright is **safe and correct as trusted local software**. The core safety
invariants (no auto-commit, no final-approval bypass, no auto-merge, no
automatic scope expansion, advisory-only reviewer, display-only operator panel,
display-only PR checks) are intact and were verified against the code during
this audit.

The next phase is **not** SaaS/production hardening. It is **local-first
hardening for open-source adoption**. The risk we are managing is no longer
"can a malicious tenant escalate" — it is "**will a first-time local user lose
trust in the first ten minutes**" because:

* a long test run makes the whole UI/API appear frozen (the event loop is
  blocked by synchronous subprocess work),
* a `Ctrl+C` or crash mid-run leaves the target repo's working tree dirty with
  no clear, human-gated recovery story,
* our own docs tell them to bind the unauthenticated backend to `0.0.0.0`,
* setup fails late and cryptically because an env var / key / CLI / repo path
  was missing, or
* SQLite throws `database is locked` under concurrent access.

None of these are correctness bugs in the safety model. All of them are
**adoption blockers** and **trust hazards**. The highest-severity items are the
event-loop freeze (Critical for perceived reliability) and the dirty-tree /
exposure / diagnostics cluster (High). All proposed mitigations are designed to
**preserve every existing safety invariant** and to **prefer safe failure with
a clear message over risky automation**.

---

## Current Safety Invariants

Verified present during this audit. The roadmap below must not weaken any of
these:

* **No auto-commit.** Commits are human-gated; the no-effective-change guard
  blocks empty commits (`backend/pipeline/chunked_orchestrator.py` checks
  `is_working_tree_clean` before committing).
* **No final-approval bypass.** Implementation only proceeds through
  `/runs/chunked` with chunk-plan approval and final approval. Legacy `/run` is
  permanently disabled (HTTP 410) in `backend/main.py`.
* **No auto-merge.** PR flows never merge.
* **Scope is never auto-expanded.** `scope_guard` fails closed on
  `SCOPE_VIOLATION`; expansion is human-approved (#27).
* **Reviewer is advisory only.** Display-only; does not gate.
* **Operator Attention Panel is display-only.**
* **Backend routes are the source of truth and revalidate before mutation.**
* **PR checks are display-only** and do not gate approval/push/merge; normal Run
  Detail loads do not call GitHub checks automatically (#31).
* **GitHub tokens are never returned in API responses** and are stored
  Fernet-encrypted (`backend/security/secrets.py`), decrypted only at use.
* **Provider/GitHub errors are sanitized** before being stored or returned
  (`sanitize_for_log` in the PR orchestrator).
* **SQLite is acceptable for trusted local/open-source mode.** PostgreSQL is a
  future hosted/team path, not an open-source requirement.

---

## Local-First Scope

Pipewright is **currently trusted local software, not hosted SaaS**. It assumes:

* a single trusted operator on their own machine,
* running against their own repositories,
* with no hosted authentication, no multi-tenant isolation, and no network
  exposure beyond loopback.

This is a legitimate and intentional posture for the open-source adoption phase.
SQLite, in-process locks, ambient git credentials, and a local Fernet key are
all **appropriate for this trust model**. The audit's job is to make that model
*obvious, reliable, and hard to misuse by accident* — not to replace it with a
hosted security model prematurely.

The frozen-UI, dirty-tree, exposure, and diagnostics risks below are all
evaluated **within this trust model**. They are about protecting a trusting
local user from confusion and lost work, not about defending against an
adversary on the same host.

---

## Non-Goals

This audit and the #32 roadmap explicitly do **not** include:

* No hosted/team production implementation.
* No PostgreSQL migration now.
* No pgvector now.
* No real auth / access-control implementation now.
* No Celery/Redis worker queue now.
* No durable multi-worker locking now.
* No SQLite vector implementation now.
* No event-loop offload implementation in *this* slice (designed in #32C).
* No crash auto-recovery — recovery guidance is human-gated only (#32E).

---

## Local-First Risk Register

Severity reflects **impact on local-first adoption and user trust**, not hosted
security exposure.

| # | Risk | Severity | Status today | Mitigation slice |
|---|------|----------|--------------|------------------|
| 1 | Event-loop blocking from synchronous patch/test/git work in async paths makes UI/API appear frozen | **Critical** | Present — `run_tests` and `local_git.*` subprocesses run synchronously inside `async` orchestrator functions | #32C |
| 2 | Crash / `Ctrl+C` mid-run leaves target repo working tree dirty; startup recovery reconciles DB state only, not Git | **High** | Present — `recover_interrupted_runs` updates DB rows; no Git working-tree reconciliation | #32E |
| 3 | Docs encourage binding the unauthenticated backend to `0.0.0.0` | **High** | Present — several docs use `--host 0.0.0.0`; dev scripts + README correctly use `127.0.0.1` | #32B |
| 4 | Late/cryptic startup failures from missing env/key/CLI/repo/test-command | **High** | Partial — failures surface at point-of-use, not as upfront diagnostics | #32B |
| 5 | SQLite `database is locked` under concurrent access | **Medium** | Present — engine has no WAL / `busy_timeout` pragmas | #32D |
| 6 | PR-mode behavior (esp. `manual_token` push relying on ambient git credentials) is under-documented | **Medium** | Behavior correct; docs thin | #32B / #32F |
| 7 | No simple backup/reset guide; DB and encryption key must travel together | **Medium** | Present — no guide; silent decrypt failure if key lost | #32F |
| 8 | No documented readiness design for future SQLite vector memory | **Low** | N/A — design gap only | #32G |

---

### Finding 1 — Event-loop blocking (Critical)

The chunked orchestrator is built on `async def` (e.g.
`execute_approved_chunks`, `_execute_single_chunk`, `resume_chunked_pipeline`,
`retry_failed_chunk` in `backend/pipeline/chunked_orchestrator.py`). Inside
those coroutines, the heavy work is **synchronous and blocking**:

* `run_tests(...)` (`backend/pipeline/tester.py`) calls `subprocess.run(...)`
  directly — a real test command that can run for many seconds or minutes.
* `local_git.*` helpers (`backend/git/local_git.py`) shell out via
  `subprocess.run(...)` for status/checkout/commit/push.

Because none of these are offloaded to a thread/executor, they **block the
asyncio event loop** for their full duration. While a test suite runs, the
FastAPI process cannot service other requests or WebSocket events, so the **UI
and API appear frozen**. A first-time user running a real project's test command
will very plausibly see a hang and conclude the tool is broken.

**Likely areas to inspect** (for the future #32C fix): chunked orchestrator
execution paths, `tester.run_tests`, and the `local_git` subprocess helpers.

**Future mitigation (design only, #32C):** offload blocking subprocess work onto
a thread/executor (e.g. `asyncio.to_thread` / `run_in_executor`) so the event
loop stays responsive — **while preserving project-lock coverage** so a project
still cannot run two operations concurrently. Do not change *what* the work
does; only move *where it runs* relative to the loop.

### Finding 2 — Crash / Ctrl+C dirty working tree (High)

`recover_interrupted_runs` (`backend/runtime/startup_recovery.py`) runs at
startup and reconciles **database state only**: it resets `running` chunks to
`pending` and marks `running`/`running_chunks` runs as `interrupted`. It does
**not** inspect or reconcile the **Git working tree** of the target repo.

So if the server is killed mid-chunk (after a patch was applied but before
commit), the DB will say "interrupted / pending" while the **target repo is left
dirty** with an applied-but-uncommitted patch. The user has no in-product
explanation of why their repo is dirty or what to do.

**Future mitigation (design only, #32E):** at startup, *detect* a dirty target
working tree associated with an interrupted run and surface **human-gated
guidance** (e.g. "Run X was interrupted; the working tree has uncommitted
changes — review with `git status`, then choose to keep or discard"). This must
be **guidance only**.

> **Explicitly forbidden in #32E:** no auto-reset, no auto-`git checkout`/`git
> stash`, no auto-resume of the run, no auto-commit. The tool detects and
> explains; the human decides and acts.

### Finding 3 — Local-only exposure (High)

The app has **no hosted auth**. Trusted local use is acceptable, but **several
docs currently instruct users to bind the backend to `0.0.0.0`**, which exposes
the unauthenticated API to the local network:

* `DECISIONS.md` (`--host 0.0.0.0` in two places)
* `docs/ops/pre-deployment-checklist.md`
* `docs/memory/m1-memory-smoke-test.md`
* `docs/phase2b-smoke-tests.md`

The good news: the **canonical paths are already correct** — `README.md`,
`scripts/dev.ps1`, `scripts/dev.sh`, and `docs/demo/local-self-use-demo.md` all
use `--host 127.0.0.1`. The README already states it is a single-user local
tool with no hosted auth.

**Future mitigation (design only, #32B):** scrub the stray docs to prefer
`127.0.0.1`, and add a **loud, log-only warning** if the backend is ever started
on a non-loopback bind (e.g. `0.0.0.0`), reminding the operator there is no auth.
**Do not** propose full hosted auth as immediate work — that is a hosted/team
non-goal.

### Finding 4 — Startup diagnostics & environment validation (High)

Today, missing configuration tends to fail **at point-of-use** with a localized
error rather than as an upfront, friendly diagnostic. Examples found:

* `PIPEWRIGHT_ENCRYPTION_KEY` / Fernet validity is only checked when a token is
  encrypted/decrypted (`backend/security/secrets.py` raises
  `"PIPEWRIGHT_ENCRYPTION_KEY is required..."`).
* Provider API keys are validated lazily (e.g. `GEMINI_API_KEY` errors only when
  Gemini is actually used).
* `gh` CLI availability/auth is only relevant for `github_cli` mode.
* Repo path existence / "is a git repo" and a configured test command are
  assumed.

**Future mitigation (design only, #32B):** a startup/diagnostics check that
reports, in one place, the state of: encryption key (present + valid Fernet),
selected providers' key presence, `gh` availability/auth *when* `github_cli`
mode is in use, target repo path exists and is a git repo, and whether a test
command is configured.

> For local mode these must be **clear, log-only warnings** for *optional* gaps —
> not hard failures. We do not want to block a user who legitimately runs an
> Anthropic-only config from starting because `GEMINI_API_KEY` is unset. Only
> truly required-for-the-requested-operation items should hard-fail, and only at
> the point that operation is attempted.

### Finding 5 — SQLite local reliability (Medium)

SQLite is the open-source local default and **should remain so**. The engine
(`backend/db/database.py`) is created with `check_same_thread=False` but
**without** `PRAGMA journal_mode=WAL` or `PRAGMA busy_timeout`. Under concurrent
access (a background run writing while the UI reads, or a stray
`uvicorn --workers >1`), this can surface `database is locked` errors — a
confusing failure for a local user.

**Future mitigation (design only, #32D):** enable `WAL` + a reasonable
`busy_timeout` on connect. This improves read/write concurrency and lets writers
wait briefly instead of erroring immediately. **PostgreSQL is not required for
the local quickstart** and remains the hosted/team production target.

### Finding 6 — PR mode clarity (Medium)

The three modes (`backend/pipeline/pr_orchestrator.py`,
`backend/projects/pr_modes.py`) behave as documented in CLAUDE.md, but the
*credential* behavior deserves explicit user-facing documentation:

* **`local_only`** (default): no push, no PR, no GitHub required. Commits
  locally and shows manual push/PR instructions.
* **`github_cli`**: pushes via `local_git.push_branch` (`git push -u origin`),
  then uses the `gh`/PyGithub flow. Requires `gh` installed and authenticated.
* **`manual_token`**: **verified behavior** — the *PR creation* uses the stored
  (decrypted) token via PyGithub, but the **push itself uses
  `local_git.push_branch`, i.e. `git push -u origin <branch>`, which relies on
  ambient local git credentials**, not the stored token injected into the push
  URL.

That last point is the one most likely to surprise a user ("I gave you a token,
why does push need my git credentials too?"). #32B/#32F docs should state it
plainly. *(This claim was verified in code during the audit; future doc slices
should re-verify before publishing user-facing guarantees.)*

### Finding 7 — Backup / reset story (Medium)

Local state is spread across: the SQLite DB (`backend/db/pipewright.db` or
`PIPEWRIGHT_DB_PATH`), memory facts/suggestions (in the DB), project config,
**encrypted GitHub tokens** (in the DB), the `PIPEWRIGHT_ENCRYPTION_KEY` (in the
environment), and the **target repos' branches/worktrees** (outside the app
entirely).

There is **no backup/reset guide** today. The sharp edge:
`secrets.decrypt_secret` fails if the key is missing or changed — so **if the DB
is restored without the matching `PIPEWRIGHT_ENCRYPTION_KEY`, stored tokens
become permanently unreadable**.

**Future mitigation (design only, #32F):** a simple reset/backup guide that
documents what to back up, how to reset cleanly, and a loud warning that **the
DB and the encryption key must travel together**.

### Finding 8 — Future SQLite vector memory readiness (Low)

A design gap only — see the dedicated section below. No implementation now.

---

## Open-Source Adoption Blockers

Ranked by how quickly they erode a first-time user's trust:

1. **Frozen UI during test runs (Finding 1).** The single most likely "this is
   broken" moment. Critical for *perceived* reliability even though the safety
   model is intact.
2. **Dirty repo after interruption (Finding 2).** A user who hits `Ctrl+C` and
   finds their repo modified with no explanation will not trust the tool with
   their code again.
3. **Accidental network exposure from our own docs (Finding 3).** We should
   never be the reason an unauthenticated backend is on `0.0.0.0`.
4. **Setup cliff (Finding 4).** Late, cryptic config failures cause abandonment
   during onboarding.
5. **`database is locked` (Finding 5).** Looks like data corruption to a
   newcomer; trivially mitigated with WAL + busy_timeout.
6. **PR-mode confusion (Finding 6) and no backup/reset story (Finding 7).**
   Lower urgency but real friction.

---

## SQLite Decision

**SQLite stays as the open-source local default.** It is the right call for a
single-trusted-operator, single-machine tool: zero-setup, file-based, no server
to run. The only reliability gap for local use is concurrency robustness, which
is addressed by the *small, local* WAL + `busy_timeout` change in #32D — **not**
by migrating databases.

PostgreSQL is **not** required for local quickstart and is intentionally
deferred. It remains the **future hosted/team production target** (multi-worker,
durable locking, pgvector). Adopting it now would add a server dependency that
directly contradicts the local-first adoption goal.

---

## Future SQLite Vector Memory Readiness

**Do not implement now (designed in #32G, docs-only).** This section records the
intended local semantic-memory path so future work has a safe target.

* **Local path:** SQLite vector / FTS / hybrid search as the local semantic
  memory backend.
* **Hosted path:** PostgreSQL + pgvector remains the future hosted/team semantic
  memory backend.

**Current memory safety principles that must remain true** in any future design:

* project-scoped memory (every fact carries `project_id`),
* human approval before a memory fact is created (suggestions → approval →
  fact),
* rejected-memory dedupe (pending-suggestion + active-fact dedupe indexes
  already exist in `backend/db/database.py`),
* stale/archive lifecycle (`status`, `is_stale`, `archived_reason`),
* no secret embedding,
* no untrusted repo text automatically becoming trusted memory.

**Future readiness requirements** (to design before any vector work ships):

* embedding model/version tracking (so re-embedding on model change is possible),
* fact-level provenance,
* source run / chunk / PR linkage (columns like `source_run_id`,
  `source_chunk_number`, `source_type`, `source_ref` already exist on
  `memory_suggestions`),
* a **hard retrieval filter applied before vector ranking**:
  `project_id` + `active` + non-stale, *then* rank by vector similarity (never
  rank first and filter after),
* role-aware retrieval,
* secret/PII exclusion at ingest and at retrieval,
* a migration path to pgvector **behind a memory-store interface**, so the local
  SQLite vector store and the hosted pgvector store are swappable without
  touching pipeline code.

---

## Future Hosted/Team Production Boundary

Everything below is **future only** and explicitly out of scope for #32 and for
the open-source adoption phase:

* PostgreSQL required for hosted/team mode.
* pgvector for hosted/team semantic memory.
* durable DB / project locks for multi-worker mode (today's `run_locks.py` is
  **in-process only** and does not survive restarts or span workers).
* real auth / access control.
* team / user / project ownership model.
* hosted repo sandboxing.
* KMS / envelope encryption (today's Fernet local-key model is local-appropriate
  only).
* worker queue / Celery / Redis.
* rate limiting / cost controls.
* enterprise audit / export.

Naming these here keeps them out of the local-first slices and prevents
scope creep toward premature SaaS hardening.

---

## Recommended #32 Local-Hardening Roadmap

Each slice is independently approvable and small. **#32A is this document.**

### #32A — Local-first hardening design audit *(this doc)*

* **Goal:** capture findings + roadmap; change no runtime behavior.
* **Likely files:** `docs/design/local-first-hardening.md` only.
* **Tests to add:** none (docs only).
* **Safety invariants to preserve:** all (no code touched).
* **What not to touch:** backend, frontend, schema, packages.

### #32B — Startup diagnostics + local-only exposure docs/fence

* **Goal:** one upfront diagnostics report (log-only warnings for optional gaps);
  scrub docs to `127.0.0.1`; loud log warning on non-loopback bind.
* **Likely files:** `backend/main.py` (lifespan diagnostics), a small new
  `backend/runtime/` diagnostics helper, `backend/core/config.py`;
  docs: `DECISIONS.md`, `docs/ops/pre-deployment-checklist.md`,
  `docs/memory/m1-memory-smoke-test.md`, `docs/phase2b-smoke-tests.md`.
* **Tests to add:** unit tests for the diagnostics helper (present/missing
  key, provider key presence, repo-path/git check, test-command presence) and
  for the non-loopback-bind warning trigger.
* **Safety invariants to preserve:** optional gaps warn, never hard-fail at
  startup; never print secret values; no new network surface.
* **What not to touch:** no hosted auth; do not change default bind behavior of
  user-invoked commands beyond docs + warning.

### #32C — Event-loop blocking fix

* **Goal:** offload blocking subprocess work (tests, git) off the event loop
  while preserving project-lock coverage.
* **Likely files:** `backend/pipeline/chunked_orchestrator.py`,
  `backend/pipeline/tester.py`, `backend/git/local_git.py` (or a thin async
  wrapper around them).
* **Tests to add:** a test proving the event loop stays responsive (another
  coroutine/request progresses) while a simulated long-running test command is
  in flight; a test proving the project lock still serializes concurrent runs.
* **Safety invariants to preserve:** project lock still prevents concurrent
  operations per project; no change to patch/test/commit ordering or gating; no
  auto-commit.
* **What not to touch:** the *logic* of patch/test/git steps; approval flow.

### #32D — SQLite WAL + busy_timeout

* **Goal:** enable `journal_mode=WAL` and a sane `busy_timeout` on connect.
* **Likely files:** `backend/db/database.py` (engine connect hook / event
  listener).
* **Tests to add:** a test asserting `PRAGMA journal_mode` returns `wal` and
  `busy_timeout` is set on a fresh connection; a basic concurrent
  read-while-write test that no longer raises `database is locked`.
* **Safety invariants to preserve:** schema unchanged; existing DBs keep
  working; no data migration.
* **What not to touch:** no PostgreSQL; no schema files; no ORM model changes.

### #32E — Git-aware crash recovery guidance

* **Goal:** detect a dirty target working tree tied to an interrupted run and
  surface **human-gated** guidance.
* **Likely files:** `backend/runtime/startup_recovery.py` (detection only),
  a read-model/route to expose the guidance, frontend display in Run Detail.
* **Tests to add:** unit tests for "interrupted run + dirty tree ⇒ guidance
  surfaced" and "clean tree ⇒ no guidance"; an explicit test that recovery
  performs **no** git mutation.
* **Safety invariants to preserve:** **no auto-reset, no auto-resume, no
  auto-commit, no auto-stash/checkout**; detection + guidance only; human
  decides.
* **What not to touch:** the existing DB-state reconciliation behavior must stay
  correct; no automatic git writes anywhere.

### #32F — Setup / reset / backup docs

* **Goal:** document what local state exists, how to back it up, how to reset,
  and that **DB + encryption key must travel together**; document PR-mode
  credential behavior (Finding 6).
* **Likely files:** `docs/setup/` (new reset/backup guide), README cross-link.
* **Tests to add:** none (docs only); optionally a doc-lint/link check if
  tooling exists.
* **Safety invariants to preserve:** never instruct users to commit secrets or
  the encryption key into a repo; never expose token values.
* **What not to touch:** backend, frontend, schema, packages.

### #32G — SQLite vector memory readiness design (docs-only)

* **Goal:** finalize the local semantic-memory readiness design from the section
  above; define the memory-store interface and the hard pre-ranking retrieval
  filter.
* **Likely files:** `docs/design/` (new memory-readiness design doc).
* **Tests to add:** none (docs only).
* **Safety invariants to preserve:** project-scoped, human-approved,
  dedupe/stale lifecycle, no secret embedding, no untrusted-text auto-trust,
  filter-before-rank.
* **What not to touch:** no vector implementation; no schema; no pgvector.

---

## Validation For This Docs-Only Slice

Because this slice is documentation only, validation is intentionally light:

```bash
git diff --check
```

* `git diff --check` — confirms no whitespace errors / conflict markers in the
  new doc.
* No backend or frontend tests are required or run for this slice; there are no
  code changes to exercise.
* Run docs tooling only if it exists in the repo; do not run unrelated heavy
  test suites.

**What was intentionally not changed:** no backend code, no frontend code, no
schema files, no package files, and no runtime behavior. WAL, event-loop
offload, crash recovery, and SQLite vector memory are designed here but
**deliberately not implemented**.
