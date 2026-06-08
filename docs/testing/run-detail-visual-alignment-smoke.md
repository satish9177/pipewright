# Run Detail Visual Alignment Smoke / Closeout Checklist (#37E)

Manual smoke validation and closeout record for **#37A–#37D2 — the Run Detail
visual-alignment work** that moved the page closer to the Claude Design "guided
cockpit." This is a checklist, not an automated suite: the frontend has no test
framework configured yet, so these UI steps are manual. They complement the
existing backend tests (approvals, scope expansion, test validation /
acknowledgement, advisory review, patch retry, the `operator_state` read model)
and the #35 / #36 Run Detail smoke checklists.

Related docs:

- State-gated Tier 2 plan: [`docs/design/state-gated-tier2-run-detail.md`](../design/state-gated-tier2-run-detail.md)
- #35 Run Detail design: [`docs/design/run-detail-guided-ux.md`](../design/run-detail-guided-ux.md)
- #36 Active Chunk design: [`docs/design/active-chunk-guided-ux.md`](../design/active-chunk-guided-ux.md)
- #35 Run Detail smoke: [`docs/testing/run-detail-guided-ux-smoke.md`](./run-detail-guided-ux-smoke.md)
- #36 Active Chunk smoke: [`docs/testing/active-chunk-guided-ux-smoke.md`](./active-chunk-guided-ux-smoke.md)

## 1. Purpose

This checklist validates that, after #37A–#37D2, the Run Detail page:

- **Reads more like the Claude Design guided cockpit** — calm pipeline rail, a
  single Safety overview, a focused operator spine with action effects, and a
  finished run whose chunk detail recedes into history.
- **Did not regress safety, approval gates, or auditability** — every approval,
  retry, scope, acknowledgement, final, and PR control behaves exactly as before;
  all warnings stay visible; Details & audit stays complete.
- **Has its remaining gaps documented** — the larger state-gated Tier 2 reorg and
  visual-token migration remain explicitly deferred (Section 11).

The redesign is **presentation / organization only**. No backend behavior, no
action wiring, and no `operator_state` mapping changed across #37.

## 2. Completed #37 work

- **#37A** — Pipeline rail + Run Summary simplification: replaced the Run Summary
  card and the old blue-box `StepIndicator` with a calm `plan → code → patch →
  test → review → ship` rail; chunk totals moved into the header meta row —
  merged.
- **#37B** — Safety surface consolidation: a single `RunSafetyStrip` "Safety
  overview" now owns the at-a-glance chips **and** the `operator_state`
  process-gate checks + unknown-state banner; `OperatorAttentionPanel` no longer
  duplicates the safety-check block — merged.
- **#37C** — Guided action effects ledger: display-only "what this affects" pills
  under wired operator actions only — merged.
- **#37D1** — State-gated Tier 2 design / implementation plan (docs only) —
  merged.
- **#37D2** — Terminal/final-state chunk history collapse: a fail-open
  "Chunk history" disclosure for finished runs with no pending chunk action —
  merged.
- **#37E** — This smoke / closeout checklist (docs only).

## 3. Safety guarantees (must remain true)

- [ ] **No backend behavior changes** — #37A–#37D2 are frontend-only; no API /
      schema / route / package changes.
- [ ] **No action wiring changes** — no new mutations; wired operator actions still
      call the same legacy mutations; `operator_state` mapping is unchanged.
- [ ] **No auto-push** — push happens only on an explicit click in a supported PR
      mode.
- [ ] **No auto-merge** — Pipewright never merges; "Merges: Never" copy is honest.
- [ ] **No auto-refresh of PR checks** — checks refresh only on the manual action.
- [ ] **No hidden approval gates** — plan / chunk / final approval controls always
      reachable when pending.
- [ ] **No hidden retry / scope / acknowledgement / chunk-approval controls** —
      they stay in their detailed components and stay reachable when active.
- [ ] **Safety overview remains read-only** — it summarizes; it wires nothing and
      changes no run behavior.
- [ ] **Details & audit remains available** — timeline / memory provenance /
      provider diagnostics unchanged and still reachable, and **separate** from the
      #37D2 Chunk history disclosure.
- [ ] **Chunk history collapse is fail-open** — any uncertainty (missing plan,
      unknown-state warning, pending plan approval, or any attention chunk) keeps
      the chunk plan fully expanded.

## 4. Pre-smoke setup

- [ ] Start backend and frontend (see the repo README / local dev scripts).
- [ ] Use a disposable **smoke project** with a real git repo.
- [ ] Note the project **PR mode** (`local_only`, `github_cli`, `manual_token`);
      the final-stage panels and effects ledger branch on it.
- [ ] Have at least one **single-chunk** run and, if practical, one **multi-chunk**
      run (so the chunk list, active card, and history collapse can be observed).
- [ ] If practical, configure a **weak / no-test command** so the weak/no-test
      acknowledgement gate can be exercised.
- [ ] If practical, reproduce: **patch failure** (retry available and unavailable),
      **scope expansion pending**, **awaiting chunk approval / high-risk review**,
      and a **memory conflict**.

## 5. #37A — Pipeline rail checks

- [ ] **Run Summary card no longer appears** anywhere on the page.
- [ ] **Feature description appears once** (the header title) — not duplicated in a
      summary card below.
- [ ] **Pipeline rail appears directly under the header.**
- [ ] **Stages read `Plan → Code → Patch → Test → Review → Ship`** (friendly
      labels, not raw `approval` / `github_pr` enums).
- [ ] **Current stage is highlighted** (inked cell + caption `current` /
      `in progress`).
- [ ] **Completed stages read as done**; **future stages are muted**.
- [ ] **A failed run marks its current stage** as failed (red), not blue.
- [ ] **Header meta still shows** the short run id (full id in tooltip) and, when a
      chunk plan exists, a `Chunk N of M` (or `M chunks`) summary.

## 6. #37B — Safety consolidation checks

- [ ] **Only one Safety overview surface** appears near the top (no second safety
      summary lower in the operator panel).
- [ ] **Scope / Tests / Review / PR chips** are still present in the Safety
      overview.
- [ ] **`operator_state` process-gate checks** ("Process gates (not code
      correctness)") render in the Safety overview when present.
- [ ] **FAIL / WEAK / N/E / N/A** process-gate statuses remain visible with their
      labels and detail text.
- [ ] **`unknown_state_warning`, when present, appears prominently** as the
      Unknown-state banner at the top of the Safety overview.
- [ ] **`OperatorAttentionPanel` no longer renders the safety-check block** or its
      own unknown-state box (those moved up).
- [ ] **Full detail panels remain available below** — weak-test
      (`RuntimeTestValidationBanner`), advisory review (`AdvisoryReviewPanel`),
      scope (`ScopeExpansionBanner`), patch failure (`PatchFailureBanner`), Finish &
      ship, and `local_only` guidance all still render in full.
- [ ] **Safety overview footer still states it is read-only** and that the detailed
      cards remain the source of truth.

## 7. #37C — Effects ledger checks

- [ ] **Ledger appears only under WIRED actions:** `approve_plan`,
      `execute_chunks`, `approve_final`, `create_pr`, `approve_memory_conflict`,
      `reject_memory_conflict`.
- [ ] **Ledger does NOT appear for preview / unmapped actions:** `retry_patch`,
      `approve_chunk`, `approve_scope_expansion`, `reject_scope_expansion`,
      `acknowledge_test_validation`, and any unknown id (these keep the
      display-only "preview" marker, no ledger).
- [ ] **Effects wording is conservative** — uncertain / mode-dependent effects read
      "may …" (e.g. `execute_chunks` → "May write during execution";
      `approve_final` → "May commit / finish (depends on mode)";
      `approve_memory_conflict` → "May write (continues execution)").
- [ ] **"Merges: Never" appears where relevant** (`execute_chunks`, `approve_final`,
      `create_pr`, both memory-conflict actions).
- [ ] **`create_pr` shows push / PR effects** ("Pushes branch: Yes (supported PR
      modes)", "Creates PR: Yes").
- [ ] **Co-equal memory-conflict actions remain equal weight** — both
      `approve_memory_conflict` and `reject_memory_conflict` use identical ledger
      styling; neither looks recommended.
- [ ] **The ledger is display-only** — it adds no buttons and changes no enable /
      disable state.

## 8. #37D2 — Chunk history collapse checks

- [ ] **`complete` / `local_only_complete`** — Chunk Plan Details is collapsed into
      a default-closed **"Chunk history"** disclosure.
- [ ] **`final_approved`** — Chunk history is collapsed while **Finish & ship stays
      visible/expanded** above it.
- [ ] **`rejected` / `final_rejected`** — collapses when no attention chunk exists.
- [ ] **`failed` with no chunk attention** — may collapse; the terminal failed card
      + Details & audit still explain the failure.
- [ ] **`failed` chunk with patch recovery / retry** — **stays expanded** (attention
      chunk forces expansion); the retry path is reachable.
- [ ] **`awaiting_final_approval`** — stays expanded (not an eligible status).
- [ ] **Awaiting chunk approval** — stays expanded.
- [ ] **Pending scope expansion** — stays expanded.
- [ ] **Running / in_progress** — stays expanded.
- [ ] **`unknown_state_warning` present** — fail-open: Chunk Plan Details stays
      expanded even in an otherwise-eligible terminal status.
- [ ] **Expanding "Chunk history" reveals the full, unchanged `ChunkPlanPanel`** —
      same summary, execution controls, active card, chunk list, banners, and
      controls as a non-collapsed run.
- [ ] **Chunk history is separate from Details & audit** — the two disclosures are
      distinct and both present.

## 9. State smoke matrix

Drive a run through as many states as the environment can reproduce. For each,
verify the rail, Safety overview, operator spine/effects, and chunk
section/history behave per Sections 5–8 with no safety regression.

- [ ] **Awaiting chunk plan approval** — rail at Plan; plan approve/reject present;
      chunk plan expanded; `approve_plan` effects ledger shown (no commit/push).
- [ ] **Plan approved / ready to execute** — Execute / Resume present;
      `execute_chunks` ledger ("may write", "Merges: Never"); expanded.
- [ ] **Running** — running chunk active; "waiting on the system"; expanded.
- [ ] **Patch failure / retry unavailable** — failure context visible; no retry
      button; chunk expanded.
- [ ] **Patch failure / retry available** (if reproducible) — Patch recovery context
      + real Retry; chunk expanded.
- [ ] **Scope expansion pending** (if reproducible) — Scope permission framing +
      approve/reject; "not code approval" copy intact; expanded.
- [ ] **Weak / no-test acknowledgement needed** — weak verdict visible; ack gate in
      Finish & ship; final approval blocked until acknowledged.
- [ ] **Awaiting chunk approval / high-risk review** — chunk expanded; inline
      approve/reject present.
- [ ] **Final approval** — Finish & ship is the primary surface; `approve_final`
      ledger ("may commit/finish · Merges: Never").
- [ ] **final_approved / ready to push** — Finish & ship push step; `create_pr`
      ledger in supported modes; Chunk history collapsed.
- [ ] **local_only complete** — manual/out-of-app guidance; no in-app PR; Chunk
      history collapsed.
- [ ] **PR open / checks refresh** — PR link present; checks refresh only on the
      manual action.
- [ ] **Terminal failed** — gentle failure card → Details & audit; chunk history
      collapses only if no attention chunk.
- [ ] **Rejected** — "rejected and rolled back" card; Chunk history collapsed.
- [ ] **Multi-chunk completed / current / pending chunks** — active chunk expanded;
      completed-non-current and future-pending chunks compact (#36); whole panel
      collapses to history once the run is finished.

## 10. Regression checklist (legacy / #35 / #36 controls)

- [ ] **Plan approve / reject** works.
- [ ] **Execute / resume** works.
- [ ] **Patch retry** works when eligible.
- [ ] **Scope approve / reject** works when available.
- [ ] **Weak-test acknowledgement still blocks final approval** until acknowledged
      (Approve Final stays disabled while an acknowledgement is missing/stale).
- [ ] **Chunk approve / reject** works.
- [ ] **Final approval** works.
- [ ] **Push / create PR works only in supported PR modes** (`github_cli` /
      `manual_token`); never in `local_only`.
- [ ] **`local_only` shows manual / out-of-app guidance** and no fake in-app PR
      path.
- [ ] **PR checks refresh remains manual** — nothing polls automatically.
- [ ] **Memory conflict override / reject** still works (if reproducible), including
      the override-once continuation.
- [ ] **Details & audit** expands / collapses (default-closed) and preserves the
      timeline, memory provenance, and provider diagnostics.
- [ ] **OperatorAttentionPanel actions** still work (wired actions reuse existing
      mutations; previews / blocked explanations unchanged).

## 11. Known limitations / deferred work

- **Full state-gated Tier 2 context cards are not implemented.** The design's
  discrete per-decision context clusters (plan / running / patch_failure / scope /
  weak_test / chunk_review / finish_ship) are not built; #37D2 delivered only the
  terminal/final chunk-history collapse. See
  [`docs/design/state-gated-tier2-run-detail.md`](../design/state-gated-tier2-run-detail.md).
- **Approval-diff / context-placement audit is deferred (#37D3).** The "Human
  Approval Required" diff card still renders separately from the active chunk's
  context; relocating it touches a live approval surface and is a separate slice.
- **Dark evidence panes / design-token migration are deferred (#37D4).** No
  paper/ink/copper palette, IBM Plex, dark diff/scope panes, or big-number verdict;
  the app keeps its current shadcn styling.
- **Some operator actions remain display-only previews** because their required
  IDs / targets are ambiguous from the action alone (`retry_patch`,
  `approve_chunk`, `approve_scope_expansion`, `reject_scope_expansion`,
  `acknowledge_test_validation`).
- **Scope / acknowledgement / retry / chunk-approval controls remain in their
  detailed components** (`ScopeExpansionBanner`, `TestValidationAckPanel`,
  `PatchFailureBanner`, `InlineChunkApprovalControls`), where their chunk/request
  context lives.
- **No frontend test runner is configured**, so this manual smoke is the coverage
  for the UI composition; backend behavior remains covered by the existing suites.

## 12. Closeout criteria

- [ ] This smoke doc exists.
- [ ] `git diff --check` passes.
- [ ] No code / runtime behavior changed (docs-only slice).
- [ ] Manual smoke performed, or explicitly scheduled before the demo.
- [ ] No safety regression found (Section 3 all true).
- [ ] With the above green, **#37 can be considered complete enough for the
      local-first / demo phase** unless smoke finds a specific issue; remaining
      items are tracked under Section 11 (deferred #37D3 / #37D4 and the larger
      state-gated Tier 2 reorg).

### Docs lint / build

No docs linter or docs build is configured in this repo (the `docs/` tree is plain
Markdown). For this docs-only slice, run `git diff --check` only; there is no docs
build/lint step to run.
