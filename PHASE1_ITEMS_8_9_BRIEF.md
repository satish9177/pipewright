# Pipewright Redesign — Phase 1, Items 8 & 9 — Implementation Handoff Brief

**Date:** 2026-06-11
**For:** the model implementing the rest of Phase 1 (item 7 already landed). You work **without the author in the loop** — this brief is self-contained. Re-verify every `file:line` against the live code before you cite or edit it; the repo moves.
**Source of record:** `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (§4.2, §4.4, §5, §6) and `PIPEWRIGHT_REDESIGN_WORKPLAN.md`. This brief operationalizes them; if they disagree, the proposal wins and you flag the drift.
**Mode:** design + implement + test, one item per PR. Item 8 first, then item 9. Do **not** start Phase 2.

---

## 0. The decisions already made (do not re-litigate)

The two §5 decisions that gate Phase 1 were ruled on by the maintainer on 2026-06-11:

- **§5.1 → accept the regression-vs-baseline verification gate, AND add scoped-verification as a policy option, default OFF.** (Gates item 9.)
- **§5.2 → auto-retry `INFRA_ERROR` with budget = 1, then pause for a human.** `INFRA_ERROR` class only — never code or scope failures. (Gates item 8.)

§5.3 and §5.4 are still PENDING and gate Phases 3 and 4 — **not** Phase 1. Do not implement steer execution or the reviewer ack gate.

## 1. Non-negotiable safety contract (from `CLAUDE.md`)

No change may weaken these (enforced in `scope_guard`, the approval gates, `patch_applier`, `path_safety`):

1. No implementation without an approved chunk plan; never bypass chunk-plan or final-approval gates.
2. Never edit outside approved `files_expected`; `scope_guard` is the authority.
3. Never create empty / no-effective-change commits; never push zero-commit branches.
4. Never open PRs against `main`/`master`/`develop`; never auto-merge.
5. Never write forbidden paths (`.env`, `.git/`, secrets, keys).
6. Never expose or persist secrets/tokens/PII; sanitize provider/Git errors.
7. Memory is advisory; source code, user instruction, tests, and safety rules win on conflict.
8. AI-suggested memory stays pending until a human approves.
9. Prefer failing safely with a clear, specific error over guessing.

Both items below touch **safety-adjacent** code (retryability policy, autonomous re-execution, the definition of "tests passed"). Call out every interaction explicitly and design conservatively. When in doubt, fail safe with a clear error.

## 2. What item 7 already landed (your foundation — do not re-do)

`backend/pipeline/test_run_validation.py` now exports a **pure** Signal-C classifier you will consume:

```python
class ExecutionIntegrity(str, Enum):
    OK, TIMEOUT, HARNESS_CRASH, COMMAND_NOT_FOUND, SIGNAL_KILL, COLLECTION_ERROR, NO_TESTS_RAN

def classify_execution_integrity(
    exit_code: int | None, output: str | None, *, timed_out: bool = False
) -> ExecutionIntegrity: ...
```

`OK` ⇒ the harness ran and the result is a real code pass/fail. Any other value ⇒ the world broke. It is orthogonal to the existing STRONG/WEAK/NONE/UNKNOWN verdict (`classify_test_run`), which is unchanged. Precedence is fixed; exactly one value returns. Tests live in `backend/tests/test_test_run_validation.py` (Signal C section). **Do not modify Signal C's behavior** — only consume it.

---

# ITEM 8 — Split `TEST_FAILURE_AFTER_APPLY` → `TEST_REGRESSION` / `HARNESS_ERROR`; auto-retry `HARNESS_ERROR` (budget 1); first narrative slice

## 8.1 Summary & scope

Today every post-apply test failure — a real assertion regression, a Windows interpreter crash, a missing test runner, a 0-test collection — collapses into the single `TEST_FAILURE_AFTER_APPLY` type and dead-ends (it is excluded from human retry; see `patch_failures.py:208-214` and proposal §1.2b). Split it by Signal C: a genuine code failure becomes `TEST_REGRESSION`; an infrastructure failure becomes `HARNESS_ERROR`. `HARNESS_ERROR` auto-retries once and then pauses for a human with a clear narrative; `TEST_REGRESSION` rolls back as today but is no longer mislabeled.

**In scope (exact files):**
- `backend/models/handoff.py` — add `exit_code` + `timed_out` to `PipelineTestResult` (the wiring gap; see §8.3).
- `backend/pipeline/tester.py` — populate those two fields on every `PipelineTestResult` it returns (success, fail, timeout). **Do not** otherwise change tester behavior; rollback stays where it is (Phase 2 moves it).
- `backend/pipeline/patch_failures.py` — add the two enum members; re-derive the retryability frozensets; add default messages.
- `backend/pipeline/chunked_orchestrator.py` — at the test-failure site (`:1496-1510`, main path) choose the failure type by Signal C and run the bounded `HARNESS_ERROR` auto-retry (§8.4). Mirror the type choice at the retry-path site (`:2388-2396`) but **without** adding a second auto-retry loop there (see trap 8.7c).
- `backend/pipeline/operator_state.py` — map the two new types to user-facing families (`:703-715`).
- `backend/memory/run_outcome_suggestions.py` — the `TEST_FAILURE_AFTER_APPLY` suggestion template (`:96`) maps to the new types.
- The narrative slice (T4): structured what-happened / why / what-next copy for `TEST_REGRESSION` and `HARNESS_ERROR`. Reuse the existing read-model in `operator_state.py` — do **not** invent a parallel narrative system (the full read-model is Phase 4 item 16).

**Explicitly out of scope (name these in your PR):**
- Removing `TEST_FAILURE_AFTER_APPLY` from the enum — it MUST stay for backward-compat (§8.7a).
- Moving rollback out of `tester.py` — Phase 2.
- `retry_with_instruction` / steer execution for `TEST_REGRESSION` — Phase 3 (§5.3 pending). In Phase 1 a regression surfaces with a clear narrative and the *existing* human options only.
- Baseline-aware verification — that's item 9.
- The driver/strangler refactor — Phase 2.

## 8.2 Verified current behavior (re-confirm before editing)

- `PatchFailureType` is a closed enum; `TEST_FAILURE_AFTER_APPLY` at `patch_failures.py:151`.
- `_RETRYABLE_TRANSIENT` (`:159-168`) contains `TEST_FAILURE_AFTER_APPLY`; `_HUMAN_RETRYABLE_FAILURE_TYPES` (`:208-214`) deliberately **excludes** it — this is the dead-end.
- Production sites of the type:
  - `chunked_orchestrator.py:1500-1509` — main path, after `if not test_result.passed:` (`:1496`). Builds the report with `failed_step="test"`, `technical_details=test_result.output`.
  - `chunked_orchestrator.py:2388-2396` — human-retry path, same shape.
  - `patch_applier.py:503` — `classify_patch_failure(phase="test")` returns it (a helper).
- `PipelineTestResult` (tester.py `:174-215`, `:231-239`) carries `passed`, counts, `output`, `duration` — **no exit code, no timeout flag**. `_persist_test_run_verdict` (`chunked_orchestrator.py:1305-1309`) currently passes a *synthetic* `0/1` to `classify_test_run`. That synthetic value is too lossy for Signal C (it can't see 127/9009/137/timeout).

## 8.3 Approach — close the wiring gap first

Signal C needs the **real** exit code and a timeout flag. So:

1. Add to `PipelineTestResult`: `exit_code: int | None = None` and `timed_out: bool = False` (defaulted, so historical serialized results still parse — invariant-adjacent, keep defaults).
2. In `tester.py`, set `exit_code=completed.returncode` on the normal branches and `timed_out=True` (and `exit_code=None`) on the `TimeoutExpired` branch (`:217-239`).
3. At the orchestrator test-failure site, compute `integrity = classify_execution_integrity(test_result.exit_code, test_result.output, timed_out=test_result.timed_out)` and choose:
   - `ExecutionIntegrity.OK` → `TEST_REGRESSION` (the code is wrong).
   - anything else → `HARNESS_ERROR` (the world broke). **Exception:** `ExecutionIntegrity.TIMEOUT` must **not** be auto-retried (re-running an infinite loop helps nobody — proposal §4.2). Map it to `HARNESS_ERROR` for the *type*, but exclude `TIMEOUT` from the auto-retry trigger (§8.4). Its narrative explains both possibilities (slow suite vs. coder-introduced loop).
4. Also update the display-only `_persist_test_run_verdict` to pass the real `exit_code` now that it exists (quality win, no behavior change to the verdict — `classify_test_run` only distinguishes zero/non-zero).

Rejected alternative: classifying from `output` alone (no exit code) — loses `COMMAND_NOT_FOUND`/`SIGNAL_KILL`/`TIMEOUT`, the cases that most need infra handling. Rejected.

## 8.4 The auto-retry, precisely (the safety-critical part)

Per §5.2: bounded auto-retry **re-runs the chunk's code → apply → test** on the already-approved plan. The maintainer's ruling is **budget = 1**.

Exact semantics — encode these as the contract:

- **Trigger:** the test failure classifies as `HARNESS_ERROR` **and** the integrity value is **not** `TIMEOUT`, **and** zero auto-retries have been spent on this chunk. Nothing else triggers it — never `TEST_REGRESSION`, never `SCOPE_VIOLATION`/`FORBIDDEN_FILE`, never an apply failure.
- **Budget:** exactly 1 auto-retry (one extra code→apply→test pass). Put the `1` in `backend/pipeline/policy.py` as a named constant (e.g. `AUTO_RETRY_INFRA_BUDGET = 1`) — no buried literal (CLAUDE.md: policy, not magic numbers).
- **Precondition each attempt:** the working tree is clean. `tester.py` already rolled back on the failed run, so the precondition holds — assert/verify it (`local_git.is_working_tree_clean`) rather than assume; if it's dirty, do **not** retry — fail with the report.
- **On the retry still being `HARNESS_ERROR`:** stop. Build the `HARNESS_ERROR` report and fail the chunk into the *human* path with a narrative ("the test process crashed before any test ran; I retried once, it crashed again; you can retry or fix your environment"). `HARNESS_ERROR` must be in `_HUMAN_RETRYABLE_FAILURE_TYPES` so the human isn't dead-ended (this is the un-dead-ending the proposal asks for).
- **On the retry succeeding:** proceed exactly as a normal pass (reviewer → gate/commit). Record the win.
- **Record every pass:** append a `PatchRecoveryAttempt` with `recovery_mode="auto"` (the model already supports this — `patch_failures.py:291-293`; `count_human_retry_attempts` already excludes `"auto"`, so auto attempts correctly do not consume the human budget).
- **Lock note:** this runs inside `project_repo_lock`. One bounded extra pass is fine (unlike the deleted 60s sleeps). Do not sleep.

**Frozenset re-derivation (preserve today's deliberate exclusions):**
- `HARNESS_ERROR`: auto-retryable (budget 1) **and** human-retryable → add to `_RETRYABLE_TRANSIENT` and to `_HUMAN_RETRYABLE_FAILURE_TYPES`.
- `TEST_REGRESSION`: a real code failure. **Not** auto-retried. Steerable only — but steer execution is Phase 3, so in Phase 1 do **not** add it to `_HUMAN_RETRYABLE_FAILURE_TYPES` (human-retrying the same plan reproduces the same regression — pointless and misleading). It keeps the existing rollback + report behavior, just correctly labeled. (If you believe it belongs in `_RETRY_WITH_INSTRUCTION_TYPES` for UI affordance, that set already implies a steer path that doesn't execute yet — leave it out until Phase 3 and say so.)
- `SCOPE_VIOLATION`, `FORBIDDEN_FILE`: unchanged. Never auto-retried.
- Keep `TEST_FAILURE_AFTER_APPLY` in `_RETRYABLE_TRANSIENT`/messages so historical reports still render coherently.

## 8.5 Tests that must exist and pass (the contract)

Put pure-taxonomy tests near the existing `patch_failures` tests; orchestrator tests with the existing orchestrator tests (find them, match markers — `pytest.mark.unit`). Concrete required assertions:

- **Anchor (type split):** given a `PipelineTestResult(passed=False)`, output `"1 failed, 4 passed"`, `exit_code=1`, `timed_out=False` → the produced report's `failure_type is TEST_REGRESSION`. Given output containing `"Fatal Python error: init_sys_streams"` → `HARNESS_ERROR`. Given `exit_code=9009` → `HARNESS_ERROR`.
- **Auto-retry budget (the dangerous one):** a `HARNESS_ERROR` on attempt 1 then `OK` on the retry → chunk proceeds (one auto attempt recorded, `recovery_mode="auto"`). A `HARNESS_ERROR` on both attempts → chunk fails to the **human** path (not dead-ended), exactly **one** auto-retry was performed (assert the coder/apply/test was invoked exactly twice — use a counting fake/mock), `manual_intervention` narrative present.
- **Never-auto-retry guards:** a `TEST_REGRESSION` → **zero** auto-retries (assert code→apply→test ran exactly once). A `SCOPE_VIOLATION` and a `FORBIDDEN_FILE` → zero auto-retries. A `TIMEOUT` integrity → typed `HARNESS_ERROR` but **zero** auto-retries.
- **Clean-tree precondition:** if the tree is dirty when the auto-retry would fire, it does not retry and fails with the report.
- **Backward-compat regression guard:** a stored `completion_summary` JSON with `failure_type: "TEST_FAILURE_AFTER_APPLY"` still parses via `patch_failure_report_from_completion_summary` (returns a report, not `None`), and `operator_state` still maps it to the test-failure family.
- **Human un-dead-ending:** `evaluate_patch_retry_eligibility` now returns `eligible=True` for a `HARNESS_ERROR` report (other preconditions met) where `TEST_FAILURE_AFTER_APPLY` returned `disallowed_failure_type`.

## 8.6 Verification commands (Windows + PowerShell)

```powershell
python -m pytest backend/tests/test_patch_failures.py -q
python -m pytest backend/tests/test_chunked_orchestrator.py -q   # or whichever file owns the orchestrator tests
python -m pytest backend/tests -q -m unit
ruff check
```
(Never `ruff format`.)

## 8.7 Where it can go wrong (traps)

- **(a) Removing the old enum member.** Do NOT. Historical `chunks.completion_summary` rows contain `"TEST_FAILURE_AFTER_APPLY"`; `patch_failure_report_from_completion_summary` validates against the enum, so removal silently turns every old failed chunk into an unparseable `None`. Keep the member; stop producing it.
- **(b) Re-running just the tests instead of code→apply→test.** Tempting (cheaper), but after a failure `tester.py` already rolled the patch back — re-running tests alone would test an empty tree. §5.2 explicitly says re-run code→apply→test. The cheaper "re-run tests only" shape needs rollback to move out of tester, which is Phase 2. Do the full re-run now.
- **(c) Duplicating the auto-retry into the human-retry path.** `_execute_retry_attempt` (`:2251-2391`) is a hand-maintained copy of the main path (proposal §1.2d). Add the *type choice* there, but the auto-retry loop belongs to fresh execution only — a human-initiated retry should not also silently auto-retry. Adding it twice deepens the divergence Phase 2 exists to kill.
- **(d) Counting auto attempts against the human cap.** They must not. `count_human_retry_attempts` already excludes `"auto"` — use `recovery_mode="auto"` and you're correct by construction. Add a test that proves it.
- **(e) False-confidence tests.** A test that builds a report directly never exercises the orchestrator's Signal-C branch or the retry loop. At least one test must drive the real orchestrator path with a fake tester that returns crash-vs-regression results, and assert invocation counts.
- **(f) TIMEOUT auto-retried.** The single most expensive mistake — re-running an infinite loop. Guard explicitly and test it.

## 8.8 Safety-contract check (item 8)

- Invariant **§2.1 (approved-plan / autonomous work):** the auto-retry runs implementation work without a fresh human trigger. This is the §5.2 tension the maintainer accepted with **budget = 1**, `INFRA_ERROR`-only, ledger-visible. Stay inside that grant: never auto-retry code/scope failures, never exceed 1.
- Invariant **§2.3 (no no-op commits)** and rollback semantics: unchanged — tester still rolls back; you only relabel and bound-retry. The clean-tree precondition is asserted, not assumed.
- All others: untouched. Say so.

---

# ITEM 9 — Baseline-aware verification + scoped mode (default OFF)

## 9.1 Summary & scope

Today a chunk commit requires the whole suite green (`tester.py:163`, `passed = returncode == 0`), so a single pre-existing red test fails every chunk on a red-baseline repo. Per §5.1: record a **baseline** of failing tests at execution start, and gate each chunk on *newly* failing tests only; pre-existing failures are disclosed, never charged to the chunk. The full suite still runs before final approval. Add **scoped verification** (run only impacted tests per chunk) as a policy option, **default OFF**.

**In scope:** `backend/pipeline/tester.py` (or a small new `verification` helper it calls), the run-start baseline run (before chunk 1, post-approval, on the run branch), baseline storage per run, the newly-failing-vs-baseline comparison, disclosure in approval/final summaries, and the scoped-mode policy knob in `policy.py` (default off). Reuse item 7's `classify_execution_integrity`: **a baseline run that is itself `INFRA_ERROR` pauses the run with a "your test environment is broken" narrative *before any LLM spend*** (proposal §4.4 — this turns the most expensive failure mode into the cheapest).

**Explicitly out of scope:** scoped-mode *enabled* by default (it ships off); an import-graph impacted-test selector (later refinement — Phase-1 scoped mode uses test files in `files_expected` + naming-convention matches only); moving rollback (Phase 2); the full narrative read-model (Phase 4).

## 9.2 The hard part — you need failing-test *identities*, not counts

"Newly failing vs. baseline" requires a set of failing **test IDs** (e.g. `tests/test_app.py::test_add`), not just the `N failed` count the current parser produces (`tester.py:78-99`). Design a deterministic per-runner failing-ID extractor (start with pytest's `FAILED <nodeid>` lines; degrade safely when IDs aren't parseable). When IDs cannot be parsed, **fail safe**: fall back to the current whole-suite-green gate for that run rather than silently passing a real regression — and disclose that the baseline was non-comparable. Never let "couldn't parse" read as "no new failures."

## 9.3 Approach (sequence it; this item is larger than item 8 — consider splitting the PR)

1. Run-start baseline: once, after approval, on the run branch, before chunk 1. Classify it with Signal C; if `INFRA_ERROR`, pause with narrative (no chunk work). Record the baseline failing-ID set on the run.
2. Chunk gate: after each chunk's test run, compute `newly_failing = current_failing_ids - baseline_failing_ids`. Non-empty → the chunk's outcome is a regression (route through item 8's `TEST_REGRESSION`). Empty → pass, even if pre-existing reds remain; **disclose** the pre-existing set in the summary.
3. Roll the baseline forward: after a committed chunk, its green-or-disclosed result becomes the next chunk's baseline (zero extra runs after the first — proposal §4.4).
4. Run gate: the full suite still runs before final approval (the last chunk's verification), with the baseline delta in the final summary. The #28F weak/none ack gate is unchanged.
5. Scoped mode (policy, default off): when enabled (threshold on measured suite duration), the chunk gate runs the deterministic impacted subset; the full suite still runs at least once before final approval. Honest disclosure: "scoped subset green; full suite deferred to final gate."

## 9.4 Tests that must exist and pass

- **Anchor:** baseline `{test_app.py::test_old}` red; a chunk leaves only `test_old` red → chunk **passes**, summary discloses 1 pre-existing failure. Same baseline; a chunk additionally reds `test_new` → chunk **fails** as a regression on `test_new` only.
- **Baseline-is-infra:** baseline run classifies `HARNESS_ERROR`/`COMMAND_NOT_FOUND` → run pauses with the env narrative; **assert no planner/coder LLM call happened** (use a mock that fails if called).
- **Unparseable IDs fail safe:** a runner whose failures can't be parsed into IDs → falls back to whole-suite-green gate (a red suite fails the chunk) and discloses non-comparability. Never passes a red suite silently.
- **Scoped mode default:** with no project override, the gate runs the full suite (scoped mode is OFF). A test flipping the policy knob runs only the impacted subset and still requires a full-suite run before final approval.
- **Regression guard:** a green-baseline repo behaves exactly as today (full suite green required, nothing disclosed).

## 9.5 Safety-contract check (item 9)

- Invariant **§2.3-spirit / §2.9 ("tests passed"):** this redefines the chunk gate from "whole suite green" to "no new failures, pre-existing disclosed." The maintainer accepted this (§5.1) *with disclosure in every approval summary and the final gate*. The disclosure is not optional — it is the thing that makes the redefinition honest. Build it as a first-class part of the summary, not an afterthought.
- Scoped mode ships **off**; never make it the default without a new decision.
- Fail-safe on unparseable baselines is a safety requirement, not a nicety.

---

## 3. Update these docs when you finish (do this — the author did it for item 7)

Treat the docs as part of "done," exactly as item 7 was handed off:

1. **`PIPEWRIGHT_REDESIGN_WORKPLAN.md`** — the canonical resume point. When an item lands + its tests pass:
   - Mark it done in the **"Phase 1 sequence"** line of *How to resume* (item 7 was changed from "(in progress)" to "✅ done"; do the same for 8, then 9).
   - Update the **TL;DR "Where we are"** bullet to state the new resume point.
   - When **all of 8 and 9** are done, state plainly that **Phase 1 is complete** and that the next line is **Phase 2 (the `_execute_single_chunk` strangler refactor) — the CRITICAL BOUNDARY that needs the human + strong-model review gate.** Do not cross it.
2. **A short per-item spec/changelog note** if you follow the repo's `specs/` convention (e.g. `specs/item8-infra-split.md`), mirroring `specs/E2-stdin-devnull.md`. Optional but matches the established pattern.
3. These planning docs are **untracked** (not committed). Update their content; **do not commit** them (or any code) unless the maintainer asks. If asked to commit: branch off `develop` first (never commit straight to `develop`/`main`), one item per commit, and end the commit message with the `Co-Authored-By` trailer the repo uses.
4. If your harness has persistent memory, the §5.1/§5.2 decisions and Phase-1 status are the durable facts worth carrying. (Claude Code mirrors them in its own memory; the workplan is the harness-agnostic source of truth.)

## 4. Working discipline (every PR)

- Read the real code first; trace the actual path; never design against this brief if the live code has drifted — correct the brief's pointer and say so.
- Smallest correct change; one item per PR; list what you deliberately did **not** change.
- Match surrounding naming, structure, and comment density.
- Tests assert the **changed behavior** (and the safety guards above), not just that code runs.
- Report on completion: changed files, tests run + results, manual validation, risks, and what was intentionally left untouched.
- After Phase 1, **stop.** Phase 2 is the strangler refactor of the apply/commit/rollback core — it needs a human review gate, not unsupervised momentum.
