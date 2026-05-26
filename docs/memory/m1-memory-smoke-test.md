# Memory M1 Smoke Test

## Prerequisites

- Backend is running.
- Frontend is running.
- A project exists, or you can create a new project with a local repo path.
- `PIPEWRIGHT_ENCRYPTION_KEY` is configured if GitHub token operations are used.

Backend local command:

```powershell
venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8001
```

Frontend local command:

```powershell
cd frontend
npm.cmd run dev
```

## UI Smoke Test

Use this checklist from the Pipewright frontend.

- Open a project.
- Open the Memory tab or page.
- Click **Generate bootstrap suggestions**.
- Confirm generated suggestions are pending.
- Approve one suggestion.
- Confirm the approved suggestion appears in Memory Facts as active.
- Reject one suggestion with a reason.
- Confirm the rejected suggestion does not become active memory.
- Add a manual memory fact.
- Try adding the same active fact again and expect a duplicate error.
- Edit a memory fact.
- Verify a memory fact and confirm `last_verified_at` changes.
- Archive a memory fact with a reason.
- Confirm the archived fact is visible in the management UI.
- Confirm the archived fact is not active.
- Preview the coder memory block.
- Confirm archived, stale, and historical facts are not included.
- Run a small chunked run and confirm the pipeline still works.

## API Smoke Test Commands

Run these from PowerShell. Replace placeholder values before running.

```powershell
$projectId = "<PROJECT_ID>"
$baseUrl = "http://localhost:8001"
```

Generate bootstrap suggestions:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "$baseUrl/api/v1/projects/$projectId/memory/bootstrap-suggestions" `
  -ContentType "application/json" `
  -Body '{"force": false}'
```

List pending suggestions:

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "$baseUrl/api/v1/projects/$projectId/memory/suggestions?status=pending"
```

Approve a suggestion:

```powershell
$suggestionId = "<SUGGESTION_ID>"
Invoke-RestMethod `
  -Method Post `
  -Uri "$baseUrl/api/v1/projects/$projectId/memory/suggestions/$suggestionId/approve"
```

List memory facts:

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "$baseUrl/api/v1/projects/$projectId/memory"
```

Preview coder memory:

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "$baseUrl/api/v1/projects/$projectId/memory/prompt-preview?role=coder"
```

Create a manual memory fact:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "$baseUrl/api/v1/projects/$projectId/memory" `
  -ContentType "application/json" `
  -Body '{
    "content": "Backend uses FastAPI.",
    "category": "stack",
    "scope": "backend",
    "priority": 100,
    "source": "manual"
  }'
```

Archive a memory fact:

```powershell
$memoryId = "<MEMORY_ID>"
Invoke-RestMethod `
  -Method Post `
  -Uri "$baseUrl/api/v1/projects/$projectId/memory/$memoryId/archive" `
  -ContentType "application/json" `
  -Body '{"reason": "Outdated after project update."}'
```

## Expected Outcomes

- Bootstrap suggestions are pending only.
- Approved suggestions become active memory.
- Rejected suggestions do not become memory.
- Prompt preview contains approved active memory.
- Archived memory is not included in prompt preview.
- Duplicate active memory is rejected.
- Secret-like memory is rejected safely without echoing the secret value.
- A small chunked run still works after memory changes.

## Backend Tests

Run targeted Memory M1 backend tests:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\test_memory.py backend\tests\test_memory_prompt_builder.py -v -m unit
venv\Scripts\python.exe -m pytest backend\tests\test_memory_api.py backend\tests\test_memory_bootstrap.py -v -m unit
```

Run the full backend unit suite:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit
```

## Frontend Validation

Run the frontend build:

```powershell
cd frontend
npm.cmd run build
```

## Troubleshooting

### Prompt Preview Is Empty

The project may have no active eligible memory for the selected role. Approve a
suggestion or add a manual active memory fact, then refresh the preview.

### Duplicate Memory Fact Already Exists

An active memory fact with the same normalized content already exists for the
project. Edit the existing fact, archive it with a reason, or use different
wording only if it represents genuinely different guidance.

### Suggestions Are Not Generated

Bootstrap skips suggestions when equivalent active memory already exists or when
there is already a pending duplicate suggestion. It also skips ambiguous repo
signals.

### Backend Stack Is Not Detected

Bootstrap detection is deterministic and requires known manifest or config
evidence. If the project has no recognized dependency/config file, add memory
manually.

### Project-Level Only

Memory M1 stores project-level memory. Monorepo and module-level memory are
deferred to M2.

### GitHub Token Operations Fail

Set `PIPEWRIGHT_ENCRYPTION_KEY` before creating or updating GitHub token
configuration. Memory operations do not need GitHub tokens, but project PR
operations do.
