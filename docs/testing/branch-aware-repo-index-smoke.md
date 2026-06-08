# Branch-Aware Repo Index Smoke Checklist

Manual verification checklist for #34: branch-aware repo index freshness,
flexible start branch behavior, and frontend surfacing for the new branch/index
safety states.

This is a docs-only smoke checklist. It does not change backend, frontend,
schema, package, test, or runtime behavior.

---

## 1. Purpose

Use this checklist to validate that #34 behaves correctly end to end:

- Repo index freshness is aware of branch, HEAD, and dirty state.
- Stale index state is surfaced and is not silently trusted.
- Users can start Pipewright from their current normal branch.
- Pipewright blocks unsafe implementation starts from `pipewright/*` branches
  or detached HEAD before planning consumes branch-specific state.
- Execution blocks if branch or HEAD drifted after planning.
- The UI clearly surfaces `stale_index`, `unsafe_start_branch`,
  `start_context_drifted`, and on-demand index freshness status.

---

## 2. Safety Guarantees

These invariants should hold across every smoke flow:

- Pipewright does not commit directly to protected base branches.
- Pipewright does not auto-checkout a different start branch.
- Pipewright does not auto-rebase, auto-sync, or otherwise reconcile branches.
- Pipewright does not auto-merge.
- Re-index does not auto-resubmit the implementation request.
- No file watcher is used as a correctness mechanism.
- Dirty-only index freshness is a warning, not a run-creation blocker.
- Execution clean-tree guards remain blocking before patch, commit, PR, and
  related write paths.
- The backend remains the source of truth for stale index, unsafe start branch,
  and start-context drift decisions.
- The frontend never navigates to `/runs/undefined` for no-run responses.

---

## 3. Pre-Smoke Setup

Before manual smoke:

- Start the backend.
- Start the frontend.
- Configure a test repository project in Pipewright.
- Use `github_cli` or `local_only` project mode as appropriate for the smoke.
- Confirm the project has a known safe PR base branch.
- Start from a normal branch, not a `pipewright/*` branch.
- Run a manual re-index once if the project has no index yet.

Inspect the target repository checkout before each flow:

```bash
git branch --show-current
git rev-parse --short=12 HEAD
git status --short
```

For clean-tree execution flows, `git status --short` should be empty before
approved execution.

---

## 4. Automated Validation Commands

Recommended validation already run across the #34D/#34E slices:

- #34D2 backend full unit suite was green:
  `2524 passed / 1 skipped / 4 deselected`
- #34E frontend production build passed.
- `git diff --check` passed.

Useful commands for final local validation:

```powershell
python -m pytest backend/tests -q -m unit --basetemp="$env:TEMP\pipewright-pytest-basetemp"
```

```powershell
cd frontend
npm.cmd run build
```

```powershell
git diff --check
```

Repo-wide frontend lint may still report pre-existing unrelated lint issues.
Treat those separately from #34 smoke unless the changed files introduce new
lint failures.

---

## 5. Manual Smoke Flows

### A. Happy Path From Normal Feature Branch

1. Checkout or create a normal branch, for example:

   ```bash
   git checkout -b feature/smoke-start-branch
   ```

2. Ensure the tree is clean:

   ```bash
   git status --short
   ```

3. Create an implementation request from the project dashboard.
4. Approve the generated plan.
5. Execute the run.
6. Verify `pipewright/<run-id>` is created from the intended start branch.
7. Verify no `unsafe_start_branch`, `stale_index`, or
   `start_context_drifted` message appears.
8. Verify final approval and PR behavior are unchanged.

### B. `stale_index` Hard Mismatch

1. Create or refresh the index on one branch.
2. Switch to another branch or otherwise change HEAD so the stored index
   snapshot no longer matches the current checkout identity.
3. Submit an implementation request.
4. Verify the UI shows a stale index banner.
5. Verify the frontend does not navigate to `/runs/undefined`.
6. Click **Re-index repository**.
7. Resubmit the implementation request.
8. Verify the run can proceed after re-index.
9. Verify re-index does not auto-resubmit the request.

### C. Dirty-Only / Soft Stale Does Not Block

1. Start from the same branch and same HEAD as the indexed snapshot.
2. Make an uncommitted edit, or create a row-count-only freshness mismatch.
3. Submit an implementation request.
4. Verify run creation is not blocked due only to dirty state or row count.
5. If using the Project Settings freshness check, verify the warning is
   informational.
6. If the tree is dirty, verify execution may still be blocked later by the
   existing clean-tree guard.

### D. `unsafe_start_branch` On `pipewright/*`

1. Checkout an old `pipewright/<run-id>` branch.
2. Submit an implementation request.
3. Verify the UI shows unsafe start branch guidance.
4. Verify no run is created.
5. Verify the frontend does not navigate to `/runs/undefined`.
6. Verify guidance says to checkout the branch you want Pipewright to start
   from.
7. Checkout a normal branch and resubmit.

### E. `unsafe_start_branch` On Detached HEAD

1. Checkout a detached HEAD.
2. Submit an implementation request.
3. Verify the UI shows detached HEAD guidance.
4. Verify no run is created.
5. Verify the frontend does not navigate to `/runs/undefined`.
6. Checkout a normal branch and resubmit.

### F. `start_context_drifted`

1. Create an implementation run on `feature/a`.
2. Approve the plan.
3. Before execution, switch to a clean `feature/b` checkout or advance HEAD on
   the same branch.
4. Click **Execute**.
5. Verify Run Detail shows a structured `start_context_drifted` warning.
6. Verify the response is not shown as a generic fallback error.
7. Verify the run is not marked failed solely because of the drift response.
8. Verify no `pipewright/<run-id>` branch was created from the wrong branch.
9. Checkout the original `feature/a` at the original HEAD if possible and
   execute again, or create a new run for the current branch.

### G. Project Settings Freshness Check

1. Open Project Settings.
2. Find the Repository Index area.
3. Verify the existing cheap index status still appears.
4. Click **Check freshness**.
5. Verify current, stale, unknown, or missing states display safely.
6. Verify only branch names, short SHAs, counts, and reasons are shown.
7. Verify the UI does not show full SHAs, repo paths, raw Git status, or dirty
   file lists.
8. Verify the freshness check does not poll automatically.

### H. Plan-To-Implementation Handoff

1. Create a `PLAN_ONLY` run.
2. While on a normal branch, hand off the plan to implementation.
3. Verify the start branch guard applies and normal handoff can proceed.
4. Repeat the handoff while on `pipewright/*` if practical.
5. Verify the handoff surfaces `unsafe_start_branch` and creates no
   implementation run.

---

## 6. Regression Checklist

Confirm these existing behaviors still hold:

- Needs-clarification run creation still works.
- Normal run creation still navigates to `/runs/{run_id}`.
- `stale_index` and `unsafe_start_branch` never navigate to
  `/runs/undefined`.
- `REPORT_ONLY` and `PLAN_ONLY` are not blocked by the start branch guard.
- The existing **Re-index repository** action still works.
- PR base safety behavior is unchanged.
- Resume behavior is unchanged.
- The frontend does not expose checkout, branch mutation, or auto-restore
  controls.

---

## 7. Known Limitations / Deferred Work

- No file watcher.
- No Git worktree isolation.
- No auto-restore of HEAD.
- No branch picker or checkout UI.
- No auto-rebase or auto-sync.
- No auto-resubmit after re-index.
- No dashboard-wide polling chip.
- Manual smoke is required because a frontend test runner is not configured.
- The start-context triage micro-window is accepted for now.
- Subdirectory-project freshness false positives are possible because the
  fingerprint is Git-root scoped.

---

## 8. Closeout Criteria

#34 is complete when:

- This smoke doc exists.
- Build and check commands pass, or unrelated baseline failures are documented.
- Manual smoke of core states is done or explicitly scheduled.
- #34 safety invariants are documented.
- No known unsafe branch/index behavior remains untracked.

