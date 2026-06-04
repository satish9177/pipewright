# Advisory Reviewer v1 Smoke / Closeout Checklist

Manual smoke validation and closeout record for **Advisory Reviewer v1
(display-only)** — an advisory, display-only AI review of a chunk's standing
diff and test evidence. This is a checklist, not an automated suite: the
frontend has no test framework yet, so the UI steps are manual and complement
the focused backend tests that already cover the reviewer models, store,
execution, read model, and orchestrator placement.

Related docs:

- Design: [`docs/design/adversarial-reviewer-stage.md`](../design/adversarial-reviewer-stage.md)
- Status: [`docs/status/current-state.md`](../status/current-state.md)
- Demo smoke: [`docs/testing/demo-smoke-checklist.md`](./demo-smoke-checklist.md)

## Completed Reviewer Work

- Design doc — merged
- Reviewer models / storage foundation (isolated `chunk_reviews` table) — merged
- Internal advisory reviewer execution after successful tests — merged
- Retry-path reviewer bug fixed (reviewer also runs after a successful
  human-triggered retry, not only the normal path) — merged
- Read-model / API surfacing (`ChunkStatus.review` overlay with
  current/stale/missing) — merged
- Frontend Advisory AI Review panel (display-only) — merged
- `operator_state` precedence bug fixed: chunk approval outranks the weak-test
  acknowledgement gate — merged
- Recovered retry copy cleaned up (neutral wording, no false "scoped") — merged
- Weak-validation old-copy cleanup (old UI no longer claims "Tests passed" for
  weak validation) — merged
- This smoke / closeout checklist (docs only)

## 1. Purpose

Advisory Reviewer v1 gives the human reviewing a chunk a second, automated
opinion on the change that was actually applied — its diff plus the runtime test
evidence — **before** they approve and commit it. It is evidence to read, not an
authority. It exists to surface possible correctness issues, test gaps, scope
concerns, and security/safety notes that a human might otherwise miss.

The product promise is that the reviewer is **purely advisory and display-only**:
it changes no outcome, gates nothing, and mutates nothing. A human remains the
sole authority for every approval, commit, and PR.

## 2. What Is Implemented

- A reviewer runs **after tests pass on a standing applied diff**, on both:
  - the normal chunk execution path, and
  - the successful human-triggered retry / recovered-patch path.
- The result is stored in the isolated `chunk_reviews` table, bound to the
  existing chunk diff/test-checkpoint identity (no new hashing scheme).
- Execution is **best-effort and fully swallowed**: a single LLM attempt with a
  timeout; any failure, timeout, or malformed response stores an `unavailable`
  record and never raises into the chunk outcome.
- `GET /runs/{run_id}/chunks` attaches a read-only `review` overlay to
  `ChunkStatus` when a review exists, classified as **current / stale / missing**.
- The frontend renders a display-only **Advisory AI Review** panel: verdict,
  summary, findings, and provider/model/timestamp metadata, with a visible
  advisory note and stale demotion. It has no buttons and wires no actions.

## 3. What Reviewer v1 Does NOT Do

- It does **not** approve, reject, acknowledge, or block anything.
- It does **not** gate chunk approval, final approval, commit, push, or PR.
- It does **not** auto-fix code or edit any files.
- It does **not** write project memory or create memory suggestions.
- It does **not** post PR comments or touch GitHub.
- It does **not** aggregate across a run or produce a per-run reviewer verdict.
- It does **not** change the chunk/retry outcome under any reviewer result.

## 4. Preconditions / Setup

- A configured `REVIEWER` role LLM (provider + model) with a valid API key for
  the selected provider. If the reviewer role is unconfigured/unavailable, the
  reviewer fails closed to an `unavailable` record and the run is unaffected.
- A project and an approved chunk plan ready to execute (see the demo smoke
  checklist for the end-to-end setup).
- Ability to reach the chunk read API / run detail page to observe the panel.
- To exercise the weak path: a project whose test command is intentionally weak
  (e.g. `python --version`), so runtime validation classifies as `weak`.

## 5. Manual Smoke Checklist

> Reviewer output is advisory. "Pass" for these steps means the **display and
> safety behavior** are correct — not that the reviewer's opinion is "right."

- [ ] **Normal successful chunk with strong tests**
  - Execute a chunk whose tests run meaningfully and pass (`strong`).
  - After tests pass and before commit, a review is generated and stored.
  - The chunk outcome is unchanged from the pre-reviewer behavior.

- [ ] **Recovered retry success path**
  - Cause a chunk to fail apply, then human-trigger a retry that applies and
    passes tests.
  - A review is generated for the **recovered standing diff** and stored.
  - The retry still pauses at chunk approval and never auto-commits.

- [ ] **Weak-validation path (`python --version`)**
  - Runtime validation shows **weak** (command exited 0, no meaningful tests).
  - The reviewer still runs advisory-only and can flag a test gap /
    "needs human attention."
  - Chunk approval remains the main next action; weak/no-test acknowledgement is
    still required **later**, before final approval.

- [ ] **Advisory Review panel display**
  - The Advisory AI Review panel appears for the chunk with a review.
  - It shows verdict, summary, findings, and provider/model/timestamp metadata.
  - The advisory-only note is visible; there are no action buttons.

- [ ] **Final approval remains governed by existing gates**
  - Final approval is still blocked while a chunk is awaiting approval.
  - Weak/no-test acknowledgement still blocks final approval once chunk approval
    is complete. The reviewer neither satisfies nor bypasses these gates.

- [ ] **Reviewer unavailable / failure path (if simulated)**
  - Force a reviewer failure (e.g. unset/break the reviewer LLM config or
    simulate a timeout).
  - An `unavailable` review is recorded; the panel shows non-blocking
    "no current advisory review" copy.
  - The chunk/retry outcome is identical to the success-with-reviewer case.

- [ ] **Stale review path (if simulated later)**
  - After a review exists, change the chunk's standing diff so the stored
    review's checkpoint hash no longer matches.
  - The overlay classifies the review as **stale**; the panel visually demotes
    it and warns it is not current advice.

## 6. Expected UI States

- **Review exists** → the Advisory AI Review panel renders; otherwise it renders
  nothing (missing is silent, not an error).
- **Current / completed review** → shows verdict, summary, findings, and
  metadata (provider, model, timestamp).
- **Stale review** → shown with a warning and visual demotion; never presented
  as current advice.
- **Unavailable review** → non-blocking copy ("no current advisory review is
  available"); no findings asserted, nothing gated.
- **Rolled-back test failure** → **no review appears**, by design: tests failed
  and the patch was rolled back, so there is no standing diff to review.

## 7. Safety Invariants

These must hold for every reviewer result (completed, failed, timed out,
malformed, unavailable):

- The reviewer **gates nothing**.
- **No auto-fix** — it never edits files.
- **No auto-approve** — it never approves a chunk or final result.
- **No auto-reject** — it never rejects a chunk or final result.
- **No memory writes** — it never writes project memory or suggestions.
- **No PR comments** — it never posts to GitHub.
- **No final-approval bypass** — existing chunk/branch/scope/memory/validation
  gates are untouched.
- **Reviewer failure must not change the chunk outcome** — execution is
  best-effort and swallowed; the chunk/retry result is byte-identical whether the
  reviewer succeeds, fails, times out, or returns malformed JSON.

## 8. Known Limitations / Deferred

- No reviewer **acknowledgement gate** — the review is informational only.
- No **per-run reviewer aggregation** or run-level reviewer verdict.
- No **PR comment integration**.
- No **memory suggestions** derived from review findings.
- The LLM call still runs **inside the current execution path**, relying on the
  timeout + swallow behavior to stay non-blocking (no separate worker/queue).
- Reviewer quality depends on the model and prompt and **may hallucinate or miss
  issues**. Findings are suggestions for human review, not verified facts; the
  human remains the authority.

## 9. Closeout Criteria

- This change is **docs-only** (no backend, frontend, schema, route, or package
  changes).
- The manual smoke checklist and expected states above are **recorded**.
- With the manual smoke completed and the safety invariants holding, **Advisory
  Reviewer v1 (display-only) is considered complete enough** for local self-use /
  demo readiness. Further reviewer capability (acknowledgement, aggregation, PR
  comments, memory suggestions, out-of-band execution) is explicitly deferred per
  Section 8.

## 10. Related Docs

- [`docs/design/adversarial-reviewer-stage.md`](../design/adversarial-reviewer-stage.md)
  — design, contracts, and non-goals.
- [`docs/status/current-state.md`](../status/current-state.md) — overall project
  status.
- [`docs/testing/demo-smoke-checklist.md`](./demo-smoke-checklist.md) —
  end-to-end demo smoke flow that this checklist complements.
