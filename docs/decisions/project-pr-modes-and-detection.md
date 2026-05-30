# Decision: Project PR modes and read-only repo detection

Status: Accepted
Phase: 2F — self-use hardening and demo readiness
Scope: PR #11C (backend / data model / detection foundation only)

## Context

The New Project / Project Settings flow asked new developers for too much up
front: local repo path, test command, default branch, GitHub token, GitHub
owner, GitHub repo, and a staging/base branch. Most of that is only needed for
the manual-token GitHub path, which is the advanced case — not the common one.

We want project setup to start simple and only ask for GitHub credentials when
the user actually opts into a GitHub flow.

## Decision

Introduce an explicit `pr_mode` on each project with three values:

- `local_only` — **the default.** Pipewright works entirely on a local branch
  and creates no remote PR. New projects start here.
- `github_cli` — **the recommended option when available.** Selected when the
  repo has a GitHub remote and the GitHub CLI (`gh`) is installed and
  authenticated. PR creation via `gh` is **future work** and is *not*
  implemented in this PR.
- `manual_token` — **advanced fallback only.** Uses a stored `github_token`
  with owner/repo. Existing projects that already configured a token are
  backfilled into this mode.

GitHub App support is explicitly **future work** and is not a valid `pr_mode`.

### Detection is read-only

A new `POST /projects/detect` endpoint inspects a repo path and recommends a
`pr_mode`. It is strictly read-only:

- It does not save project settings.
- It does not mutate git state.
- It does not create branches, push, or create PRs.

Detection reports: whether the path is a git repo, the git root, whether the
path is the git root, the current branch, the origin URL, whether origin is a
GitHub remote (and parsed owner/repo), whether `gh` is installed and
authenticated, a recommended `pr_mode`, and human-readable warnings.

### recommended_pr_mode logic

`github_cli` is recommended only when **all** of the following hold:

- the repo has a GitHub remote, AND
- `gh` is installed, AND
- `gh` is authenticated.

Otherwise the recommendation is `local_only`. `manual_token` is **never**
auto-recommended — it remains an explicit advanced choice.

## Safety guarantees (unchanged by this PR)

- `github_token` is never returned from API responses; only `has_github_token`
  is exposed. `pr_mode` is safe to expose.
- PR creation still requires final human approval. There is no auto-merge.
- Base-branch safety parity from PR #11B is untouched: forbidden base branches
  (`main`, `master`, `develop`) remain rejected and the default base branch
  stays `pipewright-staging`.
- Subprocess usage for detection follows the existing safe pattern: no
  `shell=True`, list args only, cwd pinned, short timeouts, graceful errors,
  and `gh auth status` output is never logged.

## What this PR does NOT do

- It does not implement GitHub CLI PR creation (`gh pr create`/`gh pr list`).
- It does not change `pr_orchestrator` or push/PR behavior.
- It does not rewrite the frontend Project Settings UI.
- It does not add GitHub App, OAuth, BYOK DB storage, or provider settings.

## Migration

`pr_mode` is added to the `projects` table using the existing lightweight
SQLite migration style (no Alembic). The migration adds the column without a
default so existing rows can be backfilled deterministically:

- projects with `github_token` + `github_owner` + `github_repo` → `manual_token`
- all other existing projects → `local_only`

Fresh databases get `local_only` as the column default from `schema.sql`, and
the project store always sets `pr_mode` explicitly on insert. The backfill is
idempotent and does not overwrite a value an operator later chooses.
