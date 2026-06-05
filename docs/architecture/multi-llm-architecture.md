# Pipewright Multi-LLM Architecture — LLM-M1, LLM-M2, LLM-M3 Design

**Status:** Design only. Implement LLM-M1. Do not implement LLM-M2 or LLM-M3 yet.
**Audience:** Pipewright maintainers and Codex (implementer).
**Mode:** Adversarial. The point is to find what breaks, not to celebrate the design.

---

## 0. Critical Findings on the Current Code (Read This First)

Before any new design lands, the current state of provider integration has problems that LLM-M1 has to face squarely. The planning docs say "architecture supports switching to Anthropic in 10 minutes." That claim is incorrect once you read the code.

| # | Finding | Severity | Evidence |
|---|---------|----------|----------|
| 1 | **No provider abstraction exists.** Every pipeline stage directly imports `google.generativeai` and configures `genai` inline. There is no `Provider` base class, no interface, no registry. | **Critical** | `backend/pipeline/planner.py`, `coder.py`, `triage.py`: all do `genai.configure(api_key=...)` and `genai.GenerativeModel(...)` directly. |
| 2 | **All roles use the same model.** `PLANNER_MODEL`, `CODER_MODEL`, and `TRIAGE_MODEL` are all `"gemini-2.5-flash-lite"`. Reviewer module (if it exists) is not in the indexed code. Multi-LLM is a goal, not a current capability. | High | Module-level constants in each pipeline file. |
| 3 | **No reviewer module appears in the codebase.** The planning docs describe an "adversarial reviewer" with a different model than the coder. Code search did not surface a `reviewer.py`. LLM-M1 may need to either add it or document that reviewer is "not yet wired." | High | Search returned `planner.py`, `coder.py`, `triage.py`, `patch_applier.py`, `chunked_orchestrator.py`. No reviewer. |
| 4 | **Error handling is Gemini-specific by string sniffing.** Code checks `"429" in str(error)` to detect rate limits and hardcodes a 60-second sleep. OpenAI returns `openai.RateLimitError`. Anthropic returns its own structured error. String sniffing will misclassify cross-provider errors. | High | `planner.py`, `coder.py` retry blocks. |
| 5 | **Per-call API knobs are Gemini-shaped.** `request_options={"timeout": 120}` is Gemini SDK syntax. `system_instruction=...` is Gemini. OpenAI uses messages array with role=system; Anthropic uses `system=` parameter. The current code embeds one provider's API shape into pipeline modules. | High | `coder.py` `model.generate_content(prompt, request_options=...)`. |
| 6 | **No record of which model produced any output.** `checkpoints` table has no `model_used`. There is no `llm_calls` audit table. The question "Which model wrote this code?" cannot currently be answered. | **Critical** for audit | `backend/db/schema.sql`. |
| 7 | **Settings expose only `gemini_api_key`.** No `anthropic_api_key`, `openai_api_key`. The settings singleton is global and validated at startup; adding optional keys means breaking the "validate at startup" guarantee unless we make them explicitly optional. | High | `backend/config/keys.py`. |
| 8 | **`temperature=0.2` is baked into each pipeline stage as a module constant.** Project/run-level override is not possible. | Medium | `planner.py`, `coder.py`, `triage.py`. |
| 9 | **No `Retry-After` honored.** The 60s sleep is constant. Provider may have indicated 12s or 240s. Either we wait too long or retry too soon. | Medium | Retry blocks. |
| 10 | **The retry correction prompt re-sends the original user prompt + the bad output.** With cross-provider routing, the previous-attempt output may have been from a different model. This footgun does not bite today because there's only one provider, but it bites the moment we add fallback. | Medium (M2-relevant) | `planner.py`, `coder.py` correction blocks. |

**Implication:** "Add multi-LLM" is not a config-flag change. It is a non-trivial refactor that introduces a provider layer, moves model selection from module constants to runtime config, and adds an audit table. The good news is the work is well-bounded — but estimate it like a refactor, not a flag.

---

## 1. Overall Multi-LLM Architecture

### 1.1 Vocabulary

| Term | Meaning |
|------|---------|
| **Provider** | An LLM vendor. Concretely: `gemini`, `anthropic`, `openai`. Owns SDK choice, auth, and error shape. |
| **Model** | A specific named model from a provider. E.g. `gemini-2.5-flash-lite`, `claude-sonnet-4-5`, `gpt-4o`. Pinned to a version. |
| **Role** | A pipeline role that calls an LLM: `triage`, `planner`, `architect`, `coder`, `reviewer`, `summary`. Each role has its own prompt and own (provider, model) assignment. |
| **Role assignment** | A mapping `role → (provider, model, temperature, max_output_tokens)`. Lives at the project level in M1. |
| **Run snapshot** | An immutable copy of the role assignment, frozen at the moment a run starts. The run uses this snapshot for the rest of its life, including resume. |
| **Provider registry** | An in-process module that maps `provider_name → Provider` instance. Single source of truth for what providers exist and how to call them. |
| **Capability metadata** | A static table describing each known (provider, model) pair: context window, JSON support, cost tier, deprecation status, etc. Used for pre-flight validation. |

### 1.2 The provider layer

A single, small interface that every provider implements. Pipeline stages no longer import `google.generativeai` directly. They call `provider.generate(...)`.

```python
# backend/llm/provider.py

from abc import ABC, abstractmethod
from typing import Protocol
from pydantic import BaseModel

class LLMResponse(BaseModel):
    raw_text: str
    provider: str
    model: str
    tokens_input: int | None
    tokens_output: int | None
    latency_ms: int
    finish_reason: str | None       # "stop" | "length" | "content_filter" | "tool_use" | "other"

class LLMRequest(BaseModel):
    system_prompt: str
    user_prompt: str
    model: str
    temperature: float = 0.2
    max_output_tokens: int = 4000
    timeout_seconds: int = 120
    json_mode: bool = True          # request JSON output if provider supports it
    # No streaming in M1. No tools in M1.

class Provider(ABC):
    name: str                       # "gemini" | "anthropic" | "openai"

    @abstractmethod
    def supports_model(self, model: str) -> bool: ...

    @abstractmethod
    def validate_api_key(self) -> None:
        """Raise ProviderAuthError if key is missing or invalid format.
        Does NOT make a network call. That happens in pre-flight ping."""

    @abstractmethod
    async def ping(self) -> bool:
        """One short network call to confirm key works. M1 uses this on
        pre-flight validation only, never on the hot path."""

    @abstractmethod
    async def generate(self, req: LLMRequest) -> LLMResponse:
        """Single non-streaming call. Provider-specific retry on transient
        errors is OWNED HERE, not in the pipeline stage."""
```

### 1.3 Pipeline integration

Each pipeline stage uses a *role-bound provider*, not a global one:

```python
# backend/pipeline/planner.py (after refactor)

async def run_planner(feature_description: str, run_id: str, project_id: str, chunk_number: int = 0):
    role_cfg = get_role_config_snapshot(run_id, role="planner")  # from run snapshot
    provider = registry.get(role_cfg.provider)

    req = LLMRequest(
        system_prompt=PLANNER_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(feature_description, run_id, hard_facts),
        model=role_cfg.model,
        temperature=role_cfg.temperature,
        max_output_tokens=role_cfg.max_output_tokens,
        json_mode=True,
    )
    resp = await provider.generate(req)
    record_llm_call(run_id, chunk_number, "planner", role_cfg, resp, status="success")
    return _parse_handoff(resp.raw_text, run_id)
```

The pipeline stage no longer:
- Imports any provider SDK.
- Knows about `request_options={"timeout": ...}` shape.
- Sniffs error strings for "429".
- Hardcodes a model name.

The provider implementation:
- Owns its SDK and its error taxonomy.
- Owns its retry policy.
- Returns a normalized `LLMResponse`.

### 1.4 Compatibility with chunked execution

The existing chunked orchestrator does not need structural changes. It currently calls `run_planner`, `run_coder`, etc. After LLM-M1, those functions take a `project_id` and internally read from the run snapshot. The orchestrator's job stays the same: drive chunks, hold the lock, save checkpoints, gate approvals.

One small addition: the orchestrator must write the run snapshot to `pipeline_runs.llm_config_snapshot` once, at run creation time, before any chunk starts. (See section 6.)

---

## 2. LLM-M1 Design (Implement Now)

Goal: Stop hardcoding model names. Let a project configure (provider, model) per role. Make every LLM call attributable in audit. Keep the current single-instance SQLite world.

### 2.1 Configuration scope: project-level in M1

You asked whether config should be global, project-level, or run-level. The answer:

- **Project-level: yes.** This is where M1 lives.
- **Run-level override: no.** Adds UI surface, adds a "stale config" debugging case, no real user demand yet.
- **Global default: yes, as a fallback.** A "system default" exists so first-install works with whatever API key is in the env.

Precedence (most → least specific): **run snapshot → project config → system default**. The run snapshot is what's actually used at execution time; the other two only feed into snapshot creation.

### 2.2 What lives in DB vs env in M1

| Thing | Where | Why |
|-------|-------|-----|
| API keys | **env vars only** | Encryption is a known limitation; the GitHub token issue is the precedent. Do not introduce a second encrypted-secrets problem in M1. |
| Provider/model selection per role | DB (`project_llm_config`) | Per-project. |
| Run snapshot | DB (`pipeline_runs.llm_config_snapshot` JSON column) | Immutable for the run's lifetime. |
| Per-call audit | DB (`llm_calls` table) | One row per attempt. |
| Capability metadata | **Static Python module** | Known list, no need to make this dynamic in M1. |

### 2.3 Schema (SQLite, additive migration)

```sql
-- Migration: 0002_llm_config.sql

CREATE TABLE IF NOT EXISTS project_llm_config (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    role          TEXT NOT NULL,           -- triage|planner|architect|coder|reviewer|summary
    provider      TEXT NOT NULL,           -- gemini|anthropic|openai
    model         TEXT NOT NULL,           -- pinned model id
    temperature   REAL DEFAULT 0.2,
    max_output_tokens INTEGER DEFAULT 4000,
    timeout_seconds   INTEGER DEFAULT 120,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    UNIQUE (project_id, role)
);

CREATE INDEX IF NOT EXISTS ix_project_llm_config_project
    ON project_llm_config(project_id);

-- Snapshot column on pipeline_runs
ALTER TABLE pipeline_runs ADD COLUMN llm_config_snapshot TEXT;   -- JSON blob

-- Per-call audit
CREATE TABLE IF NOT EXISTS llm_calls (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    chunk_number  INTEGER,
    role          TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    status        TEXT NOT NULL,           -- success | parse_error | provider_error | timeout | rate_limit | auth_error
    attempt_number INTEGER DEFAULT 1,
    tokens_input  INTEGER,
    tokens_output INTEGER,
    latency_ms    INTEGER,
    error_class   TEXT,
    error_message_redacted TEXT,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

CREATE INDEX IF NOT EXISTS ix_llm_calls_run ON llm_calls(run_id);
CREATE INDEX IF NOT EXISTS ix_llm_calls_status ON llm_calls(status);
```

`llm_config_snapshot` JSON shape:

```json
{
  "snapshot_version": 1,
  "captured_at": "2026-05-26T10:14:22Z",
  "roles": {
    "triage":    { "provider": "gemini",    "model": "gemini-2.5-flash-lite", "temperature": 0.2, "max_output_tokens": 2000, "timeout_seconds": 60 },
    "planner":   { "provider": "anthropic", "model": "claude-sonnet-4-5",     "temperature": 0.2, "max_output_tokens": 2000, "timeout_seconds": 120 },
    "architect": { "provider": "anthropic", "model": "claude-sonnet-4-5",     "temperature": 0.2, "max_output_tokens": 2000, "timeout_seconds": 120 },
    "coder":     { "provider": "gemini",    "model": "gemini-2.5-flash-lite", "temperature": 0.2, "max_output_tokens": 8000, "timeout_seconds": 120 },
    "reviewer":  { "provider": "anthropic", "model": "claude-sonnet-4-5",     "temperature": 0.4, "max_output_tokens": 2000, "timeout_seconds": 120 },
    "summary":   { "provider": "gemini",    "model": "gemini-2.5-flash-lite", "temperature": 0.3, "max_output_tokens": 1000, "timeout_seconds": 60 }
  }
}
```

### 2.4 Why M1 supports two providers, not three

**Pushback on the spec.** Your tool strategy lists Gemini, Anthropic, **and** OpenAI. M1 should ship with **Gemini + Anthropic only**, with OpenAI deferred to LLM-M1.x or LLM-M2. Reasons:

1. Each new provider is an adapter to write, test, error-map, and capability-document. Three adapters in one milestone is roughly tripling M1 surface area.
2. The dominant claim of multi-LLM ("reviewer must differ from coder, ideally a stronger model for review") is satisfied by `Gemini coder + Anthropic reviewer`. Adding OpenAI does not test the abstraction more; it tests the same abstraction once more.
3. OpenAI's API is the most divergent of the three (Chat Completions vs Responses API, structured outputs differ between them). Best to learn the abstraction with two providers first, then add OpenAI knowing where the rough edges actually are.

If you disagree, add OpenAI in M1, but understand the milestone roughly doubles in cost and tests at least double in count.

### 2.5 Validation before run start (mandatory)

Before a chunked run begins execution, run a **pre-flight check**:

1. For each role in the snapshot:
   - Look up `(provider, model)` in capability metadata. If unknown → fail.
   - Check the provider's required env var (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`) is set. If not → fail with a clear message naming the missing variable.
   - Call `provider.validate_api_key()` (format check, no network). If invalid → fail.
2. For each unique provider in the snapshot, call `provider.ping()` once. If any fails → fail the run.
3. If `roles.coder.model == roles.reviewer.model AND roles.coder.provider == roles.reviewer.provider`, **warn but do not fail.** Show the warning in the run UI: "Reviewer is using the same model as Coder; adversarial review is weaker in this configuration."

The result of pre-flight is stored in the run's audit trail. If pre-flight fails, no LLM call is made, no chunks execute, no DB writes happen except the failure record.

### 2.6 Capability metadata (M1 scope)

A static Python module. Updated by code change, not by user input.

```python
# backend/llm/capabilities.py

from typing import Literal
from dataclasses import dataclass

CostTier = Literal["cheap", "mid", "premium"]

@dataclass(frozen=True)
class ModelCapability:
    provider: str
    model: str
    display_name: str
    context_window: int
    supports_json_mode: bool
    supports_tools: bool          # for future; not used in M1
    supports_streaming: bool      # for future; not used in M1
    cost_tier: CostTier
    is_deprecated: bool
    notes: str

CAPABILITIES: tuple[ModelCapability, ...] = (
    ModelCapability(
        provider="gemini", model="gemini-2.5-flash-lite",
        display_name="Gemini 2.5 Flash Lite",
        context_window=1_048_576,
        supports_json_mode=True, supports_tools=True, supports_streaming=True,
        cost_tier="cheap", is_deprecated=False,
        notes="Free-tier rate limits are aggressive (20 RPD). Plan for 429s.",
    ),
    ModelCapability(
        provider="gemini", model="gemini-2.5-pro",
        display_name="Gemini 2.5 Pro",
        context_window=2_097_152,
        supports_json_mode=True, supports_tools=True, supports_streaming=True,
        cost_tier="premium", is_deprecated=False,
        notes="Best for large-context tasks.",
    ),
    ModelCapability(
        provider="anthropic", model="claude-sonnet-4-5",
        display_name="Claude Sonnet 4.5",
        context_window=200_000,
        supports_json_mode=True, supports_tools=True, supports_streaming=True,
        cost_tier="mid", is_deprecated=False,
        notes="Strong reviewer / architect.",
    ),
    ModelCapability(
        provider="anthropic", model="claude-opus-4-7",
        display_name="Claude Opus 4.7",
        context_window=200_000,
        supports_json_mode=True, supports_tools=True, supports_streaming=True,
        cost_tier="premium", is_deprecated=False,
        notes="Strongest reasoning; expensive.",
    ),
    # OpenAI added in M1.x / M2
)
```

**Note on model IDs:** these reflect publicly current names as of writing. They will go stale. The capability table is the only place that needs updating when a provider releases a new model. Pipeline stages are unaffected.

### 2.7 Default role assignments (M1 ships with these)

| Role | Default provider | Default model | Reasoning |
|------|-------------------|---------------|-----------|
| `triage` | gemini | gemini-2.5-flash-lite | Cheap, structured output is reliable enough, low blast radius. |
| `planner` | anthropic | claude-sonnet-4-5 | Planning rewards careful reasoning; not on the critical token-heavy path. |
| `architect` | anthropic | claude-sonnet-4-5 | Same as planner; architecture decisions are high-leverage. |
| `coder` | gemini | gemini-2.5-flash-lite | Cost-dominant role. Cheap model with large output budget. |
| `reviewer` | anthropic | claude-sonnet-4-5 | Must differ from coder. Stronger reasoning model boosts adversarial reviews. |
| `summary` | gemini | gemini-2.5-flash-lite | Cheap; output is short prose, not code. |

**A first-install user with only `GEMINI_API_KEY` set** gets a degraded default: every role falls back to Gemini, with a banner: "Reviewer is using the same provider/model as Coder. Add `ANTHROPIC_API_KEY` for stronger adversarial review."

This is intentional: the product still works with one key, but loudly says it's not at its best.

---

## 3. Role Model Assignment Rules

### 3.1 Constraints

| Constraint | Hard or soft? | M1 enforcement |
|-----------|----------------|-----------------|
| Reviewer must use a different *model* than coder | Soft | Warn in pre-flight; show banner in run UI. Do not block. |
| Reviewer should use a different *provider* than coder when possible | Soft | Same as above. |
| Planner and coder can use the same model | OK | No enforcement. |
| Triage can use the cheapest model available | OK | Default is cheapest known. |
| Triage may use local models later | M3 only | M1 does not support local. |
| Summary role may be omitted | OK | If not configured, falls back to whatever model produced the final review. |

The "must differ" rule for reviewer/coder is soft in M1 because someone with one API key still needs the pipeline to work. Hard enforcement waits for LLM-M2 where the UI shows a per-key health badge and the user actually knows whether they can comply.

### 3.2 Which roles can use cheap models

| Role | Cheap OK? | Why |
|------|-----------|-----|
| triage | Yes | Outputs chunk plan JSON; structure beats nuance. Human approves the plan anyway. |
| planner | Probably no | Planner sets the trajectory; mistakes cascade. |
| architect | No | Highest-leverage decisions per token. |
| coder | Yes-ish | Cheap for bulk lines; quality matters for tricky changes. The chunk plan limits blast radius. |
| reviewer | **No.** | Sycophancy risk dominates cost. Cheap reviewers approve bad code. |
| summary | Yes | Short prose. |

### 3.3 Which roles need coding strength

Only `coder`. Everyone else handles structure, prose, or critique. Architect needs *understanding* of code, not generation of it. This matters for capability metadata: a hypothetical `text-only` model could serve planner/reviewer/summary but not coder.

---

## 4. Provider Abstraction Design

### 4.1 Base interface

See section 1.2 for the protocol. Repeated here with the full error taxonomy.

```python
# backend/llm/errors.py

class LLMError(Exception):
    """Base. All provider errors normalize to a subclass of this."""
    provider: str
    model: str | None
    retryable: bool

class ProviderAuthError(LLMError):
    retryable = False             # do not retry; user must fix key

class ProviderRateLimitError(LLMError):
    retryable = True
    retry_after_seconds: int | None      # parsed from header if available

class ProviderTimeoutError(LLMError):
    retryable = True

class ProviderServerError(LLMError):       # 5xx
    retryable = True

class ProviderClientError(LLMError):       # 4xx other than 401/429
    retryable = False

class ProviderResponseEmptyError(LLMError):
    retryable = True             # one retry, then surface to caller

class ProviderResponseInvalidError(LLMError):
    """Provider returned non-JSON when JSON was requested, or violated
    the response schema in a way the provider should have caught."""
    retryable = True             # one correction-prompt retry

class ProviderModelDeprecatedError(LLMError):
    retryable = False

class ProviderUnsupportedModelError(LLMError):
    retryable = False
```

### 4.2 Retry policy (provider-owned)

Each provider exposes:

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2           # initial + 1 retry
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    honor_retry_after: bool = True  # use server-provided Retry-After if present
```

M1: retry only on `ProviderRateLimitError`, `ProviderTimeoutError`, `ProviderServerError`, `ProviderResponseEmptyError`. Exponential backoff with jitter, capped at `max_delay_seconds`. **Always honor `Retry-After` when given.** The current "always sleep 60 seconds" is a bug.

`ProviderResponseInvalidError` (JSON malformed) goes through a different path: it triggers the **single correction-prompt retry** already used by `planner.py`/`coder.py`, but **on the same provider/model**. We do not cross-provider fall back in M1.

### 4.3 Token usage capture

`LLMResponse` includes `tokens_input` and `tokens_output`. Each provider parses these from its own response shape:

| Provider | Field path |
|----------|-----------|
| gemini | `response.usage_metadata.prompt_token_count`, `candidates_token_count` |
| anthropic | `response.usage.input_tokens`, `output_tokens` |
| openai (later) | `response.usage.prompt_tokens`, `completion_tokens` |

If the provider doesn't return usage, store `None` and log it. Never invent numbers.

### 4.4 Response sanitization

Provider responses can contain:
- Echoed parts of the user's content (potentially a path or filename — not secret on its own).
- Provider-side warning messages that name the API endpoint or version.
- Rare cases: echoed API keys if the caller embedded one in a prompt (we never do, but defense in depth).

Before any response is logged, it passes through the same secret regex set used in the memory architecture document (section 5.1 there): OpenAI keys, Gemini keys, GitHub tokens, PEM blocks, JWTs. Any match is redacted with `[REDACTED:type]`. We log redacted text; we never log raw response body in M1.

### 4.5 API key validation (format-only, no network in M1 hot path)

```python
# backend/llm/providers/gemini.py
def validate_api_key(self) -> None:
    key = settings.gemini_api_key
    if not key:
        raise ProviderAuthError(provider="gemini", model=None,
            retryable=False, message="GEMINI_API_KEY is not set")
    if not key.startswith("AIza") or len(key) < 35:
        raise ProviderAuthError(provider="gemini", model=None,
            retryable=False, message="GEMINI_API_KEY format looks invalid")
```

This is heuristic and **will** false-positive on legitimate keys outside the common format. The pre-flight `ping()` is the real check; `validate_api_key` is a fast pre-screen.

### 4.6 Model support validation

```python
def supports_model(self, model: str) -> bool:
    return any(c.provider == self.name and c.model == model and not c.is_deprecated
               for c in CAPABILITIES)
```

Run validation calls this before pre-flight ping. An unknown or deprecated model fails fast with a clear message.

### 4.7 Streaming

**Not in M1.** All M1 calls are non-streaming. Streaming complicates retry, token counting, error mapping, and audit. Add it when there is a concrete UX justification (live coder typing in the UI), not before.

### 4.8 Keeping provider-specific logic isolated

The rule: nothing outside `backend/llm/providers/{provider}.py` may import the provider SDK. Anywhere else, you import from `backend/llm/`. Enforce by code review and by a unit test that asserts `import google.generativeai` does not appear outside the gemini adapter:

```python
def test_no_provider_sdk_leak_outside_adapters():
    project_root = Path(__file__).parent.parent.parent
    offending = []
    for py in project_root.rglob("*.py"):
        if "/llm/providers/" in str(py): continue
        if "/tests/" in str(py): continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "import google.generativeai" in text or \
           "import anthropic" in text or \
           "import openai" in text:
            offending.append(str(py))
    assert not offending, f"Provider SDK leaked outside adapters: {offending}"
```

This is a small test that prevents an entire class of decay over time.

---

## 5. Model Capability Metadata — What M1 Uses, What Can Wait

Of the fields you listed, this is what M1 actually needs vs. defers:

| Field | M1 uses? | Why |
|-------|----------|-----|
| `provider`, `model`, `display_name` | Yes | Identity. |
| `context_window` | Yes | Pre-flight: warn if estimated prompt > window. |
| `supports_json_mode` | Yes | Refuse role assignment if role needs JSON and model doesn't support it. |
| `cost_tier` | Yes (display only) | UI shows a label so user knows what they picked. |
| `is_deprecated` | Yes | Pre-flight refuses deprecated models. |
| `notes` | Yes | Surfaced in tooltip. |
| `supports_tools` | No (M1) | No tool use in M1. |
| `supports_streaming` | No (M1) | No streaming in M1. |
| `best_for` roles | No (M1) | This is M3 routing input. Static `best_for` lists go stale instantly. |
| `reliability_tier` | No (M1) | Subjective; needs benchmarking (M3). |
| `is_local` | No (M1) | Local model support is M3. |

So M1 actually only uses 7 fields. Don't overbuild the metadata table now.

---

## 6. Database / API / UI Design for LLM-M1

Schemas in section 2.3. Pydantic and routes here.

### 6.1 Pydantic

```python
# backend/models/llm_config.py

from pydantic import BaseModel, Field, field_validator
from typing import Literal

Role = Literal["triage", "planner", "architect", "coder", "reviewer", "summary"]
Provider = Literal["gemini", "anthropic"]   # add "openai" in M1.x / M2

class RoleConfig(BaseModel):
    provider: Provider
    model: str = Field(min_length=1, max_length=80)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=4000, ge=64, le=64_000)
    timeout_seconds: int = Field(default=120, ge=10, le=600)

    @field_validator("model")
    @classmethod
    def must_be_in_capability_table(cls, v, info):
        provider = info.data.get("provider")
        if not provider:
            return v
        from backend.llm.capabilities import CAPABILITIES
        if not any(c.provider == provider and c.model == v and not c.is_deprecated
                   for c in CAPABILITIES):
            raise ValueError(f"Unknown or deprecated model: {provider}/{v}")
        return v

class ProjectLLMConfig(BaseModel):
    project_id: str
    roles: dict[Role, RoleConfig]

class LLMConfigSnapshot(BaseModel):
    snapshot_version: int = 1
    captured_at: str          # ISO8601
    roles: dict[Role, RoleConfig]
```

### 6.2 API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/projects/{project_id}/llm-config` | Get the project's role mappings (merged with defaults). |
| `PUT` | `/projects/{project_id}/llm-config` | Replace the entire role mapping. Atomic. |
| `PATCH` | `/projects/{project_id}/llm-config/{role}` | Update one role. |
| `POST` | `/projects/{project_id}/llm-config/preflight` | Run pre-flight check; returns per-role pass/fail. |
| `GET` | `/llm/capabilities` | List available models with metadata. Read-only. |
| `GET` | `/runs/{run_id}/llm-snapshot` | Read the run snapshot. |
| `GET` | `/runs/{run_id}/llm-calls` | List `llm_calls` rows for the run. Optionally filter by role. |

**Notable absences (deliberate):** no API key management endpoint in M1. Keys live in env vars. The UI shows whether a key is set (`has_gemini_key: true`), never the key itself.

### 6.3 Example request/response

**PUT /projects/proj-13605886/llm-config:**

```json
{
  "roles": {
    "triage":    { "provider": "gemini",    "model": "gemini-2.5-flash-lite" },
    "planner":   { "provider": "anthropic", "model": "claude-sonnet-4-5" },
    "architect": { "provider": "anthropic", "model": "claude-sonnet-4-5" },
    "coder":     { "provider": "gemini",    "model": "gemini-2.5-flash-lite", "max_output_tokens": 8000 },
    "reviewer":  { "provider": "anthropic", "model": "claude-sonnet-4-5", "temperature": 0.4 },
    "summary":   { "provider": "gemini",    "model": "gemini-2.5-flash-lite" }
  }
}
```

**POST /projects/proj-13605886/llm-config/preflight:**

```json
{
  "ok": false,
  "results": [
    { "role": "triage",    "provider": "gemini",    "model": "gemini-2.5-flash-lite", "ok": true,  "checks": ["key_present", "format_ok", "ping_ok"] },
    { "role": "planner",   "provider": "anthropic", "model": "claude-sonnet-4-5",     "ok": false, "checks": ["key_present"], "error": "ANTHROPIC_API_KEY is not set" },
    { "role": "architect", "provider": "anthropic", "model": "claude-sonnet-4-5",     "ok": false, "checks": ["key_present"], "error": "ANTHROPIC_API_KEY is not set" },
    { "role": "coder",     "provider": "gemini",    "model": "gemini-2.5-flash-lite", "ok": true,  "checks": ["key_present", "format_ok", "ping_ok"] },
    { "role": "reviewer",  "provider": "anthropic", "model": "claude-sonnet-4-5",     "ok": false, "checks": ["key_present"], "error": "ANTHROPIC_API_KEY is not set" },
    { "role": "summary",   "provider": "gemini",    "model": "gemini-2.5-flash-lite", "ok": true,  "checks": ["key_present", "format_ok", "ping_ok"] }
  ],
  "warnings": [
    "Reviewer would use the same provider/model as Coder if you proceed without Anthropic. Add ANTHROPIC_API_KEY for stronger adversarial review."
  ]
}
```

### 6.4 Frontend surfaces (M1)

1. **Project Settings → LLM Config tab.** Per-role dropdowns: provider, model. Temperature and max-tokens behind a "Show advanced" toggle. "Test configuration" button → calls `/preflight`. Results shown per-role with a green/red dot.

2. **Run detail → LLM tab.** Read-only display of the run snapshot. Per-role row showing `provider/model` used.

3. **Run detail → LLM Calls.** Table of `llm_calls` rows: timestamp, role, provider, model, status, tokens in/out, latency, error class. This is what answers "Which model wrote chunk 2 of this run?"

4. **Run header banner** (when warnings exist). E.g., "Reviewer is using the same model as Coder."

5. **Live logs** include `provider` and `model` in each LLM-related event. Format: `[CODER provider=gemini model=gemini-2.5-flash-lite] Token usage | input=412 | output=2103`.

### 6.5 Validation rules

- `roles.coder.max_output_tokens` cannot exceed the model's `context_window / 4` (rule of thumb; prevents user from picking 64k output on a 32k model — yes, the Pydantic check should know about context window). M1 implementation: lookup capability, clamp with warning.
- `temperature` capped at 1.0 by Pydantic even though the API accepts up to 2.0; high temperatures with JSON output are a footgun.
- Removing a role from the PUT body is not allowed in M1. Full role set is required. (Partial updates go through PATCH.)

### 6.6 Tests (M1)

In `backend/tests/test_llm_*.py`:

1. `test_provider_registry_returns_correct_instance`
2. `test_unknown_provider_raises`
3. `test_unsupported_model_raises_in_pydantic_validator`
4. `test_deprecated_model_rejected_by_preflight`
5. `test_missing_api_key_fails_preflight`
6. `test_invalid_api_key_format_fails_validate_api_key`
7. `test_gemini_adapter_normalizes_response` (mocked SDK)
8. `test_anthropic_adapter_normalizes_response` (mocked SDK)
9. `test_gemini_adapter_normalizes_429_to_ProviderRateLimitError`
10. `test_anthropic_adapter_normalizes_429_to_ProviderRateLimitError`
11. `test_retry_honors_retry_after_header`
12. `test_retry_does_not_retry_on_auth_error`
13. `test_llm_call_recorded_on_success`
14. `test_llm_call_recorded_on_failure`
15. `test_run_snapshot_captured_at_run_start`
16. `test_resume_uses_run_snapshot_not_project_config` — change project config mid-run; resume must use the original snapshot
17. `test_response_secret_redaction_strips_keys`
18. `test_no_provider_sdk_leak_outside_adapters` (the linter test from section 4.8)
19. `test_warning_when_reviewer_equals_coder`
20. `test_pipeline_planner_uses_role_config` — set planner to Anthropic, mock anthropic adapter, verify it gets called and gemini does not
21. `test_preflight_failure_blocks_run_start` — chunked run creation must fail when preflight fails
22. **Smoke:** existing 1-chunk and 2-chunk smoke tests from `docs/phase2b-smoke-tests.md` must pass with `gemini` configured for all roles.

---

## 7. Integration with Current Pipewright Flow

Per-stage integration in LLM-M1:

| Stage | LLM config used | Code touch |
|-------|------------------|------------|
| Project create/edit | Project LLM config saved to `project_llm_config`. Defaults applied if user does not configure. | New endpoints; project routes module. |
| Chunked run creation | **Pre-flight runs here.** If pre-flight fails, run creation returns 422 with the per-role report. If pre-flight passes, `pipeline_runs.llm_config_snapshot` is written before any chunk row is created. | `backend/routes/runs_chunks.py`. |
| Chunk planning (triage) | Uses `snapshot.roles.triage`. | `backend/pipeline/triage.py` |
| Chunk plan approval (human) | Read-only display of model used. | UI only. |
| Per-chunk planner | Uses `snapshot.roles.planner`. | `backend/pipeline/planner.py` |
| Per-chunk architect (if used) | Uses `snapshot.roles.architect`. | `backend/pipeline/architect.py` (may not exist; if missing, OK — orchestrator skips). |
| Per-chunk coder | Uses `snapshot.roles.coder`. | `backend/pipeline/coder.py` |
| Per-chunk tests | No LLM call. | — |
| Per-chunk reviewer | Uses `snapshot.roles.reviewer`. | `backend/pipeline/reviewer.py` (**likely needs to be added**; see Finding #3). |
| High-risk per-chunk approval summary | Uses `snapshot.roles.summary` if configured, else falls back to `reviewer` role. | `chunked_orchestrator.py:_chunk_approval_summary`. |
| Final approval summary | Same as above. | Approval gate code. |
| Push + PR | No LLM call. | — |
| Run resume / recovery | **Reuses `snapshot.roles`** — never re-reads project config. | `chunked_orchestrator.py` resume path. |

The single most important rule: **resume reads from the snapshot, not from the project config.** A user can change their project's role assignment at any time; an in-flight run keeps the configuration it started with. This avoids "the run is half-Claude, half-Gemini because the user switched mid-run."

---

## 8. Auditability and Observability

### 8.1 What is captured per LLM call

`llm_calls` row written **after every attempt**, success or failure:

- `run_id`, `chunk_number`, `role`
- `provider`, `model`
- `attempt_number` (1 for first attempt, 2 for correction-prompt retry, etc.)
- `status`: `success | parse_error | provider_error | timeout | rate_limit | auth_error | empty_response`
- `tokens_input`, `tokens_output` (nullable if provider didn't return them)
- `latency_ms`
- `error_class`, `error_message_redacted` (nullable on success)

`llm_calls` is **append-only**. Never UPDATE existing rows.

### 8.2 What is captured per run

- `pipeline_runs.llm_config_snapshot` — the JSON blob.
- Pre-flight result is stored in a `pipeline_runs.preflight_report` column (or a separate `run_preflight` row, but a JSON column on `pipeline_runs` is fine in SQLite M1).

### 8.3 Live log shape

Current logs look like `[CODER] Token usage | input=412 | output=2103`. New shape:

```
[CODER provider=gemini model=gemini-2.5-flash-lite attempt=1] Calling provider...
[CODER provider=gemini model=gemini-2.5-flash-lite attempt=1] Token usage | input=412 | output=2103 | latency_ms=1840
[CODER provider=gemini model=gemini-2.5-flash-lite attempt=1] Validated handoff
```

`provider=` and `model=` are key-value pairs so live-log filtering can extract them. The `attempt=N` field is critical for spotting retries; right now it's not visible.

### 8.4 Secrets in error paths

Provider error messages occasionally include partial request payloads (Gemini sometimes echoes path fragments, Anthropic includes the model name in error JSON). Before persisting `error_message_redacted`:

1. Run the secret regex set from the memory architecture doc.
2. Truncate to 500 chars.
3. Strip control characters and ANSI escapes.

Never include API keys in error messages, even if the SDK gives them. Be paranoid.

### 8.5 Answering "Which model wrote this code?"

A query like:

```sql
SELECT chunk_number, role, provider, model, attempt_number, status, latency_ms
FROM llm_calls
WHERE run_id = ?
ORDER BY created_at;
```

answers the audit question. The UI's "Run detail → LLM Calls" tab renders this directly. The snapshot answers "what was the *intended* configuration"; `llm_calls` answers "what *actually* ran."

---

## 9. Critical Failure Cases and Mitigations

Adversarial matrix. Each row is something that will happen.

| # | Failure mode | Detection | Mitigation |
|---|--------------|-----------|------------|
| 1 | API key missing | Pre-flight `validate_api_key()` | Run creation fails; UI shows which env var is missing. |
| 2 | Invalid provider name | Pydantic `Literal["gemini","anthropic"]` | 422 at config save time, before any run. |
| 3 | Unsupported model | Capability table check in validator | 422 at config save time. |
| 4 | Provider timeout | Provider adapter raises `ProviderTimeoutError` | One retry with backoff; if still failing, surface to caller; checkpoint not saved. |
| 5 | Provider rate limit (429) | Provider adapter raises `ProviderRateLimitError` with `retry_after` | Sleep for `retry_after` (clamped to 120s); one retry; then fail. Log per-attempt. |
| 6 | Provider 5xx | `ProviderServerError` | Retry with exponential backoff (1s → 4s); then fail. |
| 7 | Provider auth failure (401/403) | `ProviderAuthError` | **Not retryable.** Fail the run. UI shows: "API key for {provider} was rejected. Re-check it." |
| 8 | Provider returns malformed JSON | `ProviderResponseInvalidError` | Correction-prompt retry on same provider; if still bad, fail the run. |
| 9 | Provider returns empty output | `ProviderResponseEmptyError` | One retry; then fail. Log. |
| 10 | Provider returns unsafe output (content filter triggered) | `finish_reason="content_filter"` | Fail the run; surface to human. Do **not** retry — silent retries hide policy issues. |
| 11 | Provider ignores JSON schema | Same as #8 | Same as #8. |
| 12 | Model context window too small | Pre-flight checks estimated input vs. `context_window` | Block before run; recommend a larger model. |
| 13 | Selected model lacks coding ability | M1: not modeled (`coder` role accepts any model in capability table). Document as known limitation. | The capability table can grow a `coding_capable: bool` in LLM-M2. |
| 14 | Reviewer == coder | Pre-flight warning | Banner in UI. Not blocked. |
| 15 | Cheap triage underestimates risk | Cannot be detected by software in M1 | **The chunk plan is human-approved.** This is the only mitigation. Surfaced in run UI: "Triage used cheap model; review the chunk plan carefully." |
| 16 | Expensive model accidentally used for every role | Pre-flight summary shows cost tier per role | UI banner: "All roles set to premium tier. This will be expensive." |
| 17 | User changes config mid-run | Run uses snapshot, not live config | Change takes effect on next run only. UI shows: "Config changes apply to the next run." |
| 18 | Resume uses different model than original | **Resume reads snapshot, never project config** | Snapshot is immutable. Test #16 in section 6.6. |
| 19 | Provider/model deprecated after run starts | Snapshot still references it; provider adapter still calls it | Provider may return its own deprecation error; we map to `ProviderModelDeprecatedError` and fail the run. Acceptable. |
| 20 | Provider changes model behavior silently | Cannot be detected by software | This is the documented permanent risk. Mitigation: capability table tracks model IDs we've validated; encourage users to pin versions. |
| 21 | Different providers have different token usage formats | Provider adapter normalizes | Already handled by `LLMResponse`. |
| 22 | Different providers use different message schemas | Provider adapter builds the schema | Pipeline never sees provider-shape. |
| 23 | Streaming response interrupted | M1 disables streaming | Not applicable. |
| 24 | Fallback model produces inconsistent output | M1 has no fallback | Deferred to LLM-M2. |
| 25 | Provider fallback hides real failures | Same as #24 | LLM-M2 fallback design must log original failure prominently. |
| 26 | User selects a model without JSON support for handoff | Pre-flight rejects | Capability table flags it. |
| 27 | User selects a local model that cannot follow instructions | M1 doesn't support local | LLM-M3. |
| 28 | Model output violates chunk scope (writes outside `files_to_modify`) | `patch_applier` already enforces this | Existing safeguard; LLM-M1 does not change. |
| 29 | Model tries to modify files outside allowed paths | Same as #28 | Same. |
| 30 | Coder/reviewer sycophancy | Reviewer != coder soft rule + adversarial reviewer prompt | M1 does soft enforcement + warning. Hard enforcement waits for LLM-M2. |
| 31 | Multi-model architecture disagreement | M1 has only one architect role | Not yet a problem; LLM-M3 introduces multi-architect debate. |
| 32 | Cost explodes from too much context | Pre-flight estimates input tokens vs. model's window | M1: warn at 80% of context window per role. |
| 33 | Provider call OK, DB audit write fails | DB write attempted in `try/except`; failure logged but not propagated to caller | The pipeline continues; the audit row is lost. **Document this trade-off.** Alternative — abort the run on audit failure — is worse: a flaky DB would kill all runs. |
| 34 | DB audit write OK, provider call fails | Caller raises; pipeline marks run failed. Audit row already records the failure. | Normal failure path. |
| 35 | Live log says model succeeded but audit row missing | Symptom of #33 | UI's "LLM Calls" view will show missing rows; live logs are best-effort, the DB is authoritative. |
| 36 | Cross-project provider config leakage | All queries scoped by `project_id`; FK + tests | Test #21 (cross-project) belongs in `tests/test_llm_config.py`. |
| 37 | Project A's snapshot used for Project B | Snapshot stored under `pipeline_runs.id` which has `project_id` FK | Read via run, not via project. |
| 38 | Secrets shown in provider error | Redaction in section 8.4 | Tested via #17 in section 6.6. |
| 39 | User changes config while run is paused | Snapshot prevails on resume (test #16) | Resume uses original snapshot. |
| 40 | Manual override → unsafe model choice | Pre-flight + warning + human approval gate | The human approval gate is the final backstop. The model choice does not bypass it. |
| 41 | No provider configured on first install | Defaults fill in Gemini for all roles if `GEMINI_API_KEY` set; otherwise pre-flight fails with clear message | First-install UX is "set GEMINI_API_KEY, run". |

---

## 10. Safety Rules (Non-Negotiable)

1. **API keys never leave env vars in M1.** Not stored in DB, not returned by any API, not logged.
2. **Provider/model strings entering execution are validated against the capability table.** No free-text model IDs.
3. **The run snapshot is immutable for the run's lifetime.** Resume uses it. No re-read from project config.
4. **Reviewer differs from coder when possible.** Soft warn now; hard enforce in LLM-M2 with cost dashboard.
5. **Source code and tests beat model output.** No multi-LLM change weakens this. The patch_applier rejects out-of-scope changes regardless of which model produced them.
6. **Human approval gates remain mandatory regardless of provider/model.** Auto-routing (LLM-M3) does not bypass approvals; routing decisions themselves can be human-overridden.
7. **Provider fallback is explicit, logged, and disabled in M1.** No silent degradation.
8. **If model output schema validation fails after the single correction retry, the run fails.** No "third try" loop. No fallback to another model.
9. **If a role's required capability (e.g., JSON mode) is missing, the run fails pre-flight.** Never start execution against an unfit model.
10. **All LLM call outcomes are recorded in `llm_calls`** before the next stage runs. Audit row failure is logged but does not abort the run (Failure Mode #33); this trade-off is documented.
11. **Response and error text are redacted for secrets before being persisted or logged.**

---

## 11. Prompt and Handoff Contract Strategy

### 11.1 Role-specific prompts in multi-LLM

Today each pipeline stage has a `*_SYSTEM_PROMPT` constant. That stays. What changes:

- The system prompt is **provider-neutral**. No "Gemini, please return JSON" — just "Respond with a JSON object."
- The provider adapter is responsible for translating the system prompt into the provider's required shape (Gemini `system_instruction=`, Anthropic `system=`, OpenAI `messages=[{"role":"system"}]`).
- Each role gets one prompt, not one prompt per provider. Provider-specific phrasing is an anti-pattern; if a model can't follow a clear prompt, it's the wrong model.

### 11.2 JSON enforcement across providers

| Provider | JSON enforcement mechanism |
|----------|----------------------------|
| Gemini | `generation_config.response_mime_type = "application/json"` |
| Anthropic | Tool use with a schema, or system-prompt instruction + `<output>` parsing |
| OpenAI (later) | `response_format={"type":"json_schema", "json_schema": ...}` |

In M1, every adapter requests JSON mode when `LLMRequest.json_mode=True`. The pipeline never sees the provider-specific knob.

For Anthropic specifically, M1 should use **structured tool use** rather than plain-text JSON output, because Anthropic's JSON-in-prose adherence is weaker than its tool-use adherence. The adapter handles this; the pipeline doesn't care.

### 11.3 Malformed JSON retry

Single correction-prompt retry, on the same provider/model. If retry fails, raise `ProviderResponseInvalidError` and let the run fail. No third try, no cross-provider fallback. (Failure Mode #11.)

### 11.4 Handoff contracts stay provider-neutral

The `PlannerHandoff`, `CoderHandoff` Pydantic schemas don't change. They are typed Python objects, not provider-specific. Multi-LLM does not touch them — the entire point of structured handoffs is provider independence.

### 11.5 Never pass entire conversation history across roles

This rule survives multi-LLM. The handoff contract is the only thing passed between roles. Mixing this with multi-LLM is dangerous in one specific way: do not pass *the raw text response from a previous role's model* to a different role's model. The handoff Pydantic object goes through. The raw text doesn't.

Why this matters with multi-LLM: in single-provider land, you could get away with sloppy text passing because the same model could parse its own output. Across providers, formatting quirks differ. Stick to Pydantic.

### 11.6 Memory injection + role routing

Memory injection (per the memory architecture doc) happens **inside the pipeline stage**, before the call to `provider.generate()`. The provider doesn't know about memory. The memory block is part of the user prompt, formatted identically regardless of provider. The 1500-token memory budget from the memory doc applies regardless of which provider serves the role.

---

## 12. LLM-M2 Design (Sketch — Do Not Build Now)

### 12.1 Provider fallback

A fallback chain like:

```yaml
coder:
  primary:  { provider: gemini,    model: gemini-2.5-flash-lite }
  fallback: { provider: anthropic, model: claude-sonnet-4-5 }
```

Triggered only on `ProviderServerError`, `ProviderRateLimitError` after primary retries exhausted. **Never** on `ProviderAuthError` or `ProviderResponseInvalidError` (those imply user/data problem, not provider health).

When fallback fires:
- Log a `fallback_triggered` event prominently.
- Record the call in `llm_calls` with `attempt_number=99` (sentinel for fallback) or a new `is_fallback` column.
- The reviewer should be informed in its prompt: "Coder ran on the fallback model. Pay closer attention to consistency with project conventions."

### 12.2 Cost tracking

A per-call cost column on `llm_calls`:
- `tokens_input * input_price + tokens_output * output_price`
- Prices come from a static `PRICING` table keyed by `(provider, model, effective_from)` so old runs cost what they cost.
- Aggregate views: per-run, per-project, per-day.

This is genuinely useful and not very risky to add. The reason it's M2 not M1 is purely scope discipline.

### 12.3 Provider health checks

A background task pings each configured provider every few minutes and stores results. UI shows green/yellow/red. Pre-flight uses the cache.

### 12.4 Per-provider retry policies

Today's "60 second blanket sleep" becomes per-provider:

| Provider | Initial backoff | Max delay | Honor Retry-After |
|----------|-----------------|-----------|-------------------|
| gemini | 5s | 90s | Yes |
| anthropic | 2s | 60s | Yes |
| openai | 1s | 30s | Yes |

### 12.5 API key validation UI

A page that lets the user enter keys, then makes a one-off `ping()` and shows whether the key works. **Storage of keys in DB requires encryption first** (the existing GitHub-token limitation). Until that is fixed, keys remain env-only. LLM-M2 does **not** add encrypted secret storage; that is its own milestone.

---

## 13. LLM-M3 Design (Sketch — Do Not Build Now)

### 13.1 Auto-routing inputs

Triage already produces a chunk plan with risk flags. Extend triage output:

```json
{
  "chunk_number": 1,
  "complexity": "hard",            // easy | medium | hard
  "risk_factors": ["migration", "security"],
  "estimated_files": 9,
  "estimated_input_tokens": 22000,
  "requires_architect": true,
  "suggested_models": {
    "planner": { "provider": "anthropic", "model": "claude-sonnet-4-5", "rationale": "9-file cross-cutting plan; reasoning model warranted" },
    "coder":   { "provider": "anthropic", "model": "claude-sonnet-4-5", "rationale": "Migration logic; cheap model insufficient" },
    "reviewer":{ "provider": "anthropic", "model": "claude-opus-4-7",   "rationale": "Security-flagged; strongest reviewer" }
  }
}
```

### 13.2 Routing rules

Examples:
- `complexity == "easy" AND no risk_factors` → cheap models throughout, skip architect.
- `complexity == "medium"` → mid-tier; architect skipped unless `requires_architect`.
- `complexity == "hard" OR "security" in risk_factors` → premium models for planner/architect/reviewer; coder mid-tier.

Rules are explicit, inspectable, and overridable by human before chunks run.

### 13.3 Human override before execution

Routing surfaces a "suggested assignments" panel. Human can override per-role per-chunk before approval. The override is recorded.

### 13.4 Routing feedback loop

When a run fails review or rejection, record the chunk's routing decision and the human override. Aggregate over time. **Do not auto-tune** — LLM-M3 surfaces the data; LLM-M4 (or beyond) could automate.

### 13.5 Local models / Ollama

A new `local` provider with `is_local=True` capability rows. Used only for `triage` and `summary` by default. **Critical:** local models often fail JSON adherence. Capability metadata should mark them honestly (`supports_json_mode=False` for most). Roles that need JSON cannot be assigned local-only models.

### 13.6 Cost-aware execution planning

Estimate cost per chunk before running. Surface to human. Allow "budget mode" — refuse to execute if estimated cost exceeds a per-run cap.

---

## 14. What NOT to Build Now (Strict)

Do not start any of these in LLM-M1:

- Auto-routing of any kind.
- Cross-provider fallback chains.
- Cost dashboards or per-run cost estimates.
- Organization-level model policies.
- Local model / Ollama support.
- Model benchmarking infrastructure.
- Per-chunk dynamic routing.
- Streaming responses.
- Tool use (function calling).
- API key storage in DB.
- Encrypted secrets for any provider key.
- New provider beyond Gemini and Anthropic. (OpenAI is LLM-M1.x or M2.)
- UI for editing capability metadata.
- Removing or weakening any human approval gate.
- Worker queue, Redis, Alembic, PostgreSQL, deployment changes.

If a feature feels borderline, default to "out." LLM-M1's only job is: replace hardcoded model names with per-role config, build a provider layer, snapshot it per run, audit every call.

---

## 15. Deliverables

| Milestone | Deliverable | Notes |
|-----------|-------------|-------|
| **LLM-M0** | This document committed to `docs/multi-llm-architecture.md`. | No code. |
| **LLM-M1.1** | Migration `0002_llm_config.sql`. Pydantic schemas for `RoleConfig`, `ProjectLLMConfig`, `LLMConfigSnapshot`. Capability table module. | Schema only; no callers wired yet. |
| **LLM-M1.2** | `backend/llm/` package: `provider.py`, `errors.py`, `registry.py`, `providers/gemini.py`, `providers/anthropic.py`. Capability validation. Format-level API key validation. Provider-side retry policy. SDK-leak lint test (section 4.8). | The provider layer. Stand-alone; pipeline stages not yet using it. |
| **LLM-M1.3** | Rewrite `planner.py`, `coder.py`, `triage.py`, and add `reviewer.py` to use `provider.generate(...)`. Remove direct `google.generativeai` imports. Remove module-level `*_MODEL` constants. Read role config from run snapshot. | The dangerous refactor. Existing smoke tests must keep passing. |
| **LLM-M1.4** | Run snapshot capture at chunked-run creation. Pre-flight validation. `llm_calls` audit writes. Live-log shape with `provider=`/`model=`. | Audit + safety. |
| **LLM-M1.5** | API endpoints (section 6.2). Frontend Project Settings → LLM Config tab. Run detail → LLM tab and LLM Calls tab. Banner when reviewer == coder. | UI. |
| **LLM-M2** | Fallback, cost tracking, provider health, per-provider retry policies, API key validation UI (env-only still). | Deferred. |
| **LLM-M3** | Auto-routing, local models, cost-aware planning, routing feedback loop. | Deferred. |

LLM-M1.3 is the riskiest step. It rewrites running code. The smoke tests in `docs/phase2b-smoke-tests.md` are the safety net.

---

## 16. Acceptance Criteria for LLM-M1

LLM-M1 is shippable when **all** of the following are true:

1. A project can configure `(provider, model)` for each of: `triage`, `planner`, `architect`, `coder`, `reviewer`, `summary` via API and UI.
2. Run creation writes the role assignment into `pipeline_runs.llm_config_snapshot` before any chunk executes.
3. Pipeline stages (planner, coder, triage, reviewer) call providers through the abstraction, not through `google.generativeai` directly. The lint test in section 4.8 passes.
4. Reviewer can be configured with a different `(provider, model)` than coder. The pipeline correctly routes the reviewer call to that provider.
5. Missing API key fails pre-flight. The run does not start. The error names the env var.
6. Unsupported or deprecated model fails pre-flight. The error names the model.
7. Malformed provider JSON output: one correction-prompt retry on the same provider; if still bad, the run fails cleanly with a clear error.
8. Every LLM call writes a row to `llm_calls` with `status`, `provider`, `model`, `tokens_input`, `tokens_output`, `latency_ms`, `error_class` (if applicable), and a redacted error message.
9. Resume of an interrupted run uses the original `llm_config_snapshot`, not the current project config. Test #16 in section 6.6 covers this.
10. UI run detail shows the snapshot (intended config) and the `llm_calls` table (actual calls).
11. Live logs include `provider=` and `model=` on every LLM-related line.
12. If `coder.(provider,model) == reviewer.(provider,model)`, pre-flight emits a warning. The run is still allowed to proceed. UI shows the warning banner.
13. Provider error messages are redacted for secrets before being logged or persisted.
14. Existing 1-chunk and 2-chunk smoke tests in `docs/phase2b-smoke-tests.md` pass unchanged.
15. No auto-routing, no fallback chain, no cost dashboard, no encrypted secret storage, no new database engine, no worker queue introduced.
16. First-install with only `GEMINI_API_KEY` set works: defaults fill in Gemini for all roles; the reviewer==coder banner appears.

---

## 17. Adversarial Closing Notes

A few things will go wrong even with LLM-M1 built exactly as designed:

- **The capability table will go stale within weeks.** Providers ship new models constantly. Build the table such that adding a row is one PR and one merge; this is largely true if you keep it in code rather than DB.
- **JSON-mode reliability varies wildly across providers.** Anthropic's plain JSON adherence is weaker than its tool-use adherence; OpenAI's `json_schema` is strict but you have to feed it the schema. Even with M1's "use JSON mode where available," expect more correction-prompt retries when the active model is Anthropic-plain-JSON than when it is Gemini-JSON. The fix is to use Anthropic tool use in the adapter; the failure mode if you don't is silent quality decay.
- **The 60-second blanket sleep is replaced by per-provider backoff, but Gemini free tier is still 20 RPD.** No amount of retry policy makes that work for a real workload. Document loudly that running M1 with Gemini free tier on every role will throttle in minutes.
- **The "reviewer != coder" rule is soft.** Users with one API key will run with reviewer==coder. The banner is the only mitigation. Expect to harden this in LLM-M2 once cost dashboards make the trade-off concrete.
- **Snapshot immutability is what saves resume from chaos.** Do not let any future feature "fix" snapshots by re-reading project config. That helpful-looking patch will be the one that ruins a debugging session months from now.
- **Adding OpenAI later is more work than it looks.** Their Responses API differs from Chat Completions. Pick one in LLM-M1.x and put it behind the same `Provider` interface, accepting that the adapter will be thicker than Gemini's or Anthropic's. The abstraction was designed for that.

If any of the above feels uncomfortable, the answer is not to expand LLM-M1 — it is to ship LLM-M1 fast, see which adversarial case actually bites, and let that drive LLM-M2 priorities.

---

## Recommended LLM-M1 Implementation Order

Codex should implement in this order, with a working commit at each step:

1. **Migration `0002_llm_config.sql`** — `project_llm_config`, `llm_calls`, plus columns on `pipeline_runs` (`llm_config_snapshot`, `preflight_report`). Indexes.
2. **Capability table** — `backend/llm/capabilities.py` with Gemini and Anthropic entries.
3. **Pydantic models** — `RoleConfig`, `ProjectLLMConfig`, `LLMConfigSnapshot`, validators that check the capability table. Unit tests.
4. **Error taxonomy** — `backend/llm/errors.py` with all `LLMError` subclasses.
5. **Provider interface** — `backend/llm/provider.py` (`LLMRequest`, `LLMResponse`, `Provider` ABC, `RetryPolicy`).
6. **Gemini adapter** — `backend/llm/providers/gemini.py`. Translates current direct-SDK code into the adapter. Honor `Retry-After`. Normalize errors. Unit tests with mocked SDK.
7. **Anthropic adapter** — `backend/llm/providers/anthropic.py`. Uses tool-use for structured JSON. Unit tests with mocked SDK.
8. **Provider registry** — `backend/llm/registry.py`. SDK-leak lint test from section 4.8.
9. **Pre-flight check** — function that, given a `LLMConfigSnapshot`, validates each role and returns a structured report.
10. **API routes for config + preflight** — endpoints in section 6.2.
11. **Run-creation hook** — chunked-run creation calls preflight, writes snapshot. Refuses to start if preflight fails.
12. **Audit recorder** — `record_llm_call(run_id, chunk_number, role, role_cfg, response, status, error)`.
13. **Refactor `triage.py`** — uses provider abstraction. Existing smoke test must still pass with Gemini.
14. **Refactor `planner.py`** — uses provider abstraction; correction-retry stays on same provider.
15. **Refactor `coder.py`** — same.
16. **Add `reviewer.py`** if absent. Uses provider abstraction.
17. **Adjust orchestrator** — pass `project_id` to stages so they can resolve the run snapshot. Resume path reads snapshot.
18. **Live-log shape change** — add `provider=` and `model=` to log lines. Update event publisher.
19. **Frontend: Project Settings → LLM Config tab** — per-role dropdowns, advanced fields, test button.
20. **Frontend: Run detail → LLM tab and LLM Calls tab** — snapshot view + audit table.
21. **Frontend: warning banner** when reviewer == coder.
22. **Full smoke test** — 1-chunk and 2-chunk smoke runs from `docs/phase2b-smoke-tests.md`:
    - First with all-Gemini config (parity with existing behavior).
    - Then with Gemini coder + Anthropic reviewer (the headline multi-LLM case).
23. **Tag `phase-llm-m1`.** Do not merge to main until all acceptance criteria in section 16 pass.

Steps 1–8 build the abstraction without changing pipeline behavior; steps 13–17 are the actual cutover. If anything goes wrong during cutover, steps 13–17 are revertible without losing the new abstraction.