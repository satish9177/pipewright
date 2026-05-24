# Phase 2B Demo Flow

Ten-minute happy-path script for a chunked Pipewright demo.

## 1. Prepare the smoke repo

```powershell
cd C:\Users\Hp\pipewright
venv\Scripts\python.exe scripts\reset_smoke_repo.py --repo-path C:\Users\Hp\pipewright-smoke-test --yes
venv\Scripts\python.exe scripts\verify_project_config.py proj-13605886
```

Expected: repo path exists, is a Git root, current branch is `main`, and
GitHub fields are present if the demo will create a PR.

## 2. Run the backend

```powershell
cd C:\Users\Hp\pipewright
venv\Scripts\python.exe -m backend.db.database
uvicorn backend.main:app --host 0.0.0.0 --port 8001
```

## 3. Create a chunked run

```powershell
$body = @{
  project_id = "proj-13605886"
  feature_description = "Add a small validated feature with tests"
} | ConvertTo-Json
$plan = Invoke-RestMethod -Method Post -Uri http://localhost:8001/runs/chunked -Body $body -ContentType "application/json"
$runId = $plan.run_id
$runId
```

Expected: `chunk_plan_status` is `awaiting_approval`.

## 4. Approve the chunk plan

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/$runId/chunks/approve"
```

Expected: `chunk_plan_status` is `approved`.

## 5. Execute chunks

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/$runId/chunks/execute"
```

Expected: chunks execute in order. If a high-risk chunk pauses, approve it:

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/$runId/chunks/<chunk_number>/approve"
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/$runId/chunks/resume"
```

Expected after all chunks: run status `awaiting_final_approval`.

## 6. Final approval

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/$runId/final-approval/approve"
```

Expected: run status `final_approved`.

## 7. Push and create PR

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8001/runs/$runId/push-pr"
```

Expected final DB state:

```powershell
venv\Scripts\python.exe scripts\find_runs.py
```

The run should be `complete`, with `branch_name`, `pr_url`, and `pr_number`
stored. The target repo should be clean on `pipewright/<run-prefix>`.
