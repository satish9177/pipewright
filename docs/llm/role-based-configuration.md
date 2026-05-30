# Role-Based LLM Configuration

Pipewright resolves an LLM **provider** and **model** independently for each
pipeline role. This lets you run everything on one cheap model (simple mode) or
assign a different provider/model per role (role-based mode) — purely through
environment variables. No UI, no database, no code changes.

This document is role-focused. For the full provider/model support matrix,
supported model lists, and safety rules, see
[`docs/llm/provider-matrix.md`](./provider-matrix.md).

---

## Roles

Roles are defined in `backend/llm/role_config.py` (`Role` enum). Each role is a
distinct LLM call site with its own prompt and its own resolved provider/model.

| Role | Env prefix | Wired into pipeline? | What it does |
|------|------------|----------------------|--------------|
| `triage` | `TRIAGE_LLM_*` | **Yes** — `backend/pipeline/triage.py`, `intent.py` | Classifies a feature request, scores complexity, and produces the chunk plan. Also backs intent detection. |
| `planner` | `PLANNER_LLM_*` | **Yes** — `backend/pipeline/planner.py` | Turns an approved chunk into a structured plan handoff (steps, files to modify/create). |
| `coder` | `CODER_LLM_*` | **Yes** — `backend/pipeline/coder.py` | Produces the structured code-change handoff that the patch layer applies. |
| `summary` | `SUMMARY_LLM_*` | **Yes** — `backend/pipeline/report_analyzer.py` | Summarizes/analyzes run reports and completion output. |
| `reviewer` | `REVIEWER_LLM_*` | Not yet | Defined and configurable today; no pipeline stage calls `Role.REVIEWER` yet. Configuring it is harmless but currently inert. |
| `architect` | `ARCHITECT_LLM_*` | Not yet | Defined and configurable today; no pipeline stage calls `Role.ARCHITECT` yet. Configuring it is harmless but currently inert. |

> **Note:** `reviewer` and `architect` exist in the role registry so their
> configuration is validated and forward-compatible, but no stage invokes them
> in the current runtime. Setting their env vars has no effect on a run until
> those stages are wired in. This is intentional — see *Intentionally paused*.

---

## Simple mode (one model for everything)

Set a single default provider/model. Every role uses it.

```dotenv
GEMINI_API_KEY=AIza...
DEFAULT_LLM_PROVIDER=gemini
DEFAULT_LLM_MODEL=gemini-2.5-flash-lite
```

If you set **no** LLM env vars at all, Pipewright falls back to a hardcoded
default of `gemini` / `gemini-2.5-flash-lite` (`DEFAULT_PROVIDER` /
`DEFAULT_MODEL` in `role_config.py`), so the minimal working config is just a
valid `GEMINI_API_KEY`.

---

## Role-based mode (different model per role)

Add role-specific overrides on top of the default. Each role-specific value
overrides the default for that role only; every other role keeps the default.

```dotenv
# Default for any role without an explicit override
GEMINI_API_KEY=AIza...
DEFAULT_LLM_PROVIDER=gemini
DEFAULT_LLM_MODEL=gemini-2.5-flash-lite

# Per-role overrides (each needs the matching provider API key)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

PLANNER_LLM_PROVIDER=anthropic
PLANNER_LLM_MODEL=claude-sonnet-4-5

CODER_LLM_PROVIDER=openai
CODER_LLM_MODEL=gpt-4o-mini

REVIEWER_LLM_PROVIDER=anthropic
REVIEWER_LLM_MODEL=claude-opus-4-7
```

> **Env var naming:** Pipewright uses `DEFAULT_LLM_PROVIDER` / `DEFAULT_LLM_MODEL`
> and `<ROLE>_LLM_PROVIDER` / `<ROLE>_LLM_MODEL`. (There is **no**
> `PIPEWRIGHT_*_PROVIDER` prefix — that is not a supported name.) Use only model
> IDs the chosen provider actually supports, listed in
> [`provider-matrix.md`](./provider-matrix.md).

---

## Resolution order

For each role, the provider and model are resolved **independently**, each by
walking this chain and taking the first non-empty value:

1. **Explicit overrides dict** — the `overrides` kwarg passed programmatically to
   `resolve_role_config` / `complete_for_role` (e.g. `{"planner_provider": ...}`).
   Not set via env; used internally/for tests.
2. **Role-specific env/setting** — `<ROLE>_LLM_PROVIDER` / `<ROLE>_LLM_MODEL`.
3. **Default env/setting** — `DEFAULT_LLM_PROVIDER` / `DEFAULT_LLM_MODEL`.
4. **Hardcoded fallback** — `gemini` / `gemini-2.5-flash-lite`.

An env var set to an empty string or whitespace is treated as **unset**, so the
next level in the chain is consulted. Provider and model resolve separately:
you can, for example, leave `CODER_LLM_MODEL` unset while setting
`CODER_LLM_PROVIDER`, and the model falls through to `DEFAULT_LLM_MODEL`.

If the resolved provider is unknown, or the resolved model is not supported by
that provider, the run fails with a clear `UnsupportedProviderError` /
`UnsupportedModelError` (raised from `get_provider_for_role`). Error messages are
sanitized — no API keys appear in them.

---

## Verify your config locally

Use the helper script to print the resolved provider/model for every role
without making any network calls or printing any secrets:

```powershell
venv\Scripts\python.exe scripts\print_role_config.py
```

Add `--validate` to also check that each role's provider is registered, its
model is supported, and the required API key is present:

```powershell
venv\Scripts\python.exe scripts\print_role_config.py --validate
```

The script imports `backend.config.keys`, which loads settings at import time
and therefore requires `GEMINI_API_KEY` to be set (a placeholder value is fine
if no role actually uses Gemini). This matches backend startup behavior.

You can also confirm resolution from a Python shell:

```powershell
venv\Scripts\python.exe -c "from backend.llm.role_config import Role, resolve_role_config; [print(r.value, resolve_role_config(r)) for r in Role]"
```

The backend itself validates all roles via `validate_all_roles()` (in
`backend/llm/__init__.py`) — a misconfigured provider/model surfaces as a clear
error rather than a silent fallback.

---

## API keys

Provider API keys are read from environment variables / `.env` only — never from
the database. A provider's key is validated only when that provider is actually
invoked.

| Variable | Required | Notes |
|----------|----------|-------|
| `GEMINI_API_KEY` | **Yes, always** | Loaded at startup by `Settings`. Required even if no role uses Gemini; a placeholder value is acceptable in that case. |
| `ANTHROPIC_API_KEY` | Only if a role uses `anthropic` | |
| `OPENAI_API_KEY` | Only if a role uses `openai` | |
| `DEEPSEEK_API_KEY` | Only if a role uses `deepseek` | Optional `DEEPSEEK_BASE_URL`; defaults to `https://api.deepseek.com`. |

Do not commit API keys. Use `.env` (gitignored) or a secrets manager.

---

## Known limitations

- **`reviewer` and `architect` are not yet wired** into any pipeline stage.
  Their env vars are accepted and validated but inert.
- **No provider fallback chains.** If a provider call fails, the error
  propagates; Pipewright does not auto-retry on a different provider.
- **No per-run or per-project override via UI/DB.** Configuration is global,
  via env vars, for the whole backend process.
- **`GEMINI_API_KEY` is mandatory at startup** even for an all-Anthropic or
  all-OpenAI setup (use a placeholder).
- **`FakeProvider` (`fake` / `fake-model`)** is for tests and local scaffolding
  only; it returns a hardcoded string and will break JSON-parsing roles unless a
  fixture supplies valid JSON.

---

## Intentionally paused

These are designed but deliberately **not** in scope for the current phase
(see `docs/architecture/multi-llm-architecture.md` and
`docs/architecture/execution-and-model-modes.md`):

- Provider/model selection **UI** in project settings
- **BYOK** provider API keys stored (encrypted) in the database
- **Execution modes** (Fast / Balanced / Safe) and the model-selection-mode UI
- **Auto cost-based routing** / capability-driven model selection
- **Ollama** / local-provider support

The current, supported surface is exactly what this document describes:
manual, env-based, per-role provider/model selection.
