# Multi-Provider / Provider Diagnostics Smoke & Closeout Checklist

Manual smoke validation and closeout record for the **local-first multi-provider /
provider diagnostics** phase (#33). This is a checklist, not an automated suite:
the frontend has no test framework yet, so UI steps are manual and complement the
focused backend tests that already cover resolution, the diagnostics endpoint, the
FakeProvider guard, coder provenance, and reviewer independence.

Related docs:

- Design / as-built audit: [`docs/design/multi-provider-modes.md`](../design/multi-provider-modes.md)
- Advisory reviewer smoke: [`docs/testing/advisory-reviewer-smoke.md`](./advisory-reviewer-smoke.md)
- Demo smoke: [`docs/testing/demo-smoke-checklist.md`](./demo-smoke-checklist.md)

## Completed Work (this phase)

- #33A — `docs/design/multi-provider-modes.md` as-built audit — merged
- #33B — metadata-only coder LLM provenance persistence (`llm_call_provenance`) — merged
- #33C — display-only reviewer independence disclosure — merged
- #33D — FakeProvider production guard (runtime resolution) — merged
- #33E — read-only `GET /llm/diagnostics` endpoint — merged
- #33F — read-only Provider Diagnostics frontend panel — merged
- #33G — this smoke / closeout checklist (docs only)

## 1. Purpose

This smoke validates the local-first multi-provider foundation end to end:

- **env-based role provider config** — `DEFAULT_LLM_*` and per-role `<ROLE>_LLM_*`
  resolved by `resolve_role_config` (precedence unchanged).
- **diagnostics API** — `GET /llm/diagnostics` reports per-role
  provider/model/status/message, read-only.
- **frontend diagnostics panel** — the read-only Provider Diagnostics panel in the
  Run Detail "Environment" section.
- **FakeProvider guard** — `fake` is refused for real runs unless under pytest or
  `PIPEWRIGHT_ALLOW_FAKE_PROVIDER=true`.
- **coder provenance** — one metadata-only row per effective coder output.
- **reviewer independence disclosure** — self-review vs independent vs
  unknown, derived from persisted provenance.

It is a foundation, not auto mode: provider selection stays in `.env`, and nothing
here selects, falls back, or edits providers.

## 2. Safety guarantees preserved

This phase changes none of the existing safety invariants. Confirm all still hold:

- No auto-commit.
- No final-approval bypass.
- No auto-merge.
- No provider fallback (a failed provider fails closed; nothing retries on another).
- No auto mode (no complexity/cost/risk-based selection).
- No editable provider settings in the UI.
- No `.env` writing from the UI.
- Reviewer remains advisory / display-only.
- Diagnostics are strictly read-only.
- Provider diagnostics never call LLM completions.
- PR checks remain display-only and gate nothing.
- SQLite remains the local/open-source default; no DB-backed provider config.

## 3. Prerequisites

- Backend running (default `http://localhost:8001`).
- Frontend running (`cd frontend && npm.cmd run dev`).
- A valid local `.env` with provider config (real keys only when intentional):

  ```env
  DEFAULT_LLM_PROVIDER=gemini
  DEFAULT_LLM_MODEL=gemini-2.5-flash-lite
  GEMINI_API_KEY=...        # only the key(s) for the provider(s) you actually use
  ```

- A test project configured (valid `repo_path` + `test_command`).
- Use **real API keys only intentionally** — they are billed.
- Do **not** use the `fake` provider except for explicit local testing
  (flows C and D below).

> Windows note: if PowerShell blocks `npm.ps1`, use `npm.cmd`. Backend tests use
> `python -m pytest`.

## 4. Backend validation commands

Run the focused unit tests for the implemented slices (the repo convention is
`-m unit`; some `@pytest.mark.api` tests need live keys + a target repo and are
deselected):

```powershell
python -m pytest backend/tests/test_llm_diagnostics.py -q -m unit
python -m pytest backend/tests/test_llm_fake_provider_guard.py -q -m unit
python -m pytest backend/tests/test_llm_call_provenance_store.py -q -m unit
python -m pytest backend/tests/test_coder_provenance.py -q -m unit
python -m pytest backend/tests/test_reviewer_independence.py -q -m unit
python -m pytest backend/tests/test_reviewer_read_model.py -q -m unit
```

Or all together:

```powershell
python -m pytest backend/tests/test_llm_diagnostics.py `
  backend/tests/test_llm_fake_provider_guard.py `
  backend/tests/test_llm_call_provenance_store.py `
  backend/tests/test_coder_provenance.py `
  backend/tests/test_reviewer_independence.py `
  backend/tests/test_reviewer_read_model.py `
  backend/tests/test_chunk_routes.py -q -m unit
```

Lint / hygiene (the repo enforces `ruff check`, **not** `ruff format` — the codebase
is intentionally not ruff-format-clean, so do not run `ruff format`):

```powershell
python -m ruff check backend/llm backend/routes/llm.py backend/pipeline/llm_call_provenance_store.py backend/pipeline/chunk_review_read_model.py
git diff --check
```

Expected: all listed unit tests pass; `ruff check` reports no issues on these
files; `git diff --check` is clean.

## 5. Frontend validation commands

```powershell
cd frontend
npm.cmd run build
npx.cmd eslint src/components/ProviderDiagnosticsPanel.tsx src/api/client.ts src/pages/RunDetailPage.tsx
```

Expected: `npm.cmd run build` (tsc typecheck + vite) succeeds; eslint on the touched
files is clean. A pre-existing "chunk larger than 500 kB" build note is unrelated
and not a failure. There is no frontend test framework (no `*.test.tsx`), so the
build + eslint are the bar; if a repo-wide `eslint .` surfaces pre-existing
unrelated issues elsewhere, the **touched** files must still be clean.

## 6. Manual smoke flow A — provider diagnostics happy path

- [ ] Set `DEFAULT_LLM_PROVIDER` / `DEFAULT_LLM_MODEL` to a real, configured
      provider/model and set that provider's key.
- [ ] Start backend and frontend.
- [ ] Open a Run Detail page.
- [ ] Confirm the **Provider Diagnostics** panel appears in the **Environment**
      section (below the Timeline).
- [ ] Confirm each role shows role name, provider, model, status, and a message.
- [ ] Confirm available roles show **Available** (green badge) with
      "Provider and model configuration validated."
- [ ] Confirm the subtitle reads
      "Read-only — provider selection is currently configured through .env."
- [ ] Click **Refresh** — confirm it re-fetches (button shows "Refreshing…").
- [ ] Confirm the panel does **not** auto-poll (it fetches on mount and on manual
      refresh only) and that nothing in a run/chunk/approval state changes.
- [ ] Sanity-check the raw endpoint: `GET http://localhost:8001/llm/diagnostics`
      returns `{ "roles": [ ... ] }` with one entry per role.

## 7. Manual smoke flow B — missing key / unavailable provider

- [ ] Configure a provider whose API key is missing (or temporarily remove the key
      for the configured provider).
- [ ] Call `GET /llm/diagnostics` or click **Refresh** in the panel.
- [ ] Confirm the affected role status is **Unavailable**.
- [ ] Confirm the message is sanitized and human-readable (e.g.
      "GEMINI_API_KEY is not configured") — describing the gap, not echoing it.
- [ ] Confirm **no secret values** and **no environment dump** appear anywhere in
      the response or panel.
- [ ] Restore `.env` and refresh — confirm the role returns to Available.

## 8. Manual smoke flow C — FakeProvider blocked

- [ ] Set one role to fake, e.g. `CODER_LLM_PROVIDER=fake` (and
      `CODER_LLM_MODEL=fake-model`).
- [ ] Do **not** set `PIPEWRIGHT_ALLOW_FAKE_PROVIDER`.
- [ ] Call `GET /llm/diagnostics` (or Refresh).
- [ ] Confirm that role's status is **Blocked** and `fake_blocked` is `true`.
- [ ] Confirm the message explains that
      `PIPEWRIGHT_ALLOW_FAKE_PROVIDER=true` is required for intentional local
      testing.
- [ ] (Optional) Start a real run for that role and confirm it **fails closed**
      with a clear provider configuration error before any fake completion is used
      (the #33D guard fires in `get_provider_for_role`).
- [ ] Restore `.env`.

## 9. Manual smoke flow D — FakeProvider explicitly allowed (local testing only)

- [ ] Set `PIPEWRIGHT_ALLOW_FAKE_PROVIDER=true`.
- [ ] Configure the fake provider intentionally (e.g. `CODER_LLM_PROVIDER=fake`,
      `CODER_LLM_MODEL=fake-model`).
- [ ] Call `GET /llm/diagnostics` (or Refresh).
- [ ] Confirm that role is no longer **Blocked** (it validates as Available;
      `fake_blocked` is `false`).
- [ ] Note: this is **only** for intentional local/test use — never for real work.
- [ ] Unset `PIPEWRIGHT_ALLOW_FAKE_PROVIDER` and restore `.env` after the test.

## 10. Manual smoke flow E — coder provenance

- [ ] With a real provider configured, run a small, safe feature through chunked
      execution so the coder stage runs.
- [ ] Inspect the local SQLite DB (`backend/db/pipewright.db`):

  ```sql
  SELECT run_id, chunk_number, role, provider, model,
         selection_source, finish_reason, input_tokens, output_tokens, created_at
  FROM llm_call_provenance
  WHERE run_id = '<your-run-id>' AND role = 'coder';
  ```

- [ ] Confirm exactly **one** coder row per (run, chunk) effective output (a
      correction/retry must not multiply rows).
- [ ] Confirm `provider` / `model` / `role` / token fields are populated when the
      provider returns them (tokens/finish_reason may be null — never invented).
- [ ] Confirm `selection_source` is `NULL` (reserved; not derived in this phase).
- [ ] Confirm the table holds **no** prompts, responses, diffs, file contents, or
      API keys — only metadata columns.
- [ ] Best-effort behavior is covered by
      `backend/tests/test_coder_provenance.py::test_provenance_failure_does_not_fail_coder`
      (a forced provenance write failure does not fail the coder) — rely on that
      test rather than manual DB fault injection.

## 11. Manual smoke flow F — reviewer independence disclosure

- [ ] **Self-review case:** configure coder and reviewer to resolve to the **same**
      provider/model (e.g. both default to the same `DEFAULT_LLM_*`), run a chunk
      through review, and open the Advisory AI Review panel.
- [ ] Confirm it shows the **self-review / "Not independent"** caution:
      "Reviewer used the same provider/model as the coder; treat this review as a
      self-check, not an independent review."
- [ ] **Independent case:** set `REVIEWER_LLM_PROVIDER` (or model) to a different
      provider/model from the coder, run another chunk, and confirm the
      **Independent reviewer** state.
- [ ] **Unknown case:** for a review where coder provenance is missing (e.g. a chunk
      reviewed without a persisted coder row), confirm the state is
      **Independence unverified / unknown** — never a false "independent".
- [ ] Confirm independence is derived from **persisted** provider/model for that
      run/chunk, not current `.env` (changing `.env` after the run must not rewrite
      a historical review's independence).
- [ ] Confirm the disclosure is advisory only and blocks **no** approval.

## 12. Known limitations / intentionally deferred

- No **auto mode** (no complexity/cost/risk-based provider selection).
- No **provider fallback** (failures fail closed; nothing retries elsewhere).
- No **UI editing** of provider settings (read-only display only).
- No **DB-backed provider config** — `.env` remains the source of truth.
- `selection_source` is **reserved / NULL** (not derived this phase).
- Diagnostics report **configured** availability (key presence + model support),
  **not** live provider health checks — no network/LLM call is made.
- **Dormant roles** (e.g. `architect`) appear in diagnostics even though they may
  have no current call site.
- No **hosted/team** provider management; this is the local-first path only.

## 13. Closeout criteria

- [ ] Backend unit tests for the listed slices pass.
- [ ] Frontend build passes; touched frontend files are eslint-clean.
- [ ] `GET /llm/diagnostics` works and returns one entry per role.
- [ ] Provider Diagnostics panel displays in the Run Detail Environment section.
- [ ] FakeProvider **blocked** (flow C) and **allowed** (flow D) behavior verified.
- [ ] A metadata-only coder provenance row is verified (flow E).
- [ ] Reviewer independence states (self-review / independent / unknown) verified
      (flow F).
- [ ] No secrets or content (prompts/responses/diffs/file contents/keys) are
      persisted or surfaced.
- [ ] No runtime safety invariant is broken (section 2 all hold).
- [ ] `git diff --check` clean; `ruff check` clean on listed files.

## 14. Final phase result

**#33 Multi-provider / provider diagnostics is complete enough for the local-first
phase** once this doc is merged and the smoke above is run. The foundation —
env-based role resolution, read-only diagnostics (API + panel), the FakeProvider
guard, coder provenance, and reviewer-independence disclosure — is in place with all
existing safety invariants preserved.

**Auto mode remains deferred** and should stay deferred unless explicitly
prioritized later, per the reserved contract in
[`docs/design/multi-provider-modes.md`](../design/multi-provider-modes.md).
