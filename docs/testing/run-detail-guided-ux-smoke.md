# Run Detail Guided UX Smoke / Closeout Checklist (#35)

Manual smoke validation and closeout record for **#35C–#35H — the guided Run
Detail experience**. This is a checklist, not an automated suite: the frontend
has no test framework configured yet, so these UI steps are manual. They
complement the focused backend tests that already cover approvals, scope
expansion, test validation/acknowledgement, memory conflict, PR push/checks, and
the `operator_state` read model.

Related docs:

- Design / audit: [`docs/design/run-detail-guided-ux.md`](../design/run-detail-guided-ux.md)
- Operator attention read model: [`docs/design/operator-state-attention-panel.md`](../design/operator-state-attention-panel.md)
- PR status / checks smoke: [`docs/testing/pr-status-checks-smoke.md`](./pr-status-checks-smoke.md)
- Scope expansion smoke: [`docs/testing/scope-expansion-recovery-smoke.md`](./scope-expansion-recovery-smoke.md)
- Stronger test validation smoke: [`docs/testing/stronger-test-validation-smoke.md`](./stronger-test-validation-smoke.md)
- Memory-use smoke: [`docs/testing/memory-trust-ui-smoke.md`](./memory-trust-ui-smoke.md)

## Completed #35 Work

- #35C Friendly labels, calmer feature-first header, short run id / full-id
  tooltip, gentler terminal outcome copy — merged
- #35D Timeline / EventLog, memory-use details, and Provider Diagnostics collapsed
  into one default-closed **Details & audit** section — merged
- #35E Read-only **Safety overview** strip near the top — merged
- #35F `OperatorAttentionPanel` `primary_action` wired for safe PROGRESS actions
  (`approve_plan`, `execute_chunks`, `approve_final`, `create_pr`) — merged
- #35G Memory-conflict co-equal actions wired; `blocked_actions` rendered as
  non-clickable explanations — merged
- #35G2 Guided spine visual polish (preview chips, equal-weight co-equal,
  calmer accents) — merged
- #35H **Finish & ship** stepper merging final approval → push/create PR → PR
  status/checks, rendered above Chunk Plan Details in final-stage states — merged
- This smoke / closeout checklist (docs only) — #35I

## 1. Purpose

This checklist validates that Run Detail now reads as a **guided cockpit** rather
than a backend console, **without** weakening safety or auditability, and with
the **legacy controls still working** where they were intentionally retained.

Concretely, it confirms:

- **Guided, not raw** — the top of the page explains what is happening, what is
  waiting, and what the safe next step is, in plain language (not backend enum
  names).
- **Safety preserved** — every approval/acknowledgement/scope gate that existed
  before #35 still applies; nothing was hidden or auto-advanced.
- **Auditability preserved** — the full timeline, memory provenance, and provider
  diagnostics are all still reachable (now under Details & audit).
- **Legacy parity** — wired guided actions call the *same* existing mutations as
  the legacy controls; the legacy controls remain visible and functional.

## 2. Safety guarantees (must remain true)

- [ ] **No auto-merge.** Pipewright never merges a PR; copy says so.
- [ ] **No auto-push.** Push / create PR only happens on an explicit click.
- [ ] **No auto-refresh of GitHub checks.** Checks update only via the manual
      Refresh in PR Status; nothing polls.
- [ ] **No hidden approval / acknowledgement / scope gates.** Weak/no-test
      acknowledgement, chunk-plan approval, chunk approval, scope expansion, and
      final approval all still gate exactly as before.
- [ ] **Details & audit** still exposes the timeline (EventLog), memory
      provenance, and provider diagnostics — nothing was deleted, only collapsed.
- [ ] **Safety strip is read-only** and clearly a *summary*, never the source of
      truth. Unknown/unverified states never imply safety.
- [ ] **Wired OperatorAttentionPanel actions reuse existing legacy mutations** —
      no new routes, no new semantics.
- [ ] **Risk / co-equal choices do not look recommended** — no single green/
      primary button for a `risk_decision`; both options carry equal weight.

## 3. Pre-smoke setup

- [ ] Start backend and frontend (see the repo README / local dev scripts).
- [ ] Use a disposable **smoke project** with a real git repo.
- [ ] Note the project **PR mode**: `local_only`, `github_cli`, or
      `manual_token`. Several Finish & ship checks branch on this.
- [ ] If practical, configure a test command that can produce **both** strong and
      weak/none validation (e.g. a real test run vs. a no-op command) so the
      weak-test acknowledgement path can be exercised.
- [ ] Have the Details & audit section in mind: it is **default-closed**, so
      "discoverable in one click" is part of the pass criteria.

## 4. Smoke states to verify

Drive a run through as many of the following states as your environment can
reproduce. Each state is a top-of-page snapshot: confirm the guided area first,
then that the matching detailed panel is still present below.

- [ ] **Awaiting chunk plan approval** — guided panel offers a wired
      "Approve chunk plan" (PROGRESS); Chunk Plan Details shows the plan +
      approve/reject controls.
- [ ] **Running / no action needed** — guided panel says waiting on the system
      / nothing needed; no misleading clickable actions.
- [ ] **Patch failure / retry available** — `retry_patch` shows as a **preview**
      (not wired); the real Retry lives in Chunk Plan Details.
- [ ] **Patch failure / retry unavailable** — retry surfaces as a blocked
      explanation ("Can't do yet: …").
- [ ] **Scope expansion needed** — `risk_decision`; co-equal approve/reject
      scope previews; the real approve/reject is in Chunk Plan Details.
- [ ] **Weak / no-test acknowledgement needed** — guided panel explains the
      acknowledgement requirement; `acknowledge_test_validation` is a preview;
      the real acknowledgement is the `TestValidationAckPanel`.
- [ ] **Awaiting chunk approval / high-risk chunk review** — chunk approval +
      advisory review visible in Chunk Plan Details; Human Approval card present.
- [ ] **Final approval** — Finish & ship Step 1 shows the `FinalApprovalPanel`;
      wired "Approve final" matches the legacy Approve Final.
- [ ] **Final approval with weak-test ack blocking** — Step 1 shows the ack
      panel; Approve Final is pre-disabled until acknowledged.
- [ ] **`final_approved` / ready to push** — Step 1 marked Done; Step 2 shows the
      push control (in supported PR modes).
- [ ] **`push_failed`** — Step 2 shows the push failure + retry using existing
      `PushPrPanel` behavior; PR state reads honestly (not "ready").
- [ ] **PR open / checks refresh** — Step 3 shows `PrStatusPanel`; checks only
      populate after clicking Refresh.
- [ ] **`local_only` complete** — terminal card shows manual-push guidance; **no**
      in-app push button anywhere.
- [ ] **Terminal failed** — gentle failure copy; pointer to Details & audit for
      the timeline; nothing pushed/merged.
- [ ] **`unknown_state_warning`** (if reproducible) — guided panel surfaces the
      warning prominently and implies no safety.
- [ ] **`stale_index` / `unsafe_start_branch` / `start_context_drifted`** (if
      reproducible) — surfaced via the existing start/execute handoff messaging;
      not silently swallowed.

## 5. Per-state checks (apply to each state above)

For every state you reach, verify:

- [ ] Friendly header / status label is understandable without backend knowledge.
- [ ] Safety strip is accurate and does **not** overclaim (unknown ≠ OK).
- [ ] OperatorAttentionPanel explains **what is waiting and why**.
- [ ] Any **wired top action** performs the *same* action as its legacy control
      (same effect, same loading/error surfaced below).
- [ ] **Preview actions look like previews** (quiet dashed chips), not disabled/
      broken buttons.
- [ ] **Blocked actions** are explanatory and **non-clickable**.
- [ ] **Risk / co-equal actions** have equal visual weight; none looks
      recommended.
- [ ] The relevant **detailed panel remains available below** the guided area.
- [ ] **Details & audit** remains discoverable (one click) and complete.

## 6. #35H Finish & ship specific checks

- [ ] In final-stage states (`awaiting_final_approval`, `final_approved`,
      `pushing`, `push_failed`, or any state with PR data), **Finish & ship
      renders above Chunk Plan Details**, directly after the guided panel.
- [ ] Steps render as a numbered stepper with distinct **Done / Current /
      Pending** states.
- [ ] Weak-test **acknowledgement is visible in Step 1** when blocking (not
      buried below the chunk plan).
- [ ] **Final approval stays blocked** until acknowledgement is current.
- [ ] **Step 2 copy never implies a merge** ("never merges").
- [ ] **PR checks refresh only manually** (Step 3 — no polling).
- [ ] **`local_only` shows no fake in-app push button** (Finish & ship does not
      render a push control for `local_only`; manual guidance stays in the
      terminal card).
- [ ] A **PR-open / completed** run still shows **PR Status / checks** in Step 3
      (the outer gate includes `showPrStatusPanel`).
- [ ] Chunk Plan Details is still present and fully functional **below** Finish &
      ship.

## 7. Regression checklist (legacy controls)

- [ ] Chunk plan **approve / reject** works (Chunk Plan Details).
- [ ] **Execute / resume** chunks works.
- [ ] **Scope expansion approve / reject** works from the legacy control.
- [ ] **Weak-test acknowledgement** works from the legacy `TestValidationAckPanel`.
- [ ] **Final approval** (approve / reject) works.
- [ ] **Push / create PR** works only in supported PR modes (`github_cli`,
      `manual_token`); `local_only` shows guidance only.
- [ ] **Memory conflict override / reject** works from **both** the guided panel
      (co-equal actions) and the legacy `MemoryConflictPanel`.
- [ ] **Details & audit** expand / collapse works; default-closed on load.
- [ ] **Memory used** lazy load (explicit "Load what the AI was given") and
      **provider diagnostics** manual refresh behavior are preserved.

## 8. Known limitations / deferred work

- Scope expansion top actions (`approve_scope_expansion` /
  `reject_scope_expansion`) remain **preview-only** because the pending
  `request_id` is not carried on `operator_state`; the legacy approve/reject in
  Chunk Plan Details is the real control.
- Weak-test acknowledgement top action (`acknowledge_test_validation`) remains
  **preview-only** because the chunk + diff checkpoint target is ambiguous from
  the action alone; the `TestValidationAckPanel` is the real control.
- `retry_patch` and `approve_chunk` top actions remain **preview-only** (they
  need a chunk number / failure-report id not present in the action).
- Branch / index states (`stale_index`, `unsafe_start_branch`,
  `start_context_drifted`) are **not** summarized in the safety strip — they are
  transient start/execute responses, not part of the persisted read model.
- Memory-use / repo-reality warnings are **not** summarized in the safety strip
  unless the memory details panel is explicitly loaded (kept lazy by #35D).
- `ChunkPlanPanel` is still dense; a future slice (candidate **#36**) may give it
  the same guided treatment.
- No frontend test runner is configured, so this manual smoke is the coverage for
  the UI composition; backend behavior remains covered by the existing suites.

## 9. Closeout criteria

- [ ] `npm.cmd run build` (frontend) passes.
- [ ] Touched-file eslint passes for any frontend change (docs-only slices: none
      required — see below).
- [ ] `git diff --check` passes.
- [ ] Manual smoke performed, or explicitly scheduled before the demo.
- [ ] No safety regression found (Section 2 all true).
- [ ] With the above green, **#35 is complete enough for the local-first / demo
      phase**; remaining items are tracked under Section 8.

### Docs lint / build

No docs linter or docs build is configured in this repo (the `docs/` tree is
plain Markdown). For this docs-only slice, run `git diff --check` only; there is
no docs build/lint step to run.
