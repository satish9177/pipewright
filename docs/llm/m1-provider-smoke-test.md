# LLM-M1 Provider Smoke Test

## Prerequisites

- Backend env configured (`.env` or equivalent environment variables)
- `GEMINI_API_KEY` set if running against the Gemini provider (required for default path)
- `ANTHROPIC_API_KEY` set if running against the Anthropic provider (optional)
- `OPENAI_API_KEY` set if running against the OpenAI provider (optional)
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

Run LLM abstraction layer and all provider tests:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\test_llm_base.py backend\tests\test_llm_registry.py backend\tests\test_llm_role_config.py backend\tests\test_llm_fake_provider.py backend\tests\test_llm_gemini_provider.py backend\tests\test_llm_anthropic_provider.py backend\tests\test_llm_openai_provider.py backend\tests\test_llm_deepseek_provider.py -v -m unit
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

## Anthropic Provider Smoke

Requires `ANTHROPIC_API_KEY`. Set planner to use Anthropic:

```powershell
$env:PLANNER_LLM_PROVIDER = "anthropic"
$env:PLANNER_LLM_MODEL = "claude-3-5-haiku-latest"
```

Run a small chunked flow (triage and coder remain on Gemini default).

Confirm the planner log line shows the Anthropic provider:

```
[LLM] role=planner provider=anthropic model=claude-3-5-haiku-latest input_tokens=... output_tokens=... finish_reason=end_turn run_id=...
```

Clear when done:

```powershell
Remove-Item Env:\PLANNER_LLM_PROVIDER
Remove-Item Env:\PLANNER_LLM_MODEL
```

## OpenAI Provider Smoke

Requires `OPENAI_API_KEY`. Set planner and coder to use OpenAI:

```powershell
$env:PLANNER_LLM_PROVIDER = "openai"
$env:PLANNER_LLM_MODEL = "gpt-4o-mini"
$env:CODER_LLM_PROVIDER = "openai"
$env:CODER_LLM_MODEL = "gpt-4o-mini"
```

Run a small chunked flow (triage remains on Gemini default).

Confirm the planner and coder log lines show the OpenAI provider:

```
[LLM] role=planner provider=openai model=gpt-4o-mini input_tokens=... output_tokens=... finish_reason=stop run_id=...
[LLM] role=coder provider=openai model=gpt-4o-mini input_tokens=... output_tokens=... finish_reason=stop run_id=...
```

Clear when done:

```powershell
Remove-Item Env:\PLANNER_LLM_PROVIDER
Remove-Item Env:\PLANNER_LLM_MODEL
Remove-Item Env:\CODER_LLM_PROVIDER
Remove-Item Env:\CODER_LLM_MODEL
```

## DeepSeek Provider Smoke

Requires `DEEPSEEK_API_KEY`. Set planner to use DeepSeek:

```powershell
$env:PLANNER_LLM_PROVIDER = "deepseek"
$env:PLANNER_LLM_MODEL = "deepseek-v4-flash"
```

Run a small chunked flow (triage and coder remain on Gemini default).

Confirm the planner log line shows the DeepSeek provider:

```
[LLM] role=planner provider=deepseek model=deepseek-v4-flash input_tokens=... output_tokens=... finish_reason=stop run_id=...
```

Clear when done:

```powershell
Remove-Item Env:\PLANNER_LLM_PROVIDER
Remove-Item Env:\PLANNER_LLM_MODEL
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
Select-String -Path backend\pipeline\*.py -Pattern "google\.generativeai|genai\.|import anthropic|import openai|print\("
```

Expected result: no matches in `triage.py`, `planner.py`, or `coder.py`.

Confirm each SDK import exists only in its own provider adapter:

```powershell
Select-String -Path backend\llm\providers\gemini.py -Pattern "google\.generativeai"
Select-String -Path backend\llm\providers\anthropic.py -Pattern "import anthropic"
Select-String -Path backend\llm\providers\openai.py -Pattern "import openai"
Select-String -Path backend\llm\providers\deepseek.py -Pattern "import openai"
```

Expected result: at least one match per file.

Note: both `openai.py` and `deepseek.py` are expected to match `import openai`
— DeepSeek reuses the OpenAI-compatible client with a different `base_url`.

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

### Missing API Key

```
ProviderConfigurationError: GEMINI_API_KEY is not configured
ProviderConfigurationError: ANTHROPIC_API_KEY is not configured
ProviderConfigurationError: OPENAI_API_KEY is not configured
```

Set the required key in your `.env` file or shell environment before starting the
backend. Keys are loaded at startup from `backend/config/keys.py` (`Settings`).
`GEMINI_API_KEY` is required at startup (no default); `ANTHROPIC_API_KEY` and
`OPENAI_API_KEY` are optional and only validated when their provider is selected.

### Unknown Provider

```
UnsupportedProviderError: Unsupported LLM provider: <name>
```

The provider name set in env is not registered in `default_registry()`. Registered
providers are: `gemini`, `anthropic`, `openai`, `fake`. Check for typos.

### Unsupported Model

```
UnsupportedModelError: Provider <name> does not support model <model>
```

The model name set in env does not match the models supported by the selected
provider. Check each adapter's `supports_model` list:
- Gemini: `gemini-2.5-flash-lite`, `gemini-2.5-flash`, `gemini-2.5-pro`, etc.
- Anthropic: `claude-3-5-haiku-latest`, `claude-3-5-sonnet-latest`, `claude-sonnet-4-5`, etc.
- OpenAI: any `gpt-*` name, or o-series (`o1`, `o3`, `o4-mini`, etc.)
- DeepSeek: any `deepseek-*` name (`deepseek-v4-flash`, `deepseek-chat`, etc.)
- Fake: `fake-model` only

### Role Override Not Taking Effect

If setting `PLANNER_LLM_PROVIDER=anthropic` has no effect, verify:
1. The env var was set in the same terminal session where the backend is running.
2. The backend was **restarted** after changing the env var — `pydantic-settings`
   reads env vars at startup, not on each request.
3. No existing `.env` file entry overrides the shell var.
4. Check backend logs for `[LLM] role=planner provider=...` to confirm the
   actual resolved provider.

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
