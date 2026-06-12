# Pipewright Redesign — Implementation Handoff Brief (rolling, per-phase)

**Date:** 2026-06-12
**What this file is:** the **rolling** implementation handoff brief for the redesign. Each phase's active spec lives here; when a slice lands, this file is repurposed for the next one. **Current contents: Phase 3, slice 2 (item 14 — post-success refinement: steering a *completed* chunk → new commit + cumulative final diff).** The previous occupant (item 13 — `steered` attempts on *failed* chunks) landed and was merged via PR #286 into `develop` (commit `1487425`), reviewed & accepted 2026-06-12; its outcome is recorded in `PIPEWRIGHT_REDESIGN_WORKPLAN.md`.
**For:** the model implementing Phase 3 item 14. Re-verify every `file:line` against the live code before you cite or edit it; item 13 moved line numbers and the repo keeps moving.
**Source of record:** `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (§4.1 entry modes, §4.3 continuation — the **completed-chunk** bullet specifically, §4.7 policy, §5 rejected alternatives, §6 phasing item 14, §7 open questions 2–3, §18.6 entry-mode matrix) and `PIPEWRIGHT_REDESIGN_WORKPLAN.md`. This brief operationalizes them; if they disagree, the proposal wins and you flag the drift.
**Mode:** **Fable designs, implements, and tests this slice** — one item per PR. This is a **design brief, not a prescribed mechanism:** it fixes the *what* (scope, the §0 invariant, decisions D1–D3, the safety-contract check, the §14.3 acceptance tests) and points at the relevant code so you don't re-discover the repo — but **you own the design (the *how*): which functions to reuse, how to branch, how to wire the re-open.** The §14.3 tests are external acceptance criteria, not yours to relax. Since you now also write the tests, **your design is reviewed by a human before you implement, and the PR is reviewed before Phase 4** — that review is the only check on the homework. This slice is **item 14 only.** Do **not** start Phase 4 (reviewer ack soft-gate / "steer this" review hook / narrative read-model) — it is still gated on the **§5.4 decision (PENDING)**. Do not build chat UI beyond the existing steer endpoint + turn persistence.

---

## 0. The boundary you are standing on (read before anything else)

Item 13 wired `EntryMode.STEERED` for **failed** chunks: a human steers, the driver re-runs from `code` with the same approved plan, and on success it **pauses with no commit** (`_pause_recovered_chunk`) while on failure it **marks the chunk `failed`** and rolls back. Item 14 lets a human steer a chunk that **already succeeded and is already committed** — the "wrong sentence" case (proposal §4.3, run `415a7669`). That sounds like "item 13 on a completed chunk," and it reuses most of item 13's machinery, **but two invariants invert and that is the whole risk surface:**

1. **A completed chunk has no failure report and is not in `failed` status.** The item-13 eligibility path hard-blocks it (`patch_failures.py:722` → `RETRY_INELIGIBLE_CHUNK_NOT_FAILED`, 422; and a `None` report is ineligible). You need a **sibling eligibility path keyed on `chunk_status == "completed"`**, not a tweak to the failed-chunk evaluator.
2. **A failed refinement must NOT degrade the already-good chunk.** Item 13's failure finalization (`_finalize_failed_attempt` → `_persist_retry_patch_failure`) marks the chunk `failed`. For a completed chunk that would **destroy a committed, human-approved chunk** over a refinement that simply didn't pan out. **The single non-negotiable invariant of this slice:** a failed post-success steer rolls the working tree back to the chunk's existing commit, leaves `chunk_status == "completed"` and that commit intact, records the failed attempt in the ledger + turn log, and returns a narrative. The good commit always survives. **If you cannot prove this with a test, stop and escalate — do not ship.**

This is why item 14 was deferred one slice (proposal §7 Q3): it touches the **final-approval invariants**, where item 13 deliberately did not. Reuse item 13 aggressively, but these two points are new code, not shared code.

### Decisions already made (do not re-litigate)

- **§5.3 (steer-without-replan, conservative variant) — DECIDED 2026-06-12, implemented in item 13.** The conservative steer-text scope pre-check (`file_scope_intent` against *effective* scope, re-confirm or route to #27 on an out-of-scope mention) **applies unchanged to post-success steers.** Reuse `_steer_mentions_outside_scope` and `_steer_scope_confirmation_result` verbatim; `scope_guard` is still the hard authority at apply.
- **D1 — commit path on a successful post-success steer: DECIDED 2026-06-12 → RE-PAUSE AT THE CHUNK GATE.** A successful steered attempt on a completed chunk regenerates, **pauses at the chunk gate** with the item-13 recovered-patch-review marker (no commit yet), and the **new commit** happens on chunk re-approval through `_commit_and_complete_chunk` — the human gates every commit, max reuse, the no-op guard (`:800`) already protects contract §2.3. Then **final approval is re-required** (see §14.1, the #281 generalization). Rejected: commit-immediately-then-reopen-final-only (a commit with no human gate in front of it).
- **D2 — budget: DECIDED 2026-06-12 → SHARE the per-chunk `HUMAN_ATTEMPT_BUDGET`.** Post-success steers consume the same per-chunk human-attempt budget as failed-chunk retries/steers, counted via the attempt ledger exactly as today (record the attempt with `recovery_mode="human_with_instruction"` so `count_human_retry_attempts` picks it up for free). One source, no second number. The value stays `policy.HUMAN_ATTEMPT_BUDGET = 2`.
- **D3 — cumulative final diff: DECIDED 2026-06-12 → ADD IT, head+tail capped by policy.** The final-approval summary (`_build_final_approval_summary:899`) gains the cumulative diff of all commits on the run branch vs. the run's base, head+tail truncated by a **new `policy.FINAL_DIFF_MAX_CHARS`** (mirror the reviewer's diff-cap discipline; single-sourced, not buried). This also fixes today's gap: final approval currently shows **no diff at all**.
- **§5.4 (reviewer ack gate + "steer this" one-click hook)** is **still PENDING** and gates **Phase 4** — not this slice. The post-success steer is **free-text via the existing endpoint only**; do not build the reviewer "steer this" conversion here.

## 1. Non-negotiable safety contract (from `CLAUDE.md`)

No change may weaken these (enforced in `scope_guard`, the approval gates, `patch_applier`, `path_safety`):

1. No implementation without an approved chunk plan; never bypass chunk-plan or final-approval gates. **A post-success refinement runs the *same already-approved plan*; it re-pauses at the *existing* chunk gate and re-requires the *existing* final gate — it never invents a new approval artifact or skips one.**
2. Never edit outside approved `files_expected`; `scope_guard` is the authority. The conservative §5.3 re-confirm carries over verbatim. The steer requests intent; it never grants scope.
3. **Never create empty / no-effective-change commits.** The refinement's new commit goes through `_commit_and_complete_chunk`'s clean-tree guard (`:800`): a steer that changed nothing → **no commit**, clear narrative. Never amend the chunk's existing commit — always a *new* commit (amending rewrites approved history).
4. Never open PRs against `main`/`master`/`develop`; never auto-merge. (Untouched by this slice, but note the final-approval gate you re-open still leads only to PR creation, never an auto-merge.)
5. Never write forbidden paths.
6. Never expose or persist secrets/tokens/PII; sanitize errors. The turn log stays steer-text-and-metadata-only (never the diff, test output, provider/Git errors). The **cumulative diff (D3) goes into the final-approval *summary* shown to the human, never into the turn log or memory.**
7. Memory/steer are advisory; source code, user instruction, tests, and safety rules win on conflict.
8. AI-suggested memory stays pending until a human approves.
9. Prefer failing safely with a clear, specific error over guessing. **A failed refinement is a safe no-op on the chunk's good state, never a dead-end of a completed chunk.**

This PR is high-risk because it is the first path that **runs new code generation against an already-committed, already-approved chunk and re-opens a closed final-approval gate.** Carry an explicit safety-contract check (§14.5).

## 2. The ground you're designing on (grounding, not a prescribed mechanism)

These are the pieces item 13 left in place and where the invariants live, so you can design against the real code instead of re-discovering it. **How you use them is your design call** — the only fixed points are §0, D1–D3, §14.5, and the §14.3 tests.

- **`chunk_driver.py`** — `EntryMode.STEERED`, the shared `_HUMAN_ATTEMPT_MODES` path, `continuation_context` threading into `code_stage`, and the clean-tree/rollback/no-commit-without-change/verdict-persistence invariants all exist and already work for the failed-chunk steer. The driver's **failure finalization currently marks the chunk `failed`** (`_finalize_failed_attempt` → orchestrator `_persist_retry_patch_failure`) — that is the path that collides with the §0 invariant for a *completed* chunk. Item 13's lesson was that this capability plugged in **without reshaping `_drive_stages`**; if your design ends up rewriting the driver loop, treat that as a signal to step back and reconsider.
- **`_retry_failed_chunk_locked` / `steer_failed_chunk` (`chunked_orchestrator.py`)** — the existing lock-acquiring steer path: lock discipline, TOCTOU-fresh state load inside the lock, eligibility selection, §5.3 pre-check, continuation build, driver call, turn write. The post-success entry needs the same lock + fresh-load + §5.3 discipline; whether you extend this path or add a sibling is yours to decide.
- **`_pause_recovered_chunk` + the `recovered_patch_review` marker** — item 13's "regenerated patch awaiting the chunk gate, not yet committed" pause. This is the natural vehicle for the D1 re-pause; the new commit would then land on chunk re-approval through `_commit_and_complete_chunk` (`:777`). Use it or design an equivalent — D1 (re-pause at the chunk gate, human gates the commit) is the fixed requirement, the vehicle is not.
- **`run_turns` + `run_turn_store.py`** — append-only turn log, sanitized, metadata-only; the `chunk_number` column already accommodates a completed-chunk turn (the item-13 schema comment said so). **No schema change is expected this slice** — if your design needs one, flag it as a deviation and say why.
- **`file_scope_intent` + the §5.3 helpers** (`_steer_mentions_outside_scope` / `_steer_scope_confirmation_result`) — the conservative §5.3 behavior is **decided and must not change**; these helpers already implement it against effective scope. Reuse or re-derive, but the behavior is fixed.
- **`policy.py`** — `HUMAN_ATTEMPT_BUDGET`, `MAX_STEER_TEXT_CHARS`, `STEER_CONTINUATION_DIFF_MAX_CHARS` exist. D3 needs a new single-sourced cap (e.g. `FINAL_DIFF_MAX_CHARS`) here — name it what you like, but it lives in policy, not buried in a stage.
- **`backend/routes/chunks.py`** — `POST /runs/{run_id}/chunks/{n}/steer` exists. The UI advertises **one** steer action; prefer one route surface (dispatch on chunk status) over a second steer endpoint, unless your design has a concrete reason otherwise — say so if it does.

---

# ITEM 14 — post-success refinement (steer a *completed* chunk) + cumulative final diff

## 14.1 Summary & scope

Let a human steer a **completed** chunk with a short free-text message; the driver re-runs from `code` with the same approved plan and `files_expected`; on success it **re-pauses at the chunk gate** (D1) and a **new commit** lands on chunk re-approval; **final approval is re-required** and its summary now shows the **cumulative diff** (D3). A failed refinement is a **safe no-op** on the chunk's existing good commit (the §0 invariant).

**The post-success `steered` attempt:**
- Re-runs from the **`code` stage** with the **same approved plan and `files_expected`** (effective scope) — `human_retry` + steer + continuation context, same as item 13.
- **Continuation context** = approved plan + prior coder handoff + the chunk's **committed diff as text** (prefer the last successful attempt's `patch` checkpoint; fall back to `git show` of the chunk commit) + the steer. **No failure-evidence section** (there is no failure). Head+tail capped by `STEER_CONTINUATION_DIFF_MAX_CHARS`. Context only — never re-applied as working-tree state.
- **Conservative §5.3 gate applies unchanged** (reuse the item-13 helpers).
- **Eligibility (new sibling path):** allowed when `chunk_status == "completed"`, the run is **before PR creation** (executing or `awaiting_final_approval`), dependencies/clean-tree hold, and the **shared per-chunk `HUMAN_ATTEMPT_BUDGET`** is not exhausted (D2). No failure report is required or consulted. Reuse the gate *sequence* shape of `_evaluate_human_attempt_eligibility` but drop the `failure_type`/`chunk_status=="failed"`/`failure_report_id` checks that don't apply, and add the `completed` + run-stage checks. Preserve report shape only where a path still produces one.
- **On success → D1:** pause at the chunk gate with the `recovered_patch_review` marker; new commit on chunk re-approval; then re-require final approval (below).
- **On failure → §0 invariant:** roll back the working tree to the chunk's commit; **leave `chunk_status == "completed"` and the commit intact**; record the failed attempt (ledger + turn); return a clear narrative. Never `failed`, never a dead-end.

**Re-opening final approval (the #281 generalization):** if the run is already `awaiting_final_approval` when the steer starts, the open final gate is now stale. **Supersede/cancel the pending final-approval gate** and move the chunk back through the chunk gate; after the new commit, re-create the final gate via the existing `_mark_awaiting_final_approval` path. Verify the gate lifecycle in `approval_gate.py` (`create_final_approval_gate_and_mark_run:371` is create/**reuse** — confirm what "reuse" does to a superseded gate and that you cannot leave two pending final gates or an orphaned one). This is the riskiest state-machine seam in the slice — design it explicitly and test both entry states (steer during execution vs. steer while `awaiting_final_approval`).

**Cumulative final diff (D3):** `_build_final_approval_summary` (`:899`) gains the cumulative diff of the run branch vs. its base, head+tail capped by `policy.FINAL_DIFF_MAX_CHARS`. Compute it deterministically from git (range diff base..branch HEAD). It is summary/display only — never persisted to the turn log or memory.

**Outcomes the design must deliver (the *what* is fixed; the *how* — exact functions, signatures, branching — is yours to design and to lay out in the design you submit for review):**
- A post-success steer path that runs only for `completed` chunks (the failed-chunk steer is unchanged and still routes to item 13's path).
- A completed-chunk eligibility decision that does **not** require or read a failure report, enforces the shared per-chunk budget (D2), and keeps the failed-chunk evaluators and their report shape untouched.
- A continuation context for the post-success case with **no failure-evidence section** (there is no failure) — approved plan + prior handoff + the committed diff as capped text + the steer.
- The §0 invariant on failure: the completed chunk and its commit survive intact; the failed attempt is recorded; a clear narrative is returned.
- D1 on success: re-pause at the chunk gate, new commit on re-approval (never an amend), no-op refinement makes no commit and does not mark the chunk `failed`.
- The final-approval re-open when the run was `awaiting_final_approval` — superseding the stale gate atomically, no orphan, no duplicate. Touch the gate machinery (`approval_gate.py`) as little as possible; it is safety-critical.
- D3: the cumulative diff in the final-approval summary, single-sourced cap in `policy.py`.

Name the exact files and functions you'll touch **in your design document**, and re-verify every line number against the live code before you cite or edit it.

**Explicitly out of scope (name these in your PR):**
- The reviewer ack soft-gate and the **"steer this" one-click review hook** (Phase 4, §5.4 PENDING). Post-success steering here is free-text only.
- The phase/narrative read-model and any conversation **UI** beyond the endpoint + turn persistence (Phase 4).
- Re-running from `plan` when "the steer contradicts the plan" — unchanged non-goal from item 13; always re-run from `code` with the same approved plan; a steer that needs a different plan stays on the reject-and-new-run path.
- Steering after PR creation / merge — item 14's window is completed chunks **before** the run leaves final approval.
- Any rename of a DB status string or the `PatchFailureType` taxonomy — never.

## 14.2 Verified current behavior (re-confirm before editing)

- `patch_failures.py:722` blocks non-`failed` chunks; `:61` `RETRY_INELIGIBLE_CHUNK_NOT_FAILED`. The shared `_evaluate_human_attempt_eligibility` (item 13) is your *template*, not your reuse target, for the completed-chunk path.
- `_commit_and_complete_chunk:777` — first-commit oriented: no-changes → `failed` (`:790`), clean-tree no-op guard → `failed` (`:800`), else commits `chunk N: title` and sets `completed`. Confirm a *second* call on an already-completed chunk produces a new commit cleanly and that the `failed`-on-no-op branch is acceptable for a refinement (it currently sets the chunk `failed`, which **violates the §0 invariant**; you must intercept the no-op-refinement case so it does NOT mark a good chunk `failed`).
- `_pause_recovered_chunk` + `recovered_patch_review` marker — the D1 re-pause vehicle.
- `_build_final_approval_summary:899` (no diff today), `_mark_awaiting_final_approval:954`, `_require_all_chunks_completed:936`.
- `approval_gate.py:371` `create_final_approval_gate_and_mark_run` (create/**reuse**, transactional) — the re-open seam.
- `chunk_driver.py` `_finalize_failed_attempt` / `_HUMAN_ATTEMPT_MODES` — the failure path that currently marks `failed`.

## 14.3 Tests that must exist and pass

- **§0 invariant (the headline test):** a completed, committed chunk steered with an in-scope steer that then **fails** verification → working tree rolled back to the chunk's commit, `chunk_status` **still `completed`**, the original commit **still present and unchanged** (assert HEAD/commit list), one failed `chunk_attempts` row + one turn row recorded, clear narrative. Prove the good commit survived.
- **Post-success success path (D1):** in-scope steer on a completed chunk → re-pauses at the chunk gate (`recovered_patch_review`), **no commit yet**; on chunk re-approval → exactly one **new** commit (commit count +1, original commit still present, not amended), chunk `completed`, final approval **re-required**.
- **No-op refinement:** a steer whose regeneration produces a byte-identical tree → **no new commit**, chunk stays `completed` (NOT `failed`), clear "nothing changed" narrative. (This is the `_commit_and_complete_chunk:800` interception.)
- **Re-open final approval (both entry states):** steer a completed chunk (a) mid-execution and (b) while `awaiting_final_approval` → in (b) the pending final gate is superseded and re-created after the new commit; never two pending final gates, never an orphan. Final approval blocked until the refinement resolves.
- **Cumulative diff (D3):** final-approval summary contains the cumulative branch diff, head+tail capped at `FINAL_DIFF_MAX_CHARS`, read from `policy` (assert no buried literal); a large diff is truncated, not dropped; the diff is not written to the turn log.
- **Budget shared (D2):** post-success steers and failed-chunk retries/steers draw one per-chunk budget; at exhaustion the next steer is refused with a terminal narrative; auto attempts never consume it; count read from `policy`.
- **§5.3 unchanged:** an out-of-scope mention in a post-success steer → re-confirm / #27 offer, zero mutation, the good commit untouched; `confirm_in_scope` proceeds with `scope_guard` as backstop.
- **Eligibility:** only `completed` chunks before PR creation are post-success-steerable; a `failed` chunk still routes to the item-13 path; dependencies/clean-tree/stale-state gates still fire.
- **Turn log additive + append-only:** one row per executed post-success steer linked to its attempt; `feature_description` immutable; `run_turns` schema unchanged.
- **Gates unchanged:** the new commit still goes through chunk approval; final approval still required; no auto-merge.

## 14.4 Traps

- **(a) Marking a completed chunk `failed` on a failed refinement.** The §0 invariant. Item 13's failure finalization does exactly this — you must branch it. Test the good commit survives.
- **(b) The `_commit_and_complete_chunk:800` no-op guard marking a good chunk `failed`.** For a *first* commit, clean-tree → `failed` is correct. For a *refinement*, clean-tree means "the steer changed nothing" and must leave the chunk `completed`. Intercept before this guard fires on the post-success path.
- **(c) Amending instead of new-committing.** Always a new commit; never amend the chunk's existing commit (rewrites approved history, breaks the audit anchor, can desync a pushed branch).
- **(d) Orphaned or duplicated final-approval gate.** The re-open seam. Supersede the pending final gate atomically; never leave two pending or an orphan. Test both entry states.
- **(e) Reshaping the driver loop.** Same item-13 lesson. Reuse `EntryMode.STEERED` + continuation threading; the only new branches are failure-finalization (restore-to-completed) and the orchestrator-side re-open. If `_drive_stages` is being rewritten, stop.
- **(f) Comparing the steer against original instead of effective scope** — unchanged §5.3 trap; reuse the item-13 helper that already uses effective scope.
- **(g) An uncapped or persisted cumulative diff.** Cap with `FINAL_DIFF_MAX_CHARS`; display only; never into the turn log or memory.
- **(h) Eligibility that consults a failure report.** A completed chunk has none. The sibling evaluator must not require or read one.

## 14.5 Safety-contract check (item 14)

- **§2.1 (approved plan):** the refinement runs the **same approved plan**; it re-pauses the **existing** chunk gate and re-requires the **existing** final gate — no new approval artifact, no skipped gate. Prove both gates still fire.
- **§2.2 (scope):** `scope_guard` unchanged; §5.3 re-confirm carries over; the steer never grants scope. Prove an out-of-scope post-success steer cannot write.
- **§2.3 (no no-op commit) + the §0 invariant:** the new commit goes through the no-op guard; a no-op refinement makes **no** commit and does **not** mark the chunk `failed`; a failed refinement leaves the good commit intact. Prove all three.
- **§2.6/§2.7 (no secrets/PII; advisory-only):** turn log stays steer-text+metadata-only; the cumulative diff is display-only and never persisted to turn log/memory; the steer is advisory.
- **§2.9 (fail safe):** budget exhaustion, out-of-scope steers, and failed refinements all end with a clear narrative and the chunk's good state preserved — never a silent commit, never a degraded chunk, never an orphaned gate. If you cannot prove the §0 invariant, **stop and escalate.**

---

## 3. Update these docs when you finish (part of "done")

1. **`PIPEWRIGHT_REDESIGN_WORKPLAN.md`** — mark item 14 done in the Phase 3 sequence + "How to resume" + the TL;DR "Where we are" bullet. State that **Phase 3 is complete** and **Phase 4 still needs the §5.4 decision** before it starts.
2. **This file** — once item 14 lands + is reviewed, repurpose it for the **first Phase-4 slice** (only after the §5.4 decision is made).
3. A short per-item spec/changelog note if you follow the `specs/` convention.
4. These planning docs are **untracked**. Update content; **do not commit** them (or any code) unless the maintainer asks. If asked to commit: branch off `develop` first (never straight to `develop`/`main`), one item per commit, end with the repo's `Co-Authored-By` trailer.

## 4. Working discipline (this PR)

- **Design first, then get it reviewed, then implement.** Produce a short design — the mechanism you chose (functions, signatures, where you branch failure-finalization, how you re-open the final gate), and how it satisfies §0 / D1–D3 / §14.5 — and have a human review it **before** writing code. You own the design; because you also write the tests, the design review is the homework check.
- Read the real code first; re-verify every `file:line`; correct the brief's pointer if the live code drifted and say so.
- **Capture a parity snapshot of today's item-13 failed-chunk steer + the final-approval path before you touch them** — "I didn't perturb failed-chunk steering or final approval" is only provable against a pre-change baseline.
- Smallest correct change; **item 14 only**; list what you deliberately did **not** change (Phase 4, reviewer hook, plan-rerun, chat UI, post-PR steering).
- Tests assert the **decided behavior** (D1 re-pause + new commit, D2 shared budget, D3 capped cumulative diff) **and the §0 invariant** — not just that code runs.
- Report on completion: changed files, tests run + results, manual validation, risks, what was intentionally left untouched.
- **A human reviews this PR before Phase 4 begins.** Do not let momentum carry the loop into the §5.4-gated Phase 4.
