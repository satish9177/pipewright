# Item 14 design — post-success refinement (steer a completed chunk) + cumulative final diff

**Date:** 2026-06-12 · **Status: REVIEWED & IMPLEMENTED 2026-06-12.** Human review of this design landed one change — `REFINABLE_RUN_STATUSES` excludes `failed` (see §2). Implemented to this design; new `backend/tests/test_refine_completed_chunk.py` (19 tests incl. the §0 headline). Parity: 563-test baseline + 87 adjacent green, ruff clean. **Awaits PR review before Phase 4.**
**Parity baseline (pre-change, develop @ `6353c54`):** `test_steered_chunk.py + test_chunk_driver.py + test_chunk_retry_route.py + test_approval_gate.py + test_approval_gate_recovery.py + test_patch_failures.py` → **174 passed**; `test_chunked_orchestrator.py + test_chunk_routes.py + test_chunk_ack_read_model.py + test_operator_state.py` → **389 passed**. These must stay green, unmodified.

Every `file:line` below was re-verified against the live code today. Brief pointers that drifted: none — `patch_failures.py:722/:61`, `_commit_and_complete_chunk:777/:790/:800`, `_build_final_approval_summary:899`, `_mark_awaiting_final_approval:954`, `_require_all_chunks_completed:936`, `approval_gate.py:371` all hold.

---

## 1. Shape of the change (one paragraph)

One steer endpoint dispatches on fresh in-lock chunk status: `failed` → item 13's path, untouched; `completed` → a new `_refine_completed_chunk_locked` that reuses the item-13 lock/TOCTOU/§5.3/driver machinery with `EntryMode.STEERED` plus a new `RefinementContext` marker threaded through `drive_chunk`. The context flips exactly two driver behaviors: failure finalization becomes *restore-to-completed* (the §0 invariant) instead of mark-failed, and the success pause stores the item-13 `recovered_patch_review` marker enriched with an optional `refinement` block. The commit still happens only on chunk re-approval through `_commit_and_complete_chunk` (D1), with a pre-commit interception so a no-op refinement can never trip the `:790`/`:800` failed-marking guards. If the run was `awaiting_final_approval`, the pending final gate is atomically superseded at steer start and re-created through the existing `_mark_awaiting_final_approval` path after the refinement resolves. `_build_final_approval_summary` gains the cumulative `start_head_sha..HEAD` diff, head+tail capped by new `policy.FINAL_DIFF_MAX_CHARS` (D3).

## 2. Eligibility — new sibling evaluator (pure, `patch_failures.py`)

```python
REFINE_INELIGIBLE_CHUNK_NOT_COMPLETED = "chunk_not_completed"     # 422
REFINE_INELIGIBLE_RUN_STATE = "run_not_refinable"                 # 409
STEER_INELIGIBLE_CHUNK_STATE = "chunk_not_steerable"              # 422 (dispatcher: neither failed nor completed)
REFINABLE_RUN_STATUSES = frozenset({"awaiting_final_approval", "chunk_approved"})

def evaluate_completed_chunk_steer_eligibility(
    *, chunk_status: str, run_status: str | None, dependencies_met: bool,
    working_tree_clean: bool, human_attempts_used: int,
    max_human_retries: int = MAX_HUMAN_RETRIES,
) -> PatchRetryEligibilityDecision
```

Gate order mirrors `_evaluate_human_attempt_eligibility` (`:682`): chunk not `completed` → 422; run status outside `REFINABLE_RUN_STATUSES` → 409; dependencies (reuse reason, 422); dirty tree (reuse, 409); budget (reuse `human_retry_cap_exhausted`, 422). It takes **no report argument at all** — trap (h) is dead by construction. New reasons get `_RETRY_INELIGIBLE_HUMAN_MESSAGES` entries. The failed-chunk evaluators and their report shape are untouched.

**Run-state window** ("before PR creation"): exactly two states are refinable — `awaiting_final_approval` (entry state b) and `chunk_approved` (entry state a — paused between chunks). **`failed` is deliberately excluded ⇒ 409 `run_not_refinable`.** When the run is `failed` on a *later* chunk M, refining a completed chunk N would, on the approve/no-op leg, flow through the unmodified approval tail (`_approve_chunk_and_commit_locked:2127` → `chunk_approved`) and **silently clobber M's failed-run blocker** — the run would stop reading `failed` without M ever being resolved. The failed chunk must be addressed first via item 13; a completed-chunk refinement is rejected outright from a failed run. Also excluded: `running*` (recovery via resume first), `awaiting_chunk_approval` (another chunk's uncommitted patch — also caught by dirty-tree), `awaiting_scope_approval` (decide that gate first), `final_approved`/`pushing`/terminal (window closed). With `failed` gone, entry state a is `chunk_approved`-only, so the failure/reject "restore `prior_run_status`" leg can only ever restore `chunk_approved` — the state the approval tail already produces — and no exit path can lose run state.

**D2 budget — one deliberate deviation, flagged:** the brief's parenthetical ("`count_human_retry_attempts` picks it up for free") assumes attempts live on a report, but §0 forbids writing any failure report to a completed chunk, and completion already discards report history (`_approve_chunk_and_commit_locked:2109` passes no `recovery_attempts`). The only durable per-chunk record of *all* human attempts is the `chunk_attempts` ledger. New pure helper:

```python
def count_human_attempt_ledger_rows(rows) -> int:
    # entry_mode in {"human_retry","steered"} and final_status != "completed"
```

`final_status != "completed"` excludes `_record_approval_completion_attempt`'s approval rows (`:835` — they re-use entry_mode `human_retry` but are gate decisions, not attempts). D2's substance is preserved exactly: one per-chunk budget shared across failed-chunk retries/steers and refinements, one number (`policy.HUMAN_ATTEMPT_BUDGET = 2`), no second counter. Failed-chunk paths keep report-based counting byte-identically (the two agree where both apply; verified by trace: a pre-completion human retry appears in both). Known edge, accepted and noted: ledger writes are best-effort, so a skipped write under-counts by one — same risk class item 13 already carries for its ledger.

## 3. Driver — `RefinementContext` (`chunk_driver.py`), no loop reshape

```python
@dataclass(frozen=True)
class RefinementContext:
    chunk_commit_sha: str | None       # the chunk's own commit (context + audit; None degrades)
    head_before: str                   # branch tip at steer start — §0 restore assert
    prior_run_status: str              # captured pre-steer run state (entry a restore)
    prior_run_step: str | None
    final_gate_superseded: bool        # entry b: re-create the final gate on restore
    prior_completion_summary: dict | None  # success summary to restore on reject/no-op
```

`drive_chunk(..., refinement: RefinementContext | None = None)`. Validation: `refinement` only with `STEERED`; `STEERED` requires `retry_report or refinement` (the `retry_report is None` raises at `:430`/`:755` relax to this). Branch points — each a one-line guard, satisfying trap (e):

1. `_finalize_failed_attempt` (`:307`) → `if refinement: return orch._finalize_failed_refinement(...)` before the human-modes persist branch.
2. The two precondition failure sites in `_drive_stages` (dependency `:388`, dirty tree `:425`) → same redirect (unreachable in practice — eligibility checked in-lock moments earlier — but a completed chunk must never be failable from any driver path).
3. `drive_chunk`'s exception guard (`:818`) → same redirect with the error.
4. The human-mode pause site (`:643`) → `orch._pause_refined_chunk(...)` instead of `_pause_recovered_chunk` (which requires the nonexistent report).

`_record_attempt` calls are untouched: refinements record `entry_mode="steered"` rows (`final_status` `"failed"` / `"awaiting_chunk_approval"`), which is precisely what the D2 counter reads. `_retry_plan_for_chunk`, scope, preflight, apply, rollback-on-failed-verify, verdict persistence: all unchanged.

## 4. Orchestrator — the new path (`chunked_orchestrator.py`)

**`steer_chunk(run_id, chunk_number, failure_report_id, steer_text, *, confirm_in_scope)`** — new public entry the route calls. Validates text pre-lock (`_validate_steer_text`), locks, loads fresh `plan_status`, dispatches on the chunk's fresh status: `failed` → `_retry_failed_chunk_locked` (item 13, unchanged — a missing `failure_report_id` flows into its evaluator and 409s as today); `completed` → `_refine_completed_chunk_locked`; else → side-effect-free 422 (`chunk_not_steerable`). `steer_failed_chunk` keeps its exact current behavior (back-compat + test parity).

**`_refine_completed_chunk_locked`** (sibling of `_retry_failed_chunk_locked:2839`, same discipline):
plan-approved guard → chunk/definition lookup → `_validate_target_repo(require_clean=False)` → `_retry_branch_precheck` (reuse) → read run status+step (small `_load_run_state(run_id)` helper, same raw-SQL pattern as `settle_run_after_scope_expansion_reject:1070`) → ledger count → `evaluate_completed_chunk_steer_eligibility` → ineligible ⇒ `_retry_ineligible_result` (reuse) → **§5.3 pre-check reused verbatim** (`_steer_mentions_outside_scope` / `_steer_scope_confirmation_result`, skipped only on `confirm_in_scope`; effective scope from plan-status definitions) → capture `head_before`, `chunk_commit_sha` (newest ledger row for this chunk with `final_status="completed"` + `head_sha`; fallback `git log --grep "chunk N:"`; else None), prior summary (parsed), prior run status/step → **entry b only:** `supersede_pending_final_gate_and_mark_run` (§5) → build continuation context → `drive_chunk(STEERED, retry_report=None, refinement=..., continuation_context=..., run_step=f"chunk_{n}_refine")` → `_record_steer_turn` (unchanged; links the newest steered attempt row). The supersede→drive span is wrapped so an unexpected raise restores run state (re-creating the gate) before propagating — no stranded gate-less run.

**Continuation context — `_build_refinement_continuation_context`** (no failure-evidence section): `[Committed chunk summary]` from the chunk's completion summary (committed truth) + `[Committed diff]` from `git show <chunk_commit_sha>` + `[Human steer]` with item 13's same does-not-expand-scope closing. Capped head+tail by `STEER_CONTINUATION_DIFF_MAX_CHARS` (new shared `_truncate_head_tail`; item 13's head-only `_truncate_head` untouched for parity). **Deviation, flagged:** the brief preferred "the last successful attempt's patch checkpoint" — but checkpoints are append-only newest-wins (`checkpoint_store.py:135`), so after any failed refinement the newest code/patch checkpoints describe the *failed* attempt, not the commit. `git show` is always the committed truth; checkpoints are not consulted. Unavailable sha ⇒ explicit `[committed diff unavailable]` note; context degrades, never blocks.

**`_pause_refined_chunk`** — sibling of `_pause_recovered_chunk:2553`: builds one fresh `PatchRecoveryAttempt` (`recovery_mode="human_with_instruction"`, `outcome="recovered"`, `test_outcome="passed"`) — no prior report to carry — stores the **same `recovered_patch_review` marker kind** (D1's letter) with the new optional field, then `_pause_for_chunk_approval`:

```python
class RefinementReviewContext(BaseModel):          # patch_failures.py
    chunk_commit_sha: str | None = None
    head_before: str | None = None
    prior_run_status: str | None = None
    prior_run_step: str | None = None
    final_gate_superseded: bool = False
    prior_completion_summary: dict | None = None

class RecoveredPatchReviewSummary(...):            # additive, optional
    refinement: RefinementReviewContext | None = None
```

Old markers parse (optional+defaulted); `_load_chunk_failure_report` and `_record_approval_completion_attempt` behavior unchanged (kind unchanged).

**`_finalize_failed_refinement(run_id, chunk_number, report|error, refinement, target_repo_path)`** — the §0 invariant, replacing every mark-failed site for refinements:
1. Tree: verify clean (stage rollback already ran); if dirty, attempt `rollback_patch` once and re-verify.
2. HEAD assert: `get_current_hash == refinement.head_before` (log-loud on mismatch; the commit itself is never deletable by any path here).
3. `update_chunk_status(run_id, n, "completed")` (clears `error_message`, publishes). **Completion summary untouched** — never overwritten on this path.
4. Run settle: tree-unrecoverable ⇒ run `failed`/`chunk_{n}_refinement_recovery_failed` with a manual-cleanup narrative (chunk stays `completed`; resume recovers after cleanup). Else `final_gate_superseded` ⇒ `_mark_awaiting_final_approval` (all chunks completed again, clean tree — both hold by construction). Else restore `prior_run_status`/`prior_run_step`.
5. Return `{"status": "refinement_failed", "chunk_status": "completed", "error": <report.message>, ...}` — a distinct result status so no caller mistakes it for a failed run. Ledger row (driver-written, `final_status="failed"`) + turn row (outcome `refinement_failed`) record the attempt.

## 5. Final-gate supersede / re-create (`approval_gate.py` — minimal touch)

Verified semantics at `:224`: the creator **reuses** a pending latest gate (without refreshing its summary) and **raises** on any decided latest gate. Two surgical changes:

- **`supersede_pending_final_gate_and_mark_run(run_id, chunk_number)`** — one transaction: `UPDATE approval_gates SET status='superseded', rejection_reason='Superseded by post-success refinement of chunk N', decided_at=now WHERE … status='pending'` and `UPDATE pipeline_runs SET status='running_chunks', current_step='chunk_{n}_refine' WHERE id=… AND status='awaiting_final_approval'`. **Both rowcounts checked; zero ⇒ raise ⇒ route 409 ("the final gate was just decided — refresh").** This closes the only real race: `_decide_final_gate` (routes/chunks.py:1100) takes no repo lock, so approve-final can interleave with the steer; the two transactions serialize on the DB and the loser observes it. While superseded, approve/reject-final 404 ("Pending final approval gate not found") — final approval is blocked until the refinement resolves, exactly §14.3.
- **`_create_final_approval_gate_for_conn`:** one added condition — latest gate `superseded` ⇒ create a new gate (today: pending ⇒ reuse, anything else ⇒ raise). Re-creation then flows through the untouched `create_final_approval_gate_and_mark_run:371` from `_mark_awaiting_final_approval` / the #44A tail. At most one pending final gate ever (creation reuses any pending; supersede only flips pending→superseded); a superseded gate carries `decided_at`+reason — decided, not orphaned. `GateStatus.SUPERSEDED = "superseded"` added (new value; **no rename**).

**Trap found beyond the brief's list — the chunk-gate creator blocks the D1 re-pause:** `_create_chunk_approval_gate_for_conn:163` raises "already decided" unless the latest chunk gate is pending or (`allow_new_attempt`) rejected. A human-reviewed completed chunk's latest gate is **approved**, so the refinement's pause would crash. Fix: new explicit `allow_after_approved: bool = False` threaded `_pause_refined_chunk → _pause_for_chunk_approval → create_chunk_approval_gate_and_mark_chunk → _create_chunk_approval_gate_for_conn`, permitting a new gate after an `approved` one **only** on the refinement pause path. Item-13/fresh paths pass nothing and stay byte-identical. (State was unreachable pre-item-14: a failed chunk can't have an approved latest gate.)

## 6. Approve / reject / no-op on a refinement pause

**Approve (`_approve_chunk_and_commit_locked:2062`):** parse the marker's `refinement` block. If present, intercept **before** `_commit_and_complete_chunk`: when `not _files_touched(code) or is_working_tree_clean(repo)` ⇒ decide the gate approved, restore `prior_completion_summary` + `completed`, settle via the existing #44A tail logic, return `{"status": "refinement_no_op", "detail": "The refinement produced no change; the original commit stands."}` — traps (a)+(b) dead: the `:790`/`:800` failed-marking guards are unreachable for refinements. Otherwise fall through verbatim: gate decided → **new** commit (`commit_files`; nothing anywhere amends — trap (c)) → refinement's success summary replaces the marker → `_record_approval_completion_attempt` (new head) → #44A tail re-creates the final gate when all chunks are completed. No ledger row on the no-op branch (no commit to anchor; the gate decision and turn outcome carry the audit).

**Reject (`_reject_chunk_and_rollback_locked:2163`):** marker has `refinement` ⇒ after the existing rollback + clean-tree verify + gate-rejected decision, **restore instead of destroy**: prior summary back (fallback: minimal honest success-shaped note, never the marker — avoids marker-nesting on a later refinement), `completed`, run settle as in §4 step 4, return `{"status": "refinement_rejected", "chunk_status": "completed"}`. Non-refinement rejects (item 13/fresh) stay byte-identical (chunk `rejected`, run `failed`). This case is §0's spirit applied to the human-reject leg; the brief doesn't name it, the contract (§9 "never a dead-end of a completed chunk") requires it.

## 7. D3 — cumulative diff

- `policy.FINAL_DIFF_MAX_CHARS = 20_000` (display-only cap, head+tail; commented like its neighbors).
- `local_git.diff_range(base_sha, repo_path)` (`git diff <base> HEAD`) + `local_git.show_commit(sha, repo_path)` — both read-only.
- `_build_final_approval_summary(run_id, plan_status, branch_name, target_repo_path=None)` appends the `start_head_sha..HEAD` diff (base from `_load_start_context:1170` — the recorded fork point the human approved on top of), truncated by `_truncate_head_tail(…, FINAL_DIFF_MAX_CHARS)`; any failure ⇒ explicit `[cumulative diff unavailable: …]` line — the gate is never blocked by a display artifact. `_mark_awaiting_final_approval` passes its `target_repo_path` through (all three existing callers already supply it). The diff lives only in the gate row (the approval artifact) — never turn log, never memory (trap g). Stale-summary-on-reuse is unreachable for the diff: commits after gate creation now always supersede first.

## 8. Cross-seam fix — `get_latest_completed_attempt_head` (`chunk_attempt_store.py:142`)

Orders by `chunk_number DESC, attempt_number DESC` — assumes chunks complete in ascending order. A refinement commit on a low-numbered chunk makes actual HEAD ≠ the highest chunk's recorded head, and `_verify_resume_head_matches_last_attempt:1391` then **bricks every resume** ("manual intervention required") on a healthy run. Fix: `ORDER BY created_at DESC, chunk_number DESC, attempt_number DESC`. Parity: for monotonic runs created_at order equals the old order (old keys retained as tiebreakers); proven by a dedicated test plus the baseline suite.

## 9. Route (`routes/chunks.py`)

`SteerChunkRequest.failure_report_id: str | None = None` (validator: non-blank *when provided*; backward compatible — the UI always sends it for failed chunks today). The route calls `steer_chunk` and adds the new pass-through result statuses; error mapping unchanged. One steer surface, no second endpoint, no UI work (Phase 4).

## 10. §14.5 safety-contract check

- **§2.1:** same approved plan (`_retry_plan_for_chunk`); the *existing* chunk gate re-fires (D1) and the *existing* final gate is re-required (supersede → re-create via `_mark_awaiting_final_approval`); no new approval artifact, no skipped gate. Tests assert both gates fire.
- **§2.2:** `scope_guard`/`apply_patch_guarded` untouched; §5.3 helpers reused verbatim against effective scope (trap f); the steer requests, never grants. Test: out-of-scope refinement steer cannot write.
- **§2.3 + §0:** commit only via `_commit_and_complete_chunk` on re-approval; no-op ⇒ no commit and chunk stays `completed` (pre-commit interception); failed refinement ⇒ tree restored, commit intact, `completed`, ledger+turn recorded, narrative returned. All three proven by tests; **if the §0 test cannot be made to pass, stop and escalate.**
- **§2.6/§2.7:** turn log stays steer-text+metadata (sanitized, append-only, schema unchanged); cumulative diff is gate-display only; the steer is advisory context in the coder prompt only.
- **§2.9:** budget exhaustion / out-of-scope / failed refinement / lost supersede race all end in clear, side-effect-free (or restored) narratives; never a silent commit, never a degraded chunk, never two pending final gates, never an orphan.

## 11. Tests (new `backend/tests/test_refine_completed_chunk.py` + targeted additions)

Maps §14.3 one-to-one: **(1) §0 headline** — failing refinement on a committed chunk: tree==head_before, original commit present, status `completed`, summary unchanged, one `steered/failed` ledger row + one turn row, `refinement_failed` narrative; run restored (entry a) / fresh pending final gate (entry b). **(2) D1** — success pauses with marker, zero new commits; approve ⇒ exactly +1 commit, original sha still in history (not amended), final approval re-required. **(3) no-op** — both legs: approve-time interception (marker + clean tree ⇒ no commit, `completed`, `refinement_no_op`) and driver-side NO_CHANGES (⇒ `refinement_failed`, still `completed`). **(4) re-open, both entry states** — supersede leaves exactly one pending gate post-resolve, never two, never an orphan; approve-final mid-refinement ⇒ 404. **(5) D3** — summary contains the branch diff; truncation proven via monkeypatched `FINAL_DIFF_MAX_CHARS` (no buried literal); large diff truncated not dropped; no diff in `run_turns`. **(6) D2** — one item-13 retry + one refinement = budget 2 exhausted ⇒ third refused `human_retry_cap_exhausted`; auto/approval rows don't count; cap read from policy. **(7) §5.3** — out-of-scope mention ⇒ 409 re-confirm, zero mutation, budget unspent; `confirm_in_scope` proceeds. **(8) eligibility/dispatch** — failed *chunk* still routes to item 13; a *completed* chunk whose **run is `failed`** (blocked on a later chunk M) ⇒ **409 `run_not_refinable`** (the narrowed window — never clobber the failed-run blocker), asserting zero mutation (chunk N still `completed`, M still `failed`, no commit, no ledger/turn row, budget unspent); pending/awaiting chunk ⇒ 422; `final_approved`/`pushing` run ⇒ 409; dirty tree ⇒ 409; unmet dep ⇒ 422. **(9) turn log** — linked row per executed refinement; `feature_description` immutable; schema unchanged. **(10) gates** — reject-of-refinement restores `completed` + commit + run settle; chunk-gate `allow_after_approved` only from the refinement path. **(11)** resume works after refine-chunk-1-of-2 (the §8 ordering fix). **(12)** pure evaluator unit tests (gate order, reasons, codes). Plus: full parity re-run of the 174+389 baseline.

## 12. Changed files · explicitly out of scope · risks

**Files:** `policy.py`, `patch_failures.py`, `chunk_driver.py`, `chunked_orchestrator.py`, `approval_gate.py`, `chunk_attempt_store.py`, `local_git.py`, `core/statuses.py`, `routes/chunks.py`, tests.

**Not changing (named per the brief):** Phase 4 entirely (reviewer ack soft-gate, "steer this" hook, narrative read-model, chat UI — §5.4 still PENDING); re-running from `plan`; steering after PR creation; any rename of DB status strings or `PatchFailureType` (one **new** gate status value `superseded`; run/chunk status taxonomies untouched); `scope_guard`/`patch_applier`/`path_safety`; the failed-chunk steer path; frontend.

**Risks, named:** (i) touches `_approve_chunk_and_commit_locked`/`_reject_chunk_and_rollback_locked` — gate-adjacent; mitigated by marker-gated branches + the 563-test baseline. (ii) `approval_gate.py` is safety-critical — touch is +1 status value, +1 transactional function, +1 creation condition, +1 explicit opt-in param. (iii) #28D display verdict and newest checkpoints reflect the latest refinement attempt even after restore — display-only; #28F acks go *stale* (conservative direction) because the diff hash moves with the new passing test checkpoint. (iv) best-effort ledger writes can under-count budget by one on infra failure (item-13-class risk). (v) `get_latest_completed_attempt_head` ordering change — parity argued + tested.
