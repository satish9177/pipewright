# Patch Failure Recovery v2 Design

> Status: **Design only (#26A).** This document defines the long-term design for
> safe recovery from `PATCH_DOES_NOT_APPLY` and related structured patch
> failures. No runtime code, test, frontend, or schema change ships with #26A.
> Implementation is phased in #26B–#26G (see *Phased Implementation Plan*).

---

## Purpose

- This document defines the long-term design for **safe recovery from
  `PATCH_DOES_NOT_APPLY`** (and the related structured patch-failure family) in
  Pipewright's chunked execution path.
- It is **design-only**. Nothing here changes runtime behavior, tests, the
  frontend, checkpoints, rollback, commit, dependency, memory, or scope logic.
- Runtime implementation is **intentionally phased** so each step is small,
  reversible, and ships behind the existing safety layer.
- **#26 handles patch repair inside the already-approved `files_expected`
  scope only.** It repairs a patch that cannot apply; it does not replan, widen
  scope, or change chunk boundaries.
- Adjacent concerns are explicitly **owned by later phases**:
  - **#27** — human-approved scope expansion recovery (renamed/moved files,
    files outside `files_expected`).
  - **#28** — stronger test validation (weak test-command policy).
  - **#29** — Memory M3 conflict lifecycle.

This design builds **on top of** the existing patch-failure classification and
fail-closed safety layer (#18A–#18D). It does not replace that layer.

---

## Existing Grounding

The following already exists and is live in the orchestrator. #26 reuses all of
it and adds nothing that weakens it.

- **Patch failure taxonomy / classification** — closed `PatchFailureType` enum
  and sanitized `PatchFailureReport` model in
  `backend/pipeline/patch_failures.py`.
- **Safe (fail-closed) failure behavior** — a non-applying patch leaves the
  working tree at its exact pre-patch state.
- **Clean-tree precondition** — `chunked_orchestrator._execute_single_chunk`
  refuses to run a chunk against a dirty tree (`DIRTY_WORKTREE`).
- **Rollback-and-verify** — `apply_patch_guarded` / `rollback_and_verify` in
  `backend/pipeline/patch_applier.py`: manifest-based reverse restore followed
  by an asserted `is_working_tree_clean()` check.
- **Pre-apply `scope_guard`** — `assert_files_in_scope(code, files_expected)`
  runs before any write (`scope_guard.py`).
- **Post-apply actual-changed-file scope recheck** —
  `validate_changed_files_in_scope` re-validates the real dirty set after apply,
  not just the declared intent.
- **Patch failure report persisted / surfaced** — `_fail_chunk_with_report`
  writes the report JSON into `chunks.completion_summary`, the headline into
  `chunks.error_message`, and emits a slim `stage_failed` event. The frontend
  renders it (`PatchFailureBanner.tsx`).
- **Dependency enforcement** — `_unmet_dependencies` blocks any chunk whose
  `depends_on` chunks are not exactly `completed` (`DEPENDENCY_NOT_MET`),
  including the resume/checkpoint-skip path.
- **Re-index UI/action exists** — `PatchFailureBanner.tsx` offers a `reindex`
  action that rebuilds the project index but **deliberately does not retry the
  failed chunk**.

### The actual gap #26 fills

The existing layer **classifies and fails safely** but does **not recover**.
Concretely, none of the following exist yet:

- No **human-triggered retry endpoint** for a failed chunk.
- No **dry-run preflight** that shares matching/precondition semantics with the
  real apply.
- No **recovery attempt history** (only a single failure snapshot is stored).
- No **current-file re-read** of approved files during a retry (the coder is
  not re-grounded against on-disk bytes).
- No **idempotent retry request** keyed by `attempt_id` / `failure_report_id`.
- No **recovered-change approval marker** to distinguish a recovery approval
  from a normal high-risk chunk approval.
- No **formal recovery lifecycle**.

Note: `max_attempts` is effectively `0` everywhere it is constructed today, so
the retry vocabulary in `suggested_actions_for` is currently inert. #26 makes
human-triggered retry real; it does **not** turn on automatic retry.

---

## Patch Model

- Pipewright does **not** use unified Git diff patches. There is no `git apply`,
  no hunk offsets, no 3-way merge.
- It uses **structured `FileChange` actions**: `create` / `modify` / `edit` /
  `delete` (see `apply_patch` in `patch_applier.py`).
- `edit` is an **exact `old_string` → `new_string` substring replacement that
  must match exactly once** (`_apply_edit_to_text`). No fuzzy matching.
- `PATCH_DOES_NOT_APPLY` therefore maps to a small, closed set of concrete
  causes:
  - `old_string` **absent** (0 occurrences).
  - `old_string` **not unique** (>1 occurrences).
  - **stale coder context** (coder generated against bytes that no longer match
    disk).
  - **whitespace / indentation mismatch** between `old_string` and disk.
  - **CRLF / LF mismatch** (exact match is byte-sensitive).
  - `create` **target already exists**.
  - **target file missing** for `edit` / `modify` / `delete`
    (`TARGET_MISSING`).
  - **structured action mismatch** (e.g. `modify` where `create` was intended).
- **Do not design Git three-way merge or fuzzy patching.** The recovery model is
  "re-ground the coder against current bytes and regenerate an exact match,"
  not "force a diff to apply."

---

## Problems This Design Solves

- Recover **stale approved-file content** failures — the file changed since the
  coder produced its edit.
- Recover **exact-match failures** by **re-reading the current approved file
  bytes** and regenerating a matching `old_string`.
- Recover **safe structured `edit` failures** strictly inside the approved
  `files_expected` set.
- **Surface retry history** so a human can see what was attempted and why.
- **Prevent silent commit of regenerated code** — recovered code is always
  human-reviewed before commit.
- **Preserve dependency safety** — a recovered chunk only unblocks dependants
  after normal completion.

---

## Problems This Design Does Not Solve

- A **wrong chunk plan** (the plan itself is incorrect). Recovery repairs a
  patch, not a plan.
- **Required files outside `files_expected`**. Recovery never widens scope.
- **Renamed / moved files** that require an amended scope.
- **Weak test-command quality** (recovery surfaces it but does not fix policy).
- **Large-file intelligent retrieval** (symbol/window extraction).
- **Semantic correctness** beyond what tests + human review provide.
- A **production analytics table** for recovery attempts.

Ownership of deferred concerns:

- **#27** — scope expansion recovery (files outside `files_expected`,
  renamed/moved files, human-approved `files_expected` amendment).
- **#28** — stronger test validation (weak-command acknowledgement / policy).
- **#29** — Memory M3 conflict lifecycle and usage tracking.
- **#32** — production persistence/analytics, including a dedicated
  recovery-attempt table if and when it is justified.

---

## Resolved Design Decisions

These are settled. Implementation phases must not relitigate them.

1. **#26 is patch-level recovery, not replanning.**
2. **#26 never changes `files_expected`.**
3. **#26 never weakens `scope_guard`.**
4. **First recovery implementation is human-triggered only.**
5. **Auto retry is deferred** (to an explicitly flagged, default-off
   experiment, #26G).
6. **Recovered chunks must pause at the existing `awaiting_chunk_approval`
   gate before commit.**
7. **No new commit site** — recovery reuses `_commit_and_complete_chunk`.
8. **No new checkpoint semantics.**
9. **Dry-run and real apply must share one matcher and one precondition
   evaluator** (no duplicated matching logic).
10. **Retry requests must include the current `attempt_id` or
    `failure_report_id`** (optimistic concurrency).
11. **Retry runs under the existing repo/project lock.**
12. **Retry re-reads only the approved `files_expected`.**
13. **Re-index is metadata-only and never expands write scope.**
14. **Rename/move recovery is deferred to #27.**
15. **A weak test command prevents silent recovered commit and requires human
    review.**
16. **Large files above the safe re-read cap stop with
    `MANUAL_INTERVENTION_NEEDED`** in early phases.
17. **Attempt history is stored as Pydantic-validated JSON in
    `completion_summary` for now.**
18. **A dedicated recovery-attempt table is deferred until production
    hardening (#32).**
19. **No memory auto-save from failed or recovering paths.**
20. **Dependent chunks remain blocked until the recovered chunk reaches normal
    `completed` status.**

---

## Recovery State Machine

Human-triggered recovery for a chunk that failed with
`PATCH_DOES_NOT_APPLY` (or another recoverable category):

```
PATCH_DOES_NOT_APPLY
   │
   ▼
failed chunk + persisted PatchFailureReport (working tree clean, asserted)
   │  human clicks "Retry"  (carries attempt_id / failure_report_id)
   ▼
backend validates attempt_id / failure_report_id  ──mismatch──▶ 409 stale (no-op)
   │ ok
   ▼
acquire project_repo_lock
   │
   ▼
re-check dependency guard  ──unmet──▶ DEPENDENCY_NOT_MET (fail, no recovery)
   │ ok
   ▼
re-check clean tree  ──dirty──▶ DIRTY_WORKTREE (stop; human cleans tree)
   │ clean
   ▼
re-read current bytes of approved files_expected (only those files)
   │
   ▼
regenerate the failed FileChange(s)   (full chunk context as constraints)
   │
   ▼
DRY-RUN via shared evaluator   ──verdict ≠ OK──▶ classify + report (no write)
   │ OK
   ▼
scope_guard (intent)  ──violation──▶ SCOPE_VIOLATION (no write)
   │ ok
   ▼
apply via guarded patch path (manifest)  + post-apply actual-dirty-set scope recheck
   │
   ▼
run tests
   ├─ fail ─▶ rollback + verify clean ─▶ TEST_FAILURE_AFTER_APPLY (report)
   │
   └─ pass ─▶ PAUSE at existing awaiting_chunk_approval
                with recovery marker (approval_reason = recovered_patch_review)
                   │
                   ▼
            human reviews regenerated diff + warnings (incl. weak-test warning)
                   │ approves
                   ▼
            existing commit path runs (_commit_and_complete_chunk)
                   │
                   ▼
            chunk → completed
                   │
                   ▼
            dependent chunks may proceed (only now)
```

Explicit statements:

- **Apply and test may happen immediately after the human clicks Retry**,
  because apply is reversible (clean-tree precondition + manifest rollback).
- **Commit must not happen until chunk approval.** Recovered code is never
  committed without human review.
- **A failed rollback, or a tree that is not clean after rollback, becomes
  `MANUAL_INTERVENTION_NEEDED`** and is never reported as recovered.
- **No new lifecycle state is introduced.** Recovery reuses the existing
  `awaiting_chunk_approval` gate; the only addition is a marker that labels the
  pending approval as a recovery review.

---

## Dry-Run Design

- Add a **zero-mutation dry-run phase** before any write in the recovery path
  (and, ideally, in the normal apply path too).
- The dry-run **must use the same matcher and precondition evaluator as the
  real apply.** Duplicated matching logic is forbidden — the two would drift and
  the dry-run's verdict would become a lie.
- Extract / share pure functions:
  - `find_unique_match(content, old_string) -> MatchResult` — the single
    occurrence-count primitive (`ok` / `absent` / `non_unique`, with count).
    `_apply_edit_to_text` calls it then replaces; dry-run calls it and stops.
  - `evaluate_file_change(change, current_content | None) -> DryRunVerdict` —
    runs every precondition currently encoded in `apply_patch`'s validation
    loop (valid action; `create` target absent; `edit`/`modify`/`delete` target
    present; `modify`/`create` content present; large-file wholesale block; the
    exact-once `old_string` rule) and returns OK or a `PatchFailureType`.
- **Real apply is gated by this evaluator:** apply becomes "dry-run verdict OK →
  backup → write," so the apply path cannot diverge from the dry-run.
- **Tests must prove the dry-run verdict and the real-apply outcome cannot
  drift** (including a fuzz test over random `(content, old_string)` pairs).

---

## Staleness / Hash Design

Phased. Use **raw-byte SHA-256** hashes. **Do not normalize text** — line
endings and whitespace are the dominant real cause of `PATCH_DOES_NOT_APPLY`, so
normalizing would hide the very signal we need.

Hashes to track (as they become available):

- **index hash** — hash recorded in the repo file index when it was built.
  Answers "is Pipewright's catalog stale vs disk?"
- **coder-input hash** — hash of the exact file bytes injected into the coder
  context. Answers "did the coder reason about different bytes than exist now?"
- **pre-apply hash** — hash captured under the repo lock immediately before
  dry-run/apply. The ground truth the edit must match.

Authoritative stale-context signal:

```
coder_input_hash != pre_apply_hash   →  stale coder context  →  re-read fixes it
index_hash       != pre_apply_hash   →  stale index          →  re-index fixes it
```

Phasing caveats:

- **Early phases may not have a coder-input hash yet** (the coder is currently
  fed an advisory relevant-files list, not guaranteed verbatim content). Until
  re-read injects verbatim content, the only available staleness signal is
  `index_hash != pre_apply_hash`.
- **Do not require a schema change just to add an index hash in the earliest
  phase** unless a content hash already exists to piggyback on.
- **Store diagnostics honestly:** when the coder-input signal is unavailable, do
  **not** overclaim "stale context." Report what is actually known.

---

## Re-Read vs Re-Index

Two distinct operations, currently conflated in the `reindex` button:

- **Re-read** is the **main recovery mechanism**. It loads the **current
  on-disk bytes of exactly the approved `files_expected`** and injects them into
  the regeneration prompt so the coder produces a matching `old_string`. It
  loads **only** approved files — never anything outside scope.
- **Re-index** refreshes the **metadata/catalog only** (`build_repo_index`). It
  is heavier and occasional (for the stale-index / target-missing family).

Rules:

- Re-index **must never mutate `files_expected`.**
- Re-index **must never add write scope.** A newly surfaced file may be *seen*
  but never *written*; `scope_guard` still enforces `files_expected` on apply.
- If re-index discovers **renamed / moved files**, **stop and defer to #27**.
  Silently retargeting an edit to the new path would change the human-approved
  blast radius — that is a scope-amendment decision, not patch recovery.
- `get_relevant_files` output remains **advisory**, never write authorization.

---

## Regeneration Strategy

- **Prefer regenerating only the failed `FileChange` action(s)** where they can
  be safely identified. Smaller surface, less model variance over the actions
  that were already fine.
- Provide **full chunk context and the unchanged actions as constraints**, plus
  the **verbatim current bytes** of the approved files and the strict
  `files_expected` list. State the file allowlist explicitly in the prompt.
- **Allow fallback to full-handoff regeneration only** when failed-change-only
  regeneration cannot be represented safely.
- **Structurally compare original vs regenerated actions** before apply/commit.
  Normalize each `FileChange` to a comparable tuple:
  - `path`
  - `action`
  - `sha256(old_string)`
  - `sha256(new_string | content)`
  Diff the tuple sets (added / removed / modified).
- **If recovery changes unrelated actions**, surface it loudly ("recovery also
  changed `src/foo.py`") and **require human review**; never auto-commit a
  drifted set.
- **Any scope violation fails** (intent and post-apply actual-dirty-set checks
  remain hard gates).
- **Unexpected `delete` actions during recovery require human review** even when
  the path is in scope — deleting an approved file to dodge an `old_string`
  mismatch is destructive and must be seen.

The prompt is advisory; `scope_guard` and the post-apply recheck are the hard
enforcement regardless of what the prompt says.

---

## Retry Endpoint / Idempotency

Design requirements (no implementation in #26A):

- The retry endpoint **must require the current `attempt_id` or
  `failure_report_id`** as an optimistic-concurrency token.
- Under the repo lock:
  1. Load the chunk's latest failure report / attempt.
  2. **Reject** if the request's attempt id does not match the latest
     (covers double-click and stale UI).
  3. **Reject** if the chunk status is not a retryable failed state.
  4. **Reject** if an attempt is already in flight.
  5. **Increment the attempt counter atomically**; the new attempt gets a fresh
     id that invalidates any other in-flight retry.
- **Double-click / duplicate retry** → `409 stale` (or equivalent).
- **Stale UI retry** → `409 stale` (or equivalent).
- Retry **must not re-triage or change chunk boundaries.**
- Retry **must not change `files_expected`.**

---

## Human Approval / Commit Lifecycle

- A recovered patch **may be applied and tested immediately** after the
  user-triggered retry (apply is reversible).
- A recovered patch **must pause at the existing `awaiting_chunk_approval`
  gate before commit.**
- **Do not create a new approval state in v1.** Reuse the existing gate
  (`_pause_for_chunk_approval` → `awaiting_chunk_approval` →
  `_approve_chunk_and_commit_locked` → `_commit_and_complete_chunk`). A
  recovered chunk is treated, for the commit decision, as if it required human
  review — even if it originally did not. This is a deliberate, correct safety
  upgrade.
- Add **marker fields / summary metadata** so the gate can be rendered as a
  recovery review:
  - `approval_reason: recovered_patch_review`
  - `recovery_attempt_id`
  - `weak_test_warning` (boolean)
- The UI should label this approval **"Review recovered change."**
- The existing approval path calls the existing commit function. **No new commit
  site.**

---

## Weak Test Command Interaction

- Full weak-test policy belongs to **#28**; #26 must not solve it.
- But **recovered code must not silently commit after a weak test command.** A
  weak command (e.g. `python --version`, per the existing classifier) means the
  recovered change is **not strongly verified**, and recovery is exactly where
  regenerated, never-human-seen code appears.
- If a weak test command is detected for a recovered chunk:
  - **Show a prominent warning.**
  - **Require human chunk approval before commit** (the recovery already pauses
    at `awaiting_chunk_approval`; the weak-test case makes that review the
    verification).
  - **Mark the recovered result as not strongly verified** (`weak_test_warning`).
- **Do not hard-block all weak tests yet** — that would break legitimate local
  self-use and steps on #28.

---

## Large File Policy

- Early #26 **must not become a large-file retrieval project.**
- If approved files are **below a safe threshold**, **full re-read is allowed**.
- If approved files are **too large**, **stop with
  `MANUAL_INTERVENTION_NEEDED`** rather than injecting a huge file blindly.
- Recommended thresholds (tunable; align with the existing
  `MAX_MODIFY_FILE_LINES = 200` philosophy):
  - **Full-read cap** — below it, inject full content.
  - **Absolute cap** — above it, manual intervention.
- **Smart windowing / symbol extraction is future work** unless deterministic
  support already exists. **Do not inject huge files blindly into retry
  prompts** (token cost and hallucination risk).

---

## Manual Intervention Conditions

Escalate to `MANUAL_INTERVENTION_NEEDED` (not retry, not reject) when continued
automation is unsafe or pointless:

- Rollback **failed** (or backup/manifest missing).
- Rollback completed but the **working tree is not clean**.
- **Retry budget exhausted** for a retryable transient category.
- A **dirty tree persists** (the user cannot/does not clean it).
- The **file changed during the attempt** (TOCTOU / concurrent external
  modification — pre-apply hash differs from a later capture).
- A **renamed / moved file requires scope amendment** (until #27 exists).
- A **large approved file exceeds the safe retry context limit**.
- An **unknown failure recurs after its cap**.
- **Structural action drift** cannot be safely reviewed/resolved.
- Recovery would require **files outside `files_expected`**.

For contrast: **retry** = a recoverable transient category with budget remaining
**and** a verified-clean tree. **Reject** = the human decides the chunk is wrong
or unwanted (and is the only path, besides view/manual, for the deterministic
categories `SCOPE_VIOLATION`, `FORBIDDEN_FILE`, `PATCH_MALFORMED`, `NO_CHANGES`,
which must never auto-retry).

---

## Interaction With Scope Guard

`scope_guard` runs at **every** layer, unchanged and strict, on **every**
attempt:

- **Before** the retry/regeneration prompt (intent constraint).
- **After** the regenerated handoff (intent check).
- **During** the dry-run.
- **Before** patch apply (declared-path + forbidden-path validation).
- **After** patch apply — the **actual dirty-set** check
  (`validate_changed_files_in_scope`).
- **Before** approval/commit (approval disabled while failed/recovering; commit
  only touches `files_expected`).

Statement: **`scope_guard` is law. Prompts are advisory.** Recovery can change
*what the coder is asked to do*, never *what is allowed*.

---

## Interaction With Chunk Dependencies

- A **failed chunk remains failed.**
- A **recovered-but-awaiting-approval chunk is not `completed`.**
- **Dependent chunks remain blocked** until the dependency is `completed`
  (`_unmet_dependencies` treats any non-`completed` status as unmet).
- **Existing dependency enforcement remains unchanged.**
- **Test requirement:** Chunk 1 recovers and reaches `awaiting_chunk_approval`;
  Chunk 2 depends on Chunk 1; **resume must not execute Chunk 2** while Chunk 1
  is not `completed`.

---

## Interaction With Checkpoints / Rollback

- **No checkpoint is saved for failed state as safe.** The only checkpoint write
  on the patch path is on the success path of `apply_patch`.
- **Failed-attempt diagnostics are not resumable checkpoints.** Diagnostics ≠
  checkpoints.
- **Attempt history is append-only diagnostics** (stored in
  `completion_summary`, not as a resume source).
- **Resumable checkpoints represent only the latest valid lifecycle state.**
- A retry **may overwrite the latest chunk `patch`/`test` checkpoint only after
  a safe, successful apply/test** (a retry re-enters from the clean-tree
  precondition, so re-apply is safe; the prior failure already restored a clean
  tree).
- **Commit still happens only through the existing commit path.**
- **Resume must not skip-complete a recovered-but-uncommitted chunk.** A
  recovered chunk may have a `test` checkpoint but no `chunk N:` commit;
  `_verify_completed_checkpoint_safe` (which requires the commit) must refuse to
  skip-complete it.

---

## Interaction With Memory

- **No memory auto-save from failed or recovering paths.** Coder
  `suggested_memory_entries` are only harvested on the success/commit path; keep
  it that way.
- Recovery-derived observations (e.g. "this repo uses CRLF line endings") may
  only become **pending suggestions through the existing M2 review lifecycle** —
  never auto-saved.
- **Dangerous suggestions** derived from failed model output (anything inferring
  content/intent from a failed attempt, or from a hallucinated path) must be
  **blocked**.
- **Memory M3 (#29)** may later help with a conflict lifecycle and usage
  tracking, allowing staleness-derived facts to be verified and expired safely.

---

## Data Model / Audit Trail

Schema-free first. Store attempt history inside the existing patch-failure
summary in `completion_summary`, validated by Pydantic.

Suggested models:

- `PatchRecoveryAttempt`
- `PatchRecoverySummary` (or extend the existing report with
  `attempts: list[PatchRecoveryAttempt]`)

Fields per attempt (all sanitized):

- `attempt_id`
- `attempt_number`
- `started_at`
- `recovery_mode` (`human` | `human_with_instruction` | `auto`)
- `failure_type`
- `failed_step`
- `changed_files_attempted`
- `changed_files_actual`
- `scope_ok`
- `preimage_matched`
- `model_used`
- `test_outcome` (`passed` | `failed` | `not_run`)
- `outcome` (`recovered` | `failed` | `manual_intervention`)
- `human_decision`
- `working_tree_clean`
- `rollback_performed`

Rules:

- **Cap the `attempts` list** (e.g. ~10) so the JSON cannot grow unbounded.
- **Never store file contents, secrets, `old_string`, or token-like values.**
  Reuse the existing `sanitize_for_log` path.
- Keep the `kind: "patch_failure"` discriminator so the existing defensive
  parser (`patch_failure_report_from_completion_summary`) stays valid.
- **A dedicated DB table is deferred to production hardening (#32)** — the
  trigger is concurrent write contention on the JSON read-modify-write
  (serialized under the repo lock today) and/or cross-run analytics needs.

---

## Backend Design

Recommended future modules/functions (not implemented in #26A):

- `backend/pipeline/patch_dry_run.py` — pure, zero-mutation dry-run.
- `backend/pipeline/patch_recovery.py` — recovery orchestration that **composes
  existing primitives** (re-read → regenerate → dry-run → `apply_patch_guarded`
  → `run_tests` → pause at approval).
- A **shared matcher / precondition evaluator** used by both dry-run and real
  apply (`find_unique_match`, `evaluate_file_change`).
- A **retry endpoint** with `attempt_id` idempotency, under the repo lock.
- A **retry-with-instruction endpoint** (later).
- A **mark-manual-intervention endpoint**.

This document **must not implement these.** It records the intended shape so the
implementation phases stay aligned.

---

## Frontend Design

Recommended (not implemented in #26A) — extend the existing
`PatchFailureBanner`/card; do not rebuild:

- Show:
  - failure category
  - files expected
  - attempted files
  - actual changed files
  - retry history
  - attempt count
  - weak-test warning
  - rollback status
  - recovery approval reason
- Add the **human-triggered retry action later** (#26E).
- Keep **approval disabled while a chunk is failed/recovering.**
- A recovered chunk's approval should clearly say **"Review recovered change."**

---

## Observability

- Emit **`recovery_attempt` events** per attempt.
- **Sanitized logs only** — no file contents, no `old_string`, no secrets.
- **Truncated technical details** (respect the existing event-bus size cap).
- Live logs should show **attempt number, category, outcome, and whether
  rollback left the tree clean** (e.g. "Attempt 2: re-read 2 approved files,
  regenerating…" / "Recovery succeeded, awaiting chunk approval" / "Budget
  exhausted — manual intervention needed").

---

## Test Plan

Future tests (not added in #26A):

- dry-run: `old_string` absent
- dry-run: `old_string` non-unique
- dry-run: `create` target exists
- dry-run: target missing
- dry-run: multi-file partial apply blocked
- **dry-run verdict and real apply outcome share behavior** (incl. fuzz)
- `PATCH_DOES_NOT_APPLY` → human retry → apply/test → `awaiting_chunk_approval`
- weak test command → recovered chunk **pauses before commit with warning**
- retry endpoint: duplicate attempt id → stale/`409`
- retry endpoint: stale UI retry rejected
- retry does **not** change `files_expected`
- retry does **not** re-triage
- regenerated patch touching outside scope → `SCOPE_VIOLATION`
- re-index does **not** expand scope
- renamed file → `MANUAL_INTERVENTION_NEEDED`
- large file exceeds threshold → `MANUAL_INTERVENTION_NEEDED`
- rollback failure → `MANUAL_INTERVENTION_NEEDED`
- Chunk 1 recovered but awaiting approval does **not** unblock Chunk 2
- resume cannot skip-complete a recovered-but-uncommitted chunk
- **no memory auto-save** from a failed/recovering path
- frontend renders attempt history and recovery warning

Commands (per project conventions):
`python -m pytest backend/tests -q -m unit`, plus targeted
`python -m pytest backend/tests/test_patch_applier.py backend/tests/test_patch_failures.py -q`,
and `cd frontend; npm.cmd run build` for UI phases.

---

## Phased Implementation Plan

### 26A — Design doc only

This document. **No runtime changes.**

### 26B — Shared dry-run/apply evaluator + diagnostics

- Extract the shared matcher / precondition evaluator.
- Add the zero-mutation dry-run.
- Improve diagnostics (incl. the available staleness signal).
- **No retry behavior yet.**

### 26C — Attempt history

- Add the Pydantic attempt models.
- Store `attempts[]` in `completion_summary`.
- Add `attempt_id` / `failure_report_id`.
- **No auto recovery yet.**

### 26D — Human-triggered retry

- Retry endpoint (idempotent via attempt id, under the repo lock).
- Re-read **approved files only**.
- Regenerate the failed change where possible.
- Dry-run → `scope_guard` → apply/test.
- **Pause at `awaiting_chunk_approval` before commit.**

### 26E — UI retry controls / history

- Render attempt history.
- Wire the retry action.
- Show the weak-test warning.
- Show the recovery approval marker.

### 26F — Re-index diagnostics

- Re-index metadata refresh.
- **No scope mutation.**
- Rename/move stops and defers to #27.

### 26G — Optional auto-retry experiment

- Feature flag, **default off**.
- Only for low-risk cases.
- Only with a `likely_test` test command.
- **Never** bypass final approval or chunk approval.

---

## Anti-Patterns

Do not:

- Implement **auto retry in v1**.
- Retry the **same stale input** without re-reading approved files.
- Use **fuzzy matching**.
- Use **forced patch application**.
- **Normalize CRLF/whitespace in the file** to make a patch apply.
- **Auto-expand `files_expected`.**
- Allow **re-index-driven scope expansion.**
- **Retry against a dirty tree.**
- **Commit before recovered-change approval.**
- **Checkpoint failed state** as safe.
- Let a **recovered-but-uncommitted chunk satisfy a dependency.**
- Store **file contents / `old_string` / secrets** in logs.
- **Auto-save memory** from recovery.
- Add a **production DB table too early.**
- Make **#26 solve #27 or #28.**

---

## Final Safety Invariants

- The **working tree must be clean before and after** a failed recovery.
- A **failed rollback escalates to manual intervention** and is never reported
  as recovered.
- **`scope_guard` remains strict.**
- **`files_expected` is never auto-expanded.**
- **Recovered code must be reviewed before commit.**
- **Dependencies remain blocked until normal completion.**
- **No failed/partial/no-op state is checkpointed as safe.**
- **No memory auto-save from recovery.**
- **All retry attempts are bounded, idempotent, and audited.**
