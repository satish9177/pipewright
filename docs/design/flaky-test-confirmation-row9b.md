# Row 9b — Flaky-test confirmation + quarantine registry (design brief)

> **Status: DESIGN ONLY. No code.** Gated on decision **D9** (proposal §22/§24),
> which is **still open**. Row 9b is in the §23 backfill set (proposal Appendix:
> "9b needs D9"). This brief is the one-page design that must precede any
> implementation prompt. It changes no runtime code, schema, routes, policy, or
> packages.

Authoritative sources read for this brief: `PIPEWRIGHT_REDESIGN_PROPOSAL.md`
§18.1 (the Row 9b design), §4.2/§4.4 (verdict + baseline verification), §22/§24
(D9); `backend/pipeline/tester.py`, `test_run_validation.py`, `test_failure_ids.py`,
`stage_contract.py`, `chunk_driver.py`, `patch_failures.py`, `policy.py`,
`chunked_orchestrator.py` (baseline/disclosure helpers), `approval_gate.py`,
`test_validation_ack_store.py`; `docs/metrics/ledger-metrics-queries.md` §12
(Row 16b soak evidence).

---

## Verdict — proceed now or stay deferred?

**Design now (this brief). Defer implementation and activation.** Concretely:

1. **D9 is the user's call and is unmade.** Confirm-run count, `disclose_and_continue`
   vs `pause`, and human-confirmed quarantine are safety-semantics decisions. Per the
   safety contract we do not guess these. This brief *proposes* defaults; it does not
   assume them.
2. **There is no real-traffic evidence the problem exists yet.** The Row 16b soak
   (2026-06-18) found the dev DB synthetic/sparse with **zero `INFRA_ERROR`/recovery
   evidence** and recommended keeping every dormant flag OFF until real soak data
   exists. Building a mechanism whose core effect is *"a failing test no longer
   dead-ends the chunk"* for an unevidenced failure mode is exactly the trade the
   project has repeatedly declined.
3. **This is the highest-risk surface in the engine** — it redefines when a red test
   charges the chunk. The honest residual (proposal §18.1) is that a *real regression
   that is also intermittent can pass one confirmation re-run and ship undercharged*.

**Recommended path:** ratify D9 → build the **pure, dormant** machinery
(PR-A classifier + PR-B registry scaffold, `flaky_confirm_runs=0` default, no wiring
into the live verify loop) → soak on real traffic → only then wire and activate.
If the user wants forward motion before D9, PR-A and PR-B are safe because they are
provably no-ops on the gate. **Nothing past PR-B should ship until D9 is decided.**

I also recommend **two deliberate deviations from the proposal's stated defaults**,
both justified by "quality first, latency/cost secondary" (CLAUDE.md) and the Row 16b
reality check — see *Proposed policy*.

---

## 1. What problem is Row 9b solving?

Today the chunk gate is **baseline-aware** (proposal §4.4 / D1, shipped):

- `run_baseline_tests` records the run-start failing pytest node IDs.
- After a chunk applies, `tester._baseline_accepts_failed_result` treats a non-zero
  run as **passing** if execution integrity is `OK` and there are **no newly-failing
  IDs vs. baseline**. `newly_failing_test_ids = current − baseline`.
- A genuinely new red test → `verify_outcome_from_result` →
  `classify_test_failure` → `TEST_REGRESSION` → `CODE_REJECTED`, charged to the chunk,
  patch rolled back.

The gap: a test that is **green at baseline and intermittently red afterwards** is
charged as `CODE_REJECTED` — false blame, the precise failure mode the redesign
exists to kill. Row 9b adds a **confirmation re-run** before charging a newly-failing
test, and a durable **quarantine registry** for known repeat offenders, **without**
silencing real regressions.

## 2. Real regression vs. infra/harness vs. flaky vs. pre-existing baseline failure

These are already separable in code; Row 9b only fills the one missing class. Mapping
to existing classifiers (`classify_execution_integrity`, `classify_test_failure`,
baseline delta):

| Class | Signals (existing) | Disposition |
|---|---|---|
| **Pre-existing baseline failure** | failing ID ∈ baseline set | **Already handled (D1).** Never charged; disclosed as `pre_existing_failing_test_ids`. Untouched by 9b. |
| **Infra / harness error** | `ExecutionIntegrity != OK` (HARNESS_CRASH, COMMAND_NOT_FOUND, SIGNAL_KILL, COLLECTION_ERROR, NO_TESTS_RAN, TIMEOUT) | `HARNESS_ERROR` → `INFRA_ERROR`. Handled by the existing bounded auto-retry (`AUTO_RETRY_INFRA_BUDGET=1`, TIMEOUT excluded). **Never enters flaky confirmation.** |
| **Real regression** | newly-failing ID, integrity `OK`, **fails again** on confirmation re-run | `TEST_REGRESSION` → `CODE_REJECTED`, charged, "confirmed on re-run" in narrative. |
| **Flaky test** (the new class) | newly-failing ID, integrity `OK`, **passes** on confirmation re-run | `NONDETERMINISTIC_TESTS`: **not charged**, mandatory disclosure. |

The flaky class occupies a single precise gap: **integrity OK + newly-failing +
clears on re-run.** It is downstream of baseline acceptance and orthogonal to
`INFRA_ERROR`.

## 3. How should Pipewright confirm flakiness?

- **Trigger (narrow).** Only when the post-apply run has `ExecutionIntegrity.OK`,
  parseable failing node IDs, and a **non-empty `newly_failing_test_ids`** set. Any
  non-OK integrity, any `TIMEOUT`, or **non-comparable IDs (`parseable=False`) →
  no confirmation; fail safe to charge** (a possible infinite loop or unknowable
  failure set is never re-run / never excused).
- **Re-run command — recommend same-command full re-run for v1 (deviation).** The
  proposal defaults to *targeted re-run of only the failing tests* where the runner
  selection syntax is known (pytest node IDs), full-suite only as fallback. I
  **recommend inverting that default**: a **same-command full re-run** is the v1
  default; targeted re-run is an opt-in optimization behind its own flag. Reason: an
  isolated re-run of one node ID can **mask an order-dependent real regression**
  (passes alone, fails in suite) by mislabeling it flaky — a correctness loss, and
  CLAUDE.md says latency/cost must never be optimized at the expense of quality.
  Cost is bounded: the extra run fires **only on a real new-failure event**, never on
  the green path.
- **How many times — recommend `flaky_confirm_runs` default `0` (deviation).** The
  proposal suggests default 1. I recommend the first ship default **0 (disabled)**,
  matching the project's "ship dormant, activate on evidence" discipline; `1` (and
  higher, for noisier projects) is the activation value the user sets per D9. The cap
  is explicit policy, never buried.
- **Clearance rule (strict).** The chunk is cleared as nondeterministic **only if the
  confirmation re-run shows ZERO newly-failing tests.** If *any* test is newly-failing
  in the re-run — the same one, or a different one — the chunk is **charged**
  `CODE_REJECTED`. A test newly-failing in **both** runs is a confirmed regression.
  The `NONDETERMINISTIC_TESTS` disclosure names exactly the run-1 newly-failing IDs
  the re-run cleared.
- **Budget.** `flaky_confirm_runs` re-runs per new-failure event, single-sourced in
  the §4.7 policy module (`policy.py`). Distinct from `AUTO_RETRY_INFRA_BUDGET`;
  they must not share a counter.
- **When allowed.** Fresh, resume, human_retry, and steered passes may confirm.
  **`auto_retry` must not** (it is infra-only and re-verifies the same generation).
  Confirmation runs **before** the driver's rollback (see *Pipeline behavior* — this
  is load-bearing).

## 4. What is written to the attempt ledger?

The `chunk_attempts` ledger (`chunk_attempt_store.py`) is append-only audit metadata —
**ids/enums/flags/refs only; never output, diffs, prompts, or secrets** (the same
discipline as `PatchRecoveryAttempt`). For a flaky event record, **on the existing
`evidence_refs_json` / `stage_outcomes_json` evidence fields (no schema change for the
first slice)**:

- `flaky_observed=true` flag (mirrors how `integrity=…` is already carried as stage
  evidence);
- the **confirmed-nondeterministic node IDs** (identities only, no output);
- `flaky_confirm_runs_spent` and the re-run **selection mode** (`full_suite` / `targeted`);
- per suppressed ID, whether it was **quarantine-sourced** vs **newly-confirmed this run**.

Only if metrics later need to query it efficiently do we add a dedicated
`flaky_observed` column (proposal §18.2 sketches one) via the existing additive
`_migrate_db` pattern — not in the first slice.

## 5. What does the quarantine registry store?

A small per-project operational table, shaped like `scope_expansion_requests` /
`test_validation_acknowledgements` (**insert + bounded status transitions, audited,
never hard-deleted**):

| Column | Notes |
|---|---|
| `id` PK; `project_id` | per-project scope |
| `test_node_id` | normalized pytest node ID; the quarantine key |
| `status` | `pending_confirmation` → `active` → `removed` (bounded transitions) |
| `evidence_count` | times observed nondeterministic |
| `first_observed_at`, `last_observed_at` | |
| `source_attempt_ids` (refs) | links to `chunk_attempts` rows — refs only |
| `test_command` | metadata for audit (NOT part of the key) |
| `confirmed_by`, `confirmed_at` | human who promoted it to `active` |
| `note` | optional human note |

**No test output, no diffs, no stack traces, no secrets.** It is **operational state,
not memory** — it never enters the memory store, prompts, FTS, or vectors, and is
never an authority channel.

## 6. Quarantine keyed by project / test id / command / file / signature?

**Per `(project_id, test_node_id)`.** Rejected alternatives, with reasons:

- **Per command** — too coarse; a flake is not a property of the command.
- **Per file** — too coarse; quarantines siblings of the offender.
- **Per failure signature (error-text hash)** — fragile, and dangerous: it would let a
  *different* real failure of the same test slip through under the old signature.
  Explicitly **rejected for v1**; the node ID is the stable identity the baseline
  machinery already uses.

Quarantine requires **parseable node IDs**. When IDs are non-comparable you cannot
quarantine precisely → **fail safe: do not quarantine, do not suppress, charge
normally.**

## 7. Effect of quarantined / flaky tests on each surface

- **Chunk approval.** A confirmed-flaky or quarantined failure **does not charge** the
  chunk, so it may proceed to commit/approval — **but the disclosure travels with it**.
  It does **not** bypass the high-risk chunk gate (`requires_human_review`), and does
  **not** itself open a chunk gate unless `flaky_action=pause`.
- **Final approval.** The nondeterministic observation is surfaced at the final gate as
  a **mandatory disclosure** (same informed-gate pattern as #28F weak/none ack and the
  D1 pre-existing-failure disclosure). Under the recommended `disclose_and_continue` it
  is prominently shown but does not hard-block; the conservative D9 alternative is to
  require an explicit ack (reuse the `test_validation_ack` shape) or `pause`. It
  **never auto-satisfies** any existing precondition and **never bypasses** final
  approval.
- **Strong/weak/none/unknown verdict.** **Untouched and orthogonal.** `classify_test_run`
  still computes the display verdict from the final accepted run. Flaky suppression
  must **never upgrade** a WEAK/NONE verdict to STRONG, and the weak/none ack gate
  (`test_validation_ack_store`) is unchanged.
- **Advisory reviewer.** Receives the flaky observation as **display-only evidence**;
  it gates nothing, commits nothing, writes no memory — unchanged contract.
- **Run Detail safety summary.** Add a "N intermittent failure(s) observed (not
  charged)" line to the existing verification-disclosure surface; never render it as
  "all clear." Read-only.

## 8. What must STILL block approval?

- A **real regression** — newly-failing that fails (or is not cleared by) the
  confirmation re-run → `CODE_REJECTED`.
- **Infra / harness errors** (`INFRA_ERROR`), including **`TIMEOUT` (never re-run)**.
- **Non-comparable failing IDs** — fail safe, treat as a real failure.
- **Scope / forbidden-path / dirty-tree / policy blocks** — `scope_guard` remains the
  authority; flaky logic never touches these.
- The **weak/none test-validation ack** at final approval (#28F) — unchanged.
- **Final approval itself** — never bypassed.

## 9. What must NEVER be auto-approved / auto-acted?

- A confirmed-flaky or quarantined chunk **never auto-approves** the chunk or the run —
  every existing human gate still stands.
- **Quarantine entries are never auto-added.** The system *observes* (writes a
  `pending_confirmation` row + hygiene-digest surfacing); a **human confirms** before
  any suppression takes effect — "system detects, human decides."
- A **real regression is never silently reclassified** as flaky.
- No auto-merge, no auto-PR, no Git/branch changes — out of scope entirely.

## 10. Safest first implementation slice (after D9)

Slice order, each additive and default-off, gate-impact only when explicitly activated:

- **PR-A — pure flaky-confirmation classifier + policy knobs.** Given run-1
  newly-failing IDs, integrity, and a re-run result, decide
  `CODE_REJECTED` vs `NONDETERMINISTIC_TESTS` (strict clearance rule, fail-safe on
  non-comparable / non-OK / TIMEOUT). Adds `flaky_confirm_runs` (default **0**),
  `flaky_action` (default `disclose_and_continue`), `flaky_targeted_rerun` (default
  **off**) to `policy.py`. **No wiring.** This is the safest possible first slice — a
  provable no-op, mirroring how Signal C and baseline verification shipped pure first.
- **PR-B — quarantine registry table + store + human-confirm route.** Insert + bounded
  status transitions; read helpers; **not yet consulted** by verify. Dormant.
- **PR-C — wire confirmation re-run into the verify/tester step** behind
  `flaky_confirm_runs>0`, **before rollback**; write the disclosure into the completion
  summary and the ledger evidence. Still a no-op at default 0.
- **PR-D — quarantine suppression** in verify (consult `active` entries;
  suppress-and-disclose, skip the confirmation re-run for known IDs) + hygiene-digest
  surfacing of `pending_confirmation` observations.
- **PR-E — Run Detail safety summary + final-gate disclosure** (read-only).
- **PR-F (optional) — targeted re-run optimization** behind `flaky_targeted_rerun`.

**Stop after PR-B until D9 is decided and real flaky evidence exists.**

## 11. Tests required

- **Pure classifier:** newly-failing → re-run clears → `NONDETERMINISTIC`; re-run
  fails same → `CODE_REJECTED`; re-run fails *different* → `CODE_REJECTED`; integrity
  non-OK never enters; `TIMEOUT` never re-run; non-comparable IDs → fail-safe charge;
  `flaky_confirm_runs=0` → byte-identical to today (flag-off no-op proof).
- **Runner-selection derivation:** pytest targeted-selection derivation + fallback to
  full-suite (when 9f-F ships); default full-suite path.
- **Registry:** CRUD; `pending → active → removed` transitions; quarantined ID
  suppressed-not-charged; quarantine **never auto-added**; non-parseable ID never
  quarantined.
- **Disclosure:** completion summary carries the nondeterministic disclosure; final
  gate surfaces it; it never auto-satisfies the #28F ack; weak/none verdict unchanged.
- **Ledger evidence:** `flaky_observed` evidence recorded; **no output/diff/secret
  leakage** (assert the redaction discipline).
- **Rollback ordering:** confirmation re-run happens **while the patch is still
  applied, before** `run_tests_with_rollback` rolls back; no double rollback;
  resume fail-closed preserved.
- **§18.6 failure-injection matrix:** gains a **flaky row per entry mode**
  (fresh/resume/auto_retry/human_retry/steered) — including the invariant that
  `auto_retry` never confirms.

## 12. Must-not-touch areas

- `scope_guard`, `path_safety` — write-safety authority.
- `approval_gate` decision logic; the **final-approval gate is never bypassed**.
- The **exit-code-based pass/fail for the non-flaky path** — normal runs unchanged.
- The **D1 baseline-acceptance internals** (`_baseline_accepts_failed_result`,
  `_verification_disclosure_from_result`): consume `newly_failing_test_ids`, do not
  rewrite them.
- The **`INFRA_ERROR` auto-retry budget/loop** — flaky confirmation is a separate
  mechanism with its own budget; do not fold them together.
- `classify_test_run` STRONG/WEAK/NONE/UNKNOWN orthogonality; the #28F weak/none ack.
- **Memory** store / injection / FTS (Row 19) / vector (Row 23) — the registry is
  operational state, not memory.
- **Git / PR / branch base / auto-merge.**
- `SCOPED_VERIFICATION_ENABLED` (the separate §5.1 scoped-verification knob) and any
  other dormant flag.
- The **Phase 2F/2G thread UI** beyond the read-only disclosure line.

---

## Proposed policy (recommended D9 answers — pending user ratification)

| Knob (`policy.py`, §4.7) | Recommended default | Proposal's stated default | Note |
|---|---|---|---|
| `flaky_confirm_runs` | **0 (disabled)** | 1 | Ship dormant; activate on real evidence. |
| Re-run mode | **same-command full re-run** | targeted-first | Targeted re-run can mask order-dependent regressions. |
| `flaky_targeted_rerun` | **off** | n/a (implied default) | Opt-in optimization, separate flag. |
| `flaky_action` | **`disclose_and_continue`** | `disclose_and_continue` (vs `pause`) | `pause` is the conservative alternative. |
| Quarantine additions | **human-confirmed only** | human-confirmed | Never auto-added. |
| Quarantine list size cap + staleness review | **explicit cap; surfaced in hygiene digest** | implied | No buried limit; reviewable/expirable. |

The two deviations (default `0`, full-suite-first) are deliberate and consistent with
"quality first; ship dormant; activate on evidence." The proposal's defaults were
written before the Row 16b soak; the user can ratify either set under D9.

## Data model / registry shape

See Q5/Q6: one per-project `known_flaky_tests`-style table keyed on
`(project_id, test_node_id)`, insert + bounded status transitions, refs-only, no
output/secrets, never memory. Ledger evidence (Q4) carried on existing
`chunk_attempts` evidence fields first; dedicated column only if metrics demand it.

## Pipeline behavior (the load-bearing constraint)

`run_tests_with_rollback` (`chunk_driver.py`) rolls back **whenever
`not test_result.passed`.** Therefore the confirmation re-run **must execute inside
the verify/tester step, while the patch is still applied, before that rollback
decision**, so `test_result.passed` already reflects the post-confirmation verdict.
Do **not** roll back between the two runs and do **not** re-apply. This keeps the T2
rollback contract (single owner, post-rollback clean-tree report) intact.

## UI / read-model impact

Read-only: a verification-disclosure line in the Run Detail safety summary and at the
final-approval gate ("N intermittent failure(s) observed — not charged; <node ids>").
No new actions, no gate wiring, no event persistence (Phase 2F PR-5 stays deferred).

## Safe PR split

PR-A (pure classifier + dormant policy) → PR-B (registry scaffold + human-confirm
route) → **[hold for D9 + evidence]** → PR-C (wire confirm re-run, default-0 no-op) →
PR-D (quarantine suppression + digest) → PR-E (read-only UI/disclosure) → PR-F
(optional targeted re-run). No PR mixes areas; each additive and default-off.

## Validation plan

The Q11 unit suite; the §18.6 matrix flaky row per entry mode; a manual smoke with a
deliberately intermittent test (confirm: cleared-on-rerun → disclosed-not-charged;
fails-on-rerun → charged); **flag-off byte-identical proof** (`flaky_confirm_runs=0`);
`ruff check`; frontend `npm.cmd run build` for the UI slice. Known live-model suite
failures, if any, called out separately from the targeted/unit proof.
