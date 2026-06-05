# LLM Provider Smoke Checklist (M2)

Use this checklist when running live smoke tests against real LLM providers.
Unit tests (fully mocked, no live API calls) can be run at any time without
API keys configured.

## Prerequisites

- Backend Python dependencies installed:
  ```powershell
  venv\Scripts\pip.exe install -r backend\requirements.txt
  ```
- API key for the provider being tested set in the shell or `.env`
- Shell LLM env vars clean before each provider smoke (see section below)
- Backend **restarted** after changing env vars — `pydantic-settings` reads env
  at startup, not per-request

## Clean Local LLM Env Vars

Run this before starting any provider smoke to ensure leftover vars from
a previous test do not affect results:

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

## Full Backend Unit Test Suite

No live API key required — all SDK calls are mocked.

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit -p no:cacheprovider
```

Expected: all unit tests pass regardless of which API keys are configured
locally. The `-p no:cacheprovider` flag suppresses a cosmetic Windows cache
permission warning.

## Gemini Live Smoke

Requires `GEMINI_API_KEY`. No provider override needed — Gemini is the hardcoded
default.

```powershell
$env:GEMINI_API_KEY = "AIza..."
```

Restart the backend, then run a small chunked flow through the UI or API
(create project → generate triage → approve chunk plan → execute one chunk).

Confirm backend logs contain `[LLM]` lines for each role:

```
[LLM] role=triage   provider=gemini model=gemini-2.5-flash-lite input_tokens=... output_tokens=... finish_reason=stop run_id=...
[LLM] role=planner  provider=gemini model=gemini-2.5-flash-lite input_tokens=... output_tokens=... finish_reason=stop run_id=...
[LLM] role=coder    provider=gemini model=gemini-2.5-flash-lite input_tokens=... output_tokens=... finish_reason=stop run_id=...
```

## Anthropic Live Smoke

Requires `ANTHROPIC_API_KEY`. Route the planner to Anthropic while triage and
coder remain on the Gemini default:

```powershell
$env:PLANNER_LLM_PROVIDER = "anthropic"
$env:PLANNER_LLM_MODEL    = "claude-3-5-haiku-latest"
```

Restart backend. Run a small chunked flow.

Confirm the planner log line shows Anthropic:

```
[LLM] role=planner provider=anthropic model=claude-3-5-haiku-latest input_tokens=... output_tokens=... finish_reason=end_turn run_id=...
```

Clear when done:

```powershell
Remove-Item Env:\PLANNER_LLM_PROVIDER
Remove-Item Env:\PLANNER_LLM_MODEL
```

## OpenAI Live Smoke

Requires `OPENAI_API_KEY`. Route planner and coder to OpenAI:

```powershell
$env:PLANNER_LLM_PROVIDER = "openai"
$env:PLANNER_LLM_MODEL    = "gpt-4o-mini"
$env:CODER_LLM_PROVIDER   = "openai"
$env:CODER_LLM_MODEL      = "gpt-4o-mini"
```

Restart backend. Run a small chunked flow (triage remains on Gemini default).

Confirm planner and coder log lines show OpenAI:

```
[LLM] role=planner provider=openai model=gpt-4o-mini input_tokens=... output_tokens=... finish_reason=stop run_id=...
[LLM] role=coder   provider=openai model=gpt-4o-mini input_tokens=... output_tokens=... finish_reason=stop run_id=...
```

Clear when done:

```powershell
Remove-Item Env:\PLANNER_LLM_PROVIDER
Remove-Item Env:\PLANNER_LLM_MODEL
Remove-Item Env:\CODER_LLM_PROVIDER
Remove-Item Env:\CODER_LLM_MODEL
```

## DeepSeek Live Smoke

Requires `DEEPSEEK_API_KEY`. Route the planner to DeepSeek:

```powershell
$env:PLANNER_LLM_PROVIDER = "deepseek"
$env:PLANNER_LLM_MODEL    = "deepseek-v4-flash"
```

Restart backend. Run a small chunked flow.

Confirm the planner log line shows DeepSeek:

```
[LLM] role=planner provider=deepseek model=deepseek-v4-flash input_tokens=... output_tokens=... finish_reason=stop run_id=...
```

Clear when done:

```powershell
Remove-Item Env:\PLANNER_LLM_PROVIDER
Remove-Item Env:\PLANNER_LLM_MODEL
```

## FakeProvider Sanity Check (dev/test only)

No API key required. Use only for provider selection verification — not for real
pipeline runs.

```powershell
$env:DEFAULT_LLM_PROVIDER = "fake"
$env:DEFAULT_LLM_MODEL    = "fake-model"
```

Run a quick role resolution check from a Python shell:

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

Expected output:

```
fake fake-model pong
```

Clear when done:

```powershell
Remove-Item Env:\DEFAULT_LLM_PROVIDER
Remove-Item Env:\DEFAULT_LLM_MODEL
```

> **Warning:** FakeProvider returns a plain string. Do not run triage, planner,
> or coder in a real pipeline with FakeProvider unless the test scaffold supplies
> valid JSON through `request.extras["response_text"]` for each role.

## Manual UI Smoke Checklist

- [ ] Start backend: `venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8001`
- [ ] Start frontend: `npm.cmd run dev` (from `frontend/`)
- [ ] Open or create a project with a local repo path
- [ ] Generate or approve project memory if prompted
- [ ] Create a chunked run for a small feature request
- [ ] Review and approve the chunk plan
- [ ] Execute the chunks
- [ ] Confirm the final approval UI works
- [ ] Optionally push a PR (requires `PIPEWRIGHT_ENCRYPTION_KEY` and a configured GitHub token in the project)
- [ ] Check backend logs for `[LLM]` lines — every role call must show `role`, `provider`, and `model`

## Troubleshooting

### Provider Still Shows Wrong Provider After Env Change

1. Confirm the env var is set in the same terminal session as the backend.
2. **Restart the backend** — `pydantic-settings` reads env at startup, not
   per-request.
3. Check for a `.env` file entry that overrides the shell var.
4. Run the clean-env block above to remove leftover vars, then restart.

### Tests Fail Because of Local Shell LLM Env Vars

Provider role-config tests can fail if `DEFAULT_LLM_PROVIDER=fake` or
`TRIAGE_LLM_PROVIDER=fake` is set in the developer shell. The shared
`clear_llm_env` fixture in `backend/tests/conftest.py` wipes all LLM env vars
via `monkeypatch.delenv` for the affected tests. Run the full unit suite to
verify:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit -p no:cacheprovider
```

### Missing API Key

```
ProviderConfigurationError: GEMINI_API_KEY is not configured
ProviderConfigurationError: ANTHROPIC_API_KEY is not configured
ProviderConfigurationError: OPENAI_API_KEY is not configured
ProviderConfigurationError: DEEPSEEK_API_KEY is not configured
```

Set the required key in `.env` or the shell before starting the backend.
`GEMINI_API_KEY` is required at startup even when all roles use a different
provider; it may be set to a placeholder value.

### Unsupported Model

```
UnsupportedModelError: Provider <name> does not support model <model>
```

Check the model name against each adapter's acceptance rule:

| Provider | Accepted models |
|----------|----------------|
| Gemini | `gemini-2.5-flash-lite`, `gemini-2.5-flash`, `gemini-2.5-pro`, etc. (static list in adapter) |
| Anthropic | `claude-3-5-haiku-latest`, `claude-3-5-sonnet-latest`, `claude-sonnet-4-5`, `claude-sonnet-4-6`, etc. |
| OpenAI | any `gpt-*` name; o-series (`o1`, `o3`, `o4-mini`, etc.) |
| DeepSeek | any `deepseek-*` name (`deepseek-v4-flash`, `deepseek-chat`, etc.) |
| Fake | `fake-model` only |

### Provider Returns Invalid JSON in Pipeline Role

FakeProvider (and any provider misconfigured without proper JSON output) will
cause triage, planner, or coder to fail JSON parsing and enter the retry path
before ultimately failing the chunk.

Do not use FakeProvider in a real pipeline unless the test scaffold supplies
valid role JSON via `request.extras["response_text"]`.

### pytest Cache Warning on Windows

```
PytestCacheWarning: could not create cache path ... [WinError 5] Access is denied
```

This is a cosmetic warning. Run with `-p no:cacheprovider` to suppress it; tests
still pass.

### PIPEWRIGHT_ENCRYPTION_KEY Required for GitHub Push

`PIPEWRIGHT_ENCRYPTION_KEY` is needed only for GitHub token storage and the PR
push step. LLM provider configuration does not require this key.
