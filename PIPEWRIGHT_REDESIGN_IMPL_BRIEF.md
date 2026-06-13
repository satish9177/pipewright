# Pipewright Redesign — Implementation Handoff Brief (rolling, per-phase)

**Date:** 2026-06-14
**What this file is:** the **rolling** implementation handoff brief for the redesign. Each slice's active spec lives here; when a slice lands, this file is repurposed for the next one. **ACTIVE OCCUPANT as of 2026-06-14: §23 order-row 11 — detection rules-as-data, PR-B only (advisory repo reality signal producer).** The previous occupant — **Row 11 PR-A (the behavior-preserving extraction)** — has **landed**: detection now lives in `backend/memory/detection_rules.py`, `bootstrap._collect_candidates` is a thin adapter, and ordered six-field parity is pinned by `backend/tests/test_memory_detection_rules.py` against committed goldens (`backend/tests/goldens/memory_detection_rules*.json`). The PR-A as-built record lives in `PIPEWRIGHT_REDESIGN_WORKPLAN.md` (Row-11 line) and `docs/status/current-state.md`; this brief no longer describes PR-A. Earlier Area B / Pass-1 occupants: M5 suggestion-quality gate (row 7) shipped via PR #292 (closeout `docs/testing/memory-m5-suggestion-quality-smoke.md`); item 17 design preserved in `PIPEWRIGHT_ITEM17_DESIGN.md`. This brief now describes the **next** Area B slice.
**For:** the engineer (Fable or human) who will implement **Row 11 PR-B only**, after this design brief is reviewed.
**Source of record:** `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (Appendix E.1 the Row-11 sub-PR split — PR-B is "emit repo reality signals advisorily"; §11.4 detection-rules-as-data / T15; §14 the §8b policy-spine table; §11.5–11.6 reality/staleness as-built). The workplan (`PIPEWRIGHT_REDESIGN_WORKPLAN.md`) carries the sequence (= proposal §23) and the decision roster (= §24). If this brief and the proposal disagree, **the proposal wins and you flag the drift.**
**Mode:** **design ratified, implementation pending.** Unlike the PR-A occupant, the maintainer has **already ratified** the design decisions for PR-B (see §11B.0). This brief fixes the *what* and the *how-constraints* and points at the real code so you do not re-discover the repo. You still **read the real code first, re-verify every `file:line`, and flag drift.** The PR is reviewed.

> **Status: DESIGN RATIFIED — not yet implemented.** No code has been written for Row 11 PR-B. The §11B.0 decisions are settled; implement against them. This slice is **Row 11 PR-B only.** Do **not** backfill test-command detection (PR-C), and do **not** touch any deeper Area B row (12/16/19/23) or the thread UI.

---

## 0. The headline invariant (read before anything else)

**The consumer already exists. PR-B is a *producer* plus one wiring line — and it stays advisory-only by construction, because everything downstream of the signal map already shipped as advisory-only.**

The end-to-end reality-check path was built in M3F3 and is fully test-locked **for all six dimensions** — it is starved only of *values* for five of them:

- `check_fact_against_signal` / `extract_dimension_values` / `_canonical_repo_value` (`memory_trust.py:331-440`) already classify a memory fact against a repo value for every dimension in `SUPPORTED_REALITY_DIMENSIONS` (`memory_trust.py:130`, derived from `_DIMENSION_VALUE_TOKENS` `:76-128`).
- `analyze_injection_events(events, repo_signals={dim: value})` already loops **every** dimension in the map and emits advisory `RealityWarning`s **for mismatches only** (`injection_analysis.py:294-319`); `match` / `unknown` / `unsupported` are silent by design.
- The `/api/v1/runs/{run_id}/memory-injections/analysis` endpoint already calls a producer — `_repo_reality_signals(project_id)` — and threads the map through (`routes/memory.py:1059-1060`).
- **The gap:** `_repo_reality_signals` (`routes/memory.py:1002-1028`) only ever populates `db_engine`, because `build_repo_fingerprint` is the "M1.5 db-only" slice. The other five dimensions are wired end-to-end on the *consumer* side and have no *producer*.

So the single non-negotiable framing:

> PR-B adds a **pure, deterministic, conservative producer** that computes **at most one confident canonical value per dimension** for `backend_framework`, `frontend_framework`, `test_runner`, `migration_tool`, `package_manager`, and wires it into `_repo_reality_signals`. `db_engine` stays sourced from the existing DB fingerprint path. The producer **mutates nothing**, **persists nothing**, **scans only capped/traversal-safe manifest files**, and emits **only canonical values the consumer can recognize**. Per-dimension ambiguity or weak signal ⇒ **omit that dimension** (a missing dimension is silent; a wrong dimension is a false warning — we always prefer the silence).

The real risk here is **not** a safety-contract breach (the path is advisory and mutation-free end to end). It is **(a) false-positive warnings** from over-broad markers, and **(b) namespace drift** — emitting a value the consumer can't canonicalize, which silently degrades a real mismatch to `unknown`. The whole §11B.7–§11B.9 + test strategy exist to make both impossible to ship unnoticed. The one *intended* observable change is that genuine, advisory mismatch warnings can now appear for five more dimensions; nothing is mutated, ever.

---

## 11B.0 Ratified maintainer decisions (settled — implement against these)

These were brought to the maintainer as a design review and **ratified**. They are not open. Implement to them; if implementation reveals one is wrong, **stop and re-raise** — do not silently deviate.

- **D1 — Producer home & shape.** Add the new per-dimension detectors **and** the aggregator in `backend/repo/repo_fingerprint.py` (it already owns safe discovery/loading/caps and the db detector). Keep them **separate from `RepoFingerprint`** so `build_repo_fingerprint` and the DB conflict-gate path stay **byte-identical**.
- **D2 — `db_engine` sourcing.** `db_engine` continues to come from the existing `detect_db_signals` / `build_repo_fingerprint` behavior. **Do not change** `evaluate_db_memory_conflicts` or `repo_reality.py`. The producer computes only the **five new** dimensions; the route merges db from the fingerprint as it does today.
- **D3 — Defer T15 physical unification.** Do **not** physically unify the vocabularies of `detection_rules.py`, `repo_fingerprint.py`, and `memory_trust.py` in PR-B. PR-B may use a **separate, conservative marker table**, but every emitted value **must be compatible with `memory_trust._DIMENSION_VALUE_TOKENS`** (§11B.9).
- **D4 — Active-dimension kill switch.** Add a single policy constant naming the active repo-reality signal dimensions. **Default enables** db_engine + the five PR-B dimensions. Rolling back to **db_engine only** must be a one-line policy edit, no code change (§11B.6).
- **D5 — `package_manager` is lockfile/section-only.** Emit `package_manager` only from **high-confidence lockfile/section** evidence (e.g. `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock` / `[tool.poetry]`, `Pipfile`, `Cargo.toml`, `go.mod`) where a canonical value exists. **Never** emit bare `pip` from arbitrary content. Ambiguous or weak ⇒ omit (§11B.7).
- **D6 — Data shape.** A small new **frozen `RealitySignal` dataclass** (`dimension`, `value`, `evidence_path`, `evidence_excerpt`). Aggregator returns `dict[str, RealitySignal]`; the route flattens to `dict[str, str]` for `analyze_injection_events` (§11B.4).
- **D7 — Display.** **No new frontend / UI / thread work.** Surface only through the existing memory-injections analysis `reality_warnings`. If the existing frontend renders those warnings generically, smoke it; otherwise note display polish as **deferred** (§11B.10, §11B.14).

---

## 11B.1 Non-negotiable safety contract (from `CLAUDE.md`)

No change may weaken these. Row 11 PR-B framing in **bold**:

1. No implementation without an approved chunk plan; never bypass a gate. **Untouched — PR-B adds a read-only signal producer feeding an advisory *analysis* endpoint; no chunk plan, gate, or orchestrator path is involved.**
2. Never edit outside approved `files_expected`; `scope_guard` is the authority. **Untouched — no execution/scope path is involved.**
3. Never create empty / no-effective-change commits. **Untouched — no commit path.**
4. Never open PRs against `main`/`master`/`develop`; never auto-merge. **Untouched — no Git path.**
5. Never write forbidden paths. **Untouched — the producer only *reads* capped manifest files + a small set of lockfile existence probes through `repo_fingerprint`'s traversal-safe loader (`load_repo_file` rejects `.env`/traversal); it writes nothing.**
6. Never expose or persist secrets/tokens/PII; sanitize errors. **Preserved — evidence is fixed human-written excerpts, never raw file content; the producer never reads `.env` values; on any error the route returns `{}` (no leak). Nothing is persisted.**
7. Memory is advisory; source code, user instruction, tests, safety rules win. **Reinforced — the producer feeds *advisory mismatch candidates* only; it makes no decision, changes no fact, and never asserts "the repo is right and memory is wrong" beyond a human-read warning.**
8. AI-suggested memory stays pending until a human approves. **Untouched — PR-B creates no suggestion and no fact; it does not touch the bootstrap/approval path at all.**
9. Prefer failing safely with a clear, specific error over guessing. **Preserved — ambiguity/unknown/weak signal ⇒ omit the dimension (no guess); any exception ⇒ `{}`. Conservative-by-omission is the whole producer.**

Additionally — two **PR-B-specific invariants** that are the substance of the risk (testable in §11B.12):

- **DB-gate isolation.** The DB conflict gate's input (`evaluate_db_memory_conflicts` → `build_repo_fingerprint`) is **byte-identical** before and after PR-B. The five new dimensions never reach the gate; the gate stays db-only.
- **Value-namespace compatibility.** Every value the producer can emit is a canonical key in `_DIMENSION_VALUE_TOKENS[dimension]` (so the consumer canonicalizes it and a real mismatch is never silently downgraded to `unknown`).

This slice is **low-risk** (read-only advisory producer, no execution/scope/Git/secret-egress/mutation). The risk is **false positives and namespace drift**, not a contract breach.

## 11B.2 Current-state verification (the ground you're designing on)

Read the real code first; re-verify every `file:line`; **correct this brief's pointers if the live code drifted, and say so.**

**The consumer side (already built — do NOT modify, just feed it):**

- **`memory_trust.py:76-128` `_DIMENSION_VALUE_TOKENS`** — the canonical value vocabulary per dimension. The keyspace your producer must emit into:
  - `db_engine`: postgresql, mysql, mongodb, sqlite *(already produced — D2)*
  - `backend_framework`: fastapi, django, flask, express, nestjs, fastify, spring_boot, rails
  - `frontend_framework`: react, vue, angular, svelte, nextjs
  - `test_runner`: pytest, unittest, jest, vitest, mocha, junit, rspec, go_test
  - `migration_tool`: alembic, prisma, flyway, liquibase, knex, django_migrations
  - `package_manager`: npm, yarn, pnpm, pip, poetry, pipenv, cargo, go_modules
- **`memory_trust.py:378-440` `check_fact_against_signal(dimension, content, repo_value)`** — pure classifier. `_canonical_repo_value` (`:356-373`) accepts either an exact canonical key **or** a recognizable token, returning the canonical key or `None`. **Emitting canonical keys directly is the robust contract** — do not rely on token-canonicalization for the new dims (e.g. `"nest"` is *not* in nestjs's tokens; emit `"nestjs"`).
- **`injection_analysis.py:186-213, 294-319`** — `analyze_injection_events` filters falsy signal values (`:209-213`) and, per distinct injected fact, calls `check_fact_against_signal` for **each** dimension in the map, surfacing only `REALITY_MISMATCH` (`:300`). It needs **only `{dimension: canonical_value}`** — nothing richer.
- **`routes/memory.py:1002-1028` `_repo_reality_signals(project_id)`** — the **producer gap**. Today: builds `build_repo_fingerprint(repo_path)` and sets `signals["db_engine"]` only when `fingerprint.db is not None and not fingerprint.db_ambiguous`. Best-effort: missing project/repo/any exception ⇒ `{}`. **This is the one function you wire into (§11B.10).**
- **`routes/memory.py:1031-1061`** — the analysis endpoint. Calls `_repo_reality_signals` (`:1059`) → `analyze_injection_events(..., repo_signals=...)` (`:1060`). No mutation, no LLM/git/GitHub. This is the only surface PR-B lights up.

**The producer side (extend here — D1/D2):**

- **`repo_fingerprint.py`** — "deterministic, zero-AI extraction of repo reality signals from capped manifest/config files." Already owns: caps (`MAX_DEPTH:40`, `MAX_MANIFEST_FILES:41`, `MAX_FILE_SIZE_BYTES:42`), `IGNORED_DIRS:44`, `MANIFEST_FILENAMES:70-96`, `load_manifest_files:277-292`, `load_repo_file:203-225` (traversal/`.env`-safe), `RepoSignal:155-162`, `detect_db_signals:339-379`, `build_repo_fingerprint:382-407`. **`detect_db_signals` returns ≤1 signal per engine, attributed to the first evidencing file, in `DB_ENGINE_ORDER`**, and `build_repo_fingerprint` decides ambiguity (`len(signals) > 1` ⇒ `db=None, db_ambiguous=True`). Mirror that collapse per new dimension.
- **Architectural direction:** `repo_fingerprint.py` currently imports **nothing from `backend.memory`**. **Keep it that way** — the repo layer must not depend on the memory layer. The value-namespace alignment to `_DIMENSION_VALUE_TOKENS` is enforced by a **test** (§11B.9), not by a runtime import.
- **`MANIFEST_FILENAMES` does NOT include lockfiles** (`yarn.lock`, `pnpm-lock.yaml`, `package-lock.json`, `poetry.lock`, `Cargo.lock`). **Do not add them** — that would change `discover_manifest_files` output, the `MAX_MANIFEST_FILES` cap interaction, and ordering, with knock-on parity risk for bootstrap. Detect lockfiles via **targeted existence probes on the repo root** (like bootstrap's K5 `is_dir()`/`is_file()` probes), independent of the manifest dict (§11B.7). `Pipfile`, `Cargo.toml`, `go.mod`, `pyproject.toml`, `package.json`, `pom.xml`, `build.gradle*` **are** already in the manifest set and loaded.

**The do-NOT-touch DB path (D2):**

- **`backend/memory/repo_reality.py`** — `evaluate_db_memory_conflicts:108-194` (read-only) and `verify_project_db_memory_against_repo:197-271` (the *only* db mutation, behind `/memory/verify-repo`). **Untouched.**
- **`backend/pipeline/chunked_orchestrator.py:55`** imports `evaluate_db_memory_conflicts`; the gate decision is `_db_conflict_block_decision:242-262`. **Untouched** — PR-B feeds nothing into this path.

**The parity guard (do-NOT-touch candidate emission):**

- **`backend/memory/detection_rules.py:672-695` `collect_detection_candidates`** and all its rule tuples — **untouched.** PR-B adds a *sibling* producer in a different module (`repo_fingerprint.py`); it never edits the candidate path. **`backend/tests/test_memory_detection_rules.py` must stay green with NO golden edits** — that is PR-B's parity guard (§11B.13). The `CandidateTemplate` dataclass carries **no** `dimension`/`signal_value` field (PR-A left no inert seam); PR-B does not add one to it.

## 11B.3 Exact PR-B scope

**PR-B = a conservative, advisory repo-reality-signal producer for five new dimensions, wired into the existing analysis read-path. Nothing else.**

In scope:
1. A small **`RealitySignal`** frozen dataclass in `repo_fingerprint.py` (D6, §11B.4).
2. **Per-dimension detectors** in `repo_fingerprint.py` for `backend_framework`, `frontend_framework`, `test_runner`, `migration_tool`, `package_manager` — conservative markers, canonical-key values (§11B.7).
3. An **aggregator** `collect_repo_reality_signals(repo_path, *, dimensions=...) -> dict[str, RealitySignal]` that loads manifest files once + runs the lockfile probes, applies per-dimension ambiguity collapse, and returns ≤1 confident signal per active non-db dimension (§11B.5).
4. A **policy kill-switch** constant for active dimensions (D4, §11B.6).
5. **Wiring** `_repo_reality_signals` to merge db (from the existing fingerprint) + the aggregator's five dims, filtered by the policy set, flattened to `{dim: value}` (§11B.10).
6. **Targeted tests** (§11B.13): producer units, the value-namespace compatibility test, endpoint wiring tests, plus the parity + db-gate guards run unchanged.
7. A short docs note (extend `docs/architecture/memory-repo-reality-conflicts.md` or proposal §11.5 as-built) that reality signals now cover six dimensions advisorily; PR-C remains the last Row-11 consumer.

Outcomes the design must deliver:
- A **pure, deterministic, total** producer (no DB/network/LLM/clock), capped via the existing `repo_fingerprint` loader.
- The existing analysis endpoint emits advisory mismatch warnings for the five new dimensions **when, and only when**, a single injected memory fact asserts one recognizable value that differs from an unambiguous repo value.
- **`db_engine` behavior and the DB conflict gate are byte-identical** to today.
- **`test_memory_detection_rules.py` green, no golden edits**; the DB-gate tests green, unchanged.
- `ruff check` clean on changed files. No frontend change.

## 11B.4 `RealitySignal` data shape (D6)

A small frozen dataclass in `repo_fingerprint.py`, beside `RepoSignal`:

```python
@dataclass(frozen=True)
class RealitySignal:
    dimension: str          # a key in memory_trust.SUPPORTED_REALITY_DIMENSIONS, e.g. "test_runner"
    value: str              # a canonical key in _DIMENSION_VALUE_TOKENS[dimension], e.g. "pytest"
    evidence_path: str | None
    evidence_excerpt: str   # fixed, human-written string — NEVER raw file content
```

- The aggregator returns `dict[str, RealitySignal]` (one confident signal per active non-db dimension; ambiguous/unknown dimensions absent).
- The route flattens to `dict[str, str]` (`{signal.dimension: signal.value}`) before calling `analyze_injection_events`, because the consumer needs only `{dimension: canonical_value}` (D6).
- `db_engine` is **not** re-modeled as `RealitySignal` — it stays a `RepoSignal` from `build_repo_fingerprint` (D2). The route reads `.value` from either type when flattening; the slight asymmetry is the deliberate price of keeping the db path byte-identical.
- `evidence_path` / `evidence_excerpt` are carried for **audit and future display**, not consumed by PR-B's warning path (the consumer ignores them). Compute them anyway — they are cheap and the warning text the consumer builds already references value-level detail; richer per-signal evidence is a clean PR-C/display seam.

## 11B.5 Aggregator behavior

`collect_repo_reality_signals(repo_path, *, dimensions: frozenset[str] = ...) -> dict[str, RealitySignal]`

1. **Resolve + guard.** Resolve `repo_path`; missing/not-a-dir ⇒ return `{}` (mirror `build_repo_fingerprint`'s empty path handling). Wrap the body so any unexpected error degrades to `{}` (the route is best-effort; analysis never fails on this).
2. **Load once.** Call `load_manifest_files(root)` **once** to get `{relative_posix: raw_text}` (capped, traversal-safe). Lowercase per-file as needed for marker checks (the existing detectors lowercase content internally; match that).
3. **Lockfile probes.** For the package-manager dimension only, do a small fixed set of **root-level existence probes** (`(root / "yarn.lock").is_file()`, etc.) — see §11B.7. These do **not** go through manifest discovery (the lockfiles are not manifests, and must not be added to `MANIFEST_FILENAMES`).
4. **Per-dimension detect + collapse.** For each requested non-db dimension, gather the set of distinct canonical values evidenced. Apply the **db-style collapse**:
   - exactly **one** distinct value ⇒ emit a `RealitySignal` (value + first-evidencing path + fixed excerpt);
   - **zero** or **more than one** distinct value ⇒ **omit** the dimension (no signal).
5. **Determinism.** Iterate files/markers in a stable order so the first-evidencing path is deterministic (mirror `detect_db_signals`, which iterates `DB_ENGINE_ORDER` and the files dict). Identical repo ⇒ identical output.
6. **Return** `dict[str, RealitySignal]` keyed by dimension, containing only the confident, active dimensions.
7. **`db_engine` is not computed here** (D2) — the route adds it from the fingerprint. The aggregator only knows the five new dimensions, so a misconfiguration can never make it shadow or diverge from the gate's db signal.

The aggregator is **pure** (no policy lookup inside): it computes whatever `dimensions` it is asked for. The policy set is applied at the call site (§11B.6) so the producer stays trivially unit-testable with arbitrary subsets.

## 11B.6 Active-dimension policy kill switch (D4)

Add a single constant to `backend/pipeline/policy.py` (the established §8b policy spine):

```python
# Repo-reality advisory signal dimensions that are live on the analysis read-path.
# Roll back to db-only by reducing this to frozenset({"db_engine"}) — no code change.
REPO_REALITY_SIGNAL_DIMENSIONS: frozenset[str] = frozenset({
    "db_engine",
    "backend_framework",
    "frontend_framework",
    "test_runner",
    "migration_tool",
    "package_manager",
})
```

- **One enforcement point.** `_repo_reality_signals` reads this set once and: (a) includes `db_engine` (from the fingerprint) only if `"db_engine"` is in it; (b) passes the **non-db subset** to the aggregator as its `dimensions` argument; (c) the aggregator computes only those. No second copy of the set.
- **Default = all six** (db + the five PR-B dims), i.e. PR-B's intended advisory behavior is on by default.
- **Rollback = one line:** set it to `frozenset({"db_engine"})` to restore exactly today's behavior (db-only warnings), with no revert and no redeploy of code logic (§11B.15).
- This also satisfies the "no buried magic numbers" principle: the live signal set is explicit, single-sourced policy, and auditable.

## 11B.7 Conservative marker strategy per dimension

**Guiding rule:** dependency-name / file-presence / config-section evidence only. **Never** match a bare single-word token (`npm`, `pip`, `nest`) against arbitrary file content — those are false-positive magnets. Emit **canonical keys** (§11B.9). Where a canonical value has no high-confidence file signal, **omit it** (omission is safe).

Recommended starting markers (the implementer owns the exact spelling; reuse existing helpers/markers verbatim where they exist, e.g. the package.json dep checks mirror `detection_rules._package_uses`):

- **`backend_framework`** (python manifests + `package.json` deps + JVM build files):
  - `fastapi` ← `fastapi` in a python manifest; `django` ← `django`; `flask` ← `flask`; `express` ← package.json dep `express`; `nestjs` ← dep `@nestjs/core`; `fastify` ← dep `fastify`; `spring_boot` ← `spring-boot` in `pom.xml`/gradle.
  - **`rails` ⇒ omit** in PR-B (needs `Gemfile`, not in the manifest set; do not add it).
- **`frontend_framework`** (`package.json` deps):
  - `react` ← dep `react`; `vue` ← dep `vue`; `angular` ← dep `@angular/core`; `svelte` ← dep `svelte`; `nextjs` ← dep `next`.
  - **Known ambiguity:** Next.js repos usually also depend on `react` ⇒ two distinct values ⇒ the collapse **omits** `frontend_framework`. Accepted as conservative for PR-B (a missing signal, not a wrong one). A "next implies react" refinement is explicitly out of scope (note it in §11B.16 as a future option).
- **`test_runner`** (python manifests + `pytest.ini` + `package.json` deps + JVM):
  - `pytest` ← `pytest` in a python manifest **or** `pytest.ini` present; `jest` ← dep `jest`; `vitest` ← dep `vitest`; `mocha` ← dep `mocha`; `junit` ← `junit` in `pom.xml`/gradle.
  - **`unittest` ⇒ omit** (stdlib, not declared); **`rspec` ⇒ omit** (needs Gemfile); **`go_test` ⇒ omit** in PR-B (it is implied by any `go.mod`, too weak to be a meaningful "this runner not that one" signal).
- **`migration_tool`**:
  - `alembic` ← `alembic.ini` present (already a manifest) — optionally also the `(root / "alembic").is_dir()` probe to match bootstrap's K5; `prisma` ← `prisma/schema.prisma` present **or** package.json dep `prisma`/`@prisma/client`; `knex` ← dep `knex`.
  - **`flyway` / `liquibase` / `django_migrations` ⇒ omit** (no high-confidence manifest marker; do not infer).
- **`package_manager`** (D5 — lockfile/section-only, root existence probes + one section check):
  - `yarn` ← `yarn.lock`; `pnpm` ← `pnpm-lock.yaml`; `npm` ← `package-lock.json`; `poetry` ← `poetry.lock` **or** `[tool.poetry]` section in `pyproject.toml` (loaded); `pipenv` ← `Pipfile` (loaded manifest); `cargo` ← `Cargo.toml` (loaded manifest); `go_modules` ← `go.mod` (loaded manifest).
  - **`pip` ⇒ never emitted** from arbitrary content (D5). The presence of `requirements.txt` alone is too weak to distinguish pip from poetry/pipenv/etc.; treat pip as not-detected in PR-B.
  - Polyglot repos (e.g. `go.mod` + `yarn.lock`) yield ≥2 distinct managers ⇒ collapse **omits** the dimension. Correct and conservative.

Evidence excerpts are **fixed human strings** per (dimension, value), in the style of `repo_fingerprint._DB_EVIDENCE` (e.g. `"Detected pytest test runner."`), never raw file content.

## 11B.8 Ambiguity / unknown behavior

- **Per-dimension ambiguity collapse** (the core safety move): >1 distinct canonical value evidenced for a dimension ⇒ **omit** that dimension. Mirrors `build_repo_fingerprint`'s db ambiguity (`db=None, db_ambiguous=True`) but applied independently per dimension (one ambiguous dimension never suppresses a confident one).
- **No signal** for a dimension ⇒ absent from the map ⇒ the consumer never warns for it.
- **Weak/insufficient signal** (e.g. pip, go_test, rails) ⇒ intentionally not emitted ⇒ absent.
- **Downstream tolerance is already correct:** `analyze_injection_events` drops falsy values (`:209-213`); `check_fact_against_signal` returns `UNKNOWN` when the memory fact has no single recognizable value or the repo value is unrecognizable, and `UNSUPPORTED` for a non-vocabulary dimension — **none of which produce a warning**. So even a stray/odd entry can only ever *fail to warn*, never falsely warn, as long as values are canonical (§11B.9).
- **Missing project / repo_path / any exception** ⇒ `{}` (route best-effort, unchanged contract).

## 11B.9 Value validation against `memory_trust._DIMENSION_VALUE_TOKENS` (D3)

This is the anti-drift guarantee. Because the producer (repo layer) must not import the memory layer (§11B.2), alignment is enforced by a **test**, not a runtime import:

- **Compatibility test (mandatory).** Enumerate every `(dimension, value)` the producer's marker table can emit (expose the table or a `producible_signal_values()` helper for the test to read). Assert, for each:
  1. `dimension in memory_trust.SUPPORTED_REALITY_DIMENSIONS`;
  2. `value in memory_trust._DIMENSION_VALUE_TOKENS[dimension]` (canonical key, not a token);
  3. **round-trip:** `check_fact_against_signal(dimension, <a fact text asserting value>, value).status == REALITY_MATCH` — proving the consumer canonicalizes the emitted value and a real match is recognized (so a real mismatch can't silently downgrade to `unknown`).
- This test fails loudly if anyone later adds a producer value the consumer can't recognize, or renames a vocabulary key — exactly the drift D3 is worried about while the vocabularies stay physically separate.
- The producer itself stays free of any `backend.memory` import; the **direction `memory → repo` is preserved** (the consumer already depends on the repo signal shape; the repo never depends on memory).

## 11B.10 `_repo_reality_signals` wiring

Modify only `_repo_reality_signals` (`routes/memory.py:1002-1028`); the endpoint body (`:1031-1061`) and `analyze_injection_events` are unchanged.

Shape (illustrative, not prescriptive):

```python
def _repo_reality_signals(project_id: str) -> dict[str, str]:
    try:
        project = get_project(project_id)
        if not project:
            return {}
        repo_path = (project.get("repo_path") or "").strip()
        if not repo_path:
            return {}
        active = policy.REPO_REALITY_SIGNAL_DIMENSIONS
        signals: dict[str, str] = {}
        # db_engine: unchanged source (D2) — the same fingerprint the gate uses.
        if "db_engine" in active:
            fingerprint = build_repo_fingerprint(repo_path)
            if fingerprint.db is not None and not fingerprint.db_ambiguous:
                signals["db_engine"] = fingerprint.db.value
        # five new dimensions: conservative producer, policy-filtered.
        for dim, sig in collect_repo_reality_signals(
            repo_path, dimensions=active - {"db_engine"}
        ).items():
            signals[dim] = sig.value
        return signals
    except Exception:
        return {}
```

- **db stays first and unchanged** — same `build_repo_fingerprint` call, same unambiguous guard. The gate is never touched.
- The aggregator receives only the **non-db active dimensions**; with the default policy that's the five PR-B dims, with the rollback policy it's the empty set (aggregator returns `{}`).
- Output is `dict[str, str]` exactly as the consumer expects; `reality_signal_available` (in the consumer) becomes true whenever any dimension is present.
- **Frontend (D7):** the new warnings flow into the existing `reality_warnings` array (`routes/memory.py:980-997` shapes them generically over `analysis.reality_warnings`). Before claiming "lights up for free," **grep the frontend** for the warnings renderer (e.g. the M3F3 panel consuming `reality_warnings` / `reality_warning_count`) and confirm it renders the list dimension-agnostically. If it hardcodes db wording, that is **deferred display polish**, not PR-B — note it; do not add/alter frontend in this slice.

## 11B.11 Non-goals (name these in your PR)

- **No memory mutation** — no `mark_fact_stale`, no archive, no delete, no `verify_fact`, no `last_verified_at` bump (D8/B3 is a *later* decision, not this slice).
- **No memory auto-approval, no suggestion/fact creation** — PR-B does not touch the bootstrap/approval path.
- **No injection-eligibility or prompt-builder change** — the signal is computed on the *analysis read path* only, never in `prompt_builder`, never during injection (exactly like today's db M3F3 signal).
- **No DB conflict-gate change** — `evaluate_db_memory_conflicts` / `repo_reality.py` / the orchestrator gate are untouched (D2).
- **No change to `detection_rules.py` candidate emission or the PR-A goldens** — `test_memory_detection_rules.py` stays green, no golden edits.
- **No schema change, no persistence** — the producer is compute-on-read; nothing is stored.
- **No new endpoint.** **No frontend / UI / thread work** (D7).
- **No T15 physical vocab unification** (D3) — separate marker table, value-compatible only.
- **No PR-C test-command backfill** (§11B.17). **No request-aware selection (Row 12), post-run hygiene (Row 16), retriever/FTS (Row 19), vector/embedding (Row 23), thread UI (22b–22e).**

## 11B.12 Safety invariants (restated as testable claims)

1. **No mutation.** After hitting the analysis endpoint on a repo with new-dimension mismatches, every injected fact is unchanged (`status='active'`, `is_stale` falsy). *(Extend the existing `test_endpoint_surfaces_reality_mismatch_without_mutating_memory` to a new dimension.)*
2. **DB-gate isolation.** `build_repo_fingerprint` output and `evaluate_db_memory_conflicts` behavior are byte-identical pre/post PR-B; the five new dims never enter the gate. *(DB-gate tests run unchanged and green.)*
3. **Value-namespace compatibility.** Every producible value canonicalizes and round-trips to `REALITY_MATCH`. *(The §11B.9 compatibility test.)*
4. **Conservative-by-omission.** Ambiguous/unknown/weak ⇒ dimension absent ⇒ no warning; the producer can never *falsely* warn for a dimension it is unsure about. *(Per-dimension ambiguity unit tests + an endpoint ambiguous-repo test.)*
5. **Read-surface safety.** Only capped/traversal-safe manifest reads + a fixed set of root lockfile existence probes; no `.env` values read; evidence excerpts are fixed strings, never raw content. *(Producer unit tests + a no-dotenv-read assertion.)*
6. **Best-effort isolation.** Any producer failure ⇒ `_repo_reality_signals` returns `{}`; the analysis endpoint still succeeds. *(A fault-injection unit test on the producer path.)*
7. **Parity guard.** `collect_detection_candidates` and its goldens are untouched. *(`test_memory_detection_rules.py` green, no golden edits.)*
8. **Kill-switch.** With `REPO_REALITY_SIGNAL_DIMENSIONS = frozenset({"db_engine"})`, `_repo_reality_signals` output equals today's db-only behavior. *(A policy-override test.)*

## 11B.13 Targeted test plan (targeted only — no full suite unless high-risk found)

- **Producer units** (`test_repo_fingerprint.py` style, temp dirs — or a new `test_repo_reality_signals.py`): each new dimension detects its canonical value from a representative manifest/lockfile; per-dimension ambiguity ⇒ omit; no signal ⇒ omit; weak signals (pip/go_test/rails) ⇒ omit; evidence path captured + excerpt is a fixed string; missing repo ⇒ `{}`; exception ⇒ `{}`; `MANIFEST_FILENAMES` unchanged (lockfiles detected via root probes, not discovery).
- **Value-namespace compatibility test** (§11B.9) — the anti-drift guard. Pure.
- **Endpoint wiring / reality-warning tests** (extend `test_memory_reality_warnings.py`): repo with pytest + memory "Tests use Jest" ⇒ `test_runner` mismatch warning, `advisory_only=true`, memory unmutated; repo with React + memory "Frontend uses Vue" ⇒ `frontend_framework` mismatch; ambiguous repo (jest+pytest, or react+next) ⇒ no warning for that dimension; matching repo ⇒ no warning; kill-switch reduced to db-only ⇒ new dims produce no warnings.
- **Parity guard:** `python -m pytest backend/tests/test_memory_detection_rules.py -q` green, **no golden edits**.
- **DB-gate guards (db path touched only indirectly):** `python -m pytest backend/tests/test_db_memory_conflict_gate.py backend/tests/test_memory_repo_reality.py backend/tests/test_repo_fingerprint.py -q` green, unchanged.
- **Policy:** if the constant lands in `policy.py`, add a `test_policy.py` assertion (default contents + the documented rollback value).
- **Lint/cleanliness:** `ruff check` on changed Python files; `git diff --check`; `git status --short`.
- **Full backend suite is NOT required** — PR-B is additive, advisory, mutation-free, and does not touch gate/scope/Git/execution; the parity guard + db-gate guards + targeted units cover the blast radius. **Escalate to `python -m pytest backend/tests -q -m unit` only if implementation uncovers a high-risk change** (e.g. you find you must touch `build_repo_fingerprint`, the gate, or shared discovery) — and explain why in the PR.

## 11B.14 Manual smoke plan

1. Create a throwaway project pointed at a repo with a clear, unambiguous stack (e.g. FastAPI + pytest, single `package.json` with one frontend framework).
2. Add an **approved** memory fact that contradicts it (e.g. "Backend uses Django.", "Tests use Jest.") via the memory API.
3. Trigger a run (or insert a `pipeline_runs` row + record a memory injection event, as the existing `test_memory_reality_warnings.py` integration fixtures do) so provenance exists.
4. `GET /api/v1/runs/{run_id}/memory-injections/analysis` and confirm: new `reality_warnings` for `backend_framework` / `test_runner` with `advisory_only: true`, sane `memory_value`/`repo_value`; `reality_signal_available: true`.
5. Confirm **no mutation** — the contradicting fact is still `active`, not stale; `/memory/verify-repo` still affects db only.
6. Point at an **ambiguous** repo (two test runners, or react+next) and confirm **no** warning for that dimension.
7. Flip `REPO_REALITY_SIGNAL_DIMENSIONS` to `frozenset({"db_engine"})`, repeat step 4, confirm the new-dimension warnings disappear (db-only behavior restored).
8. **Display check (D7):** load the run in the frontend; if the existing reality-warnings panel renders the new dimensions, record it as validated; if it is db-specific, record "frontend display polish deferred" (not a PR-B blocker).
9. Record the results (warnings seen, no-mutation spot-check, kill-switch behavior) in the PR description. *(No external service contacted; local read-only smoke.)*

## 11B.15 Rollback plan

- **Kill-switch first (no revert):** set `policy.REPO_REALITY_SIGNAL_DIMENSIONS = frozenset({"db_engine"})`. The producer is no longer consulted (empty `dimensions`), and the analysis endpoint returns exactly today's db-only warnings. One-line, instant, auditable (D4).
- **Full revert:** `git revert` of the single PR. PR-B is **schema-free and state-free** — no column, migration, or persisted artifact — so revert is clean and total; existing facts, suggestions, and runs are unaffected by applying or reverting.
- Because the new behavior is gated by the policy constant and proven by committed targeted tests, a regression is caught by the suite, not in production; if one slips through, the kill-switch contains it without a deploy of code logic.

## 11B.16 Risks / edge cases

- **(a) False-positive warnings from over-broad markers** — the top risk. *Mitigation:* dependency-name / file-presence / section evidence only; never bare single-word tokens against content; emit canonical keys; per-dimension ambiguity ⇒ omit; the §11B.9 compatibility test + endpoint tests pin it.
- **(b) Namespace drift** (producer emits a value the consumer can't canonicalize ⇒ a real mismatch silently degrades to `unknown`, no warning). *Mitigation:* the §11B.9 round-trip compatibility test; emit canonical keys, never tokens.
- **(c) react + next ambiguity** suppressing `frontend_framework`. *Accepted* as conservative (missing, not wrong). A "next ⇒ react" precedence refinement is a **future option**, explicitly out of PR-B scope; note it.
- **(d) Lockfiles aren't manifests.** Detecting package managers must not add lockfile names to `MANIFEST_FILENAMES` (would change discovery/cap/order and risk bootstrap parity). *Mitigation:* root existence probes, independent of discovery; assert `MANIFEST_FILENAMES` unchanged.
- **(e) Accidentally perturbing the db path / gate.** *Mitigation:* D2 — db stays from `build_repo_fingerprint`; the aggregator never computes db; DB-gate tests run unchanged.
- **(f) Double discovery cost.** The route calls `build_repo_fingerprint` (loads files) and the aggregator (loads again). *Acceptable* on a read-only advisory endpoint; if it matters, expose a lower-level `detect_*_signals(files)` taking pre-loaded files and load once — but keep db single-sourced. Do not optimize at parity's expense.
- **(g) New warnings appearing on existing runs' analysis.** This is the **intended** advisory behavior, not a regression — call it out so reviewers expect it; it is still mutation-free.
- **(h) Repo-global (not per-scope) signals**, same as db today (monorepo with backend+frontend). *Accepted* for advisory; ambiguity collapse already omits genuinely-mixed dimensions.
- **(i) Over-scoping into PR-C / display.** Adding test-command detection or a new UI "while we're here" breaks the smallest-reviewable rule and D7. *Mitigation:* name PR-C and display as out of scope in the PR.

## 11B.17 Explicit statement: PR-C remains later

**Row 11 ships in three ordered sub-PRs. PR-A (extraction) has landed; this brief is PR-B (advisory reality signals); PR-C is NOT in this window.**

- **PR-C (later) — backfill test-command detection** (§23 order-row 1's detector) onto the shared ruleset so `test_command_quality`'s duplicated runner knowledge reads from one source. `test_command_quality` stays the *classifier*; only the *detector* table becomes shared. **PR-B does not touch `test_command_quality`, `test_command_detection`, or any test-command path.**

After Row 11, the **proposal §E.1 hard stop** holds: run a real self-use smoke before opening the backfill set (6b/6c/7a-`plan_versions`/7b/9b) or the deeper Area B rows (12 request-aware selection / D5, 16 post-run hygiene / D7, 19 retriever+FTS, 23 vector / D6) or the §21 thread UI (22b–22e / D13). Those are gated on open §24 decisions and are **not** opened by this brief.

---

## 11B.18 Update these docs when you finish (part of "done")

1. **`PIPEWRIGHT_REDESIGN_WORKPLAN.md`** — mark Row 11 PR-B done in the TL;DR / sequence; note PR-C remains. *(Commit only if the maintainer asks.)*
2. **`docs/status/current-state.md`** — flip the "PR-B remains later" line to done; PR-C remains the last Row-11 consumer. *(Commit only if asked.)*
3. **A short as-built note** — extend `docs/architecture/memory-repo-reality-conflicts.md` (or proposal §11.5) that advisory reality signals now cover six dimensions on the analysis read-path, gated by `policy.REPO_REALITY_SIGNAL_DIMENSIONS`, mutation-free, db unchanged.
4. **This file** — once PR-B lands and is reviewed, repurpose this rolling brief for **Row 11 PR-C** (test-command backfill), or mark it dormant until the next slice.
5. These planning docs are **tracked**. Update content; **do not commit** doc or code changes unless the maintainer asks. If asked to commit: branch off `develop` first (never straight to `develop`/`main`), one purpose per commit, end with the repo's `Co-Authored-By` trailer.

## 11B.19 Working discipline (this slice)

- **Design is ratified (§11B.0); implement against it.** If implementation shows a ratified decision is wrong, **stop and re-raise** with the maintainer — do not silently deviate.
- **Read the real code first; re-verify every `file:line`; correct this brief's pointers** if the live code drifted and say so.
- **Smallest correct change; Row 11 PR-B only.** List what you deliberately did **not** change (the db path/gate, `detection_rules.py` + goldens, the approval/injection/prompt/scope/Git paths, the schema, the deeper memory rows, the thread UI, PR-C).
- **Conservative-by-omission is the product here** — when unsure whether a marker is high-confidence, omit the value. A missing dimension is silent; a wrong dimension is a false warning.
- Tests assert the **decided behavior** (per-dimension confident detection, ambiguity ⇒ omit, value-namespace compatibility, no mutation, db-gate isolation, kill-switch) **and the §0 framing** — not just that code runs.
- **Validation gate before "done":** targeted `repo_fingerprint`/reality-signal tests; targeted memory reality-warning tests; the DB conflict-gate tests (db path touched indirectly); `test_memory_detection_rules.py` as the parity guard; `ruff check` on changed files; `git diff --check`; `git status --short`. Do **not** require the full backend suite unless a high-risk change is discovered (explain it if so).
- Report on completion: changed files, tests run + results, manual validation (the §11B.14 smoke), risks, and what was intentionally left untouched.
- A human reviews this PR.
