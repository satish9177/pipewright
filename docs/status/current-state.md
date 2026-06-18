# Pipewright — Current State

> Status snapshot for new users, reviewers, recruiters, and future AI assistants.
> This is a **docs-only** page. It changes no runtime code, schema, routes, or
> packages. It records what is complete, what is intentionally deferred, and what
> is safe to start next, after the Operator State / Attention Panel phase and the
> Adversarial Reviewer Stage v1 design doc.

---

## ⚠️ Reconciled 2026-06-17 — read this first

This page was written **before** the Pipewright redesign
(`PIPEWRIGHT_REDESIGN_PROPOSAL.md`), and several sections below are **stale**. For
the authoritative current roadmap and status, **`PIPEWRIGHT_REDESIGN_WORKPLAN.md`
is canonical** (sequence: proposal §23; decisions: §24; cycle window: Appendix E).

Current truth:

- **Area A (Pipeline) Pass 1 is COMPLETE.** The engine redesign landed on
  `develop`: the stage driver + append-only attempt ledger, baseline-aware
  verification, bounded `INFRA_ERROR` auto-retry, steered attempts + post-success
  refinement, the **reviewer informed-approval soft gate (now LIVE)**, the
  phase/narrative read-model, the trivial-task stage profile, and provider prompt
  caching.
- **Area B (Pass 2 — Memory) has begun.** §23 **order-row 7 — the M5
  suggestion-quality gate — is COMPLETE** (PR #292): a deterministic scorer, an
  objective-junk floor, per-run caps, the structured coder handoff channel, and an
  additive `quality_score` column — all **pending-only, human-approved**, with the
  content gate intact. Closeout:
  [`../testing/memory-m5-suggestion-quality-smoke.md`](../testing/memory-m5-suggestion-quality-smoke.md).
- **§23 order-row 11 PR-A — detection rules-as-data — COMPLETE.** `_collect_candidates`
  is now a thin adapter (discover → lower → delegate); all detection logic lives in
  `backend/memory/detection_rules.py`. Ordered six-field parity proven with synthetic
  goldens and tests (`test_memory_detection_rules.py` 5 pass; 42 bootstrap tests + full
  3153-unit suite green; ruff clean; dogfood parity at 9 ordered candidates). No
  schema/frontend/UI/gate/scope/Git/PR/memory-authority/vector/request-aware/post-run
  hygiene changes. Pre-PR-B fixture hardening (Django, Next.js, bare Dockerfile,
  alembic.ini, extra Python 3.11 patterns, peerDependencies) is also complete.
- **Row 11 PR-B â€” advisory repo reality signals â€” COMPLETE.** PR-B added
  pure/read-only compute-on-read repo reality signals for non-DB memory dimensions.
  They surface only through the existing memory injection analysis `reality_warnings`
  path. `db_engine` remains sourced from the existing DB fingerprint path, and DB
  conflict gate behavior is unchanged. The rollback / kill switch is
  `policy.REPO_REALITY_SIGNAL_DIMENSIONS`; setting it back to
  `frozenset({"db_engine"})` restores db-only advisory signal behavior. PR-B mutates
  no memory facts: no stale marking, no archiving, no writes, no `last_verified_at`
  bump, no active memory creation, and no auto-approval.
- **Row 11 PR-C — test-command detector rules refactor — COMPLETE.** PR-C
  refactored `backend/pipeline/test_command_detection.py` into explicit ordered
  detector rules while preserving `suggested_test_command` behavior byte-for-byte.
  It included no PR-C2/new detector coverage, no classifier or runtime-validation
  change, and no frontend/schema/memory/gate/scope/Git/PR behavior change.
- **Row 12 PR-A — request-aware memory-selection scaffolding — COMPLETE.** It
  shipped no-op-by-default scaffolding only: `request_context` exists but remains
  dormant, and `request_context=None` preserves existing memory injection
  behavior. Memory budgets and the token estimator are single-sourced in policy;
  adaptive budget scaffolding exists but remains disabled. `security` and
  `forbidden_paths` are mandatory safety facts and cannot be `budget_dropped`;
  mandatory safety overflow uses the typed `MandatoryMemoryBudgetExceeded`, with
  prompt-preview translating that debug/observability overflow into HTTP 422.
  `not_relevant_to_request` is defined but dormant and not emitted. PR-A added no
  relevance ordering, no relevance omission, no D5 activation, no human-pinning,
  no per-project off-switch, no orchestrator `request_context` plumbing, and no
  schema/frontend/gate/scope/Git/PR/memory-mutation/vector/FTS/thread UI change.
- **Row 12 PR-B — request-aware relevance ordering — COMPLETE.** Coder-only
  `request_context` plumbing landed: `run_coder` builds it from `plan.goal`,
  `plan.feature_description`, `files_to_modify + files_to_create`, and
  `continuation_context`, and passes it to the memory builder. A populated context
  reorders **only** the non-mandatory relevance tier by a deterministic rung-0
  signal — path-token overlap, then content-token Jaccard (reusing the
  `memory_trust` helpers) — tie-broken by the existing
  `(category, scope, priority, created_at)` key. `request_context=None` stays
  byte-identical and all-zero overlap preserves the legacy order. The `security` /
  `forbidden_paths` mandatory tier stays first and is never scored, reordered, or
  budget-dropped. **No relevance omission and no `not_relevant_to_request`
  emission** — exclusions keep existing reasons (`budget_dropped` /
  `category_not_allowed_for_role`). PR-B added no D5 activation, no human-pinning,
  no per-project off-switch, no grace threshold / relevance floor, no
  adaptive-budget activation, no `MemoryRetriever` / FTS / vector / embedding work,
  no planner/triage/prompt-preview plumbing, and no
  schema/frontend/gate/scope/Git/PR/memory-mutation change; the scoring
  `files_expected` never reaches `scope_guard`.
- **Row 12 PR-C — request-aware relevance omission + priority-based pinning +
  global off-switch — COMPLETE & MERGED (dormant-by-default). Row 12 is now fully
  implemented (PR-A scaffolding + PR-B ordering + PR-C omission/pinning).** D5
  confirmed 2026-06-14 (proposal §24's "D5 confirmed" note). One default-off policy
  flag
  `MEMORY_RELEVANCE_OMISSION_ENABLED=False` gates **both**: omission emits
  `not_relevant_to_request` for zero path-overlap + zero token-overlap relevance
  facts, but only when the flag is on, the request carries signal, the
  relevance-candidate count exceeds `MEMORY_SMALL_STORE_GRACE_THRESHOLD` (12, over
  non-mandatory in-policy relevance facts), and at least one relevance fact carries
  signal (all-zero signal omits nothing); priority-based pinning routes
  `priority ≤ MEMORY_PIN_PRIORITY_THRESHOLD` (10) facts into the mandatory tier
  (never scored/omitted/budget-dropped). **The flag ships `False`, so default
  behavior is byte-identical to PR-B.** No schema/frontend/per-project setting/
  planner/triage/prompt-preview plumbing/adaptive-budget activation/retriever/FTS/
  vector/memory-mutation change. Rollback: keep `MEMORY_RELEVANCE_OMISSION_ENABLED=False`.
- **Row 12 omission readiness Stage 2 — COMPLETE.** Added backend unit/integration
  readiness proof and a manual UI smoke doc for future activation: flag-on safety,
  coder-path omission persistence, pinned/safety survival, flag-off rollback, and
  the current prompt-preview pin-overflow 422 tripwire are covered. The flag still
  ships `False`; activation and prompt-preview wording changes remain later.
- **Row 16 PR-A — dormant post-run hygiene trigger — COMPLETE.** Added
  default-off `MEMORY_POSTRUN_HYGIENE_ENABLED` and a success-terminal-only, post-lock
  `pr_orchestrator` trigger for existing run-outcome suggestion generation. Because
  the flag ships false, the manual `memory-suggestions/generate` route remains the
  only active generation path by default. When enabled in tests, the trigger is
  best-effort, uses `requested_by="postrun_auto"`, creates pending suggestions only
  through the existing generator, and does not auto-approve, create active facts, or
  mutate memory lifecycle state. It is not attached to `_update_run_status`.
- **Row 16 PR-B — post-run hygiene digest / observability — COMPLETE.** Added a
  read-only `GET /api/v1/runs/{run_id}/memory-suggestions` digest plus a neutral
  Run Detail card showing persisted pending suggestions from the run. It does not
  call the generator, does not show transient generated/skipped/blocked/floored/
  capped counts, does not mutate suggestions/facts/gates, and does not replace the
  existing manual generate route. `MEMORY_POSTRUN_HYGIENE_ENABLED` remains
  default false.
- **Row 16 controlled env-gated soak — AVAILABLE.** Maintainers can set
  `PIPEWRIGHT_MEMORY_POSTRUN_HYGIENE_ENABLED=true` for local/dev soak of the
  existing PR-A trigger. Unset, false-ish, and invalid values keep automatic
  post-run hygiene disabled; no committed config enables it. Rejected same-content
  suggestions no longer silently return from later runs; default-on PR-C activation
  remains deferred pending maintainer decision and fresh soak evidence.
- **Row 16b - ledger metrics / observability queries doc - COMPLETE (docs-only).**
  Added [`../metrics/ledger-metrics-queries.md`](../metrics/ledger-metrics-queries.md):
  SELECT-only SQLite queries over existing ledger metadata for pre-activation
  evidence collection across stage profiles, attempt outcomes, `INFRA_ERROR`
  recovery, approvals, steer/refinement usage, reviewer acknowledgement proxies,
  Row 16 suggestion yield, Row 12 omission pressure, prompt-cache opportunity,
  plan-turn activity, and stuck runs. It does not activate dormant flags, change
  runtime behavior, change schema, alter policy, or touch approval/final
  approval/Git/PR behavior.
- **Row 16b soak evidence run — COMPLETE (docs-only, 2026-06-18).** The §11
  queries were run `SELECT`-only against a throwaway read-only copy of
  `backend/db/pipewright.db` (no SQL errors; no code/flag/schema/runtime change).
  **Recommendation: keep all dormant flags OFF.** The dev DB has ~255 runs over
  ~3 weeks but reads as **seeded/synthetic** (sub-minute approval latency,
  frequent gate timeouts, small `run_turns`/`chunk_attempts` samples). Key
  findings: `INFRA_ERROR` retry/recovery has **zero evidence**; prompt-cache
  activation has **no evidence** because `llm_call_provenance` is incomplete and
  effectively DeepSeek/coder-only (no Anthropic/Gemini provenance); Row 12
  omission still shows **no meaningful coder/planner pressure**; the Row 16 auto
  hygiene path has no evidence and the manual `run_outcome` suggestion rejection
  rate is a quality yellow flag; the reviewer acknowledgement path exists but has
  low recorded ack volume. Results recorded in
  [`../metrics/ledger-metrics-queries.md`](../metrics/ledger-metrics-queries.md)
  §12. Gather real-traffic soak data (and broader provenance coverage) before
  revisiting any activation.
- **§23 row 7b — plan-gate turns + plan-version lineage — COMPLETE & MERGED,
  default-off.** PR-A added
  the internal plan-turn engine scaffold; PR-B added
  `POST /runs/{run_id}/plan-turns` behind `PLAN_TURNS_ENABLED`; PR-C added the
  collapsed Run Detail "Revise plan" affordance at the chunk-plan approval gate;
  and PR-D wired local activation through `PIPEWRIGHT_PLAN_TURNS_ENABLED` while
  keeping the default off. Final env-var smoke was **PARTIAL only because browser
  visual automation was blocked locally** (`CreateProcessAsUserW failed: 5`; the
  Vite dev process also exited under automated launch), not because of a known
  product bug. Backend/API/DB smoke with process-local
  `PIPEWRIGHT_PLAN_TURNS_ENABLED=true` passed on project `proj-4d529cfb`, run
  `dcacba8c-a993-44b6-bc50-e3ba0c57bea1`: the run reached
  `awaiting_chunk_plan_approval`; `POST /runs/{run_id}/plan-turns` returned 200
  with `plan_version=2`, `total_chunks=3`, and
  `chunk_plan_status=awaiting_approval`; DB contained v2
  `plan_versions.source='plan_turn'`; live `pipeline_runs.chunk_plan` changed
  from 2 to 3 chunks; pending chunk rows were replaced; and the run still required
  explicit plan approval. The existing approve route approved the revised plan,
  chunks stayed pending, and no execution route was called / auto-triggered.
  Flag-off verification passed after restart without the env var: a valid
  plan-turn request returned 404 and the frontend source maps that to "Plan
  revision is not enabled for this run." Follow-up stabilization slices are also
  merged: **Slice A** added backend `GET /runs/{run_id}/plan-versions` as a
  read-only lineage/audit model; **Slice B** added nullable
  `approved_plan_version` binding on chunk-plan approval and exposes it as
  top-level `approved_version`; and **Slice C** added the frontend plan-version
  lineage display on Run Detail. Later manual smoke after Slice C confirmed
  flag-off behavior still hides valid plan-turn requests as 404 with the
  frontend disabled copy, and flag-on behavior shows the revise/history/approval
  flow: revisions update lineage, approval stamps/displays the approved version,
  and the revised plan still requires explicit human approval. Safety invariants
  held: no auto-execution, no approval blocker from lineage/stamping, and no
  final-approval bypass. Production frontend build passed with the existing Vite
  chunk-size warning. The earlier Windows browser-automation gap remains recorded;
  no newer uncompleted browser/manual limitation was recorded in this closeout.
  Default remains off.
- **Row 19 PR-A/PR-B/PR-C — FTS-backed MemoryRetriever rung 1 — COMPLETE &
  MERGED, default-off and explicit-rebuild-only.** PR-A added the guarded SQLite
  FTS5 scaffold/table and explicit rebuild lifecycle; PR-B added the
  `MemoryRetriever` seam with deterministic rung-0 behind it; PR-C added FTS
  rung-1 behind `PIPEWRIGHT_MEMORY_FTS_RETRIEVAL_ENABLED`. Flag-off behavior is
  byte-identical to deterministic rung-0/current memory output. Flag-on uses FTS
  only as an advisory ordering signal over the canonical rung-0 candidate set: it
  never adds, drops, caps, or cross-projects candidates, and mandatory/safety facts
  are never scored, demoted, omitted, or dropped. Explicit rebuild/population is
  required for flag-on to have effect. There is no rebuild-on-write, no lazy
  rebuild-on-read, no endpoint, no frontend, no `schema.sql` FTS DDL, no
  approval/execution/final-approval/Git/PR behavior change, and no Row 23 vector
  memory.
- **Row 19 FTS populate/soak follow-up — COMPLETE & MERGED.** PR-1 added an
  explicit guarded manual FTS rebuild CLI; PR-2 added the read-only compare/seed
  soak harness and [`../design/row-19-fts-soak-results.md`](../design/row-19-fts-soak-results.md).
  The seeded soak passed: included set identical, mandatory tier identical, only
  relevance-tier order changed, no cross-project facts, deterministic output. The
  real-project soak safely fell back with zero ordering delta. FTS retrieval
  remains default-off/dormant. No activation trigger, no rebuild-on-write, no lazy
  rebuild-on-read, no default-on flip, no endpoint/frontend/`schema.sql`/boot-
  migration populate, and no Row 23 vector/embedding work. The later
  approval-write-path rebuild trigger remains deferred and should not be started
  unless future soak shows real value.
- **Phase 2F Thread UI / Run Timeline — COMPLETE & MERGED (2026-06-17), read-only.**
  The Run Detail thread/run timeline shipped through PR-0..PR-4 plus review fixes
  PR-A/PR-B/PR-C (design brief + closeout:
  [`../design/phase-2f-thread-ui.md`](../design/phase-2f-thread-ui.md) §13). PR-0
  added the backend read-only `GET /runs/{run_id}/timeline` derived from existing
  persisted tables; PR-1 added the frontend `useRunTimeline` hook + additive
  `RunTimeline`; PR-2 added the read-only `RunTimelineDetail` master-detail panel;
  PR-3 promoted the timeline to the primary Run Detail layout and made the existing
  `OperatorAttentionPanel` sticky/prominent; PR-4 added the Plain English / Developer
  view toggle. PR-A fixed backend timeline correctness/redaction tests; PR-B fixed
  persisted/live dedupe and timeline refresh; PR-C fixed redaction polish, sticky
  height, the `localStorage` guard, and a11y. **Invariants held:** no PR-5 / event
  persistence, no schema or event table, no backend writes, no POST lifecycle handler
  changes, no approval/final-approval/Git/PR behavior change, no memory-retrieval
  change, no FTS/Row 19 activation, and no Row 23/vector work — the only backend
  surface added is the single read-only GET. **PR-5 (fine-grained event persistence)
  remains deferred** and is the only unshipped slice of the brief.
- **Phase 2G Run Detail Product UI - COMPLETE & MERGED (2026-06-17), frontend
  presentation/composition only.** The design spec is complete
  ([`../design/phase-2g-run-detail-product-ui.md`](../design/phase-2g-run-detail-product-ui.md)).
  PR-1 merged the two-column cockpit/context shell; PR-2 merged Running and
  Needs-review context rail trust facts; PR-4 merged Done-state PR
  de-duplication and the authoritative PR rail; PR-5 merged the Failed-state
  failure rail; PR-3 merged decision evidence near the approval cockpit; and
  PR-6 merged visual/register polish plus Plain/Developer mode cleanup. Final
  state: Run Detail is organized around the cockpit, safety overview, context
  rail, decision evidence, timeline, and collapsed audit/details. **Invariants
  held:** no backend behavior changes, no mutation handler changes, no
  approval/final approval/Git/PR behavior changes, no new actions, no event
  persistence / Phase 2F PR-5 work, no memory retrieval changes, no FTS/Row 19
  changes, and no Row 23 work. **Validation recorded:** build/lint/diff checks
  passed per slice; PR-3 demo-smoke passed all 10 checks; PR-6 SSR smoke passed
  running/final approval/done/failed across Plain and Developer modes; and
  protected-path checks confirmed frontend/docs-only scope per slice.
- **Deferred (not opened):** Row 16 PR-C activation, vector/embedding memory
  (row 23), and Phase 2F **PR-5** (fine-grained event persistence — the only
  unshipped slice of the thread/run timeline UI; needs its own brief). The
  remaining Row 12 gate is **operational, not code** — flipping
  `MEMORY_RELEVANCE_OMISSION_ENABLED` on is a soak decision (and omission/pinning
  stay dormant until then). **Next step: a maintainer / Claude roadmap review
  before opening any next row/PR or activating a flag.**

The redesign preserves every safety invariant in this doc: human approval gates,
scope guard, branch/PR safety, no empty commits, no auto-merge, pending-only
human-approved memory, and "memory is advisory." Sections below tagged **[STALE
2026-06-14]** predate the redesign and are kept only for history.

---

## What Pipewright is (one paragraph)

Pipewright is a **human-controlled AI engineering pipeline orchestrator** for
existing codebases. It takes a feature request, classifies intent, proposes a
chunk plan, and **stops for human approval** before writing any code. It then
executes approved chunks one at a time through a guarded patch layer, runs your
verification command, surfaces recovery options when something goes wrong, and
reaches a local commit or a pull request **only after an explicit final human
approval**. It is a safety and workflow layer *around* coding LLMs — not a
free-roaming agent, not an autocomplete, and not a greenfield app generator.

Pipewright does **not** prove your code is correct. It enforces process safety
(scope, approvals, guarded patching, honest test-validation visibility) so a human
stays in control of every risky step.

---

## Pipeline flow (today)

```
feature request
  → chunk plan
  → human approves chunk plan
  → execute chunk (planner → coder → guarded patch)
  → run verification command (tests)
  → [recovery paths if needed: patch retry / scope expansion / weak-test ack]
  → chunk approval (high-risk chunks) / chunk completion
  → final human approval
  → PR creation (github_cli / manual_token) OR local-only complete
```

The Operator Attention Panel is a **display-only** overlay across these states; it
explains what needs attention but is not itself a gate.

---

## Completed work (by area)

### Core pipeline & safety (stable, test-locked)

- Legacy single-shot `/run` retired (HTTP 410); `POST /runs/chunked` is the only
  implementation path.
- Chunk plan approval before any code is written.
- Scope guard / `files_expected` allowlist; out-of-scope edits blocked.
- Forbidden-path / traversal protection (`.env`, secrets, credentials, absolute
  paths, `..` escapes).
- No empty / no-effective-change commits.
- Large-file safe targeted edits.
- Branch safety: never PR against `main` / `master` / `develop`; default base
  `pipewright-staging`.
- `local_only` by default; no auto-merge; final approval before completion.

See [`../stabilization/final-smoke-status.md`](../stabilization/final-smoke-status.md)
for the guarantee-to-test mapping.

### Recovery & validation (recent milestones)

| Milestone | Area | Status |
| --- | --- | --- |
| **#26** | Patch Failure Recovery v2 | **Complete.** Failed apply → clean failure + plain-English copy + guarded, server-revalidated retry; nothing committed on failure. |
| **#27** | Scope Expansion Recovery | **Complete, manually smoke-validated.** `SCOPE_VIOLATION` pauses the run, shows requested extra files, requires human approval to amend scope, then retries. Never auto-expands; never weakens `scope_guard`. |
| **#28** | Stronger Runtime Test Validation | **Complete, manually smoke-validated.** Runtime verdict (`strong`/`weak`/`none`/`unknown`) joined from command string + execution evidence; weak/none requires a human acknowledgement bound to the exact diff before final approval. |

### Operator State / Attention Panel

**Complete enough and manually smoke-validated.** Delivered:

- design doc ([`../design/operator-state-attention-panel.md`](../design/operator-state-attention-panel.md));
- shared eligibility helpers (reused by routes and the read model);
- a pure, read-only `operator_state` helper (no I/O, never persisted);
- API / read-model surfacing on the chunk read response;
- a frontend **display-only** attention panel;
- plain-English patch-failure copy;
- smoke verification across key states (plan approval, patch failure/retry, scope
  expansion, weak-test acknowledgement, chunk approval, final approval, PR /
  local-only, terminal).

It is additive and display-only: the existing controls remain authoritative and
routes revalidate every mutating action.

### Adversarial Reviewer Stage v1 — **design only** — **[STALE 2026-06-14]**

> **[STALE 2026-06-14]** Superseded by the redesign. The **reviewer
> informed-approval soft gate (Pass 1 §4.5 / Phase 4 item 15) shipped** (PR #287):
> a delivered `high` × {`requirement_mismatch`, `security`} finding now requires a
> human acknowledgement before approval. The reviewer still **gates nothing on its
> own, commits nothing, mutates nothing, and writes no memory** — it is advisory
> and the human decides. The "no AI review runs in the pipeline today" bullet
> below is out of date. See `PIPEWRIGHT_REDESIGN_WORKPLAN.md` (Phase 4 item 15).

- Design doc merged: [`../design/adversarial-reviewer-stage.md`](../design/adversarial-reviewer-stage.md).
- **Implementation intentionally deferred** pending a priority decision.
- **No AI review runs in the pipeline today.** The `reviewer` LLM role is
  configurable but not invoked by any stage.
- When built, v1 is specified to be **advisory / display-only**: it gates nothing,
  commits nothing, mutates nothing, writes no memory, and does not weaken
  #26/#27/#28.

---

## Intentionally deferred (do not start accidentally)

- **Adversarial Reviewer implementation.** Design-only. Requires explicit
  prioritization before any code slice — it is a new product feature, not polish.
- **Reviewer acknowledgement gate.** Not in scope until the advisory reviewer ships
  and is smoke-validated.
- **Memory writes from reviewer findings**, **PR comments**, **durable audit
  system**, **multi-model routing UI** — all out of the reviewer stage.
- **Memory M3** (conflict lifecycle, categories, usage tracking, constrained
  LLM-assisted memory, pgvector at scale). **[STALE 2026-06-14]** The redesign
  reframes Area B (memory) as the §23 row series: M5 (row 7) shipped; **row 11
  PR-A (detection rules-as-data) is COMPLETE**; pre-PR-B fixture hardening is
  COMPLETE; **PR-B (advisory repo reality signals) is COMPLETE**; **PR-C
  (behavior-preserving test-command detector rules refactor) is COMPLETE**; and
  **Row 11 is COMPLETE**. Request-aware selection **row 12 PR-A** (no-op
  scaffolding) is COMPLETE; its **PR-B** (ordering) and **PR-C** (omission /
  pinning, D5), plus post-run hygiene (row 16). **Row 19 retriever/FTS is now
  complete and merged, default-off and explicit-rebuild-only.** Vector/embedding
  memory (row 23) remains not-next until explicitly opened; the Phase 2F read-only
  thread/run timeline UI is now COMPLETE & MERGED (PR-0..PR-4 + PR-A/PR-B/PR-C),
  with only its PR-5 event persistence deferred. See the workplan.
- **Production hardening** (Postgres/Alembic, durable events, DB locks at scale,
  deployment).
- **Deployment / Ollama / Provider Settings UI / BYOK DB storage / execution
  modes / GitHub App / OAuth / multi-tenant auth / visual diff editor / per-file
  approval** — all paused per project rules.

---

## Known limitations (be honest)

- Pipewright **does not prove code correctness.** A `strong` test verdict means
  meaningful tests ran and passed per Pipewright's process rules — not that the
  change is correct.
- `operator_state` currently surfaces through **chunk read data**, so a legacy run
  with **no chunk plan** may not show an attention panel.
- The Operator Attention Panel actions are **display-only**; the older controls
  remain the real controls.
- The **Adversarial Reviewer Stage is design-only**; no review executes.
- **No durable audit trail / run-history table** yet.
- **No role-based PM / manager views** yet.
- **No multi-model routing UI** — model selection is env-based per role only.
- **Production deployment / hardening is not complete**; this is local single-user
  self-use / demo-ready, not a production SaaS.
- Single-instance assumptions remain (SQLite, in-memory live logs, in-process repo
  locks).
- **Not all edge cases are solved**; recovery paths cover the common failures
  surfaced during dogfooding, not every possible state.

### Non-blocking self-use smoke follow-ups

The self-use smoke friction cleanup is closed. These are follow-ups, not
blockers, and they must preserve the existing safety invariants: no auto-retry,
no silent create-to-edit patch rewriting, no approval or final-approval bypass,
no scope/path-safety weakening, and no retry eligibility or budget change.

- **Steer-button eligibility parity:** `Retry with instruction` should not appear
  enabled when the server-side steer route would reject because the tree is
  dirty, the attempt budget is exhausted, the branch/state is stale or wrong, or
  a similar ineligible condition applies.
- **Create-collision primary copy:** for create-target-existing
  `PATCH_DOES_NOT_APPLY` failures, make the primary Run Detail headline/detail
  say the file already exists and should be edited, instead of relying only on
  diagnostic or suggested-instruction text.
- **Test evidence confidence mismatch:** run `d3ba080a` showed the backend tester
  ran the configured absolute-path pytest command successfully with `179 passed /
  0 failed`, while Run Detail still displayed unknown/unverified evidence.
  Investigate whether command classification, persisted test-run validation, the
  chunk read model, or frontend display is losing the stronger evidence.
- **Optional frontend tests:** add component coverage for `PatchFailureBanner`
  retry/steer affordance rendering when the frontend test posture supports it.
- **Minor classifier cleanup/extensions:** clean unreachable or overly narrow
  command matchers, and extend wrappers only when real setups need them.

---

## What is safe to start next

**[Updated 2026-06-14]** §23 order-row 11 **PR-A is COMPLETE**: `_collect_candidates`
is a thin adapter; detection rules live in `backend/memory/detection_rules.py`; parity
proven with synthetic goldens and tests. Pre-PR-B fixture hardening is also complete;
PR-B added pure/read-only advisory repo reality signals for non-DB dimensions, computed
on read and surfaced only through existing memory injection analysis `reality_warnings`.
`db_engine` and DB conflict gate behavior remain unchanged; `policy.REPO_REALITY_SIGNAL_DIMENSIONS`
is the db-only rollback kill switch. PR-B mutates no memory facts: no stale marking,
archiving, writes, `last_verified_at` bump, active memory creation, or auto-approval.
PR-C refactored the test-command detector into ordered rules with no PR-C2/new
coverage and no classifier/runtime-validation/frontend/schema/memory/gate/scope/Git/PR
behavior change. **Row 11 is complete; Row 12 is complete — PR-A, PR-B, and PR-C are all merged.**
PR-A was no-op-by-default request-aware-selection scaffolding (`request_context`
dormant; `request_context=None` preserves existing injection behavior; policy owns
memory budgets / token estimation; adaptive budgets disabled; safety facts
mandatory; prompt-preview maps mandatory overflow to 422). **PR-B added
request-aware relevance ordering** of the non-mandatory tier (coder-only
`request_context` plumbing; deterministic rung-0 path/token overlap; ordering only,
no omission; mandatory tier never scored/reordered/dropped). **PR-C added
request-aware relevance omission + priority-based human-pinning + a global
off-switch**, all behind one default-off flag `MEMORY_RELEVANCE_OMISSION_ENABLED`
(grace threshold 12 over relevance-tier facts; pinned = `priority ≤ 10`). See
`PIPEWRIGHT_REDESIGN_WORKPLAN.md`, `PIPEWRIGHT_REDESIGN_IMPL_BRIEF.md`, and the
proposal's Appendix E.1/E.2. **D5 is confirmed (2026-06-14) and Row 12 is complete —
PR-A, PR-B, and PR-C are all merged, with PR-C shipped dormant
(`MEMORY_RELEVANCE_OMISSION_ENABLED=False`).** Omission and pinning are not active
until the flag is explicitly flipped later (a soak decision, not a code change).
**Recommended next step: a maintainer / Claude roadmap review before opening any new
row/PR or activating a flag** — Row 16 PR-A and PR-B are implemented, and an
env-gated local/dev soak is available with default false; Row 19 is complete and
default-off; do not auto-start default-on Row 16 PR-C, row 23, or Phase 2F PR-5
(fine-grained event persistence).

- **Safe now (no decision needed):** documentation / smoke-checklist upkeep; small
  honest stabilization fixes; optional PR-B soak follow-ups such as an endpoint
  test for `db_engine` + non-DB warning coexistence, or tightening
  backend-framework manifest substring matching later only if false positives
  appear in soak.
- **Next memory step:** Row 12 is complete and merged — **PR-A** (scaffolding),
  **PR-B** (relevance ordering), and **PR-C** (relevance omission + priority-based
  pinning + global off-switch, dormant-by-default) are all done. Row 19 is also
  complete and merged: PR-A inert FTS scaffold, PR-B retriever seam, and PR-C
  default-off FTS rung-1 retrieval behind
  `PIPEWRIGHT_MEMORY_FTS_RETRIEVAL_ENABLED`. Flag-off is byte-identical to rung-0;
  flag-on FTS is advisory ordering only and requires explicit index rebuild. The
  recommended next step is a **maintainer / Claude roadmap review before opening
  any new row or activating a flag** — do not auto-start Row 16 PR-C, row 23
  (vector, D6), or Phase 2F PR-5 (event persistence). The only outstanding Row 12 action is
  operational: a soak decision on whether/when to flip
  `MEMORY_RELEVANCE_OMISSION_ENABLED` on.
- **Row 16 (post-run hygiene) — PR-A and PR-B implemented; env-gated soak available.**
  [`../design/memory-postrun-hygiene-row16.md`](../design/memory-postrun-hygiene-row16.md)
  records the D7 framing: `MEMORY_POSTRUN_HYGIENE_ENABLED` defaults false and can
  be enabled only for local/dev soak with
  `PIPEWRIGHT_MEMORY_POSTRUN_HYGIENE_ENABLED=true`; the manual
  `memory-suggestions/generate` route stays the only active generation path by
  default. The PR-A trigger is success-terminal-only, best-effort, and called from
  `pr_orchestrator` after `complete` is committed and the repo lock is released —
  not `_update_run_status`, with no shared terminal-settle refactor. PR-B adds only
  a read-only pending-suggestion digest/card; it does not call the generator or show
  transient generated/skipped/blocked/floored/capped counts. Failed/`rejected`/
  `push_failed` hygiene and default-on PR-C activation are later decisions. Rejected
  same-content suggestions are suppressed by project/content hash so they do not
  silently reappear from later runs. Row 16b is docs-only observability guidance:
  [`../metrics/ledger-metrics-queries.md`](../metrics/ledger-metrics-queries.md)
  prepares evidence collection for future activation decisions without enabling
  post-run hygiene by default or changing runtime behavior.
- **Deferred (explicitly):** activating `MEMORY_RELEVANCE_OMISSION_ENABLED` (soak
  decision, not code); Row 16 PR-C activation; vector/embedding memory
  (row 23, D6); Phase 2F PR-5 (fine-grained event persistence — the rest of the
  thread/run timeline UI shipped 2026-06-17, PR-0..PR-4 + PR-A/PR-B/PR-C). Demo /
  README / devex polish remains fine opportunistically, but is no longer the
  recommended next step.

---

## Context handoff (copy-paste for a future ChatGPT / Claude / Codex chat)

```text
PROJECT: Pipewright — a human-controlled AI engineering pipeline orchestrator for
existing codebases. Flow: request → chunk plan → human approval → execute chunk
(planner → coder → guarded patch) → run verification command → recovery paths if
needed → chunk/final human approval → PR or local-only complete. It is a safety
layer around coding LLMs, NOT an autonomous agent.

STATUS: Local self-use / demo-ready. NOT production SaaS.

COMPLETED SAFETY SYSTEMS:
- Chunk plan approval before any edit; final approval before any commit/PR.
- Scope guard (files_expected allowlist); out-of-scope edits blocked.
- Forbidden-path/traversal protection (.env, secrets, credentials, .. escapes).
- No empty / no-effective-change commits. No auto-merge. Branch safety
  (never PR main/master/develop; default base pipewright-staging).
- #26 Patch Failure Recovery v2: clean failure + guarded, server-revalidated retry.
- #27 Scope Expansion Recovery: SCOPE_VIOLATION pauses; human approves extra files
  before retry; never auto-expands; never weakens scope_guard.
- #28 Stronger Runtime Test Validation: strong/weak/none/unknown verdict; weak/none
  requires human acknowledgement bound to the exact diff before final approval.
- Operator State / Attention Panel: read-only, display-only overlay; complete and
  smoke-validated.

REDESIGN STATUS (2026-06-14): Area A (Pipeline) Pass 1 COMPLETE — stage driver +
attempt ledger; baseline-aware verification; bounded INFRA_ERROR auto-retry; steered
+ post-success refinement; reviewer informed-approval SOFT GATE (LIVE, advisory,
human decides); phase/narrative read-model; trivial-task profile; prompt caching.
Area B (Memory) Pass 2 ACTIVE: M5 suggestion-quality gate COMPLETE (PR #292) —
deterministic scorer + junk floor + per-run caps + structured coder channel +
quality_score column, all pending-only/human-approved. §23 ROW 11 PR-A COMPLETE —
detection rules extracted to backend/memory/detection_rules.py; _collect_candidates
is thin adapter; six-field parity proven with synthetic goldens + tests.
Pre-PR-B fixture hardening COMPLETE. ROW 11 PR-B COMPLETE: pure/read-only
compute-on-read advisory repo reality signals now cover non-DB dimensions and
surface only through existing memory injection analysis reality_warnings. db_engine
and DB conflict gate behavior are unchanged. policy.REPO_REALITY_SIGNAL_DIMENSIONS
is the kill switch; db-only rollback is possible. PR-B mutates no memory facts:
no stale marking, archiving, writes, last_verified_at bump, active memory creation,
or auto-approval. ROW 11 PR-C COMPLETE: behavior-preserving test-command detector
rules refactor, with no PR-C2/new detector coverage and no classifier,
runtime-validation, frontend, schema, memory, gate, scope, Git, or PR behavior
change. ROW 11 COMPLETE. CANONICAL roadmap: PIPEWRIGHT_REDESIGN_WORKPLAN.md (sequence
proposal §23; decisions §24; cycle window Appendix E). This current-state page is
a snapshot; the workplan wins.

CURRENT NEXT RECOMMENDED TASK: a maintainer / Claude roadmap review before opening any
new row/PR or activating a flag. Row 12 is COMPLETE and MERGED; D5 confirmed 2026-06-14.
Row 12 PR-A (scaffolding), PR-B (relevance ordering), and PR-C (relevance omission +
priority-based pinning + global off-switch, dormant-by-default) are all merged. The
only outstanding Row 12 action is operational (a soak decision on whether/when to flip
MEMORY_RELEVANCE_OMISSION_ENABLED on). Row 19 is COMPLETE and MERGED: PR-A added the
guarded inert SQLite FTS scaffold and explicit rebuild lifecycle; PR-B added the
MemoryRetriever seam and deterministic rung-0; PR-C added default-off FTS rung-1 behind
PIPEWRIGHT_MEMORY_FTS_RETRIEVAL_ENABLED. Flag-off is byte-identical to rung-0/current
memory behavior. Flag-on uses FTS only as an advisory ordering signal over canonical
rung-0 candidates, never adds/drops/caps/cross-projects candidates, never scores/
demotes/omits/drops mandatory or safety facts, and requires explicit FTS rebuild/
population to matter. No rebuild-on-write, lazy rebuild-on-read, endpoint, frontend,
schema.sql FTS DDL, approval/execution/final approval/Git/PR behavior change, or Row 23
vector memory. Phase 2F Thread UI / Run Timeline is COMPLETE & MERGED (2026-06-17),
read-only, through PR-0..PR-4 + review fixes PR-A/PR-B/PR-C: PR-0 backend read-only
GET /runs/{run_id}/timeline from persisted tables; PR-1 useRunTimeline + additive
RunTimeline; PR-2 read-only RunTimelineDetail; PR-3 timeline promoted to primary Run
Detail layout + OperatorAttentionPanel made sticky; PR-4 Plain English / Developer
view toggle. No PR-5/event persistence, no schema/event table, no backend writes, no
POST lifecycle handler changes, no approval/final-approval/Git/PR change, no
memory-retrieval change, no FTS/Row 19 activation, no Row 23/vector work. Phase
2G Run Detail Product UI is COMPLETE & MERGED (2026-06-17), frontend
presentation/composition only: PR-1 cockpit/context shell; PR-2 Running/Review
context rail; PR-4 Done PR de-duplication + authoritative PR rail; PR-5 Failed
failure rail; PR-3 decision evidence near approval; PR-6 visual/register polish
and Plain/Developer cleanup. Run Detail is now organized around the cockpit,
safety overview, context rail, decision evidence, timeline, and collapsed
audit/details; no backend behavior, mutation handler, approval/final
approval/Git/PR, event persistence, memory retrieval, FTS/Row 19, or Row 23 work
changed. Do not auto-start Row 16 PR-C, row 23, or Phase 2F PR-5 (event
persistence).
PR-A: request_context dormant; request_context=None
preserves existing injection behavior; budgets/estimator single-sourced in policy;
adaptive budget scaffolding disabled; security+forbidden_paths mandatory and cannot
be budget-dropped; MandatoryMemoryBudgetExceeded is the typed overflow;
prompt-preview maps mandatory overflow to HTTP 422; dormant not_relevant_to_request
not emitted. PR-B: coder-only request_context plumbing; deterministic rung-0
path/token relevance ordering of the non-mandatory tier (reusing memory_trust
helpers), tie-broken by the legacy key; request_context=None byte-identical;
all-zero overlap preserves legacy order; mandatory tier never
scored/reordered/dropped; ordering only, no omission, not_relevant_to_request still
never emitted. PR-C: one default-off flag MEMORY_RELEVANCE_OMISSION_ENABLED gates
BOTH relevance omission (zero path+token overlap relevance facts excluded with
not_relevant_to_request, only above MEMORY_SMALL_STORE_GRACE_THRESHOLD=12 counted over
non-mandatory relevance facts, with the all-zero degeneracy guard preserved) and
priority-based pinning (priority<=MEMORY_PIN_PRIORITY_THRESHOLD=10 joins the mandatory
tier, never scored/omitted/budget-dropped); flag ships False so default == PR-B
byte-for-byte; no schema/frontend/per-project/planner/triage/prompt-preview plumbing/
adaptive/retriever/FTS/vector/memory-mutation change. DEFERRED: activating
MEMORY_RELEVANCE_OMISSION_ENABLED (soak decision, not code); post-run hygiene (row 16 —
PR-A COMPLETE: dormant default-off success-terminal-only pr_orchestrator trigger behind
MEMORY_POSTRUN_HYGIENE_ENABLED; manual route remains the only active path by
default unless env-gated soak is explicitly enabled. PR-B COMPLETE: read-only pending-suggestion digest/card. ENV-GATED SOAK
AVAILABLE: PIPEWRIGHT_MEMORY_POSTRUN_HYGIENE_ENABLED=true enables local/dev soak
only; rejected same-content suggestions no longer silently return from later runs;
default-on PR-C activation deferred); vector/embedding (row 23); Phase 2F PR-5
(fine-grained event persistence — the rest of the thread/run timeline UI shipped).

INVARIANTS (do not violate):
- Never bypass chunk plan approval or final approval. Final approval is NOT
  automatic.
- Never auto-expand scope or weaken scope_guard. Never auto-merge.
- Never claim Pipewright proves code correctness.
- The reviewer is an advisory informed-approval SOFT GATE (LIVE as of Area A Pass
  1): it can require human acknowledgement of a delivered high×{requirement_mismatch,
  security} finding, but it gates/commits/writes nothing itself and can never reject
  or auto-act — the human always decides.
- Routes revalidate server-side; the Attention Panel is display-only.
- Docs-only changes must not alter runtime behavior, schema, routes, or packages.
```

---

## Related docs

- README — [`../../README.md`](../../README.md)
- Demo walkthrough — [`../demo/local-self-use-demo.md`](../demo/local-self-use-demo.md)
- Demo / readiness smoke checklist — [`../testing/demo-smoke-checklist.md`](../testing/demo-smoke-checklist.md)
- Next-phase roadmap — [`../roadmap/next-phase.md`](../roadmap/next-phase.md)
- Safety guarantees & tests — [`../stabilization/final-smoke-status.md`](../stabilization/final-smoke-status.md)
- Operator State design — [`../design/operator-state-attention-panel.md`](../design/operator-state-attention-panel.md)
- Adversarial Reviewer design (deferred) — [`../design/adversarial-reviewer-stage.md`](../design/adversarial-reviewer-stage.md)
