# Adversarial Reviewer Stage Design (v1)

> Status: **Design only.** This document defines the design for an **advisory,
> display-only AI review** of a chunk's actual applied diff and test evidence,
> produced after tests pass and before the human reviews the result. **No backend
> code, frontend code, route, model, migration, schema change, package change, or
> runtime behavior ships with this document.** Implementation is phased after this
> doc (see *Safe Future Implementation Slices*).
>
> This is strictly **additive and orthogonal** to the existing recovery/validation
> work — Patch Failure Recovery v2 (#26), Scope Expansion Recovery (#27), and
> Stronger Runtime Test Validation (#28) — and to the Operator State / Attention
> Panel. The reviewer stage **does not replace or weaken any existing safety,
> scope, validation, or approval behavior.** The review is **evidence for the
> human, not authority.**
>
> **Priority note.** This design doc is safe to write now. **Implementation must be
> explicitly re-prioritized against the current demo / README / devex readiness
> phase before any code slice ships.** The reviewer stage is a new product feature,
> not UI polish, and it strengthens no existing guarantee on its own — it adds an
> LLM call, a storage surface, and a sycophancy/hallucination risk in exchange for
> advisory commentary. Treat the build as off the current phase's critical path
> until that re-prioritization happens.

---

## 1. Purpose and Non-Goals

### Purpose

After a chunk's code change is applied and its tests have run, Pipewright already
has the strongest evidence it will ever have about that change: the **actual
standing diff**, the **runtime test verdict** (#28), and any **recovery context**
(#26 retry / #27 scope expansion). Today that evidence is surfaced to the human as
raw material. The Adversarial Reviewer Stage adds one more piece of evidence: a
**structured AI review of that same diff and test evidence**, produced *before* the
human reviews the result, so the human starts from an adversarial second opinion
rather than a blank diff.

The reviewer is deliberately adversarial in intent — its job is to look for
correctness gaps, test gaps, scope concerns, security/safety concerns, and
requirement mismatches — but it has **no authority**. It informs the human; it
never decides.

### v1 scope (explicit)

- v1 is **per chunk**. One review per chunk, bound to that chunk's applied diff.
- v1 is **advisory-only**. The review is a recommendation, never a decision.
- v1 is **display-only**. It is surfaced through the read model and UI; it drives
  no action.
- v1 **gates nothing**. It does not block chunk approval, final approval, commit,
  or PR.
- v1 is **additive** to the existing safety gates (#26/#27/#28, chunk approval,
  final approval). Those remain authoritative and unchanged.

### Non-goals (explicit, v1)

The reviewer in v1 must **not**:

- **auto-reject** a chunk or run;
- **auto-fix** / propose-and-apply any change;
- **auto-approve** anything;
- **block final approval** (or chunk approval, or commit, or PR);
- **commit, push, or create PRs**;
- **mutate files** in the target repo;
- **write project memory** from its findings;
- **weaken #26/#27/#28** or any approval/scope/validation gate.

The following are **out of scope for this entire stage** (not just v1's first
slice):

- **No reviewer acknowledgement gate in v1.** A future, optional gate may copy the
  #28-style diff-hash-bound precondition, but only after smoke validation and only
  as a separate behavior-changing slice.
- **No memory writes from reviewer findings.**
- **No PR comments / GitHub interaction.**
- **No durable audit system** built as part of this stage.
- **No multi-model routing UI.** Model selection reuses the existing role-based LLM
  config (see §12); no new UI is introduced here.

---

## 2. Pipeline Placement

### Current flow

```
feature request
  → chunk plan approval
  → execute chunk
  → code/patch
  → tests
  → chunk approval
  → final approval
  → PR creation
```

### Proposed flow (where the reviewer fits)

```
plan → code → patch → test → [AI review] → chunk approval → final approval → PR
```

### Recommended placement

Run the reviewer **after patch apply and successful test execution, after the
runtime test verdict is persisted, and before chunk-approval / chunk-completion
evidence is surfaced** to the human.

Concretely, in the existing chunk execution path
([`backend/pipeline/chunked_orchestrator.py`](../../backend/pipeline/chunked_orchestrator.py),
`_execute_single_chunk`), this is the point **after** the runtime verdict is
recorded (`_persist_test_run_verdict`) and **before** the
`requires_human_review` / commit branch (`_pause_for_chunk_approval` /
`_commit_and_complete_chunk`).

### Placement rules

- **Run the reviewer only on standing applied changes.** The review must describe a
  diff that actually exists on disk right now.
- **Only run on passing tests.** On test failure the patch is rolled back (the
  tester attempts rollback, and the orchestrator reports a
  `TEST_FAILURE_AFTER_APPLY` failure). There is no standing diff to review.
- **Never review rolled-back changes as if they still exist.** A review of a change
  that no longer exists is worse than no review — it manufactures false context.
- **The reviewer result is best-effort evidence, not a gate.** Its success or
  failure must not influence whether the chunk proceeds (see §6).

### Lock-hold caveat (be honest)

The recommended placement is **inside the existing project/repo lock**
(`project_repo_lock`, held across the locked execution path). Putting a network LLM
call there **increases lock-hold time** for the duration of the review.

- For local, single-operator self-use this is primarily "the operator waits a few
  extra seconds per chunk," not a contention bug — there is normally one run at a
  time per project.
- v1 may accept the in-lock placement **only** with (a) a **strict timeout** and
  (b) **total failure swallow** (see §6), so a slow or failing provider can never
  extend the lock indefinitely or break the chunk.
- **Future improvement:** move the review *out* of the lock by running it against
  **persisted diff/checkpoint evidence** after the lock is released, rather than
  live inside the execution critical section. This is explicitly a later, optional
  improvement and is not required for v1.

### Low-risk auto-completing chunks (be honest)

Low-risk chunks that do not set `requires_human_review` currently **auto-complete
and commit locally** without pausing for per-chunk approval. For those chunks,
reviewer v1 is **advisory evidence available at final review**, not a hard
pre-commit gate — the commit happens locally before the human necessarily reads the
review. This is acceptable in v1 because the change is **local-only, not pushed**,
and **final approval remains mandatory**. Turning the reviewer into a pre-commit
gate for these chunks would be a **behavior-changing slice** and is explicitly out
of v1.

---

## 3. Reviewer Input Contract

All inputs must be derived from data Pipewright has **already loaded and already
scoped** during normal chunk execution. The reviewer introduces no new privileged
data access.

### Inputs

- `run_id`
- `chunk_number`
- feature description
- chunk title / description
- approved `files_expected` / effective scope for the chunk
- **actual** changed files (what the patch really touched)
- a **bounded, sanitized diff** of the applied change
- the configured test command
- a **test output summary/preview**, not unbounded raw output (reuse the existing
  tail-preserving test-output preview produced for #28)
- the runtime test-validation **verdict**: `strong | weak | none | unknown` (#28)
- the runtime test-validation **reason / counts** when available
- **scope expansion** info if applicable (#27)
- **patch retry / recovered attempt** info if applicable (#26)
- relevant project constraints **only if already safely available** (no new
  memory load performed for the reviewer)

### Hard exclusions

The following must **never** be sent to the reviewer model:

- `.env`, secrets, private keys, or tokens of any kind;
- raw provider credentials;
- raw GitHub tokens;
- **full file contents beyond the bounded diff**;
- raw provider/Git error strings that may embed secrets;
- **unbounded test output**;
- raw memory dumps;
- files outside the allowed/effective scope — **unless** explicitly represented as a
  *scope-violation fact* (i.e. "the previous attempt tried to touch X", as
  diagnostic metadata, never as file content).

### Required protections

- **Diff size cap.** Enforce a byte/line cap with tail-preservation. The diff is the
  single input most able to smuggle large or sensitive content to an external
  provider; it must be bounded and is the primary egress-control point.
- **Test output cap / summary.** Reuse the existing bounded preview; never the raw
  stream.
- **Redaction / sanitization** of all free-text inputs before they leave the
  process.
- **Forbidden-path defense-in-depth.** Even though the diff is already scope-guarded
  upstream, re-filter forbidden paths when assembling reviewer input, so a single
  upstream regression cannot leak a sensitive path's contents to the provider.

---

## 4. Reviewer Output Contract

The reviewer returns a single structured object (Pydantic-style). The shape below is
the proposed contract.

### Review object

- `review_status`: `completed | failed | unavailable` (closed enum)
- `verdict`: `approve_with_notes | needs_human_attention | risky` — **nullable**;
  populated only when `review_status == completed`
- `summary`
- `findings[]` (see finding shape)
- `test_gap_summary`
- `scope_summary`
- `security_or_safety_summary`
- `recommended_human_action`
- `reviewed_test_checkpoint_hash` — the diff/test-checkpoint identity the review was
  computed against (see §5; do **not** invent a new identity scheme)
- `checkpoint_id` — the source checkpoint id, if useful
- `run_id`
- `chunk_number`
- `model` / `provider` metadata, **sanitized**
- `created_at`

### Finding shape

- `category`: `correctness | test_gap | scope | security | maintainability |
  requirement_mismatch | uncertainty`
- `severity`: `info | warning | high`
- `title`
- `explanation`
- `affected_files[]` (must reference files actually present in the reviewed diff)
- `suggested_human_check`
- `confidence`

### Output rules

- **Reuse the existing diff/test-checkpoint identity.** Do **not** invent a separate
  diff-identity system. Pipewright already derives a canonical per-chunk diff
  identity from the chunk's latest test checkpoint git hash
  ([`backend/pipeline/test_validation_ack_store.py`](../../backend/pipeline/test_validation_ack_store.py),
  `compute_chunk_diff_hash`), and the #28 test-validation acknowledgement binds to
  exactly that value. The reviewer **must reuse the same identity concept** so that
  **review staleness and acknowledgement staleness cannot diverge**. In today's
  model `reviewed_test_checkpoint_hash` and a "diff hash" are the **same value**;
  store the one canonical hash (plus `checkpoint_id`), not two parallel notions.
- **Closed status enum, validated.** When `review_status` is `failed` or
  `unavailable`, `verdict` is null and `findings` / all summary fields are
  empty/nullable, enforced by validation. There is no partial record: anything that
  fails validation collapses to `unavailable`.
- `confidence` and `severity` are **display hints only**; they grant no authority.

---

## 5. Staleness Model

The reviewer result is **bound to the reviewed test-checkpoint / diff identity**
(§4). Staleness reuses the same model the #28 acknowledgement gate already uses
(`ACK_CURRENT` / `ACK_STALE` / `ACK_MISSING` semantics in
[`test_validation_ack_store.py`](../../backend/pipeline/test_validation_ack_store.py)),
so the two never disagree.

### Classification (computed on read)

Given the chunk's **current** diff identity (recomputed via the existing
`compute_chunk_diff_hash`):

- **current** — the review's hash matches the current chunk diff/test-checkpoint
  identity. The review describes the change as it stands now.
- **stale** — a review exists, but its hash differs from the current identity. The
  review describes an earlier version of the change.
- **missing** — no review exists for the chunk (e.g. a legacy chunk, or a chunk
  skip-completed from a checkpoint without a fresh review).

### Why staleness happens

- A **patch retry** (#26) re-applies and re-tests the change, writing a new test
  checkpoint with a new hash → any prior review becomes **stale**.
- A **scope expansion approve-and-retry** (#27) produces a new applied change and a
  new test checkpoint → any prior review becomes **stale**.
- In general, a changed diff / test checkpoint **requires a fresh review** before
  the displayed advice can be considered current.

This staleness is obtained **for free by construction**: because the review binds to
the same checkpoint hash the #28 ack binds to, the same events that invalidate an
acknowledgement invalidate a review, in lockstep.

### Stale review behavior in v1

- **Display a warning only.** ("This review was generated for an earlier version of
  this change.")
- **Does not block approval.**
- **Must not be shown as current advice.** A stale review must be visually demoted
  and never presented as describing the standing diff.

A later, optional reviewer **acknowledgement gate** may copy the #28-style
diff-hash-bound precondition (require a *current* review/acknowledgement before
final approval). That is **explicitly not in v1** and would be a separate
behavior-changing slice gated on smoke validation.

---

## 6. Failure Behavior

The reviewer is **best-effort**. It models its failure semantics on the existing
display-only verdict persistence (`_persist_test_run_verdict` in
`chunked_orchestrator.py`), which already records evidence on a fully-swallowed,
never-fail-the-chunk basis.

### Rules

- **Single attempt** per chunk diff identity.
- **Hard timeout.**
- **Catch provider errors.**
- **Catch malformed JSON.**
- **Catch Pydantic validation failures.**
- **Catch low-quality / unusable output** where deterministic checks exist (e.g.
  findings referencing files absent from the diff, empty/garbage summary).
- **Sanitize** every stored or displayed error string (no secrets, no tokens, no raw
  provider/Git text).
- On any failure, persist `review_status = unavailable` (or `failed`), **or** persist
  no record at all and let the read model render **"review unavailable."**
- **Never fail the chunk** because the reviewer failed.
- **Never block approval** because the reviewer failed (v1).
- **Never retry aggressively inside a repo lock.**

### Critical invariant

> The chunk outcome must be **identical** whether the reviewer succeeds, fails,
> times out, or returns malformed output.

This invariant is the single most important property of the stage and must be
covered by an explicit test (force the reviewer to raise / time out / return junk;
assert the chunk still completes, commits, or pauses exactly as it would have
without the reviewer).

---

## 7. Storage and Read-Model Recommendation

### Storage

Introduce an **isolated, additive storage foundation** in a later implementation
slice — a **dedicated `chunk_reviews` table** (or equivalent), mirroring the small,
boring CRUD shape already used by the #27 scope-expansion store and the #28
acknowledgement store
([`test_validation_ack_store.py`](../../backend/pipeline/test_validation_ack_store.py)).

**Do not overload:**

- `completion_summary` — it is **already polymorphic**, holding either a normal
  completion summary or a serialized patch-failure report; adding a third shape
  makes a fragile field worse.
- `checkpoints` — this is **safety / resume substrate**
  ([`backend/checkpoint/checkpoint_store.py`](../../backend/checkpoint/checkpoint_store.py));
  advisory LLM output must not live near skip-completion / resume logic.
- patch-failure summaries — reviewer output is not failure data.

**Reason:** reviewer output is **advisory LLM evidence**, not checkpoint/resume
substrate and not patch-failure data. Keeping it in its own table isolates a
non-authoritative, potentially noisy artifact from the authoritative safety
substrate, and matches the established #27/#28 precedent.

### Read model

- Expose the review as a **read-only overlay** on the chunk status / chunk read
  response, in the same spirit as the existing `test_validation` overlay on
  `ChunkStatus` ([`backend/models/chunk.py`](../../backend/models/chunk.py)).
- **Compute `current` / `stale` / `missing` on read**, using the existing diff
  identity helper.
- **No LLM call during read.** Reads return persisted evidence only.
- **Fail closed.** If the review overlay cannot be loaded or mapped, the read must
  degrade to "review unavailable" rather than failing the request — the same
  fail-closed posture used by the operator-state overlay
  (`_augment_plan_with_operator_state` in
  [`backend/routes/chunks.py`](../../backend/routes/chunks.py), which returns an
  `unknown` state rather than breaking the read).

---

## 8. UI Recommendation

A future UI panel must be **display-only** and clearly subordinate to the authoritative
human gates.

### Panel contents

- a clear label — **"AI Review"** / **"Advisory Review"**;
- a **verdict chip** (`approve_with_notes` / `needs_human_attention` / `risky`);
- the `summary`;
- `findings` **grouped by severity**;
- test gaps;
- scope / security notes;
- a **stale warning** when the review does not match the current diff;
- an **unavailable** state when the review failed or is missing;
- `model` / `provider` / `created_at` as **sanitized** metadata.

### Hard UI rules

- The review panel **must not look like an approval**.
- `approve_with_notes` **must not** be presented as "safe to merge."
- **No approve/reject controls** may be wired to reviewer output.
- **Existing human gates remain authoritative** and visually primary.
- Copy must explicitly state the review is **advisory and non-blocking**, consistent
  with the plain-English operator copy already established for the Operator State /
  Attention Panel.

---

## 9. Risks and Mitigations

| # | Risk | Mitigation |
|---|------|------------|
| 1 | **LLM call inside repo lock increases latency** (extends lock-hold time). | Strict timeout + total failure swallow in v1; later, run review outside the lock against persisted diff/checkpoint evidence. |
| 2 | **Stale review shown as current** after #26/#27 changed the diff → false confidence. | Bind review to the test-checkpoint hash; recompute identity on read; demote and warn on `stale`; never show stale as current. |
| 3 | **Reviewer sycophancy / rubber-stamping** (especially same provider/model as the coder). | Use the existing `REVIEWER` LLM role (§12), defaulting to a different model than `CODER` when configured; adversarial prompt framing; treat `approve_with_notes` as the lowest-signal verdict. |
| 4 | **Hallucinated findings** (issues or files not in the diff) → noise → alarm fatigue. | Constrain findings to files present in the reviewed diff; deterministic check drops findings citing absent files; de-emphasize low-confidence findings. |
| 5 | **Malformed JSON** from the model. | Pydantic validation; failure → `unavailable`; chunk unaffected. |
| 6 | **Provider timeout / hang.** | Hard time-box, single attempt, swallow. |
| 7 | **Token / cost blowup on large diffs.** | Bounded, tail-preserving diff cap; bounded test-output preview. |
| 8 | **Sensitive data egress through the diff** to an external provider. | Diff size cap + redaction + forbidden-path defense-in-depth (§3). |
| 9 | **User over-trusts advisory review** (reads it as approval) → erodes human-review discipline. | Subordinate, clearly-labeled UI; explicit "advisory / non-blocking" copy; zero wired actions. |
| 10 | **User ignores noisy reviews.** | Severity grouping; conservative verdict thresholds; terse output; drop low-value findings. |
| 11 | **Conflicting signals with the test verdict** (review `risky` vs verdict `strong`, or vice versa). | Document precedence: existing gates and the #28 verdict are authoritative; the review is commentary that never overrides them. |
| 12 | **Review attached to the wrong chunk/diff.** | Key strictly by `(run_id, chunk_number, test-checkpoint hash)`; never identify by chunk number alone (retries reuse the number). |
| 13 | **Low-risk chunk auto-completes/commits before the human sees the review.** | Accept in v1 (local-only, no push, final approval still required); state plainly; a pre-commit gate would be a separate behavior-changing slice. |
| 14 | **Schema / coupling creep** if stored in `completion_summary` / `checkpoints`. | Dedicated isolated `chunk_reviews` table; read-only overlay. |
| 15 | **Unsanitized provider/Git errors** leak secrets into DB/logs/API. | Sanitize all stored/displayed error strings before persistence or return. |
| 16 | **Scope creep into auto-fix / auto-reject / blocking gate** in a later slice without smoke. | Hard non-goals (§1); any gating/acknowledgement behavior is a separate, smoke-gated slice; enforce via acceptance criteria (§11). |

---

## 10. Safe Future Implementation Slices

Descriptive names, each independently shippable, small, and reversible. Each slice
states what it **must not change**.

### Design doc only
This document. **Must not change** any code, schema, route, package, or runtime
behavior.

### Pure reviewer models and storage foundation
Pydantic review/finding models + the isolated `chunk_reviews` storage and its
boring CRUD + staleness classification helpers. **Must not** execute any review,
add any route, call any LLM, or touch any existing flow. Read-only to all existing
behavior; covered by unit tests for the models, store, and staleness states.

### Internal advisory reviewer execution after successful tests
Run the reviewer at the placement in §2, best-effort / time-boxed / fully-swallowed,
writing to the storage from the previous slice, bound to the test-checkpoint hash.
**Must not** add API or UI, change the chunk outcome, gate anything, or alter
#26/#27/#28. **Required test:** forced reviewer failure leaves the chunk outcome
identical.

### Read-model / API surfacing
Attach the read-only review overlay (with computed `current`/`stale`/`missing`) to
the chunk read response, fail-closed. **Must not** call the LLM during a read,
introduce any action, or change any approval behavior.

### Frontend advisory review panel
Display-only panel per §8. **Must not** wire any approve/reject control to reviewer
output, look like approval, or change existing gate UI authority.

### Deferred optional acknowledgement gate (after smoke)
Optional, separate, behavior-changing slice that may add a #28-style diff-hash-bound
precondition (require a *current* review acknowledgement before final approval).
**Must not** be built until the advisory stages are smoke-validated, and **must not**
introduce auto-reject/auto-fix/auto-approve. Out of v1.

---

## 11. Acceptance Criteria for Future Implementation

At minimum, the implementation is acceptable only if:

- reviewer output is **advisory only** (drives no action);
- **reviewer failure does not change the chunk outcome** (timeout, provider error,
  malformed JSON, validation failure, junk output all yield identical chunk
  behavior);
- the review is **bound to the current diff / test-checkpoint identity** (reusing
  `compute_chunk_diff_hash`, not a new scheme);
- **stale reviews are detected on read** and never shown as current advice;
- **no approval gate behavior changes** in v1 (chunk approval, final approval, #26,
  #27, #28 all unchanged);
- **no file mutation** by the reviewer;
- **no memory writes** from reviewer findings;
- **no PR comments** / GitHub interaction;
- **no frontend action wiring** to reviewer output;
- a **test proves a forced reviewer failure does not fail the chunk**.

---

## 12. Real Implementation Anchors

These exist in the repository today and are the intended anchors for future
implementers. (Symbols are named rather than pinned to line numbers, which drift.)

- **Chunk execution path where tests complete** —
  [`backend/pipeline/chunked_orchestrator.py`](../../backend/pipeline/chunked_orchestrator.py),
  `_execute_single_chunk` (the post-test, pre-commit/pre-pause region between
  `run_tests` and the `requires_human_review` branch).
- **Test verdict persistence path** — `_persist_test_run_verdict` in the same file,
  which writes the display-only runtime verdict best-effort/swallowed; the reviewer
  should mirror its failure posture and run just after it.
- **Test-validation acknowledgement / diff-hash helper** —
  [`backend/pipeline/test_validation_ack_store.py`](../../backend/pipeline/test_validation_ack_store.py),
  `compute_chunk_diff_hash` and the `ACK_CURRENT` / `ACK_STALE` / `ACK_MISSING`
  classification (the identity and staleness model to reuse), backed by
  [`backend/checkpoint/checkpoint_store.py`](../../backend/checkpoint/checkpoint_store.py)
  (`load_chunk_step_checkpoint`, `git_commit_hash`).
- **Read-model overlay pattern** — the `test_validation` overlay on `ChunkStatus`
  ([`backend/models/chunk.py`](../../backend/models/chunk.py)) and the fail-closed
  operator-state augmentation `_augment_plan_with_operator_state`
  ([`backend/routes/chunks.py`](../../backend/routes/chunks.py)), plus the pure
  read-model computation pattern in
  [`backend/pipeline/operator_state.py`](../../backend/pipeline/operator_state.py).
- **Existing reviewer LLM role / config** — `Role.REVIEWER` in
  [`backend/llm/role_config.py`](../../backend/llm/role_config.py) (resolved from
  `REVIEWER_LLM_PROVIDER` / `REVIEWER_LLM_MODEL`, falling back to the default
  provider/model). The reviewer stage should use this existing role; **no new
  multi-model routing UI is required.**

---

## Appendix: One-Paragraph Summary

The Adversarial Reviewer Stage v1 adds a **per-chunk, advisory, display-only** AI
review of the **actual applied diff and test evidence**, produced **after tests pass
and the runtime verdict is persisted, before the human reviews the chunk**. It is
**best-effort** (single attempt, hard timeout, fully swallowed), so a chunk's
outcome is **identical** whether the reviewer succeeds or fails. It is **bound to the
existing test-checkpoint diff identity** so its staleness tracks the #28
acknowledgement model exactly, it is stored in an **isolated additive table** and
surfaced as a **fail-closed read overlay**, and it **gates nothing, mutates nothing,
and approves nothing**. It is additive to #26/#27/#28 and to the Operator State
panel, and its build should be re-prioritized against the current demo/devex phase
before any code ships.
