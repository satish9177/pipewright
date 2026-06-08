# Active Chunk / ChunkPlanPanel Guided UX (#36)

> Status: design / planning (docs-only). No frontend, backend, API, schema,
> route, or package changes are introduced by this document. It is the
> source-of-truth input for the #36 implementation slices that follow. It builds
> directly on [`docs/design/run-detail-guided-ux.md`](./run-detail-guided-ux.md)
> (#35) and reuses its tier vocabulary.

## 1. Purpose

#36 is **Phase 2 of the Run Detail / pipeline UX redesign**. #35 reworked the
top of the page — the guided spine, the read-only Safety overview strip, the
collapsed "Details & audit" area, the wired primary / co-equal operator actions,
and the "Finish & ship" stepper. #36 turns its attention to **Tier 2: the
context a human needs for the current chunk decision**, which today lives almost
entirely inside one component: `ChunkPlanPanel`.

Tier recap (from #35):

- **Tier 1 — What now / guided action.** Mostly done in #35 (header,
  Safety overview, `OperatorAttentionPanel`, Finish & ship).
- **Tier 2 — Context for the current decision.** **#36 focus.** "Which chunk
  matters, what changed, what is the evidence, what action is needed."
- **Tier 3 — Details & audit.** Done in #35D (Timeline / EventLog, memory
  provenance, provider diagnostics collapsed default-closed).

Like #35, **#36 does not rewrite behavior.** It reorders, summarizes, relabels,
and (only behind parity) consolidates the chunk/evidence area so a non-expert can
understand it. Every approval gate, acknowledgement gate, scope decision, and
audit surface that exists today must still exist and still work.

## 2. Current problem

`ChunkPlanPanel` (`frontend/src/components/ChunkPlanPanel.tsx`, ~645 lines) is
still essentially a backend console. In a single `Card` it renders, top to
bottom:

- Plan-level metadata (total chunks, current chunk, complexity, feature
  description).
- **Execution Controls** (Execute / Resume), shown once the plan is approved,
  plus the start-context-drift warning.
- A flat list of **every chunk**, each rendering its own: title/description,
  status badge, scope warnings, risk level, token estimate, human-review flag,
  files expected, depends-on, rationale, runtime test-validation banner, advisory
  review panel, scope-expansion banner, patch-failure banner / recovered-patch
  marker / completion summary / error, and inline high-risk chunk approval
  controls.
- Plan-level **approve / reject** controls (when awaiting approval).

The result:

- The component owns plan approval, execution, active-chunk review, failure
  recovery, scope expansion, weak-test validation, advisory review, and
  completion summary all at once.
- Every chunk competes equally — the one chunk that needs a decision *now* is not
  visually dominant. A completed chunk and the failing chunk look the same weight.
- In final / completed states the whole chunk list is still expanded by default,
  competing with the #35 guided spine and Finish & ship card directly above it
  (rendered from `RunDetailPage.tsx` under the "Chunk Plan Details" section).

This is the exact density #35 left behind on purpose: #35I's own "Known
limitations" calls out that `ChunkPlanPanel` is still dense and nominates **#36**
to give it the guided treatment.

## 3. Design goal

After #36, a user looking at Tier 2 should be able to answer, in order, without
backend knowledge:

1. **Which chunk matters now?** — the active/current chunk is visually dominant.
2. **What changed?** — the chunk's intent, files, and (where relevant) diff/result.
3. **What evidence exists?** — runtime test verdict, advisory review summary.
4. **Are tests strong or weak?** — the test-validation verdict, never hidden.
5. **What did the reviewer find?** — advisory findings summarized, expandable.
6. **Is scope safe?** — approved scope vs. any requested expansion, clearly framed
   as a file-permission decision, not code approval.
7. **What action is needed?** — the single next safe step for this chunk (or "none,
   waiting on the system").
8. **Where can I inspect everything?** — all chunks and full technical detail are
   one click away, never deleted.

## 4. Current component map

What `ChunkPlanPanel` renders today (file/line anchors are approximate and for
orientation only):

| Area | Current location | Owns a real action? |
| --- | --- | --- |
| Plan metadata (totals, complexity, feature description) | `ChunkPlanPanel` header + grid (~191–218) | No (display) |
| **Execution Controls** (Execute / Resume) | `isApproved` block (~222–298) | **Yes** — `onExecute` / `onResume` |
| Start-context-drift warning | inside Execution Controls (~243–277) | No (display; recovery is manual) |
| Per-chunk header + status badge | chunk map (~350–367) | No (display) |
| Scope warnings (`[SCOPE]` notes, #22B) | chunk map (~369–391) | No (display) |
| Risk / token / human-review / files / depends-on / rationale | chunk map (~393–465) | No (display) |
| Runtime test-validation banner (#28E) | `RuntimeTestValidationBanner` (~470) | No (display) |
| Advisory AI review (#33C) | `AdvisoryReviewPanel` (~478) | No (display) |
| **Scope-expansion banner** (#27F) | `ScopeExpansionBanner` (~484–492) | **Yes** — approve/reject scope (self-owned) |
| **Patch-failure banner** (#18E/#26E2) | `PatchFailureBanner` (~500–507) | **Yes** — retry / reindex (self-owned + `onRetryChunk`) |
| Recovered-patch marker (#26E3) | `RecoveredReviewMarker` (~508–514) | No (display) |
| Completion summary / error | chunk map (~516–534) | No (display) |
| **Inline high-risk chunk approval** | `showInlineChunkApproval` (~537–584) | **Yes** — `onApproveChunk` / `onRejectChunk` |
| **Plan approve / reject** | `isAwaitingApproval` block (~606–640) | **Yes** — `onApprove` / `onReject` |

Sibling evidence components already exist and are display-only or self-owning:

- `RuntimeTestValidationBanner` — display-only verdict (strong/weak/none/unknown).
- `AdvisoryReviewPanel` — display-only advisory review; no actions.
- `ScopeExpansionBanner` — owns its own approve/reject scope mutation and copy.
- `PatchFailureBanner` — owns retry/reindex; eligibility decided by backend.
- `TestValidationAckPanel` — the weak-test acknowledgement gate, **rendered by
  `RunDetailPage` inside Finish & ship (#35H), not by `ChunkPlanPanel`.**
- `AttemptHistory`, `RecoveredReviewMarker` — display-only recovery context.

This matters for #36: most of the evidence is *already* in dedicated
subcomponents. The density problem is mainly **layout and hierarchy**, not
missing decomposition of the evidence widgets themselves.

## 5. UX problems ranked by risk / confusion

1. **Too many responsibilities in one component (highest).** Plan approval,
   execution, recovery, scope, validation, review, completion, and chunk approval
   all live in one `Card`. Any change risks all of them; a new user can't form a
   mental model.
2. **The active chunk is not dominant.** The chunk needing a decision now has the
   same visual weight as completed/pending chunks. The "current chunk" number is
   only a small metadata field at the top.
3. **All chunks compete equally with the current decision.** A 12-chunk run
   renders 12 full cards; the one awaiting approval / failed is buried in the
   middle.
4. **Weak-test / reviewer / completion evidence is verbose and can feel
   duplicated.** The test verdict appears in the chunk card *and* is summarized in
   the #35E Safety strip; advisory findings and completion summaries are long-form
   inline.
5. **Scope expansion can be misread as code approval.** `ScopeExpansionBanner`
   already carries strong "this is not code approval" copy, but in a dense list a
   skimming user could treat "Approve scope expansion and retry" as approving the
   change. Framing must stay explicit (Section 9).
6. **Chunk-specific recovery needs chunk context — which is why #35 left the top
   actions as previews.** `retry_patch`, `approve_chunk`,
   `approve_scope_expansion`, and `acknowledge_test_validation` all need a chunk
   number / request id / failure-report id that `operator_state` does not carry,
   so the real controls live here in Tier 2. #36 must keep them here and keep them
   real; it must not fake a top-level wired action.
7. **Final / completed runs still show too much chunk detail by default.** After
   final approval, the full expanded chunk list sits below Finish & ship adding
   noise to a "done" state.

## 6. Proposed new hierarchy

A display/composition hierarchy for Tier 2 (names are design labels, not a
committed file layout):

- **`ActiveChunkCard`** — the one chunk that matters now, visually dominant. Shows
  the condensed decision context and hosts (or links directly to) the real action
  for that chunk.
- **`CurrentDecisionEvidence`** — the evidence cluster for the active chunk: test
  verdict, advisory review summary, scope status, failure/recovery context.
- **`ChunkEvidenceSummary`** — a compact, reusable one-line/at-a-glance summary of
  a chunk's evidence (status, risk, test verdict, review verdict) used in the list.
- **`CompactChunkList` / `AllChunksAccordion`** — the remaining chunks as compact
  rows; completed chunks collapsed by default, expandable to today's full detail.
- **`RecoveryContext`** — the framing wrapper for patch-failure and scope-expansion
  states, reusing `PatchFailureBanner` / `ScopeExpansionBanner` unchanged.
- **`TechnicalChunkDetails`** — the verbose fields (token estimate, depends-on,
  rationale, raw completion summary, technical diagnostics) collapsed by default.

Important: these are **composition layers over existing components**, not
rewrites. `RuntimeTestValidationBanner`, `AdvisoryReviewPanel`,
`ScopeExpansionBanner`, `PatchFailureBanner`, `AttemptHistory`, and
`RecoveredReviewMarker` are reused as-is wherever possible.

## 7. Active chunk card design

`ActiveChunkCard` shows, for the single active/current chunk:

- Chunk number, title, and friendly status (reuse `getStatusDisplay`).
- One-line description (from `definition.description`).
- Files expected vs. touched (files attempted on failure), kept short.
- Risk level + human-review flag.
- **Scope status** — in scope, or "expansion requested" (links to `RecoveryContext`).
- **Test validation verdict** — strong/weak/none/unknown via
  `RuntimeTestValidationBanner` (or its condensed summary).
- **Advisory review summary** — verdict + headline from `AdvisoryReviewPanel`,
  with full findings behind an expander.
- **Completion summary, condensed** — short form; full text under
  `TechnicalChunkDetails`.
- A clear link/accordion to "full chunk details" and to "all chunks".

The card must never *invent* a decision. If the active chunk has no pending
decision (e.g. running), it states that and shows no action. Which chunk is
"active" should derive from existing data (`plan.current_chunk_number`, and the
chunk whose `status` is `awaiting_chunk_approval` / `failed` / `running`), not a
new backend field.

## 8. State-specific behavior

| State | Active chunk card / Tier 2 behavior |
| --- | --- |
| Awaiting chunk plan approval | No single "active chunk"; Tier 2 shows the plan summary + the real plan approve/reject (today's `isAwaitingApproval` block). Chunks shown compact for review. |
| Plan approved / ready to execute | Execution Controls remain real and reachable; active chunk = next to run; no chunk decision pending. |
| Running | Active chunk = the running chunk; status "running"; no action; "waiting on the system." |
| Patch failure, retry **unavailable** | `RecoveryContext` shows `PatchFailureBanner` (no retry button); manual-intervention copy preserved. |
| Patch failure, retry **available** | `RecoveryContext` shows `PatchFailureBanner` with the real Retry (`onRetryChunk`); backend decides eligibility. |
| Scope expansion needed | `RecoveryContext` shows `ScopeExpansionBanner` as the primary action, framed per Section 9; normal Retry suppressed (today's `pendingScope` rule). |
| Weak / no-test acknowledgement needed | Tier 2 surfaces the test gap on the chunk; the **real acknowledgement stays in `TestValidationAckPanel`** (Finish & ship, #35H). Tier 2 must point to it, not duplicate the gate. |
| Awaiting chunk approval / high-risk review | Active chunk dominant; inline approve/reject (`onApproveChunk` / `onRejectChunk`) preserved, plus advisory review summary. |
| Chunk completed | Compact summary; full detail collapsed. |
| Final approval | Tier 2 recedes; Finish & ship (#35H) above is primary; chunk detail collapsed by default. |
| `local_only` complete | No push controls anywhere; chunk list collapsed; terminal guidance owned by `RunDetailPage`. |
| Terminal failed | Failure context (last failed chunk) reachable; pointer to Details & audit; nothing pushed/merged. |

## 9. Scope expansion rule (explicit)

Scope expansion is a **file-permission decision, not code approval.** This is a
safety-critical framing and #36 must not weaken it.

- The **primary** UI for a pending scope expansion shows **originally approved
  files vs. requested extra files** (`ScopeExpansionBanner` already does this:
  "Currently approved scope" vs. "Files the previous attempt tried to touch").
- The explicit "Approving scope expansion is **not** code approval" note must
  remain.
- Any attempted-diff / patch preview, **if** ever shown, must be **secondary and
  clearly labeled blocked / not applied / not approved.** Approving scope only
  re-runs the chunk with extra files allowed; the recovered chunk still goes
  through normal chunk approval before any commit.
- The action label must keep distinguishing "approve scope expansion and retry"
  from chunk approval.

## 10. Diff / evidence guidance

How evidence should be prioritized per state:

- **Normal chunk approval** — the diff/result is legitimate primary evidence for
  the decision.
- **Scope expansion** — the **requested file list is primary**; any attempted diff
  is **secondary** and labeled not-applied (Section 9).
- **Weak / no test** — the **test gap must remain visible** and must not be
  visually minimized; it never silently disappears because other evidence looks
  positive. (The gate itself stays in `TestValidationAckPanel`.)
- **Advisory review** — **summarize findings first** (verdict + headline),
  full findings/notes behind an expander; keep the "advisory only — does not
  approve, reject, or block anything" disclaimer and stale-review demotion.

## 11. Implementation-safe roadmap

Small, independently shippable slices, each frontend-only unless explicitly noted,
each preserving behavior and parity:

- **#36A — docs-only audit/design** (this document).
- **#36B — split `ChunkPlanPanel` into pure subcomponents** with **no visual or
  behavior change** (pure extraction; same DOM/props/handlers). Parity is the
  acceptance bar.
- **#36C — `ActiveChunkCard` display-only above the existing chunk list.** Adds an
  at-a-glance card; the existing list and all real controls stay below and
  unchanged.
- **#36D — compact all-chunks list / collapse completed chunk details** by
  default; full detail one click away.
- **#36E — recovery-context polish** for patch failure and scope expansion
  (framing/copy/layout only; reuse `PatchFailureBanner` / `ScopeExpansionBanner`).
- **#36F — weak-test / advisory evidence density polish** (summary-first,
  expandable; no gate changes).
- **#36G — smoke / docs closeout** (manual smoke checklist, mirroring #35I).

Ordering rationale: extract first (#36B) so later slices compose cleanly; add the
display-only card (#36C) before collapsing the list (#36D) so nothing is hidden
before its summary exists.

## 12. Testing / safety requirements

- **No backend changes** unless explicitly justified and called out (none expected
  in #36B–#36F).
- **No route / API / schema / package changes** in the early slices.
- **Legacy controls remain** until parity is proven; #36B is a pure extraction.
- **Retry / scope / ack / chunk-approval behavior must not change.** Eligibility
  stays backend-owned; the frontend never replicates allowlists.
- **Chunk-specific actions must never be guessed without their IDs.** The real
  controls stay in Tier 2 because that is where the chunk number / `request_id` /
  `failure_report_id` are available; no top-level fake wired action is introduced
  (consistent with `frontend/src/lib/operatorPrimaryAction.ts`).
- **Existing safety banners must remain available**: scope, patch failure,
  runtime test validation, advisory review, weak-test acknowledgement, start-
  context drift.
- **Validation per slice:** `cd frontend && npx eslint <changed files>`,
  `npm.cmd run build` (`tsc -b && vite build`), and `git diff --check`. No
  frontend test runner is configured, so UI composition is covered by manual
  smoke (#36G).

## 13. Non-goals

- No new backend behavior.
- No new `operator_state` action wiring unless the IDs/targets are explicit
  (scope/ack/retry/chunk-approval stay where their context lives).
- No automatic retry.
- No auto approval of plans, chunks, scope, or final.
- No removal of chunk audit details — only collapsing/summarizing.
- No hiding of weak-test, reviewer, or scope warnings.
- No full frontend redesign or design-system migration.

## 14. Closeout criteria

- A clear Tier 2 design exists (this document) that downstream slices can follow.
- The implementation slices (#36B–#36G) are safe, incremental, and each pass
  eslint / build / `git diff --check` with no safety regression.
- Future implementation can move Tier 2 toward the Claude Design target **without
  weakening any approval gate, scope decision, acknowledgement gate, or audit
  surface**, and without faking chunk-specific actions.

## Related docs

- [`docs/design/run-detail-guided-ux.md`](./run-detail-guided-ux.md) — #35 Tier
  1/3 redesign (parent).
- [`docs/design/operator-state-attention-panel.md`](./operator-state-attention-panel.md)
  — the read-only operator state behind the guided spine.
- [`docs/design/scope-expansion-recovery.md`](./scope-expansion-recovery.md) —
  scope-expansion approve/reject behavior.
- [`docs/design/stronger-test-validation.md`](./stronger-test-validation.md) —
  runtime test verdict + acknowledgement gate.
- [`docs/design/adversarial-reviewer-stage.md`](./adversarial-reviewer-stage.md)
  — advisory AI review overlay.
- [`docs/testing/run-detail-guided-ux-smoke.md`](../testing/run-detail-guided-ux-smoke.md)
  — #35 smoke / closeout checklist (model for #36G).

### Docs lint / build

No docs linter or docs build is configured in this repo (the `docs/` tree is
plain Markdown). For this docs-only slice, run `git diff --check` only; there is
no docs build/lint step to run.
