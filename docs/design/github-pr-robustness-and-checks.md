# GitHub / PR Robustness and Checks Integration — Design & Audit (#31)

> Status: **Design / audit only.** This document inspects the *current* final
> approval → push → PR flow and proposes the safest design for #31. It ships no
> backend code, no frontend code, no schema change, no route change, no
> migration, and no runtime behavior change. It is the #31A deliverable.
>
> Existing routes remain the source of truth and must continue to revalidate
> every mutating action under their existing guard/lock flow. Nothing here
> weakens an existing safety guard, adds auto-merge, or adds reviewer-authored
> PR comments.

---

## 0. Scope of this audit

Principal-engineer review requested before starting #31. Five questions:
current flow audit, safety invariant audit, GitHub/PR robustness drawbacks,
product/UX audit, and a desired design with implementation slices. A concise
"start #31 now or not" recommendation is at the end.

The audit was produced by reading the live code, not the roadmap. Every claim
below cites a file and the behavior observed there.

---

## 1. Current flow audit

### 1.1 From final approval to push / PR

There are **two distinct, explicitly human-triggered steps**. Final approval
does **not** auto-push. This is correct and central to the safety model.

**Step A — Final approval** (`backend/routes/chunks.py`):

- `POST /runs/{run_id}/final-approval/approve` → `approve_final_approval_route`
  (chunks.py:2135).
  1. `_ensure_mutating_run` — rejects `report_only` / `plan_only` runs.
  2. `_require_test_validation_acknowledged` — #28F gate: 409 if a weak/none
     runtime test verdict is not acknowledged against the current diff.
  3. `_decide_final_gate(run_id, APPROVED, FINAL_APPROVED)` (chunks.py:762):
     in one `engine.begin()` transaction it flips the pending
     `approval_gates` row (`approval_type='final'`, `chunk_number=0`,
     `status='pending'`) to `approved`, and sets `pipeline_runs.status` and
     `current_step` to `final_approved`.
- The final gate is created upstream by
  `create_final_approval_gate` / `_create_final_approval_gate_for_conn`
  (approval_gate.py:276, :220) when the run reaches `awaiting_final_approval`.
- **No push, branch, or PR call happens here.** The run simply sits at
  `final_approved`.

**Step B — Push + PR** (separate route, separate click):

- `POST /runs/{run_id}/push-pr` → `push_pr_route` (chunks.py:2224) →
  `push_and_create_pr(run_id)` (pr_orchestrator.py:607).
- `push_and_create_pr`:
  1. `_load_run(run_id)` — loads the run row (**before** taking the lock; see
     section 3 stale-read note).
  2. `with project_repo_lock_sync(project_id)` — in-process per-project lock.
  3. `_push_and_create_pr_locked(run_id, run)`:
     - Branch name is derived deterministically: `pipewright/{run_id[:8]}`.
     - If `run.pr_url` already set → return the existing PR (idempotent
       short-circuit).
     - `require_project` → load project, `normalize_pr_mode`.
     - `evaluate_push_pr_eligibility(run_status, pr_mode, has_pr_url=False)`
       (pr_orchestrator.py:55) — pure status/mode gate. Allowed statuses:
       `{final_approved, push_failed}` for remote modes; additionally
       `{complete}` for `local_only`. Ineligible → `RuntimeError` (→ HTTP 400).
     - Dispatch by mode (below).

### 1.2 Per-mode behavior

**`local_only`** — `_complete_local_only` (pr_orchestrator.py:380):
- No push, no PR, no GitHub token. Read-only verification that the local branch
  exists (`branch_exists`). Marks run `complete` (`current_step=local_only_complete`).
- Returns manual `git push` / "open a PR" instructions. This is a *success*,
  never `push_failed`.

**`github_cli`** — `_push_and_create_pr_github_cli` (pr_orchestrator.py:457):
1. `validate_base_branch(base_branch)` — resolve missing → `pipewright-staging`;
   reject `main`/`master`/`develop`.
2. `ensure_remote_base_branch(repo_path, base_branch, "origin")` — base must
   exist on remote (#20B-2), else clear `REMOTE_BASE_MISSING`.
3. `gh_pr.ensure_gh_ready()` — gh installed + authenticated, else
   `GH_NOT_READY_MESSAGE`.
4. `_mark_pushing` → status `pushing`, `current_step='push_pr'`, clear `push_error`.
5. `_verify_local_branch_for_cli` — branch exists; checkout if not current;
   `ensure_clean_worktree`; if owner/repo configured, also `_verify_local_branch`
   (origin remote-match).
6. `_ensure_branch_has_commits_ahead(repo_path, branch, base)` — `commits_ahead`
   (local `base..branch`) must be > 0.
7. Push only if not already on remote: `branch_exists_remote` → `push_branch`
   (`git push -u origin <branch>`).
8. `_save_pushed` → set `pushed_at` (COALESCE; run stays `pushing`).
9. `gh_pr.find_open_pr` → reuse; else `gh_pr.create_pr`.
10. `_save_pr_metadata` → status `complete`, `pr_url`, `pr_number`, `pr_created_at`,
    clear `push_error`.
- Any exception → `_mark_push_failed` (sanitized) → status `push_failed`,
  `current_step='push_pr_failed'` → re-raise (→ HTTP 400).

**`manual_token`** — `_push_and_create_pr_manual_token` (pr_orchestrator.py:516):
- Same shape via PyGithub. `_require_project_github` decrypts the stored token
  (`decrypt_secret`). `_get_github_repo` authenticates. `_find_existing_pr`
  (state=`open`, `head=owner:branch`, `base`) → reuse; else `_create_pr`.
- Same `push_failed` handling. Token is never returned in responses; errors are
  `sanitize_for_log`-sanitized before persist and re-raise.

### 1.3 Components involved (map)

| Layer | File | Role |
|---|---|---|
| Route | `backend/routes/chunks.py` | final-approval approve/reject, `/push-pr`, read-model assembly (`_augment_plan_with_operator_state`) |
| Route | `backend/main.py` | `GET /runs/{id}`, `GET /runs` (raw `pipeline_runs` row) |
| Orchestration | `backend/pipeline/pr_orchestrator.py` | push + PR per mode, eligibility, persistence |
| Gate | `backend/pipeline/approval_gate.py` | create/return final gate |
| Gate | `backend/human/approval_gate.py` | generic gate store (`/gates`) |
| Git | `backend/git/local_git.py` | safe subprocess git: branch/checkout/clean-tree/push/commits_ahead/remote-url |
| Git | `backend/git/gh_pr.py` | `gh pr list` / `gh pr create`, find-or-create |
| Git | `backend/git/gh_cli.py` | `gh` installed/authenticated detection |
| Git | `backend/git/pr_preflight.py` | remote base-branch existence check |
| Safety | `backend/github/branch_safety.py` | forbidden/default base branch |
| Locking | `backend/pipeline/run_locks.py` | in-process per-project lock |
| Read model | `backend/pipeline/operator_state.py` | computed attention-panel state |
| Project | `backend/projects/pr_modes.py`, `project_store.py` | mode normalization, project load |
| Security | `backend/security/secrets.py`, `backend/llm/sanitize.py` | token decrypt, error sanitize |
| Schema | `backend/db/schema.sql` | `pipeline_runs` PR columns, `projects` github_*/pr_mode |
| FE | `frontend/src/components/PushPrPanel.tsx` | push button, branch/PR/error display |
| FE | `frontend/src/components/OperatorAttentionPanel.tsx` | renders `operator_state` |
| FE | `frontend/src/components/FinalApprovalPanel.tsx` | final approval UI |
| FE | `frontend/src/pages/RunDetailPage.tsx` | panel orchestration, polling |
| Docs | `docs/design/operator-state-attention-panel.md` | operator-state spec |

### 1.4 PR modes today

`local_only` (default), `github_cli`, `manual_token` — and **only** these three.
`pr_modes.normalize_pr_mode` rejects anything else; GitHub App is explicitly not
a mode. The legacy `backend/github/github_client.py` is **not imported by any
non-test runtime path** (only `branch_safety` is shared); it is effectively dead
for the chunked flow.

### 1.5 Where branch safety is enforced today

- **Base branch:** `validate_base_branch` (branch_safety.py) — forbids
  `main`/`master`/`develop`, defaults to `pipewright-staging`. Called in both
  remote modes before any push.
- **Remote base exists:** `ensure_remote_base_branch` (pr_preflight.py).
- **Run branch identity:** push branch is always `pipewright/{run_id[:8]}` —
  derived, never user-supplied.
- **Worktree/branch state at push:** `_verify_local_branch` /
  `_verify_local_branch_for_cli` — branch exists, checked out, clean tree,
  origin remote matches `owner/repo` (manual_token + cli-with-owner).
- **Commits exist:** `_ensure_branch_has_commits_ahead` (> 0 ahead of base).
- **Stale-branch guard at run *start*:** `assert_not_on_stale_pipewright_branch`
  (local_git.py:206) — blocks a *new* run from starting on a foreign
  `pipewright/*` branch. (This is a start-of-run guard, not a push-time guard.)

### 1.6 Where final approval is enforced today

- `evaluate_push_pr_eligibility` requires `run.status ∈ {final_approved,
  push_failed}` (or `complete` for local_only) before any push/PR. This is the
  load-bearing check: the run reaches `final_approved` **only** via
  `_decide_final_gate`, which flips the pending `final` gate. So push trusts
  `pipeline_runs.status` as the proxy for "final gate approved."
- `_require_test_validation_acknowledged` blocks the *final approval* itself
  (and therefore everything downstream) when weak/none verdicts are unacked.

### 1.7 Where PR creation can fail today

Every failure path below collapses into `_mark_push_failed` (status
`push_failed`, sanitized `push_error`) and re-raises as HTTP 400:

- `validate_base_branch` (forbidden base) — before push.
- `ensure_remote_base_branch` (`REMOTE_BASE_MISSING` / `GIT_COMMAND_FAILED`).
- `ensure_gh_ready` (gh missing/unauthenticated).
- `_verify_local_branch*` (branch missing, checkout fail, dirty tree, remote
  mismatch).
- `_ensure_branch_has_commits_ahead` (0 ahead, **or local base ref missing** —
  see section 3.6).
- `push_branch` (network, auth, permission, non-fast-forward).
- `gh_pr.find_open_pr` / `create_pr` (gh error, JSON parse, **URL/number parse**).
- `_get_github_repo` / `_create_pr` (PyGithub auth, 403, network, GraphQL).

### 1.8 State persisted after PR creation / failure

`pipeline_runs` columns (schema.sql:87-92): `pr_url`, `pr_number`,
`branch_name`, `pushed_at`, `pr_created_at`, `push_error`. Plus `status` /
`current_step`. Notably **`COALESCE`** is used for `pushed_at`, `pr_created_at`,
`pr_url`/`pr_number` so a retry never clobbers a first success. There is **no**
persisted PR *state* (open/merged/closed), **no** checks/status data, **no**
typed failure classification, and **no** attempt counter.

---

## 2. Safety invariant audit

| Invariant | Verdict | Enforced at | Residual trust risk |
|---|---|---|---|
| No auto-commit before approval | **Protected** | Chunk commits happen in chunk execution under chunk approval; push/PR is a separate explicit route. | None observed in this flow. |
| No final-approval bypass | **Protected** | `evaluate_push_pr_eligibility` requires `final_approved`/`push_failed`; `_decide_final_gate` is the only writer of `final_approved`. | Push trusts `run.status`, not a fresh re-read of the gate row, and the run is loaded **before** the lock (section 3.1). If any other code path wrote `status='final_approved'` without flipping the gate, push would proceed. |
| No auto-merge | **Protected** | No merge call exists anywhere; `gh_pr`/PyGithub paths create/reuse only. | None. |
| No push/PR before backend final-approval rules pass | **Protected** | Eligibility gate + ack gate. | Same `run.status`-as-proxy + stale-read caveat. |
| Never push from main/master/develop | **Partially protected** | Push branch is always `pipewright/{run_id[:8]}`; *base* is `validate_base_branch`-checked. The **current checked-out branch** is verified to equal the run branch before push. | Strong for base and head. The head branch can never be a protected branch by construction. OK. |
| Never silently continue on wrong branch | **Protected (at push)** | `_verify_local_branch*` checks `get_current_branch == branch_name`, checks out if not, fails on detached HEAD (`get_current_branch` raises on empty). | The push-time check is solid. There is **no** equivalent "wrong branch" surfacing in `operator_state` for the push step (the `wrong_branch` context exists for scope/retry, not push). |
| Routes remain source of truth, revalidate before mutation | **Partially protected** | `/push-pr` re-derives eligibility and re-runs all git/GitHub checks inside the lock. | The run **row** used for eligibility is the pre-lock snapshot (`push_and_create_pr` loads then locks). Low risk because the lock is non-blocking (second caller 409s), but it is not a fresh read under the lock. |
| Reviewer remains advisory only | **Protected** | `_augment_plan_with_reviews` is additive, display-only, fail-closed, applied last; no action surface. | None. #31 must not add reviewer→PR-comment coupling. |
| Scope never auto-expanded | **Protected** | Out of this flow; scope expansion requires explicit human approval. | None introduced by PR flow. |

**Net:** the hard safety invariants (no auto-merge, no bypass, no protected-base
push) are intact. The two soft spots are **(a)** push eligibility trusts a
pre-lock `run.status` snapshot rather than a fresh under-lock read of the gate,
and **(b)** the in-process lock is not durable across workers (section 3.2).

---

## 3. GitHub / PR robustness drawbacks

Ranked roughly by trust impact.

### 3.1 Pre-lock stale run read
`push_and_create_pr` (pr_orchestrator.py:607) loads `run` **before**
`project_repo_lock_sync`, then passes that snapshot to the locked function,
which reads `run.status` and `run.pr_url` from it. Correctness today rests on
the lock being non-blocking (a concurrent caller 409s rather than waiting), so
the stale window is small — but the locked path should re-load the run under the
lock. **Recommendation: reload inside the lock.**

### 3.2 In-process lock only → multi-worker duplicate PR
`run_locks` is a process-local `threading.Lock`/set. Under
`uvicorn --workers >1` (or multiple backend processes against the same DB), two
`/push-pr` calls for the same run on different workers can both pass eligibility
and both push/create. `find_open_pr` mitigates the *duplicate PR* outcome on the
second pusher only if the first has already created+listed the PR; otherwise two
`create_pull` calls can race. **This is the single biggest robustness gap for
any non-single-process deployment.** Mitigations: DB-level idempotency
(conditional `UPDATE ... WHERE pr_url IS NULL`) and/or a durable advisory lock.

### 3.3 Duplicate / stale PR detection is "open-only, first match"
`find_open_pr` (gh_pr.py) and `_find_existing_pr` (PyGithub) filter
`state="open"` and take the first result. Consequences:
- A previously **merged or closed** PR for the same head/base is ignored → a new
  PR is created (arguably fine, possibly surprising).
- The branch could have **multiple** open PRs; only the first is reused.
- No reconciliation against the `pr_url` already stored on *other* runs sharing
  a colliding `run_id[:8]` branch prefix (8 hex chars — low but nonzero
  collision risk).

### 3.4 PR URL/number parsing is heuristic
`_extract_pr_url` scans `gh pr create` stdout for the last `/pull/` line;
`_parse_pr_number` int-parses the URL tail. If gh changes output, prints a tip
line, or the PR is created but stdout is unexpected, the orchestrator raises
"PR was created but its URL/number could not be parsed," marks the run
`push_failed` — **even though the PR now exists on GitHub.** Recovery works
(next retry `find_open_pr` reuses it), but the intermediate state lies to the
user ("failed" when it succeeded).

### 3.5 Failures are a single opaque string, not a taxonomy
Except `PrPreflightError.failure_type` and `GhCliError`, every failure collapses
into `push_error` (a sanitized string) + status `push_failed`. The UI cannot
distinguish "gh not installed" (fix locally) from "permission denied" (fix on
GitHub) from "network" (just retry) from "remote base missing" (push the base).
The messages are honest but **not machine-actionable**, so the UI can only dump
raw text.

### 3.6 `commits_ahead` compares against a possibly-missing *local* base ref
`_ensure_branch_has_commits_ahead` runs `git rev-list --count base..branch`
against the **local** `base_branch` (e.g. `pipewright-staging`). The preflight
only guarantees the base exists on the **remote**. If the local repo has never
fetched/created `pipewright-staging`, `rev-list` fails → `push_failed` with a
git error. This is a real, common first-run failure mode that reads as a generic
push failure rather than "fetch the base branch."

### 3.7 No GitHub Checks / status integration at all
There is **zero** code that queries PR check runs, commit statuses, or
mergeability. After a PR is created the run is `complete` and the user has no
in-app signal whether CI is pending/passing/failing or whether the PR is
mergeable. This is the core feature gap #31 exists to close.

### 3.8 `origin`-only, github.com-only assumptions
Remote name is hardcoded `"origin"` (push, ls-remote, get-url). `_remote_matches`
only parses `github.com` SSH/HTTPS URLs → GitHub Enterprise or a non-`origin`
remote fails the origin-match check in manual_token / cli-with-owner.

### 3.9 Other enumerated cases (current behavior)

| Case | Current behavior |
|---|---|
| Branch already pushed | Handled: `branch_exists_remote` skips re-push; `pushed_at` COALESCE-preserved. |
| PR already exists | Reused if **open** (section 3.3). |
| Retry after partial failure (pushed, PR failed) | Handled: re-enters, skips push, find-or-create PR. Good. |
| gh missing / unauthenticated | Clear `GH_NOT_READY_MESSAGE` before any push. Good. |
| origin missing | `get_remote_url` raises → `push_failed`; message references the remote. |
| base branch missing (remote) | `REMOTE_BASE_MISSING` with `git push -u origin <base>` hint. Good. |
| permission denied | Generic `push_failed`, not classified (section 3.5). |
| network failure | Generic `push_failed`, retryable but unclassified. |
| detached HEAD | `get_current_branch` raises "current branch is empty" → `push_failed`. Reasonable but generic. |
| wrong branch | Auto-checkout to run branch if branch exists; fails if it cannot. No operator-panel surfacing for push step. |
| dirty tree | `ensure_clean_worktree` raises with file list. Good. |
| stale run state | Eligibility recomputed; pre-lock snapshot caveat (section 3.1). |
| concurrent final approval | `_decide_final_gate` gate-flip is `WHERE status='pending'`; only one flips, both run-UPDATEs succeed (benign). Not under project lock. |
| concurrent PR creation | Single-process: 409 via lock. Multi-process: race (section 3.2). |
| checks pending/failing/unavailable | Not represented anywhere (section 3.7). |
| unclear UI after final approval | `operator_state` jumps `final_approved`→`_pr_ready_state` ("Create pull request"); no distinct pushing/failed/checks states (section 4). |
| local_only confusion | Reasonably handled (`local_only_manual_push`, manual instructions). |
| manual_token risks | Token decrypted only at push, never returned, errors sanitized. Good. |
| PR URL not shown clearly | Shown in `PushPrPanel` and the `complete` block; **not** in the operator panel. |
| failure messages not actionable | section 3.5. |

---

## 4. Product / UX audit (Run Detail after final approval)

Walking the page as a user, driven by `RunDetailPage.tsx` +
`operator_state`:

### 4.1 What is confusing / reduces trust today

1. **`push_failed` masquerades as "ready to create PR."** In `operator_state`,
   `PUSH_FAILED` satisfies `pr_decision.eligible`, so `compute_operator_state`
   returns `_pr_ready_state` → title **"Create pull request"** with a green
   "Final approval is complete" safety check and **no mention that the previous
   attempt failed**. The `push_error` is shown only in `PushPrPanel`, in a
   separate card. The single most-prominent panel and the error live in
   different places and tell different stories.
2. **No "pushing" attention state.** During `pushing`, `operator_state` returns
   the generic `_running_state` ("Pipewright is running"). The user can't tell
   push from chunk execution.
3. **No checks state at all.** After `complete` + `pr_url`, the panel says
   "Pull request is ready / No further in-app action is required" even if CI is
   red. That is an over-strong trust claim.
4. **Two sources of truth for the PR.** `OperatorAttentionPanel`,
   `PushPrPanel`, and the `complete` block each render PR info independently;
   they can momentarily disagree (e.g., operator panel "Create PR" while
   PushPrPanel shows a stale `push_error`).
5. **Raw error dump.** `PushPrPanel` renders `run.push_error`
   `whitespace-pre-wrap` — honest but not actionable; no "what to do next."

### 4.2 What the user should *always* see after final approval

- The run's PR mode and what that mode will/won't do.
- A single authoritative PR status (none / pushing / created / failed) with the
  branch and (if created) a clickable PR URL + number.
- If failed: a classified reason and the one next action.
- Final-approval as a settled fact (passed), distinct from PR status.

### 4.3 What the Operator Attention Panel should say (target)

| Situation | Title | Waiting on | Primary surface |
|---|---|---|---|
| local-only final approval | "Manual push required" | nobody | manual `git push` / open-PR instructions (already exists) |
| PR creation pending (`pushing`) | "Creating pull request…" | system | progress; no action; do not offer "Create PR" |
| PR created | "Pull request created" | system (checks) or nobody | clickable URL + number; checks status if known |
| PR creation failed | "Pull request could not be created" | human | classified reason + single next action (retry / fix gh / push base) |
| checks pending | "Checks are running" | system | PR link + "checks pending"; no merge claim |
| checks failed | "Checks failed on the pull request" | human | PR link + "review failing checks on GitHub" |
| checks passed | "Checks passed" | nobody | PR link + "ready to review/merge on GitHub" (never auto-merge) |
| GitHub unavailable | "Pull request status is temporarily unavailable" | system | last-known PR data + "status could not be refreshed; retry"; never imply success/failure |

The key principle: **`complete` must stop meaning "checks are green."** `complete`
means "Pipewright finished its part (PR exists or branch is local)." Check
status is a separate, explicitly-unknown-until-fetched dimension.

---

## 5. Desired design for #31

### 5.1 Design principles

1. Read-model first. Add status *fields* and a *display* before any new action.
2. Idempotent and recoverable. Every push/PR step must be safe to re-run.
3. Display-only checks. Read GitHub check/status; never gate, never merge,
   never comment.
4. Honest unknowns. "Unavailable" and "not yet fetched" are first-class, never
   coerced to success/failure.
5. No new mutating routes in the early slices.

### 5.2 Desired PR lifecycle / state machine

A new explicit **`pr_state`** dimension on the run, orthogonal to `status`:

```
                         local_only
   final_approved ───────────────────────────► local_pending  (terminal-ok)

   final_approved ──/push-pr──► pushing ──► pushed ──► pr_open ──► (checks)
        ▲                          │                       │
        │                          ▼                       ▼
        └──────────────────── push_failed            pr_create_failed
             (retry)                 (retry; branch may already be pushed)

   pr_open ──refresh checks──► checks_unknown ─┬─► checks_pending
                                               ├─► checks_passed
                                               ├─► checks_failed
                                               └─► checks_unavailable (transient)
```

- `pr_state` transitions are driven by the existing `/push-pr` path plus a new
  **read-only** checks refresh (no new mutating route).
- `status` (the existing column) keeps its current meaning; `pr_state` is the
  new, finer dimension the UI reads. Existing `push_failed`/`complete` values
  are preserved for backward compatibility.

### 5.3 Read-model fields (additive; no behavior change)

Surface a typed read model from the run row (do **not** keep returning the raw
`dict(row._mapping)` for PR concerns):

| Field | Source | Notes |
|---|---|---|
| `pr_mode` | project | already available |
| `pr_state` | derived from status + columns | new enum (section 5.2) |
| `branch_name` | run | existing |
| `pr_url`, `pr_number` | run | existing |
| `pushed_at`, `pr_created_at` | run | existing |
| `push_error` | run | existing (sanitized) |
| `failure_kind` | new column or derived | classified taxonomy (section 6) |
| `failure_next_action` | derived from `failure_kind` | one actionable hint |
| `checks_state` | new (checks refresh) | `unknown/pending/passed/failed/unavailable` |
| `checks_summary` | new | counts: passed/failed/pending; never raw logs |
| `checks_fetched_at` | new | staleness |

`checks_*` start as `unknown` and are only ever populated by an explicit,
read-only refresh. They never gate anything.

### 5.4 Backend models / store changes

- **Schema (additive only):** `pipeline_runs.pr_state TEXT`,
  `pipeline_runs.failure_kind TEXT`, `pipeline_runs.checks_state TEXT`,
  `pipeline_runs.checks_summary TEXT` (JSON), `pipeline_runs.checks_fetched_at
  DATETIME`. All nullable; `CREATE TABLE IF NOT EXISTS` + additive `ALTER`
  guarded by existence checks (match existing migration style). No backfill
  required — `NULL` ⇒ derive from current columns.
- **Failure classification:** introduce a small pure classifier (sanitized
  string + known error types → `failure_kind`), reusing `PrPreflightError.
  failure_type` and `GhCliError` where present. Pure, unit-testable, no I/O.
- **Checks reader:** a narrow, read-only module (mirror `gh_cli`/`gh_pr`
  discipline: list-args, no shell, short timeout, sanitized output) that runs
  `gh pr checks <number> --json ...` (or PyGithub `get_combined_status` /
  check-runs for manual_token) and returns a compact summary. **Read-only.**

### 5.5 Route changes

- **None in #31A–#31D for *mutation*.** The checks reader is wired into the
  **existing** read path (`GET /runs/{id}` typed read model and/or the chunk
  read-model augmentation) or a single new **GET** `…/pr-status` refresh
  endpoint that performs only a read against GitHub. No new POST.
- The existing `/push-pr` is hardened (idempotency, reload-under-lock,
  classification) without changing its contract.

### 5.6 Idempotency strategy

1. **DB-conditional PR write:** persist `pr_url`/`pr_number` via
   `UPDATE pipeline_runs SET pr_url=:url WHERE id=:id AND pr_url IS NULL` and
   treat `rowcount=0` as "already recorded, reuse." Closes the multi-worker race
   at the persistence boundary even if two creates happen.
2. **Reload run under the lock** before eligibility (fix section 3.1).
3. **Find-before-create** stays, broadened to also detect a just-created PR when
   parsing failed (re-list by head/base after a create error before declaring
   failure — converts section 3.4 false-failures into successes).
4. Keep `COALESCE` on `pushed_at`/`pr_created_at`.

### 5.7 Branch verification strategy

- Keep all existing push-time checks.
- Add: if `commits_ahead` fails because the **local** base ref is missing,
  classify as `LOCAL_BASE_MISSING` with hint "fetch the base branch"
  (distinct from "0 commits ahead") (fix section 3.6).
- Add a push-step **wrong-branch / detached-HEAD** classification so the
  operator panel can show a branch-specific failure rather than a generic one.

### 5.8 Duplicate PR detection strategy

- Prefer the run's own `pr_url` (idempotent short-circuit) — already present.
- `find_open_pr` remains primary; on `create` error, **re-list** before failing.
- Optionally record the PR `node_id`/number to detect the
  reused-vs-newly-created distinction for display ("reused existing PR").
- Do **not** auto-close or auto-dedupe existing PRs.

### 5.9 GitHub checks / status strategy

- Display-only. Fetched on explicit read (run-detail load or a GET refresh),
  never on a timer that mutates.
- Map to `checks_state`: any failing → `failed`; any pending and none failing →
  `pending`; all complete and success → `passed`; fetch error/no checks →
  `unavailable`/`unknown` (distinguish "GitHub unreachable" from "no checks
  configured").
- Never block final approval, push, or anything on checks. Never merge. Never
  comment.

### 5.10 Frontend display strategy

- **Single PR status source:** the operator panel and `PushPrPanel` read the
  same typed read model; add `_pushing_state`, `_push_failed_state`
  (classified), and `_pr_checks_*` states to `operator_state` so the panel is
  authoritative.
- Add explicit `push_failed` operator state (do **not** reuse `_pr_ready_state`)
  showing the classified reason + single next action.
- Show checks as a clearly-labeled, possibly-stale, never-gating badge with
  `checks_fetched_at`.
- `complete` copy changes from "no further action required" to "Pipewright
  finished; PR exists" + separate checks line.

### 5.11 Manual smoke checklist (target, per slice)

local_only:
- [ ] Final approve → operator panel "Manual push required"; instructions shown; run `complete`; no push.

github_cli happy path:
- [ ] gh authed, base on remote, branch ahead → push → PR created; URL+number shown; `pr_state=pr_open`.
- [ ] Re-click `/push-pr` → reuses PR (no duplicate); identical URL.

github_cli failures (each shows classified reason + one action):
- [ ] gh not installed / not authed.
- [ ] remote base missing.
- [ ] local base ref missing (fetch hint, distinct from "0 ahead").
- [ ] dirty tree.
- [ ] detached HEAD / wrong branch.
- [ ] simulated network/permission error.
- [ ] create succeeds but stdout unparseable → re-list recovers to success, not failure.

checks (display-only):
- [ ] pending / passed / failed each render correctly; GitHub-unreachable shows "unavailable," never success/failure; nothing is gated.

manual_token:
- [ ] PR created via token; token never appears in any response or error.

idempotency / concurrency:
- [ ] Two `/push-pr` in flight (same process) → one 409, no duplicate.
- [ ] (If multi-worker is in scope) conditional PR write prevents double-record.

### 5.12 Test plan

- **Unit (pure, no I/O):** failure classifier (string+type → `failure_kind`,
  `next_action`); `pr_state` derivation from columns; checks-summary →
  `checks_state` mapping; eligibility unchanged; operator-state precedence for
  new `pushing`/`push_failed`/`checks_*` states.
- **Store:** conditional `pr_url IS NULL` write returns reuse on second call;
  additive columns default `NULL` and derive correctly.
- **Git/gh (mocked subprocess):** checks reader parses `gh pr checks` JSON;
  sanitization on error; `LOCAL_BASE_MISSING` vs `0 ahead` branching; re-list
  after create-parse-failure.
- **Route:** `/push-pr` idempotent reuse; reload-under-lock; read model exposes
  new fields; no token leakage in any error.
- **Regression:** all existing pr_orchestrator / branch-safety / operator-state
  tests stay green; assert no new mutating route; assert reviewer stays
  advisory.

---

## 6. Failure taxonomy (proposed `failure_kind`)

| `failure_kind` | Trigger | `next_action` | Retryable |
|---|---|---|---|
| `gh_not_ready` | gh missing/unauth | `gh auth login`, then retry | yes |
| `remote_base_missing` | base not on origin | `git push -u origin <base>` | yes |
| `local_base_missing` | local base ref absent | `git fetch origin <base>` | yes |
| `no_commits_ahead` | 0 ahead of base | nothing to PR; investigate | no |
| `dirty_tree` | uncommitted changes | review/restore working tree | yes |
| `wrong_branch` | detached/unverifiable HEAD | checkout run branch | yes |
| `remote_mismatch` | origin ≠ configured repo | fix origin / project config | yes |
| `push_rejected` | non-fast-forward/permission | resolve on GitHub/remote | maybe |
| `auth_failed` | token/gh auth rejected | re-auth | yes |
| `network` | transient connectivity | retry | yes |
| `pr_parse_unconfirmed` | create ok, parse failed | auto re-list; usually self-heals | yes |
| `unknown` | unclassified (sanitized) | inspect error; retry | maybe |

All values derive from already-sanitized strings/typed errors. No raw secret
ever reaches a `failure_kind` or `next_action`.

---

## 7. Implementation slices

Ordered for safety: read-model and idempotency before any display of checks,
display before any new surface.

- **#31A — Design / audit (this document).** Docs only. ✅ deliverable here.
- **#31B — Backend read-model & status foundation.** Additive nullable columns
  (`pr_state`, `failure_kind`, `checks_*`); pure `pr_state` derivation; pure
  failure classifier; typed run read model (stop leaking raw row for PR
  concerns). No GitHub calls, no new mutating route, no FE. Unit + store tests.
- **#31C — PR creation idempotency / recovery hardening.** Reload run under the
  lock; conditional `pr_url IS NULL` write; re-list-after-create to kill
  false-failures (section 3.4); `local_base_missing` vs `no_commits_ahead` split;
  wire `failure_kind` into `_mark_push_failed`. No contract change. Tests for
  each.
- **#31D — GitHub checks display-only foundation.** Read-only checks reader
  (`gh pr checks` / PyGithub status), `checks_state`/`checks_summary`/
  `checks_fetched_at`, mapping logic. Read path only (run-detail load or a GET
  refresh). No gating, no timer-driven mutation. Mocked-subprocess tests.
- **#31E — Frontend PR status / operator panel surfacing.** New
  `pushing` / `push_failed` (classified) / `pr_checks_*` operator states;
  single PR status source; checks badge with staleness; `complete` copy fix.
  FE build + operator-state precedence tests.
- **#31F — Smoke docs / manual checklist.** Promote section 5.11 into
  `docs/testing/…` smoke checklist; honest status note in README/demo docs.

Slices are independently shippable and each preserves all current safety
guards. #31B and #31C are backend-only and reversible; #31D adds only reads;
#31E is the first user-visible change and depends on B–D.

---

## 8. Deferred / non-goals (explicit)

**Non-goals for #31 (must not appear):**
- Auto-merge or any merge call.
- Reviewer-authored PR comments or reviewer→PR coupling.
- Gating final approval / push / PR on check status.
- New mutating routes in #31B–#31D.
- Frontend changes before the read model exists.
- Raising the large-file threshold or weakening scope/approval guards.
- Storing or returning GitHub tokens; logging `gh auth status` output.
- Multi-tenant auth, GitHub App, OAuth, deployment, Ollama (paused per CLAUDE.md).

**Deferred (future, not #31):**
- Durable cross-process lock / DB advisory lock (only if multi-worker deploy
  becomes real — section 3.2). #31C's conditional write is the interim guard.
- GitHub Enterprise / non-`origin` remote support (section 3.8).
- Timer/webhook-driven checks auto-refresh (start with on-read only).
- Rich check logs / annotations (keep summaries compact).

---

## 9. Final recommendation

**Should #31 start now?**

Yes — with one reordering. The current final-approval → push → PR flow is
**safe in its hard invariants** (no auto-merge, no protected-base push, no
final-approval bypass, advisory reviewer, no scope auto-expand). The real gaps
are **robustness and honesty**, which is exactly #31's charter.

But before adding *checks* (the headline feature), fix the two issues that
actively **mislead the user or risk a duplicate PR**, because checks built on a
dishonest base inherit the dishonesty:

1. **`push_failed` showing as "Create pull request" with a green final-approval
   check (section 4.1.1)** — a trust bug. Fix in **#31B/#31E** by giving `push_failed`
   its own operator state.
2. **The create-succeeded-but-parse-failed false failure (section 3.4)** and **pre-lock
   stale read / conditional PR write (section 3.1, section 3.2, section 5.6)** — correctness/idempotency.
   Fix in **#31C** *before* #31D checks.

So: **start #31 now, but run #31B → #31C (read model + idempotency/honesty)
before #31D (checks display).** Do not begin #31E/#31D until the run's PR state
is honest and the create path is idempotent. No other product weakness outranks
this; the codebase is otherwise in good shape for this phase.

> Reminder for implementers: this document changes no runtime behavior. Each
> subsequent slice must keep every existing safety test green, add no mutating
> route until #31E (and only the minimal one), and never let check status gate a
> human gate.
