# Pipewright — Current State

> Status snapshot for new users, reviewers, recruiters, and future AI assistants.
> This is a **docs-only** page. It changes no runtime code, schema, routes, or
> packages. It records what is complete, what is intentionally deferred, and what
> is safe to start next, after the Operator State / Attention Panel phase and the
> Adversarial Reviewer Stage v1 design doc.

---

## ⚠️ Reconciled 2026-06-14 — read this first

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
  global off-switch — COMPLETE (dormant-by-default).** D5 confirmed 2026-06-14
  (proposal §24's "D5 confirmed" note). One default-off policy flag
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
- **Deferred (not opened):** post-run hygiene / auto-generation (row 16),
  retriever/FTS (row 19), vector/embedding memory (row 23), and the thread/run UI
  (rows 22b–22e). The remaining Row 12 gate is **operational, not code** — flipping
  `MEMORY_RELEVANCE_OMISSION_ENABLED` on is a soak decision.

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
  pinning, D5), plus post-run hygiene (row 16), retriever/FTS (row 19),
  vector/embedding memory (row 23), and thread UI remain not-next until explicitly
  opened. See the workplan.
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
behavior change. **Row 11 is complete; Row 12 PR-A and PR-B are also complete.**
PR-A was no-op-by-default request-aware-selection scaffolding (`request_context`
dormant; `request_context=None` preserves existing injection behavior; policy owns
memory budgets / token estimation; adaptive budgets disabled; safety facts
mandatory; prompt-preview maps mandatory overflow to 422). **PR-B added
request-aware relevance ordering** of the non-mandatory tier (coder-only
`request_context` plumbing; deterministic rung-0 path/token overlap; ordering only,
no omission; mandatory tier never scored/reordered/dropped). See
`PIPEWRIGHT_REDESIGN_WORKPLAN.md`, `PIPEWRIGHT_REDESIGN_IMPL_BRIEF.md`, and the
proposal's Appendix E.1/E.2. **D5 is confirmed (2026-06-14) and Row 12 PR-C is
complete (dormant-by-default);** the next Row 12 gate is operational — flipping
`MEMORY_RELEVANCE_OMISSION_ENABLED` on is a soak decision, not a code change.

- **Safe now (no decision needed):** documentation / smoke-checklist upkeep; small
  honest stabilization fixes; optional PR-B soak follow-ups such as an endpoint
  test for `db_engine` + non-DB warning coexistence, or tightening
  backend-framework manifest substring matching later only if false positives
  appear in soak.
- **Next memory step:** Row 12 is fully implemented — **PR-A** (scaffolding),
  **PR-B** (relevance ordering), and **PR-C** (relevance omission + priority-based
  pinning + global off-switch, dormant-by-default) are all complete. The remaining
  Row 12 action is operational: a soak decision on whether/when to flip
  `MEMORY_RELEVANCE_OMISSION_ENABLED` on. The next *code* memory step is row 16
  (post-run hygiene, D7), row 19 (retriever/FTS), or row 23 (vector, D6).
- **Deferred (explicitly):** activating `MEMORY_RELEVANCE_OMISSION_ENABLED` (soak
  decision, not code); post-run hygiene (row 16, D7); retriever/FTS (row 19);
  vector/embedding memory (row 23, D6); the thread/run UI (rows 22b–22e). Demo /
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

CURRENT NEXT RECOMMENDED TASK: Row 12 is fully implemented; D5 confirmed 2026-06-14.
Row 12 PR-A (scaffolding), PR-B (relevance ordering), and PR-C (relevance omission +
priority-based pinning + global off-switch, dormant-by-default) are COMPLETE. The
remaining Row 12 action is operational (a soak decision on whether/when to flip
MEMORY_RELEVANCE_OMISSION_ENABLED on). PR-A: request_context dormant; request_context=None
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
MEMORY_RELEVANCE_OMISSION_ENABLED (soak decision, not code); post-run hygiene (row 16);
retriever/FTS (row 19); vector/embedding (row 23); thread UI (22b–22e).

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
