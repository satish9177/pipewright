# Patch Failure Recovery (#18A — Design)

> Status: **Design only.** This document defines the recovery model. Implementation lands in #18B–#18F (see §7). No code or schema change ships with #18A.

## Context

Pipewright runs: intent → chunk plan → human approval → code/patch → tests → final approval → PR. Today the patch step (`backend/pipeline/patch_applier.py`) and chunk executor (`backend/pipeline/chunked_orchestrator.py`) already have scattered recovery primitives — a backup/manifest rollback, a no-effective-change guard, scope guard, forbidden-path checks, working-tree-clean checks, and a test-failure rollback. But the behavior is **inconsistent and partly invisible to the user**: failures collapse into a single `error_message` string, the user gets no structured category, no safe next-action menu, and the guarantees around the working tree (especially with uncommitted user work) are not uniformly enforced before patching.

`#18A` is **design only**. The goal is a single, deterministic recovery model that: (1) classifies every patch failure into a fixed taxonomy, (2) guarantees the working tree is restorable to its exact pre-patch state, (3) never commits/checkpoints/pushes failed or partial state, (4) never bypasses scope or approval, and (5) hands the user a clear failure reason plus a bounded set of safe recovery actions. No schema change.

**Locked product decisions:**
- **Pre-patch worktree policy:** require a clean tree. Refuse to patch a dirty tree (→ `DIRTY_WORKTREE`). Rollback can therefore never destroy unsaved user work.
- **Rollback mechanism:** in-place + manifest. `git apply --check` dry-run → back up touched files → apply → validate scope → test → restore from manifest in reverse on any failure. No temp worktree, no scratch branch.
- **Retry policy:** capped auto (1–2) for transient categories, then force `MANUAL_INTERVENTION_NEEDED`. Re-index is a separate, human-initiated path.

---

## Recommended architecture (one paragraph)

Introduce a single **deterministic patch failure classifier and report model** that sits between the patch applier and the chunk orchestrator, riding entirely on existing storage (no schema change). The chunk executor enforces a strict linear lifecycle — **precondition (clean tree + captured pre-patch HEAD/status) → dry-run (`git apply --check`) → all-or-nothing apply with a touched-file manifest → scope/forbidden re-validation of *actual* changed files → tests → commit only on green** — and on **any** failure performs a manifest-based rollback (restore exactly the touched files, in reverse), verifies the tree is clean again, and emits a structured `PatchFailureReport`. That report (failure_type from a closed enum, sanitized message, technical details, attempted vs. actual changed files, allowed files, rollback_performed, working_tree_clean, suggested_actions, retry budget) is persisted as JSON in the existing `chunks.completion_summary` column and surfaced live via the existing `Event.data` bus; the human-readable headline reuses `chunks.error_message`. The frontend renders a failure banner with the category, message, changed-file diff-of-intent, rollback status, an optional stale-index hint, and a bounded action set — while **disabling all approval controls** whenever the chunk is in a failed state. Retries are capped per category and degrade to "manual intervention needed" so deterministic failures (scope, forbidden, malformed) can never loop.

---

## 1. Failure taxonomy

Closed enum `PatchFailureType` (new module `backend/pipeline/patch_failures.py`). Columns: **Trigger**, **User message**, **Retry?**, **Re-index?**, **Human action req'd?**, **Run/chunk status**.

| failure_type | Trigger condition | User-facing message | Retry allowed? | Re-index suggested? | Human action required? | Status recommendation |
|---|---|---|---|---|---|---|
| `PATCH_MALFORMED` | Coder output isn't a valid set of actions / diff is unparseable / missing fields. Detected before touching disk. | "The generated change was malformed and could not be read as a patch." | Yes (auto-capped) — regenerate | No | No (until cap) | chunk=`failed`, run=`failed` |
| `PATCH_DOES_NOT_APPLY` | `git apply --check` fails, or `old_string` not found / context mismatch. Nothing applied. | "The change could not be applied to the current files. It may be based on an out-of-date version of the repo." | Yes (auto-capped) | **Yes** | No (until cap) | chunk=`failed`, run=`failed` |
| `PATCH_PARTIAL_APPLY_BLOCKED` | Multi-file apply where some files would apply and others would not; we refuse to apply any (all-or-nothing). | "Only part of this change could be applied cleanly, so nothing was applied to keep the repo consistent." | Yes (auto-capped) | **Yes** | No (until cap) | chunk=`failed`, run=`failed` |
| `SCOPE_VIOLATION` | `ScopeDriftError` — coder targeted / actually changed a path not in `files_expected`. | "The change tried to edit files outside the approved chunk scope and was rejected." | **No** (deterministic) | No | **Yes** — reject chunk or replan | chunk=`failed`, run=`failed` |
| `FORBIDDEN_FILE` | `is_forbidden_write_path()` hit (`.env`, `.git`, secrets, lockfiles, compose, etc.). | "The change tried to modify a protected file and was rejected for safety." | **No** | No | **Yes** — reject/replan | chunk=`failed`, run=`failed` |
| `TARGET_MISSING` | Edit/modify/delete targets a file that does not exist in the working tree. | "A file this change expected to edit no longer exists in the repo." | Yes (auto-capped) | **Yes** | No (until cap) | chunk=`failed`, run=`failed` |
| `STALE_INDEX_OR_FILE_CHANGED` | Pre-patch file hash/content differs from what the plan/index was built on, or indexed path absent on disk. | "The repo changed since this plan was built, so the change is based on stale information." | Yes — **after re-index** | **Yes (primary)** | Soft — re-index then retry | chunk=`failed`, run=`failed` |
| `NO_CHANGES` | Apply succeeds but produces no effective git diff (existing no-effective-change guard). | "This change produced no actual edits — it may already be present in the repo. Nothing was committed." | No (pointless) | Maybe | **Yes** — accept/close or replan | chunk=`failed`, run=`failed` |
| `TEST_FAILURE_AFTER_APPLY` | Apply + scope OK, but `run_tests()` returns non-zero / times out. Patch is rolled back. | "The change applied but the project's tests failed, so it was rolled back." | Yes (auto-capped) — retry with instruction | No | Soft — review test output | chunk=`failed`, run=`failed` |
| `DIRTY_WORKTREE` | Pre-patch precondition: `is_working_tree_clean()` is false. Nothing applied. | "You have uncommitted changes. Commit or stash them before running this chunk so it can be safely rolled back." | Yes — after user cleans tree | No | **Yes** — commit/stash | chunk=`failed`, run=`failed` (pre-execution) |
| `UNKNOWN_PATCH_FAILURE` | Any uncaught exception in the patch/validate/test path. | "An unexpected error stopped this change. It was rolled back; no changes were kept." | Yes (auto-capped, once) | No | **Yes** if cap hit | chunk=`failed`, run=`failed` |

Notes:
- "Retry allowed" is the *policy ceiling*; the actual offer is gated by the per-chunk retry budget (see §5). Deterministic categories (`SCOPE_VIOLATION`, `FORBIDDEN_FILE`) are **never** auto-retried — a retry of the same plan produces the same violation.
- `STALE_INDEX_OR_FILE_CHANGED` is the bridge to the Phase 2H stale-index work: its message and the "Re-index" action are the primary recovery, not a plain retry.

---

## 2. Safe patch lifecycle

Linear, fail-closed. Lives in `_execute_single_chunk()` / `apply_patch()`. Acquire the existing `project_repo_lock_sync()` (`backend/git/run_locks.py`) around the **entire** before→commit window so concurrent runs cannot interleave.

```
[ acquire project_repo_lock_sync ]
  │
  ├─ 0. BEFORE PATCH (preconditions; nothing written)
  │     a. is_working_tree_clean(repo)?  ── no ─▶ DIRTY_WORKTREE → report, no apply, release lock
  │     b. capture pre_patch_head = current git HEAD hash (local_git)
  │     c. capture pre_patch_status (porcelain) — must be empty (asserted by 0a)
  │     d. for each target file: capture pre-image hash (for staleness compare)
  │
  ├─ 1. VALIDATE INTENT (still nothing written)
  │     a. parse coder output  ── invalid ─▶ PATCH_MALFORMED
  │     b. assert_files_in_scope(coder_output, files_expected) ── fail ─▶ SCOPE_VIOLATION
  │     c. is_forbidden_write_path(each path) ── hit ─▶ FORBIDDEN_FILE
  │     d. existence check per action (edit/modify/delete need file) ── miss ─▶ TARGET_MISSING
  │     e. staleness: if pre-image hash != hash the plan/index assumed ─▶ STALE_INDEX_OR_FILE_CHANGED
  │
  ├─ 2. DRY-RUN (git apply --check for diff-style; dry compute for edit/create)
  │     a. compute the full edit set in memory; verify each `old_string` occurs EXACTLY once
  │     b. if any file would fail ─▶ PATCH_DOES_NOT_APPLY (nothing written)
  │     c. if some-but-not-all would apply ─▶ PATCH_PARTIAL_APPLY_BLOCKED (nothing written)
  │
  ├─ 3. APPLY (all-or-nothing, with manifest)
  │     a. back up every touched file → backups/{run_id}/chunk_{n}/manifest.json (existing)
  │     b. apply all changes
  │     c. on ANY exception mid-apply ─▶ rollback_from_manifest() → UNKNOWN_PATCH_FAILURE
  │
  ├─ 4. VALIDATE ACTUAL CHANGED FILES (post-apply scope re-check — guards wrong-file writes)
  │     a. changed_files_actual = get_dirty_files(repo)
  │     b. every actual path ∈ files_expected? ── no ─▶ rollback ─▶ SCOPE_VIOLATION
  │     c. none forbidden? ── hit ─▶ rollback ─▶ FORBIDDEN_FILE
  │     d. changed_files_actual empty? ─▶ rollback path n/a (tree already clean) ─▶ NO_CHANGES
  │
  ├─ 5. RUN TESTS (run_tests)
  │     a. non-zero / timeout ─▶ rollback_from_manifest() ─▶ TEST_FAILURE_AFTER_APPLY
  │
  ├─ 6. COMMIT (only here)
  │     a. re-assert NOT is_working_tree_clean (real diff exists)
  │     b. commit locally (never empty). No push/PR — that stays behind final approval.
  │
  └─ 7. REPORT
        success → chunk=completed, completion_summary = success payload
        failure → chunk=failed, error_message = headline,
                  completion_summary = PatchFailureReport JSON
[ release lock ]
[ POST-ROLLBACK ASSERTION on every failure path: is_working_tree_clean(repo) == True;
  if not, escalate to MANUAL_INTERVENTION_NEEDED and DO NOT mark recoverable ]
```

Per-concern behavior:
- **Clean-tree check:** mandatory gate at step 0a. Reuse `local_git.is_working_tree_clean` / `ensure_clean_worktree`.
- **Pre-patch hash/status capture:** step 0b–0d. HEAD hash + per-file pre-image hashes; the latter feed staleness detection (§8) and are the rollback source of truth alongside the manifest.
- **Temp branch/worktree/staging:** **rejected** for #18A (see §3). In-place + manifest only.
- **Dry-run / check mode:** step 2, before any write. This is the single biggest reliability win — most `PATCH_DOES_NOT_APPLY` / `PARTIAL_APPLY` cases are caught with zero disk mutation.
- **Partial-apply prevention:** all-or-nothing. If the dry-run says any file fails, nothing is written (`PATCH_PARTIAL_APPLY_BLOCKED`). The apply loop itself also rolls back on mid-loop exception so a crash can't leave a half-applied tree.
- **Changed-file scope validation:** step 4 re-checks the **actual** dirty set, not just declared intent — this catches a patch that writes a file it didn't declare.
- **Rollback behavior:** restore exactly the manifest's touched files in reverse order (existing `_rollback_from_manifest`), then **assert the tree is clean**. A failed rollback is itself a hard failure → manual intervention, never reported as "recovered."

---

## 3. Rollback strategy — recommendation

| Approach | Verdict for #18A |
|---|---|
| `git apply --check` before apply | **Adopt.** Cheap, zero-mutation pre-flight. Catches most apply failures before any write. |
| Apply inside temp worktree | Reject for now. Strong isolation but heavy, slower, and brittle on Windows (path/locking). Revisit only if in-place proves insufficient. |
| Apply on temp branch | **Reject.** Committing to a scratch branch violates "never commit failed/partial state" and "never checkpoint without tests." |
| Snapshot touched files (manifest) | **Adopt as the core.** Already partly built (`backups/{run_id}/.../manifest.json`). Restores exactly the affected files. |
| `git restore` scoped files | Use as a **secondary verification**, not the primary path: after manifest restore, the tree should be clean; if any tracked target still shows a diff, `git restore <those paths>` and re-assert clean. Never `git restore` unscoped. |
| Full hard reset (`git reset --hard`) | **Forbidden.** Would obliterate any user state; the clean-tree precondition makes it unnecessary anyway. |

**Recommended:** **clean-tree precondition + `git apply --check` dry-run + touched-file manifest snapshot + reverse restore + post-rollback clean assertion**, with scoped `git restore` only as a belt-and-suspenders fallback. Because we refuse to patch a dirty tree, rollback can only ever touch files the patch itself created/modified — it can never destroy unsaved user work. This is the safest *practical* model for a local dev tool: simple, fast, Windows-friendly, and already aligned with existing code.

---

## 4. Failure report shape & storage

New dataclass/Pydantic `PatchFailureReport` in `backend/pipeline/patch_failures.py`:

```json
{
  "failure_type": "PATCH_DOES_NOT_APPLY",
  "message": "The change could not be applied to the current files. It may be based on an out-of-date version of the repo.",
  "technical_details": "git apply --check failed: hunk #2 FAILED at line 41 in src/foo.py (sanitized)",
  "changed_files_attempted": ["src/foo.py", "src/bar.py"],
  "changed_files_actual": [],
  "allowed_files": ["src/foo.py", "src/bar.py"],
  "suggested_actions": ["reindex", "retry", "reject_chunk"],
  "rollback_performed": true,
  "working_tree_clean": true,
  "retry": { "attempts": 1, "max_attempts": 2, "retryable": true },
  "stale_index_hint": true,
  "chunk_number": 3,
  "failed_step": "patch"
}
```

Field rules:
- `failure_type` ∈ the closed enum (§1). `message` is the safe, human headline (no paths to secrets, no tokens).
- `technical_details` is **sanitized** before storage/return (reuse the existing provider/Git error sanitization path — CLAUDE.md rule 14). Never echo file contents, env values, or tokens.
- `changed_files_attempted` = what the coder declared/targeted; `changed_files_actual` = `get_dirty_files()` observed post-apply (empty after rollback). The pair makes wrong-file and no-op cases obvious.
- `suggested_actions` is an ordered subset of the action vocabulary (§5), computed deterministically from `failure_type` + retry budget.
- `rollback_performed` / `working_tree_clean` are **asserted facts**, not hopes — both come from a real post-rollback `is_working_tree_clean()` call.
- `stale_index_hint` true for `STALE_INDEX_OR_FILE_CHANGED`, `PATCH_DOES_NOT_APPLY`, `TARGET_MISSING`.

**Storage — no schema change:**
- Headline → existing `chunks.error_message` (already rendered in `ChunkPlanPanel.tsx:292-299`).
- Full report JSON → existing `chunks.completion_summary` column (already JSON, already round-tripped to the frontend). Add a discriminator key, e.g. `{"kind": "patch_failure", ...report...}`, so the UI can tell success summaries from failure reports.
- Live updates → existing event bus `Event` with `kind="chunk_failed"` (or reuse `stage_failed`), `level="error"`, and the report dict in `Event.data` (capped at 4000 bytes — keep `technical_details` short).
- **Schema later (out of scope for #18A):** if we want failure analytics/queries across runs, a dedicated `patch_failures` table or typed columns would be a future `#18x`. Not now.

---

## 5. User recovery options

Action vocabulary (stable identifiers): `retry`, `retry_with_instruction`, `reindex` (re-index then retry), `reject_chunk`, `mark_manual_intervention`, `view_details`.

`view_details` is **always** offered. Availability of the rest by failure type:

| failure_type | retry | retry_with_instruction | reindex | reject_chunk | mark_manual |
|---|---|---|---|---|---|
| `PATCH_MALFORMED` | ✅ (cap) | ✅ | — | ✅ | ✅ |
| `PATCH_DOES_NOT_APPLY` | ✅ (cap) | ✅ | ✅ | ✅ | ✅ |
| `PATCH_PARTIAL_APPLY_BLOCKED` | ✅ (cap) | ✅ | ✅ | ✅ | ✅ |
| `SCOPE_VIOLATION` | — | ✅ (replan) | — | ✅ | ✅ |
| `FORBIDDEN_FILE` | — | — | — | ✅ | ✅ |
| `TARGET_MISSING` | ✅ (cap) | ✅ | ✅ | ✅ | ✅ |
| `STALE_INDEX_OR_FILE_CHANGED` | — (force reindex first) | — | ✅ (primary) | ✅ | ✅ |
| `NO_CHANGES` | — | ✅ | — | ✅ (accept/close) | ✅ |
| `TEST_FAILURE_AFTER_APPLY` | ✅ (cap) | ✅ (primary) | — | ✅ | ✅ |
| `DIRTY_WORKTREE` | ✅ (after user cleans tree) | — | — | ✅ | ✅ |
| `UNKNOWN_PATCH_FAILURE` | ✅ (cap, once) | ✅ | — | ✅ | ✅ |

Rules:
- **Every retry re-enters the full lifecycle** (§2) from step 0, including a fresh clean-tree check and dry-run. Retry never resumes mid-flight.
- **Retry budget (capped-auto):** per-chunk counter (held in `completion_summary` retry block, no schema). Transient categories allow `max_attempts` 1–2; once exhausted, the only offered actions collapse to `reject_chunk`, `mark_manual_intervention`, `view_details`. `reindex` resets/uses a separate counter so a legit stale-index recovery isn't blocked by patch-retry exhaustion.
- **`reject_chunk`** uses the existing reject endpoint/flow → chunk=`rejected`. **`mark_manual_intervention`** sets a terminal `MANUAL_INTERVENTION_NEEDED` state (can reuse `failed` + a flag in the report to avoid a new status; or add status string — prefer the flag to stay schema-free).
- **`retry_with_instruction`** appends a human note to the chunk description before re-planning/re-coding — never bypasses scope or approval; the re-planned chunk still honors `files_expected`.
- Deterministic failures (`SCOPE_VIOLATION`, `FORBIDDEN_FILE`) **never** offer plain `retry` — only replan-with-instruction or reject.

---

## 6. UI expectations

Render in `ChunkPlanPanel.tsx` (per-chunk) and/or a banner in `RunDetailPage.tsx`, reusing the existing red-card pattern (`border-red-500`, `text-red-500`) and the Phase 2H amber stale-index note style (`text-amber-700`, `text-xs`).

When a chunk's `completion_summary.kind === "patch_failure"` (or run/chunk status `failed`):
- **Failure banner:** red `Card` with a clear title.
- **Failure category:** human label for `failure_type` (small badge using `getStatusDisplay` danger tone).
- **Concise message:** `report.message`.
- **Technical details:** collapsed by default; expand via `view_details` (reuse `<pre>` monospace). Already sanitized server-side.
- **Changed files attempted:** list `changed_files_attempted`; if `changed_files_actual` differs, show both ("attempted X, repo now shows Y").
- **Rollback status:** explicit line — "✓ Rolled back, working tree clean" (green) when `rollback_performed && working_tree_clean`; **loud red warning** if `working_tree_clean === false` ("Manual intervention needed — repo not clean").
- **Suggested actions:** render only `report.suggested_actions` as buttons, reusing button patterns (green primary, outline, destructive, `disabled={isPending}`). Wire to the §5 endpoints.
- **Stale-index hint:** show the Phase 2H amber note when `report.stale_index_hint` is true; pair it with the `reindex` button.
- **Disabled approval when patch failed:** while the chunk is failed/awaiting recovery, **disable Approve Chunk and Final Approval controls** (gate the existing buttons in `ChunkPlanPanel`/`FinalApprovalPanel` on a "no active patch failure" predicate). Approval must be impossible over a failed/rolled-back chunk.

---

## 7. Implementation PR split (after #18A)

- **#18A — Design doc only** (this document). No code.
- **#18B — Failure taxonomy + report model/helper.** `backend/pipeline/patch_failures.py`: `PatchFailureType` enum, `PatchFailureReport` model, `suggested_actions_for(type, budget)`, sanitizer wiring, serialize/deserialize to `completion_summary`. Pure unit tests. No behavior change yet.
- **#18C — Patch applier dry-run / rollback / no-partial safety.** Add clean-tree precondition, pre-patch hash capture, `git apply --check`/in-memory dry-run, all-or-nothing apply, post-apply actual-changed-file scope re-check, post-rollback clean assertion. Map each failure path to a `PatchFailureType`. Unit tests per category (incl. forced rollback, dirty-tree refusal). No route/UI change.
- **#18D — Backend route / run-status wiring.** Persist `PatchFailureReport` into `completion_summary`, headline into `error_message`, emit `chunk_failed` event. Add retry/reindex/manual-intervention endpoints (or extend existing chunk endpoints) with the budget counter. Integration tests.
- **#18E — Frontend failure banner + retry/re-index actions.** Banner, category, details, changed-files, rollback status, stale-index hint, action buttons; disable approval on failure. Component tests + `npm.cmd run build`.
- **#18F — Smoke tests / docs.** End-to-end smoke for the common categories (does-not-apply, scope-violation, test-failure-after-apply, dirty-worktree), README/docs note on recovery behavior and safety guarantees.

Each PR is independently shippable and reversible; #18B/#18C land server-side safety before any UI promises it.

---

## 8. Hidden risks / edge cases

- **User has uncommitted work:** resolved by the clean-tree precondition — refuse with `DIRTY_WORKTREE` before any write. This is the single most important guard; it makes manifest rollback provably safe.
- **Patch modifies generated files:** treat like any file — must be in `files_expected`; if a build artifact path is also forbidden/lockfile, `FORBIDDEN_FILE` blocks it. Generated files often differ from the index → likely `STALE_INDEX_OR_FILE_CHANGED`; re-index is the right nudge.
- **Create / delete / rename:** create → backup is "file absent," rollback = delete it. delete → backup the content, rollback = recreate. **rename = delete+create**; both halves must be in scope and both manifest entries must roll back atomically, or the whole chunk fails. A rename where only one side is in `files_expected` → `SCOPE_VIOLATION`.
- **Applies but tests fail:** `TEST_FAILURE_AFTER_APPLY` → mandatory rollback → assert clean. Never commit. Test output goes (sanitized, truncated) into `technical_details`.
- **Scope guard catches wrong files *after* apply:** explicit step 4 re-check on the **actual** dirty set, then rollback → `SCOPE_VIOLATION`. Don't rely on pre-apply intent alone.
- **LLM outputs full files instead of a diff:** for large files this is already blocked (`modify` > `MAX_MODIFY_FILE_LINES` → must use `edit`). If a full-file blob slips through and can't be parsed into valid actions → `PATCH_MALFORMED`. Never silently accept a wholesale overwrite of a large file.
- **Empty / no-op patch:** `NO_CHANGES` via the existing no-effective-change guard, checked at step 4d / pre-commit. No commit, no push.
- **Concurrent runs:** wrap before→commit in `project_repo_lock_sync()`. A second run on the same repo blocks/serializes; never two patches racing the same tree. The clean-tree check inside the lock also catches a tree dirtied by another process.
- **Windows path separators:** normalize to forward slashes for scope/forbidden comparison (existing `normalize_relative_path`), but use OS-correct paths for disk I/O. All scope set membership compares normalized.
- **CRLF / LF line endings:** `old_string` exact-match and `git apply` are both EOL-sensitive — a CRLF/LF mismatch surfaces as `PATCH_DOES_NOT_APPLY`. Capture/compare using the bytes on disk; do not silently rewrite EOLs (that would itself be an out-of-scope change). Document this as a known cause and let re-index/regenerate handle it. (Future option: normalize EOL for matching only — out of scope for #18A.)
- **Stale index vs. stale git state:** distinct. Stale *index* = Pipewright's file index lags the repo (→ re-index). Stale *git/file* = the on-disk file changed since the plan's pre-image hash (→ regenerate/retry). Both fold into `STALE_INDEX_OR_FILE_CHANGED` for the user but the hint differentiates the suggested action (reindex vs. retry).
- **Retry loop causing repeated failures:** capped budget + deterministic categories never auto-retry. After cap → `MANUAL_INTERVENTION_NEEDED`. Each retry is full-lifecycle, so a deterministic failure can't masquerade as transient.
- **Large patch performance:** dry-run on huge edit sets and per-file backup copies cost I/O. Keep `technical_details` truncated; cap manifest/event payload sizes; the existing large-file edit policy already discourages whole-file blobs.
- **Security risk from patch content:** never write outside `files_expected`; `is_forbidden_write_path` blocks secrets/.git/.env/lockfiles; sanitize all `technical_details` before storage/return; never echo file contents or token-like strings in messages/events. Patch content is data, never executed.

---

## Top 5 non-negotiable safety invariants

1. **The working tree is always restorable to its exact pre-patch state.** Refuse to patch a dirty tree; on any failure, roll back from the manifest and *assert* `is_working_tree_clean()` — a failed rollback escalates to manual intervention and is never reported as recovered.
2. **Never commit, checkpoint-as-final, or push failed/partial/no-op state.** Commit happens only after apply + scope re-check + green tests; all-or-nothing apply means no half-applied tree; the no-effective-change guard blocks empty commits.
3. **Approvals and scope are never bypassed by recovery.** Retry/replan re-enter the full lifecycle and re-validate `files_expected`; approval controls are disabled while a chunk is in a failed/recovering state; `SCOPE_VIOLATION`/`FORBIDDEN_FILE` are never auto-retried.
4. **Every failure is a deterministic category with a sanitized, human-safe report.** Closed `PatchFailureType` enum; no secrets/tokens/file-contents in messages, `technical_details`, or events; the user always sees the reason and a bounded, safe action set.
5. **Recovery cannot loop or run away.** Per-chunk capped retry budget; deterministic failures don't auto-retry; exhaustion degrades to `MANUAL_INTERVENTION_NEEDED`; concurrent runs are serialized under the repo lock.

---

## Verification (for the eventual implementation PRs, not #18A)

#18A ships no code, so verification = design review. For #18C–#18F:
- Unit: `python -m pytest backend/tests -q -m unit` — one test per `PatchFailureType` incl. forced rollback and dirty-tree refusal; assert `working_tree_clean` true after every failure path.
- Targeted: `python -m pytest backend/tests/test_patch_applier.py -q` and the new `test_patch_failures.py`.
- Frontend: `cd frontend; npm.cmd run build` plus banner/action component tests.
- Manual smoke: trigger does-not-apply (edit a target out-of-band), scope-violation (coder targets unlisted file), test-failure (introduce a failing test), dirty-worktree (leave an uncommitted edit) — confirm rollback, clean tree, correct category, correct action set, disabled approval.
