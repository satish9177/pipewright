# LLM Provider Matrix

## Overview

Pipewright supports multiple LLM providers behind the `backend.llm` abstraction.
Pipeline roles (triage, planner, coder) call `complete_for_role` from
`backend.llm` and never import provider SDKs directly. The active provider and
model are resolved from environment variables at backend startup.

No provider API keys are stored in the database. All keys are read from
environment variables or a `.env` file.

## Supported Providers

| Provider | Provider ID | API Key Env | Example Model | Notes |
|----------|-------------|-------------|---------------|-------|
| Gemini | `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash-lite` | **Default** when no LLM env vars are set |
| Anthropic/Claude | `anthropic` | `ANTHROPIC_API_KEY` | `claude-3-5-haiku-latest` | Native Anthropic SDK; system messages handled automatically |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` | Accepts `gpt-*` and `o`-series models |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-v4-flash` | OpenAI-compatible API; reuses `openai` SDK with DeepSeek base URL |
| FakeProvider | `fake` | none | `fake-model` | Tests and local dev only — not for production pipelines |

## Env Config Precedence

For each pipeline role the provider and model are resolved in this order. The
first non-empty value wins:

1. **Explicit overrides dict** — `overrides` kwarg passed to `complete_for_role`
   or `resolve_role_config`; for per-call programmatic override
2. **Role-specific env** — `<ROLE>_LLM_PROVIDER` / `<ROLE>_LLM_MODEL`; affects
   only that role
3. **Default env** — `DEFAULT_LLM_PROVIDER` / `DEFAULT_LLM_MODEL`; affects all
   roles not overridden above
4. **Hardcoded fallback** — `provider="gemini"`, `model="gemini-2.5-flash-lite"`;
   used when no env is set at all

An env var set to an empty string or whitespace is treated as unset and the next
level in the chain is checked.

## Generic Role/Model Env Vars

| Variable | Scope |
|----------|-------|
| `DEFAULT_LLM_PROVIDER` | All roles (fallback) |
| `DEFAULT_LLM_MODEL` | All roles (fallback) |
| `TRIAGE_LLM_PROVIDER` / `TRIAGE_LLM_MODEL` | Triage role only |
| `PLANNER_LLM_PROVIDER` / `PLANNER_LLM_MODEL` | Planner role only |
| `CODER_LLM_PROVIDER` / `CODER_LLM_MODEL` | Coder role only |
| `REVIEWER_LLM_PROVIDER` / `REVIEWER_LLM_MODEL` | Reviewer role only |
| `SUMMARY_LLM_PROVIDER` / `SUMMARY_LLM_MODEL` | Summary role only |

## API Key Env Vars

| Variable | Required at Startup | Notes |
|----------|---------------------|-------|
| `GEMINI_API_KEY` | Yes | Required even if all roles use a different provider; may be a placeholder value |
| `ANTHROPIC_API_KEY` | No | Validated only when `provider=anthropic` is invoked |
| `OPENAI_API_KEY` | No | Validated only when `provider=openai` is invoked |
| `DEEPSEEK_API_KEY` | No | Validated only when `provider=deepseek` is invoked |

DeepSeek also accepts an optional `DEEPSEEK_BASE_URL`; defaults to
`https://api.deepseek.com`.

Do not commit API keys to the repository. Use `.env` (gitignored) or a secrets
manager.

## Example Configurations

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

**All DeepSeek:**

```
DEEPSEEK_API_KEY=sk-...
DEFAULT_LLM_PROVIDER=deepseek
DEFAULT_LLM_MODEL=deepseek-v4-flash
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

**FakeProvider (unit/dev only — do not use in production):**

```
DEFAULT_LLM_PROVIDER=fake
DEFAULT_LLM_MODEL=fake-model
```

> **Warning:** FakeProvider returns a hardcoded string. Running triage, planner,
> or coder with FakeProvider without test fixtures providing valid role JSON via
> `request.extras["response_text"]` will fail JSON parsing.

## What Is Not Supported Yet

- Provider/model selection UI in project settings
- DB-stored provider API keys
- Encrypted BYOK provider secret storage
- Provider fallback chains — if the selected provider fails, the error propagates
  normally; no automatic retry to a different provider
- Auto-routing based on cost, capability, or prompt characteristics
- Execution modes (streaming, batch)
- Tool/function calling
- Local provider (Ollama or similar)
- Per-run `llm_calls` audit table

## Safety Rules

These constraints are enforced by guard tests in
`backend/tests/test_guards_provider_isolation.py`:

- Only `backend/llm/providers/gemini.py` may import `google.generativeai`
- Only `backend/llm/providers/anthropic.py` may import the `anthropic` SDK
- Only `backend/llm/providers/openai.py` and `backend/llm/providers/deepseek.py`
  may import the `openai` SDK (DeepSeek reuses the OpenAI-compatible client)
- Pipeline files (triage, planner, coder, orchestrator) must not import any
  provider SDK directly
- All provider error messages are sanitized through `sanitize_for_log` before
  being stored or propagated — no API keys, bearer tokens, prompt content, or raw
  response bodies appear in errors or log lines
- API keys are read from env/`.env` only; never commit keys to the repository
- Clear temporary provider env vars after smoke testing (see
  `docs/llm/m2-provider-smoke-checklist.md`)

## Provider Adapter Files

| File | SDK imported | Responsibility |
|------|-------------|----------------|
| `backend/llm/providers/gemini.py` | `google.generativeai` | Gemini chat completions |
| `backend/llm/providers/anthropic.py` | `anthropic` | Anthropic messages API; system-message concatenation |
| `backend/llm/providers/openai.py` | `openai` | OpenAI chat completions |
| `backend/llm/providers/deepseek.py` | `openai` | DeepSeek via OpenAI-compatible endpoint |
| `backend/llm/providers/fake.py` | none | Deterministic stub for tests |
