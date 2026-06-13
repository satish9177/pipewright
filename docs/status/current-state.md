# Pipewright — Current State

> Status snapshot for new users, reviewers, recruiters, and future AI assistants.
> This is a **docs-only** page. It changes no runtime code, schema, routes, or
> packages. It records what is complete, what is intentionally deferred, and what
> is safe to start next, after the Operator State / Attention Panel phase and the
> Adversarial Reviewer Stage v1 design doc.

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

### Adversarial Reviewer Stage v1 — **design only**

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
  LLM-assisted memory, pgvector at scale).
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

The recommended immediate focus is **demo / README / devex readiness** before any
new product feature. The detailed options and the recommendation are in
[`../roadmap/next-phase.md`](../roadmap/next-phase.md). In short:

- **Safe now:** documentation, demo polish, public README, smoke-checklist upkeep,
  small honest stabilization fixes.
- **Needs an explicit decision first:** Adversarial Reviewer implementation
  (new feature), GitHub/PR robustness, production hardening, multi-LLM/provider
  modes, Memory M3.

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

DESIGN-ONLY (NOT IMPLEMENTED): Adversarial Reviewer Stage v1. Advisory/display-only
by design. No AI review runs today. Implementation deferred pending prioritization.

CURRENT NEXT RECOMMENDED TASK: demo / README / devex readiness BEFORE starting the
reviewer or any new product feature.

INVARIANTS (do not violate):
- Never bypass chunk plan approval or final approval. Final approval is NOT
  automatic.
- Never auto-expand scope or weaken scope_guard. Never auto-merge.
- Never claim Pipewright proves code correctness.
- The Adversarial Reviewer is design-only; do not claim it is live.
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
