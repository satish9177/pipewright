# Pipewright Redesign — Implementation Handoff Brief (rolling, per-phase)

**Date:** 2026-06-11
**What this file is:** the **rolling** implementation handoff brief for the redesign. Each phase's active spec lives here; when a phase lands, this file is repurposed for the next one (Phase 1's brief — items 8 & 9 — was the previous occupant and is now superseded; its outcome is recorded in the workplan). **Current contents: Phase 2.**
**For:** the model implementing Phase 2. You may work **without the author in the loop on the typing**, but Phase 2 is the **CRITICAL BOUNDARY**: it is *not* an unsupervised flat fan-out. A human reviews each of the three PRs before the next starts (see §0). Re-verify every `file:line` against the live code before you cite or edit it; the repo moves and the Phase-1 work already shifted line numbers.
**Source of record:** `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (§3 Candidate B, §4.1, §4.2, §5, §6) and `PIPEWRIGHT_REDESIGN_WORKPLAN.md`. This brief operationalizes them; if they disagree, the proposal wins and you flag the drift.
**Mode:** design + implement + test, **one item per PR, serialized 10 → 11 → 12** (P7 folds into 12). Behavior-preserving at every step. Do **not** start Phase 3.

---

## 0. The boundary you are standing on (read before anything else)

Phases 0 and 1 were parallel-safe because their items were independent, decision-free deterministic fixes. **Phase 2 is none of those things.** It is a behavior-preserving *strangler refactor of the apply / commit / rollback core* — the exact place where a subtle miss becomes a safety regression. Three rules are non-negotiable:

1. **Three serialized PRs, in order: item 10 → item 11 → item 12.** Item 11 depends on item 10's stage contract; item 12 depends on item 11's driver. Never fold two items into one PR "because the next one was easy."
2. **A human reviews each PR before the next begins.** You own the tests, but the implementer cannot grade its own homework — the golden tests below must capture *today's* behavior **before** you refactor, never be written to match whatever the new code produces. If the only proof a path is unchanged is a test you wrote after changing it, you have proved nothing.
3. **Behavior-preserving means byte/branch-identical observable behavior.** Same statuses, same commits, same gates, same failure reports, same rollback semantics. The only thing that changes is *where the code lives*, plus the additive attempt ledger (item 12) and P7's strengthened resume check.

### Decisions already made (do not re-litigate)

- **§5.1** (regression-vs-baseline gate + scoped option default-OFF) and **§5.2** (auto-retry `INFRA_ERROR` budget = 1) — DECIDED 2026-06-11, already implemented in Phase 1. Phase 2 *moves* this behavior into the driver; it does **not** change it.
- **§5.3** (steer-without-replan) and **§5.4** (reviewer ack gate) are **still PENDING** and gate **Phase 3 / Phase 4** — *not* Phase 2. Do **not** implement the `steered` entry mode, the real `retry_with_instruction` execution path, or the reviewer ack soft-gate here. Item 11's entry-mode table leaves a *seam* for `steered`; it does not fill it.

## 1. Non-negotiable safety contract (from `CLAUDE.md`)

No change may weaken these (enforced in `scope_guard`, the approval gates, `patch_applier`, `path_safety`):

1. No implementation without an approved chunk plan; never bypass chunk-plan or final-approval gates.
2. Never edit outside approved `files_expected`; `scope_guard` is the authority — the driver **moves call sites, never semantics**.
3. Never create empty / no-effective-change commits; never push zero-commit branches.
4. Never open PRs against `main`/`master`/`develop`; never auto-merge.
5. Never write forbidden paths (`.env`, `.git/`, secrets, keys).
6. Never expose or persist secrets/tokens/PII; sanitize provider/Git errors.
7. Memory is advisory; source code, user instruction, tests, and safety rules win on conflict.
8. AI-suggested memory stays pending until a human approves.
9. Prefer failing safely with a clear, specific error over guessing.

Phase 2 touches the **most** safety-critical code in the system: the clean-tree precondition, scope pre-check, dry-run, guarded apply, rollback, the no-op-commit guard, and the resume skip-logic. Every PR must carry an explicit safety-contract check (each item has a §x.5 below). When in doubt, fail safe with a clear error and keep today's behavior.

## 2. The foundation Phases 0–1 left you (do not re-do; build on)

- **`backend/pipeline/policy.py`** is the single source for behavioral constants: `TESTER_TIMEOUT_SECONDS`, `MAX_OUTPUT_CHARS`, `AUTO_RETRY_INFRA_BUDGET = 1`, `SCOPED_VERIFICATION_ENABLED = False`, file-context caps, `REVIEWER_MAX_DIFF_CHARS`. Any new Phase-2 cap (e.g. the attempt-ledger size cap, a per-run attempt budget) goes **here**, never as a buried literal.
- **`backend/pipeline/test_run_validation.py`** exports the pure `classify_execution_integrity(...) -> ExecutionIntegrity` (Signal C). Consume it; do not modify it.
- **Item 8 split + auto-retry** lives in `chunked_orchestrator.py`: `_classify_test_failure`, `_build_test_failure_report`, `_should_auto_retry_harness_error`, `_record_auto_retry_start`, `_record_auto_retry_result`, and `AUTO_RETRY_INFRA_BUDGET`. **Critical for item 11:** the auto-retry currently re-implements the *entire* `code → no-changes → scope → dry-run → apply → test` sequence inline inside `_execute_single_chunk` (re-verify — roughly the `if not test_result.passed:` block after `:1890`). That inline copy is the divergence the driver exists to delete: there are now **three** copies of the stage sequence — fresh (`_execute_single_chunk`), the inline auto-retry, and `_execute_retry_attempt` (`:2885`). Collapsing all three into one driver loop is the whole point of item 11.
- **Item 9 baseline verification:** `run_baseline_tests`, `_ensure_verification_baseline`, `_run_tests_baseline_kwargs`, baseline roll-forward and disclosure helpers in `chunked_orchestrator.py`. The driver's `verify` stage wraps these; behavior unchanged.

---

# ITEM 10 — Extract stages behind a uniform `StageOutcome` contract (no caller change)

## 10.1 Summary & scope

Turn each pipeline step into a stage callable with one uniform signature and one typed return, **without changing any caller**. After this PR, `_execute_single_chunk` still runs the same steps in the same order and produces identical results — but each step is now a pure-ish stage that returns a `StageOutcome` carrying its outcome *class*. This is the substrate the driver (item 11) iterates. Nothing about execution order, gates, commits, or rollback changes yet.

**The ordered stage list (proposal §4.1):** `plan → code → preflight (scope + dry-run) → apply → verify → review → gate-or-commit`.

**The outcome taxonomy (proposal §3 Candidate B-3, §4.2):** every stage returns one of five classes —
- `SUCCESS`
- `CODE_REJECTED` (test regression, requirement mismatch — the change is wrong)
- `INFRA_ERROR` (harness crash, timeout-ambiguous, collection error, rate-limit exhaustion — the world broke)
- `POLICY_BLOCKED` (scope violation, forbidden path, dirty tree — the rules said no)
- `NEEDS_HUMAN` (gates, acks, clarifications)

**In scope (exact files):**
- A new small module (e.g. `backend/pipeline/stage_contract.py`) defining `StageOutcome` (outcome class enum + evidence refs + checkpoint payload + the existing `PatchFailureReport`/handoff carried verbatim) and the `OutcomeClass` enum.
- The existing stage modules adapted to the contract **behind their current functions** — prefer thin adapter wrappers over rewriting `run_planner`/`run_coder`/`assert_files_in_scope`/`dry_run_changes`/`apply_patch_guarded`/`run_tests`/the reviewer so the pure stages keep their current signatures and tests.
- A single mapping from the closed `PatchFailureType` taxonomy (`patch_failures.py`) onto the five outcome classes (e.g. `SCOPE_VIOLATION`/`FORBIDDEN_FILE`/`DIRTY_WORKTREE` → `POLICY_BLOCKED`; `HARNESS_ERROR`/timeout → `INFRA_ERROR`; `TEST_REGRESSION` → `CODE_REJECTED`; etc.). One source of truth for the map; reused by item 11.

**Explicitly out of scope (name these in your PR):**
- The driver itself, and any change to `_execute_single_chunk`'s control flow — item 11.
- Moving rollback out of `tester.py` — item 11.
- Deleting `_execute_retry_attempt` or the inline auto-retry — item 11/12.
- The attempt ledger and P7 — item 12.
- Renaming any DB status string (`core/statuses.py`) — never; the user-facing phase model is a Phase-4 read-model.

## 10.2 Verified current behavior (re-confirm before editing)

- Stage sequence inside `_execute_single_chunk` (`:1742`): dependency guard → `running` status → clean-tree precondition (`DIRTY_WORKTREE`) → enriched description → `run_planner` → `_surface_files_expected_for_edit` → `run_coder` → `NO_CHANGES` guard → `assert_files_in_scope` (`SCOPE_VIOLATION`) → `dry_run_changes` (apply-phase classify) → `apply_patch_guarded` → `run_tests` → `_persist_test_run_verdict` → fail/auto-retry/commit branch.
- `PatchFailureType` is a **closed** enum; `PatchFailureReport` is persisted into `chunks.completion_summary` and must round-trip (`patch_failure_report_from_completion_summary`). The outcome-class map must not require any change to the stored shape.
- Stages already pure / near-pure: planner, coder, `scope_guard`, `patch_applier`/`patch_dry_run`, `tester`, `reviewer`. Most need only an adapter.

## 10.3 Tests that must exist and pass

- **Contract round-trip:** each adapted stage, given the inputs the orchestrator passes today, returns a `StageOutcome` whose carried `PatchFailureReport`/handoff is identical to what the underlying function returns today (snapshot the current return first).
- **Taxonomy map totality:** every `PatchFailureType` member maps to exactly one `OutcomeClass`; a test iterates the enum and asserts no member is unmapped (guards against a future enum addition silently defaulting).
- **Golden no-behavior-change:** an end-to-end `_execute_single_chunk` test (fresh, success path; and a `TEST_REGRESSION`, a `HARNESS_ERROR`+auto-retry, a `SCOPE_VIOLATION`, a `DIRTY_WORKTREE`) produces **identical** chunk status, commit, gate, and stored report to `main` before this PR. Capture these as snapshots from the pre-refactor code.

## 10.4 Traps

- **(a) Rewriting the pure stages instead of wrapping them.** That balloons the diff and breaks their existing tests for no behavior gain. Adapt at the boundary.
- **(b) Letting the outcome class become a second source of truth for retryability.** In item 10 the class is *carried*, not yet *acted on* — the existing frozensets in `patch_failures.py` still drive behavior. Wiring policy to read the class is item 11. Don't half-wire it here.
- **(c) Changing the stored report shape** to fit the new type — breaks historical `completion_summary` parsing (the same back-compat trap item 8 hit). The `StageOutcome` *wraps* the existing report; it does not replace it.

## 10.5 Safety-contract check (item 10)

- All nine invariants **untouched** — this PR adds a type and adapters; control flow, gates, scope authority, commit/rollback are byte-identical. State that explicitly, and let the golden tests prove it.

---

# ITEM 11 — The driver replaces `_execute_single_chunk` internals (`fresh` + `resume`); rollback moves out of `tester.py`

## 11.1 Summary & scope

Introduce **one driver** that iterates the ordered stage list and owns the cross-cutting concerns, replacing the *internals* of `_execute_single_chunk` (the public function and its signature stay). The driver collapses the **three** current copies of the stage sequence into one loop. In this PR it serves exactly two entry modes — `fresh` and `resume` — and the bounded `INFRA_ERROR` auto-retry. `human_retry` and `steered` are item 12 / Phase 3.

**The driver owns (proposal §4.1):** dependency + dirty-tree preconditions; checkpoint write-and-verify; the `INFRA_ERROR` auto-retry loop (budget from `policy.AUTO_RETRY_INFRA_BUDGET`, `TIMEOUT` excluded — exactly item 8's rule, now in one place); pause/gate returns; **rollback-to-clean on any failed attempt**; verdict persistence for both pass and fail.

**Invariants the driver enforces identically in every mode** (this is what kills the §1.2d divergence): scope pre-check before any write, dry-run before apply, no commit without effective change, rollback to clean tree on any failed attempt, verdict persisted for pass *and* fail.

**The one genuine behavior-location change — `tester.py` loses its rollback side-effect (T2).** Today `tester.py` rolls back the patch on a failed test run; the orchestrator then *verifies* cleanliness and avoids a double rollback. Move the rollback into the driver: stages produce verdicts; the **driver** decides remediation from the outcome class. **Rollback semantics are unchanged — same trigger, same result — only the call site moves to the one place that already makes that decision in the other two paths.** This is the single highest-risk hunk in all of Phase 2; treat it as such.

**In scope (exact files):**
- A new `backend/pipeline/chunk_driver.py` (or similar) holding the driver loop and the entry-mode dispatch.
- `chunked_orchestrator.py`: `_execute_single_chunk` becomes a thin `fresh`-mode call into the driver; `_resume_chunked_pipeline_locked` (`:2208`) skip-logic becomes the `resume` mode (keep the `_verify_completed_checkpoint_safe` discipline verbatim). `execute_approved_chunks` (`:2202`) and `resume_chunked_pipeline` (`:2344`) keep their signatures.
- `tester.py`: remove the rollback side-effect; it returns a verdict only. The driver performs the rollback.
- The inline auto-retry block in `_execute_single_chunk` is **deleted** and re-expressed as the driver's single auto-retry loop (net code reduction).

**Explicitly out of scope:** `human_retry`/`steered` modes, the attempt ledger, P7 (all item 12); any taxonomy or status rename; the reviewer ack gate (Phase 4).

## 11.2 Entry modes in this PR (proposal §4.1 table)

| Mode | Replaces | Behavior in this PR |
|---|---|---|
| `fresh` | `_execute_single_chunk` body | All stages, top to bottom. |
| `resume` | `_resume_chunked_pipeline_locked` skip-logic | Skip stages whose checkpoint is verified by `_verify_completed_checkpoint_safe`; never skip on an unverified checkpoint. |
| `auto_retry` | the inline `HARNESS_ERROR` retry | Internal to the driver: re-run from the `code` stage, bounded by `AUTO_RETRY_INFRA_BUDGET`, `INFRA_ERROR` only, `TIMEOUT` excluded, clean-tree asserted. |

Leave a *named seam* for `human_retry` and `steered` (e.g. the mode enum has the values, dispatch raises `NotImplementedError` for them) so item 12 / Phase 3 plug in without reshaping the driver — but do not implement them.

### 11.2a Two landmines in item 10's inert outcome-class map (review finding, 2026-06-11)

Item 10 added `OutcomeClass` and a total `PatchFailureType→OutcomeClass` map (`stage_contract.outcome_class_for_failure`). The class is **inert** in item 10 (carried, never acted on). When item 11 starts *acting* on it, two mappings will silently change behavior if you trigger off the class alone — and **no golden catches either** (no scenario exercises these failure types):

1. **Auto-retry must NOT trigger on `OutcomeClass.INFRA_ERROR` alone.** `UNKNOWN_PATCH_FAILURE → INFRA_ERROR`, but today it arises in the **apply/dry-run** phase and is *never* auto-retried — auto-retry fires only in the verify/test branch, gated on `HARNESS_ERROR` + `integrity != TIMEOUT`. Keep the today-equivalent gate (**stage == `verify` AND `integrity != TIMEOUT`**), not `outcome_class is INFRA_ERROR`. Add a golden for an apply-phase `UNKNOWN_PATCH_FAILURE` proving **zero** auto-retries.
2. **`CODE_REJECTED` is coarser than human-retry eligibility.** `PATCH_DOES_NOT_APPLY` / `TARGET_MISSING` / `PATCH_PARTIAL_APPLY_BLOCKED` are human-retryable today; `TEST_REGRESSION` / `NO_CHANGES` are not — yet all map to `CODE_REJECTED`. The class is a narrative/coarse signal, **never** the authority for retryability. Item 12's `human_retry` eligibility must keep deriving from the failure-type-level frozensets in `patch_failures.py` (as §12.1 already requires), not collapse onto the class.

## 11.3 Tests that must exist and pass (the contract)

- **Golden fresh path:** success, `TEST_REGRESSION`, `HARNESS_ERROR`+auto-retry-then-pass, `HARNESS_ERROR`×2→human path, `TIMEOUT`→no-retry, `SCOPE_VIOLATION`, `FORBIDDEN_FILE`, `DIRTY_WORKTREE`, `NO_CHANGES`, baseline-disclosure, baseline-infra-pause-before-LLM — each produces **identical** observable results to pre-PR `main` (status, commit SHA presence, gate, stored report, invocation counts). These are the same snapshots item 10 captured, now run through the driver.
- **Auto-retry is exactly once and only for infra:** assert `code→apply→test` runs exactly twice on `HARNESS_ERROR`, exactly once on `TEST_REGRESSION`/`SCOPE_VIOLATION`/`TIMEOUT`; auto attempts recorded `recovery_mode="auto"` and do **not** consume the human budget.
- **Rollback-move equivalence (the dangerous one):** with `tester.py` no longer rolling back, the driver leaves the tree **clean** after every failed attempt — assert `is_working_tree_clean` is true post-failure for each failure class, and that no double-rollback occurs (count rollback invocations = exactly one per failed attempt). Add a test that would have caught a *missing* rollback (tree left dirty) and one that would have caught a *double* rollback.
- **Resume parity:** a run resumed mid-chunk skips exactly the verified-checkpoint stages and no others; an unverified/missing checkpoint causes re-execution, never a skip (fail-closed preserved).
- **No-op commit guard preserved:** an effective-no-change result still refuses to commit.

## 11.4 Approach (sequence it)

1. Land the driver loop calling item 10's stages for `fresh` mode only; prove golden parity; keep `_execute_single_chunk` as the thin entry. 2. Move rollback from `tester.py` into the driver; prove the rollback-move equivalence tests. 3. Fold `resume` skip-logic into the driver as the `resume` mode, reusing `_verify_completed_checkpoint_safe`. 4. Delete the inline auto-retry; prove auto-retry tests still pass against the driver loop. Do **not** touch `_execute_retry_attempt` yet (item 12).

## 11.5 Safety-contract check (item 11)

- **§2.2 (scope):** `scope_guard` is unchanged; the driver calls it in the `preflight` stage before any write, in every mode. Prove the pre-write scope check fires identically.
- **§2.3 (no no-op commit) + rollback:** the no-op guard stays in the commit stage; rollback **moves** call sites but keeps trigger and result — backed by the rollback-move equivalence tests. This is the hunk most likely to regress safety; if you cannot prove equivalence, **stop and escalate** rather than ship.
- **§2.1 (autonomous work):** the auto-retry budget and `INFRA_ERROR`-only scope are unchanged from §5.2 — just relocated. Never broaden the trigger.
- **Resume fail-closed:** `_verify_completed_checkpoint_safe` discipline preserved verbatim; never skip on an unverified checkpoint.

---

# ITEM 12 — Collapse `human_retry` into the driver; delete `_execute_retry_attempt`; attempt ledger; P7

## 12.1 Summary & scope

Make `human_retry` a driver entry mode, **delete the duplicated `_execute_retry_attempt`** (`:2885`), land the **attempt ledger** (additive schema), and — now that the ledger records per-chunk HEADs — fold in **P7** (the resume branch-HEAD drift check that was deferred from Phase 0 for exactly this reason).

**In scope (exact files):**
- `chunked_orchestrator.py`: `_execute_retry_attempt` deleted; `_retry_failed_chunk_locked` (`:3042`) re-expressed as a `human_retry` driver call (re-run from the `code` stage with the same approved plan). `retry_failed_chunk` (`:3141`) keeps its signature. Existing eligibility evaluation (`evaluate_patch_retry_eligibility`, `count_human_retry_attempts`) carries over, re-expressed over outcome classes — **preserve today's deliberate exclusions** (`TEST_REGRESSION` not human-retryable in Phase 1; `HARNESS_ERROR` is; `SCOPE_VIOLATION`/`FORBIDDEN_FILE` never).
- **Attempt ledger — additive only.** A new `chunk_attempts` table (one row per driver pass over a chunk): `entry_mode` (`fresh`/`resume`/`auto_retry`/`human_retry`), per-stage outcome classes, evidence refs, final outcome class, and the **git HEAD SHA at attempt end**. Follow the existing additive-migration pattern: `CREATE TABLE IF NOT EXISTS` in `backend/db/schema.sql` and a guarded block in `backend/db/database.py:_migrate_db` (`:120`). Append-only; never rewrite historical rows. This extends `PatchFailureReport.attempts` / `PatchRecoveryAttempt` (`patch_failures.py:285`) and the per-step checkpoint store — unify, don't fork.
- **P7 (resume HEAD-drift):** with per-chunk HEAD recorded, add to `resume` an **exact-SHA** check that the branch HEAD matches the last recorded chunk HEAD; on mismatch (foreign commits stacked on top) fail closed with a clear narrative. Resume already fails closed on missing commits (`_verify_completed_checkpoint_safe`), dirty tree, and missing branch — P7 closes only the remaining "foreign commits" gap, now without false positives (the reason it was deferred).

**Explicitly out of scope:** `steered` mode / real `retry_with_instruction` execution (Phase 3, §5.3 pending); reviewer ack gate (Phase 4, §5.4 pending); the user-facing phase/narrative read-model (Phase 4).

## 12.2 Verified current behavior (re-confirm before editing)

- `_execute_retry_attempt` (`:2885`) is the hand-maintained second copy of the stage sequence — it carries `dry_run_changes` and `_surface_files_expected_for_edit` (the asymmetries §1.2d names). After item 11 the driver already does both uniformly, so the copy is pure redundancy to delete.
- `checkpoints` table already has a `git_commit_hash` column (`schema.sql:109`), but per the workplan `_commit_and_complete_chunk` (`:755`) does not reliably record a per-chunk SHA usable for an exact resume check — confirm and let the new ledger be the authoritative per-attempt HEAD source for P7.
- `count_human_retry_attempts` already excludes `recovery_mode="auto"`, so auto attempts correctly never consume the human budget — preserve this when the ledger becomes the attempt source.

## 12.3 Tests that must exist and pass

- **`human_retry` parity:** a human retry through the driver produces the same status/commit/gate/report as `_execute_retry_attempt` did pre-deletion (snapshot first). The dry-run + `files_expected`-surfacing the old retry path had are now present (they come from the driver) — assert they fire.
- **Eligibility preserved:** `HARNESS_ERROR` eligible; `TEST_REGRESSION` not (Phase 1 rule); `SCOPE_VIOLATION`/`FORBIDDEN_FILE` never; human budget respected; auto attempts excluded from the human count.
- **Ledger is additive + append-only:** migration runs idempotently on an existing DB (no data loss); every driver pass writes exactly one append-only `chunk_attempts` row with the correct `entry_mode` and end HEAD; historical rows are never mutated.
- **P7 fail-closed, no false positive:** resume with a foreign commit stacked on the branch → fails closed with a clear narrative; resume with the branch exactly where the last attempt left it → proceeds (no false-positive dead-end — the failure mode that got P7 deferred).
- **Migration round-trip:** a DB created before this PR migrates cleanly; a stored chunk with no ledger rows still resumes/retries.

## 12.4 Traps

- **(a) Deleting `_execute_retry_attempt` before the driver covers everything it did.** It carries dry-run + `files_expected` surfacing; only delete once item 11's driver provides both and the parity snapshot proves it. Do item 11 first.
- **(b) A non-additive migration.** Never `DROP`/rewrite; `CREATE TABLE IF NOT EXISTS` + guarded `ALTER`. A failed migration on a live DB is a data-loss incident.
- **(c) P7 false positives on legitimate resume.** The whole reason P7 waited for the ledger is that a heuristic (no recorded SHA) dead-ends valid resumes. Use the **exact recorded HEAD**; if no HEAD was recorded for a chunk (pre-ledger run), **degrade to today's behavior** (the existing fail-closed checks), do not invent a block.
- **(d) Letting the ledger become an authority channel.** It is an append-only audit record; it must never grant scope, approval, or retry budget that policy/gates don't already grant.

## 12.5 Safety-contract check (item 12)

- **§2.1 / §2.2 / §2.3:** unchanged — `human_retry` runs the same approved plan inside the same `files_expected`, through the same driver invariants. Eligibility exclusions preserved.
- **Resume safety strengthened, not weakened:** P7 adds a fail-closed check; it can only *block* a risky resume, never permit one that today's checks would block. Prove the no-false-positive case.
- **Schema:** additive + idempotent; append-only; no historical rewrite.

---

## 3. Update these docs when you finish (part of "done")

1. **`PIPEWRIGHT_REDESIGN_WORKPLAN.md`** — the canonical resume point. When each item lands + tests pass: mark it done in the Phase 2 sequence and the "How to resume" steps; update the TL;DR "Where we are" bullet. When **all of 10–12 + P7** land, state plainly that **Phase 2 is complete** and the next line is **Phase 3 (continuation / steer turns) — which needs the §5.3 decision first.**
2. **This file (`PIPEWRIGHT_REDESIGN_IMPL_BRIEF.md`)** is the rolling brief: once Phase 2 is fully done and reviewed, repurpose it for **Phase 3** (continuation), the same way it was repurposed from Phase 1 to Phase 2.
3. **A short per-item spec/changelog note** if you follow the repo's `specs/` convention, mirroring the earlier items. Optional but matches the pattern.
4. These planning docs are **untracked**. Update their content; **do not commit** them (or any code) unless the maintainer asks. If asked to commit: branch off `develop` first (never straight to `develop`/`main`), one item per commit, end the message with the repo's `Co-Authored-By` trailer.

## 4. Working discipline (every PR)

- Read the real code first; trace the actual path; never design against this brief if the live code has drifted — correct the brief's pointer and say so.
- **Capture golden snapshots of today's behavior before you refactor.** A behavior-preserving refactor is only provable against a baseline taken *before* the change.
- Smallest correct change; one item per PR, serialized 10 → 11 → 12; list what you deliberately did **not** change.
- Match surrounding naming, structure, and comment density.
- Tests assert the **changed structure preserves behavior** (and the safety guards above), not just that code runs.
- Report on completion: changed files, tests run + results, manual validation, risks, and what was intentionally left untouched.
- **This is the CRITICAL BOUNDARY.** A human reviews each PR before the next. Do not let momentum carry the loop across item boundaries unreviewed, and do not start Phase 3 (it needs the §5.3 decision).
