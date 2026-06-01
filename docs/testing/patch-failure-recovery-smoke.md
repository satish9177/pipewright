# Patch Failure Recovery Smoke Checklist

Manual + automated verification for the patch failure recovery feature
(#18A–#18E). Run this against a clean local environment after pulling the
latest branch. This is a verification checklist only — #18F changes no runtime
code.

---

## 1. Purpose

This checklist verifies the end-to-end patch failure recovery feature shipped
across #18A–#18E:

- **Structured `PatchFailureReport`** — every patch failure is classified into a
  closed `PatchFailureType` and serialized to a stable report shape (#18B).
- **Safe guarded patch application** — `apply_patch_guarded` runs the clean-tree
  precondition, dry-run, apply, and post-apply scope validation (#18C).
- **Rollback / no-partial safety** — any failure rolls back from the manifest and
  the working tree is verified clean afterward (#18C/#18D).
- **Backend failure persistence** — the failed chunk stores the report JSON in
  `completion_summary` and the user-facing message in `error_message`, and emits
  a slim `stage_failed` event (#18D).
- **Frontend `PatchFailureBanner`** — failed chunks render a structured red
  banner instead of raw JSON (#18E).
- **Failed chunks cannot be approved** — the approval path rejects any chunk that
  is not `awaiting_chunk_approval` (#18D), and the UI hides approve controls for
  failed chunks (#18E).

---

## 2. Safety invariants

These must hold in every scenario below:

- A **dirty working tree is refused before patching** (fail fast with
  `DIRTY_WORKTREE`; no planner/coder/apply/test/commit runs for that chunk).
- **Failed or partial patch state is never committed.**
- **Failed patch chunks are not approvable** (backend rejects; UI hides controls).
- **Rollback leaves the working tree clean** (verified after every failure).
- The **structured failure report is stored in `completion_summary`** as JSON with
  `kind: "patch_failure"`.
- **Raw JSON is never shown in the UI** — only the structured banner.
- The **success path still works** end-to-end (apply → test → commit).

---

## 3. Pre-smoke setup

Start from a clean, up-to-date checkout:

```bash
git checkout develop
git pull origin develop
git status
```

Start the backend (separate terminal):

```bash
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

Start the frontend (separate terminal):

```bash
cd frontend
npm run dev
```

> On Windows, if PowerShell blocks `npm.ps1`, use `npm.cmd` instead of `npm`.

Run the automated tests:

```bash
python -m pytest backend/tests/test_patch_failures.py backend/tests/test_patch_applier.py -q
python -m pytest backend/tests/test_chunked_orchestrator.py -q
python -m pytest backend/tests/test_chunk_routes.py -q
python -m pytest backend/tests -q -m unit
cd frontend
npm run build
```

> A cosmetic Windows cache permission warning (`PytestCacheWarning … Access is
> denied`) may appear; add `-p no:cacheprovider` to suppress it. It does not
> affect results.

**Lint note:** `npm run lint` currently reports pre-existing errors in files
unrelated to this feature (e.g. `Layout.tsx`, `ProjectSettingsPanel.tsx`,
`ui/badge.tsx`, `ui/button.tsx`, `hooks/useRunEvents.ts`). The patch-failure
files (`utils/patchFailure.ts`, `components/PatchFailureBanner.tsx`,
`components/ChunkPlanPanel.tsx`) are lint-clean. Verify changed-file lint with:

```bash
cd frontend
npx eslint src/utils/patchFailure.ts src/components/PatchFailureBanner.tsx src/components/ChunkPlanPanel.tsx
```

---

## 4. Manual smoke scenarios

Use a small throwaway target repo. After each failing scenario, confirm the
target repo working tree is clean (`git -C <target_repo> status --porcelain`
returns empty).

| # | Scenario | Steps | Expected `failure_type` | Expected behavior |
|---|----------|-------|-------------------------|-------------------|
| **A** | Dirty worktree | Make an uncommitted edit in the target repo, then run a small implementation chunk. | `DIRTY_WORKTREE` | Fails before planner/coder/apply/test (no such log lines for the chunk after the precondition); "Patch failed" banner appears; no approve button; message tells the user to commit or stash first. |
| **B** | Patch does not apply | Run a chunk against a stale plan / target text that no longer matches (e.g. the `old_string` is gone). | `PATCH_DOES_NOT_APPLY` | Rollback/clean status visible; stale-index hint shown if `stale_index_hint` is set; no raw JSON; nothing committed. |
| **C** | Scope violation | Force/simulate a patch that touches a file outside `files_expected`. | `SCOPE_VIOLATION` | No commit; `changed_files_attempted` and `allowed_files` visible in the banner; approve controls hidden/disabled; tree clean after rollback. |
| **D** | Test failure after apply | Run a chunk whose code applies cleanly but fails the project's tests. | `TEST_FAILURE_AFTER_APPLY` | `rollback_performed = true`; `working_tree_clean = true`; technical (test) details collapsed by default; no double-rollback symptoms (single rollback, tree clean). |
| **E** | No changes | Trigger a no-op edit (new content identical to existing). | `NO_CHANGES` | No commit; banner message makes clear nothing changed / may already be present. |
| **F** | Success path | Run a normal small exact-path edit that applies and whose tests pass. | _(none)_ | No `patch_failure` completion_summary; no `PatchFailureBanner`; normal approval/commit path works. |
| **G** | Failed chunk approval blocked | After any failure (A–E), call the chunk approve endpoint for that chunk. | _(n/a)_ | Backend returns a safe error (HTTP 400); chunk remains `failed`; nothing committed. |

---

## 5. UI checklist

In the run detail page, for a failed patch chunk:

- [ ] Red **"Patch failed"** banner appears for `patch_failure` summaries.
- [ ] `failure_type` badge is visible.
- [ ] `message` is visible.
- [ ] Rollback status line is visible ("Rolled back" / "Rollback not performed").
- [ ] Working-tree status is visible (clean, or the red "Manual intervention
      needed — working tree is not clean" warning).
- [ ] Stale-index amber note appears **only** when `stale_index_hint` is true.
- [ ] Technical details are **collapsed by default**.
- [ ] **View details** toggles the technical details open/closed.
- [ ] Recovery action buttons are **disabled except View details**, with the
      "Recovery actions are not wired yet…" helper text.
- [ ] Raw `completion_summary` JSON is **not** shown.
- [ ] The generic duplicate "Error" block is **not** shown (banner replaces it).
- [ ] Approve controls are **not** shown for failed patch chunks.

For the success path (scenario F):

- [ ] No `PatchFailureBanner` renders.
- [ ] The normal Completion Summary still renders as before.

---

## 6. Data verification

If you need to inspect the API/DB/logs to confirm persistence (do **not** paste
secrets or real user paths into reports):

- **`completion_summary`** for the failed chunk should contain JSON with
  `kind: "patch_failure"` and the expected `failure_type`. Inspect via the
  `GET /runs/{run_id}/chunks` response or the `chunks.completion_summary` column.
- **`error_message`** for the failed chunk should equal `report.message`
  (the human-facing headline), not a raw exception string.
- The **`stage_failed` event** (stage `patch`, level `error`) should carry slim
  `data`: `kind`, `failure_type`, `chunk_number`, `failed_step`,
  `rollback_performed`, `working_tree_clean`, `manual_intervention_needed`,
  `stale_index_hint`, `suggested_actions`, and the
  `changed_files_attempted_count` / `changed_files_actual_count`.
- The **event `data` must NOT contain the full `technical_details`** — those stay
  only in `completion_summary` (the event payload is intentionally slim and size
  capped).

---

## 7. Completion criteria

This feature is considered verified when:

- [ ] Backend targeted tests pass
      (`test_patch_failures.py`, `test_patch_applier.py`,
      `test_chunked_orchestrator.py`, `test_chunk_routes.py`).
- [ ] Backend full unit suite passes (`-m unit`).
- [ ] Frontend build passes (`npm run build`).
- [ ] Manual smoke scenarios A–G pass, or known limitations are documented here.
- [ ] The working tree is clean after each failure scenario.
- [ ] No runtime code was changed in #18F (docs only).

---

## 8. Current status

**#18 Patch Failure Recovery status:**

- **#18A** design doc — complete
- **#18B** taxonomy / report helper (`PatchFailureType`, `PatchFailureReport`) — complete
- **#18C** guarded patch applier seam (`apply_patch_guarded`) — complete
- **#18D** backend orchestration / status wiring — complete
- **#18E** frontend `PatchFailureBanner` — complete
- **#18F** smoke / docs — this checklist

Retry / re-index **action execution is intentionally not wired yet**. Suggested
actions render as disabled placeholders in the UI; the next manual step is
decided from the structured failure report and details.
