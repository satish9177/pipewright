# Red-team audit — Pipewright after Area A / Pass 1

**Date:** 2026-06-12
**Auditor:** Fable (adversarial design + implementation review)
**Status:** Record only. No fixes implemented in this document; findings are a hardening queue.

---

## 1. Scope

- Area A (pipeline execution engine) **Pass 1**, audited after the final two slices landed: **item 17a** (trivial-task stage profile, PR #288) and **item 17b** (provider prompt caching, PR #290). Phase 4 and Area A Pass 1 are marked complete.
- Focus: **local-first open-source readiness** — reliability and contributor trust for a single developer running Pipewright on their own machine against a local repo.
- **Not** a production / hosted / multi-user / team audit. Deployment, BYOK-in-DB, provider-settings UI, and execution modes remain intentionally paused (see `README.md`).
- Verified against current `develop` content (code, not only docs). Where a claim could not be confirmed from code it is marked **uncertain** below.
- The full backend unit suite was green at audit time: `2989 passed, 1 skipped, 4 deselected, 0 failures`.

---

## 2. Headline verdict

**The safety core is strong and matches the design intent.** The audit attacked the gates, scope guard, Git/PR rules, rollback, resume, reviewer acknowledgement, and prompt caching directly; the obvious bypasses did not work (see §4, false alarms).

**The remaining risks live one layer down** — in the file/index layer, language support, onboarding honesty, and soak observability. These matter precisely because *observable, human-controlled automation* is the product's reason to exist: a broken first 30 minutes reads as "untrustworthy," not "beta."

Self-use today (Python/pytest repos): **strong.** Open-source demo: **yes, with conditions** (land F1; demo on a Python repo; refresh onboarding docs). Inviting external users: **not yet** — the safety story is external-ready, the first-30-minutes story is not.

---

## 3. Top findings (F1–F10)

Severity scale: **must-fix** / **should-fix** / **later**.

### F1 — Windows newline / EOL rewrite risk — **must-fix**
`patch_applier.py` writes/reads with `encoding="utf-8"` and **no `newline=` argument**, so on Windows Python translates `\n`→CRLF on write and normalizes on read. An `edit`/`modify` against an LF repo (Node/Rust/modern Python with `.gitattributes eol=lf` or `core.autocrlf=false`) rewrites the **whole file's** line endings. Cascades: whole-file diffs swamp the reviewer diff cap, churn the diff-hash that ack staleness binds to, bloat the cumulative final diff, and fail LF-strict verification (`prettier --check`, `eslint linebreak-style`, `gofmt`, `rustfmt`) as a false `TEST_REGRESSION`. Invisible under `core.autocrlf=true` checkouts, which is why smokes missed it. Fix: per-file EOL detect/preserve in `patch_applier.py` **and** its `patch_dry_run.py` twin (keep in lockstep).

### F2 — Language-support / indexer extension cliff — **must-fix if multi-language is claimed; otherwise should-fix + honest docs**
`repo_indexer.py` `SUPPORTED_EXTENSIONS` is a fixed set ({.py, .js, .jsx, .ts, .tsx, .java, .sql, .yml, .yaml, .json, .toml, .md, .txt}); `file_alias_grounding.py` deliberately reuses it as `_KNOWN_EXTENSIONS`. Missing `.rs .go .rb .cs .php .kt .swift .scala .vue .svelte .html .css .xml (pom.xml!) .gradle .properties (Spring!)`. Consequences on those repos: indexer indexes ~nothing → triage returns empty `files_expected` + high risk → `scope_guard` dead-ends at execution; **and** the user's explicit "only modify `src/main.rs`" is not even detected as a file mention (constraint parser requires a known extension), so the allowlist/forbidden/steer-precheck machinery is blind (scope_guard still backstops apply). Separately, **baseline-aware verification (T3) is pytest-only** — `extract_pytest_failing_test_ids` + `_baseline_accepts_failed_result` require parseable pytest node IDs on both sides; Jest/Maven/cargo repos with one pre-existing red test fail every chunk with an honest but dead-end "non-comparable" disclosure. Slice: (1) extend the extension set; (2) honest language-support matrix in README; (3) per-runner failing-ID parsers later.

### F3 — `should_skip_path` substring de-indexing bug — **should-fix**
`repo_indexer.py` skips a path when a skip-name is a **substring of the file name** (not an exact name match). So `distance.py` ("dist"), `targets.py` ("target"), `coverage_report.py`, `build_tools.py` are silently never indexed → can't be grounded → "verify the path or reindex" hardening that reindexing can't fix. Deterministic, invisible, and the error message actively misleads. Fix: exact-name match for file names, keep exact part-match for directories.

### F4 — No-tests repos fail baseline with misleading copy — **should-fix**
`_ensure_verification_baseline` fails the run on any non-OK integrity including `NO_TESTS_RAN` (markers include pytest "no tests ran" and npm's default "no test specified"), with the message "Fix the test environment or command." Test-less personal repos are the normal local-first case; the copy implies breakage when the truth is "you have no tests." The working escape hatch (configure `echo ok` → WEAK → #28F ack at final approval) is undiscoverable. Fix: a dedicated `NO_TESTS_RAN` baseline narrative + setup docs. **Do not weaken the gate.**

### F5 — 17a soak control-cohort pollution / stale soak doc — **should-fix (before trusting the soak)**
Ineligible fresh chunks under nonzero sampling record `stage_profile='standard'` — the same label as the eligible-but-unsampled control — so the soak control mixes in never-eligible chunks (multi-chunk, risky) that fail more, **biasing the keep/rollback comparison optimistic for `merged_plan_code`**. Two adjacent doc defects in the same area: (a) the soak doc's NULL table omits that `auto_retry` continuation rows keep `stage_profile='merged_plan_code'`; (b) the soak doc still says "Item 17b is NOT implemented yet / `PROMPT_CACHE_ENABLED` does not exist on develop" — stale since PR #290. Fix: record eligibility (a third label or an additive nullable flag) + refresh the soak queries and the two stale notes.

### F6 — Default-on 17a experiment with unlabeled synthesized plans — **should-fix before OSS exposure**
`policy.MERGED_PROFILE_SAMPLE_PCT = 50` ships on for every clone; the synthesized handoff persists as a normal plan-summary and the `synthesized_from_triage` evidence lives only in the `chunk_attempts` ledger. **No UI/summary surface tells the human that no planner LLM examined this chunk** — and trivial-eligible chunks are by construction the auto-commit ones, so the first human eyes are at final approval. Cuts against the observable-automation moat. Fix: label the plan source in the persisted summary / final-approval payload; separately decide keep-50-documented vs. default-0-with-opt-in for any public release (recommend default-0 at release, 50 only while the maintainer is the sole user).

### F7 — Trivial-profile denylist misses flat auth/security filenames — **should-fix (small)**
`stage_profile.py` matches denylist entries like `auth`, `login`, `session` only as whole path **segments** or exact basenames. `src/auth.py`, `login.py`, `sessions.py`, `jwt_utils.py` escape, so planner elision rests on the LLM-asserted `risk_level`/`requires_human_review` — exactly where the deterministic margin should hold. Contained (reviewer still runs; scope/path-safety/gates unchanged), so the only loss is planner elision on a security-adjacent file. Fix: add basename globs (`auth*`, `*auth*`, `login*`, `session*`, `*password*`, `crypto*`, `jwt*`, `oauth*`, `*token*`).

### F8 — Negative constraints are not deterministically surfaced — **should-fix**
"create this feature but do not add tests": nothing detects or enforces the concept-level negative; triage/planner may still plan test files. Contained — the mandatory chunk-plan gate shows `files_expected` and `scope_guard` blocks unapproved test files post-approval — but only an attentive human catches it. Path-shaped constraints ("only modify X / do not touch Y") work well; directory-/concept-level phrasing ("don't touch the auth module") yields no detection, no note, no flag, and a missed parse is indistinguishable from "no constraint given." Proposal §4.6 layer 2 (show detected constraints as structured fields at plan approval) was never implemented. Fix: persist `UserFileConstraints` at run creation and render them read-only at the plan gate; concept-level negatives stay explicitly out of deterministic scope.

### F9 — Hardcoded provider model allowlists can go stale — **later**
`anthropic.py` `_SUPPORTED_MODELS` is a hardcoded list that already tops out below current model IDs; pointing a role (env-overridable, good) at a newer ID raises `UnsupportedModelError`. **Uncertain:** the other providers' lists were not audited, but the pattern is shared. Fix: prefix-accept `claude-*` with a warning, or move lists to policy/env.

### F10 — Local-first ergonomics: global timeout, source-edit flags, lite default model — **later (mostly by design)**
`TESTER_TIMEOUT_SECONDS = 300` is global with no per-project override (large suites → `TIMEOUT` → deliberately no auto-retry → human pause every chunk). Activating 17b requires editing `policy.py` + restart with no env/config surface and no UI indication of cache state (default-off confusion is real but harmless; **Anthropic edge cases checked and safe** — short marked prompts silently uncached with no error, memory/user messages never marked). `role_config.py` defaults **all roles, including coder, to `gemini-2.5-flash-lite`** — the out-of-box quality impression rides on a lite model, which deserves a deliberate decision + README guidance. Policy layering is explicitly deferred by design; keep deferred but a per-project test timeout and a `PROMPT_CACHE_ENABLED` env read are cheap when needed.

### Bonus (onboarding, trivial) — hardcoded foreign `cache_dir`
`backend/pytest.ini` pins `cache_dir = C:\Users\Hp\pipewright\.pytest_cache`, an absolute path from a different machine. Every contributor's first `pytest` run emits `PytestCacheWarning: ... Access is denied: 'C:\Users\Hp'` and the cache (incl. `--lf`/`--stepwise`) silently doesn't work. One-line fix: `cache_dir = .pytest_cache`. (Note: `pytest.ini` is config, not runtime code — out of scope for this docs-only record; listed for the queue.)

---

## 4. False alarms / verified-safe paths

These were attacked and held; record them so they are not re-litigated.

- **17b prompt-caching payload risk** — flags ship off ⇒ byte-identical string path; on ⇒ concatenated system text provably byte-identical, metadata-only delta; cache hit/miss ignored downstream; OpenAI/DeepSeek/Gemini/Fake ignore the marker; nothing run-varying or memory-bearing is ever marked. Pinned by `test_prompt_cache.py`.
- **Scope bypass** — `scope_guard` is exact-match, fails closed on empty scope, runs in every driver mode before apply; the main path got the dry-run preflight (E8); steer pre-check refuses out-of-scope mentions side-effect-free (409, zero mutation); the only widening channel is the human-approved #27 flow.
- **Retry / steer / refine gate bypass** — human and steered attempts always pause at the chunk gate; the refinement §0 invariant (a failed refinement restores the completed chunk + commit) holds on every failure path including the exception guard, pinned by `test_refine_completed_chunk.py`; final-gate supersede is atomic with a clean 409 on race.
- **Reviewer ack route enforcement** — enforced on both chunk-approve and final-approve routes; an unavailable/failed reviewer delivers no findings so the gate can never deny approval; ack creation is strict, diff-hash bound, stale after any re-run. Accepted-by-design residual: route-level fail-open means a systematic read-model bug would silently disable the *soft* gate (warning log only).
- **PR / local_only / GitHub safety** — forbidden bases single-sourced (`branch_safety.py`), default `pipewright-staging`, missing remote base = clear preflight error with zero side effects, idempotent PR metadata saves, sanitized push errors, `local_only` does no remote action, no auto-merge path exists.
- **SQLite / interrupted runs** — WAL + 5s busy timeout; resume is fail-closed with checkpoint verification plus the P7 exact-SHA HEAD-drift check; dirty-tree precondition in every mode; ledger writes best-effort and never break the pipeline.
- **Missing / typo'd test command** — exit 127/9009 → `COMMAND_NOT_FOUND` fails the run at baseline **before any LLM spend**, with an accurate message. The proposal's "cheapest failure" promise is implemented.

---

## 5. Recommended next local-first PRs

One small PR per finding group; safety invariants unchanged in all of them.

- **PR 1 — F1 EOL preservation** in `patch_applier.py` and its `patch_dry_run.py` twin (per-file EOL detect/preserve; byte-level round-trip tests). Highest user-visible payoff per line changed.
- **PR 2 — F2/F3 indexer reach + correctness**: extend `SUPPORTED_EXTENSIONS` (`.rs .go .rb .cs .kt .gradle .properties .xml .html .css .vue .svelte` …) and switch `should_skip_path` to exact file-name matching. One change unlocks indexer, grounding, constraint detection, and the steer pre-check together.
- **PR 3 — F5 soak cohort eligibility label + soak-doc refresh** (additive, replay-safe; folds in the stale-17b and auto-retry-row corrections). Makes the 17a keep/rollback decision trustworthy.
- **Later — F4 / F6 / F8 local-first UX**: no-tests onboarding narrative; synthesized-plan labeling + release default decision; persist & surface detected file constraints at the plan gate.

---

## 6. Non-goals (for the PRs this audit recommends)

- **Do not start Area B** (memory / request-aware retrieval).
- **Do not start** the stage-driver/worker, durable-runtime/thread, or vector-memory work.
- **Do not implement any F1–F10 fix in the docs PR that records this audit.**
- **Do not weaken any safety invariant** — no auto-merge, no hidden scope expansion, no AI approval/reviewer/memory authority, no protected-base PR bypass. Findings are fixed *around* the invariants, never through them.

---

## 7. How to use this audit

- Treat F1–F10 as the **local-first hardening queue** to work down *before* inviting wider open-source use. The safety core does not block that invitation; the file/index/onboarding layer does.
- Land **one small PR per finding group** (the §5 ordering), each with its own tests, in the project's normal per-PR review gate. Prefer deterministic checks over LLM checks throughout.
- Keep the **finding IDs (F1–F10) stable** when referencing this audit from PRs or follow-up notes, so the queue stays traceable.
- Re-run this style of adversarial pass at the next major milestone (e.g. before Area B exposure), not as a one-off.
