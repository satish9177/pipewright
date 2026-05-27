# LLM-M1 Provider Abstraction

## Overview

LLM-M1 moved the Pipewright pipeline roles (triage, planner, coder) away from
direct Gemini SDK imports. All three roles now call `complete_for_role(...)` from
the `backend.llm` public API. The Gemini SDK is isolated to a single adapter file:
`backend/llm/providers/gemini.py`.

This is behavior-neutral by default. The pipeline still uses Gemini with the same
model and temperature settings as before. The abstraction layer adds no overhead
to the happy path and introduces no fallback chains, auto-routing, or execution
modes.

## Completed Slices

| Slice | Branch | Description |
|-------|--------|-------------|
| LLM-M1-A | `feature/llm-m1-provider-abstraction` | Provider abstraction scaffolding: base contracts, registry, role_config, errors, sanitize, FakeProvider, GeminiProvider |
| LLM-M1-B1 | `feature/llm-m1-migrate-triage` | Triage migrated to `backend.llm` |
| LLM-M1-B2 | `feature/llm-m1-migrate-planner` | Planner migrated to `backend.llm` |
| LLM-M1-B3 | `feature/llm-m1-migrate-coder` | Coder migrated to `backend.llm` |
| LLM-M1-B4 | `chore/llm-provider-guards` | Provider isolation guard tests; AGENTS.md rules |
| LLM-M1-C | `feature/llm-m1-role-config-validation` | Env-based role config validation tests |
| LLM-M1-D | `feature/llm-m1-provider-audit` | Consolidated token usage logging via shared `log_token_usage` |
| LLM-M2-A | `feature/llm-m2-anthropic-provider` | AnthropicProvider adapter (`claude-*` models) |
| LLM-M2-B | `feature/llm-m2-openai-provider` | OpenAIProvider adapter (`gpt-*` and `o`-series models) |

## Files and Responsibilities

### `backend/llm/base.py`

Defines the provider contract types:

- `Message` — a single conversation turn with `role` (`system`/`user`/`assistant`)
  and `content`
- `LLMRequest` — the request envelope: `messages`, `model`, `temperature`,
  `max_output_tokens`, `timeout_seconds`, `response_format`, `extras`
- `LLMResponse` — the response envelope: `text`, `provider`, `model`,
  `input_tokens`, `output_tokens`, `finish_reason`, `raw`
- `BaseLLMProvider` — abstract base class with `name`, `supports_model`, and
  `complete` (async)

### `backend/llm/errors.py`

Structured provider error hierarchy:

- `LLMError` — base; all messages are sanitized through `sanitize_for_log` before
  being stored or re-raised
- `ProviderConfigurationError` — misconfiguration (e.g. missing key)
- `UnsupportedProviderError` — registry lookup failed
- `UnsupportedModelError` — provider does not support the requested model
- `ProviderExecutionError` — API call failed
- `ProviderRateLimitError` — 429; `retryable=True`; optional `retry_after_seconds`
- `ProviderTimeoutError` — timeout; `retryable=True`
- `ProviderAuthError` — auth failure
- `ProviderContentFilteredError` — content policy rejection
- `ProviderInvalidResponseError` — unparseable response

### `backend/llm/sanitize.py`

`sanitize_for_log(value)` strips known secret patterns from strings before they
appear in error messages or logs. Patterns covered: Gemini API keys (`AIza...`),
Anthropic keys (`sk-ant-...`), OpenAI keys (`sk-...`), Bearer tokens, and
`key=`/`token=`/`secret=` context pairs.

### `backend/llm/registry.py`

`ProviderRegistry` maps provider name strings to `BaseLLMProvider` instances.
`default_registry()` returns a registry pre-registered with `GeminiProvider`,
`AnthropicProvider`, `OpenAIProvider`, and `FakeProvider`.

### `backend/llm/role_config.py`

`resolve_role_config(role, overrides)` returns a `RoleConfig(provider, model)` for
a given pipeline role. See [Env Configuration](#env-configuration) for precedence
rules.

### `backend/llm/providers/gemini.py`

The only runtime file that imports `google.generativeai`. Wraps the Gemini
`genai.Client` with the `BaseLLMProvider` interface. Reads `GEMINI_API_KEY` from
settings. All other pipeline files must not import this module or the Gemini SDK
directly.

### `backend/llm/providers/anthropic.py`

The only runtime file that imports the `anthropic` SDK. Wraps `AsyncAnthropic`
with the `BaseLLMProvider` interface. Reads `ANTHROPIC_API_KEY` from settings.
Translates `system` messages into Anthropic's top-level `system=` parameter
(multiple system messages are concatenated with `"\n\n"`). Supported models
include `claude-3-5-haiku-latest`, `claude-3-5-sonnet-latest`, `claude-sonnet-4-5`,
`claude-sonnet-4-6`, `claude-opus-4-1`, and versioned variants.

### `backend/llm/providers/openai.py`

The only runtime file that imports the `openai` SDK. Wraps `AsyncOpenAI` with
the `BaseLLMProvider` interface. Reads `OPENAI_API_KEY` from settings. Passes
messages through as-is (OpenAI natively supports system/user/assistant roles).
Accepts any model whose name starts with `gpt-` or matches the o-series pattern
(`o1`, `o3`, `o4-mini`, etc.).

### `backend/llm/providers/fake.py`

Deterministic provider for tests and local scaffolding. `name="fake"`,
`supports_model("fake-model")`. Returns `request.extras.get("response_text",
"fake response")`. Must not be used in production pipeline runs where valid JSON
responses are expected.

### `backend/llm/__init__.py`

Public API surface:

- `complete_for_role(role, request, overrides=None)` — resolves provider/model,
  validates model support, calls `provider.complete(request)`
- `get_provider_for_role(role, overrides=None)` — returns `(provider, model)` pair
- `log_token_usage(response, *, run_id, role)` — emits the structured audit log
  line
- `validate_all_roles(overrides=None)` — validates config for every role; called at
  startup

## Provider Interface

Pipeline roles interact with the provider through the `LLMRequest`/`LLMResponse`
envelope only. Direct SDK calls are prohibited outside adapter modules.

```python
# In a pipeline role (e.g. planner.py):
from backend.llm import complete_for_role, log_token_usage
from backend.llm.base import LLMRequest, Message
from backend.llm.role_config import Role

request = LLMRequest(
    messages=[
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content=user_prompt),
    ],
    model=PLANNER_MODEL,
    temperature=PLANNER_TEMPERATURE,
    max_output_tokens=PLANNER_MAX_TOKENS,
    response_format="json_object",
)

response = await complete_for_role(Role.PLANNER, request)
log_token_usage(response, run_id=run_id, role=Role.PLANNER)
raw_text = response.text
```

Key contract notes:

- `messages` is a list of `Message` objects (system/user/assistant turns)
- `response.text` is the decoded text content — the only field pipeline roles
  should read
- `response.raw` is populated by provider adapters for internal audit purposes;
  pipeline roles must not log or expose `raw`
- `response.input_tokens` and `response.output_tokens` may be `None` if the
  provider does not return them
- `response.finish_reason` may be `None`; `log_token_usage` renders it as
  `"unknown"` in that case

## Env Configuration

Provider and model selection is controlled by environment variables (or equivalent
settings object fields). No DB configuration exists yet.

### Variables

| Variable | Scope | Example |
|----------|-------|---------|
| `DEFAULT_LLM_PROVIDER` | All roles (fallback) | `gemini` |
| `DEFAULT_LLM_MODEL` | All roles (fallback) | `gemini-2.5-flash-lite` |
| `TRIAGE_LLM_PROVIDER` | Triage role only | `fake` |
| `TRIAGE_LLM_MODEL` | Triage role only | `fake-model` |
| `PLANNER_LLM_PROVIDER` | Planner role only | `gemini` |
| `PLANNER_LLM_MODEL` | Planner role only | `gemini-2.5-flash-lite` |
| `CODER_LLM_PROVIDER` | Coder role only | `gemini` |
| `CODER_LLM_MODEL` | Coder role only | `gemini-2.5-flash-lite` |
| `REVIEWER_LLM_PROVIDER` | Reviewer role only | `gemini` |
| `REVIEWER_LLM_MODEL` | Reviewer role only | `gemini-2.5-flash-lite` |
| `SUMMARY_LLM_PROVIDER` | Summary role only | `gemini` |
| `SUMMARY_LLM_MODEL` | Summary role only | `gemini-2.5-flash-lite` |

### Precedence

Resolution runs in this order for each role. The first non-empty value wins:

1. **Explicit overrides dict** — `overrides` kwarg passed directly to
   `complete_for_role` or `resolve_role_config`; for per-call programmatic
   override
2. **Role-specific env** — `<ROLE>_LLM_PROVIDER` / `<ROLE>_LLM_MODEL`; affects
   only that role
3. **Default env** — `DEFAULT_LLM_PROVIDER` / `DEFAULT_LLM_MODEL`; affects all
   roles not overridden above
4. **Hardcoded fallback** — `provider="gemini"`, `model="gemini-2.5-flash-lite"`;
   used when no env is set at all

If an env variable is set to an empty string or whitespace, it is treated as unset
and the next level in the precedence chain is checked.

## Supported Providers

| Provider | Name string | API key env | Example model | Notes |
|----------|-------------|-------------|---------------|-------|
| Gemini | `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash-lite` | **Default** when no LLM env vars are set |
| Anthropic/Claude | `anthropic` | `ANTHROPIC_API_KEY` | `claude-3-5-haiku-latest` | Real production provider |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` | Real production provider |
| FakeProvider | `fake` | none | `fake-model` | Tests and local dev only — not production |

### Example Configurations

**All Gemini (default — no LLM env vars required):**

```
GEMINI_API_KEY=AIza...
```

**All Anthropic:**

```
ANTHROPIC_API_KEY=sk-ant-...
DEFAULT_LLM_PROVIDER=anthropic
DEFAULT_LLM_MODEL=claude-3-5-haiku-latest
```

**All OpenAI:**

```
OPENAI_API_KEY=sk-...
DEFAULT_LLM_PROVIDER=openai
DEFAULT_LLM_MODEL=gpt-4o-mini
```

**Mixed roles (Gemini default, Anthropic planner, OpenAI coder):**

```
GEMINI_API_KEY=AIza...
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
DEFAULT_LLM_PROVIDER=gemini
DEFAULT_LLM_MODEL=gemini-2.5-flash-lite
PLANNER_LLM_PROVIDER=anthropic
PLANNER_LLM_MODEL=claude-3-5-haiku-latest
CODER_LLM_PROVIDER=openai
CODER_LLM_MODEL=gpt-4o-mini
```

**FakeProvider (unit/dev only):**

```
DEFAULT_LLM_PROVIDER=fake
DEFAULT_LLM_MODEL=fake-model
```

> **Warning:** FakeProvider returns a hardcoded string. It is not suitable for
> real pipeline runs unless test fixtures supply valid role JSON via
> `request.extras["response_text"]`. Running triage/planner/coder with
> FakeProvider without that fixture will fail JSON parsing.

## Logging and Audit

Every `complete_for_role` call site in the pipeline calls `log_token_usage`
immediately after receiving the response. The shared helper emits a single
structured `INFO` log line on the `backend.llm` logger:

```
[LLM] role=planner provider=gemini model=gemini-2.5-flash-lite input_tokens=312 output_tokens=147 finish_reason=stop run_id=abc123
```

Field rendering:

| Field | Value when unavailable |
|-------|------------------------|
| `input_tokens` | `unavailable` |
| `output_tokens` | `unavailable` |
| `finish_reason` | `unknown` |
| `run_id` | `none` |

What the log line does **not** include:

- Prompt content or system prompt text
- Response text
- `response.raw` provider metadata
- API keys or secrets

There is no DB `llm_calls` audit table yet. Per-run provider/model audit
persistence is deferred to a future phase.

## Safety Rules

These constraints are enforced by guard tests in
`backend/tests/test_guards_provider_isolation.py` and documented in `AGENTS.md`:

- Only `backend/llm/providers/gemini.py` may import `google.generativeai`; all
  other runtime files are scanned and must have no such import
- Only `backend/llm/providers/anthropic.py` may import the `anthropic` SDK
- Only `backend/llm/providers/openai.py` may import the `openai` SDK
- Pipeline roles (triage, planner, coder) must call `complete_for_role` from
  `backend.llm`; direct provider SDK calls are prohibited
- Pipeline roles must not use `print(...)` — use the module-level logger instead
- Provider error messages are sanitized through `sanitize_for_log` before being
  stored or propagated; raw API error bodies are not forwarded to callers
- No prompt content, file content, or response text may appear in log lines
- No fallback chains between providers
- No auto-routing based on prompt characteristics
- No execution modes (batch, streaming, etc.) in the current phase

## Known Limitations

- No streaming support
- No tool calling / function calling support
- No cost dashboard or token cost tracking
- No DB-persisted `llm_calls` audit table
- FakeProvider returns a hardcoded string and will produce invalid JSON if used
  in a real triage/planner/coder role path without test fixtures providing valid
  JSON via `extras["response_text"]`
- No frontend model settings UI yet
- No DB project-level model config yet — provider/model selection is env-only
- No provider fallback chains — if the selected provider fails, the error
  propagates normally; no automatic retry to a different provider

## Future Phases

- Provider/model selection UI in project settings
- Per-run provider/model audit persistence (`llm_calls` table)
- Execution modes (streaming, batch) — deferred
- Auto provider selection based on cost or capability — deferred
- Ollama or local provider adapter — not planned for current phase
