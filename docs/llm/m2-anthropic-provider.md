# LLM-M2-A Anthropic Provider

## Overview

LLM-M2-A adds Anthropic/Claude as the second real provider behind the
`backend.llm` abstraction. No pipeline role logic changed. Provider selection
is still controlled entirely by env vars using the same precedence chain
established in LLM-M1.

## New File

`backend/llm/providers/anthropic.py` — the only runtime file that imports
the `anthropic` SDK. All other pipeline and runtime files remain SDK-free.

## Configuration

Set the Anthropic API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Route any role to Anthropic using the existing env vars:

```
DEFAULT_LLM_PROVIDER=anthropic
DEFAULT_LLM_MODEL=claude-3-5-haiku-latest
```

Or per-role:

```
PLANNER_LLM_PROVIDER=anthropic
PLANNER_LLM_MODEL=claude-3-5-haiku-latest
CODER_LLM_PROVIDER=anthropic
CODER_LLM_MODEL=claude-sonnet-4-5
```

When no LLM env vars are set, the default remains Gemini (`gemini-2.5-flash-lite`).

## Supported Models

| Model ID | Notes |
|----------|-------|
| `claude-3-5-sonnet-latest` | Claude 3.5 Sonnet latest alias |
| `claude-3-5-haiku-latest` | Claude 3.5 Haiku latest alias |
| `claude-3-5-sonnet-20241022` | Claude 3.5 Sonnet versioned |
| `claude-3-5-haiku-20241022` | Claude 3.5 Haiku versioned |
| `claude-3-opus-20240229` | Claude 3 Opus versioned |
| `claude-sonnet-4-5` | Claude 4 Sonnet |
| `claude-sonnet-4-6` | Claude 4.6 Sonnet |
| `claude-opus-4-1` | Claude 4 Opus |
| `claude-opus-4-7` | Claude 4.7 Opus |
| `claude-haiku-4-5` | Claude 4 Haiku |
| `claude-haiku-4-5-20251001` | Claude 4.5 Haiku versioned |

Anthropic accepts `claude-*` model IDs and warns when the ID is outside this
known-good set.

## Message Translation

Anthropic's messages API separates the system prompt from the turn list.
The provider adapter handles this automatically:

- `role="system"` messages are concatenated with `"\n\n"` and passed as the
  `system=` parameter
- `role="user"` and `role="assistant"` messages map directly to
  `{"role": ..., "content": ...}` in the `messages=` list
- Zero system messages is valid; the `system=` parameter is omitted

## Error Mapping

| Anthropic SDK exception | Mapped to | `retryable` |
|-------------------------|-----------|-------------|
| `AuthenticationError` | `ProviderAuthError` | `False` |
| `PermissionDeniedError` | `ProviderAuthError` | `False` |
| `RateLimitError` | `ProviderRateLimitError` | `True` |
| `APITimeoutError` | `ProviderTimeoutError` | `True` |
| `APIConnectionError` | `ProviderTimeoutError` | `True` |
| `BadRequestError` (content filter) | `ProviderContentFilteredError` | `False` |
| `BadRequestError` (other) | `ProviderInvalidResponseError` | `False` |
| `InternalServerError` | `ProviderExecutionError` | `True` |
| `APIStatusError` 5xx/529 | `ProviderExecutionError` | `True` |
| `APIStatusError` other | `ProviderExecutionError` | `False` |
| Unknown | `ProviderExecutionError` | `False` |

All error messages pass through `sanitize_for_log` before being stored or
raised. No API keys, bearer tokens, prompt text, or raw response bodies appear
in error messages.

## Smoke Test Commands

Install the SDK (already in `backend/requirements.txt`):

```powershell
venv\Scripts\pip.exe install anthropic==0.26.0
```

Run unit tests (no live API call, fully mocked):

```powershell
venv\Scripts\python.exe -m pytest backend\tests\test_llm_anthropic_provider.py -v -m unit -p no:cacheprovider
```

Manual provider selection check (requires `ANTHROPIC_API_KEY`):

```powershell
$env:PLANNER_LLM_PROVIDER = "anthropic"
$env:PLANNER_LLM_MODEL = "claude-3-5-haiku-latest"
# run a small pipeline or role-level test
# confirm log line: [LLM] role=planner provider=anthropic model=claude-3-5-haiku-latest ...
Remove-Item Env:\PLANNER_LLM_PROVIDER
Remove-Item Env:\PLANNER_LLM_MODEL
```

## Known Limitations

- `ANTHROPIC_API_KEY` is optional at startup; `ProviderConfigurationError` is
  raised only when a role is actually invoked with `provider=anthropic`
- No streaming support
- No tool/function calling
- `response_format="json_object"` is enforced only at the prompt level; no
  native Anthropic JSON schema enforcement in M2-A
- No cost tracking or DB audit table

## Safety Rules

- Only `backend/llm/providers/anthropic.py` may `import anthropic`; enforced
  by `test_guards_provider_isolation.py::test_anthropic_sdk_import_only_in_provider_adapter`
- Pipeline files (triage, planner, coder, orchestrator) must not import
  the `anthropic` SDK directly; enforced by
  `test_pipeline_files_do_not_import_anthropic_sdk`
