# LLM-M1 Provider Smoke Test

## Prerequisites

- Backend env configured (`.env` or equivalent environment variables)
- `GEMINI_API_KEY` set if running against the real Gemini provider
- `PIPEWRIGHT_ENCRYPTION_KEY` set if the run includes a GitHub PR push step
- Backend running if performing API or UI smoke
- Frontend running if performing UI smoke

Start backend:

```powershell
venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8001
```

Start frontend:

```powershell
cd frontend
npm.cmd run dev
```

## Unit Test Commands

Run LLM abstraction layer tests:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\test_llm_base.py backend\tests\test_llm_registry.py backend\tests\test_llm_role_config.py backend\tests\test_llm_fake_provider.py backend\tests\test_llm_gemini_provider.py -v -m unit
```

Run pipeline role migration and guard tests:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\test_triage.py backend\tests\test_planner.py backend\tests\test_coder.py backend\tests\test_guards_provider_isolation.py -v -m unit
```

Run the full backend unit suite:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit
```

If the `.pytest_cache` permission warning appears on Windows (WinError 5), disable
the cache provider:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit -p no:cacheprovider
```

## FakeProvider Sanity Check

Set triage to use the fake provider:

```powershell
$env:TRIAGE_LLM_PROVIDER = "fake"
$env:TRIAGE_LLM_MODEL = "fake-model"
```

Run a quick end-to-end provider selection check from a Python shell:

```powershell
venv\Scripts\python.exe -c @"
import asyncio, os, sys
sys.path.insert(0, '.')

from backend.llm import complete_for_role
from backend.llm.base import LLMRequest, Message
from backend.llm.role_config import Role

async def check():
    req = LLMRequest(
        messages=[Message(role='user', content='ping')],
        model='fake-model',
        extras={'response_text': 'fake fake-model ok'},
    )
    resp = await complete_for_role(Role.TRIAGE, req)
    print(resp.provider, resp.model, resp.text)

asyncio.run(check())
"@
```

Expected output:

```
fake fake-model ok
```

Clear env vars when done:

```powershell
Remove-Item Env:\TRIAGE_LLM_PROVIDER
Remove-Item Env:\TRIAGE_LLM_MODEL
```

## Default Gemini Smoke

Ensure no LLM provider overrides are set:

```powershell
Remove-Item Env:\DEFAULT_LLM_PROVIDER -ErrorAction SilentlyContinue
Remove-Item Env:\DEFAULT_LLM_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:\TRIAGE_LLM_PROVIDER -ErrorAction SilentlyContinue
Remove-Item Env:\TRIAGE_LLM_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:\PLANNER_LLM_PROVIDER -ErrorAction SilentlyContinue
Remove-Item Env:\PLANNER_LLM_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:\CODER_LLM_PROVIDER -ErrorAction SilentlyContinue
Remove-Item Env:\CODER_LLM_MODEL -ErrorAction SilentlyContinue
```

Confirm `GEMINI_API_KEY` is present:

```powershell
$env:GEMINI_API_KEY
```

Run a small chunked project flow through the UI or API (create project, generate
triage, approve chunk plan, execute one chunk).

Confirm backend logs contain `[LLM]` lines for each role:

```
[LLM] role=triage provider=gemini model=gemini-2.5-flash-lite input_tokens=... output_tokens=... finish_reason=stop run_id=...
[LLM] role=planner provider=gemini model=gemini-2.5-flash-lite input_tokens=... output_tokens=... finish_reason=stop run_id=...
[LLM] role=coder provider=gemini model=gemini-2.5-flash-lite input_tokens=... output_tokens=... finish_reason=stop run_id=...
```

## Role Override Smoke

Set only triage to use the fake provider in a unit/dev context:

```powershell
$env:TRIAGE_LLM_PROVIDER = "fake"
$env:TRIAGE_LLM_MODEL = "fake-model"
```

Run role config validation tests to confirm planner and coder still resolve to the
default (Gemini) while triage resolves to fake:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\test_llm_role_config_pipeline.py -v -m unit
```

Do not run a real pipeline with fake provider unless test fixtures supply valid
JSON responses via `request.extras["response_text"]`. FakeProvider returns a
hardcoded string that will fail JSON parsing in triage/planner/coder.

Clear when done:

```powershell
Remove-Item Env:\TRIAGE_LLM_PROVIDER
Remove-Item Env:\TRIAGE_LLM_MODEL
```

## Guard Checks

Scan pipeline files for prohibited SDK imports or `print()` calls:

```powershell
Select-String -Path backend\pipeline\*.py -Pattern "google\.generativeai|genai\.|print\("
```

Expected result: no matches in `triage.py`, `planner.py`, or `coder.py`.

Confirm the Gemini SDK import exists only in the provider adapter:

```powershell
Select-String -Path backend\llm\providers\gemini.py -Pattern "google\.generativeai"
```

Expected result: at least one match in `gemini.py`.

Run the full isolation guard test suite:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\test_guards_provider_isolation.py -v -m unit
```

## Manual UI Smoke

- Start backend and frontend.
- Create or open a project with a local repo path.
- Generate or approve project memory if needed.
- Create a chunked run for a small feature request.
- Review and approve the chunk plan.
- Execute the chunks.
- Confirm the final approval UI still works.
- Optionally push a PR (requires `PIPEWRIGHT_ENCRYPTION_KEY` and a configured
  GitHub token).
- Check backend logs for `[LLM]` lines showing `role`, `provider`, and `model`
  fields on every call.

## Troubleshooting

### Missing GEMINI_API_KEY

```
ProviderConfigurationError: ... key ...
```

Set `GEMINI_API_KEY` in your `.env` file or shell environment before starting the
backend. The key is read at startup from `backend/config/keys.py` (`Settings`).

### Unknown Provider

```
UnsupportedProviderError: Unsupported LLM provider: <name>
```

The provider name set in env (e.g. `TRIAGE_LLM_PROVIDER=anthropic`) is not
registered in `default_registry()`. Only `gemini` and `fake` are registered in
LLM-M1. Check for typos.

### Unsupported Model

```
UnsupportedModelError: Provider gemini does not support model <name>
```

The model name set in env does not match the models declared in
`GeminiProvider.supports_model`. Check the model name against the Gemini provider
adapter.

### FakeProvider Returns Invalid JSON in Real Role Path

FakeProvider returns a plain string (`"fake response"` by default). If triage,
planner, or coder calls FakeProvider without an `extras["response_text"]` value
that is valid JSON, the role will raise `ValidationError` or `json.JSONDecodeError`
and trigger the retry path before failing.

Do not set `TRIAGE_LLM_PROVIDER=fake` (or equivalent) in a real pipeline run
unless the test scaffold supplies valid JSON through the extras mechanism.

### `.pytest_cache` Permission Warning on Windows

```
PytestCacheWarning: could not create cache path ... [WinError 5] Access is denied
```

This is a cosmetic warning from a corrupted `.pytest_cache` directory. Tests still
pass. Run with `-p no:cacheprovider` to suppress:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit -p no:cacheprovider
```

### PIPEWRIGHT_ENCRYPTION_KEY Required for GitHub Token Storage

If a pipeline run reaches the PR push step and fails with an encryption error, set
`PIPEWRIGHT_ENCRYPTION_KEY` in your environment. LLM provider configuration does
not require this key; only the GitHub token storage path does.
