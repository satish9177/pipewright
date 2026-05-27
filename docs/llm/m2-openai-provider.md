# LLM-M2-B OpenAI Provider

## Overview

LLM-M2-B adds OpenAI as the third real provider behind the `backend.llm`
abstraction. No pipeline role logic changed. The SDK is isolated to
`backend/llm/providers/openai.py`. Gemini remains the default.

## New File

`backend/llm/providers/openai.py` — the only runtime file that imports the
`openai` SDK. All pipeline and other runtime files remain SDK-free.

## Configuration

Set the OpenAI API key:

```
OPENAI_API_KEY=sk-...
```

Route any role to OpenAI using the existing env vars:

```
DEFAULT_LLM_PROVIDER=openai
DEFAULT_LLM_MODEL=gpt-4o-mini
```

Or per-role:

```
TRIAGE_LLM_PROVIDER=openai
TRIAGE_LLM_MODEL=gpt-4o-mini
PLANNER_LLM_PROVIDER=openai
PLANNER_LLM_MODEL=gpt-4o
CODER_LLM_PROVIDER=openai
CODER_LLM_MODEL=gpt-4o
```

When no LLM env vars are set, the default remains Gemini (`gemini-2.5-flash-lite`).

## Supported Models

`supports_model` accepts any name starting with `gpt-` or any o-series model
starting with `o` followed by a digit (`o1`, `o3`, `o4-mini`, etc.).
Empty strings and names from other providers are rejected.

Common tested model IDs:

| Model ID | Notes |
|----------|-------|
| `gpt-4o` | GPT-4o |
| `gpt-4o-mini` | GPT-4o mini |
| `gpt-4.1` | GPT-4.1 |
| `gpt-4.1-mini` | GPT-4.1 mini |
| `gpt-5` | GPT-5 |
| `gpt-5-mini` | GPT-5 mini |
| `o1` | o1 reasoning |
| `o1-mini` | o1 mini |
| `o3` | o3 reasoning |
| `o3-mini` | o3 mini |
| `o4-mini` | o4 mini |

## Message Translation

OpenAI's chat completions API natively supports `system`, `user`, and
`assistant` roles in the messages list. Multiple system messages are allowed.
Messages are passed through as-is with no transformation.

## response_format

When `LLMRequest.response_format == "json_object"`, the adapter passes
`response_format={"type": "json_object"}` to the chat completions API. For
`"text"` (the default), the parameter is omitted. No JSON schema enforcement
in M2-B.

## Error Mapping

| OpenAI SDK exception | Mapped to | `retryable` |
|----------------------|-----------|-------------|
| `AuthenticationError` | `ProviderAuthError` | `False` |
| `PermissionDeniedError` | `ProviderAuthError` | `False` |
| `RateLimitError` | `ProviderRateLimitError` | `True` |
| `APITimeoutError` | `ProviderTimeoutError` | `True` |
| `APIConnectionError` | `ProviderTimeoutError` | `True` |
| `ContentFilterFinishReasonError` | `ProviderContentFilteredError` | `False` |
| `BadRequestError` (content filter) | `ProviderContentFilteredError` | `False` |
| `BadRequestError` (other) | `ProviderInvalidResponseError` | `False` |
| `InternalServerError` | `ProviderExecutionError` | `True` |
| `APIStatusError` 5xx/529 | `ProviderExecutionError` | `True` |
| `APIStatusError` other | `ProviderExecutionError` | `False` |
| Unknown | `ProviderExecutionError` | `False` |

All error messages pass through `sanitize_for_log`. API keys and secrets are
redacted before being stored in `LLMError.message`.

## Smoke Test Commands

Run unit tests (no live API call, fully mocked):

```powershell
venv\Scripts\python.exe -m pytest backend\tests\test_llm_openai_provider.py -v -m unit -p no:cacheprovider
```

Manual provider selection check (requires `OPENAI_API_KEY`):

```powershell
$env:PLANNER_LLM_PROVIDER = "openai"
$env:PLANNER_LLM_MODEL = "gpt-4o-mini"
# run a small pipeline or role-level test
# confirm log line: [LLM] role=planner provider=openai model=gpt-4o-mini ...
Remove-Item Env:\PLANNER_LLM_PROVIDER
Remove-Item Env:\PLANNER_LLM_MODEL
```

## Known Limitations

- `OPENAI_API_KEY` is optional at startup; `ProviderConfigurationError` is
  raised only when a role is actually invoked with `provider=openai`
- No streaming support
- No tool/function calling
- `max_retries=0` is set on the client; retry logic is handled at the pipeline
  level (rate limit 429 already triggers the role's existing retry path)
- No cost tracking or DB audit table

## Safety Rules

- Only `backend/llm/providers/openai.py` may `import openai`; enforced by
  `test_guards_provider_isolation.py::test_openai_sdk_import_only_in_provider_adapter`
- Pipeline files must not import the `openai` SDK directly; enforced by
  `test_pipeline_files_do_not_import_openai_sdk`
