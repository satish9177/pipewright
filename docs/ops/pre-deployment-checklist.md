# Pipewright Pre-deployment Checklist

Use this checklist before running Pipewright in a deployment-like environment.
It is written for the current single-instance MVP architecture.

## A. Required Local Checks

- Confirm the working tree is clean:

```powershell
git status --short
```

- Confirm the deployment branch is `develop`:

```powershell
git branch --show-current
```

- Pull the latest `develop` before deploying:

```powershell
git checkout develop
git pull --ff-only origin develop
```

- Run backend unit tests from the repo root:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit
```

- If frontend code was touched, run the frontend build and typecheck from
  `frontend/`:

```powershell
npm.cmd run build
npx.cmd tsc --noEmit
```

- Optional API tests may call Gemini and can hit rate limits. Run them only when
  `GEMINI_API_KEY` is configured and rate limits allow:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m api -s
```

## B. Required Environment Variables and Config

Required:

- `GEMINI_API_KEY`: Gemini API key used by planner, coder, and triage.

Recommended safe app config:

- `APP_ENV`: deployment environment name. Default: `local`.
- `APP_NAME`: FastAPI app name. Default: `Pipewright`.
- `LOG_LEVEL`: backend logging level. Default: `INFO`.
- `CORS_ALLOWED_ORIGINS`: comma-separated HTTP origins for the frontend.
  Default: `http://localhost:5173,http://localhost:3000`.
- `WS_ALLOWED_ORIGINS`: comma-separated WebSocket origin allowlist.
  Default:
  `http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,http://127.0.0.1:3000`.

Optional LLM provider config:

- `DEFAULT_LLM_PROVIDER`: default provider for all roles. Default: `gemini`.
- `DEFAULT_LLM_MODEL`: default model for all roles. Default:
  `gemini-2.5-flash-lite`.
- `TRIAGE_LLM_PROVIDER` / `TRIAGE_LLM_MODEL`: triage role override.
- `PLANNER_LLM_PROVIDER` / `PLANNER_LLM_MODEL`: planner role override.
- `CODER_LLM_PROVIDER` / `CODER_LLM_MODEL`: coder role override.
- `REVIEWER_LLM_PROVIDER` / `REVIEWER_LLM_MODEL`: reviewer role override.
- `SUMMARY_LLM_PROVIDER` / `SUMMARY_LLM_MODEL`: summary role override.

If these are unset, Pipewright keeps the current Gemini default behavior.

GitHub configuration:

- Project-level GitHub credentials are stored through the project API, not
  environment variables.
- `github_token`, `github_owner`, `github_repo`, and `github_base_branch` are
  project fields.
- Project API responses must not return `github_token`; they return
  `has_github_token` instead.
- Do not put real project GitHub tokens in committed files.

## C. Backend Startup Commands

Local development with reload:

```powershell
venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8001
```

Pipeline runs should avoid `--reload` because file watching can restart the
process while target repositories and backup files are changing:

```powershell
venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8001
```

Use port `8001` for Pipewright so port `8000` remains available for target repo
services.

## D. Frontend Startup and Build Commands

From `frontend/`, build and typecheck:

```powershell
npm.cmd run build
npx.cmd tsc --noEmit
```

Local frontend development:

```powershell
npm.cmd run dev
```

Confirm the frontend origin is present in both `CORS_ALLOWED_ORIGINS` and
`WS_ALLOWED_ORIGINS`.

## E. Smoke Test Checklist

- `GET /health` returns status `ok`.
- Create a project with `POST /projects`.
- If a GitHub token is provided, verify the project response does not contain
  `github_token` and does contain `has_github_token`.
- `GET /projects/{project_id}` returns the project and still does not expose
  `github_token`.
- Repository indexing currently has no public HTTP route. If a deployment adds
  one later, run it before chunk planning and verify indexed files are scoped to
  the project.
- Create a chunked run with `POST /runs/chunked`.
- Review the chunk plan with `GET /runs/{run_id}/chunks`.
- Approve the chunk plan with `POST /runs/{run_id}/chunks/approve`.
- Connect to `/ws/runs/{run_id}/events` and verify Live Log receives replay or
  live events.
- Execute chunks with `POST /runs/{run_id}/chunks/execute`.
- If a chunk requires approval, use
  `POST /runs/{run_id}/chunks/{chunk_number}/approve` or reject it intentionally
  to verify rollback behavior in a safe test repo.
- When all chunks complete, verify a final approval gate exists.
- Final approve with `POST /runs/{run_id}/final-approval/approve`.
- Push/create PR with `POST /runs/{run_id}/push-pr` only when project GitHub
  credentials are configured and the target repo remote is correct.

## F. Deployment Limitations

- Project/repo execution locks are in-process only. They protect a single API
  process, not multiple processes or multiple instances.
- For Cloud Run or GCP, set max instances to `1` until a database-backed or
  Redis-backed distributed lock is added.
- SQLite is local and single-instance oriented. Do not run multiple writers
  against the same deployment database.
- The event bus is in-memory and process-local.
- Live logs are not durable; restarting the backend clears buffered events.
- Background pipeline tasks run inside the API process. A process restart can
  interrupt active work.
- Gemini API tests and provider calls are external and rate-limited.
- GitHub PR creation is non-fatal; a failed PR create can still leave the run
  completed or ready for retry depending on the stored run status.

## G. Rollback and Failure Notes

- Check `/runs/{run_id}` and server logs before retrying a failed operation.
- Do not use `git reset --hard` as automatic recovery in target repositories.
- Do not force push target repositories.
- Do not manually delete `pipewright/*` branches unless following the documented
  stale branch cleanup flow.
- If a target repo is dirty, inspect changed files before retrying execution or
  resume.
- If PR creation fails, confirm project GitHub settings and retry
  `POST /runs/{run_id}/push-pr` only after the target repo and run status look
  safe.
- If WebSocket logs are missing after a backend restart, rely on persisted run,
  chunk, gate, and checkpoint state rather than the live-log buffer.
