# Memory Injection Discipline Smoke & Closeout Checklist (M3F5)

Manual smoke validation and closeout record for the **Memory M3F — memory
injection discipline** phase. This is a checklist, not an automated suite: it
complements the focused backend tests that already cover the byte-identical
prompt block, the free structured exclusions (M3F2a), and the advisory
repo-reality warnings (M3F3).

This document is **docs-only**. It adds no code, schema, route, test, prompt,
injection, mutation, or runtime behavior.

Related docs:

- Design / as-built audit: [`docs/design/memory-injection-discipline.md`](../design/memory-injection-discipline.md)
  (§2 as-built injection path, §14 M3F2a free exclusions, §15 M3F3 reality warnings)
- M3 trust lifecycle design: [`docs/design/memory-m3-trust-lifecycle.md`](../design/memory-m3-trust-lifecycle.md)
- Provenance smoke (M3C): [`docs/testing/memory-provenance-smoke.md`](./memory-provenance-smoke.md)
- Lifecycle smoke (M3D): [`docs/testing/memory-lifecycle-smoke.md`](./memory-lifecycle-smoke.md)
- Trust UI smoke (M3E): [`docs/testing/memory-trust-ui-smoke.md`](./memory-trust-ui-smoke.md)
- Local setup: [README → Quick local setup](../../README.md#quick-local-setup)

## Phase slices (this phase)

- **M3F1** — `docs/design/memory-injection-discipline.md` design/audit — merged (docs only).
- **M3F2a** — read-only structured surfacing of *free* exclusions
  (`budget_dropped`, `category_not_allowed_for_role`); prompt block byte-identical;
  no injection behavior change — merged.
- **M3F2b** — status-excluded (stale/archived/historical) bounded summary —
  **deferred / optional** (requires widening the active-only query).
- **M3F3** — read-only advisory repo-reality mismatch warnings on the analysis
  read path; no auto-stale/archive/supersede/resolve; no prompt block change — merged.
- **M3F4** — gated, opt-in injection *tightening* — **deferred / optional**.
- **M3F5** — this smoke / closeout checklist (docs only).

## 1. Purpose

M3F made memory injection **more observable, not more autonomous.** It tightens
what a human can *see* about why a fact was or was not injected; it never lets
memory decide truth, never auto-resolves, and never weakens the human approval
gate.

- **M3F1** documented the injection discipline plan and re-derived the as-built
  injection path, naming the structured-exclusion gap.
- **M3F2a** surfaced the two *free* deterministic exclusion reasons that were
  already knowable from the rows the builder loads:
  - `budget_dropped` — an in-policy, active fact dropped because the role token
    budget filled.
  - `category_not_allowed_for_role` — an active fact whose category is outside
    the selected role's allow-list.
- **M3F3** surfaced advisory **repo-reality mismatch warnings** when an injected
  fact disagrees with an already-computed repo signal, on the analysis read path.
- **M3F4 injection tightening is intentionally deferred** — surface before
  suppress. M3F only warns; it never excludes a fact from the prompt.

The guiding principle for the whole phase: **System detects; human decides.
Source code / explicit user instruction / tests / safety rules beat memory.**

## 2. Safety guarantees preserved

M3F changes none of the existing safety invariants. Confirm all still hold:

- **Prompt block output unchanged** — `build_project_memory_block` is
  byte-identical for the same facts (only the `Generated` timestamp varies).
- **Included memory selection unchanged** — the same facts are injected as before
  M3F2a/M3F3 for the same data.
- `ROLE_CATEGORIES` unchanged.
- `ROLE_TOKEN_BUDGETS` unchanged.
- **No SQL widening for inactive facts in M3F2a** — the active-only
  (`status='active' AND is_stale=0`) query is unchanged; category exclusions are
  a post-fetch classification, not a new query.
- **No status-excluded full rows** — stale / archived / historical facts are
  neither loaded nor surfaced as M3F2a exclusions (deferred to M3F2b).
- No auto-stale.
- No auto-archive.
- No auto-supersede.
- No auto-resolve.
- No latest-wins (a newer fact is never automatically correct).
- No LLM truth decisions.
- No embeddings / vector / pgvector / semantic retrieval.
- **No repo / git scan inside `prompt_builder`** — the M3F3 repo signal is
  computed on the analysis read path (via the capped, traversal-safe
  `build_repo_fingerprint`), never during injection.
- **No reviewer / summary memory injection** — those policies exist but remain
  unwired; no provenance is recorded for them.
- Provenance and analysis are **read-only / advisory** — loading them never
  mutates memory or run state.
- The human remains responsible for memory lifecycle actions (mark stale,
  supersede, approve-and-supersede via the M3D/M3E surfaces).

## 3. Features covered

- **Prompt preview** can expose live `excluded_entries` alongside a
  byte-identical `memory_block` (computed live via
  `build_project_memory_block_detailed`; nothing persisted).
- **Runtime provenance** persists `excluded_entries` with `exclusion_reason` in
  the append-only `memory_injection_events` snapshot.
- **`RunMemoryProvenancePanel`** shows included and excluded summaries with reason
  labels, plus a read-only "Repo reality warnings" section.
- **`budget_dropped`** means in-policy active memory existed but did not fit the
  role token budget.
- **`category_not_allowed_for_role`** means active memory exists but that role
  does not use that memory category.
- **Safety highlight** — a budget-dropped `security` / `forbidden_paths` fact is
  surfaced loudly ("Safety memory was budget-dropped.").
- **Repo-reality warnings** are **advisory-only**, computed on the analysis read
  path, and surface only an unambiguous fact-vs-repo mismatch.
- **Unknown / ambiguous repo signals do not create scary warnings** —
  `reality_signal_available` is `false` and no warning is shown.

## 4. Manual smoke setup

Use the standard local setup — do not invent new commands:

- Start the backend and frontend per
  [README → Quick local setup](../../README.md#quick-local-setup).
  (Windows note: if PowerShell blocks `npm.ps1`, use `npm.cmd`.)
- Select a project that has **Project Memory enabled** and whose `repo_path`
  points at a small local checkout.
- Create **active memory facts across multiple categories** (via the existing
  memory routes / UI, as in the [M2 smoke checklist](./memory-m2-smoke-checklist.md)):
  - One category allowed for `coder` / `planner` (e.g. `stack`, `db`, `test`,
    `structure`).
  - One category **not** allowed for some role (e.g. an `architecture`, `style`,
    or `other` fact relative to `triage`, whose allow-list is narrower) so a
    `category_not_allowed_for_role` exclusion appears.
  - Optionally a high-priority `security` or `forbidden_paths` fact, then run a
    tight-budget role (e.g. `triage=400`) so it can be budget-dropped and the
    safety highlight appears.
  - Optionally a fact that **conflicts with repo DB reality** — e.g. memory says
    `Project uses MongoDB.` while the repo manifests point at PostgreSQL — to
    exercise the M3F3 reality warning.
- Run a **tiny Pipewright feature** (chunked execution) that reaches at least the
  planner and coder stages, to generate provenance. Note the `run_id`.

> Do not invent long setup commands if the README already covers them. The
> facts/run setup mirrors the [provenance smoke](./memory-provenance-smoke.md)
> and [lifecycle smoke](./memory-lifecycle-smoke.md).

## 5. Smoke checklist — prompt preview exclusions (M3F2a)

`GET /api/v1/projects/{project_id}/memory/prompt-preview` (optionally with a
`role`):

- [ ] The prompt preview `memory_block` still looks the same as before M3F2a (the
      `=== PROJECT MEMORY … ===` block; only the `Generated` timestamp differs).
- [ ] Included facts are unchanged — the same facts render in the block.
- [ ] `excluded_entries`, if present, are clearly **separate** from the prompt
      block text (structured detail, not rendered into the block).
- [ ] `category_not_allowed_for_role` appears for active facts whose category is
      outside the selected role's allow-list.
- [ ] `budget_dropped` appears when the role token budget forces an in-policy
      active fact out (use a tight-budget role like `triage`).
- [ ] **No** stale / archived / historical full rows appear as M3F2a exclusions
      (those are deferred to M3F2b).
- [ ] Calling preview performs **no** auto-resolution and **no** mutation
      (re-list the project's memory facts before/after; unchanged).

## 6. Smoke checklist — runtime provenance exclusions (M3F2a)

In Run Detail, open the `Memory Provenance` panel (lazy / read-only):

- [ ] The panel loads lazily — provenance endpoints are not called until
      `Load provenance` is selected (verify in devtools Network).
- [ ] Injection events show **included** entries as before
      (`as injected during this run`).
- [ ] **Excluded** entries show reason labels.
- [ ] `category_not_allowed_for_role` copy says the role does not use that memory
      category.
- [ ] `budget_dropped` copy says the memory was in-policy but did not fit the
      role token budget.
- [ ] A budget-dropped `security` / `forbidden_paths` fact shows the
      **"Safety memory was budget-dropped."** highlight.
- [ ] `entries_hash` remains based on **included** entries only (excluded entries
      do not change it).
- [ ] **No action buttons** appear in the provenance panel (no mark-stale,
      archive, supersede, approve, reject, or approve-and-supersede controls).

## 7. Smoke checklist — repo-reality warnings (M3F3)

`GET /api/v1/runs/{run_id}/memory-injections/analysis`, and the
`RunMemoryProvenancePanel` "Repo reality warnings" section:

- [ ] The analysis response includes `reality_signal_available`,
      `reality_warning_count`, and `reality_warnings`; the panel can show a
      read-only "Repo reality warnings" section.
- [ ] Warning copy is **advisory only** (`advisory_only: true`); the panel says
      "These are read-only warnings. The system did not change memory."
- [ ] Warning copy says the **repo signal suggests a mismatch / may be outdated**
      (`warning_type: "reality_mismatch_candidate"`).
- [ ] **No latest-wins** language ("newer wins", "current beats old").
- [ ] **No "system resolved"** language ("resolved", "fixed", "truth").
- [ ] **Unknown / missing / ambiguous** repo signals produce **no** scary
      warning (`reality_signal_available: false`, empty `reality_warnings`).
- [ ] The warning does **not** change memory status (the fact stays `active` /
      non-stale — contrast `/memory/verify-repo`, which is the only mutator).
- [ ] The warning does **not** remove the memory from the prompt.
- [ ] To act on a flagged fact, the human still uses the deliberate
      stale / supersede UI (M3E); the warning is informational only.

## 8. Smoke checklist — prompt / injection invariants

- [ ] `build_project_memory_block` output is **byte-identical** before/after
      M3F2a/M3F3 except the generated timestamp (lock with the golden test in
      `test_memory_prompt_builder.py` / `test_memory_free_exclusions.py`).
- [ ] Included memory entries are the **same** before/after M3F2a/M3F3 for the
      same data.
- [ ] `ROLE_CATEGORIES` and `ROLE_TOKEN_BUDGETS` are unchanged.
- [ ] Reviewer / summary remain **unwired** — no provenance recorded for them.
- [ ] **No repo scan happens in `prompt_builder`** (the M3F3 repo signal is
      computed only on the analysis read path).
- [ ] Existing lifecycle rules still hold:
  - [ ] `active` (non-stale) facts are **injected**.
  - [ ] `stale` / `archived` / `historical` facts are **not** injected.

## 9. Regression commands (PowerShell)

```powershell
# Focused M3F backend tests (free exclusions + reality warnings + supporting)
python -m pytest backend\tests\test_memory_free_exclusions.py `
  backend\tests\test_memory_reality_warnings.py `
  backend\tests\test_memory_prompt_builder.py `
  backend\tests\test_memory_injection_provenance.py `
  backend\tests\test_memory_injection_analysis.py `
  backend\tests\test_memory_trust.py `
  backend\tests\test_memory_repo_reality.py -q -m unit

# Lint (repo enforces `ruff check`, NOT `ruff format`) on the M3F backend files
python -m ruff check backend\memory\prompt_builder.py `
  backend\memory\injection_store.py `
  backend\memory\injection_analysis.py `
  backend\routes\memory.py

# Frontend build + touched-file ESLint
cd frontend
npm.cmd run build
npx.cmd eslint src\components\RunMemoryProvenancePanel.tsx src\api\client.ts
cd ..

# Whitespace / conflict-marker hygiene
git diff --check
```

Expected: all listed tests pass; `ruff check` reports no issues on these files;
the frontend build succeeds (a pre-existing chunk-size warning is unrelated);
touched-file ESLint is clean; `git diff --check` is clean.

Note: **repo-wide lint may still report pre-existing, unrelated errors** outside
the M3F files. The touched-file ESLint command above should be clean for the M3F
UI files. The `-m unit` selector deselects `@pytest.mark.api` tests that need
live keys + a target repo; none of the M3F tests above require live keys.

## 10. Known limitations / deferred work

- **M3F2b** status-excluded summary (stale / archived / historical as a bounded
  count) is **deferred / optional** — it requires widening the active-only query.
- **M3F4** injection *tightening* (gated, opt-in exclusion) is **deferred /
  optional** — surface before suppress; only if M3F2a/M3F3 data justify it.
- **No automatic exclusion** of reality-mismatch facts — warnings are advisory
  only.
- No auto-stale / auto-archive / auto-supersede / auto-resolve.
- No reviewer / summary memory injection.
- No semantic / vector / embedding memory.
- **Reality warnings currently depend on safely-available repo signals** and may
  cover **only limited dimensions** — today only `db_engine`
  (postgresql / mysql / mongodb / sqlite). Wider dimensions await safe signal
  sources.
- No **project-level aggregate analytics** yet (provenance/analysis are
  run-scoped).
- No **retention / pruning** changes to `memory_injection_events`.

## 11. Closeout criteria

Memory M3F can be considered **complete enough for the local-first phase** when:

- [ ] M3F2a tests pass (`test_memory_free_exclusions.py`).
- [ ] M3F3 tests pass (`test_memory_reality_warnings.py`).
- [ ] Prompt-block byte-identical tests pass
      (`test_memory_prompt_builder.py` golden + free-exclusions block test).
- [ ] Manual **prompt preview exclusion** smoke passes (§5).
- [ ] Manual **Run Detail provenance exclusion** smoke passes (§6).
- [ ] Manual **repo-reality warning** smoke passes (§7).
- [ ] **No auto-resolution** behavior exists (no auto-stale / archive / supersede
      / resolve / latest-wins).
- [ ] Known limitations (§10) are documented.
- [ ] **M3F4 is explicitly deferred.**

## 12. Result

Fill in after the manual smoke is run:

- **Result:** PASS / FAIL / PARTIAL — _to be filled_
- **Date:** _to be filled_
- **Notes:** _to be filled_
- **Follow-up bugs:** _to be filled_
