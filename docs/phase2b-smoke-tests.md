# Phase 2B Smoke Tests

Manual smoke tests for the chunked Pipewright flow. Run commands from
`C:\Users\Hp\pipewright` unless noted.

Common setup:

```powershell
venv\Scripts\python.exe -m backend.db.database
uvicorn backend.main:app --host 0.0.0.0 --port 8001
```

Useful helpers:

```powershell
venv\Scripts\python.exe scripts\find_runs.py
venv\Scripts\python.exe scripts\verify_project_config.py proj-13605886
```

## 1-chunk success

Starting repo state: target repo on `main`, clean worktree.

API calls:

```powershell
$body = @{
  project_id = "proj-13605886"
  feature_description = "Add a small one-file feature with tests"
} | ConvertTo-Json
$plan = Invoke-RestMethod -Method Post -Uri http://localhost:8001/runs/chunked -Body $body -ContentType "application/json"
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/$($plan.run_id)/chunks/approve"
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/$($plan.run_id)/chunks/execute"
```

DB verification:

```powershell
venv\Scripts\python.exe scripts\find_runs.py --status awaiting_final_approval
```

Git verification:

```powershell
cd C:\Users\Hp\pipewright-smoke-test
git status --short
git log --oneline -5
```

Expected statuses: all chunks `completed`, run `awaiting_final_approval`,
one final approval gate pending.

Common failure and fix: dirty worktree means review target repo changes, then
manually run `git restore .` and `git clean -fd` only if safe.

## 2-chunk success

Starting repo state: target repo on `main`, clean worktree, no old
`pipewright/*` branch checked out.

API calls: use the same calls as the 1-chunk success with a feature request
large enough to triage into two chunks.

DB verification:

```powershell
venv\Scripts\python.exe scripts\find_runs.py
```

Git verification:

```powershell
git log --oneline -5
```

Expected statuses: chunks 1 and 2 `completed`, run `awaiting_final_approval`,
two local commits named `chunk 1:` and `chunk 2:`.

Common failure and fix: if execution blocks on a stale `pipewright/*` branch,
checkout the configured base branch in the target repo and retry.

## Test failure rollback

Starting repo state: target repo on `main`, clean worktree.

API calls: create and approve a chunked run whose generated tests fail, then:

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/<run_id>/chunks/execute"
```

DB verification:

```powershell
venv\Scripts\python.exe scripts\find_runs.py --status failed
```

Git verification:

```powershell
git status --short
git log --oneline -5
```

Expected statuses: failed chunk `failed`, run `failed`, no commit for the
failed chunk, worktree restored by the patch rollback manifest.

Common failure and fix: if files remain dirty, inspect rollback output and
manually restore the target repo before retrying.

## Dirty repo blocks execution

Starting repo state: target repo has an uncommitted change.

API call:

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/<run_id>/chunks/execute"
```

Expected statuses: run fails preflight; no planner/coder/patch/test starts.

Common failure and fix: review dirty files, then manually clean with
`git restore .` and `git clean -fd` only if safe.

## Stale completed chunk resume

Starting repo state: target repo clean and checked out on
`pipewright/<run-prefix>`.

API call:

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/<run_id>/chunks/resume"
```

Expected statuses: chunks with valid test checkpoints and commits are repaired
to `completed`; pending chunks resume from chunk boundaries.

Common failure and fix: missing commit for a test checkpoint requires manual
recovery. Do not use `git reset --hard`.

## Final approval happy path

Starting state: run is `awaiting_final_approval`.

API call:

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/<run_id>/final-approval/approve"
```

Expected statuses: final gate `approved`, run `final_approved`.

Common failure and fix: if no final gate exists, resume the run so Pipewright
can create the final approval gate after all chunks are completed.

## High-risk chunk approve path

Starting state: one chunk has `requires_human_review=1`.

API calls:

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/<run_id>/chunks/execute"
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/<run_id>/chunks/<chunk_number>/approve"
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/<run_id>/chunks/resume"
```

Expected statuses: chunk pauses at `awaiting_chunk_approval` after tests pass,
approval commits the chunk, resume continues or creates final approval.

Git verification: no chunk commit exists before chunk approval; after approval,
the commit exists and the worktree is clean.

Common failure and fix: if approval says the chunk is not awaiting approval,
check the chunk status and pending chunk gate in the database.

## High-risk chunk reject path

Starting state: chunk is `awaiting_chunk_approval` and worktree contains its
tested uncommitted patch.

API call:

```powershell
$body = @{ reason = "Not safe" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/<run_id>/chunks/<chunk_number>/reject" -Body $body -ContentType "application/json"
```

Expected statuses: chunk gate `rejected`, chunk `rejected`, run `failed`, no
chunk commit, worktree clean after rollback.

Common failure and fix: if rollback cannot clean the worktree, Pipewright
returns an error and leaves state for manual recovery.

## Push + PR creation

Starting state: run `final_approved`, target repo clean on
`pipewright/<run-prefix>`, GitHub fields configured.

API call:

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/<run_id>/push-pr"
```

Expected statuses: branch pushed, one GitHub PR created, run `complete`,
`pr_url` and `pr_number` stored.

Common failure and fix: missing GitHub config fields can be inspected with
`venv\Scripts\python.exe scripts\verify_project_config.py <project_id>`.

## Push-pr idempotency

Starting state: run already has `pr_url`, or remote branch/PR already exists.

API call:

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/<run_id>/push-pr"
```

Expected statuses: existing PR metadata is returned; no duplicate PR is
created.

Common failure and fix: if status is not `final_approved` or `push_failed`,
complete final approval before calling push-pr.
