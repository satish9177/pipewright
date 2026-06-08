# Active Chunk / ChunkPlanPanel Guided UX Smoke / Closeout Checklist (#36)

Manual smoke validation and closeout record for **#36C–#36F — the guided
ChunkPlanPanel (Tier 2) current-chunk experience**. This is a checklist, not an
automated suite: the frontend has no test framework configured yet, so these UI
steps are manual. They complement the existing backend tests (approvals, scope
expansion, test validation/acknowledgement, advisory review, patch retry, and the
`operator_state` read model) and the #35 Run Detail smoke checklist.

Related docs:

- Design / audit: [`docs/design/active-chunk-guided-ux.md`](../design/active-chunk-guided-ux.md)
- Parent Run Detail design: [`docs/design/run-detail-guided-ux.md`](../design/run-detail-guided-ux.md)
- #35 Run Detail smoke: [`docs/testing/run-detail-guided-ux-smoke.md`](./run-detail-guided-ux-smoke.md)
- Scope expansion smoke: [`docs/testing/scope-expansion-recovery-smoke.md`](./scope-expansion-recovery-smoke.md)
- Stronger test validation smoke: [`docs/testing/stronger-test-validation-smoke.md`](./stronger-test-validation-smoke.md)

## Completed #36 Work

- #36A `docs/design/active-chunk-guided-ux.md` design / audit plan — merged
- #36B `ChunkPlanPanel` split into pure same-file subcomponents
  (`ChunkPlanSummary`, `ExecutionControls`, `ChunkScopeWarning`,
  `InlineChunkApprovalControls`, `ChunkCard`, `PlanApprovalControls`) with no
  visual / behavior change — merged
- #36C display-only `ActiveChunkCard` ("Current chunk at a glance") above the
  full chunk list — merged
- #36D compact all-chunks list: non-attention chunks collapse by default,
  attention / decision chunks stay fully expanded (`CollapsibleChunkCard`) —
  merged
- #36E recovery-context polish: `ScopePermissionContext` and
  `PatchRecoveryContext` framing around the scope-expansion and patch-failure
  banners — merged
- #36F evidence-summary framing: `ChunkEvidenceSection` chip row above the
  runtime test-validation and advisory-review banners — merged
- This smoke / closeout checklist (docs only) — #36G

## 1. Purpose

This checklist validates that `ChunkPlanPanel` (Run Detail **Tier 2**) now reads
as a guided, scannable current-chunk view after #35, **without** weakening safety,
hiding warnings, or changing any action behavior.

Concretely, it confirms:

- **Current chunk is easy to scan** — the active / current chunk surfaces "at a
  glance" (`ActiveChunkCard`) and stays expanded in the list, while finished /
  future chunks collapse to a compact row.
- **No safety / action regression** — every approval, retry, scope, acknowledgement,
  and final/PR gate behaves exactly as before; the guided framing is presentation
  only.
- **Warnings stay visible** — weak / no-test, advisory findings, scope-permission,
  and patch-failure context are all still shown in full; nothing critical is hidden
  behind a collapse.
- **Full detail is one click away** — collapsed chunks expand to the exact original
  `ChunkCard`; #35 Details & audit remains reachable.

## 2. Safety guarantees (must remain true)

- [ ] **No backend behavior changes** — #36 is frontend-only; no API / schema /
      route / package changes.
- [ ] **No approval / retry / scope / acknowledgement / final / PR behavior
      changes** — every mutation is the same as before #36.
- [ ] **`ActiveChunkCard` is read-only** — it contains no action buttons and wires
      nothing; it points to the full chunk details below.
- [ ] **Collapsed chunks never hide attention / decision chunks** — active, failed,
      pending-scope, awaiting-approval, running, and recovery chunks always render
      the full `ChunkCard` and are never collapsed.
- [ ] **Scope expansion remains a file-permission decision, not code approval** —
      the framing and banner copy keep this distinction explicit.
- [ ] **Weak-test and advisory warnings remain visible** — the evidence summary
      chips are additive; the full banners still render with full warning copy.
- [ ] **Full details remain one click away** — every collapsed chunk has a
      "Show details" affordance that reveals the original card.
- [ ] **#35 Details & audit remains available** — timeline / memory provenance /
      provider diagnostics are unchanged and still reachable.

## 3. Pre-smoke setup

- [ ] Start backend and frontend (see the repo README / local dev scripts).
- [ ] Use a disposable **smoke project** with a real git repo.
- [ ] Have at least one run with a **single chunk** and, ideally, one with
      **multiple chunks** (so collapse / expand behavior can be observed).
- [ ] Note the project **PR mode** (`local_only`, `github_cli`, `manual_token`);
      the final-stage checks branch on it.
- [ ] If practical, reproduce states for: **weak / no-test validation**, **advisory
      review** (ideally one `needs_human_attention` / `risky` and one
      `self_review`), **patch failure** (retry available and unavailable), **scope
      expansion pending**, and **awaiting chunk approval / high-risk review**.

## 4. Smoke states to verify

Drive a run through as many states as your environment can reproduce. For each,
look at the `ChunkPlanPanel` (Tier 2) area: the `ActiveChunkCard` first, then the
chunk list.

- [ ] **Awaiting chunk plan approval** — plan summary + `PlanApprovalControls`
      (approve / reject) present; chunks render; approve/reject unaffected by #36.
- [ ] **Plan approved / ready to execute** — `ExecutionControls` (Execute / Resume)
      present and functional; start-context-drift warning still surfaces when
      relevant.
- [ ] **Running chunk** — the running chunk is the active chunk: `ActiveChunkCard`
      shows it and it stays expanded in the list.
- [ ] **Completed current chunk** — when `current_chunk_number` points at a
      completed chunk, it is the active chunk and stays expanded.
- [ ] **Completed non-current chunk** — collapses to a compact row by default.
- [ ] **Future pending chunk** — collapses to a compact row by default.
- [ ] **Patch failure / retry available** — full `ChunkCard`, "Patch recovery"
      context, and the real Retry button (from `PatchFailureBanner`) appear.
- [ ] **Patch failure / retry unavailable** — context still explains the failure;
      no retry button is offered (eligibility unchanged).
- [ ] **Scope expansion pending** — "Scope permission request" framing +
      `ScopeExpansionBanner` approve/reject; patch-failure banner (if any) is the
      secondary diagnostic with no competing retry.
- [ ] **Weak / no-test validation** — amber "Tests: weak / none" chip + full amber
      `RuntimeTestValidationBanner`.
- [ ] **Advisory review needs attention / independent reviewer** — verdict +
      independence chips appear; full `AdvisoryReviewPanel` still renders.
- [ ] **Awaiting chunk approval / high-risk review** — chunk stays expanded;
      `InlineChunkApprovalControls` (approve / reject chunk) present.
- [ ] **Final approval** — handled by #35 Finish & ship above the plan; #36 did not
      move it.
- [ ] **`local_only` complete** — terminal manual-push guidance from #35; no in-app
      push button introduced by #36.
- [ ] **Terminal failed** — gentle failure copy from #35; chunk list still shows the
      relevant chunk's failure context.

## 5. ActiveChunkCard checks (#36C)

- [ ] **Renders for the active / current chunk** — uses `current_chunk_number` when
      present, else the first chunk with an attention status.
- [ ] **Does not render a noisy card when no active chunk exists** — when nothing
      qualifies, the "Current chunk at a glance" card is absent (not an empty box).
- [ ] **Shows chunk number / title / friendly status badge.**
- [ ] **Shows expected files summary** (truncated, "+N more").
- [ ] **Shows risk level and human-review requirement.**
- [ ] **Shows test / review / scope / failure / completion signals when available**
      as compact chips (mirroring, not replacing, the full banners below).
- [ ] **Contains no action buttons** — read-only; no approve / reject / retry /
      execute / scope controls.
- [ ] **Points users to full details below** — the "use the controls in the full
      chunk details below" note is present.

## 6. Compact list checks (#36D)

- [ ] **Active / current chunk is expanded** (full `ChunkCard`).
- [ ] **Failed chunk is expanded.**
- [ ] **Pending scope-expansion chunk is expanded.**
- [ ] **Awaiting-chunk-approval chunk is expanded.**
- [ ] **Running / in-progress chunk is expanded.**
- [ ] **Completed non-current chunks are collapsed by default.**
- [ ] **Future pending chunks are collapsed by default.**
- [ ] **Collapsed row still shows** chunk number / title, friendly status, risk,
      files summary, and test / review verdict chips when available.
- [ ] **"Show details" reveals the full original `ChunkCard`** (same content,
      controls, and banners as before #36).
- [ ] **"Hide details" re-collapses** a safe (non-attention) chunk.
- [ ] **No real controls are hidden for decision chunks** — any chunk with a
      control / pending decision is never collapsible.

## 7. Recovery-context checks (#36E)

- [ ] **Patch failure shows "Patch recovery" context** when the retry path is the
      active recovery path (no pending scope expansion).
- [ ] **Patch recovery explains nothing was committed** and that retry is available
      only when Pipewright allows it.
- [ ] **Retry eligibility and button behavior unchanged** — the real Retry stays
      inside `PatchFailureBanner`; when a scope expansion is pending the retry
      button is suppressed exactly as before, and "Patch recovery" framing is not
      shown (banner renders bare as the secondary diagnostic).
- [ ] **Scope expansion shows "Scope permission request"** framing above the banner.
- [ ] **Scope expansion clearly says file-permission decision, not code approval.**
- [ ] **Approved vs requested files remain visible** in `ScopeExpansionBanner`.
- [ ] **Scope approve / reject behavior unchanged** — same controls, same
      `onActionComplete` refresh.
- [ ] **No attempted diff / code preview is introduced as a primary approval** for
      scope expansion.

## 8. Evidence summary checks (#36F)

- [ ] **Test verdict chip appears** when `test_validation` data exists
      (strong / weak / none / unverified).
- [ ] **Weak / no-test remains amber and visible** — the chip is amber and the full
      amber `RuntimeTestValidationBanner` still renders below it.
- [ ] **Full `RuntimeTestValidationBanner` still appears** with its headline,
      reason, zero-tests note, and "informational only" line.
- [ ] **Advisory review verdict chip appears** (no blocking concern / needs
      attention / risky) for a completed review.
- [ ] **Reviewer independence / self-review / stale review signals appear** when
      available (independent / not independent / unverified, and a stale chip).
- [ ] **Findings count / highest severity chip appears** when findings exist
      (e.g. "3 findings · high").
- [ ] **Full `AdvisoryReviewPanel` still appears and remains advisory-only** —
      "Advisory only" note, full findings list, summaries, and recommended human
      check all present; no actions wired.
- [ ] **`TestValidationAckPanel` behavior remains in #35 Finish & ship, unchanged**
      — #36 did not move, duplicate, or alter the weak/no-test acknowledgement gate.

## 9. Regression checklist (legacy / #35 controls)

- [ ] **Plan approve / reject** works (`PlanApprovalControls`).
- [ ] **Execute / resume** works (`ExecutionControls`).
- [ ] **Patch retry** works when eligible (`PatchFailureBanner`).
- [ ] **Scope approve / reject** works (`ScopeExpansionBanner`).
- [ ] **Chunk approve / reject** works (`InlineChunkApprovalControls`).
- [ ] **Final approval and weak-test acknowledgement** still work from #35 Finish &
      ship.
- [ ] **PR / `local_only` behavior unchanged** — push / create PR only in supported
      modes; `local_only` shows guidance only.
- [ ] **Details & audit** still expands / collapses (default-closed) and is
      complete.
- [ ] **OperatorAttentionPanel actions from #35** still work (wired PROGRESS actions
      reuse existing mutations; previews / blocked explanations unchanged).

## 10. Known limitations / deferred work

- `ActiveChunkCard` is **read-only** by design; all real controls live in the full
  chunk details below.
- Scope-expansion, weak-test acknowledgement, patch retry, and chunk-approve **top
  actions** (OperatorAttentionPanel) remain **preview-only** when the required IDs /
  targets are ambiguous from the action alone (carried over from #35, not changed by
  #36).
- Some evidence intentionally appears in **both** the summary chips and the full
  banner — the chips are a scan aid, not a replacement; this duplication is by
  design.
- A **full diff viewer / richer code-review layout** is still future work; #36 does
  not introduce an applied/approvable diff preview.
- **Branch / index** and **memory provenance** signals remain governed by their
  existing read / lazy-load data availability (not summarized into the active-chunk
  view).
- No frontend test runner is configured, so this manual smoke is the coverage for
  the UI composition; backend behavior remains covered by the existing suites.

## 11. Closeout criteria

- [ ] This smoke doc exists.
- [ ] `git diff --check` passes.
- [ ] No code / runtime behavior changed (docs-only slice).
- [ ] Manual smoke performed, or explicitly scheduled before the demo.
- [ ] No safety regression found (Section 2 all true).
- [ ] With the above green, **#36 can pause** for the local-first / demo phase
      unless smoke finds a specific issue; remaining items are tracked under
      Section 10.

### Docs lint / build

No docs linter or docs build is configured in this repo (the `docs/` tree is plain
Markdown). For this docs-only slice, run `git diff --check` only; there is no docs
build/lint step to run.
