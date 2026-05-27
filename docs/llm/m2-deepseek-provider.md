# LLM-M2-C DeepSeek Provider

## Overview

LLM-M2-C adds DeepSeek as a provider behind the `backend.llm` abstraction.
DeepSeek exposes an OpenAI-compatible chat completions API, so the adapter
reuses the existing `openai` SDK with `base_url` pointed at
`https://api.deepseek.com`. No separate DeepSeek SDK is added.

No pipeline role logic changed. The SDK usage is isolated to
`backend/llm/providers/deepseek.py`. Gemini remains the default.

## New File

`backend/llm/providers/deepseek.py` — the only runtime file that imports the
`openai` SDK for DeepSeek calls (alongside `backend/llm/providers/openai.py`).
All pipeline and other runtime files remain SDK-free.

## Configuration

Set the DeepSeek API key:

```
DEEPSEEK_API_KEY=sk-...
```

Optionally override the base URL (defaults to `https://api.deepseek.com`):

```
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

Route any role to DeepSeek using the existing env vars:

```
DEFAULT_LLM_PROVIDER=deepseek
DEFAULT_LLM_MODEL=deepseek-v4-flash
```

Or per-role:

```
TRIAGE_LLM_PROVIDER=deepseek
TRIAGE_LLM_MODEL=deepseek-v4-flash
PLANNER_LLM_PROVIDER=deepseek
PLANNER_LLM_MODEL=deepseek-v4-pro
CODER_LLM_PROVIDER=deepseek
CODER_LLM_MODEL=deepseek-v4-pro
```

When no LLM env vars are set, the default remains Gemini (`gemini-2.5-flash-lite`).

## Supported Models

`supports_model` accepts any name starting with `deepseek-`, plus the common
aliases `deepseek-chat` and `deepseek-reasoner`.

Common model IDs:

| Model ID | Notes |
|----------|-------|
| `deepseek-v4-flash` | DeepSeek V4 Flash |
| `deepseek-v4-pro` | DeepSeek V4 Pro |
| `deepseek-chat` | General chat alias |
| `deepseek-reasoner` | Reasoning model alias |
| `deepseek-coder` | Code-focused model |

Model names are not fetched at runtime. If DeepSeek releases a new model,
any name starting with `deepseek-` will be accepted by `supports_model`.

## Message Translation

DeepSeek's OpenAI-compatible API natively supports `system`, `user`, and
`assistant` roles in the messages list. Multiple system messages are allowed.
Messages are passed through as-is with no transformation.

## response_format

When `LLMRequest.response_format == "json_object"`, the adapter passes
`response_format={"type": "json_object"}` to the chat completions API as
best-effort. For `"text"` (the default), the parameter is omitted.

## OpenAI SDK Reuse

DeepSeek uses `openai.AsyncOpenAI` with `base_url="https://api.deepseek.com"`.
This means:
- The `openai` SDK must be installed (already a dependency).
- Error types from DeepSeek API calls are OpenAI SDK exception classes and are
  mapped identically to OpenAIProvider errors.
- The provider isolation guard allows `import openai` in both
  `backend/llm/providers/openai.py` and `backend/llm/providers/deepseek.py`.

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
venv\Scripts\python.exe -m pytest backend\tests\test_llm_deepseek_provider.py -v -m unit -p no:cacheprovider
```

Manual provider selection check (requires `DEEPSEEK_API_KEY`):

```powershell
$env:PLANNER_LLM_PROVIDER = "deepseek"
$env:PLANNER_LLM_MODEL = "deepseek-v4-flash"
# run a small pipeline or role-level test
# confirm log line: [LLM] role=planner provider=deepseek model=deepseek-v4-flash ...
Remove-Item Env:\PLANNER_LLM_PROVIDER
Remove-Item Env:\PLANNER_LLM_MODEL
```

## Known Limitations

- `DEEPSEEK_API_KEY` is optional at startup; `ProviderConfigurationError` is
  raised only when a role is actually invoked with `provider=deepseek`
- No streaming support
- No tool/function calling
- `max_retries=0` is set on the client; retry logic is handled at the pipeline
  level
- No cost tracking or DB audit table
- `response_format="json_object"` is passed best-effort; verify DeepSeek
  endpoint support for the specific model before relying on structured output

## Safety Rules

- Only `backend/llm/providers/deepseek.py` may import `openai` for DeepSeek
  calls (alongside `openai.py`); enforced by
  `test_guards_provider_isolation.py::test_openai_sdk_import_only_in_provider_adapters`
- Pipeline files must not import the `openai` SDK directly; enforced by
  `test_pipeline_files_do_not_import_openai_sdk`
