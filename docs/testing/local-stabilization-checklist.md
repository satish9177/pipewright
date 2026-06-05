# Local Stabilization Checklist

Use this checklist before moving to deployment or a new major feature phase.
Run it against a clean local environment after pulling the latest main/develop.

---

## 1. Pre-Test Cleanup

Before running any tests or smokes, clear temporary LLM env vars so leftover
provider settings from a previous session do not affect results:

```powershell
Remove-Item Env:\DEFAULT_LLM_PROVIDER  -ErrorAction SilentlyContinue
Remove-Item Env:\DEFAULT_LLM_MODEL     -ErrorAction SilentlyContinue
Remove-Item Env:\TRIAGE_LLM_PROVIDER   -ErrorAction SilentlyContinue
Remove-Item Env:\TRIAGE_LLM_MODEL      -ErrorAction SilentlyContinue
Remove-Item Env:\PLANNER_LLM_PROVIDER  -ErrorAction SilentlyContinue
Remove-Item Env:\PLANNER_LLM_MODEL     -ErrorAction SilentlyContinue
Remove-Item Env:\CODER_LLM_PROVIDER    -ErrorAction SilentlyContinue
Remove-Item Env:\CODER_LLM_MODEL       -ErrorAction SilentlyContinue
Remove-Item Env:\REVIEWER_LLM_PROVIDER -ErrorAction SilentlyContinue
Remove-Item Env:\REVIEWER_LLM_MODEL    -ErrorAction SilentlyContinue
Remove-Item Env:\SUMMARY_LLM_PROVIDER  -ErrorAction SilentlyContinue
Remove-Item Env:\SUMMARY_LLM_MODEL     -ErrorAction SilentlyContinue
```

Always restart the backend after changing any env var — `pydantic-settings`
reads env at startup, not per-request.

---

## 2. Backend Tests

Run the full backend unit suite. No live API keys required — all SDK calls are
mocked.

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit -p no:cacheprovider
```

Expected: all tests pass. The `-p no:cacheprovider` flag suppresses a cosmetic
Windows cache permission warning.

---

## 3. Frontend Validation

From the `frontend/` directory, run the production build and TypeScript check:

```powershell
npm.cmd run build
npx.cmd tsc --noEmit
```

Expected: build succeeds with no TypeScript errors.

---

## 4. Memory Checks

With backend and frontend running, verify the project memory flow end to end.

### Setup

Start backend:

```powershell
venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8001
```

Start frontend:

```powershell
npm.cmd run dev
```

### Checklist

- [ ] Create or open a project with a local repo path
- [ ] Navigate to the project Memory tab
- [ ] Run bootstrap suggestions — verify suggestions are generated from the repo
- [ ] Approve at least one suggestion — verify it appears as an active memory entry
- [ ] Reject at least one suggestion — verify it is dismissed without saving
- [ ] Add a manual memory entry — verify it saves and appears in the list
- [ ] Edit an existing entry — verify content updates correctly
- [ ] Verify active entries appear in the prompt preview
- [ ] Archive an entry — verify it no longer appears in the prompt preview
- [ ] Re-open the project — verify archived entries are excluded from active list

---

## 5. Chunked Run Checks

Verify the core pipeline flow using a small test repo or a safe local directory.

- [ ] Create a chunked run via `POST /runs/chunked` or the UI
- [ ] Review the chunk plan (`GET /runs/{run_id}/chunks`)
- [ ] Approve the chunk plan
- [ ] Execute a chunk — verify it completes successfully (happy path)
- [ ] **Fail path:** if a chunk fails, verify rollback behavior is safe and the
  run status reflects the failure
- [ ] Resume a failed chunk — verify execution resumes from the correct point
- [ ] Reach the final approval gate — verify the gate appears before push
- [ ] Approve the final gate
- [ ] Push PR (optional — requires `PIPEWRIGHT_ENCRYPTION_KEY` and a configured
  GitHub token in the project)
- [ ] Verify live log stream (`/ws/runs/{run_id}/events`) shows events during
  execution

---

## 6. LLM Provider Checks

### Default Gemini

No provider env vars needed. Run a chunked flow and verify backend logs:

```
[LLM] role=triage   provider=gemini model=gemini-2.5-flash-lite ...
[LLM] role=planner  provider=gemini model=gemini-2.5-flash-lite ...
[LLM] role=coder    provider=gemini model=gemini-2.5-flash-lite ...
```

### FakeProvider Sanity Check (no key needed)

```powershell
$env:DEFAULT_LLM_PROVIDER = "fake"
$env:DEFAULT_LLM_MODEL    = "fake-model"
```

Run the Python provider selection check:

```powershell
venv\Scripts\python.exe -c @"
import asyncio, sys
sys.path.insert(0, '.')
from backend.llm import complete_for_role
from backend.llm.base import LLMRequest, Message
from backend.llm.role_config import Role

async def check():
    req = LLMRequest(
        messages=[Message(role='user', content='ping')],
        model='fake-model',
        extras={'response_text': 'pong'},
    )
    resp = await complete_for_role(Role.TRIAGE, req)
    print(resp.provider, resp.model, resp.text)

asyncio.run(check())
"@
```

Expected output: `fake fake-model pong`

Clear when done:

```powershell
Remove-Item Env:\DEFAULT_LLM_PROVIDER
Remove-Item Env:\DEFAULT_LLM_MODEL
```

### Anthropic / OpenAI / DeepSeek (only if key is available)

Run only when the corresponding API key is configured. See
`docs/llm/m2-provider-smoke-checklist.md` for full per-provider instructions.

- [ ] Anthropic: `ANTHROPIC_API_KEY` present → set `PLANNER_LLM_PROVIDER=anthropic`, run flow, confirm `provider=anthropic` in logs
- [ ] OpenAI: `OPENAI_API_KEY` present → set `PLANNER_LLM_PROVIDER=openai`, run flow, confirm `provider=openai` in logs
- [ ] DeepSeek: `DEEPSEEK_API_KEY` present → set `PLANNER_LLM_PROVIDER=deepseek`, run flow, confirm `provider=deepseek` in logs

Clear provider env vars after each smoke:

```powershell
Remove-Item Env:\PLANNER_LLM_PROVIDER
Remove-Item Env:\PLANNER_LLM_MODEL
```

---

## 7. Safety Checks

- [ ] Scan backend logs for a recent run — confirm no prompt content, file
  content, or response text appears in `[LLM]` lines
- [ ] Confirm no `google.generativeai`, `import anthropic`, or `import openai`
  outside provider adapter files:
  ```powershell
  Select-String -Path backend\pipeline\*.py -Pattern "google\.generativeai|import anthropic|import openai"
  ```
  Expected: no matches
- [ ] Confirm `.env` is listed in `.gitignore` and no API keys are tracked:
  ```powershell
  git status --short
  ```
- [ ] Confirm `PIPEWRIGHT_ENCRYPTION_KEY` is set if testing the push PR flow

---

## 8. Known Local Issues

| Issue | Workaround |
|-------|------------|
| `.pytest_cache` WinError 5 permission warning | Run with `-p no:cacheprovider`; tests still pass |
| Provider unexpectedly resolves to `fake` | Run the pre-test cleanup block above; restart backend |
| Backend does not pick up new env vars | Always restart the backend process after changing env vars |
| `test_local_git` failures on Windows | Fixed; `local_tmp` fixture uses project-local `.pytest_tmp/` directory |

---

## 9. Pass/Fail Recording

Record results after each stabilization run. Add a date and branch.

| Area | Scenario | Result | Notes |
|------|----------|--------|-------|
| Backend tests | Full unit suite | | |
| Frontend | Build + typecheck | | |
| Memory | Bootstrap suggestions | | |
| Memory | Approve/reject suggestion | | |
| Memory | Manual entry add/edit/archive | | |
| Memory | Prompt preview excludes archived | | |
| Chunked run | Happy path end to end | | |
| Chunked run | Fail path + rollback | | |
| Chunked run | Final approval + push PR | | |
| LLM | Gemini default run | | |
| LLM | FakeProvider sanity check | | |
| LLM | Anthropic live (if key available) | | |
| LLM | OpenAI live (if key available) | | |
| LLM | DeepSeek live (if key available) | | |
| Safety | No prompt content in logs | | |
| Safety | Provider SDK import isolation | | |
| Safety | No API keys in git status | | |
