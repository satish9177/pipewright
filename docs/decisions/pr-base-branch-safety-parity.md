# Decision: GitHub PR safety parity for the modern chunked path

Status: Accepted (Phase 2F, PR #11B)

## Context

A read-only audit found that the modern chunked PR path
(`backend/pipeline/pr_orchestrator.py`) had drifted from the safety behavior
the legacy GitHub client (`backend/github/github_client.py`) already enforced:

- The modern path **defaulted `base_branch` to `main`** when the project had no
  `github_base_branch`, and it did **not** validate forbidden base branches.
  Nothing stopped an automated push/PR from targeting `main`, `master`, or
  `develop`.
- `push_error` could persist **raw exception text** and that field is returned
  by `GET /runs` and `GET /runs/{id}`, creating a small token/PII leak surface.

PR #11A retired the legacy `POST /run` endpoint. This change hardens the modern
PR path only. `/runs/chunked` behavior is otherwise unchanged.

## Decision

- **Centralized base-branch validation.** A single source of truth,
  `backend/github/branch_safety.py`, exposes `validate_base_branch`,
  `DEFAULT_BASE_BRANCH` (`pipewright-staging`), and `FORBIDDEN_BASE_BRANCHES`
  (`main`, `master`, `develop`). Both `github_client.py` and `pr_orchestrator.py`
  use it, so the rule cannot drift between paths again.
- **Forbidden base branches are rejected.** The modern push/PR flow calls
  `validate_base_branch` before any git push or GitHub call. A forbidden base
  branch raises and the run is marked `push_failed` with a clear message —
  **no push and no PR are created.**
- **Safe default, never `main`.** A missing/empty `github_base_branch` resolves
  to `pipewright-staging`. The modern path no longer silently defaults to `main`.
- **`push_error` is sanitized before persistence.** Exception text is run
  through the existing `sanitize_for_log` sanitizer before being stored in
  `pipeline_runs.push_error` and before being re-raised into the HTTP response
  detail, so `GET /runs` and `GET /runs/{id}` cannot surface raw secret-like
  values. (`pipeline_runs` has no `github_token` column, so the token itself was
  never in those payloads; `push_error` was the only leak surface.)

## Approval safety (unchanged)

Final approval requirements are unchanged. PR creation still happens only after
final human approval. There is no auto-merge, no force push, no pushing before
the approved state, and branch/commit checks are not weakened.

## Tests

In `backend/tests/test_pr_orchestrator.py`:

- `test_push_pr_rejects_main_base_branch`,
  `test_push_pr_rejects_master_base_branch`,
  `test_push_pr_rejects_develop_base_branch` (plus a parametrized variant) —
  forbidden base branches raise and mark the run `push_failed`.
- `test_forbidden_base_branch_does_not_push_or_create_pr` — a forbidden base
  branch performs no push and creates no PR.
- `test_push_pr_defaults_to_staging_branch` — a missing `github_base_branch`
  targets `pipewright-staging`.
- `test_push_error_is_sanitized` — token-like exception text is redacted before
  storage and in the re-raised message.
- `test_run_payloads_never_expose_token` — `GET /runs` and `GET /runs/{id}`
  expose no `github_token` and no raw token-like values from a push error.

## Constraints honored

No GitHub CLI, Project Settings UI, local-only mode, deployment, Ollama,
provider settings UI, BYOK DB storage, execution modes, GitHub App, OAuth, or
token-encryption work. `/runs/chunked` behavior is unchanged except for these PR
safety checks. The legacy `POST /run` endpoint is not reintroduced.
