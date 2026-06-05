# Multi-Provider Modes — As-Built Audit & Forward Contract

Status: **Audit (docs-only).** This document changes no code, schema, or runtime
behavior. It records what the codebase **already does** for multi-provider LLM
routing, names the **actual** remaining gaps, and reserves a forward design that
preserves every existing safety invariant.

Architecture name used throughout:
**role-based provider resolution with recorded provenance and disclosure.**

> **This is not a greenfield provider-routing design.** Basic per-role
> multi-provider routing is already implemented and wired into every pipeline
> stage. Treating this as a from-scratch feature would re-skin shipped code and
> call it progress. The genuine gaps are narrow and listed in section 6.

---

## 0. How to read the citations

Every "as-built" claim below cites `path:line`. Citations were verified against
the working tree at audit time. If any citation drifts (line moves, function
renamed), re-verify against the current tree before relying on the claim — the
claim is only as good as the cited code.

---

## 1. Current provider registry

**Registered providers** (`backend/llm/registry.py:34-47`, `default_registry()`):

| Provider | Registration | Adapter |
|---|---|---|
| `gemini` | `registry.py:42` | `backend/llm/providers/gemini.py` |
| `anthropic` | `registry.py:43` | `backend/llm/providers/anthropic.py` |
| `openai` | `registry.py:44` | `backend/llm/providers/openai.py` |
| `deepseek` | `registry.py:45` | `backend/llm/providers/deepseek.py` |
| `fake` | `registry.py:46` | `backend/llm/providers/fake.py` |

- **The fake provider IS registered in the default production registry**
  (`registry.py:46`). `FakeProvider` (`backend/llm/providers/fake.py:8-28`)
  supports only the model `fake-model` (`fake.py:9`) and returns a deterministic
  canned string (`fake.py:18-28`). There is currently **no guard** preventing a
  real run from resolving to `fake` if configured to do so. See section 6 and section 12.
- Registry is a name→instance map with duplicate-registration protection
  (`ProviderRegistry.register`, `registry.py:13-17`) and a fail-closed lookup:
  unknown provider names raise `UnsupportedProviderError`
  (`registry.py:19-28`).
- `ProviderRegistry.list()` returns the sorted registered names
  (`registry.py:30-31`).

Note (not a gap, an observation): `default_registry()` is constructed fresh on
each resolution call (see section 3). It is stateless today, so this is harmless; it is
recorded here only so a future provider with client/connection state does not
silently regress latency or correctness.

---

## 2. Current role-based resolver

**Role enum** (`backend/llm/role_config.py:16-22`):
`TRIAGE`, `PLANNER`, `CODER`, `REVIEWER`, `SUMMARY`, `ARCHITECT`.

- `ARCHITECT` is **defined but has no call site** (see section 3). There is **no
  `MEMORY` role** in the enum today.

**Resolver** (`resolve_role_config`, `backend/llm/role_config.py:41-73`)
returns a frozen `RoleConfig(provider, model)` (`role_config.py:25-28`).
Precedence, highest to lowest:

1. **Call-site override** — `overrides[{role}_provider]` / `{role}_model`
   (`role_config.py:48-49`).
2. **Role-specific env/setting** — `{ROLE}_LLM_PROVIDER` / `{ROLE}_LLM_MODEL`
   (`role_config.py:51-60`, via `_setting_or_env`, `role_config.py:31-38`).
3. **Default env/setting** — `DEFAULT_LLM_PROVIDER` / `DEFAULT_LLM_MODEL`
   (`role_config.py:62-68`).
4. **Hardcoded fallback** — `gemini` / `gemini-2.5-flash-lite`
   (`role_config.py:12-13`, applied at `role_config.py:70-72`).

**Simple and manual config already share one resolver path.** There is no
separate code path for "simple" vs "manual":

- *Simple* = only `DEFAULT_LLM_*` set (or nothing → hardcoded fallback). Every
  role resolves through steps 3–4.
- *Manual* = one or more `{ROLE}_LLM_*` set. Those roles resolve at step 2;
  unset roles fall through to steps 3–4.

Both are the **same function** (`resolve_role_config`) reading **different
inputs**. There is no backend mode flag and no branching on a mode.

Settings surface (`backend/config/keys.py:12-44`): provider keys are optional at
startup (`keys.py:13-20`); every role's `{ROLE}_LLM_PROVIDER` / `_MODEL` plus the
`DEFAULT_LLM_*` pair are declared (`keys.py:25-38`).

---

## 3. Current LLM call sites

All stages route through `complete_for_role(role, request)`
(`backend/llm/__init__.py:31-38`), which resolves via
`get_provider_for_role` (`__init__.py:15-28`) and overwrites the request model
with the resolved model (`__init__.py:37`).

| Stage | Role | Call site | Provenance handling |
|---|---|---|---|
| Coder | `CODER` | `backend/pipeline/coder.py:303-306` | **Discarded** — `_call_llm` returns `response.text` only (`coder.py:306`); `provider`/`model` are logged then dropped |
| Planner | `PLANNER` | `backend/pipeline/planner.py:127-130` | **Discarded** — returns `response.text` only (`planner.py:130`) |
| Triage | `TRIAGE` | `backend/pipeline/triage.py:158-164` | **Discarded** — returns `response.text` only (`triage.py:164`) |
| Intent | `TRIAGE` | `backend/pipeline/intent.py:547-555` | **Discarded**; notable for explicit fail-closed handling (see section 4) |
| Reviewer | `REVIEWER` | `backend/pipeline/reviewer.py:331-385` | **Persisted** — `response.provider` / `response.model` flow into the stored record (`reviewer.py:355-364`) |
| Summary / report analyzer | `SUMMARY` | `backend/pipeline/report_analyzer.py:549-554` | **Discarded** — returns `response.text` only (`report_analyzer.py:554`) |

- **Memory:** there is **no memory LLM call site** and no `MEMORY` role today.
- **Architect:** `Role.ARCHITECT` is defined (`role_config.py:22`) but **invoked
  nowhere** — it is resolvable but dormant.
- Token usage for every call is logged (provider, model, tokens, finish_reason,
  run_id) via `log_token_usage` (`backend/llm/__init__.py:41-63`). **Logs are
  not an audit trail** — they rotate and are not queryable per run.

**Conclusion:** provenance is persisted for exactly **one** role (reviewer).
Every execution/pre-approval role currently discards provider/model after the
call.

---

## 4. Current provider validation & error behavior

**Model-support validation:** `get_provider_for_role` rejects a resolved
provider/model mismatch with `UnsupportedModelError` before any network call
(`backend/llm/__init__.py:21-27`). Per-provider allowlists exist, e.g.
`GeminiProvider.supports_model` / `_SUPPORTED_MODELS`
(`backend/llm/providers/gemini.py:26-39`).

**Config / key validation:** each provider exposes `validate_config(model)`,
e.g. `gemini.py:41-55`, which raises `ProviderConfigurationError` when the key is
absent (`gemini.py:49-55`). `complete()` calls `validate_config` first
(`gemini.py:58`), so a missing key fails **before** the request is sent.
`validate_all_roles` (`backend/llm/__init__.py:66-71`) can validate every role's
resolved provider/model. Keys are optional at startup and required only at use
(`backend/config/keys.py:13-20`).

**Structured error taxonomy** (`backend/llm/errors.py`):
`LLMError` base (`errors.py:8-29`) carrying `provider`, `model`, and a
`retryable` flag; subclasses include `ProviderConfigurationError`,
`UnsupportedProviderError`, `UnsupportedModelError`, `ProviderExecutionError`,
`ProviderRateLimitError` (retryable, `errors.py:48-63`), `ProviderTimeoutError`
(retryable, `errors.py:66-79`), `ProviderAuthError`,
`ProviderContentFilteredError`, `ProviderInvalidResponseError`.

**Sanitization:** `LLMError.__init__` runs the message through
`sanitize_for_log` (`errors.py:20`, `backend/llm/sanitize.py`). Provider error
mapping sanitizes before constructing the typed error, e.g.
`_map_gemini_error` (`gemini.py:149-204`, sanitize at `gemini.py:150`).

**Fail-closed?** Yes, today:

- Unknown provider / unsupported model → raises, no call
  (`registry.py:19-28`, `__init__.py:21-27`).
- Missing key for a selected provider → `ProviderConfigurationError`, no silent
  substitution (`gemini.py:49-55`).
- Intent stage explicitly catches `ProviderConfigurationError` /
  `UnsupportedProviderError` and **defaults to the safe `plan_only` path**
  rather than guessing (`intent.py:548-555`).
- Reviewer failures degrade to an `unavailable` advisory record and **never**
  affect the chunk outcome (`reviewer.py:370-385`).

**Startup diagnostics** (`backend/runtime/startup_diagnostics.py`): log-only,
never logs secret values (`startup_diagnostics.py:8-12`).
`check_provider_keys` (`startup_diagnostics.py:165-191`) warns only for providers
**actually selected by role config** (`_selected_providers`,
`startup_diagnostics.py:132-144`) that lack a key — reporting presence only,
never the value.

---

## 5. Existing provenance (reviewer) — the proven pattern

**Reviewer provider/model provenance is already persisted.** The reviewer
resolves the role config (`reviewer.py:336`), runs the advisory review, and
stores `response.provider` / `response.model` on the review record
(`reviewer.py:355-364`).

**Storage pattern** — a dedicated, isolated, additive table
(`backend/pipeline/chunk_review_store.py`):

- `create_review` inserts into the **`chunk_reviews`** table, including
  `provider` and `model` columns (`chunk_review_store.py:63-126`; columns at
  `:88` / `:96`, bound at `:113-114`).
- The store's own docstring states the design rule we should reuse: review
  evidence lives in a dedicated table **separate from checkpoints (the
  resume/safety substrate) and from `chunks.completion_summary`**, so advisory
  LLM evidence never couples into the resume path
  (`chunk_review_store.py:5-15`). It performs only boring CRUD — no LLM, git,
  route, or repo calls (`chunk_review_store.py:10-12`).
- The read model exposes `provider` / `model` for disclosure
  (`backend/models/chunk.py:192-193`).

**Execution-path roles do NOT persist provenance.** Coder, planner, triage,
intent, and summary discard provider/model after the call (see section 3). Their
provenance exists only in rotating logs.

---

## 6. Actual gaps

Basic multi-provider routing is **not** a gap — it is implemented and wired
(sections 1–4). The real gaps are:

1. **Execution-path provenance.** The coder produced the diff that gets
   committed, yet its provider/model is not persisted (`coder.py:303-306`).
   Same for planner/triage/summary. Only the reviewer persists (section 5).
2. **UI / read-model disclosure.** Outside the reviewer read model, no surface
   shows which provider/model produced a given output.
3. **Reviewer-independence warning.** Nothing detects or surfaces when the
   reviewer resolves to the *same* provider/model as the coder (a real risk with
   the shared hardcoded default; see section 8, section 11).
4. **Fake-provider production guard.** `fake` is selectable in a real run with no
   guard (section 1, section 12).
5. **Provider availability diagnostics surfacing.** Key/availability checks exist
   but are **log-only** at startup (`startup_diagnostics.py`); there is no
   per-run, role-scoped pre-flight or operator-visible surface (section 13).
6. **Auto-mode contract.** No reserved provenance fields or disclosure/no-drift
   guarantees for a future auto policy (section 14).

---

## 7. Architecture naming

**role-based provider resolution with recorded provenance and disclosure.**

- **Resolver** — chooses provider/model from override → role env → default env →
  hardcoded config. Pure, env-driven. Already exists
  (`resolve_role_config`, `role_config.py:41-73`).
- **Provenance** — records which provider/model was *actually* used for a given
  output. Exists for the reviewer (section 5); missing on the execution path.
- **Disclosure** — shows provider/model and trust facts to the operator. Exists
  in the reviewer read model (`chunk.py:192-193`); otherwise missing.

**Simple / manual / auto are UX labels only:**

- **simple** = `DEFAULT_LLM_*` only.
- **manual** = role-specific `{ROLE}_LLM_*` overrides.
- **auto** = a future policy input into the **same** resolver.

**Explicit constraints:**

- **No backend `mode` enum.**
- **No separate simple/manual/auto backend code paths.** All three are
  configurations (or, for auto, an additional input) of the one resolver. Any
  PR introducing a backend mode branch is out of scope and contradicts the
  as-built design.

---

## 8. Safety risks specific to multi-provider routing

1. **Reviewer == coder → false independence.** If reviewer and coder resolve to
   the same provider+model (the default case: both fall back to
   `gemini-2.5-flash-lite`, `role_config.py:12-13`), the "advisory review" is the
   model grading its own output, with no operator signal. Live today.
2. **Fake provider in a real run.** `fake` is registered in production
   (`registry.py:46`) and selectable via config. A coder set to `fake` would emit
   a canned string (`fake.py:18-28`) that still flows downstream. Live today.
3. **Structured-output parity differs by provider.** Coder/planner/triage/summary
   request `response_format="json_object"` (`base.py:22`; e.g.
   `coder.py:299`, `planner.py:123`, `triage.py:154`,
   `report_analyzer.py:545`). Providers honor JSON mode unevenly; repointing a
   structured role at a weaker provider can raise parse-failure rates. Parse
   failures must continue to fail closed (no patch/commit).
4. **Silent provider fallback would break trust/provenance.** Auto-retrying a
   failed call on a different provider can change the model behind an approved
   plan or a committed diff, and can re-issue a mutating call after partial side
   effects. See section 10.
5. **Provider drift between plan approval and execution.** If the provider/model
   that produced an approved plan differs from the one that executes it, the
   operator approved something other than what ran. See section 10 and section 14.
6. **Missing API keys must fail closed.** Today they do (section 4). The invariant: a
   missing key for a selected provider raises; it must **never** silently route
   to a different provider.

---

## 9. Provenance design — specification only (NOT implemented here)

Future additive table, proposed name **`llm_call_provenance`**. Modeled on the
isolation discipline already proven by `chunk_reviews`
(`chunk_review_store.py:5-15`).

Suggested fields:

| Field | Notes |
|---|---|
| `id` | row id |
| `run_id` | owning run |
| `chunk_number` | nullable (pre-chunk roles like triage may have none) |
| `role` | `triage` / `planner` / `coder` / `reviewer` / `summary` / … |
| `provider` | resolved, effective provider name |
| `model` | resolved, effective model name |
| `selection_source` | how it was chosen (see section 14): `override` / `env_role` / `env_default` / `hardcoded_default` / `auto_policy` |
| `finish_reason` | from `LLMResponse.finish_reason` (`base.py:32`) |
| `input_tokens` | from `LLMResponse` (`base.py:30`) |
| `output_tokens` | from `LLMResponse` (`base.py:31`) |
| `created_at` | ISO timestamp |

Storage rules:

- **Store provenance separately from the checkpoint/resume safety substrate.**
  It must never be read by the resume path (same rule the reviewer store
  follows, `chunk_review_store.py:5-15`).
- **Write best-effort:** a provenance write failure must **never** abort a run,
  mirroring the reviewer's `unavailable` degradation (`reviewer.py:370-385`).
- Granularity: per **(run, chunk, role) output** — not per raw LLM call (a
  retry/correction pass should not multiply audit rows; keep the effective
  output's provenance, optionally a `retry_count`).

**Never store** (privacy/secrets):

- API keys or tokens — never, in any form.
- Prompts or system prompts (they contain user request text and code).
- Full LLM responses or generated diffs (the diff already lives in git /
  checkpoints).
- Unsanitized provider errors (only `sanitize_for_log`-scrubbed, typed errors —
  reuse `errors.py` / `sanitize.py`, do not bypass them).
- File contents or repo paths beyond what existing records already hold.

**Safe to store:** provider name, model name, `selection_source`,
`finish_reason`, token counts (integers), timestamps.

---

## 10. Fallback stance

**No automatic provider fallback for mutating or trust-bearing roles.**

- The **coder must never silently retry on a different provider/model.**
- Today there is **no fallback at all** — `complete_for_role` raises on failure
  and the pipeline fails closed (section 4). This is the correct default and should be
  preserved.

Phrase the stance as a deliberate invariant, not a missing feature: fallback is
**intentionally disabled** for mutating roles to preserve plan→execute
provenance integrity. Any future fallback must be:

- **opt-in** (disabled by default),
- **operator-visible** (surfaced as a trust fact, never silent),
- **persisted** (recorded in `selection_source` / a reason field),
- **limited to read-only pre-approval roles** (e.g. triage, summary) on
  explicitly `retryable` errors only (`errors.py:48-79`),

and **never** applied to the coder or to anything past final approval unless
separately and explicitly approved.

---

## 11. Reviewer independence — future design

- **First display version (cheapest):** warn when the *currently resolved*
  reviewer provider/model equals the *currently resolved* coder provider/model
  (`resolve_role_config(Role.REVIEWER)` vs `resolve_role_config(Role.CODER)`,
  `role_config.py:41-73`). No new persistence required.
- **Once coder provenance is persisted (section 9):** independence must compare the
  **actually persisted** coder provider/model against the **actually persisted**
  reviewer provider/model **for that run/chunk** — not the currently resolved
  config, which can drift after the fact.
- The warning must be **display-only / advisory** and must **not block**
  local-first users who legitimately run a single provider (e.g. one API key).
  Blocking would violate the local-first promise. Consistent with "reviewer is
  advisory" and the display-only Operator Attention Panel.

Suggested wording: *"Reviewer used the same provider+model as the coder; treat
this as a self-check, not an independent review."*

---

## 12. FakeProvider guard — future rule only (NOT implemented here)

Future rule:

- `fake` should be **default-denied** in normal resolver use — resolving a real
  role to `fake` should raise a clear `ProviderConfigurationError` (fail-closed).
- **Allow** only when **either**: a pytest/test environment is detected (e.g.
  `PYTEST_CURRENT_TEST` present), **or** `PIPEWRIGHT_ALLOW_FAKE_PROVIDER=true` is
  explicitly set.
- **Registry injection for tests may remain supported** — tests that construct a
  registry with a fake directly bypass the resolver gate by design.
- Do **not** key the rule on "local dev mode": Pipewright is local-first, so that
  condition is always true and would block nothing.

This keeps the entire existing test suite green (fake stays registered and
test-detectable) while making `CODER_LLM_PROVIDER=fake` impossible to ship
against real code by accident. **Not implemented in this PR.**

---

## 13. Pre-flight validation — future direction only

- Validate **only the roles a given run will actually use** — never all six
  unconditionally (that would resurrect the over-validation that selective key
  validation already fixed).
- Classify each role as **required vs advisory** for the run type:
  - *Required* (e.g. triage, planner, coder for a chunked implementation run): a
    missing provider/key **fails before tokens are spent**.
  - *Advisory* (e.g. reviewer, summary): a missing provider/key **warns and
    degrades gracefully** — which the current code already supports (reviewer →
    `unavailable`, `reviewer.py:370-385`; summary is non-blocking).
- Build on the existing primitives: `validate_all_roles`
  (`__init__.py:66-71`), provider `validate_config` (`gemini.py:41-55`), and the
  role-scoped key check in `startup_diagnostics.py:165-191`.

---

## 14. Auto mode — reserved contract only (no heuristics)

Do **not** design selection heuristics now. Reserve only:

- `selection_source = auto_policy` as a reserved value of the section 9 field.
- An optional, nullable future `selection_reason` field (why auto chose what it
  chose) — reserved now so auto mode does not later force a migration.
- **Auto choices must be disclosed** per run (what was chosen, and ideally why).
- **Auto must not silently change provider/model between an approved plan and its
  execution** (the section 8.5 drift invariant). The provider/model resolved at plan
  approval must be the one recorded and used at execution, or the change must be
  surfaced and re-approved.

---

## 15. Safety invariants preserved

This design is additive and display-oriented; it preserves all existing
invariants:

- No auto-commit.
- No final approval bypass.
- No auto-merge.
- No automatic scope expansion.
- Reviewer remains advisory only.
- Operator Attention Panel remains display-only.
- Backend routes remain the source of truth and revalidate before mutation.
- PR checks remain display-only and do not gate approval/push/merge.
- Normal Run Detail loads do not call GitHub checks automatically.
- SQLite remains the local/open-source default; PostgreSQL/pgvector remain the
  future hosted/team path.

Provenance is **recorded and disclosed**, never used to gate, retry, commit,
merge, or expand scope.

---

## 16. Recommended next slices (each small, additive, reviewable)

1. **Coder / execution-path LLM provenance persistence** — add the
   `llm_call_provenance` table (section 9) and record coder (then planner/triage/
   summary) provider/model, reusing the reviewer's isolation pattern. Highest
   value: it ties the committed diff to the model that produced it.
2. **Reviewer-independence disclosure** — derive and surface the warning (section 11),
   preferring **actually persisted** coder vs reviewer provenance once slice 1
   lands; display-only, no gating.
3. **Fake-provider production guard** — implement the section 12 rule.
4. **Provider availability diagnostics surfacing** — per-run, role-scoped
   required/advisory pre-flight (section 13), building on existing checks.
5. **Provider/model visibility in read models / UI** — disclosure surface beyond
   the reviewer (section 6.2).
6. **Env docs polish** — document the real `{ROLE}_LLM_*` / `DEFAULT_LLM_*`
   variables (`keys.py:25-38`); no invented names.
7. **Auto-mode design later** — only after manual provenance + disclosure are
   stable; honor the section 14 reserved contract.

---

## Audit closeout checklist

- [x] Every as-built claim cites real file paths and line numbers.
- [x] The doc states this is **not** greenfield provider routing (section 6 banner, section 0).
- [x] The diff adds **only** `docs/design/multi-provider-modes.md`.
- [x] No backend, frontend, schema, package, or test files changed.
- [x] No runtime behavior changed.
- [x] Safety invariants restated and mapped (section 15).
- [x] `selection_source` values and the auto-mode contract reserved in writing
  (section 9, section 14); no backend `mode` enum proposed (section 7).
