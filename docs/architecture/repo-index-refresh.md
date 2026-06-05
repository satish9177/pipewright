# Repo Index Refresh / Stale-Index Recovery (#19A — Design)

> Status: **Design only.** This document defines the product behavior and the
> backend/frontend shape for repo index refresh and stale-index recovery. **No
> runtime code and no schema change ship with #19A.** Implementation lands in
> #19B–#19F (see section "PR split").

## Context

Pipewright builds a lightweight, zero-AI map of a project's files in the
`file_index` table (`backend/repo/repo_indexer.py`). Every downstream grounding
step reads that map, never the live filesystem:

- `plan_path_grounding.get_indexed_paths_and_dirs()` — chunk `files_expected`
  grounding (#9B) and the "is the index empty?" gate in `chunks.py`.
- `file_alias_grounding.resolve_explicit_edit_target()` — the explicit-target
  resolver (#17B/#17C) that decides GROUNDED / NOT_FOUND / AMBIGUOUS / etc.
- `repo_indexer.get_relevant_files()` — the triage prompt's "RELEVANT INDEXED
  FILES" block.

The index is only ever built when it is **completely empty for a project**:

```python
# repo_indexer.ensure_repo_indexed()
if count == 0:
    return build_repo_index(project_id, target_repo_path)
# else: "already_indexed" — never rescans, even if the repo changed.
```

`build_repo_index()` *can* force a fresh scan (atomic delete-all + re-insert in
`save_file_index()`), but **there is no route and no UI** that calls it. The
only way to refresh today is a manual Python call.

### The observed bug

1. User (or Pipewright) adds `README.md` to the repo.
2. User asks: "add hello bro line to README.md".
3. `ensure_repo_indexed()` sees rows already exist → does nothing.
4. The resolver checks `file_index`, does not find `README.md`, and returns
   `NOT_FOUND` → Pipewright says "README.md not found" / "create it first".
5. **Restarting the backend does not help** — the index lives in SQLite, not in
   memory, and the count is still non-zero, so `ensure_repo_indexed()` still
   no-ops.

Index staleness is now a self-use blocker: the index silently diverges from
disk and the user has no in-product way to reconcile it.

---

## Short recommendation

Ship the smallest combination that closes the loop **without** a file watcher
and **without** weakening any safety guard:

1. **#19B — Manual re-index endpoint** `POST /projects/{project_id}/reindex`
   (read-only scan; wraps `build_repo_index`; refuses to run while the project
   repo lock is held). Plus a `GET` of index age/count for status display.
2. **#19C — Stale explicit-path auto re-index + retry once.** When the user
   names an explicit path/alias that is **absent from the index but present on
   disk**, re-index once and re-resolve. Bounded to exactly one attempt. If
   still missing → fall through to the existing clarification.
3. **#19D — Frontend re-index button + "last indexed" status** in Project
   Settings, and a "Re-index and retry" affordance on the stale clarification.
4. **#19E — Post-success index refresh.** After a chunk's changes are
   **committed**, refresh the index so the next chunk/run sees Pipewright's own
   created/deleted/renamed files.
5. **NOT now:** a file watcher / incremental indexer (D). Too much surface area
   and a background-mutation source for a local-first tool whose safety story
   depends on deterministic, on-demand index reads.

This matches the user's current thinking. The critique below tightens two
points: re-index must be **read-only and lock-aware**, and the post-success
refresh must happen **after commit, not after patch apply**.

---

## 1. Problem statement

| Symptom | Root cause |
|---|---|
| Restarting the backend does not refresh the index. | The index is persisted in SQLite; `ensure_repo_indexed()` keys off row count, not freshness. A restart re-reads the same stale rows. |
| Manual `build_repo_index()` is the only refresh path. | No route, no UI. Requires opening a Python shell — not viable for self-use, impossible for a demo. |
| Users do not know **when** to re-index. | There is no surfaced "last indexed" signal and no prompt. The index is invisible until it is wrong. |
| "create README.md, then edit README.md" fails on the second step. | The create may land on disk (manually, or via a future Pipewright create), but the index is never updated, so the edit resolver reports NOT_FOUND. |
| File-not-found clarification can be **wrong**. | `resolve_explicit_edit_target()` consults only `file_index`. A file that exists on disk but is missing from the index yields a false "not found / create it" clarification — actively misleading. |

The core defect: **`file_index` is treated as ground truth for file existence,
but it is a cache that is only ever populated once.**

---

## 2. Failure modes

| # | Failure mode | Mitigation (this design) |
|---|---|---|
| 1 | Index exists but is stale (rows present, disk diverged). | Manual re-index endpoint (#19B); post-success refresh after Pipewright's own commits (#19E); surfaced index age so the user can spot staleness (#19D). |
| 2 | File exists on disk but is missing from the index. | Stale explicit-path recovery: when the user names a path that is on disk but not indexed, re-index once and re-resolve (#19C). |
| 3 | File deleted on disk but still in the index. | `build_repo_index()` already replaces the whole project's rows atomically, so a deleted file drops out on any re-index. No partial-delete path. |
| 4 | File renamed. | Treated as delete + add by a full rescan; both old and new resolve correctly after re-index. No rename tracking needed. |
| 5 | Pipewright-created file not added to index. | Post-success index refresh after commit (#19E). |
| 6 | User-created file not indexed. | Manual re-index (#19B) and stale explicit-path recovery (#19C). |
| 7 | Wrong `project_id`. | Endpoint validates the project via `get_project`; unknown → 404. Never scans an unrelated repo. |
| 8 | Wrong / missing repo path. | `build_repo_index()` already resolves and checks the path exists and is a dir; otherwise raises. Endpoint maps to a safe 400/500 with a sanitized message. |
| 9 | Dirty worktree during re-index. | Re-index is **read-only** — it never stages/commits/patches. It does **not** require a clean tree and must not (see section "Backend design"). Scanning a dirty tree just indexes current on-disk content, which is correct. |
| 10 | Stale Pipewright branch / wrong branch checked out. | The scanner walks the working tree as-is (whatever is checked out). Document this: re-index reflects the **current checkout**, not any specific branch. We do not switch branches to index. |
| 11 | Indexing scans the wrong branch. | Same as #10 — we never change refs; the index always mirrors the live working tree. A note in the UI ("reflects current files on disk") sets the expectation. |
| 12 | Indexing while a run is executing. | The endpoint refuses (HTTP 409) when `is_project_locked(project_id)` is true. Execution holds `project_repo_lock`; re-index must not race a patch/commit. |
| 13 | Big-repo scan cost. | Existing skip set (`node_modules`, `.git`, `dist`, caches), 1 MB file cap, and supported-extension filter bound the walk. Re-index is synchronous for MVP; document that very large repos take a few seconds. (Async/streaming is a future option, not MVP.) |
| 14 | Binary/secrets accidentally indexed. | Already prevented: `should_skip_path()` calls `is_forbidden_path()`, `is_supported_file()` filters extensions, and NUL-byte detection skips binaries. Re-index reuses the exact same scanner — **no new path** can bypass these. |
| 15 | Unsupported extension. | `is_supported_file()` excludes it from the index. For the stale-recovery flow, a named target that exists on disk but has an unsupported extension is reported as "unsupported", not "not found" (see section "Stale target recovery"). |
| 16 | Case mismatch `README.md` vs `readme.md` (Windows). | Real hazard: explicit-path resolution is **case-sensitive** against the index, but Windows filesystems are case-insensitive. Mitigation: when an exact (case-sensitive) index miss occurs, surface case-insensitive candidates as a clarification rather than a flat NOT_FOUND (see section "Stale target recovery", open question Q4). |
| 17 | Index DB out of sync with the configured repo (project's `repo_path` changed). | Re-index always rescans `project.repo_path` and replaces all rows for the `project_id`, so a path change is reconciled on the next re-index. |
| 18 | Re-index fails midway. | `save_file_index()` runs the delete + all inserts inside one `engine.begin()` transaction. A mid-scan failure rolls back to the **previous** index — never a half-built one. The endpoint returns an error; the old index stays intact. |
| 19 | Multiple users/runs re-indexing the same project concurrently. | The per-project lock (#12) serializes mutation; re-index acquires/checks the same lock so two re-indexes (or a re-index + a run) cannot interleave. Last writer wins, and each write is atomic. |

---

## 3. Product UX options (compared)

| Option | What it is | Pros | Cons | Verdict |
|---|---|---|---|---|
| **A. Manual re-index button only** | User clicks "Re-index" in Project Settings. | Simple, explicit, zero magic, easy to reason about. | User must *know* to click it; the bug still bites mid-flow until they do. | **Ship (MVP core).** Necessary but not sufficient alone. |
| **B. Auto re-index on every run** | Rebuild before each chunked run. | Always fresh. | Per-run scan cost on every request; hides staleness instead of surfacing it; punishes large repos. | **Reject.** Too blunt; wasteful; masks the real signal. |
| **C. Auto re-index only when stale/missing-target suspected** | When an explicitly-named target is missing from the index but on disk, re-index once and retry. | Targeted, cheap, fixes the exact reported bug, bounded. | Only covers explicitly-named targets (not fuzzy/entity references — acceptable, those already fail safe). | **Ship (MVP core).** The precise fix for the observed bug. |
| **D. File watcher / incremental index** | Background watcher updates rows on FS events. | Always current, no user action. | Background mutation, OS-specific watchers, debouncing, partial-update consistency, races with runs, large new surface area. Conflicts with "deterministic on-demand reads". | **Defer.** Not MVP. Revisit only if A+C+E prove insufficient. |
| **E. Post-success index update for Pipewright-created/deleted/renamed files** | After a successful chunk commit, refresh the index. | Closes the "create then edit" loop for Pipewright's own changes; cheap (only on success). | Slight coupling of the executor to the indexer. | **Ship (MVP core).** Required so multi-chunk runs see their own new files. |

**Recommended MVP combination: A + C + E.** Manual control, targeted
auto-recovery for the exact bug, and self-healing after Pipewright's own
commits — with no background process and no schema change.

### Critique of the user's current thinking

The proposed plan (manual button + endpoint + auto re-index/retry once on
explicit on-disk-but-unindexed target + post-success refresh, no watcher) is the
right MVP. Two corrections:

- **"Add auto re-index and retry once when explicit target path exists on disk
  but not in index"** — correct, but the retry must be **hard-capped at one**
  and gated on an **explicit** target only. Never auto-re-index for vague/fuzzy
  requests (that would mask the specificity guard and add latency to every vague
  request). Never loop.
- **"Add auto index refresh after successful Pipewright patch creates/deletes/
  renames files"** — refresh after **commit**, not after patch apply. A patch
  can apply and then be rolled back by a failing test or rejected at chunk/final
  approval. Indexing post-apply would record files that may be reverted. Index
  only what has been committed (see section "Post-success index refresh flow").

---

## 4. Backend design

### Route

```
POST /projects/{project_id}/reindex
```

Matches the existing `/projects/{project_id}` style in `backend/routes/projects.py`.
A noun under the project resource. (Considered `/projects/{id}/index:rebuild`;
rejected for consistency with the flat route style already in use.)

Optional companion for status display (#19D):

```
GET /projects/{project_id}/index   ->  { files_indexed, indexed_at, ... }
```

### Behavior

1. **Validate project.** `get_project(project_id)`; `None` → 404. Never scan an
   unknown/unrelated repo.
2. **Validate repo path.** Defer to `build_repo_index()`, which resolves and
   checks `repo_path` exists and is a directory. Map failures to a sanitized
   400/500 (no raw stack traces, per safety rules #13/#14).
3. **Refuse during active execution.** If `is_project_locked(project_id)` →
   `HTTP 409` (reuse the `ProjectRepoLockError` → 409 pattern from `chunks.py`).
   This prevents a re-index racing a patch/commit. The endpoint should acquire
   the project repo lock for the duration of the scan so a run cannot start
   mid-scan either.
4. **Scan + replace.** Call `build_repo_index(project_id, project.repo_path)`.
   This already: walks the tree, skips forbidden/secret/binary/oversized files,
   filters to supported extensions, and replaces all rows for the project in one
   transaction.
5. **Read-only on the repo.** Re-index never stages, commits, patches, pushes,
   switches branches, or writes to the working tree. The *only* write is to the
   `file_index` SQLite table.

### Response shape

```json
{
  "status": "complete",
  "project_id": "<id>",
  "target_repo_path": "<resolved, display-safe path>",
  "files_indexed": 128,
  "indexed_at": "2026-06-02T12:34:56Z",
  "message": "Re-indexed 128 files."
}
```

- `indexed_at` — see section "Data/model": derivable from existing rows; no schema
  change.
- `target_repo_path` — return the resolved path for transparency. It is a local
  filesystem path (not a secret), but run it through the same project-response
  sanitization used elsewhere so we never leak tokens/secret-like values.

### Clean-worktree question (explicitly: do NOT require it)

Re-index **must not** require a clean worktree. Rationale:

- Re-index is read-only; it cannot destroy uncommitted work, so the
  clean-tree precondition that protects *patching* (#18) does not apply here.
- The whole point of re-index is often to pick up files the user just
  created/edited — which by definition means a dirty tree. Requiring clean
  would defeat the feature.
- Scanning a dirty tree indexes the **current on-disk content**, which is
  exactly what grounding should reflect.

The only gate is the **execution lock** (#12), not worktree cleanliness.

---

## 5. Stale target recovery flow

User says **"edit README.md"** and `README.md` is not in the index. Exact safe
flow (executed inside the explicit-target branch in `_create_chunked_run_core`,
just before/after `resolve_explicit_edit_target`):

```
resolve_explicit_edit_target(...) == NOT_FOUND for an explicit path/alias
  │
  ├─ Is the named path present ON DISK (exact, then case-insensitive)?
  │     │
  │     ├─ YES, exact on-disk match, and we have NOT already re-indexed this request:
  │     │     → re-index once (read-only, lock-aware)
  │     │     → re-resolve ONE time
  │     │         ├─ now GROUNDED → proceed normally (pin files_expected)
  │     │         └─ still NOT_FOUND → fall through to existing clarification
  │     │            (do NOT re-index again — hard cap = 1)
  │     │
  │     ├─ YES on disk, but UNSUPPORTED extension → "unsupported file type"
  │     │     clarification (explain we index/edit text/code files; .md/.txt/.py…),
  │     │     NOT "create it".
  │     │
  │     ├─ YES on disk, but only a CASE-INSENSITIVE match (README.md vs readme.md):
  │     │     → re-index, then offer the real on-disk casing as a candidate
  │     │       (ambiguous-style clarification), never silently rewrite the path.
  │     │
  │     └─ YES on disk, but FORBIDDEN/secret path → existing forbidden refusal
  │           (re-index never changes this; .env/.git/secrets stay refused).
  │
  └─ NOT on disk at all → keep the CURRENT behavior unchanged:
        existing create-file clarification (#17C) — "create README.md? Pipewright
        will not create it automatically." We never auto-create.
```

Safety invariants for this flow:

- The on-disk existence probe is a **single `Path.exists()` / `is_file()`** on
  the explicitly-named relative path resolved under `project.repo_path`,
  rejecting absolute paths and `..` traversal (reuse the normalization already
  in `file_alias_grounding`). It is **not** a filesystem walk and **not** a
  content read.
- Re-index is attempted **at most once per request**. A boolean "already
  re-indexed" flag in the request flow prevents any loop (test: section "Tests" — no
  infinite re-index loop).
- We **never auto-create** a file. A genuinely-missing-on-disk target keeps the
  exact #17C create clarification.
- Re-index never relaxes forbidden/secret refusals.

This auto-recovery applies **only to explicit paths/aliases** (the cases where
the user named a concrete file). Fuzzy/entity requests (`NO_TARGET`) keep
flowing through the specificity guard untouched — they must not trigger a scan.

---

## 6. Post-success index refresh flow

After a chunk's changes are durably recorded, refresh the index so subsequent
chunks/runs see Pipewright's own created/deleted/renamed files.

**Where:** in the chunk executor, **after the commit succeeds** for a chunk
(`approve_chunk_and_commit` / the orchestrator's post-commit success path),
inside the existing project lock, never on the failure path.

**Why after commit (not after apply, not after tests, not after final
approval):**

| Candidate point | Rejected because |
|---|---|
| After patch apply | The patch may still be rolled back by a failing test or rejected at approval. Indexing here records files that may be reverted (#18 rollback). |
| After tests pass | Closer, but the change is not yet committed; a later chunk/final rejection could still revert. |
| **After commit** ✅ | The change is durable in git history. Indexing here records exactly what is now on disk and committed. This is the safe point. |
| After final approval only | Too late: a multi-chunk run would not see chunk 1's new file when planning/executing chunk 2. |

**What to refresh (MVP):** a **full `build_repo_index()` rebuild** for the
project. Rationale: the scan is already bounded and cheap on normal repos, the
rebuild is atomic, and a full rebuild is strictly simpler and safer than a
targeted delta (no partial-update consistency bugs, no rename bookkeeping). A
touched-files-only incremental update is a possible future optimization, **not
MVP** — "prefer simple, boring, testable".

**Failure isolation:** the refresh is best-effort and must **never** fail the
run. Wrap it like `triage._ensure_index_without_blocking` already does — log a
warning on failure and continue. A stale index after a successful commit is
recoverable via the manual button; a crashed run is not acceptable.

---

## 7. Interaction with #18 patch failure reports

`PatchFailureReport` already carries `stale_index_hint: bool` and, for
apply/target/stale categories, `reindex` in `suggested_actions`. The
`PatchFailureBanner` renders the hint text and renders the action buttons
**disabled** with the copy "Recovery actions are not wired yet."

Recommended split:

- **#19D (this batch):** once `POST /projects/{id}/reindex` exists, enable the
  **`reindex` button** in `PatchFailureBanner` to call **only** the re-index
  endpoint. Clicking it re-indexes and updates the displayed index age. It does
  **not** auto-retry the failed chunk.
- **Separate future PR (e.g. #19G / #20):** wiring "re-index **and** retry the
  failed chunk" as one action. Auto-retry-after-reindex touches the run
  lifecycle and retry budget (#18's capped-retry model) and deserves its own
  scoped PR with its own tests. Do not couple it into the endpoint PR.

So: **clicking re-index calls the endpoint only; retry stays manual / future.**
This keeps each PR single-purpose and avoids quietly re-entering execution from
a failure banner.

---

## 8. Frontend UX

- **Project Settings — Re-index button.**
  - Primary button "Re-index repository".
  - Status line: "Last indexed: <relative time> · <N> files" (from
    `GET /projects/{id}/index`).
  - On click: call `POST …/reindex`, show a spinner, then update the status
    line. On 409 (run active): "A run is active for this project — re-index when
    it finishes." On error: sanitized message.
  - Helper copy: **"The index is a cached map of your repo's files. Re-index
    after adding, renaming, or deleting files outside Pipewright."**

- **Clarification UI — "Re-index and retry".**
  - When the backend signals "this file exists on disk but is missing from the
    index" (the #19C path), the stale clarification offers a **"Re-index and
    retry"** affordance that re-runs the original request after re-indexing.
  - This is distinct from the #18 banner button: here the original request has
    not yet produced a run, so "retry" just re-submits the request — safe and
    already idempotent.

- **PatchFailureBanner — reindex action.**
  - Enable the existing `reindex` button (currently disabled) to call the
    endpoint once the backend exists (#19D). Keep the other recovery actions
    disabled until their own PRs. Update the "not wired yet" copy accordingly
    (only the still-disabled actions carry that caveat).

- **Copy discipline:** always describe the index as "a cached file map", be
  honest that it can go stale, and never imply Pipewright auto-creates files.

---

## 9. Data / model

- **Does `file_index` have `indexed_at`?** **Yes.** `schema.sql` defines
  `indexed_at DATETIME DEFAULT CURRENT_TIMESTAMP` with `UNIQUE(project_id, path)`.
- **Can we show index age from existing data?** **Yes.** `save_file_index()`'s
  INSERT does **not** set `indexed_at`, so the column takes its `CURRENT_TIMESTAMP`
  default on every rebuild. The newest/most-common `indexed_at` for a project is
  the last-indexed time. A small read helper (`MAX(indexed_at)` and `COUNT(*)`
  for the project) is all that's needed.
- **Do we need a new table/fields?** **No.** Index age and file count are both
  derivable from existing rows.
- **Schema change?** **None.** (Matches #18's "no schema change" discipline.)

One caveat to verify in #19B: confirm SQLite stamps the same `indexed_at` across
a single transaction's inserts (it effectively does, as `CURRENT_TIMESTAMP` is
fixed per statement/transaction); using `MAX(indexed_at)` is robust regardless.

---

## PR split

Recommended small, single-purpose PRs (refines the proposed split):

| PR | Scope | Tests |
|---|---|---|
| **#19A** | This design doc. No code. | — |
| **#19B** | Backend: `POST /projects/{id}/reindex` (lock-aware, read-only) + `GET /projects/{id}/index` age/count read helper. | Endpoint rebuilds/replaces rows; 404/409/path-error handling; forbidden files still excluded. |
| **#19C** | Stale explicit-path recovery: on-disk-but-unindexed → re-index once + re-resolve, hard-capped. Includes unsupported-extension and case-mismatch handling. | On-disk-missing-from-index triggers one re-index + retry; no loop; create/forbidden/unsupported branches. |
| **#19D** | Frontend: Project Settings re-index button + index-age status; enable `reindex` button in `PatchFailureBanner` (endpoint-only). | Button calls endpoint; status renders; stale-index message appears. |
| **#19E** | Post-success index refresh after chunk **commit** (best-effort, lock-held, never fails the run). | Create-file chunk success makes the next edit find the file; deleted file drops out; refresh failure does not fail the run. |
| **#19F** | Smoke docs / manual validation checklist (mirrors `docs/testing/patch-failure-recovery-smoke.md`). | — |
| *(future)* **#19G/#20** | "Re-index **and** retry the failed chunk" from the #18 banner (touches retry budget/lifecycle). | Re-index + bounded retry; respects #18 cap; no loop. |

This keeps each PR to one purpose and orders them so #19C and #19D both depend
only on #19B.

---

## Test plan

Backend (`pytest -m unit`):

- Re-index endpoint rebuilds `file_index` from disk (count matches scan).
- Re-index replaces stale rows (a file removed from disk is gone after re-index;
  a file added is present).
- Re-index is atomic: a simulated mid-scan failure leaves the **previous** index
  intact (no half-built state).
- Unknown `project_id` → 404; no scan performed.
- Missing/invalid `repo_path` → sanitized 400/500; no rows touched.
- Re-index refused with 409 while the project repo lock is held.
- Re-index does **not** index forbidden/secret/binary/oversized/unsupported
  files (reuses scanner guarantees).
- Stale recovery: explicit path on disk but missing from index → exactly one
  re-index + successful re-resolve → run proceeds.
- Stale recovery: still missing after one re-index → falls through to existing
  clarification; **no second re-index** (no infinite loop).
- Stale recovery: path **not** on disk → unchanged create-file clarification;
  no re-index.
- Stale recovery: unsupported extension on disk → "unsupported" clarification,
  not "create".
- Case mismatch (`README.md` vs `readme.md`): real on-disk casing offered as a
  candidate, not silently rewritten.
- Post-success: create-file chunk commit → next edit resolves the new file.
- Post-success: deleted-file chunk commit → file removed from index after
  refresh.
- Post-success refresh failure is swallowed (run still succeeds).
- Index age helper returns last-indexed time + count without a schema change.

Frontend (`npm.cmd run build` + component tests):

- Re-index button calls `POST …/reindex` and updates the status line.
- 409 path shows the "run active" message.
- Stale-index clarification renders the "Re-index and retry" affordance.
- `PatchFailureBanner` `reindex` button is enabled and calls the endpoint
  (other recovery actions stay disabled).

---

## Open questions

1. **Sync vs async re-index for large repos.** MVP is synchronous. Do we need a
   progress/streaming response or a background job for very large repos, or is a
   few-second blocking call acceptable for local self-use? (Lean: synchronous
   for MVP.)
2. **Lock acquisition vs. lock check on the endpoint.** Should re-index *acquire*
   `project_repo_lock` for the scan duration, or only *check* `is_project_locked`
   and proceed? Acquiring is safer (prevents a run starting mid-scan) but makes
   re-index briefly block run starts. (Lean: acquire.)
3. **Auto-refresh debounce on post-success.** In a multi-chunk run, do we
   re-index after **every** chunk commit, or once at the end of the run? Per-chunk
   is needed so chunk N sees chunk N-1's new files; end-of-run is cheaper. (Lean:
   per-chunk, since the cost is bounded and correctness needs it.)
4. **Case-insensitive matching policy (Windows).** Should the explicit-path
   resolver become case-insensitive when the OS filesystem is case-insensitive,
   or always offer candidates and let the user pick? (Lean: keep resolution
   case-sensitive against the index, but surface case-variant candidates as a
   clarification — never silently rewrite the user's path.)
5. **`GET /projects/{id}/index` payload.** Just `{files_indexed, indexed_at}`,
   or also a small breakdown (by `file_type`)? (Lean: minimal for MVP.)
6. **Should re-index emit an event** on the existing event bus for live UI
   feedback, or is a plain request/response enough? (Lean: request/response for
   MVP.)

---

## Top safety invariants

1. **Re-index is read-only on the repo.** It never stages, commits, patches,
   pushes, or switches branches. Its only write is to the `file_index` table.
2. **Re-index never bypasses forbidden/secret exclusion.** It reuses the exact
   scanner (`should_skip_path` + `is_forbidden_path` + extension/binary filters);
   no `.env`/`.git`/secret/binary file can enter the index.
3. **Pipewright never auto-creates files.** Stale recovery only re-indexes and
   re-resolves; a missing-on-disk target keeps the existing "create it yourself"
   clarification.
4. **Auto re-index is hard-capped at one attempt per request** and only fires for
   an **explicitly-named** target that exists on disk — never for vague requests,
   never in a loop.
5. **Re-index is atomic.** Delete + re-insert run in one transaction; a failed
   scan leaves the previous index intact, never a half-built one.
6. **Re-index is execution-lock-aware.** It refuses (409) while a run holds the
   project repo lock, so it cannot race a patch/commit.
7. **Post-success refresh happens only after commit**, is best-effort, and must
   never fail the run.
8. **No schema change.** Index age and count are derived from existing
   `file_index` rows.
9. **No new bypass of approval, scope, or no-effective-change guards.** Index
   refresh changes only what files grounding can *see*; it never changes what is
   allowed to be edited, approved, or committed.
