# Scope Expansion Recovery Design (#27A)

> Status: **Design only (#27A).** This document defines the design for **safe,
> human-approved recovery from `SCOPE_VIOLATION`** when a chunk's implementation
> genuinely needs files outside its approved `files_expected`. **No runtime
> code, route, model, migration, frontend component, or test ships with #27A.**
> Implementation is phased after this doc (see *Suggested Implementation
> Slices*).
>
> This builds **on top of** #18 (patch-failure classification / fail-closed
> safety) and #26 (Patch Failure Recovery v2; human-triggered retry inside the
> already-approved scope). See
> [`docs/architecture/patch-failure-recovery-v2.md`](../architecture/patch-failure-recovery-v2.md),
> which already names **#27** as the owner of scope-expansion recovery. #27 does
> **not** replace or weaken any #18/#26 safety.

---

## 1. Problem Statement

Pipewright executes code changes in human-approved chunks. Each chunk carries an
approved file allowlist, `files_expected`. `scope_guard.assert_files_in_scope`
(and the post-apply `validate_changed_files_in_scope` recheck) enforce that a
generated patch may only touch files inside that allowlist. Any other path is a
`SCOPE_VIOLATION` and the chunk fails closed.

#26 made human-triggered retry real, but **deliberately treats `SCOPE_VIOLATION`
as non-retryable under the same scope**: re-running the same plan against the
same allowlist reproduces the same violation, so `SCOPE_VIOLATION` is absent
from `_HUMAN_RETRYABLE_FAILURE_TYPES` and is hard-rejected by
`evaluate_patch_retry_eligibility`.

The real gap: sometimes the implementation legitimately needs a file the human
did not list when approving the chunk plan (a closely-coupled helper, a sibling
module, a config the change genuinely depends on). Today the only paths are
"reject the chunk" or "manual intervention." There is no **safe, audited,
human-gated way to amend the effective allowlist and retry**.

#27 fills exactly that gap, and nothing more. It never widens scope on its own.
It surfaces the extra files the previous attempt *tried* to touch, asks a human
to approve an expanded allowlist, and — only after approval — retries the chunk
under the amended effective scope, still pausing at the existing chunk-approval
gate before any commit.

### What #27 is not

- Not auto-expansion. A human must approve every added file.
- Not replanning, re-chunking, or re-triage.
- Not a weakening of `scope_guard`. The guard stays strict and is still the
  real authority at retry time.
- Not code approval. Scope approval only authorizes *retrying under a wider
  allowlist*; the regenerated code is reviewed and committed only through the
  existing chunk-approval path.
- Not a recovery path for a dirty tree or a chunk that needed manual
  intervention.

---

## 2. Safety Invariants (non-negotiable)

These hold at every layer and must never be relitigated by an implementation
slice:

1. **Never auto-expand `files_expected`.**
2. **Never weaken `scope_guard`** (pre-apply intent check and post-apply
   actual-dirty-set recheck both stay strict).
3. **Never silently edit files outside the approved scope.** A retry under an
   amended allowlist is still bounded by that allowlist; anything outside it
   fails closed exactly as today.
4. On an **eligible** `SCOPE_VIOLATION`, **pause** and show the requested extra
   files for human review.
5. **Require human approval** to amend the effective scope.
6. After approval, **retry the chunk** under the amended effective scope.
7. If the human **rejects**, the chunk stays failed/rejected / manual
   intervention; nothing is amended or retried.
8. **Dependent chunks remain blocked** until the recovered/amended chunk
   reaches normal `completed` status (`_unmet_dependencies` treats any
   non-`completed` status as unmet).
9. **No auto-approval. No auto-commit. No final-approval bypass.**
10. A **successful expanded retry still pauses at `awaiting_chunk_approval`**;
    commit happens only through the existing approval path
    (`_commit_and_complete_chunk`). #27 adds **no new commit site**.
11. **`chunks.files_expected` is immutable.** The original human-approved chunk
    scope is never rewritten.
12. **Scope approval is only available** when `failure_type == SCOPE_VIOLATION`
    **and** `working_tree_clean == true` **and**
    `manual_intervention_needed == false`. Otherwise scope approval is refused
    and the only path is manual intervention.

---

## 3. Pre-Apply vs Post-Apply `SCOPE_VIOLATION`

A `SCOPE_VIOLATION` can be raised at two distinct points, and they leave the
working tree in two very different states. The distinction is **real in code**
and drives eligibility.

### Pre-apply `SCOPE_VIOLATION` (eligible class)

- `scope_guard.assert_files_in_scope(code, files_expected)` raises
  `ScopeDriftError` **before any file is written** (see
  `_execute_retry_attempt`, and the equivalent in the forward execution path).
- No bytes touched disk, no patch checkpoint was written, no rollback was
  needed.
- The working tree is therefore **clean**, and `manual_intervention_needed`
  is **false**.
- This is the case scope expansion is designed for: the human simply did not
  list a file the implementation needs.

### Post-apply `SCOPE_VIOLATION` (ineligible class)

- The patch may have written files and saved a patch checkpoint, after which
  the **post-apply actual-dirty-set recheck**
  (`validate_changed_files_in_scope`, called inside `apply_patch_guarded`)
  detected an out-of-scope change and triggered rollback.
- If rollback fully restored a clean tree, `working_tree_clean` is true and
  `manual_intervention_needed` is false.
- **But if rollback failed or left the tree dirty**, the failure report is
  built with `rollback_performed=True` and `working_tree_clean=False`, and
  `build_patch_failure_report` sets `manual_intervention_needed=True`. A patch
  checkpoint may also exist on disk while the working tree no longer matches it.
- A dirty tree / manual-intervention case **must not be offered scope
  expansion.** Manual intervention only. No exception.

### The eligibility gate subsumes the distinction

The implementation does **not** need a separate "pre vs post" flag. The two
state flags already encode everything the gate needs:

> Scope expansion is eligible **iff**
> `failure_type == SCOPE_VIOLATION AND working_tree_clean == true AND
> manual_intervention_needed == false`.

Section 3 explains *why* a `SCOPE_VIOLATION` can arrive with a dirty tree.
Section 4 just reads the two flags. A pre-apply violation naturally satisfies
the gate (clean tree, no rollback); a post-apply violation that left the tree
dirty naturally fails it.

---

## 4. Eligibility Rules for Offering Scope Expansion Approval

Scope expansion approval is offered for a failed chunk **only when all of the
following hold**, evaluated from the persisted `PatchFailureReport` and a fresh
read of repo/run state under the lock:

1. `report.failure_type == SCOPE_VIOLATION`.
2. `report.working_tree_clean == true`.
3. `report.manual_intervention_needed == false` (and a fresh re-check confirms
   the tree is still clean at request time).
4. The chunk's current status is `failed`.
5. The chunk's dependencies are met (`not _unmet_dependencies(...)`) — a chunk
   blocked on an incomplete dependency is not eligible.
6. `amendments_used < MAX_SCOPE_AMENDMENTS` for this chunk (see §7).
7. The requested extra files pass write-path validation (see §12 / §22).

If any condition fails, scope expansion is **not** offered; the chunk stays
failed and the human's options remain reject / manual intervention. In
particular, **dirty tree or `manual_intervention_needed` always refuses scope
approval** (invariant 12 / §18).

This is a **distinct gate** from #26's `evaluate_patch_retry_eligibility`, which
hard-rejects `SCOPE_VIOLATION`. #27 introduces its own eligibility helper; it
does **not** route through the #26 front door (see §16).

---

## 5. Dedicated `scope_expansion_requests` Table (v1 proposal)

Scope expansion is **execution authority**: an approved request widens what the
coder/patcher are allowed to write. That authority needs **one typed, durable,
authoritative home with audit columns** — not a flag buried in a JSON blob.

> This is the one place #27 deliberately diverges from #26's "schema-free first"
> stance. #26 stores attempt diagnostics in `completion_summary` because they
> are diagnostics. #27's approved extra files are *authorization*, so they get a
> real table.

Proposed table (column names indicative; exact DDL is an implementation slice,
**not** part of #27A):

| column | purpose |
| --- | --- |
| `id` | primary key (uuid). |
| `run_id` | the pipeline run. |
| `project_id` | lock key / scoping. |
| `chunk_number` | the chunk this request amends. |
| `failure_report_id` | the `PatchFailureReport.failure_report_id` this request answers (optimistic-concurrency token; ties the request to the exact failure that motivated it). |
| `requested_files` | **untrusted** — the extra paths the failed attempt tried to touch (see §23). Diagnostic display only. |
| `approved_files` | the human-approved expanded allowlist (normalized safe relative paths). The **authoritative** contribution to effective scope. Empty until approved. |
| `status` | `pending` / `approved` / `applied` / `rejected` / `superseded` (see §6). |
| `created_at`, `decided_at`, `applied_at` | audit timestamps. |
| `decided_by` / `decision_reason` | who approved/rejected and why (audit). |

Notes:

- `requested_files` and `approved_files` are **separate** columns on purpose: a
  human may approve a subset of what was requested, and the requested set is
  never trusted as authorization.
- The `schema.sql` initializer runs `cursor.executescript()`, which understands
  SQL comments and string literals, so the DDL needs no special handling for
  semicolons inside comments.
- All stored values are sanitized; no file contents, `old_string`, secrets, or
  token-like values are ever stored (mirrors #26's sanitization discipline).

---

## 6. Request Lifecycle

```
            human requests scope expansion (eligible SCOPE_VIOLATION)
                              │
                              ▼
                           pending  ──── human rejects ────▶ rejected (terminal)
                              │
                              │  human approves (approve-and-retry endpoint)
                              │  validations pass, request flips atomically
                              ▼
                          approved  ──── retry writes new attempt/report ────▶ applied
                              │
                              │  (crash window: see §14 — re-click re-drives retry)
                              ▼
                          applied   ── chunk later reaches completed via approval path
                              │
   superseded ◀── a newer in-force request for the same chunk replaces this one
```

State definitions:

- **pending** — a human asked for expansion; nothing is authorized yet.
- **approved** — a human approved the expanded allowlist; the request now
  contributes to effective scope (see §8). The retry has been authorized but
  may not have produced an attempt record yet.
- **applied** — an approved request whose retry has produced a fresh attempt /
  failure-report / recovered-review marker. Terminal-ish for the happy path.
- **rejected** — a human declined; contributes nothing, ever. Terminal.
- **superseded** — replaced by a newer in-force request for the same chunk
  (defensive; primarily relevant if MAX is ever raised above 1). Contributes
  nothing.

**In-force statuses** (those that contribute to effective scope) are
`approved` **and** `applied`. `pending`, `rejected`, and `superseded`
contribute **nothing**. See §8 for why both `approved` and `applied` must stay
in force.

---

## 7. `MAX_SCOPE_AMENDMENTS = 1` (v1 rule)

A chunk may have **at most one** approved scope amendment in v1.

- `amendments_used` = count of `approved`/`applied` requests for the chunk.
- If, after one approved amendment, the chunk still needs *more* files outside
  the (already amended) effective scope, #27 does **not** offer a second
  expansion. It falls back to **manual intervention**.
- Rationale: one amendment covers the common "missed a coupled file" case while
  bounding blast-radius growth and keeping the audit trail simple. Repeated
  expansion starts to look like a wrong chunk plan, which is a re-plan decision,
  not a recovery.

`MAX_SCOPE_AMENDMENTS` is a named constant so the bound is explicit and a future
slice can revisit it deliberately (the `superseded` state already anticipates a
world where it is > 1).

---

## 8. Effective `files_expected` Overlay Design

This is the load-bearing mechanism and it relies on an **existing, verified
propagation chain**. Document it as an invariant so a future refactor cannot
silently sever effective scope from enforcement.

### Single merge site (spec decision #4)

Compute effective scope in exactly **one** place: `chunk_store.get_chunk_plan_status`.

```
effective_files_expected =
    chunks.files_expected (original, immutable)
    ∪ approved_files of in-force scope_expansion_requests for this chunk
```

The merged set is placed on `ChunkStatus.files_expected` returned by
`get_chunk_plan_status`. **No other site computes effective scope.**

### The propagation chain (verified end-to-end; treat as an invariant)

```
chunks.files_expected column  +  scope_expansion_requests.approved_files (in-force)
        │  merged in get_chunk_plan_status
        ▼
ChunkStatus.files_expected            (_chunk_row_to_status builds ChunkStatus)
        │
        ▼
_definition_by_number(...)            (builds ChunkDefinition with
        │                              files_expected = chunk_status.files_expected,
        │                              NOT the triage JSON's files_expected)
        ▼
ChunkDefinition.files_expected
        │
        ├──▶ assert_files_in_scope(code, chunk.files_expected)        (pre-apply guard)
        ├──▶ apply_patch_guarded(..., files_expected=chunk.files_expected)
        │        └──▶ validate_changed_files_in_scope(actual, allowed) (post-apply recheck)
        └──▶ _retry_plan_for_chunk(...)  surfaces the allowlist to the coder
```

**Verified:** `_definition_by_number` overlays `chunk_status.files_expected`
(from the DB row, via `get_chunk_plan_status`) onto the `ChunkDefinition`; it
pulls only `description` from the immutable triage JSON. So if
`get_chunk_plan_status` returns the merged scope on `ChunkStatus.files_expected`,
the amended allowlist reaches **both** scope_guard sites and the coder prompt
automatically. No separate "effective ChunkDefinition" needs to be constructed
at the retry call site.

> **Invariant to protect:** the wire
> `DB column → _chunk_row_to_status → ChunkStatus.files_expected →
> _definition_by_number → ChunkDefinition.files_expected → scope_guard /
> post-apply recheck` is what makes #27 safe *and* functional. A future refactor
> of `get_chunk_plan_status` or `_definition_by_number` that reads
> `files_expected` from the triage JSON instead of the `ChunkStatus` row would
> silently sever effective scope from enforcement. Tests must pin this wire.

### Why the unconditional overlay is safe

Because the overlay lives in `get_chunk_plan_status`, it is **unconditional**:
*every* downstream reader (forward execution, resume, commit-time checks, the
expanded retry) sees effective scope, not just the retry. This is the spec's
intent and it is safe **under exactly one guard**:

> The merge is fed **exclusively** by `scope_expansion_requests` rows whose
> status is **in-force** (`approved` or `applied`). `pending`, `rejected`, and
> `superseded` contribute **nothing**. `chunks.files_expected` stays immutable
> (decision #2 preserved); approved scope is authoritative everywhere
> downstream, keyed solely on in-force request rows.

### Why both `approved` and `applied` must stay in force

The in-force set must hold **from approval through commit completion**, not just
during retry execution. After a successful expanded retry the chunk sits at
`awaiting_chunk_approval` with a `recovered_patch_review` marker, and by then
the request has flipped to `applied`. The human's later chunk-approval runs the
commit path, which re-reads the plan and re-checks scope. If `applied` were
**not** in force, effective scope would snap back to the original allowlist and
the commit could reject the in-scope-but-extra files. Therefore both `approved`
and `applied` are in force; the invariant is *"in force from approval until the
chunk reaches `completed`."*

---

## 9. Why `chunks.files_expected` Remains Immutable

- It is the **original human-approved chunk scope** — a record of what the human
  agreed to when approving the chunk plan. Rewriting it would destroy the audit
  trail of the original decision and make "what did the human actually approve?"
  unanswerable.
- Keeping it immutable means the amendment is **additive and reversible at the
  data layer**: the effective scope is always reconstructable as
  `original ∪ approved extras`, and removing/rejecting a request cleanly reverts
  to the original.
- It keeps the two authorities cleanly separable: the **chunk plan approval**
  (original scope) and the **scope amendment approval** (extras), each with its
  own typed home and audit columns.

---

## 10. Why `chunk.status` Remains `failed` in v1

Spec decision #5: do **not** add a new chunk status in v1 unless the audit
proves one is required. It is not required.

- The chunk stays `chunk.status = failed`.
- The "paused, awaiting a human scope decision" condition is represented as:
  - a **run-level** status `awaiting_scope_approval`, and
  - a **pending `scope_expansion_request`** row.
- This mirrors #26, which added **no** new chunk lifecycle state and represented
  the recovery pause by reusing the existing `awaiting_chunk_approval` gate plus
  a marker.
- A successful expanded retry then reuses #26's existing pause: the chunk moves
  to `awaiting_chunk_approval` with the `recovered_patch_review` marker — again,
  **no new chunk status**.

Keeping the chunk `failed` also keeps dependency blocking correct for free:
`_unmet_dependencies` treats any non-`completed` status as unmet, so dependents
stay blocked through the whole pending → approved → applied →
awaiting_chunk_approval window (§19).

---

## 11. Backend Route / API Design

Two routes (shape only; **not** implemented in #27A):

### Approve-and-retry (one idempotent endpoint)

`POST /runs/{run_id}/chunks/{chunk_number}/scope-expansion/approve`

Request body carries, at minimum:

- `failure_report_id` — optimistic-concurrency token; must match the current
  persisted report (mirrors #26's `RetryChunkRequest`).
- `approved_files` — the human-approved expanded allowlist (the human may
  approve a subset of `requested_files`; never trusts `requested_files` as-is).

**Internal order is mandatory and must not be reordered** (spec decision #7):

1. Acquire the project/repo lock (`project_repo_lock` — same lock #26 uses).
2. Load **all** state fresh inside the lock (chunk status, persisted report,
   `failure_report_id`, the `scope_expansion_request` row, amendments_used).
3. **Branch precheck** (read-only; verify HEAD is on `pipewright/{run_id[:8]}`,
   reuse the `_retry_branch_precheck` pattern — verify-only, never checkout).
4. Validate `approved_files` (write-path validation + denylist + normalization;
   §12 / §22).
5. **Atomically flip** the request `pending → approved`.
6. Run the retry using the amended effective scope (reuse #26 internal
   execution; §16). On a fresh attempt/report being written, flip
   `approved → applied`.

If a **side-effect-free precheck fails** (branch verification, validation,
eligibility), the endpoint must:

- return **409** (branch/dirty-tree/stale) or **422** (validation/eligibility),
  following #26's status-code conventions;
- keep the request **pending**;
- **not** approve, **not** retry, **not** mutate summary/state (except safe
  error reporting if existing conventions require it).

The endpoint is **idempotent** (see §14): a re-click after a crash between
`approved` and the retry write must re-drive the retry, not reject as
"already approved."

### Reject

`POST /runs/{run_id}/chunks/{chunk_number}/scope-expansion/reject`

- Under the lock, flip the request `pending → rejected`, record
  `decision_reason`.
- The chunk stays `failed`; effective scope is unchanged (rejected contributes
  nothing). No retry, no commit.

> #27 adds these **two** routes only. It does **not** add a plain "retry"
> route — the existing #26 `/retry` route stays exactly as it is and continues
> to reject `SCOPE_VIOLATION`.

---

## 12. Backend Validations

Before approval is allowed, `approved_files` must pass **write-path** validation
(not read-path): the same level of strictness `patch_applier` uses for writes.
Reuse `path_safety.is_forbidden_write_path` and `normalize_relative_path`, and
add the #27 denylist delta (§22).

Each approved path must:

- normalize to a **safe relative path** (forward slashes, no traversal);
- **reject** absolute paths and `../` traversal (`validate_safe_relative_path`
  semantics);
- **reject** symlink escapes (resolve and assert containment under the repo
  root);
- **reject** forbidden / generated / vendor paths;
- **reject** high-risk denylist matches (§22);
- resolve to a path **inside the target repo root**.

Other validations:

- `failure_report_id` must match the current persisted report (else 409 stale).
- The chunk must currently be eligible (§4); re-evaluate **inside the lock**.
- `amendments_used < MAX_SCOPE_AMENDMENTS` (else fall back to manual
  intervention; do not approve).
- `approved_files` must be non-empty (an empty amendment is a no-op and must not
  be approved) and must add at least one path not already in the original
  `files_expected`.

---

## 13. Branch / Lock / TOCTOU Requirements

- **One lock.** All mutating work happens under `project_repo_lock(project_id)`
  (async) / `project_repo_lock_sync` at the route layer, exactly as #26 does.
  The only pre-lock read is the immutable `project_id` lock key.
- **Load fresh inside the lock.** Chunk status, the persisted report, its
  `failure_report_id`, `amendments_used`, and the request row are all read
  *inside* the lock so a concurrent double-submit cannot bypass
  `MAX_SCOPE_AMENDMENTS` or act on a stale snapshot (the #26D2 TOCTOU fix
  pattern).
- **Branch precheck is read-only and verify-only.** Reuse
  `_retry_branch_precheck`: if HEAD is not on `pipewright/{run_id[:8]}` (wrong
  branch, missing branch, detached HEAD, or git error), return a side-effect-free
  **409** and keep the request pending. Retry runs against the working tree, so
  it must never checkout/create/switch a branch on a request it may reject.
- **Dirty-tree re-check inside the lock.** Even if the stored report says clean,
  re-confirm `is_working_tree_clean` at request time; a tree that went dirty
  since the failure refuses scope approval (409) and routes to manual
  intervention.

---

## 14. Crash-Window / Idempotency Behavior

The request lifecycle must survive a crash between flipping `approved` and the
retry writing a new attempt/report.

- Supported transition: `pending → approved → applied` (with `superseded` as a
  defensive sibling).
- If the process crashes **after** `pending → approved` but **before** the retry
  writes a fresh attempt / failure-report / recovered-review marker, re-clicking
  the approve endpoint must **re-drive the retry** rather than reject the request
  as "already approved."
- Mechanism: the endpoint treats a request already in `approved` (but not yet
  `applied`) as "approval done, retry not yet observed" and resumes at step 6
  (run the retry) under the lock. The `failure_report_id` token plus the
  `approved`/`applied` distinction make the operation safely repeatable.
- The retry execution itself is the #26 machinery, which already marks the chunk
  `running` inside the lock so a crash leaves a resumable, non-stuck chunk.

---

## 15. `scope_guard` Interaction

`scope_guard` stays **strict and unchanged**. #27 does not modify
`assert_files_in_scope` or `validate_changed_files_in_scope`. It only changes
*what allowlist is passed in*, via the effective-scope overlay (§8).

- **Pre-apply** `assert_files_in_scope(code, chunk.files_expected)` runs on the
  **effective** allowlist (original ∪ approved extras). A file the human did not
  approve is still rejected.
- **Post-apply** `validate_changed_files_in_scope(actual, allowed)` runs on the
  same effective allowlist against the **actual dirty set**.
- Empty allowlist is still treated as unsafe (both helpers reject it).

**`scope_guard` is law; the human-approved allowlist is the input, not a
bypass.** Scope approval changes the bounds; it never disables the check.
`requested_files` is advisory and is never fed to the guard as authorization
(§23).

---

## 16. Retry Interaction With Existing #26 Internal Machinery

Spec decision #9: reuse #26's **internal retry execution**, not its **public
eligibility / front door**.

- **Bypass** `evaluate_patch_retry_eligibility`. It hard-rejects
  `SCOPE_VIOLATION` (absent from `_HUMAN_RETRYABLE_FAILURE_TYPES`) and gates on
  `chunk_status == "failed"` + clean tree under the *original* scope. #27 has
  its own eligibility gate (§4).
- **Reuse** `_execute_retry_attempt`: regenerate the coder handoff
  (`_retry_plan_for_chunk` → `run_coder`) → pre-apply `assert_files_in_scope`
  (now on effective scope) → `dry_run_changes` → `apply_patch_guarded`
  (post-apply recheck on effective scope) → `run_tests` →
  `_pause_recovered_chunk`. The success path already produces the
  `awaiting_chunk_approval` pause with the `recovered_patch_review` marker — #27
  gets that for free.
- **Structure:** introduce a **sibling locked orchestrator** (analogous to
  `_retry_failed_chunk_locked`) that runs #27's eligibility gate, flips the
  request status, and then calls `_execute_retry_attempt` with the effective
  scope already overlaid via `get_chunk_plan_status`. It must **never** call
  `_execute_single_chunk`, never run the planner/triage, never mutate
  `chunks.files_expected`, never weaken `scope_guard`, and never commit.
- The order matters: **change the approved scope first** (flip request to
  `approved`, so `get_chunk_plan_status` overlays it), **then** reuse the safe
  internal retry execution against the amended effective scope.

A retry under the amended scope can still safely **re-fail** (a fresh
`SCOPE_VIOLATION` against the new allowlist, a dry-run/apply/test failure) and
produce a **new `failure_report_id`** via the existing
`_persist_retry_patch_failure` path. If it re-fails with `SCOPE_VIOLATION` and
`amendments_used` has reached `MAX_SCOPE_AMENDMENTS`, it routes to manual
intervention (§7).

---

## 17. Checkpoint / Rollback / Commit Safety

- **No new commit site.** A successful expanded retry pauses at
  `awaiting_chunk_approval`; the existing approval path
  (`_commit_and_complete_chunk`) commits the newest code checkpoint later, after
  human review. #27 never commits.
- **No checkpoint deletion in #27.** Spec decision #12: do not implement stale
  checkpoint cleanup here.
- **Documented invariant:** *A patch checkpoint is not proof that a patch is
  currently applied on disk. Git working-tree state is the source of truth.* The
  eligibility gate therefore keys on the **live `is_working_tree_clean` check**
  (re-confirmed under the lock), not on the presence/absence of a checkpoint. A
  post-apply `SCOPE_VIOLATION` may have left a patch checkpoint behind even
  though rollback restored (or failed to restore) the tree; the checkpoint is
  not consulted to decide eligibility.
- Rollback failure or a non-clean tree after rollback ⇒ `manual_intervention`
  ⇒ scope approval refused (§18).

---

## 18. Dirty-Tree / Manual-Intervention Behavior

**No exception.** If, at any decision point (stored report or fresh re-check):

- `working_tree_clean == false`, **or**
- `manual_intervention_needed == true`,

then scope expansion approval is **refused**. The endpoint returns 409, keeps
the request pending (or never creates one), and the chunk's only paths are
clean-the-tree-then-reassess or manual intervention. A dirty tree means manual
intervention only — scope approval is never offered to "rescue" a dirty tree,
because retrying against an unknown on-disk state cannot be made safe by
widening an allowlist.

---

## 19. Dependency Blocking Behavior

- A chunk awaiting scope approval is `failed`; a chunk that successfully retried
  is `awaiting_chunk_approval`. **Neither is `completed`.**
- `_unmet_dependencies` treats any non-`completed` status as unmet, so dependent
  chunks remain **blocked** through the entire pending → approved → applied →
  awaiting_chunk_approval window, and only unblock once the amended chunk reaches
  `completed` via the normal approval/commit path.
- Existing dependency enforcement is **unchanged**. #27 adds no exception and no
  early unblock.

---

## 20. Frontend Approval UI Requirements

Extend the existing patch-failure surface (`PatchFailureBanner` / card); do not
rebuild. **Not implemented in #27A.**

- Show, for an eligible `SCOPE_VIOLATION`:
  - the failure category and the original `files_expected`;
  - the **requested extra files** the previous attempt tried to touch
    (clearly labeled untrusted — §21 / §23);
  - an editable approval list so the human approves an **explicit** expanded
    allowlist (and may approve a subset);
  - the `MAX_SCOPE_AMENDMENTS` budget and how much is used.
- Provide **Approve scope & retry** and **Reject** actions wired to the two
  routes (§11).
- **Do not** offer scope approval when the chunk is dirty-tree /
  manual-intervention / cap-exhausted; show the manual-intervention message
  instead.
- After a successful expanded retry, render the existing recovery review pause
  (`recovered_patch_review`) labeled **"Review recovered change"** — the chunk
  approval gate, unchanged.
- Keep chunk approval **disabled** while the chunk is failed / awaiting scope
  decision.
- **No high-risk acknowledgement checkbox in v1** (spec decision #11). High-risk
  files are blocked outright (§22), not approvable with a warning.

---

## 21. UX Copy: Scope Approval Is Not Code Approval

The UI must make the two distinct authorities unmistakable:

- Scope approval **only** authorizes *retrying under a wider allowlist*. It is
  **not** approval of any code. Suggested copy near the action: *"Approving
  scope lets Pipewright try again while allowed to edit these extra files. You
  will still review and approve the actual change before anything is
  committed."*
- For the requested files list, **do not** say "these files are required."
  Required copy (spec decision #13): *"The previous attempt tried to touch these
  extra files."* Humans review and approve the expanded allowlist; retry-time
  `scope_guard` remains the real authority.
- The later chunk-approval gate keeps its own copy (**"Review recovered
  change"**) and is where code is actually approved and committed.

---

## 22. Edge Cases & Security / Path Validation

**Use write-path validation, not read-path validation.** `approved_files` are
about to authorize writes, so they are validated with
`is_forbidden_write_path`-level strictness plus the #27 denylist.

### Denylist (v1) — block completely, no acknowledgement

At least these are blocked from scope approval:

- `.env*` (note: `.env.example` / `.env.sample` are explicitly *allowed* by
  `is_forbidden_path`)
- secrets / private paths
- `.git/*`
- `.github/workflows/*`
- `migrations/*`, `migration/*`
- `alembic/versions/*`
- `pyproject.toml`
- `package-lock.json`
- `requirements.txt`
- `Dockerfile`
- `docker-compose.yml`

### Honest gap: `is_forbidden_write_path` does not cover all of these today

Cross-referencing the denylist against the current
`path_safety.is_forbidden_write_path`:

- **Already covered** by `is_forbidden_write_path`: `.env*` (via `.env`
  substring + `is_forbidden_path`), `secrets` / `private` (substring),
  `.git/*` (component), `requirements.txt`, `package-lock.json`, `Dockerfile`
  (name `dockerfile`), `docker-compose.yml`.
- **NOT covered today** and therefore **#27 must add them on top of**
  `is_forbidden_write_path`: `pyproject.toml`, `.github/workflows/*`,
  `migrations/*`, `migration/*`, `alembic/versions/*`.

A reader must not assume `is_forbidden_write_path` alone is sufficient — #27's
scope-approval validation is a **superset**: `is_forbidden_write_path(path)`
**OR** the #27 path-prefix/name denylist above. Where this delta lives (extend
`path_safety` vs a #27-local validator) is an implementation-slice decision, but
the denylist must be enforced regardless of where it lives.

### Other path edge cases

- **Normalization first.** Normalize (`normalize_relative_path`) before any
  matching; compare lowercased final components / path prefixes so
  case/slash variants cannot dodge the denylist.
- **Traversal / absolute paths** rejected (`validate_safe_relative_path`).
- **Symlink escape**: resolve the candidate and assert it stays under the repo
  root.
- **Vendor / generated paths** (e.g. `node_modules/`, build output) rejected.
- **Duplicate / already-in-scope paths**: de-duplicate; reject an amendment that
  adds nothing new.
- **Requested ≠ approved**: never auto-approve `requested_files`; the human's
  `approved_files` is the only authorization, and it is validated independently.
- **Stale `failure_report_id`**: 409, no mutation.
- **Empty `approved_files`**: rejected (no-op amendment).

---

## 23. `requested_files` Is Untrusted

`requested_files` is the set of extra paths a **failed model attempt** tried to
touch. It is **diagnostic display only**, never authorization:

- The UI must say *"The previous attempt tried to touch these extra files,"*
  **not** *"these files are required."*
- The human reviews and approves an explicit allowlist; the approved set may be
  a subset of (or differ from) `requested_files`.
- Retry-time `scope_guard` enforces the **approved** allowlist, not
  `requested_files`. Even an approved path is re-validated by the guard on every
  attempt.

---

## 24. Explicit v1 Deferrals

- **No second amendment** (`MAX_SCOPE_AMENDMENTS = 1`); more ⇒ manual
  intervention.
- **No high-risk acknowledgement checkbox** — high-risk files are blocked, not
  approvable-with-warning.
- **No checkpoint deletion / stale-checkpoint cleanup** (§17).
- **No new chunk status** — chunk stays `failed`; pause is a run status +
  pending request (§10).
- **No auto-expansion, no auto-approval, no auto-retry, no auto-commit.**
- **No rename/move auto-retargeting** — that remains its own concern; #27 only
  adds explicitly human-approved files.
- **No cross-run analytics table** for scope expansions beyond the audit columns
  on `scope_expansion_requests`.
- **No change to the #26 `/retry` route** — it still rejects `SCOPE_VIOLATION`.

---

## 25. Suggested Implementation Slices (after #27A)

Each slice is small, reversible, and ships behind the existing safety layer.
**None ship with #27A.**

- **#27B — Table + lifecycle data layer.** Add `scope_expansion_requests` DDL
  and a typed store with the status state machine
  (`pending`/`approved`/`applied`/`rejected`/`superseded`). No routes, no
  overlay yet. Tests for status transitions and audit columns.
- **#27C — Effective-scope overlay.** Wire the merge into
  `get_chunk_plan_status` keyed on in-force (`approved`/`applied`) request rows.
  Pin the propagation-chain invariant (§8) with a test that an approved request
  reaches `ChunkDefinition.files_expected`, and that `chunks.files_expected`
  stays immutable. No routes yet.
- **#27D — Eligibility helper + validations.** Pure `SCOPE_VIOLATION`
  eligibility gate (§4) and `approved_files` write-path/denylist validation
  (§12/§22), including the `is_forbidden_write_path` delta. Pure-function tests.
- **#27E — Approve-and-retry + reject routes.** The two endpoints (§11), lock /
  branch precheck / fresh-load / atomic flip / crash-window idempotency (§13,
  §14), reusing `_execute_retry_attempt` via a sibling locked orchestrator
  (§16). Tests for 409/422 paths, dirty-tree refusal, idempotent re-click.
- **#27F — Frontend approval UI.** Extend `PatchFailureBanner`; requested-files
  (untrusted) display, explicit approval list, scope-not-code copy (§20/§21).
- **#27G — Smoke tests + docs.** Backend/frontend/manual smoke checklist (below)
  and a short user-facing note.

---

## Mandatory Smoke Tests (for the implementation slices, not #27A)

**Backend (`python -m pytest backend/tests -q -m unit`, plus targeted):**

- Eligible pre-apply `SCOPE_VIOLATION` (clean tree) ⇒ scope expansion offered.
- Post-apply `SCOPE_VIOLATION` with dirty tree / `manual_intervention_needed`
  ⇒ scope approval **refused** (409), manual intervention only.
- Approved request ⇒ `get_chunk_plan_status` returns effective scope on
  `ChunkStatus.files_expected`; `chunks.files_expected` unchanged.
- Effective scope reaches `assert_files_in_scope` **and** the post-apply recheck
  (propagation-chain invariant, §8).
- Approving extra files then retrying ⇒ reaches `awaiting_chunk_approval` with
  `recovered_patch_review`; **no commit** until chunk approval.
- Retry under amended scope can re-fail with a **new `failure_report_id`**.
- Second expansion attempt blocked by `MAX_SCOPE_AMENDMENTS = 1` ⇒ manual
  intervention.
- Denylist: each high-risk path (incl. the `is_forbidden_write_path` delta —
  `pyproject.toml`, `.github/workflows/*`, `migrations/*`, `migration/*`,
  `alembic/versions/*`) is rejected; `.env.example` allowed.
- Path validation: absolute, `../`, symlink escape, vendor/generated paths
  rejected.
- Stale `failure_report_id` ⇒ 409, request stays pending, no mutation.
- Wrong/detached branch ⇒ 409, side-effect-free, request stays pending.
- Crash window: re-click after `approved` (before `applied`) re-drives retry.
- Reject ⇒ chunk stays `failed`, effective scope unchanged.
- Dependent chunk stays blocked until the amended chunk reaches `completed`.
- `scope_guard` still rejects a path **not** in the approved allowlist on retry.

**Frontend (`cd frontend; npm.cmd run build`):**

- Renders requested-files as *"previous attempt tried to touch"* (untrusted),
  the editable approval list, and the scope-not-code warning.
- Hides scope approval for dirty-tree / manual-intervention / cap-exhausted.
- Renders the post-retry "Review recovered change" gate.

**Manual:**

- Drive a chunk to a pre-apply `SCOPE_VIOLATION`, approve a single extra file,
  confirm retry pauses at chunk approval, approve, confirm a single clean
  commit; confirm a dependent chunk only runs afterward.
- Confirm a dirty-tree `SCOPE_VIOLATION` offers no scope approval.

---

## Final Safety Invariants (recap)

- Scope expansion is offered **only** for `SCOPE_VIOLATION` with
  `working_tree_clean == true` and `manual_intervention_needed == false`.
- `chunks.files_expected` is **immutable**; effective scope = original ∪
  in-force approved extras, merged in one site.
- `scope_guard` stays strict and is the real authority on every attempt.
- `requested_files` is untrusted; only human-approved, validated `approved_files`
  authorize anything.
- No auto-expansion, no auto-approval, no auto-commit, no new commit site, no
  final-approval bypass.
- A successful expanded retry still pauses at `awaiting_chunk_approval`.
- Dependents stay blocked until the amended chunk reaches `completed`.
- Dirty tree / manual intervention ⇒ scope approval refused, no exception.
- High-risk files are blocked outright in v1.
