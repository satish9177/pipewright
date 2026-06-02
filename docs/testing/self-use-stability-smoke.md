# Self-use Stability Smoke Checklist

Manual + automated verification that **#17 (ambiguous file clarification +
selection UX)**, **#18 (patch failure recovery)**, and **#19 (repo index refresh
/ stale-index recovery)** work together before we begin Memory M2. Run this
against a clean local environment after pulling the latest branch.

This is a verification checklist only — **#20A changes no runtime code**
(docs-only).

---

## 1. Purpose

Verify **#17**, **#18**, and **#19** together — as a single end-to-end self-use
flow — before starting **Memory M2**.

These features shipped separately and each has its own focused smoke doc
(`docs/testing/repo-index-refresh-smoke.md`,
`docs/testing/patch-failure-recovery-smoke.md`). This checklist exercises them
in combination during a real dogfood session and records any stability issues
found so they can be fixed or tracked before M2.

---

## 2. Pre-smoke setup

Check out the integration branch and confirm a clean tree:

```bash
git checkout develop
git pull origin develop
git status
```

Start the backend (from the repo root):

```bash
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

Start the frontend:

```bash
cd frontend
npm run dev
```

> On Windows, prefer `npm.cmd` if PowerShell blocks `npm.ps1`.

Prepare the **target repo** (the repo Pipewright operates on, not Pipewright
itself):

- **Target repo clean** — `git status` in the target repo shows a clean working
  tree (no uncommitted changes) before each scenario unless a scenario
  explicitly requires a dirty tree.
- **Target repo base branch is `pipewright-staging`** — the project's PR base
  branch is `pipewright-staging` (never `main`, `master`, or `develop`).
- **Push `origin/pipewright-staging` if missing** — confirm the base branch
  exists **on the remote**, not just locally. A local-only base branch is the
  root cause of the #20B-2 PR preflight bug below:

  ```bash
  # In the target repo:
  git ls-remote --heads origin pipewright-staging
  # If the branch is missing on origin, publish it:
  git checkout pipewright-staging
  git push -u origin pipewright-staging
  ```

---

## 3. Automated checks

Run the backend full unit suite (from the repo root):

```bash
python -m pytest backend/tests -q -m unit
```

Confirm the frontend builds:

```bash
cd frontend
npm run build
```

> If the full backend suite has known API / live-model failures unrelated to
> #17–#19, call them out explicitly and confirm the unit suite (`-m unit`) is
> green.

---

## 4. Manual smoke checklist

### A. #17 Ambiguous file UX

- [ ] **Ambiguous README candidate list** — with `README.md` and
      `docs/README.md` both on disk, ask **"update the readme"**. A clarification
      lists the ranked candidates with a `clarification_id`; no run is created
      and nothing is auto-selected.
- [ ] **`yes 1` works** — replying `yes 1` (confirm + pick candidate 1) pins
      that candidate and the chunk plan proceeds against it.
- [ ] **`1` does not globally select** — replying with a bare `1` (no
      confirmation) does **not** globally select / pin a candidate on its own; it
      must not silently start a run on an unconfirmed target.
- [ ] **Case mismatch `manual.md` → `MANUAL.md` — currently known bug** — file
      is indexed as `MANUAL.md`; asking **"add hello bro in manual.md"** surfaces
      `MANUAL.md` as a candidate, but selecting it loops back to
      `needs_clarification` instead of pinning it. See **#20B-1** below. (The
      exact-case request **"add hello bro in MANUAL.md"** works.)

### B. #18 Patch failure recovery

- [ ] **Dirty worktree gives `DIRTY_WORKTREE`** — with uncommitted changes in the
      target repo, a run that tries to patch fails with a `DIRTY_WORKTREE`
      failure reason (not a silent stash/overwrite).
- [ ] **Patch failure banner appears** — a patch failure surfaces the
      PatchFailureBanner with a sanitized reason and suggested actions (no raw
      diff / stack trace).
- [ ] **Failed chunk cannot be approved** — a chunk in a patch-failed state
      cannot be approved; approval is blocked until the failure is resolved.
- [ ] **Success path unaffected** — a clean exact-path edit on an indexed file
      still plans → executes → approves → commits normally.

### C. #19 Repo index refresh

- [ ] **Project Settings re-index works** — clicking **Re-index repository**
      shows a loading state, then a success message; files-indexed / last-indexed
      update; no repo files are modified.
- [ ] **Dirty worktree re-index works** — re-index succeeds with an uncommitted
      supported file present; the new file is indexed and the worktree stays
      dirty (no commit/stage).
- [ ] **Explicit on-disk but unindexed file auto re-indexes once** — a request
      naming a file that exists on disk but is missing from `file_index`
      triggers exactly one auto re-index and resolves the target; no false
      "create it first" clarification; no loop.
- [ ] **Missing file create clarification unchanged** — naming a file that does
      **not** exist on disk still shows the create-file clarification; no
      auto-create, no unsafe edit.
- [ ] **Pipewright-created file is editable in next request** — after a
      successful chunk creates a file, a follow-up request can edit it without a
      manual re-index (post-commit refresh).

### D. GitHub PR flow

- [ ] **Base branch exists locally and remotely** — `pipewright-staging` is
      present both locally and as `origin/pipewright-staging`.
- [ ] **Head branch pushed** — the run's `pipewright/<run_id>` head branch is
      pushed to origin.
- [ ] **Head has commits ahead of base** — the head branch contains at least one
      chunk commit not present on the base branch.
- [ ] **PR creation works** — with the above satisfied, PR creation against
      `pipewright-staging` succeeds (reuses an existing PR when possible; never
      auto-merges).
- [ ] **Current known bug: poor preflight when remote base branch missing** —
      when `pipewright-staging` exists locally but **not** on origin, PR creation
      fails with an opaque GraphQL error instead of a clear recovery message. See
      **#20B-2** below.

---

## 5. Known issues found during smoke

Two bugs were found during manual dogfood smoke. They are documented here and
tracked as follow-up PRs (see Section 7). **#20A does not fix them.**

### #20B-1 — Case-mismatch clarification selection loop

- **Repro:**
  1. File exists and is indexed as `MANUAL.md`.
  2. Ask: **"add hello bro in manual.md"** (lowercase).
  3. Pipewright shows candidate `MANUAL.md` with a `clarification_id`.
  4. Click / select `MANUAL.md`.
  5. The selection makes an API call but returns `needs_clarification` again —
     an endless selection loop.
- **Expected:** Selecting `MANUAL.md` should pin that candidate and create the
  chunk plan, exactly as the exact-case request **"add hello bro in MANUAL.md"**
  already does.
- **Suspected cause:** The clarification-resolution path re-matches the
  **original lowercase request text** (`manual.md`) instead of using the
  selected candidate's pinned path, so the case-insensitive match re-triggers
  the ambiguity/clarification check rather than treating the user's selection as
  authoritative.
- **Workaround:** Re-issue the request using the file's exact on-disk casing
  (e.g. **"add hello bro in MANUAL.md"**).

### #20B-2 — GitHub PR / base branch preflight error

- **Repro:**
  1. A chunk commit exists locally and on `origin pipewright/<run_id>`.
  2. The project base branch `pipewright-staging` exists **locally** but not as
     `origin/pipewright-staging`.
  3. PR creation fails with:

     ```text
     GraphQL: Head sha can't be blank, Base sha can't be blank,
     No commits between pipewright-staging and pipewright/<run_id>,
     Base ref must be a branch.
     ```
- **Expected:** Pipewright should **preflight remote base branch existence**
  (e.g. `git ls-remote --heads origin pipewright-staging`) before
  `gh pr create`, and give a clear recovery message — e.g. "Base branch
  `pipewright-staging` is not on origin; push it first" — instead of surfacing an
  opaque GraphQL error.
- **Suspected cause:** PR creation assumes the base branch exists on the remote.
  The base was created locally but never pushed, so GitHub has no base ref and
  reports blank head/base SHAs and "Base ref must be a branch."
- **Workaround:** Push the base branch to origin before creating the PR:

  ```bash
  git push -u origin pipewright-staging
  ```

---

## 6. Completion criteria

- [ ] All critical smoke paths in Section 4 (A–D) pass, or any limitation is
      documented here.
- [ ] Known issues are either fixed or tracked (#20B-1 and #20B-2 are tracked as
      follow-up PRs in Section 7).
- [ ] Target repo is clean after the smoke run (no leftover uncommitted changes,
      no stray Pipewright branches left dangling).
- [ ] **No runtime code changed in #20A** (docs-only).

---

## 7. Next PRs

- **#20B-1** — Fix case-mismatch clarification selection loop.
- **#20B-2** — Add PR creation / base branch preflight (clear recovery message
  when the remote base branch is missing).
- **#20C** — Update stabilization status.
- **#20D** — Tag stable local-self-use milestone.
