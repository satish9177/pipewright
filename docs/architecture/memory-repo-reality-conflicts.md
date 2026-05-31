# Memory M1.5 — Repo Reality Conflict Detection (Design)

**Status:** Design only. No code, no schema, no API, no UI, no prompt change in this PR (#16A).
**Phase:** M1.5 — a focused, deterministic slice pulled forward from the M2 "stack
fingerprint" sketch in [`memory-architecture.md`](./memory-architecture.md) §3.5.
**Mode:** Adversarial. The goal is to find where stale memory silently poisons a run,
not to celebrate that M1 shipped.

---

## 0. Why this exists (the one failure M1 cannot detect)

M1 is project-scoped, human-gated, secret-filtered, and budget-bounded. It has one
hole, named explicitly in `memory-architecture.md` §15:

> "The first time a memory entry 'becomes wrong' (because the repo changed), no part
> of M1 will detect it automatically. The signal will be the model producing weird
> output."

Concrete failure:

1. Active memory fact: `[db] Project uses PostgreSQL.` (approved months ago).
2. The repo migrated to MongoDB: `requirements.txt` now has `pymongo`, no `psycopg`.
3. User asks: "update the DB models."
4. M1 injects the PostgreSQL fact as current truth. The coder writes SQLAlchemy/Postgres
   code against a Mongo repo.

The core ordering rule that M1.5 enforces mechanically:

> **Current repository state > Project State Memory > Semantic Memory.**
> Memory is advisory. Deterministic source/config signal wins on conflict.

M1.5's job is narrow: when a **deterministic** repo signal **clearly** contradicts an
active memory fact, **exclude that fact from prompts** and, **only when the conflict is
relevant to the requested run**, force a human gate before proceeding.

---

## 1. Goal and non-goals

### Goal

- Detect **high-impact, high-confidence** conflicts between active project memory and
  **deterministic** repo signals (manifests/config — not AI, not heuristics-on-prose).
- **Exclude** the conflicting memory fact from the injected prompt block.
- **Warn or block** only when the conflict category intersects the **scope of the
  requested run**. An unrelated README edit is never blocked by a DB conflict.
- Always prefer a clear, loud human gate over silent guessing or silent auto-rewriting.

### Non-goals (explicitly out of M1.5)

- No semantic/vector memory, no pgvector, no embeddings, no PostgreSQL move.
- No full repo scan on every run. Read only capped manifest/config files.
- No automatic rewriting of memory **content**. M1.5 may flip a status flag; it never
  edits the human-authored text.
- No bulk approval, no cross-project memory, no org/shared memory.
- No complex UI in the first slices. Detection + exclusion + gate first; resolution UI last.
- No new prompt format. The exclusion path reuses the existing `is_stale` filter
  (see §6).

---

## 2. Conflict categories

Start deterministic. Each category maps a **repo signal** to an existing M1 memory
`category`, with a default severity and a run-scope-gated block/warn behavior.

| Conflict category | Signal files | Example repo evidence | Maps to memory `category` | Default severity | Behavior when run scope matches |
|---|---|---|---|---|---|
| `db` | `requirements.txt`, `pyproject.toml`, `package.json`, `docker-compose.yml`, `prisma/schema.prisma`, `.env.example` (var **names** only) | `pymongo` present, `psycopg`/`asyncpg` absent → Mongo, not Postgres | `db` | **high** | **block** (gate) on DB/model/migration/query runs |
| `test_runner` | `package.json` scripts, `pytest.ini`, `pyproject.toml`, `jest/vitest config`, `pom.xml`, `build.gradle` | `vitest` config present, memory says "Jest" | `test` | **high** | **block** on test-writing / test-command runs |
| `framework_backend` | `requirements.txt`, `pyproject.toml`, `package.json`, `pom.xml`, `build.gradle` | `fastapi` absent, `django` present; memory says FastAPI | `stack` | medium–high | **block** on backend route/service runs; warn otherwise |
| `build_tool` | `package.json` (`packageManager`, lockfile presence), `pom.xml`, `build.gradle`, `Cargo.toml`, `go.mod` | `pnpm-lock.yaml` present, memory says "npm" | `stack` / `deploy` | medium | **block** on build/dependency runs; warn otherwise |
| `orm_migration` | `alembic.ini`, `migrations/`, `prisma/schema.prisma`, `package.json` (`prisma`, `typeorm`, `sequelize`) | `alembic.ini` gone, `prisma/` present | `db` | medium–high | **block** on migration runs; warn otherwise |
| `language_primary` | `pyproject.toml`/`requirements.txt` (Python), `package.json`+`tsconfig.json` (JS/TS), `pom.xml`/`build.gradle` (JVM), `go.mod`, `Cargo.toml` | memory says "JavaScript", repo has `tsconfig.json` + `.ts` dominant | `stack` | medium | **warn** (migrations are gradual; rarely a hard block) |
| `folder_layout` | top-level directory listing only | memory says "backend in `api/`", repo has `backend/` | `structure` | low | **warn** only — never block |

**Implementation starts with `db` only** (#16B/#16C). It is the highest-impact,
lowest-ambiguity signal (a project is essentially never on two primary databases by
accident), and Pipewright's own dogfood repo exercises it.

### Severity definitions

- **high** — single, unambiguous, mutually-exclusive signal (Postgres XOR Mongo driver).
  Eligible to set `is_stale` and to block a scope-matched run.
- **medium** — clear but a project might legitimately hold both (e.g., two test runners
  during a migration). Excludes from prompt only when the signal is exclusive; otherwise
  warns.
- **low** — structural/cosmetic. Warn only. Never excludes, never blocks.

---

## 3. Repo signal extraction

### Reuse, don't reinvent

`backend/memory/bootstrap.py` **already** implements the exact extraction surface M1.5
needs, safely:

- `_discover_manifest_files(root)` — walks at most `BOOTSTRAP_MAX_DEPTH=5`, skips
  `node_modules`/`venv`/`.git`/etc., caps at `BOOTSTRAP_MAX_MANIFEST_FILES=100`.
- `_load_repo_file(root, rel)` — resolves through `validate_safe_relative_path`
  (path-traversal safe), caps file size at 200 KB, returns `None` on anything unsafe.
- `_collect_candidates(root)` — already detects FastAPI/Django/Flask, SQLAlchemy,
  pytest, **PostgreSQL/MySQL/MongoDB**, Python 3.11, and `package.json` scripts/deps.

**Design decision:** #16B extracts a shared, pure module
`backend/repo/repo_fingerprint.py` (name TBD) that returns a structured
`RepoFingerprint`, and `bootstrap.py` is refactored to consume it. This avoids two
divergent extractors drifting apart — the exact duplication risk PR #15F just fixed for
forbidden paths. The shared module is the single source of "what the repo actually is."

### Extractors (deterministic parse first, substring fallback)

| Source | Parse strategy | Signal extracted |
|---|---|---|
| `package.json` | `json.loads`; inspect `dependencies`/`devDependencies`/`scripts`/`packageManager` | backend framework, test runner, build tool, ORM (prisma/typeorm/sequelize), DB driver (`pg`, `mongodb`, `mysql2`) |
| `requirements.txt` | line scan, normalize names | DB driver, framework, pytest |
| `pyproject.toml` | TOML parse (`tomllib`, py3.11 stdlib) with substring fallback | deps, `requires-python`, test config |
| `pom.xml` | element/text scan (no full XML trust); substring fallback | JVM framework, build = maven, test runner |
| `build.gradle` / `.kts` | substring scan | build = gradle, JVM framework |
| `docker-compose.yml` / `compose.yaml` | YAML parse with substring fallback | DB **service images** (`postgres:`, `mongo:`, `mysql:`) — strong DB signal |
| `alembic.ini` + `migrations/` presence | file existence | ORM/migration = Alembic |
| `prisma/schema.prisma` | provider line scan (`provider = "postgresql"`) | DB **and** ORM = Prisma (authoritative) |
| `.env.example` / `.env.sample` | **variable NAMES only** | weak DB hint (`MONGO_URL`, `DATABASE_URL=postgres...` → name only, never the value) |
| top-level folders | directory listing | folder layout |

### Hard extraction rules

- **Never read `.env`.** It is already blocked by `is_forbidden_path`; only
  `.env.example`/`.env.sample` are readable, and **only variable names** are used —
  never values. Evidence excerpts must redact anything after `=`.
- Inspect only **capped** manifest/config files (reuse the 100-file, depth-5, 200 KB caps).
- **Unknown signal → no action.** If the extractor cannot determine a category, M1.5
  does nothing for that category. Absence of evidence is never treated as conflict.
- **Ambiguous signal → warn, never auto-stale.** Two mutually-coexisting signals (e.g.
  both `pytest` and `jest` configs present) is "warn," not a clear conflict.
- **Clear, exclusive conflict → exclude memory from prompt (set `is_stale`) and, if the
  run scope matches, block via a human gate.**

---

## 4. Memory comparison

Per relevant category, for a given `project_id`:

1. Load **active** memory facts for the category
   (`status='active' AND is_stale=0`, scoped by `project_id` — the M1 isolation
   invariant is non-negotiable).
2. Derive the repo's value for that category from the fingerprint (§3).
3. Compare:
   - **Same** → call the existing `verify_fact()` to bump `last_verified_at`. (The
     "verify" endpoint already exists and resets staleness signals.) This is the
     *positive* path and is just as important: it records that memory was confirmed
     against reality.
   - **Unknown / ambiguous** → no mutation. Optionally emit a warning surfaced in run
     detail.
   - **Clear, exclusive conflict (high confidence)** → record a conflict result and set
     the fact `is_stale=1`. **Never edit `content`.**

### Mechanism note (important, grounded in shipped code)

`build_project_memory_block` (prompt_builder.py) already selects only
`is_stale = 0 AND status = 'active'`. **Setting `is_stale=1` therefore already excludes a
fact from every injected prompt** — no prompt-builder change is required for exclusion.

> Divergence from the M1 doc: `memory-architecture.md` §2.4/§7.2 describe stale facts as
> *still injected with a `[stale]` tag*. The shipped builder **excludes** them. M1.5 is
> designed against the shipped behavior (exclude), which is the safer of the two. If the
> team ever restores `[stale]`-tagged injection, M1.5 must switch to a dedicated
> `conflict_excluded` flag so conflicting facts stay out regardless. Flagged as an open
> question in §13.

### What M1.5 must never do

- Never rewrite or "correct" `content` automatically.
- Never archive automatically (archive requires a human reason; it is a human decision).
- Never promote the repo signal into a new memory fact automatically — that goes through
  the existing **suggestion** queue (human-gated), exactly like bootstrap.
- Never widen scope: a conflict in project A never touches project B.

---

## 5. Block vs warn policy

The decision is a function of **(severity, exclusivity, run-scope intersection)**. A
conflict only **blocks** when it is high-confidence **and** the requested run touches the
conflicting category.

### Block (halt at run start with a human gate — never a silent kill)

- `db` conflict during DB / model / migration / query work.
- `test_runner` conflict during test-writing or test-command work.
- `build_tool` conflict during build / dependency work.
- `framework_backend` conflict during backend route/service work.

"Block" reuses the existing approval/gate machinery (the same loud, human-gated stop M1's
high-risk path already uses). It is a gate, not a crash. The gate message states the
memory value, the repo value, and the evidence path.

### Warn (inject the rest of memory, surface a notice, proceed)

- `folder_layout` differences.
- Ambiguous framework signals (two frameworks both present).
- REST vs GraphQL mixed repo.
- Partial JS → TS migration (`language_primary` mid-migration).

### No action

- Unknown repo signal (extractor inconclusive).
- Run scope unrelated to the conflict (e.g., a docs/README edit during a `db` conflict —
  the DB fact is still excluded from the prompt, but the run is **not** blocked).

### Run-scope inference (deterministic, conservative)

Reuse what already exists: chunk `files_expected` and the existing `classify_file`
classifier in `repo_indexer.py` (it already yields `model`, `migration`, `route`,
`service`, `test`, `config`, …). Map a run/chunk to "DB-sensitive" when its expected
files classify as `model`/`migration` or live under migration/model paths. When scope is
**uncertain, default to warn, not block** — a false block is a worse user experience than
a visible warning, and the fact is already excluded from the prompt either way.

---

## 6. Prompt injection behavior

- **Conflicting memory is excluded from the prompt block** by the existing `is_stale=0`
  filter. No new format, no new builder path.
- When ≥1 fact was excluded for the role being built, the block **may** carry a single
  short line (kept inside the existing sentinels, one line, no paragraph — consistent with
  §7.5 of the M1 doc):

  ```
  Note: 1 memory entry was excluded because it conflicts with current repo signals.
  ```

  This line is informational; it never contains the stale value (so a wrong fact is not
  re-introduced into the prompt by the warning itself).
- The existing footer ("source code wins on conflict") stays. M1.5 makes that sentence
  *mechanically true* for the detected categories instead of relying on the model to obey it.
- **Stale and `historical` facts are never injected as current guidance.** `historical`
  is already an allowed status and already non-`active`, so it is already excluded.

---

## 7. Human resolution flow (design only — no UI in M1.5 first slices)

When a human reviews a detected conflict, the future choices are:

| Choice | Effect | Notes |
|---|---|---|
| **Update memory** | Human edits content to match reality; fact returns to `active`, `is_stale=0`, `last_verified_at=now` | Uses existing edit path; not auto-applied |
| **Archive memory** | Existing `archive_fact` with required reason | For facts that should not have existed |
| **Mark historical** | Set status `historical` (already supported) | "This *was* true; keep for audit, never inject" |
| **Dismiss (detector wrong)** | Clear the conflict, `verify_fact` the memory | The detector produced a false positive; record it |
| **Override once for this run** | Proceed past the gate for the current run only; no persistent state change | The run-level escape hatch; logged |
| **Keep stale but excluded** | Leave `is_stale=1`; do nothing else | Defer the decision; fact stays out of prompts |

No UI is built in M1.5's first slices. These map cleanly onto existing endpoints
(verify/archive/edit) plus, eventually, a conflict-resolution surface (#16E).

---

## 8. Data model options

### Option A — No new table; compute on demand, set `is_stale`

- **Pros:** Zero schema change. Exclusion works immediately via the existing
  `is_stale=0` filter. Smallest, safest first slice.
- **Cons:** No durable record of *why* a fact is stale (repo_value, evidence). The "why"
  lives only in logs / run detail. Human resolution UI has nothing structured to render.
  Re-running detection re-derives everything.

### Option B — Add `memory_conflicts` table (propose for future; **do not add in #16A**)

```
memory_conflicts (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    memory_fact_id  TEXT NOT NULL,
    category        TEXT NOT NULL,        -- db | test_runner | ...
    memory_value    TEXT,                 -- what memory claimed
    repo_value      TEXT,                 -- what the repo shows
    evidence_path   TEXT,                 -- e.g. requirements.txt
    evidence_excerpt TEXT,                -- capped, redacted (no .env values)
    severity        TEXT,                 -- low | medium | high
    status          TEXT,                 -- open | resolved | dismissed | overridden
    created_at      DATETIME,
    resolved_at     DATETIME,
    resolution      TEXT                  -- updated | archived | historical | dismissed | override_once
)
```

- **Pros:** Durable, queryable, drives the resolution UI; mirrors the existing
  `memory_suggestions` shape (which already has `evidence_path`/`evidence_excerpt`).
- **Cons:** New table + migration + lifecycle management. Only worth it once human
  resolution UI exists.

### Option C — `repo_fingerprints` table (persist latest fingerprint)

- **Pros:** Skip re-extraction; enables "what changed since last run" diffs.
- **Cons:** Introduces fingerprint **staleness of the fingerprint itself** — now there
  are two things that can be out of date. Recomputing from capped manifests is cheap
  (≤100 small files), so this is premature.

### Recommendation (staged)

1. **First behavioral slice (#16C): Option A.** Compute on demand, exclude via
   `is_stale`, log the conflict with evidence into run detail. Fast, schema-free safety win.
2. **When resolution UI lands (#16E): add Option B** (`memory_conflicts`). Only then is a
   durable, queryable record justified.
3. **Defer Option C** until extraction cost is actually measured to matter.

---

## 9. Pipeline integration design — where M1.5 runs

| Candidate site | Risk | Verdict |
|---|---|---|
| Project creation / bootstrap | Low — already walks manifests there | **Yes (early).** Compute fingerprint at bootstrap; compare against any seeded memory. |
| Manual "Verify memory" action | **Lowest** — explicit, no run impact | **Yes — ship first.** Zero runtime-behavior risk; pure opt-in. |
| Before prompt memory injection (all runs) | Medium — adds work to every run | No (first). Too broad; violates "no full scan every run." |
| Before DB/test/framework-sensitive runs only | Medium, scoped | **Yes (second).** Run detection only when the run's scope intersects a covered category. |
| Startup hook | High — surprising, slow, touches resumable runs | **No. Never.** (Same conclusion as the backup-cleanup review: startup is the wrong place.) |

### Recommended safest-first integration order

1. **Manual `verify-memory` action (#16C)** — operator/endpoint triggers detection for a
   project; conflicts get `is_stale=1` + logged. No pipeline behavior changes at all.
2. **Bootstrap-time comparison (#16C/#16D)** — reuse the existing bootstrap walk; compare
   the fingerprint against seeded memory and flag conflicts at project setup.
3. **Pre-prompt-injection, scope-gated (#16D)** — only for runs whose scope intersects a
   covered category (start: `db`). Conflicting facts are already excluded by `is_stale`;
   the gate fires only on a scope match. Computed **once at run start**, never per chunk
   (consistent with the M1 run-start snapshot principle).

This ordering means the **first** shippable code changes **no run behavior** — it only
adds an explicit verification tool and excludes provably-wrong facts from prompts.

---

## 10. Tests for future implementation

For the `db`-first slices (#16B–#16D). All unit-level, deterministic, using temp repos
and an isolated DB — mirroring the existing memory test suite.

1. `db` memory says PostgreSQL + repo manifests show MongoDB → conflict detected, fact
   `is_stale=1`, excluded from `build_project_memory_block`.
2. `db` memory matches repo (both Postgres) → no conflict; `last_verified_at` updated.
3. Unknown DB signal (no driver in any manifest) → **no conflict, no mutation**.
4. Ambiguous signal (both `psycopg` and `pymongo` present) → **warn only**, fact **not**
   staled.
5. Detected `db` conflict + DB-shaping run scope (chunk touches `models/`/`migrations/`)
   → run **blocked** at a human gate.
6. Detected `db` conflict + unrelated run (README edit) → run **not blocked**; fact still
   excluded from the prompt.
7. Staled/conflicting fact is **never** present in any role's injected block.
8. **Project isolation:** a conflict in project A never flags or affects project B.
9. **`.env` values never read** — extractor reads only `.env.example` names; a fake
   `.env` with a secret is never opened (guarded by `is_forbidden_path`).
10. Evidence excerpts are **capped and redacted** — no value after `=` in any
    `.env.example` line ever appears in the recorded evidence.
11. `historical`-status fact is never injected (regression guard).
12. Detection runs **once at run start**, not per chunk (call-count assertion).
13. Extraction respects the existing caps (depth 5, ≤100 files, ≤200 KB) — a deep/oversized
    tree does not blow up or hang.

---

## 11. Recommended implementation PR order

| PR | Scope | Touches | Runtime behavior change |
|---|---|---|---|
| **#16A (this PR)** | Design doc only | `docs/architecture/memory-repo-reality-conflicts.md`, pointer in `memory-architecture.md` | **None** |
| **#16B** | Shared deterministic fingerprint extractor, **db only**; refactor `bootstrap.py` to consume it | `backend/repo/repo_fingerprint.py` (new), `backend/memory/bootstrap.py` | None (pure extraction; bootstrap output unchanged) |
| **#16C** | Compare active `db` memory vs repo signal; set `is_stale` on clear conflict; manual verify action; positive path bumps `last_verified_at` | `backend/memory/repo_reality.py` (new), `backend/memory/memory_store.py` (`mark_fact_stale`), `backend/routes/memory.py` | Conflicting db facts excluded from prompts (the intended safety win); no other change |
| **#16D** | Run-scope block/warn policy for `db` conflicts; pre-prompt-injection gate on db-sensitive runs | pipeline run-start path | Scope-matched db conflicts gate the run (human-gated, loud) |
| **#16E** | `memory_conflicts` table (Option B) + conflict resolution API/UI, **if needed** | schema + routes + frontend | Adds resolution surface; no change to detection semantics |

### #16C — as implemented

- **Service:** `backend/memory/repo_reality.py` →
  `verify_project_db_memory_against_repo(project_id, repo_path) -> dict`. Builds the
  fingerprint via `build_repo_fingerprint`, loads active `category='db'` facts, and
  compares each fact's recognizable DB value against the repo signal.
- **Store helper:** `backend/memory/memory_store.py` →
  `mark_fact_stale(project_id, memory_id, reason=None)`. Sets `is_stale=1`,
  `status='stale'`; stores the reason in the existing `archived_reason` column (no
  schema change). Content is never edited; the fact is never archived/deleted.
- **Endpoint:** `POST /api/v1/projects/{project_id}/memory/verify-repo`. Manual only;
  derives `repo_path` from the project. Returns `repo_db_signal`, `ambiguous`,
  `checked_count`, `verified_fact_ids`, `staled_fact_ids`, `skipped_fact_ids`,
  `warnings`, and `evidence` (fixed fingerprint excerpts — never file content).
- **Data model:** Option A (no `memory_conflicts` table). Exclusion is the existing
  `is_stale=0` builder filter. **No run blocking, no per-run gate** — that remains #16D.
- Matching → `verify_fact` (bumps `last_verified_at`). Unknown/ambiguous repo signal,
  no active DB memory, or memory whose content names zero or multiple engines →
  skipped, never staled.

Each PR is independently shippable and testable. Expand to `test_runner` /
`framework_backend` / etc. only after `db` is proven in real use.

> **#16D — run-scope gate (designed separately):** When a clear DB conflict should
> **block**, **warn**, or **do nothing** based on the requested run's scope is designed
> in [`memory-conflict-run-gate.md`](./memory-conflict-run-gate.md). #16C stops stale DB
> memory from being *injected*; #16D decides when a relevant conflict should *pause a
> run*. No gating is implemented in #16C.

---

## 12. What NOT to build yet (strict list)

- pgvector, embeddings, semantic retrieval of any kind.
- PostgreSQL migration, Alembic.
- Full repo scans (AST, every-file, every-run). Capped manifests only.
- Cross-project or org/shared memory.
- Automatic rewriting of memory **content**.
- A big conflict dashboard / analytics UI.
- LangChain / LangGraph / any agent-memory framework.
- `repo_fingerprints` persistence (Option C) — deferred until measured need.
- Auto-archiving or auto-promoting on conflict (human gate only).
- Branch-scoped or folder-scoped conflict detection beyond the existing 5 scopes.

---

## 13. Risks and open questions

- **Stale-injection divergence (must resolve before #16C).** The shipped builder excludes
  `is_stale=1`; the M1 doc says stale is injected with a tag. M1.5 relies on exclusion. If
  the team restores tagged-stale injection, M1.5 needs a dedicated `conflict_excluded`
  flag instead of reusing `is_stale`. Recommend: confirm exclusion is the intended shipped
  behavior and update `memory-architecture.md` §2.4/§7.2 to match.
- **False positives block real work.** A wrong "block" is worse than a wrong "warn."
  Mitigation: block only on high-severity + exclusive signal + scope match; default to
  warn whenever scope is uncertain; always provide "override once for this run."
- **`is_stale` overload.** Today `is_stale` is set by time-based `flag_stale_memories`,
  by archive, and (proposed) by conflict. Without Option B's `memory_conflicts` record,
  a human cannot tell *why* a fact is stale. This is the main argument for promoting
  Option B at #16E.
- **Multi-DB / polyglot repos** legitimately use two databases. The `db` extractor must
  treat "two exclusive drivers present" as **ambiguous → warn**, not conflict.
- **Monorepos** with multiple manifests can yield mixed signals. The depth/file caps
  bound cost; the extractor should attribute signals to the nearest manifest and prefer
  `docker-compose`/`prisma` provider lines (authoritative) over loose `.env.example` hints.
- **Run-scope inference accuracy** depends on `files_expected` being populated and
  `classify_file` being right. When unknown, default to warn.

---

## 14. Confirmation

This PR (#16A) is **design and documentation only**. It adds one architecture document
and a cross-reference pointer. It introduces **no** conflict-detection code, **no** schema,
**no** API, **no** UI, **no** prompt-injection change, and **no** change to the memory
store or any runtime path. The existing `is_stale=0` prompt filter, `verify_fact`,
`archive_fact`, and bootstrap extractors are described, not modified.

Cross-references: [`memory-architecture.md`](./memory-architecture.md) §3.5
(signal-driven staleness, foreshadowed as M2), §5 failure modes #12–#16 (stack/repo/test/
migration changes), and §15 (the undetected-conflict gap this design closes).
