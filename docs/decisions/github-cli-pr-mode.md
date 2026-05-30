# Decision: GitHub CLI PR creation and simplified project setup

Status: Accepted
Phase: 2F — self-use hardening and demo readiness
Scope: PR #11D (PR creation behavior + New Project / Project Settings UI)

## Context

PR #11C added the `pr_mode` field (`local_only`, `github_cli`, `manual_token`),
a read-only `POST /projects/detect` endpoint, and `gh` installed/authenticated
detection — but did not yet act on any of it. PR creation still always used the
manual-token PyGithub path, and project setup still demanded a GitHub token up
front.

This PR makes `pr_mode` drive the modern PR path and reshapes the setup UI so a
new user is never forced to paste a token.

## Decision

### PR creation modes (modern `pr_orchestrator` path only)

PR creation still happens **only after final human approval**, inside the
project repo lock. `pr_orchestrator` dispatches on `project.pr_mode`:

- **`local_only` (default).** No push, no PR, no token required. After final
  approval the run is marked `complete` with a safe, non-failure result that
  includes manual instructions:
  - `git checkout <branch>`
  - `git push origin <branch>`
  - open a PR by hand.
  This is explicitly a successful, no-remote-action outcome — it never sets
  `push_failed`.

- **`github_cli` (recommended when available).** After final approval:
  - validate the base branch (PR #11B safety, unchanged),
  - fail safely **before any push** if `gh` is missing or unauthenticated with:
    *"GitHub CLI is selected, but gh is not installed or not authenticated. Run
    `gh auth login`, then retry PR creation."*,
  - push the approved branch with the existing safe git helper,
  - find an existing open PR with `gh pr list` and reuse it,
  - otherwise create one with `gh pr create`.
  No auto-merge, no force push, no branch deletion. All `gh` errors are
  sanitized before they are stored or returned.

- **`manual_token` (advanced fallback).** The existing PyGithub flow, unchanged:
  same final-approval gate, branch safety, commits-ahead check, and push/PR
  idempotency. Existing token-based projects (backfilled to `manual_token` by
  the PR #11C migration) keep working exactly as before.

### Safe subprocess rules for `gh`

`gh` PR commands live in `backend/git/gh_pr.py` and follow the established
pattern: no `shell=True`, list args only, `cwd` pinned to the project repo,
short timeout, no secrets logged, and every error sanitized via
`sanitize_for_log` before it is surfaced.

### Project setup UI

The New Project and Project Settings screens call `POST /projects/detect` and
surface the detected facts (git repo, git root, current branch, GitHub remote
owner/repo, `gh` installed/authenticated, recommended mode). The PR mode is a
three-way choice:

- **Local only** — the default; no GitHub setup required.
- **GitHub CLI** — shown as *Recommended* when `recommended_pr_mode` is
  `github_cli`.
- **Manual token** — hidden behind an *Advanced* toggle; the token/owner/repo
  fields only appear when `manual_token` is selected or Advanced is open.

The user must still explicitly choose `github_cli`/`manual_token` — detection
only *suggests*. Owner/repo are auto-filled from detection without clobbering
user edits. When `gh` is unavailable the UI shows: *"Install GitHub CLI and run
`gh auth login`, or continue in Local only mode."* The stored token is never
displayed; only `has_github_token` is surfaced.

## Safety guarantees (unchanged)

- PR creation requires final human approval. No auto-merge.
- Base-branch safety parity from PR #11B is intact: `main`/`master`/`develop`
  are rejected; the default base branch stays `pipewright-staging`.
- `github_token` is never returned by the API.
- GitHub CLI mode pastes no token into Pipewright; it relies on `gh` auth.

## What this PR does NOT do

- No GitHub App and no OAuth (both remain **future work**).
- No Electron/Tauri folder picker.
- No deployment, Ollama, provider settings UI, BYOK DB storage, or execution
  modes.
- It does not reintroduce the legacy `/run` endpoint.

## Future work

- A GitHub App installation flow would remove the remaining manual-token path
  entirely and is the intended long-term replacement for `manual_token`.
- Capturing the PR number directly from `gh pr create --json` once a pinned
  minimum `gh` version is guaranteed (today the number is parsed from the
  printed PR URL).
