# Frontend Smoke Checklist

Use this checklist to manually verify the Phase 2D frontend chunked-run flow.
It assumes the current single-instance MVP setup.

## 1. Start Services

From the repo root, start the backend on port `8001`:

```powershell
venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8001
```

From `frontend/`, start the frontend:

```powershell
npm.cmd run dev
```

Open the Vite URL, usually `http://localhost:5173`.

## 2. Project Setup

- Open Projects.
- Create a new project.
- Confirm the project response/UI does not display `github_token`.
- Open the project dashboard.
- Confirm project settings show:
  - repo path
  - test command
  - branch
  - GitHub owner
  - GitHub repo
  - GitHub base branch
  - token configured badge from `has_github_token`
- Configure or update GitHub settings:
  - `github_owner`
  - `github_repo`
  - `github_base_branch`
  - `github_token`
- Save settings.
- Confirm the token input clears after save.
- Confirm the stored token is never displayed.
- Confirm leaving token blank keeps the existing token.

## 3. Create Chunked Run

- On ProjectDashboard, enter a feature description.
- Click `Create Chunked Run`.
- Confirm the app navigates to `/runs/{run_id}`.
- Confirm RunDetail shows the run summary and chunk plan area.
- Confirm status badges render readable labels.

## 4. Review Chunk Plan

- Confirm the chunk plan shows:
  - plan status
  - feature description
  - total chunks
  - current chunk
  - chunk title and description
  - files expected
  - dependencies
  - risk level
  - token estimate
  - human review requirement
  - rationale
- Approve the chunk plan.
- For a safe negative-path check, create a disposable run and reject the chunk
  plan with a reason.

## 5. Execute and Resume Chunks

- After plan approval, click `Execute Chunks`.
- Confirm buttons disable while the request is pending.
- Confirm run/chunk state refreshes after the request.
- Confirm chunk statuses are readable.
- If execution fails or is interrupted, click `Resume Run`.
- Confirm backend errors, including lock conflicts, are visible in the UI.

## 6. Live Log

- During planning/execution, confirm Live Log receives events.
- Confirm Live Log shows:
  - connection status
  - timestamp
  - run or chunk source tag
  - stage/kind label
  - message text
- Confirm the empty state is clear when no live events are available.

## 7. High-Risk Chunk Approval

If a chunk enters `awaiting_chunk_approval`:

- Confirm the chunk card shows high-risk approval controls.
- Confirm the chunk number and status are visible.
- Approve the chunk.
- Confirm the run can continue after approval.
- For a safe negative-path check, reject a high-risk chunk only in a disposable
  repo and confirm the error/rollback state is visible.

## 8. Final Approval

When all chunks complete:

- Confirm RunDetail shows the final approval panel.
- Confirm approve/reject buttons are enabled only when a pending final gate is
  present.
- Confirm a missing final gate shows:
  `Run is awaiting final approval, but no pending final approval gate was found. Refresh the run or resume execution.`
- Approve final approval.
- For a safe negative-path check, reject final approval in a disposable run and
  confirm the rejected status is visible.

## 9. Push and Create PR

After final approval:

- Confirm the GitHub PR panel appears.
- Confirm branch name is visible if available.
- Confirm the panel warns if GitHub token/owner/repo configuration is missing.
- Click `Push and Create PR`.
- Confirm buttons disable while pending.
- Confirm PR URL appears when creation succeeds.
- Confirm PR number appears when returned.
- If push/PR fails, confirm `push_failed` status and error text are visible.
- Retry push/PR only after checking the target repo and project GitHub settings.

## 10. Approval Queue

- Open Approval Queue.
- Confirm pending gates are classified as:
  - Chunk Approval
  - Final Approval
  - Legacy/Pre-merge Approval
- Confirm chunk number appears when available.
- Confirm gate status badges are readable.
- Confirm `View run` navigates to the right RunDetail page.
- Confirm approve/reject behavior is unchanged.

## 11. Validation Commands

Run from `frontend/`:

```powershell
npm.cmd run build
npx.cmd tsc --noEmit
```

Optional backend unit tests from repo root:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit
```

## 12. Pass Criteria

- Project APIs never display `github_token`.
- Project settings can save GitHub PR config safely.
- New feature requests create chunked runs.
- RunDetail supports the chunked lifecycle through PR creation.
- Live Log is readable and does not block core workflow if empty.
- Approval Queue clearly identifies gate types.
- Build and TypeScript validation pass.
