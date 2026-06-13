# Pipewright Redesign — Implementation Handoff Brief (rolling, per-phase)

**Date:** 2026-06-14
**What this file is:** the **rolling** implementation handoff brief for the redesign. Each slice's active spec lives here; when a slice lands, this file is repurposed for the next one. **ACTIVE OCCUPANT as of 2026-06-14: §23 order-row 11 — detection rules-as-data, PR-A only (the behavior-preserving extraction).** The previous occupant (Phase-4 item 17 — trivial-task stage profile + provider prompt caching) landed via PR #288 / PR #290 and completed Area A (Pipeline) Pass 1; its design is preserved in `PIPEWRIGHT_ITEM17_DESIGN.md`. The M5 suggestion-quality gate (§23 order-row 7, the first Area B slice) landed via PR #292; its closeout lives in `docs/testing/memory-m5-suggestion-quality-smoke.md`. This brief now describes the **next** Area B slice.
**For:** the engineer (Fable or human) who will design, then — after human design review — implement **Row 11 PR-A only**.
**Source of record:** `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (§11.4 detection-rules-as-data / T15; §14 the §8b policy-spine table that names this refactor; Appendix E.1 the Row-11 sub-PR split and the hard stop after Row 11; §11.6 "what stays as-is"). The workplan (`PIPEWRIGHT_REDESIGN_WORKPLAN.md`) carries the sequence (= proposal §23) and the decision roster (= §24). If this brief and the proposal disagree, **the proposal wins and you flag the drift.**
**Mode:** **design-first.** This document is a **design brief, not a prescribed mechanism.** It fixes the *what* (scope, the headline invariant, the safety check, the acceptance/parity tests) and points at the real code so you do not re-discover the repo — but **you own the *how*** (the exact shape of the ruleset, how heterogeneous detection kinds are represented, where the new module sits, how the evaluator is wired back into `_collect_candidates`). Because you also write the tests, **your design is reviewed by a human before you implement, and the PR is reviewed.** This slice is **Row 11 PR-A only.** Do not emit reality signals (PR-B), do not backfill test-command detection (PR-C), and do not touch any deeper Area B row (12/16/19/23) or the thread UI.

> **Status: DESIGN — not yet implemented.** No code has been written for Row 11. This brief is the input to the design review; ratify §11A.0 (the design decisions) with the maintainer before writing code.

---

## 0. The headline invariant (read before anything else)

Row 11 PR-A is a **pure structural refactor with ZERO behavior change.** Its entire justification is "one source of truth for detection knowledge, so PR-B (reality signals) and PR-C (test-command detection) can reuse the same ruleset instead of re-deriving it" (proposal §11.4 / §14). PR-A itself buys *nothing observable* — it is the enabling extraction. So the bar is not "the rules are now data"; it is "the rules are now data **and** an auditor cannot find a single emitted bootstrap candidate, in any order, that changed." The single non-negotiable invariant:

> For **every** repository, `generate_bootstrap_suggestions` and the underlying `_collect_candidates` produce the **identical ordered list of candidates** — same `content`, `category`, `scope`, `priority`, `evidence_path`, `evidence_excerpt`, **in the same order** — before and after the refactor. The refactor moves *where the detection knowledge lives* (an if/elif chain → a declarative ruleset + a pure evaluator); it changes **no detection, no template, no priority, no scope derivation, and no ordering.**

Two structural guarantees your design must prove:

- **Order is behaviorally significant — pin it, don't just pin the set.** `generate_bootstrap_suggestions` (`bootstrap.py:824-845`) dedupes candidates by `content_hash` in **first-seen order** (`seen_hashes`). Several candidates carry **identical content from different evidence**: e.g. "Project uses MongoDB." is emitted both from the `DB_MONGODB_MARKERS` manifest scan and from a `package.json` `mongoose` dependency; "Run backend unit tests with pytest." is emitted from both a `requirements.txt`/`pyproject` pytest marker and from `pytest.ini`; "Current local database is SQLite." has a single first-evidence selection. For each such collision the **first** candidate in list order wins the surviving `evidence_path`/`evidence_excerpt`/`priority`. A refactor that reorders candidates would silently change which evidence the user sees on an approved fact — a real, if subtle, behavior change. **Parity asserts the full ordered candidate list, not a set.**

- **Heterogeneous rule kinds must survive intact.** `_collect_candidates` is **not** one uniform table; it is six distinct detection *kinds* (see §11A.2). A faithful PR-A preserves each kind's exact semantics. It is explicitly **allowed** to leave the irreducibly-procedural kinds as small evaluator primitives rather than forcing everything into one flat table — over-flattening that changes semantics to "look more declarative" is the failure mode, not the goal. Data-fy the large uniform block; represent the rest honestly; prove parity regardless of how much became data.

This is a refactor sitting on a **suggestion-only, advisory** path: every bootstrap candidate is a *pending* suggestion that a human must approve before it becomes memory (`bootstrap.py` module docstring; `generate_bootstrap_suggestions` only ever inserts `status='pending'`). PR-A does not touch approval, injection, scope, gates, or Git. The real risk is **not** a safety-contract regression — it is a **silent parity break**: a dropped rule, a reordered candidate, a priority typo, a marker-set that no longer matches the same substrings. The whole test strategy (§11A.6) exists to make that impossible to ship unnoticed.

---

## 11A.0 Design decisions for the maintainer (ratify BEFORE any code)

These are the substance of the design review. Bring a recommendation for each; the maintainer rules. **Code starts only after these are settled.**

- **A0 — Ruleset representation.** How is a rule expressed as data? Options: (a) a list of frozen dataclass rule objects (`ManifestContentRule`, `PackageJsonRule`, `FirstEvidenceRule`, …) evaluated in declaration order; (b) plain dict/tuple records with a kind tag dispatched by the evaluator; (c) a hybrid — a uniform table for the large per-file/content kind (K1, §11A.2), with the procedural kinds (K3 package.json, the K5 filesystem probes, the K2 python-3.11 corpus regex) kept as typed evaluator steps interleaved at their current positions. **Recommendation: (c) typed dataclass rules in one ordered module, evaluator dispatches on rule kind.** It maximizes "data" for the part that *is* uniform without distorting the parts that are not, and the declaration order in the module *is* the candidate order (one source of truth for ordering). Whatever you choose, the ruleset is **pure data + a pure evaluator**: no DB, no network, no LLM, deterministic.

- **A1 — Where the module lives.** A new `backend/memory/detection_rules.py` (rules table + pure evaluator) is the natural home; `bootstrap._collect_candidates` becomes a thin adapter that loads the repo files (its existing `_discover_manifest_files` / `_load_repo_file` / `_lower_files` path) and calls the evaluator. **Confirm** the module name and that `CandidateSuggestion` either stays in `bootstrap.py` and is imported by the rules module, or moves to a shared spot both import (avoid a circular import). **Recommendation:** keep `CandidateSuggestion` where it is; the rules module imports it. Re-export nothing new from `bootstrap` beyond what tests already touch (`CandidateSuggestion`, `_collect_candidates`).

- **A2 — How much of the filesystem-probe and corpus-aggregate logic becomes data vs. stays code.** Three kinds resist a flat table: the `_python_311_detected` regex-over-joined-content with separate first-match evidence selection (K2); the first-evidence-across-corpus rules (SQLite/Docker/Prisma-schema/Alembic, K4); and the **filesystem probes** that read outside the manifest-files dict (`(root / "alembic").is_dir()`, `(root / "backend" / "pipeline" / "patch_applier.py").is_file()`, K5). **Decide** for each: keep as an evaluator primitive invoked by a typed rule, or express as data with a small predicate vocabulary. **Recommendation:** model them as **typed rules with a named predicate kind** (e.g. `kind="first_evidence"`, `kind="path_exists"`), so they still live *in the ordered ruleset* (preserving position) but the evaluator runs the right primitive. Do **not** drop the filesystem probes or change what they read — Alembic's `is_dir()` fallback and the patch-applier `is_file()` rule are part of current behavior.

- **A3 — Single-source-of-truth boundary for PR-A.** The proposal's end-state is "one ruleset → three consumers (bootstrap, reality signals, test-command)." **PR-A builds the ruleset and rewires bootstrap onto it — and stops.** **Confirm** that PR-A adds **no** dimension/value metadata that only PR-B/PR-C would consume *unless* it is free and inert (e.g. a rule may carry an optional `dimension`/`signal_value` field that PR-A leaves unused and untested, purely to shape the table for the next PR). **Recommendation:** allow inert optional fields **only** if they are documented as PR-B/PR-C seams and have zero effect on PR-A's emitted candidates; otherwise leave them out and let PR-B add them. Smallest reviewable change wins. The maintainer rules on whether the seam is worth the forward-reference now.

- **A4 — Parity baseline capture.** "Behavior-preserving" is only provable against a pre-refactor baseline. **Confirm** the approach: capture golden snapshots of the **full ordered candidate list** (all six `CandidateSuggestion` fields) from the *current* `_collect_candidates` across a fixture matrix (§11A.6) **and** against the Pipewright repo itself (dogfood), commit them, then assert the refactored evaluator reproduces them byte-for-byte. **Recommendation: golden snapshots committed in the test tree**, generated from `develop` before the refactor lands, asserted in the same PR. This is the Phase-2 golden/characterization discipline the proposal's Appendix E.1 names for exactly this slice.

---

## 11A.1 Non-negotiable safety contract (from `CLAUDE.md`)

No change may weaken these. Row 11 PR-A framing in **bold**:

1. No implementation without an approved chunk plan; never bypass a gate. **Untouched — PR-A is a refactor of a suggestion *generator*; it never touches the chunk plan, the approval gates, or the orchestrator.**
2. Never edit outside approved `files_expected`; `scope_guard` is the authority. **Untouched — no execution/scope path is involved.**
3. Never create empty / no-effective-change commits. **Untouched — no commit path is involved.**
4. Never open PRs against `main`/`master`/`develop`; never auto-merge. **Untouched — no Git path is involved.**
5. Never write forbidden paths. **Untouched — the detector only *reads* manifest files (it already refuses `.env` values, see `test_bootstrap_does_not_read_dotenv_values`); the refactor preserves exactly which files are read and how.**
6. Never expose or persist secrets/tokens/PII; sanitize errors. **Untouched — the existing content gate (`validate_memory_content`) and the dotenv-value safety still run on every candidate in `generate_bootstrap_suggestions`; PR-A does not move the gate. No new logging of file contents.**
7. Memory is advisory; source code, user instruction, tests, safety rules win. **Reinforced — bootstrap candidates remain *pending suggestions* requiring human approval. PR-A is purely about *where the detection logic lives*, never about promotion, injection, or authority.**
8. AI-suggested memory stays pending until a human approves. **Untouched — `generate_bootstrap_suggestions` still inserts only `status='pending'`; PR-A adds no auto-approval, no active-fact creation.**
9. Prefer failing safely with a clear, specific error over guessing. **Preserved — the evaluator is total and deterministic; a malformed `package.json` still falls back exactly as today (`_package_uses`/`_package_has_script` swallow `JSONDecodeError` identically). No new failure mode is introduced.**

This slice is **low-risk** (advisory suggestion generator, no execution/scope/Git/secret-egress contact). The risk is **silent parity drift**, not a contract breach. Carry the §11A.7 check anyway and prove it.

## 11A.2 Current-state verification (the ground you're designing on)

Read the real code first; re-verify every `file:line`; **correct this brief's pointers if the live code drifted, and say so.**

- **`backend/memory/bootstrap.py` — `_collect_candidates` is `:191-601`** (one function, returns `list[CandidateSuggestion]`). **Pointer-drift correction:** the proposal §14 table (`:540`) and the workplan both cite `bootstrap.py:191-492`; the live function actually ends at **line 601** (the `:492` figure predates later additions — JVM/go/rust/sqlite/docker/prisma/alembic/default/patch-applier rules). Use `:191-601`. Flag this drift in your PR description; do **not** silently fix the proposal in the same PR (docs-only edits to the proposal are a separate, maintainer-requested change).
- **`CandidateSuggestion`** is the frozen dataclass at `bootstrap.py:63-70`: `content, category, scope, priority(=DEFAULT_PRIORITY), evidence_path, evidence_excerpt`. The parity snapshot is the ordered tuple of these six fields per candidate.
- **`generate_bootstrap_suggestions` (`:811-845`)** is the only consumer of `_collect_candidates`. It iterates candidates **in order**, runs `validate_memory_content` (the content gate), computes `content_hash`, and **dedupes by first-seen `content_hash`** (`seen_hashes`, `:832-834`), then by active-fact and pending-suggestion existence. **This is why candidate order is behavior** (§0): the first candidate with a given content wins; later duplicates are dropped. PR-A must not perturb order.
- **The six rule *kinds* inside `_collect_candidates`** (your ruleset must preserve each, in this position order):
  - **K1 — per-file content-marker rules (`:200-281`):** for paths ending `requirements.txt`/`pyproject.toml`/`Pipfile`/`setup.py`, substring/marker checks on **lowered** content (`fastapi`/`uvicorn`→FastAPI; `django`; `flask`; `sqlalchemy`; `pytest`; and `_content_has_any` against `DB_POSTGRES_MARKERS`/`DB_MYSQL_MARKERS`/`DB_MONGODB_MARKERS`) → fixed candidate templates with fixed priorities. **This is the large uniform block** — the prime "rules-as-data" target. Note FastAPI fires on `fastapi` **or** `uvicorn` (one candidate).
  - **K2 — corpus-aggregate with separate evidence selection (`:283-299`):** `_python_311_detected` runs a regex set over the **joined lowered content of all files**, then evidence is the **first** file whose content contains `"3.11"`. Not per-file; preserve the join + the first-match evidence pick.
  - **K3 — parsed-manifest rules for `package.json` (`:301-439`):** JSON parse + boolean composition (`_package_uses` for react/vite/typescript/next/express/nest/fastify/prisma/mongoose), then an **ordered if/elif** for the frontend-stack candidate (react+vite+ts → one; **elif** react+vite → another; **elif** next → another — mutual exclusion matters), independent `if`s for express/nest/fastify/prisma/mongoose, **script-based** rules (`_package_has_script`/`_package_script_contains` for build; `test` script with scope `frontend if react/vite/next else backend`), and raw-substring checks (`jest`/`vitest`). Prisma scope is `_scope_from_path(path)`. **The elif mutual-exclusion and the derived scopes are load-bearing parity details.**
  - **K4 — first-evidence-across-corpus rules (`:441-578`):** `pytest.ini` (filename); JVM build files (`pom.xml`/`build.gradle*`/`settings.gradle` with content substrings `spring-boot`/`maven`/`junit`, plus the `pom.xml and "maven"` sub-condition); `go.mod`/`Cargo.toml` (filenames; Cargo scope via `_scope_from_path`); and the `next(...)`-first-match evidence rules for **SQLite** (marker `_content_has_any(DB_SQLITE_MARKERS)` **or** `schema.sql` suffix), **Docker** (`Dockerfile`/`docker-compose.{yml,yaml}`), **Prisma schema** (`prisma/schema.prisma`).
  - **K5 — filesystem probes outside the files dict (`:568-599`):** **Alembic** = `alembic.ini` filename **or** `(root / "alembic").is_dir()`; **patch-applier architecture rule** = `(root / "backend" / "pipeline" / "patch_applier.py").is_file()`. These read the filesystem directly, **not** the discovered-manifest dict. Preserve exactly what they probe.
  - **K6 — unconditional default candidate (`:580-588`):** the "Never log secrets, API keys, tokens, or .env values." security rule is **always** appended (priority 0). It must remain unconditional and in its current position.
- **Shared detection constants already extracted** to `backend/repo/repo_fingerprint.py` (single source of truth): `DB_POSTGRES_MARKERS` (`:112`), `DB_MYSQL_MARKERS` (`:113`), `DB_MONGODB_MARKERS` (`:114`), `DB_SQLITE_MARKERS` (`:115`), `IGNORED_DIRS` (`:44`), `MAX_DEPTH` (`:40`), `MAX_MANIFEST_FILES` (`:41`), `discover_manifest_files`, `load_repo_file`, `content_has_any`. **bootstrap re-exports the caps under `BOOTSTRAP_*` names because existing tests monkeypatch `bootstrap.BOOTSTRAP_MAX_MANIFEST_FILES` (`:51-56`, `:103-111`).** Your refactor must keep that monkeypatch surface working — the discovery wrapper reads the module-level caps at call time; do not move discovery into the rules module in a way that bypasses it (`test_bootstrap_respects_max_manifest_files` will catch you).
- **The helper predicates** (`_package_uses`, `_package_has_script`, `_package_script_contains`, `_scope_from_path`, `_python_311_detected`, `_content_has_any`, `_add_candidate`) are the evaluator's primitive vocabulary. The cleanest PR-A reuses them verbatim from the evaluator; if you move any into the rules module, prove byte-identical behavior.
- **Existing test coverage to build on** — `backend/tests/test_memory_bootstrap.py` already pins much of the behavior (`test_bootstrap_detects_frontend_stack`, `..._backend_requirements_in_backend_folder`, `..._backend_from_arbitrary_folder_name`, `..._node_backend_from_nested_package_json`, `test_folder_name_does_not_override_dependency_content`, `test_bootstrap_ignores_node_modules_and_dist`, `..._examples_templates`, `..._respects_max_manifest_files`, `..._evidence_path_is_nested_path`, `..._does_not_read_dotenv_values`). These stay green **unmodified** — they are part of the parity proof. PR-A **adds** the ordered-list golden characterization (§11A.6); it does not rewrite these.

---

## 11A.3 Exact PR-A scope

**PR-A = extract `bootstrap.py` detection into rules-as-data, behavior-preserving. Nothing else.**

In scope:
1. A new declarative **detection ruleset** (data) + a **pure evaluator** (`detection_rules.py` per A1), representing the six rule kinds of §11A.2 in their current declaration/position order.
2. Rewire `_collect_candidates` to be a thin adapter: discover/load/lower the manifest files exactly as today (preserving the `BOOTSTRAP_*` monkeypatch caps), call the evaluator, return the candidate list.
3. **Ordered-candidate golden characterization tests** (§11A.6) proving byte-for-byte parity across a fixture matrix and the dogfood repo, plus keeping every existing `test_memory_bootstrap.py` assertion green unmodified.
4. A short docs note (proposal §11.4 / §14 already describe the end-state; PR-A adds an as-built pointer that the bootstrap detector now reads from the shared ruleset, and that PR-B/PR-C are the remaining consumers). Update the workplan's Row-11 line and the `bootstrap.py:191-492` pointer drift **only if the maintainer asks to commit doc changes** — otherwise just flag the drift in the PR description.

Outcomes the design must deliver (the *what* is fixed; the *how* is yours):
- A **pure, deterministic, total evaluator** over a declarative ruleset, no DB/network/LLM.
- `_collect_candidates` produces the **identical ordered candidate list** as before — proven against committed pre-refactor goldens **and** the dogfood repo.
- Every existing `test_memory_bootstrap.py` assertion green, unmodified.
- The `BOOTSTRAP_MAX_MANIFEST_FILES` monkeypatch surface and the dotenv/secret safety preserved.
- `ruff check` clean on changed files. No frontend change (backend-only slice).

## 11A.4 Non-goals (name these in your PR)

- **PR-B — emitting repo reality signals** for the six dimensions `check_fact_against_signal` supports (`db_engine`, `backend_framework`, `frontend_framework`, `test_runner`, `migration_tool`, `package_manager`). **Later.** PR-A may shape the table to make PR-B cheap (A3) but emits **no** signal and adds **no** signal test.
- **PR-C — backfilling §23 order-row 1's test-command detection** onto the shared ruleset. **Later.** `test_command_quality` stays the classifier; PR-A does not touch it.
- **Any new behavior at all** — no new framework/runner detected, no template/priority/scope changed, no candidate added or removed, no ordering changed. PR-A is parity-only.
- **No memory auto-approval, no active-fact creation** — candidates stay pending; the approval path is untouched.
- **No request-aware selection (Row 12), no post-run hygiene (Row 16), no retriever/FTS (Row 19), no vector/embedding work (Row 23), no thread/chat UI (Rows 22b–22e).** These are behind the §E.1 hard stop and/or open §24 decisions.
- **No schema change, no injection-path change, no gate/scope/Git contact.**
- **No proposal/workplan content rewrite beyond flagging the `:191-492→:191-601` drift** (commit docs only if the maintainer asks).
- **No collapsing of the heterogeneous rule kinds into a lossy uniform table** that changes semantics to look more "declarative."

## 11A.5 Safety invariants (the parity contract, restated as testable claims)

1. **Ordered-list identity.** `_collect_candidates(root)` returns a list equal — element-by-element, all six fields, **same order** — to the pre-refactor list, for every repo in the fixture matrix and for the dogfood repo. *(The §0 headline.)*
2. **Dedupe-survivor identity.** Because dedupe is first-seen by content hash, the surviving `evidence_path`/`evidence_excerpt`/`priority` for every duplicated content (MongoDB, pytest, SQLite, etc.) is unchanged. *(Falls out of #1 but is asserted explicitly via `generate_bootstrap_suggestions` end-to-end on at least one repo that triggers a duplicate.)*
3. **Read-surface identity.** The set of files read, the caps applied, and the monkeypatch behavior (`BOOTSTRAP_MAX_MANIFEST_FILES`) are unchanged. *(`test_bootstrap_respects_max_manifest_files` + the ignore-dirs tests.)*
4. **Safety-gate identity.** The dotenv/secret refusal and `validate_memory_content` gate still run on every candidate, unmoved. *(`test_bootstrap_does_not_read_dotenv_values`, `test_bootstrap_validation_rejects_secret_like_suggestion`.)*
5. **Pending-only identity.** No path creates an active fact or auto-approves; suggestions remain `status='pending'`. *(Existing approval tests stay green; no new write path.)*
6. **Determinism/totality.** The evaluator has no DB/network/LLM/clock dependence; identical input repo → identical output, every run.

## 11A.6 Characterization / parity test plan

**The test strategy is the whole point of PR-A.** It must make a silent parity break impossible to ship.

- **Golden ordered-candidate snapshots (the core).** Build a **fixture matrix** of synthetic repos (reuse the `project_repo` tmp-dir + the `_write_basic_python_repo` style already in `test_memory_bootstrap.py`) that, between them, trigger **every** rule kind and the known collisions:
  - Python backend: `requirements.txt`/`pyproject.toml`/`Pipfile`/`setup.py` variants → FastAPI(+uvicorn), Django, Flask, SQLAlchemy, pytest, and each DB-marker (postgres/mysql/mongodb).
  - Python 3.11 hint (K2) in a couple of manifest shapes → assert the **first-`3.11`-file** evidence pick.
  - `package.json` variants (K3): react+vite+ts; react+vite (no ts); next; express; nest (all three nest spellings); fastify; prisma (+ `@prisma/client`); mongoose / `mongodb`; build script (vite and non-vite); jest; vitest; test script with frontend vs. backend scope. **Assert the elif mutual-exclusion** (react+vite+ts must yield exactly the combined candidate, not the react+vite one).
  - K4: `pytest.ini`; `pom.xml`(+maven/spring-boot/junit); `build.gradle`/`.kts`/`settings.gradle`; `go.mod`; `Cargo.toml` (root and nested → scope); `schema.sql`-only SQLite; SQLite marker in a manifest; Docker (`Dockerfile`, both compose names); `prisma/schema.prisma`.
  - K5: an `alembic.ini`; an `alembic/` **directory with no ini** (the `is_dir()` fallback); a repo with `backend/pipeline/patch_applier.py` present (the architecture rule) and one without.
  - K6: assert the security default candidate is present, last-among-unconditionals, priority 0, in **every** fixture.
  - **Collision fixtures:** a repo with both a `mongoose` package.json and a `DB_MONGODB_MARKERS` manifest (→ assert which "Project uses MongoDB." survives dedupe end-to-end via `generate_bootstrap_suggestions`); a repo with both `requirements.txt` pytest and `pytest.ini` (→ pytest candidate survivor).
  - **Procedure:** on `develop` *before* the refactor, run the current `_collect_candidates` over each fixture, serialize the **ordered list of all six fields**, and commit it as a golden (JSON or an inline expected-list). After the refactor, assert equality. The fixtures are deterministic, so the goldens are stable.
- **Dogfood snapshot.** Run `_collect_candidates` over the **Pipewright repo root itself** and snapshot the ordered list; assert parity post-refactor. This catches anything the synthetic matrix misses (real `requirements`/`schema.sql`/`alembic`/`patch_applier.py` interplay). If the snapshot is environment-sensitive (it should not be — discovery is capped and deterministic), pin the inputs.
- **Existing suite unchanged.** Every test in `test_memory_bootstrap.py` passes **unmodified**. If any existing test needs a change to pass, that is a parity break — stop and investigate, do not edit the test to match new output.
- **Evaluator unit tests (purity).** Direct tests of the evaluator over hand-built file dicts: it is pure (same input → same output), total (empty repo → just the unconditional K6 default; malformed `package.json` → same fallback as today), and order-stable.
- **Run:** `python -m pytest backend/tests/test_memory_bootstrap.py -q` and the new rules test file; then the unit set `python -m pytest backend/tests -q -m unit`. `ruff check` on changed files. No `npm` build (backend-only).

## 11A.7 Manual smoke plan

A real, human-visible parity check beyond the asserted goldens:

1. On `develop` (pre-refactor), pick 2–3 real repos (Pipewright itself; a Node/React repo; optionally a JVM or Go repo), run `generate_bootstrap_suggestions` for a throwaway project pointed at each, and record the **ordered list of pending suggestions** (content + evidence_path + priority) from `list_suggestions(project_id, status="pending")`.
2. On the PR branch (post-refactor), repeat against the **same** repos and a fresh throwaway project, and **diff** the recorded suggestion lists. They must be identical (same content, evidence, priority, order).
3. Confirm the suggestions are all `status='pending'` (nothing auto-approved, no active fact created) — spot-check `memory_facts` is untouched for the throwaway project.
4. Record the diff result in the PR description as the manual validation line. *(No external service is contacted; this is a local read-only smoke.)*

## 11A.8 Rollback plan

- **PR-A adds a module and rewires one function; it has no flag and needs none** (there is no behavior to toggle — it is parity-only). The rollback lever is **`git revert` of the single PR**: because `_collect_candidates`'s public contract and the entire consuming path are unchanged, reverting the extraction restores the exact prior code with no data migration, no schema change, and no state to unwind.
- The change is **schema-free and state-free**: no column, no migration, no persisted artifact changes. Existing pending suggestions, facts, and projects are unaffected by either applying or reverting.
- Because parity is asserted by committed goldens, a future regression in this area is caught by the test suite, not discovered in production — but if one slips through, revert is clean and total.

## 11A.9 Risks / edge cases

- **(a) Reordering candidates.** The top risk. Any change to declaration/evaluation order silently changes dedupe survivors (§0). *Mitigation:* the ruleset's declaration order **is** the candidate order (A0); golden tests assert the ordered list, not a set; a collision fixture asserts the survivor end-to-end.
- **(b) Dropping or merging a rule kind.** Forcing K2/K3/K4/K5 into a flat table can lose the corpus-join (3.11), the elif mutual-exclusion (frontend stack), the first-evidence pick (SQLite/Docker), or the filesystem probes (Alembic dir, patch-applier). *Mitigation:* model them as typed rules (A2), not flattened; per-kind fixtures.
- **(c) Marker/substring drift.** Re-typing a substring (`spring-boot`, `@nestjs/core`, `uvicorn`) or swapping a `_content_has_any(MARKERS)` for a single literal changes matches. *Mitigation:* reuse the `repo_fingerprint` marker frozensets and the existing helper predicates verbatim; do not re-spell literals.
- **(d) Breaking the monkeypatch caps surface.** Moving discovery into the rules module could bypass `bootstrap.BOOTSTRAP_MAX_MANIFEST_FILES`. *Mitigation:* keep discovery in the bootstrap adapter (A1); `test_bootstrap_respects_max_manifest_files` guards it.
- **(e) Filesystem-probe relocation.** The Alembic `is_dir()` and patch-applier `is_file()` rules read `root`, not the files dict. A rules module that only sees the files dict would silently drop them. *Mitigation:* pass `root` to the evaluator (or run those probes in the adapter at their current positions); fixtures for both with-dir and with-file cases.
- **(f) Scope-derivation drift.** `_scope_from_path` (Prisma, Cargo) and the `test`-script frontend/backend scope are derived, not constant. *Mitigation:* reuse `_scope_from_path` verbatim; fixtures that exercise both branches.
- **(g) Over-scoping into PR-B/PR-C.** Adding signal emission or test-command detection "while we're here" breaks the smallest-reviewable rule and the §E.1 sub-PR split. *Mitigation:* A3 keeps any forward-reference fields inert and untested; the PR description names PR-B/PR-C as out of scope.
- **(h) Circular import / `CandidateSuggestion` ownership.** Moving the dataclass can create a `bootstrap ↔ detection_rules` cycle. *Mitigation:* A1 keeps `CandidateSuggestion` in `bootstrap`; rules module imports it (or both import from a leaf module).
- **(i) Editing an existing test to make it pass.** If a `test_memory_bootstrap.py` assertion fails post-refactor, that is a real parity break, not a stale test. *Mitigation:* the existing suite is frozen during PR-A; investigate any failure as a bug in the refactor.

## 11A.10 Explicit statement: PR-B and PR-C are later

**Row 11 ships in three ordered sub-PRs; this cycle opens PR-A only.** PR-A is the behavior-preserving extraction of `bootstrap`'s detection into rules-as-data with proven parity — and it **stops there.**

- **PR-B (later) — emit repo reality signals advisorily.** Compute the repo signal *values* for the six dimensions `check_fact_against_signal` already supports (`db_engine`, `backend_framework`, `frontend_framework`, `test_runner`, `migration_tool`, `package_manager`; `memory_trust.SUPPORTED_REALITY_DIMENSIONS`), surfacing **advisory-only**. It mutates nothing — it never marks a fact stale, never archives, never auto-bumps `last_verified_at` (that is decision D8/B3, not this slice).
- **PR-C (later) — backfill test-command detection** (§23 order-row 1's detector) onto the shared ruleset, so `test_command_quality`'s duplicated runner knowledge reads from the one source. `test_command_quality` stays the *classifier*; only the *detector* table becomes shared.

Neither PR-B nor PR-C is in this window. After Row 11, the **§E.1 hard stop** holds: run a real self-use smoke before opening any of the backfill set (6b/6c/7a-`plan_versions`/7b/9b) or the deeper Area B rows (12 request-aware selection, 16 post-run hygiene, 19 retriever/FTS, 23 vector/embedding) or the §21 thread UI rows (22b–22e). Those are gated on open §24 decisions (D5/D6/D7/D13) and are **not** opened by this brief.

---

## 11A.11 Update these docs when you finish (part of "done")

1. **`PIPEWRIGHT_REDESIGN_WORKPLAN.md`** — mark Row 11 PR-A done in the sequence/TL;DR; note PR-B/PR-C remain. *(Commit only if the maintainer asks.)*
2. **The `bootstrap.py:191-492` pointer** in proposal §14 (`:540`) and the workplan — correct to `:191-601`, or at minimum flag the drift in the PR. *(Commit only if asked.)*
3. **A short `docs/design/` as-built note** (or extend proposal §11.4) — the detection ruleset shape and that bootstrap now reads from it; PR-B/PR-C are the remaining consumers.
4. **This file** — once PR-A lands and is reviewed, repurpose this rolling brief for Row 11 PR-B (reality signals), or mark it dormant until the next slice.
5. These planning docs are **tracked**. Update content; **do not commit** doc or code changes unless the maintainer asks. If asked to commit: branch off `develop` first (never straight to `develop`/`main`), one purpose per commit, end with the repo's `Co-Authored-By` trailer.

## 11A.12 Working discipline (this slice)

- **Design first, then get it reviewed, then implement.** Produce a short design answering **A0–A4** — the ruleset representation, the module home, how the procedural/probe kinds are modeled, the SoT boundary for PR-A, and the parity-baseline approach — and have the maintainer review it **before** writing code. You own the design; because you also write the tests, the design review is the homework check.
- **Read the real code first; re-verify every `file:line`; correct this brief's pointers** if the live code drifted and say so (you already have one known drift to confirm: `:191-492 → :191-601`).
- **Capture the parity goldens on `develop` before you touch anything** — "behavior-preserving" is only provable against a pre-change baseline.
- **Smallest correct change; Row 11 PR-A only.** List what you deliberately did **not** change (reality signals, test-command detection, the approval/injection/scope/Git paths, the schema, the deeper memory rows, the thread UI).
- Tests assert the **decided behavior** (ordered-candidate parity, preserved rule kinds, preserved read-surface and safety gates) **and the §0 invariant** (identical ordered candidate list) — not just that code runs.
- Report on completion: changed files, tests run + results, manual validation (the §11A.7 diff), risks, and what was intentionally left untouched.
- A human reviews this PR.
