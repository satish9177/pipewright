# Repo Index Refresh / Stale-Index Recovery Smoke Checklist

Manual + automated verification for the repo index refresh / stale-index
recovery feature (#19A–#19E). Run this against a clean local environment after
pulling the latest branch. This is a verification checklist only — #19F changes
no runtime code.

---

## 1. Purpose

This checklist verifies the end-to-end repo index refresh / stale-index recovery
feature shipped across #19A–#19E:

- **Manual backend re-index endpoint** — `POST /projects/{project_id}/reindex`
  forces a fresh scan and atomically replaces the project's `file_index` rows
  (#19B).
- **Index status / age** — `GET /projects/{project_id}/index` reports
  `files_indexed`, `indexed_at`, and `status` without scanning (#19B).
- **Explicit-target stale-index auto re-index once** — when a request names a
  file that exists on disk but is missing from `file_index`, the chunked-run
  route re-indexes once and re-resolves instead of falsely saying "create it
  first" (#19C).
- **Frontend Re-index button + index age** — Project Settings shows the index
  status/count/last-indexed time and a manual re-index button (#19D).
- **PatchFailureBanner reindex action** — when a patch failure report's
  `suggested_actions` includes `reindex`, the banner offers an enabled
  re-index button (re-index only) (#19D).
- **Post-commit index refresh** — after Pipewright successfully commits a chunk,
  the index is refreshed so later chunks/runs see files it created/deleted/
  renamed (#19E).

---

## 2. Safety invariants

These must hold in every scenario below:

- **Re-index is read-only on repo files.** It only updates the `file_index`
  table.
- **Re-index never stages, commits, pushes, or switches branches**, and never
  writes to the working tree.
- **Re-index does not require a clean working tree.** It reflects the current
  on-disk files as checked out.
- **Forbidden / secret / binary / unsupported files stay excluded** — re-index
  reuses `build_repo_index`, so the existing scanner exclusions are unchanged.
- **Auto re-index is explicit-target-only and hard-capped at one attempt.** It
  never fires for vague requests and never loops.
- **Missing-on-disk targets still use the create-file clarification.** Pipewright
  never auto-creates a file.
- **Post-commit refresh happens only after a successful chunk commit** — never on
  patch failure, test failure, no-change, or while awaiting approval.
- **Post-commit refresh is best-effort** — a refresh failure is logged and
  swallowed; it must never fail a successful chunk/run.
- **No file watcher.**
- **No retry / re-index-and-retry yet** — the PatchFailureBanner reindex action
  re-indexes only; it does not retry the failed chunk.

---

## 3. Pre-smoke setup

Pull the latest branch and confirm a clean tree:

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

Run the targeted backend tests:

```bash
python -m pytest backend/tests/test_project_reindex.py -q
python -m pytest backend/tests/test_chunk_routes_stale_reindex.py -q
python -m pytest backend/tests/test_post_commit_reindex.py -q
python -m pytest backend/tests -q -m unit
```

Confirm the frontend builds:

```bash
cd frontend
npm run build
```

> **Note on lint:** a full-repo `npm run lint` / `eslint` may surface
> **pre-existing, unrelated** errors (e.g. a `react-hooks/set-state-in-effect`
> warning in `ProjectSettingsPanel.tsx` that predates #19). #19D used
> **changed-file scoped** `eslint` on the files it touched and those were clean.
> On Windows, prefer `npm.cmd` if PowerShell blocks `npm.ps1`.

---

## 4. Manual smoke scenarios

| # | Scenario | Steps | Expected |
|---|----------|-------|----------|
| **A** | Project Settings index status | Open Project Settings. | "Repository index" section visible; status shown (Indexed / Not indexed); files-indexed count shown; last-indexed time shown (or **Never**); helper copy explains the index is a cached map of the repo's files. |
| **B** | Manual Re-index button | Click **Re-index repository**. | Loading state ("Re-indexing…"); success message (e.g. "Re-indexed N files."); files-indexed / last-indexed update; **no repo files modified**. |
| **C** | Re-index with dirty worktree | Create an uncommitted, supported file in the target repo, then click **Re-index repository**. | Re-index succeeds; the supported file appears in the index; the dirty worktree **remains dirty** (file still uncommitted); no commit/stage occurs. |
| **D** | Explicit target on disk but missing from index | Create/commit `README.md` in the target repo; make the index stale (or delete the `README.md` row from `file_index`); ask: **"add hello bro line to README.md"**. | Backend auto re-indexes **once**; **no** false "create README.md first" clarification; the normal chunk plan proceeds (target grounded). |
| **E** | Missing target on disk | Ensure `README.md` does **not** exist on disk and is not indexed; ask: **"add hello to README.md"**. | The existing create-file clarification appears; **no** auto-create; **no** unsafe edit; no re-index needed. |
| **F** | Unsupported / excluded file on disk | Create an unsupported/excluded file (e.g. a binary-content `.md`, or a file under a skipped dir like `node_modules/`); ask to edit it by its exact path. | Safe "exists on disk but unsupported or excluded" clarification; **not** a false "create it first"; no edit. |
| **G** | Ambiguous after re-index | Have `README.md` and `docs/README.md` on disk but a stale index missing both; ask: **"update readme"** (or "add hello in the readme"). | Re-index **once**; ambiguous candidate clarification listing ranked candidates (`README.md`, `docs/README.md`) with a `clarification_id`; **no** auto-select; no run created. |
| **H** | PatchFailureBanner reindex action | Produce or inspect a patch failure whose report `suggested_actions` includes `reindex`; click **Re-index and refresh index**. | Calls the re-index endpoint; shows success/error; **does not retry** the failed chunk; other recovery actions remain disabled placeholders. |
| **I** | Post-commit refresh after create | Run a successful chunk that **creates** `README.md`; after the commit, ask to edit `README.md`. | `README.md` is found **without** a manual re-index; the normal edit flow proceeds. |
| **J** | Post-commit refresh after delete | Run a successful chunk that **deletes** a file; check index status (or ask to edit the deleted file). | The deleted file drops from the index after the commit; a later request no longer grounds to the deleted file. |
| **K** | Active run conflict | Attempt a manual re-index while the project repo lock is held / a run is actively executing for that project. | Friendly **"A run is active for this project — re-index when it finishes."** message / HTTP **409**; **no** partial index corruption (the previous index stays intact). |
| **L** | Success path unaffected | Run a normal exact-path edit on an already-indexed file. | The normal plan → execute → approval → PR path still works unchanged. |

---

## 5. UI checklist

- [ ] Project Settings "Repository index" section is visible.
- [ ] **Last indexed** formats safely even when the backend returns a SQLite
      timestamp (e.g. `2026-06-02 12:34:56`); falls back to the raw string or
      "Never", never crashes.
- [ ] **Re-index repository** button is disabled while loading.
- [ ] 409 / active-run state shows the friendly "re-index when it finishes"
      message, not a raw error.
- [ ] PatchFailureBanner **reindex** button is enabled **only** for the `reindex`
      action (and only when a `projectId` is available).
- [ ] PatchFailureBanner reindex **does not retry** the failed chunk.
- [ ] All other recovery actions remain disabled placeholders.
- [ ] No raw JSON is shown to the user (errors are sanitized messages).

---

## 6. Data / API verification

- [ ] `GET /projects/{project_id}/index` returns `files_indexed`, `indexed_at`,
      and `status` (`indexed` when rows exist, `not_indexed` / `indexed_at: null`
      when none).
- [ ] `POST /projects/{project_id}/reindex` returns `files_indexed`,
      `indexed_at`, and a human-readable `message` (plus `project_id` and
      `target_repo_path`).
- [ ] After re-index, `file_index` **contains** a newly added supported file.
- [ ] After re-index, `file_index` **no longer contains** a file deleted from
      disk.
- [ ] Unsupported / forbidden / binary files are **absent** from `file_index`.
- [ ] Unknown `project_id` → **404** on both endpoints; an active project repo
      lock → **409** on re-index, and `build_repo_index` is not called.

Quick API spot-checks (replace `PID`):

```bash
curl -s http://127.0.0.1:8001/projects/PID/index
curl -s -X POST http://127.0.0.1:8001/projects/PID/reindex
```

---

## 7. Completion criteria

- [ ] #19B backend endpoint/status tests pass
      (`backend/tests/test_project_reindex.py`).
- [ ] #19C stale explicit-target tests pass
      (`backend/tests/test_chunk_routes_stale_reindex.py`).
- [ ] #19E post-commit refresh tests pass
      (`backend/tests/test_post_commit_reindex.py`).
- [ ] Backend full unit suite passes (`python -m pytest backend/tests -q -m unit`).
- [ ] Frontend build passes (`cd frontend && npm run build`).
- [ ] Manual scenarios A–L pass, or any limitation is documented here.
- [ ] **No runtime code changed in #19F** (docs-only).

---

## 8. Current status

**#19 Repo Index Refresh / Stale-Index Recovery:**

- **#19A** design: **complete** (`docs/architecture/repo-index-refresh.md`)
- **#19B** backend endpoint / status: **complete**
- **#19C** stale explicit-target recovery: **complete**
- **#19D** frontend re-index UI: **complete**
- **#19E** post-commit refresh: **complete**
- **#19F** smoke / docs: **this checklist**

**Intentionally deferred (out of scope for #19):**

- File watcher / continuous auto-indexing.
- Re-index-and-retry the failed chunk from the PatchFailureBanner (the action
  re-indexes only).
- Incremental / touched-file-only indexing (every refresh is a full rebuild).
- Memory M2.
