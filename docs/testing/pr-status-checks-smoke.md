# PR Status & Checks Smoke / Closeout Checklist (#31)

Manual smoke validation and closeout record for **#31 — GitHub / PR Robustness
and Checks Integration**. This is a checklist, not an automated suite: the
frontend has no test framework yet, so the UI steps are manual and complement
the focused backend tests that already cover the PR read model, push/PR
idempotency and recovery, the checks foundation, and the explicit refresh
endpoint.

Related docs:

- Design / audit: [`docs/design/github-pr-robustness-and-checks.md`](../design/github-pr-robustness-and-checks.md)
- Status: [`docs/status/current-state.md`](../status/current-state.md)
- Demo smoke: [`docs/testing/demo-smoke-checklist.md`](./demo-smoke-checklist.md)

## Completed #31 Work

- #31A GitHub / PR robustness design audit — merged
- PR read-model foundation (typed, honest `pr_status` derived on read) — merged
- PR creation idempotency / recovery hardening (reload-under-lock, idempotent
  save, parse-unconfirmed re-list recovery, split local-base-missing vs
  no-commits-ahead) — merged
- GitHub checks display-only backend foundation (`pr_checks`, gh helper,
  `unavailable`-on-failure) — merged
- Explicit `GET /runs/{run_id}/pr-status` refresh endpoint — merged
- Frontend PR Status panel (display-only, explicit refresh, no polling) — merged
- This smoke / closeout checklist (docs only)

## 1. Purpose

This checklist validates the completed #31 work end to end: that Pipewright now
shows an **honest** PR/push state, creates PRs **idempotently and recoverably**,
exposes an **explicit** PR status + checks refresh, renders a **display-only**
frontend PR Status panel, and makes **no hidden GitHub calls during a normal Run
Detail load**.

Concretely, it confirms:

- **Honest PR state** — a failed push reads as `push_failed`, never as "ready to
  create a PR"; a recorded PR reads as `pr_open`.
- **Idempotent / recoverable PR creation** — retrying never duplicates a PR, and
  an already-created-but-unconfirmed PR is reused.
- **Explicit PR status / checks refresh** — checks are fetched only when the
  operator asks, through `GET /runs/{run_id}/pr-status`.
- **Display-only frontend PR Status panel** — it surfaces state and checks but
  grants no authority and gates nothing.
- **No hidden GitHub calls** — opening Run Detail does not call GitHub; only the
  explicit refresh (and the existing push/PR control) do.

## 2. Safety guarantees

These invariants must hold throughout the smoke run:

- [ ] **No auto-merge** — Pipewright never merges a PR.
- [ ] **No final-approval bypass** — push/PR still requires final approval first.
- [ ] **No check-based gating** — check state never blocks or enables approval,
  push, or merge.
- [ ] **No reviewer PR comments** — nothing posts comments to GitHub.
- [ ] **No polling** — nothing refreshes checks on a timer.
- [ ] **No automatic checks refresh on page load** — checks load only on click.
- [ ] **No hidden GitHub calls during normal Run Detail load.**
- [ ] **Checks are display-only.**
- [ ] **GitHub failures show `unavailable`, not `failed`** — a gh/network error
  is never presented as a failing build.
- [ ] **Normal push/PR behavior remains controlled by existing backend routes**
  (`POST /runs/{id}/push-pr`) — #31 added no new mutating path.

## 3. Validation commands

Backend (focused #31 suites):

```bash
pytest backend/tests/test_pr_checks.py backend/tests/test_pr_status.py backend/tests/test_pr_orchestrator.py
```

Frontend build (typecheck + bundle):

```bash
npm run build
```

Frontend lint on the touched files only:

```bash
npx eslint frontend/src/api/client.ts frontend/src/components/PrStatusPanel.tsx frontend/src/pages/RunDetailPage.tsx
```

> Note: a **repo-wide** `npm run lint` may still report pre-existing, unrelated
> errors (e.g. in shared UI primitives and hooks) that are not part of #31. The
> touched-file lint above must be clean; repo-wide pre-existing findings are out
> of scope for this closeout. On Windows, use `npm.cmd` if PowerShell blocks
> `npm.ps1`.

## 4. Manual smoke: local_only mode

- [ ] Configure or use a project in **`local_only`** PR mode.
- [ ] Take a run through to **final approval** and complete it.
- [ ] Confirm **no GitHub call** is made (no push, no PR, no `gh`).
- [ ] Confirm the **manual push / open-PR instructions** remain clear (branch
  name + `git push` + "open a PR when ready").
- [ ] Confirm the **PR Status panel does not show misleading GitHub checks** —
  there is no PR, so there is no "Refresh PR checks" action and no checks
  summary.
- [ ] Confirm **no auto-push / auto-PR** happens at any point.

## 5. Manual smoke: github_cli happy path

- [ ] Ensure **`gh` is installed and authenticated** (`gh auth status`).
- [ ] Ensure the **base branch exists on `origin`** (default
  `pipewright-staging`).
- [ ] Run a feature through to **final approval**.
- [ ] Click the **existing push / create-PR control** (`Push and Create PR`).
- [ ] Confirm the **PR is created**.
- [ ] Confirm the **PR URL, number, and branch** are visible.
- [ ] Confirm the **PR Status panel shows the PR state** (`PR open`) and a link
  to the PR.
- [ ] Confirm **re-click / retry does not duplicate** the PR — the existing PR
  is reused (idempotent push/PR behavior).

## 6. Manual smoke: explicit checks refresh

- [ ] Load **Run Detail normally** and confirm **no checks refresh happens
  automatically** (no `gh`/GitHub call on load; no checks summary appears yet).
- [ ] Click **Refresh PR checks**.
- [ ] Confirm **`GET /runs/{run_id}/pr-status` is called** (network tab / server
  logs).
- [ ] Confirm checks render as **`pending` / `passed` / `failed` / `unavailable`
  / `no_checks` / `unknown`** as applicable.
- [ ] Confirm **`unavailable` is not presented as a failing build**.
- [ ] Confirm **failed checks do not block or mutate** final approval, push, or
  merge state.

## 7. Manual smoke: push_failed honesty

- [ ] Simulate or observe a PR-creation failure, e.g. **gh unauthenticated**,
  **remote base missing**, **dirty tree**, **local base missing**, or a
  **permission / network error**.
- [ ] Confirm the backend `pr_status` state is **`push_failed`**.
- [ ] Confirm the Operator / PR UI does **not** say "ready to create PR" as the
  **main** state.
- [ ] Confirm a **failure summary and next action** are visible.
- [ ] Confirm **retry remains available through the existing push/PR control**
  when the failure is retryable.
- [ ] Confirm **non-retryable failures** (e.g. forbidden base branch, no commits
  ahead) are **clearly blocked** rather than offered as a one-click retry.

## 8. Manual smoke: checks states

For each state, validate the high-level UI copy/behavior (not brittle exact CSS).

### Pending checks

- [ ] State badge reads as **checks pending** (in-progress wording).
- [ ] Counts (passed / failed / pending) are shown; nothing implies the build is
  done.

### Passed checks

- [ ] State badge reads as **checks passed**.
- [ ] Pending count is zero; failed count is zero.

### Failed checks

- [ ] State badge reads as **checks failed**.
- [ ] The failure is informational only — **no** control to merge, and approval /
  push state is unchanged.

### Unavailable checks

- [ ] State badge reads as **checks unavailable** (neutral, not red/failed).
- [ ] Copy makes clear this is a retrieval problem, **not** a failing build, and
  invites another refresh.

### No checks

- [ ] State badge reads as **no checks configured**.
- [ ] No counts implying pass/fail; this is a normal, non-error state.

### Unknown / not refreshed yet

- [ ] Before any refresh, **no checks summary** is shown — only the derived PR
  state and the explicit "Refresh PR checks" affordance.
- [ ] An indeterminate result after refresh reads as **unknown**, never as
  passed or failed.

## 9. Regression / safety checklist

- [ ] **Final approval still required** before any push/PR.
- [ ] **Normal Run Detail load still does not call GitHub.**
- [ ] **Refresh checks button is explicit and user-triggered.**
- [ ] **No polling.**
- [ ] **No PR comments.**
- [ ] **No merge call.**
- [ ] **No raw check logs stored** — only aggregate counts + derived state.
- [ ] **No GitHub token or secret appears in the UI or logs** (errors are
  sanitized before persist/return).
- [ ] **Reviewer remains advisory only.**
- [ ] **Scope behavior unchanged.**
- [ ] **`local_only` semantics unchanged.**

## 10. Known limitations / deferred work

- Checks are **explicit-refresh only** — no polling or webhooks yet.
- Checks are **display-only**, not a gate.
- **GitHub Enterprise / non-`origin` remote** support remains deferred unless
  already supported by the configured `gh`/remote.
- **Durable cross-worker locking** remains deferred; the current per-project
  lock is in-process and is sufficient only for single-worker local use.
- **`PushPrPanel` and `PrStatusPanel` may both show branch / PR info** for now;
  future UI consolidation can clean this up.

## 11. Closeout criteria

#31 can be considered complete when:

- [ ] Backend tests pass (Section 3 suites).
- [ ] Frontend build and touched-file lint pass (Section 3).
- [ ] Manual smoke confirms **`local_only`**, **`github_cli` PR creation**,
  **explicit checks refresh**, **`push_failed` honesty**, and the **safety
  invariants** (Sections 4–9).
- [ ] **No hidden GitHub calls** happen during a normal Run Detail load.

## Result

If every box above is checked, **#31 — GitHub / PR Robustness and Checks
Integration is complete and can be closed.** PR state is honest, PR creation is
idempotent and recoverable, checks are surfaced only on explicit refresh and are
strictly display-only, and no GitHub calls happen during a normal Run Detail
load. If any box fails, fix the underlying issue (or file a scoped follow-up)
before closing #31.
