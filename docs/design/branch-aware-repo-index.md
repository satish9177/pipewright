# Branch-Aware Repo Index And Flexible Start Branch Audit

Status: #34A docs-only audit and target design.

This document is intentionally not an implementation plan for this slice. #34A
does not change backend behavior, frontend behavior, database schema, route
contracts, tests, or package files. It records the current behavior and proposes
later slices for branch-aware index freshness and cleaner start-branch handling.

## Summary

Pipewright already has meaningful repo-index refresh behavior. #34 is not
"build reindex from zero." The remaining trust gap is narrower: the index is a
single persisted cache for a project, and the cache does not remember which
checkout produced it. A fresh-looking `indexed_at` only says when rows were
written. It does not say whether the current branch, `HEAD`, or dirty tree still
matches the checkout that was scanned.

The branch side has a similar shape. Execution already creates an isolated
`pipewright/<run-id>` branch from the current checkout in the target repo. That
means "start from the user's current branch" mostly exists implicitly. The
uncomfortable part is the lifecycle around it: Pipewright can leave `HEAD` on a
run branch after a completed run, while the stale `pipewright/*` guard tells the
operator to return to a configured base branch instead of treating the current
branch as an explicit start context.

The target model should be:

- current checkout branch = start context and fork point
- `pipewright/<run-id>` = isolated run branch and only local commit target
- configured safe base branch = PR target only
- start branch is never mutated by Pipewright
- run branch is never the protected base branch
- PR base validation stays centralized in `backend/github/branch_safety.py`

## Evidence Inspected

Repo index and consumers:

- `backend/repo/repo_indexer.py`
- `backend/db/schema.sql`
- `backend/routes/projects.py`
- `backend/routes/chunks.py`
- `backend/pipeline/triage.py`
- `backend/pipeline/report_analyzer.py`
- `backend/pipeline/plan_path_grounding.py`
- `backend/pipeline/file_scope_intent.py`
- `backend/pipeline/file_alias_grounding.py`
- `backend/pipeline/chunked_orchestrator.py`
- `frontend/src/api/client.ts`
- `frontend/src/components/ProjectSettingsPanel.tsx`
- `frontend/src/pages/ProjectDashboard.tsx`

Branch and PR behavior:

- `backend/git/local_git.py`
- `backend/pipeline/chunked_orchestrator.py`
- `backend/pipeline/pr_orchestrator.py`
- `backend/github/branch_safety.py`
- `backend/git/pr_preflight.py`
- `backend/git/repo_inspect.py`

Related tests and docs:

- `backend/tests/test_repo_indexer.py`
- `backend/tests/test_project_reindex.py`
- `backend/tests/test_chunk_routes_stale_reindex.py`
- `backend/tests/test_post_commit_reindex.py`
- `backend/tests/test_pr_orchestrator.py`
- `docs/architecture/repo-index-refresh.md`
- `docs/testing/repo-index-refresh-smoke.md`
- `docs/decisions/pr-base-branch-safety-parity.md`
- `docs/decisions/project-pr-modes-and-detection.md`
- `docs/troubleshooting.md`
- `docs/phase2b-smoke-tests.md`

## Existing Repo Index Behavior

`backend/repo/repo_indexer.py` is the deterministic zero-AI indexer. It scans a
target repo on demand, extracts lightweight metadata, and stores rows in
SQLite's `file_index` table.

Current scanner behavior:

- uses `Path.rglob("*")` over the live working tree
- skips heavy/generated directories in `SKIP_NAMES`
- skips files over 1 MB
- skips forbidden names through `is_forbidden_path`
- indexes supported text/code/docs extensions plus `Dockerfile`,
  `.env.example`, and `.env.sample`
- skips unreadable and NUL-containing binary-like files
- records path, file type, imports, mtime-derived `last_modified`, token
  estimate, line count, size, and `indexed_at`

Current store behavior:

- `save_file_index(project_id, files)` deletes all rows for the project and
  inserts the rebuilt rows inside one `engine.begin()` transaction.
- `build_repo_index(project_id, target_repo_path)` resolves the repo path, scans
  the live tree, and replaces the project's rows.
- `ensure_repo_indexed(project_id, target_repo_path)` only checks whether any
  rows exist for the project. If count is zero it builds the index; otherwise it
  returns `already_indexed`.
- `get_project_index_status(project_id)` reads `COUNT(*)` and
  `MAX(indexed_at)` only. It does not scan the repo.

The schema in `backend/db/schema.sql` currently has:

```sql
CREATE TABLE IF NOT EXISTS file_index (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    path TEXT NOT NULL,
    file_type TEXT NOT NULL,
    summary TEXT,
    key_imports TEXT,
    last_modified DATETIME,
    token_estimate INTEGER DEFAULT 0,
    line_count INTEGER DEFAULT 0,
    size_bytes INTEGER DEFAULT 0,
    indexed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, path)
);
```

That table is project-scoped and path-unique. It has no branch name, detached
HEAD marker, `HEAD` SHA, dirty-state digest, repo path identity, or index
version identity.

Small storage note: `last_modified` is declared as `DATETIME`, but the indexer
currently stores an ISO string produced from the file mtime. That mismatch is
not a #34A blocker.

Current routes and UI:

- `GET /projects/{project_id}/index` returns read-only index status:
  `project_id`, `files_indexed`, `indexed_at`, and `status`.
- `POST /projects/{project_id}/reindex` forces a full rebuild through
  `build_repo_index`, acquires the project repo lock, returns 409 when a run
  holds the lock, and does not require a clean worktree.
- Project Settings displays "Repository index", file count, last indexed time,
  and a "Re-index repository" button.
- Clarification copy in `ProjectDashboard` already tells users that candidates
  are based on the current repo index and to re-index after recent file changes.

Current reindex behavior from the prior repo-index-refresh work:

- Manual project reindex exists (#19B).
- Read-only index status exists (#19B).
- Explicit target stale recovery exists (#19C): when an explicit target is
  missing from the index but exists on disk, the chunked route reindexes once
  and re-resolves. It is hard-capped and explicit-target-only.
- Frontend index status and manual reindex UI exist (#19D).
- Post-commit index refresh exists (#19E): after a chunk commit completes,
  `chunked_orchestrator` refreshes the index best-effort so later chunks/runs
  can see files Pipewright created, deleted, or renamed.

What remains missing: freshness identity. The index may be rebuilt recently and
still represent a different branch, a different `HEAD`, or a dirty tree state
that no longer matches the user's current checkout.

## RepoFingerprint Name Collision

`backend/repo/repo_fingerprint.py` already defines `RepoFingerprint` and
`build_repo_fingerprint`. That module is not about repo-index freshness. It is
the deterministic memory/repo-reality signal extractor used for semantic
database-engine detection.

Current meaning:

- reads capped manifest/config files
- detects database engine signals such as PostgreSQL, MySQL, MongoDB, or SQLite
- returns `RepoFingerprint(db, db_signals, db_ambiguous)`
- avoids raw content evidence and avoids secret values

#34 must not reuse `RepoFingerprint`, `build_repo_fingerprint`, or
`backend/repo/repo_fingerprint.py` for freshness. Reusing that name would make
two unrelated concepts look interchangeable: semantic repo-reality detection and
checkout/index identity.

Recommended future names:

- `WorkingTreeFingerprint`
- `IndexFreshnessStatus`
- `IndexFreshnessComparison`

Recommended future modules:

- `backend/repo/index_freshness.py`
- `backend/repo/working_tree_fingerprint.py`

## Why `indexed_at` Is Insufficient

`indexed_at` is wall-clock freshness only. It answers "when did SQLite insert
these rows?" It does not answer "what checkout did these rows describe?"

Examples:

- A 10-second-old index can be wrong if the user switched from `feature/a` to
  `feature/b` immediately after indexing.
- A 10-second-old index can be wrong if the user pulled, merged, rebased, or
  amended `HEAD`.
- A 10-second-old index can be wrong if a file was created, deleted, or renamed
  outside Pipewright after the scan.
- A dirty-tree index can be "fresh" for the dirty files that existed at scan
  time and stale for dirty files changed immediately afterward.

The current `file_index` schema is branch-blind. Its uniqueness constraint is
`UNIQUE(project_id, path)`, not `UNIQUE(project_id, branch, path)` or anything
similar. That is the correct shape for a single current-checkout index, but it
means a freshness comparison needs separate identity metadata.

## Current Branch Behavior

Run execution creates the run branch in
`backend/pipeline/chunked_orchestrator.py`.

The relevant sequence in `_execute_approved_chunks_locked` is:

1. validate target repo and require a clean worktree
2. apply DB memory-conflict policy before branch mutation
3. compute `branch_name = f"pipewright/{run_id[:8]}"`
4. call `local_git.assert_not_on_stale_pipewright_branch(target_repo_path, run_id)`
5. call `local_git.create_or_checkout_branch(branch_name, target_repo_path)`
6. execute pending chunks on that branch

`backend/git/local_git.py` implements `create_or_checkout_branch` as:

- if the branch already exists, `git checkout <branch>`
- otherwise, `git checkout -b <branch>`

There is no explicit start point argument. For a new run branch, Git creates it
from the target repo's current `HEAD`. Therefore flexible start branch behavior
mostly exists implicitly: whatever branch the user has checked out when approved
execution begins is the fork point, unless the stale Pipewright branch guard
blocks first.

The stale Pipewright branch guard:

- reads `git rev-parse --abbrev-ref HEAD`
- returns when detached or when the current branch does not start with
  `pipewright/`
- allows the expected current run branch `pipewright/<run-id>`
- rejects any other `pipewright/*` branch with a message instructing the user to
  checkout the configured base branch before starting a new run

This guard is valuable, but the current UX is awkward for #34's desired model.
After a run completes, PR creation and local-only completion can leave `HEAD` on
`pipewright/<run-id>`. The next run may be created and planned while the target
repo is still on that old run branch, then execution blocks later on the stale
branch guard. The guard prevents the final accidental commit target, but the
operator experience is still "Pipewright told me to go back to a fixed base
branch," not "choose your start branch intentionally."

The practical risk the guard is defending against is real: if a future path
bypassed the guard, or if planning/indexing already consumed the old run branch
before the guard fired, a new `pipewright/<new-run-id>` branch could be based on
the previous run branch rather than the user's intended start branch.

PR behavior is separate. `backend/pipeline/pr_orchestrator.py` pushes the
approved run branch only after final approval, verifies the local branch exists,
checks out the run branch if needed, verifies clean worktree, checks commits
ahead of the configured base branch, and then pushes/creates or finds the PR.
The PR base branch is resolved and validated through
`backend/github/branch_safety.py`.

Two branch-related failure modes deserve explicit names for later slices:

- Planning-to-execution start-context drift: run creation may validate freshness
  and build an approved chunk plan against branch X. If the user switches the
  target repo to branch Y before approved execution, `git checkout -b
  pipewright/<run-id>` forks from Y. The approved plan and `files_expected` may
  no longer match the execution context. Future #34D should own start-context
  drift verification immediately before creating or checking out
  `pipewright/<run-id>`. This is a correctness check, not copy polish.
- Fork-point vs PR-base divergence: PR diff/ahead checks conceptually compare
  the configured base branch to `pipewright/<run-id>` (`base..run_branch`). If
  the user starts from a branch that has commits not present in the configured
  PR base, the eventual PR can include unrelated commits. Example: the user
  starts from `main`, the PR base is `pipewright-staging`, and
  `pipewright-staging` is behind `main`. This is why "protected branch as fork
  point" can be technically safe while still deserving a warning when the start
  branch and PR base diverge. #34A should not change `branch_safety.py` policy.

## Desired Branch Model

The desired branch model should use precise words:

- Start branch: the user's current checkout at run creation or approved
  execution time, depending on the future slice's chosen enforcement point.
- Run branch: `pipewright/<run-id>`, created from the start context and used for
  all Pipewright commits.
- PR base branch: the configured safe branch used only as the pull request
  target.

Rules:

- The current branch is the start context and fork point.
- The run branch is isolated and named `pipewright/<run-id>`.
- The configured safe base branch is the PR target, not necessarily the start
  branch.
- Pipewright must never commit to or otherwise mutate the start branch.
- The run branch must never be a protected base branch.
- PR base protections should remain controlled by
  `backend/github/branch_safety.py` unless a future audit proves otherwise.

This lets a user intentionally start from `feature/already-in-progress` while
still opening the eventual PR against `pipewright-staging` or another configured
safe integration branch.

Flexible start branch does not mean Pipewright will execute on a dirty tree.
The existing `_validate_target_repo(..., require_clean=True)` preflight and the
other clean-tree guards still apply before execution, patch, commit, and PR
paths; users may need to commit or stash local changes before approved execution
even when indexing can reflect dirty files.

## Protected Branch Matrix

| Branch role | Protected branch policy |
| --- | --- |
| Protected branch as fork point/start branch | Allowed, maybe warn. Starting from `main`, `master`, or `develop` is read-only at the start branch level if all commits land on `pipewright/<run-id>`. A warning is useful because it may indicate the operator has not selected an intended feature branch. |
| Protected branch as commit target | Forbidden. Pipewright commits must land only on `pipewright/<run-id>`, never directly on a protected branch. |
| Protected branch as PR base | Forbidden by existing `branch_safety.py` validation for automated PR paths. `main`, `master`, and `develop` are rejected; missing config defaults to `pipewright-staging`. |
| Run branch | Always `pipewright/<run-id>`, never the start branch and never the protected base. |

## Recommended Index Freshness Design

Use a single current-checkout index stamped with freshness identity. Do not
introduce a branch-keyed multi-index cache for #34.

Rationale:

- The product behavior today is "index what is currently checked out."
- Most consumers expect one project index, not an index selector.
- A branch-keyed cache would need invalidation rules for branch deletion,
  rebases, amended commits, untracked files, and dirty trees.
- The immediate trust issue is detecting mismatch, not serving many historical
  indexes.

Recommended future fingerprint fields:

- `repo_path_resolved`: canonical resolved target repo path.
- `branch_name`: current branch name.
- `branch_is_detached`: boolean for detached `HEAD`.
- `detached_head_label`: deterministic display value when detached, for example
  `DETACHED@<short-sha>`.
- `head_sha`: `git rev-parse HEAD`.
- `dirty_digest`: stable digest of `git status --porcelain -uall` output.
- `dirty_files_count`: optional display field derived from the same status
  output.
- `index_row_count`: optional sanity check against the stored status count.
- `captured_at`: timestamp for display, not correctness.

Detached HEAD handling must be deterministic. `git branch --show-current`
returns an empty string in detached state, while
`git rev-parse --abbrev-ref HEAD` can return `HEAD`. Future code should not
store an empty branch name and call it fresh. It should represent detached state
explicitly and compare by `head_sha` plus dirty digest.

What the fingerprint should not include:

- no full content hash of the repository
- no Merkle tree
- no mtime-based correctness claim
- no per-read heavy scan
- no embedding/vector identity
- no provider or LLM metadata

The comparison should be direct: compute current working-tree fingerprint, load
the last index fingerprint, compare the identity fields, and classify the index
as current, stale, unknown, or missing.

TOCTOU note for future implementation: the stored freshness identity must
describe the checkout that actually produced the scanned rows. Capturing a
fingerprint only before the scan can be wrong if `HEAD` or dirty state changes
while scanning. Safer options are to capture after the scan, or capture before
and after the scan and mark the index unknown/stale when they differ.

#34B persistence decision: store one project-level snapshot in a new
`project_index_fingerprints` table keyed by `project_id`. This avoids bloating
every `file_index` row and preserves `file_index` uniqueness as
`UNIQUE(project_id, path)`. Unlike the earlier #19 index-refresh work, #34B
uses an additive schema change because the freshness identity is durable
metadata for the whole current-checkout index.

## Dirty-State Policy

Separate two concepts:

- Index freshness dirty state: a warning and freshness signal.
- Execution dirty tree: a safety guard before patching, retrying, scope
  expansion, commit, or final approval.

Re-index currently does not require a clean tree, and that should remain true.
The index is a cache of the current on-disk checkout, including supported
uncommitted/untracked files. Dirty state belongs in the freshness identity so
Pipewright can say, "this index was built against a different dirty tree."

Execution is different. The existing clean-tree guards protect user work and
rollback semantics. #34 must not weaken these guards:

- `_validate_target_repo(..., require_clean=True)` before approved execution
- `local_git.ensure_clean_worktree` before final approval summary
- live clean-tree checks before retry and scope-expansion retry paths
- clean-tree checks before push/PR verification

If a future implementation exposes stale index status at run creation, it should
not reinterpret "dirty tree" as safe to patch. Dirty can be acceptable for
indexing and unacceptable for execution at the same time.

## Untracked And `.gitignore` Behavior

The current indexer walks the filesystem independently of Git. It does not call
`git ls-files`, `git check-ignore`, or any `.gitignore`-aware API. That means:

- supported untracked files can be indexed
- ignored files can be indexed if they are not blocked by `SKIP_NAMES`,
  forbidden-path rules, size limits, binary detection, or extension filtering
- files skipped by `SKIP_NAMES` may differ from files ignored by `.gitignore`
- reindexing a dirty worktree can intentionally pick up uncommitted files

This is not necessarily wrong for local-first grounding, but the behavior should
be described honestly. The freshness fingerprint's dirty digest should use
`git status --porcelain -uall`, because that captures tracked and untracked Git
state. It will not make the index itself `.gitignore`-accurate.

`.gitignore`-accurate indexing should be deferred unless a future slice
explicitly takes it on. If implemented later, prefer Git's own structured
answers over reimplementing ignore parsing.

## Where Freshness Should Be Checked

Recommended check points:

- Authoritative check at run creation, before triage/planning/grounding trusts
  cached `file_index` rows.
- On-demand display/status check in the project dashboard or settings panel.
- Explicit refresh/reindex action that updates both the file rows and freshness
  identity.
- Keep the existing explicit-target stale path safety net from #19C.

Do not run Git freshness subprocess checks on every repo grounding read unless
later profiling or correctness evidence shows it is necessary. Consumers such as
`get_relevant_files`, plan path grounding, alias grounding, and file-scope
intent should stay cheap and deterministic. The authoritative boundary should
be run creation, with an on-demand status path for UI visibility.

Suggested future run-creation behavior:

- If there is no index, tell the operator to index or run an explicit refresh.
- If the index fingerprint matches the current checkout, proceed.
- If the index fingerprint differs, surface a clear stale-index state with the
  old branch/HEAD/dirty summary and current branch/HEAD/dirty summary.
- Do not silently proceed. Prefer reusing the acknowledgement-gate style:
  surface a reindex action, and optionally an explicit acknowledge-and-proceed
  path if the product chooses to allow override.
- Do not invent a complex new waiting state unless later UX proves necessary.
- Do not silently switch branches.
- Do not mutate the start branch.

## Alternatives And Deferrals

| Alternative | Evaluation | Decision for #34 |
| --- | --- | --- |
| Branch-keyed multi-index cache | Attractive for fast branch switching, but it adds invalidation complexity for rebases, amended commits, deleted branches, dirty trees, and untracked files. | Defer. Use one current-checkout index with freshness identity first. |
| Git worktree isolation | Strong isolation story, but high operational and cleanup complexity. It also changes how users reason about local files. | Defer. Keep current local-first working tree model. |
| Auto-restore HEAD after run | Could reduce stale-run-branch surprises, but it is a Git mutation after execution and needs careful failure/dirty-state handling. | Defer. First document/start-branch state clearly and surface warnings. |
| Auto-rebase or auto-sync start branch onto PR base | Dangerous and outside current human approval semantics. It mutates branch history or content in ways Pipewright should not guess. | Defer. Never automatic in #34. |
| File watcher as correctness mechanism | Useful for UX, not correctness. Watchers are OS-specific, can miss events, and create background mutation paths. | Defer. Explicit freshness checks are the correctness mechanism. |
| `.gitignore`-accurate indexing | More faithful to Git, but changes current behavior around untracked/ignored files and needs tests. | Defer unless explicitly scoped. |
| Per-read freshness checking | Maximally defensive but expensive and noisy. It would turn every grounding read into Git subprocess work. | Defer. Check at run creation and status/refresh boundaries. |

## Proposed Future Implementation Slices

1. #34A docs-only branch/index trust audit.
2. #34B working-tree/index freshness fingerprint foundation, including the
   `project_index_fingerprints` persistence table.
3. #34C stale index detection and run-creation surfacing.
4. #34D start-branch/run-branch safety cleanup, explicitly covering
   start-context drift verification before `checkout -b`,
   `HEAD`-left-on-run-branch lifecycle, and stale `pipewright/*` guard message
   rewrite. Keep the current guard behavior until a later implementation
   intentionally changes it.
5. #34E frontend stale-index warning + refresh action.
6. #34F smoke docs/manual checklist.

## Non-Goals For #34A

- no schema migration
- no route contract change
- no frontend UI change
- no backend runtime behavior change
- no new dependency
- no branch checkout behavior change
- no PR base policy change
- no change to clean-tree execution guards

## Open Questions For Later Slices

1. Should the start-branch fingerprint be captured at run creation or at
   approved execution? Capturing at run creation protects planning/grounding,
   but execution still needs to verify the start context has not drifted before
   `git checkout -b`.
2. What exact override policy should stale index handling use? The baseline
   recommendation is "do not silently proceed"; prefer a reindex action plus an
   acknowledgement-style override only if the product intentionally allows it.
   Avoid a new waiting state unless later UX proves it is needed.
3. Should starting from a protected branch be a warning only, or require an
   explicit acknowledgement? It is safe if Pipewright never commits there, but it
   may still indicate operator confusion or fork-point/PR-base divergence.
4. Should Pipewright capture the human-selected start branch in
   `pipeline_runs`? That would improve auditability but requires a schema
   change and is outside #34A.
5. Should #34D update stale Pipewright branch guidance from "checkout the
   configured base branch" to "checkout the branch you want to start from"?
   That better matches the desired model.
6. How should #34C wire `project_index_fingerprints` into reindex/run-creation
   flows without adding per-read freshness checks in hot grounding paths?
