# Run Detail Guided UX Redesign (#35)

> Status: design / planning (docs-only). No frontend, backend, API, or route
> changes are introduced by this document. It is the source-of-truth input for
> Claude Design (#35B) and for the PR-by-PR implementation slices that follow.

## 1. Title and purpose

**#35 is a Run Detail / pipeline UX redesign — not a full frontend rewrite.**

The Run Detail page (`frontend/src/pages/RunDetailPage.tsx`) already works and is
functionally complete: every pipeline state can be driven to completion today.
The problem is not missing capability — it is that the page exposes too much of
the backend's internal vocabulary and scatters real actions across many panels,
so a new user cannot quickly tell what is happening or what is safe to click.

The goal of #35 is **a guided product experience, not more buttons.** We are not
adding features. We are reordering, summarizing, relabeling, and (later, behind
parity tests) consolidating the controls that already exist so that the page
guides the user to the single next safe action.

Explicit framing for everyone working on #35:

- This is a **UX/IA redesign of an existing, working page.**
- It must **preserve every safety guarantee, approval gate, and audit surface.**
- "Beautiful" here means **calm, understandable, and trustworthy**, not decorative.

## 2. Current problem

The Run Detail page feels scary because it surfaces backend concepts directly and
spreads decisions across the whole scroll:

- **Backend concepts are exposed raw.** Users are asked to reason about *resume*,
  *retry*, *final approval*, *weak validation*, *scope approval*, *chunk plan
  approval*, and *PR state* before they understand what any of it means or
  whether it is safe.
- **Real actions are scattered.** Approve/reject, Execute/Resume, per-chunk
  approve/reject/retry, scope-expansion approve/reject, weak-test acknowledge,
  final approve/reject, push/PR — these live in 5+ different panels at different
  scroll depths, mostly with the same visual weight.
- **The top panel already explains the next action, but cannot act.**
  `OperatorAttentionPanel` renders the backend's `operator_state` recommendation
  at the top of the page — title, explanation, and recommended actions — but its
  buttons are deliberately **display-only** ("Display only — use the existing
  controls below to act."). The user reads the recommendation, then has to hunt
  downward for the matching real control.
- **Users are forced to learn the backend model first.** Understanding the page
  currently requires understanding the pipeline's internal states. That is
  backwards: the UI should explain the model, not require it.

## 3. Core finding

**The guided next-action model already exists in the read model. The frontend
renders it as a dead preview.**

`ChunkPlanResponse.operator_state` (`OperatorState` in
`frontend/src/api/client.ts`) is computed on chunk reads and already provides:

- `title` — a plain, human-readable headline.
- `explanation` — what is happening, in prose.
- `waiting_on` — `human` / `system` / `nobody`.
- `decision_type` — `progress` / `risk_decision` / `none`.
- `primary_action`, `neutral_actions`, `secondary_actions` — offered actions.
- `blocked_actions` — what cannot be done yet, each with a `blocked_reason`.
- `safety_checks` — process-gate status (`passed` / `failed` / `weak` /
  `not_evaluated` / `not_applicable`).
- `trust_facts` — auditable context facts.
- `out_of_app_instruction` — e.g. a manual git command to run outside Pipewright.
- `unknown_state_warning` — set when the backend is not confident about state.

Implications for the redesign:

1. **Promote `operator_state` into the page spine.** It should become the primary
   thing the user reads, not a banner above the real controls.
2. **Eventually make its actions real** by mapping each `OperatorAction.id` to the
   mutation the corresponding legacy button already calls — but only **after
   parity tests prove the mapping**, and only while keeping legacy controls live.
3. This makes #35 largely a **wiring and ordering problem, not a new-design
   problem.** Most of the "guided experience" is already computed server-side.

Open question to confirm during implementation (not a backend change request):
verify each `OperatorAction.id` maps 1:1 onto an existing route in
`frontend/src/api/runs.ts` / `client.ts`. Where an action has no backing route,
it stays a preview/blocked item — never fabricate a call.

## 4. Product goal

A user should be able to answer all of the following **within ~5 seconds** of
opening the page:

- **What is happening?** (plain status, not an enum)
- **Is it safe?** (summarized safety signal)
- **What needs my attention?** (is it waiting on me, or on the system?)
- **What is the next best action?** (one clear thing)
- **What happens if I click the main button?** (does it commit? push? just retry?)
- **Where can I inspect technical details?** (audit is one click away)

If any of these takes a scroll or backend knowledge to answer, the redesign has
not met its goal.

## 5. Design principles

1. **One clear next action.** The page should point to a single primary step
   whenever the state allows it.
2. **Human-friendly copy, not backend enum language.** No `RUNNING_CHUNKS`,
   `FINAL_APPROVED`, `AWAITING_SCOPE_APPROVAL` in headlines. Raw enums may remain
   in tooltips / audit views for traceability.
3. **Safety is summarized at the top but never hidden.** A top safety strip
   summarizes and links down to the full warning; it never replaces it.
4. **Technical details remain available but do not dominate.** Timeline,
   provenance, and diagnostics move into a collapsed "Details & audit" section —
   preserved, not removed.
5. **Risk decisions show co-equal choices, not a recommended default.** When
   `decision_type` is `risk_decision`, present neutral, equal-weight options and
   keep the honest "Pipewright does not recommend one over the other" framing.
6. **Do not invent frontend actions the backend does not expose.** The UI may
   only offer actions present in `operator_state` / backed by an existing route.
7. **Do not weaken auditability.** Every fact, event, and diagnostic visible
   today must remain reachable.
8. **Do not hide critical warnings.** Weak-test, scope violation, self-review
   (non-independent), budget-dropped safety memory, repo-reality-stale memory,
   and `unknown_state_warning` must each remain individually visible on expand.

## 6. Proposed page hierarchy (three tiers)

```
HEADER
  Pipeline Run · <feature, 1 line>            [friendly state badge]
  (raw run id demoted to tooltip / audit footer)

TIER 1 — WHAT NOW  (always visible; the only thing most users need to read)
  • Plain title + explanation                 (from operator_state)
  • Page mood from waiting_on                  (human / system / nobody)
  • SAFETY STRIP                               (summarized safety_checks +
                                                unknown_state_warning, links down)
  • PRIMARY next action                        (one CTA when progress)
  • Co-equal / secondary actions               (when risk_decision)
  • "What's blocked, and why"                  (blocked_actions)

TIER 2 — CONTEXT FOR THIS DECISION  (state-gated; only when relevant)
  • The diff / chunk being approved
  • Active chunk detail
  • Runtime test verdict + advisory review for the active chunk

TIER 3 — DETAILS & AUDIT  (collapsed by default; one click away; never removed)
  ▸ Full chunk list (entire plan)
  ▸ Timeline / event log
  ▸ Memory provenance & diagnostics
  ▸ Provider / environment diagnostics
  ▸ Run id, branch, raw technical fields
```

Key moves: **promote** the operator panel into the spine; **demote** Timeline +
Memory Provenance + Provider Diagnostics into one collapsed accordion; **gate**
Tier 2 strictly on the active state so only relevant context shows.

## 7. Current component map

Render order in `RunDetailPage.tsx` (top → bottom):

| # | Region | Component | Has real actions? |
|---|--------|-----------|-------------------|
| 1 | Header (status + raw UUID) | inline / `RunStatusBadge` | No |
| 2 | Run Summary | inline + `StepIndicator` | No |
| 3 | Operator Attention (preview) | `OperatorAttentionPanel` | **No — deliberately dead buttons** |
| 4 | Memory Suggestions (terminal only) | `RunMemorySuggestions` | Yes |
| 5 | Chunk Plan Details | `ChunkPlanPanel` (+ scope, patch-failure, runtime-test, advisory-review, ack banners) | **Yes — busiest region** |
| 6 | Human Approval (legacy gate) | inline card | Yes |
| 7 | Memory Conflict | `MemoryConflictPanel` | Yes |
| 8 | Final Approval + PR | `TestValidationAckPanel` + `FinalApprovalPanel` + `PushPrPanel` + `PrStatusPanel` | Yes (×4) |
| 9 | Terminal outcome (complete / failed / rejected) | inline cards | No |
| 10 | Timeline | `EventLog` | No |
| 11 | Memory Diagnostics | `RunMemoryProvenancePanel` | No (load only) |
| 12 | Environment | `ProviderDiagnosticsPanel` | No (refresh only) |
| 13 | Back | button | — |

Roughly **13 stacked regions, ~10 renderable at once, with action buttons in
regions 4–8** — plus a non-interactive preview of those same actions in region 3.

## 8. UX problems ranked by confusion / risk

1. **Two parallel action systems.** `operator_state` recommends the next step with
   dead buttons; the live buttons are scattered elsewhere. This is the core of
   "I don't know what to click."
2. **Execute vs Resume confusion.** "Execute Chunks" and "Resume Run" sit side by
   side, both enabled when the plan is approved, with no guidance on which is
   correct. Picking wrong feels dangerous (even though it is safe).
3. **Backend vocabulary leaks everywhere.** `formatStatusLabel` uppercases the raw
   enum (`AWAITING_CHUNK_PLAN_APPROVAL`, `RUNNING_CHUNKS`, `FINAL_APPROVED`,
   `PUSH_FAILED`); `[PROGRESS]` / `[RISK DECISION]` tags, `N/E` / `N/A` safety
   codes, and `advisory_only=true` style fields are shown to end users.
4. **Too many approval surfaces look identical.** Plan approval, high-risk chunk
   approval, final approval, scope-expansion approval, and weak-test
   acknowledgement all use the same green button — five different meanings, one
   visual weight. None of them commit code, but the UI doesn't reassure that.
5. **Diagnostics always visible.** Timeline, Memory Diagnostics, and Provider
   Diagnostics render at full weight for every run in every state, pushing the
   actual decision off-screen and signaling "this is complicated."
6. **Safety signals buried.** Weak-test, self-review, scope violation,
   budget-dropped safety memory, repo-reality-stale memory, and
   `unknown_state_warning` are each isolated in their own card; there is no single
   "what you must know before approving" strip.
7. **No primary/secondary hierarchy.** On a single screen the user can see ~6
   near-equal-weight enabled buttons. The page never says "do this one thing."
8. **Terminal states too abrupt.** `failed` → "Check the terminal for error
   details" (a CLI instruction in a web UI). Legacy/empty states leak internal
   history ("Legacy runs or interrupted runs may not have chunk plan data").
9. **Raw UUID prominence.** The run UUID is the most prominent subheading in the
   header — high prominence, near-zero user value, machine-console tone.

## 9. Recommended next-action model

The model already exists in `operator_state`. The UI should become a thin renderer
of it. The only behavioral change (later, slices #35F/#35G) is letting the offered
actions be clickable.

```
operator_state.waiting_on      → page mood
   human   → amber "It's your move"        (show actions)
   system  → blue  "Pipewright is working" (spinner + live step, hide actions)
   nobody  → green "Done / nothing needed"

operator_state.decision_type   → button hierarchy
   progress       → ONE primary CTA
   risk_decision  → co-equal neutral buttons, NO recommended default
   none           → no buttons

operator_state.primary_action / neutral_actions / secondary_actions
   → map action.id → the existing mutation already wired in RunDetailPage
   → primary = filled, secondary = outline, advanced = ghost / "More"

operator_state.blocked_actions → "Can't do yet" list with blocked_reason
                                  (never a live button)

operator_state.safety_checks   → the top safety strip (summarized, links down)

operator_state.unknown_state_warning
   → fail-closed: show a cautious, non-committal banner and HIDE the primary CTA;
     fall back to existing explicit controls
```

Contracts to preserve:

- **Never invent an action `operator_state` didn't offer.**
- **Never present a recommended default for a `risk_decision`.**
- **If `operator_state` is missing/null** (legacy/older runs), degrade gracefully
  to the existing explicit controls.

## 10. State-by-state UX copy direction

Principles: lead with what is true and what is safe; name the one next action;
reassure whether approving here does or does not commit; keep raw enums out of the
headline. Copy is illustrative — final wording belongs in one shared status-copy
map, not scattered across components.

| State | Friendly badge | Headline | Body / reassurance | Primary action |
|---|---|---|---|---|
| **awaiting chunk plan approval** | "Needs your review" | "Review the plan before any code is written" | "Pipewright split your request into chunks. Approving lets it start — no files change yet." | Approve plan (+ Reject) |
| **ready to execute chunk** | "Ready to run" | "Plan approved — ready to start" | "You'll review results before anything is committed." | Start execution |
| **running** | "Working…" | "Pipewright is working on chunk N of M" | Live step + spinner; hide approve/reject. "Nothing to do right now." | (none) |
| **patch failure** | "A step failed safely" | "A code change couldn't be applied" | Plain explanation. "Nothing was committed." Show rollback / tree state. | Retry code change (if eligible) else View details |
| **retry available** | "You can retry" | "Try generating this change again" | "Retry asks the AI to redo it. May fail again; nothing commits until you review." | Retry code change |
| **scope expansion needed** | "Out-of-scope change blocked" | "This chunk tried to edit files outside its approved scope" | "Approving is NOT code approval — it only lets Pipewright retry with these files." | Approve scope & retry (+ Reject), co-equal |
| **weak/no test acknowledgement needed** | "Tests didn't really verify this" | "Acknowledge weak test validation to continue" | "You may proceed, but must acknowledge per chunk. Acknowledging is not code approval." | Acknowledge (per chunk) |
| **awaiting chunk approval** | "High-risk chunk — your call" | "Review this chunk before execution continues" | Show diff + advisory review + test verdict for this chunk. | Approve chunk (+ Reject) |
| **final approval** | "Final sign-off" | "Approve the finished work before it leaves your machine" | "Last gate before push/PR. Approving authorizes the commit/PR step." Surface ack-blocking inline. | Approve & finish (+ Reject) |
| **PR ready** | local: "Committed locally" / gh: "Ready to push" | local: "Your changes are committed to `<branch>`" / gh: "Ready to push and open a PR" | local: manual push hint. gh: "Pushing never auto-merges." | local: (none) / gh: Push & create PR |
| **PR open** | "PR open" | "Pull request created" | Link "Open on GitHub". "Checks aren't fetched automatically." | Refresh PR checks (secondary) |
| **push failed** | "Push failed" | "Couldn't push to GitHub" | Sanitized error + next action. "Your commit is safe locally." | Try push again (if retryable) |
| **terminal failed** | "Run failed" | "This run stopped at: `<friendly step>`" | "See Timeline below for what happened. Nothing was pushed." | View timeline |
| **branch/index stale warning** | "Repo may have changed" | "Pipewright's view of your repo may be out of date" | "Recently added/removed files may be missing. Re-index, or switch to the run branch." | Re-index / Show branch command |

## 11. Component recommendations

- **Promote `OperatorAttentionPanel` into "What to do next."** Make it the page
  spine; eventually wire its actions (behind parity tests).
- **Collapse Timeline, Memory Provenance, and Provider Diagnostics into a single
  default-closed "Details & audit" section.** Nothing removed; show counts on the
  affordance (e.g. "Timeline (42 events)") so audit is obviously reachable.
- **Merge `FinalApprovalPanel` + `PushPrPanel` + `PrStatusPanel` (later) into one
  "Finish & ship" card** with internal steps (approve → push → PR status). They
  already share a section and badge; three cards for one linear flow is redundant.
- **Keep the existing safety banners** (`RuntimeTestValidationBanner`,
  `AdvisoryReviewPanel`, `ScopeExpansionBanner`, `TestValidationAckPanel`,
  `PatchFailureBanner`). Their copy is already careful and honest — they need
  **repositioning, not rewriting**.
- **Split `ChunkPlanPanel` only late in the sequence.** It currently owns plan
  approval + execution controls + per-chunk cards + five embedded banners +
  retry/scope/ack wiring. Decomposing it (Plan / Execution / ChunkList /
  ActiveChunk) is the highest-leverage but highest-regression-risk change, so it
  comes after the guided model is trusted.

## 12. Implementation roadmap

Safe, incrementally mergeable sequence. Each slice preserves all behavior and all
audit surfaces.

- **#35A — Docs-only UX planning document.** (This file.)
- **#35B — Claude Design.** Visual/product design using this doc as input.
- **#35C — Friendly labels, calmer header, terminal outcome cleanup.** Display-only
  label map; demote raw UUID; consolidate terminal cards. Zero logic change.
- **#35D — Collapsed "Details & audit" section.** Wrap Timeline + Memory Provenance
  + Provider Diagnostics in a default-closed accordion. No data removed.
- **#35E — Read-only safety strip.** Summarize `safety_checks` +
  `unknown_state_warning` at the top, linking down to full banners. No new actions.
- **#35F — Wire `operator_state.primary_action`** to the existing mutation while
  **keeping legacy controls rendered**. Pivotal slice; gate behind parity tests.
- **#35G — Wire secondary / co-equal actions** + render `blocked_actions`
  (non-interactive), with parity tests per action id.
- **#35H — Merge finish/ship cards** into "Finish & ship".
- **#35I — Split `ChunkPlanPanel` / active-chunk card.** Largest blast radius; last.
- **#35J — Final visual polish + smoke checklist.**

Stop-and-ship points: after **#35D** the page is already calmer with zero behavior
change; after **#35G** it is genuinely guided; **#35H–#35J** are polish/refactor.
Legacy controls are only removed once #35F/#35G are proven at parity.

## 13. Testing and safety requirements

- The existing **frontend build must pass** (`cd frontend; npm.cmd run build`).
- **Touched-file lint** should pass (`ruff` is backend-only; for frontend use the
  project's configured eslint/tsc on changed files).
- **No backend tests are required** for the docs-only slice (#35A) or for purely
  display-only slices that touch no backend code.
- **Action-wiring slices (#35F/#35G) must include tests proving each
  `operator_state` action id maps to the same mutation/route as the legacy
  button** it replaces. A mismapped action is the primary regression risk.
- **Legacy controls must remain live until parity is proven**, then be removed in a
  separate, reviewable slice.
- **`operator_state` missing/null must degrade to the existing explicit controls.**
  The guided flow must never be a hard dependency.

## 14. Inputs needed for Claude Design (#35B)

Capture real screenshots of these states, and where possible attach the live
`operator_state` JSON alongside each so Design sees the data driving the view:

- awaiting chunk plan approval
- running_chunks
- patch failure with retry available
- awaiting_scope_approval
- awaiting_final_approval with weak-test acknowledgement outstanding
- final_approved in **github_cli** mode and in **local_only** mode
- pr_open with checks failed / pending
- terminal failed
- a state with `unknown_state_warning` present

Also provide Design with: the friendly-label map (for consistent copy) and the
three-tier wireframe from section 6 as the layout contract.

## 15. Non-goals

- **No backend behavior changes.**
- **No new mutation routes.**
- **No approval / retry / scope-expansion / test-acknowledgement / PR / memory /
  provider semantics changes.**
- **No autonomous action** — the UI never acts without an explicit human click.
- **No removal of audit / debug information** — everything stays reachable.
- **No full frontend rewrite** — this is a redesign of one page's IA and copy.
- **No memory UI redesign** beyond surfacing run-decision context.

## 16. Closeout criteria

This document is complete when:

- It clearly explains **why #35 exists** (the scattered, backend-flavored,
  un-guided current page).
- It **distinguishes product design from implementation** (Claude Design vs the
  PR-by-PR slices).
- It gives **Claude Design enough context** — page hierarchy, state copy, the
  `operator_state` model, and the screenshots to capture — to produce a useful
  design.
- It gives **Codex / Claude Code a safe, PR-by-PR implementation sequence** with
  explicit parity-test and degradation requirements that preserve every safety
  guarantee and audit surface.
