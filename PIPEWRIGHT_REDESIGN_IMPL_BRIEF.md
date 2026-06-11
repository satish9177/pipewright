# Pipewright Redesign — Implementation Handoff Brief (rolling, per-phase)

**Date:** 2026-06-12
**What this file is:** the **rolling** implementation handoff brief for the redesign. Each phase's active spec lives here; when a phase lands, this file is repurposed for the next one. **Current contents: Phase 3, slice 1 (item 13 — `steered` attempts on failed chunks).** The previous occupant (Phase 2, items 10–12 + P7) landed and was reviewed & accepted 2026-06-12; its outcome is recorded in `PIPEWRIGHT_REDESIGN_WORKPLAN.md`.
**For:** the model implementing Phase 3 item 13. Re-verify every `file:line` against the live code before you cite or edit it; the Phase-2 driver work moved line numbers and the repo keeps moving.
**Source of record:** `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (§3 Candidate B, §4.1 entry modes, §4.3 continuation, §4.7 policy, §5.3 decision, §6 phasing, §18.6 entry-mode matrix) and `PIPEWRIGHT_REDESIGN_WORKPLAN.md`. This brief operationalizes them; if they disagree, the proposal wins and you flag the drift.
**Mode:** design + implement + test, **one item per PR.** This slice is **item 13 only.** Item 14 (post-success refinement) is **deferred** by decision (see §0). Do **not** start item 14, Phase 4, or any reviewer-ack / narrative-read-model work.

---

## 0. The boundary you are standing on (read before anything else)

Phase 2 collapsed the three hand-maintained copies of the stage sequence into **one driver** (`backend/pipeline/chunk_driver.py`) with typed entry modes. It left a deliberate, named seam: `EntryMode.STEERED` exists in the enum and `drive_chunk` raises `NotImplementedError` for it. **Phase 3 item 13 fills exactly that seam — and nothing else.** This is the first time the redesign adds a *new user-facing execution capability* rather than relocating an existing one, so the safety surface is real: you are letting human free-text drive a new code-generation attempt. The whole point of building the driver first was that this capability plugs in **without reshaping execution** — if you find yourself rewriting the driver loop, stop; you are off the rails.

Three rules are non-negotiable:

1. **One PR: item 13.** Turn log + `steered` mode for **failed** chunks + budgets in policy. Nothing past that line.
2. **A human reviews this PR before any Phase-3 follow-on (item 14) or Phase 4 begins.** You own the tests; you do not grade your own homework. Capture parity snapshots of today's `human_retry` behavior *before* you touch the retry path, so "I preserved it" is provable against a pre-change baseline, not against whatever the new code emits.
3. **`steered` is `human_retry` + a steer string + a continuation-context block — not a new pipeline.** Same approved plan, same `files_expected`, same gates, same rollback, same budget accounting. The steer is **advisory context inside the approved chunk**, never an authority channel.

### Decisions already made (do not re-litigate)

- **§5.1** (regression-vs-baseline gate + scoped option default-OFF) and **§5.2** (auto-retry `INFRA_ERROR` budget = 1) — DECIDED 2026-06-11, implemented in Phase 1, relocated into the driver in Phase 2. Unchanged here.
- **§5.3 (steer-without-replan) — DECIDED 2026-06-12: accept steered attempts inside unchanged approved scope, the CONSERVATIVE variant.** A steer may re-run code generation without re-approving the whole plan **only** while it stays within the approved chunk and approved `files_expected`. If the steer text *appears to mention a file/path outside the effective approved scope*, **do not silently proceed** — pause for explicit human re-confirmation, or route to the existing scope-expansion approval flow (#27). `scope_guard` remains the hard authority at apply either way. Free text is advisory only: it never grants scope, never approves a gate, never bypasses chunk/final approval. (Implementation detail in §13.)
- **Item 14 (post-success refinement: steering a *completed* chunk → new commit + cumulative final diff) — DEFERRED one slice by decision 2026-06-12.** It touches the final-approval invariants and should soak behind failure-steering first. **Out of scope for this PR.** It becomes the next occupant of this rolling brief.
- **§5.4 (reviewer ack gate)** is **still PENDING** and gates **Phase 4** — not this slice. Do **not** implement the ack soft-gate or the "steer this" review hook here.

## 1. Non-negotiable safety contract (from `CLAUDE.md`)

No change may weaken these (enforced in `scope_guard`, the approval gates, `patch_applier`, `path_safety`):

1. No implementation without an approved chunk plan; never bypass chunk-plan or final-approval gates. **A steered attempt runs the *same already-approved plan* — it does not re-open or replace the plan-approval artifact.**
2. Never edit outside approved `files_expected`; `scope_guard` is the authority — the steer **requests** intent, it never **grants** scope. The conservative §5.3 re-confirm is an *additional* gate in front of `scope_guard`, never a replacement for it.
3. Never create empty / no-effective-change commits; never push zero-commit branches. The no-op-commit guard (`_commit_and_complete_chunk`) still fires for steered attempts.
4. Never open PRs against `main`/`master`/`develop`; never auto-merge.
5. Never write forbidden paths (`.env`, `.git/`, secrets, keys).
6. Never expose or persist secrets/tokens/PII; sanitize provider/Git errors. **The turn log stores user steer text and metadata only — never diffs, test output, stack traces, provider errors, or secrets** (same discipline as the `chunk_attempts` ledger).
7. Memory is advisory; source code, user instruction, tests, and safety rules win on conflict. **The steer and the turn log are advisory too (contract §2.7 analog): a narrative/steer never touches scope, approval, Git, or merge decisions.**
8. AI-suggested memory stays pending until a human approves.
9. Prefer failing safely with a clear, specific error over guessing. At budget exhaustion: terminal with a clear narrative, never a silent extra attempt.

This PR is high-risk because it is the first **inbound free-text execution path**. Carry an explicit safety-contract check (§13.5). When in doubt, fail safe with a clear error and keep today's behavior.

## 2. The foundation Phase 2 left you (do not re-do; build on)

- **`backend/pipeline/chunk_driver.py`** is the single execution engine. `EntryMode` (`:77`) already has `STEERED = "steered"` (`:86`); `drive_chunk` (`~:690`) raises `NotImplementedError` for it (`:723`). The driver already enforces, *identically in every mode*: dependency + dirty-tree preconditions, scope pre-check before any write, dry-run before apply, no commit without effective change, **rollback to clean tree on any failed attempt**, verdict persistence for pass *and* fail. **`steered` inherits all of this for free** — your job is the entry-mode dispatch + the continuation context, not the invariants.
- **`human_retry` is already a driver entry mode.** `_retry_failed_chunk_locked` (`chunked_orchestrator.py:~2598`) calls `chunk_driver.drive_chunk(EntryMode.HUMAN_RETRY, …)` (`:~2673`); `retry_failed_chunk` (`:~2690`) is the lock-acquiring entry point; `_retry_plan_for_chunk` (`:~2320`) builds the retry handoff. **`steered` is `human_retry` with a steer string threaded into the continuation context.** Treat `human_retry` as the steer-less special case (proposal §4.3) — reuse its eligibility, locking, and budget plumbing; do not fork it.
- **Eligibility lives in `patch_failures.py`, never in the outcome class.** `MAX_HUMAN_RETRIES = 2` (`:45`), `count_human_retry_attempts` (`:589`), `evaluate_patch_retry_eligibility` (`:611`), and the retryability frozensets are the authority. `retry_with_instruction` is *already named*: `ACTION_RETRY_WITH_INSTRUCTION` (`:123`), `_RETRY_WITH_INSTRUCTION_TYPES` (`:174`), surfaced to the UI in failure reports (`:430`) — **but it has no execution path anywhere** (this is the half-built G1 primitive the proposal §1 names). Item 13 is what finally wires it.
- **The attempt ledger exists.** `chunk_attempts` (`schema.sql:205`) is append-only, one row per driver pass, with `entry_mode` (`:211`) already admitting `"steered"`, plus `stage_outcomes_json`, `final_outcome_class`, `final_status`, `head_sha`. Written via `chunk_attempt_store.record_chunk_attempt`; migrated by the guarded `_ensure_chunk_attempts_shape` in `database.py`. **The ledger records *that* a steered attempt happened; the turn log (new, §13) records the *steer text and conversation*.** Do not put steer free-text in `chunk_attempts` (it is metadata-only); the turn log is its home.
- **`backend/pipeline/policy.py`** is the single source for behavioral constants (currently `AUTO_RETRY_INFRA_BUDGET = 1` at `:29`, plus the Phase-0/1 caps). **The combined human+steered per-chunk attempt budget and any per-run ceiling go here** — see §13 trap (e). `MAX_HUMAN_RETRIES` currently still lives in `patch_failures.py:45`; relocating/generalizing it is part of this item (one source of truth).
- **`backend/pipeline/file_scope_intent.py`** already extracts grounded file mentions from free text: `extract_user_file_constraints(...)` (`:234`) → `UserFileConstraints`. **Reuse it for the conservative §5.3 steer-text check — do not write a second path-detection heuristic** (a parallel parser is exactly the E9-class over-fire bug the proposal called out). If its shape doesn't fit steer prose, *extend it with tests*, don't fork it.
- **`backend/pipeline/scope_expansion.py` + the `scope_expansion_requests` table** (`schema.sql:229`) are the audited #27 home for human-approved scope amendments. *Effective scope* = original `files_expected` ∪ approved-/applied-row `approved_files`. The conservative §5.3 check compares the steer's mentioned paths against **effective** scope, and routes an out-of-scope mention to **this** flow — never to a new ad-hoc grant.

---

# ITEM 13 — `steered` attempts on failed chunks (the real `retry_with_instruction`) + turn log + budgets in policy

## 13.1 Summary & scope

Fill the `EntryMode.STEERED` seam so a human can steer a **failed** chunk with a short free-text message and the driver re-attempts — carrying forward the approved plan, the prior coder handoff, the prior applied diff *as text*, and the classified failure evidence, plus the steer — **without a new run and without bypassing any gate.** This is the missing `retry_with_instruction` execution path, finally wired to the driver instead of grafted onto a deleted copy.

**The `steered` entry mode (proposal §4.1, §4.3):**
- Re-runs from the **`code` stage** with the **same approved plan and same `files_expected`** (effective scope). It is `human_retry` plus a steer string in the continuation context.
- **Continuation context block** = approved plan + prior coder handoff + prior applied-diff (as text) + classified failure evidence + the steer text. Assembled as *context only*.
- **The tree is always rolled back clean between attempts** (the driver already guarantees this). The prior diff travels as **context, never as standing working-tree state** — this deliberately costs some regeneration fidelity to preserve the clean-tree precondition and the repo-lock's meaning (rejected alternative recorded in proposal §5).
- **Conservative §5.3 gate (DECIDED):** before running, extract grounded path mentions from the steer text via `file_scope_intent`. If any mentioned path is **outside effective approved scope**, do **not** run — return a clear `NEEDS_HUMAN` narrative offering (a) explicit re-confirm to proceed inside current scope, or (b) the existing scope-expansion approval flow (#27). In-scope steers proceed. `scope_guard` still enforces at apply regardless.
- **Eligibility is unchanged and derived from `patch_failures.py` frozensets, never the coarse outcome class.** A steer is offered exactly where `retry_with_instruction` is already advertised — `_RETRY_WITH_INSTRUCTION_TYPES` (which includes `TEST_REGRESSION`/`TEST_FAILURE_AFTER_APPLY` and `SCOPE_VIOLATION` as *steerable*). Preserve today's deliberate exclusions: deterministic non-retryable failures stay non-retryable; `HARNESS_ERROR` stays auto-retry's domain.
- **Budget:** steered attempts share the per-chunk human budget. Generalize `MAX_HUMAN_RETRIES` into a single combined `human_retry + steered` per-chunk budget **single-sourced in `policy.py`** (carry today's value of 2 unless the maintainer sets the proposal's suggested 5 — flag the number, don't bury it). Auto attempts (`recovery_mode="auto"`) stay excluded from the human count, exactly as `count_human_retry_attempts` already does. At budget exhaustion: terminal with a narrative — fail safe.

**The turn log (new, additive — proposal §4.3):** a run gains an append-only conversation record: `user steer message → targeted chunk → resulting attempt → outcome`. The original `feature_description` stays **immutable** (the audit anchor); turns are additive context. Follow the `chunk_attempts` additive pattern exactly: `CREATE TABLE IF NOT EXISTS` in `schema.sql` + a guarded idempotent block in `database.py`. **Metadata + user steer text only** — never diffs, test output, provider/Git errors, or secrets. Link each turn to its `chunk_attempts` row (the attempt is the ledger's job; the turn is the message's).

**In scope (exact files):**
- `backend/pipeline/chunk_driver.py`: implement `EntryMode.STEERED` dispatch (delete the `NotImplementedError` branch at `:723`); thread the steer + continuation context into the `code`-stage re-entry; keep `human_retry`'s preconditions and the budget check as shared code.
- `backend/pipeline/chunked_orchestrator.py`: a `_steer_failed_chunk_locked` sibling of `_retry_failed_chunk_locked` (or a `steer` param on the existing path) that builds the continuation context and calls `drive_chunk(EntryMode.STEERED, …)`; reuse `_retry_plan_for_chunk` for the (unchanged) plan.
- `backend/pipeline/file_scope_intent.py`: reuse/extend `extract_user_file_constraints` for the conservative steer-text scope check (with tests if extended).
- `backend/pipeline/policy.py`: the combined human+steered per-chunk budget (relocated from `patch_failures.py:45`); optional per-run token/turn-length ceiling.
- `backend/pipeline/patch_failures.py`: have eligibility read the policy budget (one source of truth); preserve frozensets and report shape verbatim for audit continuity.
- `backend/db/schema.sql` + `backend/db/database.py`: the additive, append-only turn-log table + guarded migration.
- A new turn-log store module mirroring `chunk_attempt_store` (append-only writer + reader).
- `backend/routes/chunks.py`: the steer endpoint (the `retry_with_instruction` action the UI already advertises but the backend cannot perform). **Note:** `retry_failed_chunk` itself has *no public route yet* (`chunked_orchestrator.py:~2690` comment "#26D3"); if the human-retry route is still unwired, wire the steer endpoint cleanly rather than assuming a sibling exists.

**Explicitly out of scope (name these in your PR):**
- **Item 14: steering a *completed* chunk** (post-success refinement / "wrong sentence" → new commit + cumulative final diff). Deferred by decision. The turn log you build should *accommodate* it later, but item 13 only services `chunk_status == "failed"` chunks.
- **Re-running from `plan`** when "the steer contradicts the plan" (the parenthetical in proposal §4.1's table). Determining contradiction is an LLM judgment and a brittle classifier; item 13 **always re-runs from `code`** with the same approved plan. A steer that genuinely needs a different plan stays on today's reject-and-new-run path. Name this as a deliberate non-goal.
- The reviewer ack soft-gate and the "steer this" one-click review hook (Phase 4, §5.4 pending).
- The phase/narrative read-model and any conversation **UI** beyond the minimal endpoint + turn persistence (Phase 4). Item 13 is backend turn primitive + execution, not the chat UI.
- Any rename of a DB status string or the `PatchFailureType` taxonomy — never.

## 13.2 Verified current behavior (re-confirm before editing)

- `chunk_driver.py`: `EntryMode.STEERED` is the only `NotImplementedError` mode; `AUTO_RETRY` is internal-only (raises `ValueError` if entered directly); `human_retry` requires `retry_report` + `project_runtime`. The driver's failed-attempt rollback and clean-tree guarantee already hold for every mode — **confirm by reading the loop, do not assume.**
- `patch_failures.py`: `_RETRY_WITH_INSTRUCTION_TYPES` (`:174`) is the *advertised-but-unexecuted* steer eligibility set; `MAX_HUMAN_RETRIES = 2` (`:45`); `count_human_retry_attempts` (`:589`) already excludes `recovery_mode="auto"`; `evaluate_patch_retry_eligibility` (`:611`) is the authority. Report shape persisted in `completion_summary` must still round-trip.
- `chunk_attempts` ledger (`schema.sql:205`) already admits `entry_mode="steered"`; one append-only row per driver pass; metadata-only. **No turn-log table exists** (grep is empty) — you are adding it.
- `scope_expansion_requests` (`schema.sql:229`) + `scope_expansion.py` are the #27 flow; effective scope = original ∪ approved. `_approve_and_retry_scope_expansion_locked` already re-drives via the driver — model the steer's out-of-scope route on it.
- `file_scope_intent.extract_user_file_constraints` (`:234`) returns grounded path mentions + `uncertain_mentions`; reconciliation already distinguishes grounded from uncertain. Use grounded mentions for the hard re-confirm; treat uncertain ones conservatively (prefer re-confirm — see trap (b)).

## 13.3 Tests that must exist and pass

- **`human_retry` parity (capture snapshots first):** a `human_retry` through the driver produces the **same** status/commit/gate/report/ledger row as before this PR. Prove `steered` did not perturb the steer-less path.
- **Steered success + steered failure:** a failed chunk steered with an in-scope steer re-runs from `code`, applies, verifies, and either commits (success) or rolls back clean (failure) — assert tree clean after a failed steered attempt, exactly one rollback, one append-only `chunk_attempts` row with `entry_mode="steered"`, and one turn-log row linked to it.
- **Continuation context is carried, not the working tree:** assert the prior diff reaches the coder as *context* and the working tree is clean at steered entry (the diff is never standing tree state).
- **Conservative §5.3 — the decided behavior:**
  - In-scope steer → proceeds, no re-confirm.
  - Steer mentioning a path **outside effective scope** → **does not run**; returns `NEEDS_HUMAN` with the re-confirm / scope-expansion offer; `scope_guard` never had to fire because nothing was attempted.
  - Steer mentioning a path **already in effective scope via an approved expansion** → proceeds (compares against *effective*, not original, scope — trap (c)).
  - Explicit re-confirm after an out-of-scope steer → proceeds; touching the new file still routes through #27 at apply (`scope_guard` backstop intact).
- **Eligibility preserved:** steerable exactly where `_RETRY_WITH_INSTRUCTION_TYPES` says; deterministic non-retryables still rejected; `SCOPE_VIOLATION` steerable but `scope_guard` still authoritative; `HARNESS_ERROR` remains auto-retry's domain.
- **Budget (combined, in policy):** human + steered attempts share one per-chunk budget; at exhaustion the next steer is refused with a terminal narrative; auto attempts never consume it. Assert the count is read from `policy`, not a buried literal.
- **Turn log additive + append-only:** migration idempotent on a pre-PR DB (no data loss); every steer writes exactly one append-only turn row with the steer text + chunk + linked attempt; historical rows never mutated; `feature_description` never updated.
- **Gates unchanged:** a steered attempt on a `requires_human_review` chunk pauses at the same chunk gate; final approval is still required.

## 13.4 Traps

- **(a) Forking `human_retry` instead of generalizing it.** `steered` = `human_retry` + steer + continuation context. If you copy-paste the retry path you have re-created the very duplication Phase 2 deleted. Share the code; the mode differs only in the carried context and the §5.3 pre-check.
- **(b) The conservative §5.3 check over-firing *or* under-firing.** Reuse `file_scope_intent`, don't invent a parser (E9 lesson). Design it **fail-safe**: when path extraction is *uncertain*, prefer re-confirm over silent proceed — a false-positive re-confirm is mere friction, while `scope_guard` at apply is the real backstop for a false-negative. Never let the steer-text scan *replace* `scope_guard`; it is an *earlier, softer* gate in front of it.
- **(c) Comparing the steer against *original* `files_expected` instead of *effective* scope.** If a prior #27 expansion already widened scope, the check must use original ∪ approved-expansion files, or it will re-confirm on paths the human already approved. Pull effective scope the same way the apply path does.
- **(d) Putting steer free-text in `chunk_attempts`.** That table is metadata-only by design (no prompts/diffs/free-text). Steer text lives in the **turn log**; the ledger row just records that a steered attempt ran and its outcome.
- **(e) A budget literal that drifts.** Don't leave `MAX_HUMAN_RETRIES` in `patch_failures.py` *and* add a steered budget elsewhere — single-source the combined budget in `policy.py` and have eligibility read it. The exact number (keep 2, or adopt the proposal's 5) is a maintainer choice — surface it, don't bury it.
- **(f) Treating the prior diff as standing tree state.** The tree is rolled back clean between attempts; the diff is *context text*. Re-applying it as working state would break the clean-tree precondition and the rollback semantics — the exact safety property Phase 2 centralized.
- **(g) A non-additive turn-log migration.** `CREATE TABLE IF NOT EXISTS` + guarded `ALTER`; never `DROP`/rewrite. Append-only; never mutate a historical turn. A failed migration on a live DB is a data-loss incident.

## 13.5 Safety-contract check (item 13)

- **§2.1 (approved plan):** a steered attempt runs the **same already-approved plan** through the same chunk/final gates — the plan-approval artifact is not re-opened or replaced. State this and prove the gates still fire.
- **§2.2 (scope):** `scope_guard` is **unchanged** and remains the authority; the conservative §5.3 re-confirm is an *additional* fail-safe gate in front of it, not a substitute. The steer requests intent; it never grants scope. Out-of-scope intent routes only through #27. Prove the pre-write scope check fires identically and that an out-of-scope steer cannot write.
- **§2.3 (no no-op commit) + rollback:** unchanged — the driver's no-op guard and clean-tree rollback already cover `steered`. Prove tree-clean + single-rollback after a failed steered attempt.
- **§2.6 / §2.7 (no secrets/PII; advisory-only):** the turn log stores steer text + metadata only (never diffs/output/provider errors/secrets), mirroring the ledger's discipline; the steer and turn log are advisory and never an authority channel for scope/approval/Git/merge.
- **§2.9 (fail safe):** budget exhaustion and out-of-scope steers end with a clear, specific narrative — never a silent extra attempt and never a guess. If you cannot prove the §5.3 gate blocks an out-of-scope steer before any write, **stop and escalate** rather than ship.

---

## 3. Update these docs when you finish (part of "done")

1. **`PIPEWRIGHT_REDESIGN_WORKPLAN.md`** — the canonical resume point. When item 13 lands + tests pass: mark it done in the Phase 3 sequence and the "How to resume" steps; update the TL;DR "Where we are" bullet. State plainly that **item 14 (post-success refinement) is the deferred next slice** and **gates on nothing further** (§5.3 is now decided), and that **Phase 4 still needs the §5.4 decision** before it starts.
2. **This file (`PIPEWRIGHT_REDESIGN_IMPL_BRIEF.md`)** is the rolling brief: once item 13 is done and reviewed, repurpose it for **item 14** (post-success refinement + cumulative final diff), the same way it was repurposed from Phase 2 to here.
3. **A short per-item spec/changelog note** if you follow the repo's `specs/` convention, mirroring the earlier items. Optional but matches the pattern.
4. These planning docs are **untracked**. Update their content; **do not commit** them (or any code) unless the maintainer asks. If asked to commit: branch off `develop` first (never straight to `develop`/`main`), one item per commit, end the message with the repo's `Co-Authored-By` trailer.

## 4. Working discipline (this PR)

- Read the real code first; trace the actual path; if the live code has drifted from this brief, correct the brief's pointer and say so.
- **Capture parity snapshots of today's `human_retry` behavior before you touch the retry path.** A "steered didn't break human_retry" claim is only provable against a pre-change baseline.
- Smallest correct change; **item 13 only**; list what you deliberately did **not** change (item 14, plan-rerun branch, reviewer ack, chat UI).
- Match surrounding naming, structure, and comment density (the driver, the ledger store, and the scope-expansion flow are your templates).
- Tests assert the **decided behavior** (conservative §5.3, combined budget, append-only turn log) and the safety guards above — not just that code runs.
- Report on completion: changed files, tests run + results, manual validation, risks, and what was intentionally left untouched.
- **A human reviews this PR before item 14 or Phase 4 begins.** Do not let momentum carry the loop into the deferred item or into the §5.4-gated Phase 4.
