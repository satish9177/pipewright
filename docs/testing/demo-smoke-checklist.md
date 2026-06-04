# Demo / Readiness Smoke Checklist

> A short, repeatable local demo flow that exercises the current pipeline and the
> recovery / validation gates, plus the Operator Attention Panel. Use it before a
> demo, a screen recording, or a readiness review.
>
> This is a **manual** checklist (docs-only). It changes no runtime behavior. For a
> narrated end-to-end walkthrough with troubleshooting, see
> [`../demo/local-self-use-demo.md`](../demo/local-self-use-demo.md). For the
> per-feature smoke docs, see the `#26`/`#27`/`#28` smoke checklists linked at the
> bottom.

---

## 0. Prerequisites

- Pipewright set up locally — [`../setup/local-dev.md`](../setup/local-dev.md).
- At least one working LLM provider key (Gemini by default).
- A **disposable local git repo** to point at — do not use a repo with uncommitted
  work you care about.
- Confirm LLM config resolves:

  ```powershell
  python scripts\print_role_config.py --validate
  ```

  Every role you plan to use should show `[OK]`.

> Honesty note: a passing demo proves the **process** works. It does **not** prove
> the generated code is correct, and the Adversarial Reviewer Stage is **not** part
> of this flow (it is design-only).

---

## 1. Start backend and frontend

Backend (repo root, venv active):

```powershell
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

Frontend:

```bash
cd frontend
npm run dev          # Windows if npm.ps1 is blocked: npm.cmd run dev
```

- [ ] Backend reachable at `http://127.0.0.1:8001`.
- [ ] Frontend reachable at the printed URL (typically `http://127.0.0.1:5173`).

> Drop `--reload` once you start an actual run — reload can interrupt execution.

---

## 2. Create / select a test project

1. **New Project** → paste the **local repo path** (find it with `pwd` at the repo
   root that contains `.git`).
2. **Detect project** (read-only; reports git repo, branch, GitHub remote, `gh`
   status — changes nothing).
3. Set a **verification command** (see the strong/weak variants below).
4. Leave **PR mode** on **Local only** for the core demo.

- [ ] Project created; detection succeeded; verification command set.

---

## 3. Run a small, well-scoped feature

Suggested request (single-file, explicit, no auto-commit expectation):

```text
Add a hello_message function in src/app.py that returns 'hello'.
Only modify src/app.py. Do not commit automatically.
```

- [ ] Pipewright triages the request and proposes a **chunk plan**.
- [ ] The plan shows **Files Expected** (should be `src/app.py` only).

---

## 4. Approve the chunk plan, then execute

- [ ] **Approve the chunk plan.** (No code is written before this.)
- [ ] **Execute chunks.** Planner → coder → guarded patch → verification command.
- [ ] The scope guard keeps the change to `src/app.py`.

---

## 5. Observe the Operator Attention Panel

At each pause, check the panel answers, in plain English:

- [ ] **What Pipewright is waiting for** (e.g. "Review the chunk plan", "Review
      final result").
- [ ] **What action is available** (a single, specific primary action — not a
      generic "Continue").
- [ ] **What is blocked and why** (blocked actions list a reason).
- [ ] **Process safety checks** (branch / tests / final approval shown as passed /
      failed / weak / not evaluated).

> The panel is **display-only**. The existing controls are still the real controls;
> the panel just explains the current state.

---

## 6. Recovery path — patch failure / retry (optional, if triggered)

To see it deliberately, point a chunk at a file whose expected content has drifted,
or hand-edit the working tree so the generated change cannot apply cleanly.

- [ ] The chunk **fails cleanly** with plain-English copy; **nothing is committed**.
- [ ] A **guarded retry** is offered; the panel surfaces it as the available action.
- [ ] Retry eligibility is revalidated server-side (a stale/unsafe retry is
      rejected, not silently run).

Detail: [`patch-retry-smoke.md`](patch-retry-smoke.md),
[`patch-failure-recovery-smoke.md`](patch-failure-recovery-smoke.md).

---

## 7. Recovery path — scope expansion (optional, if triggered)

Trigger by requesting a change that needs a file outside `files_expected`.

- [ ] On `SCOPE_VIOLATION` the run **pauses** and shows the **requested extra
      files**.
- [ ] Approving scope only authorizes a **retry under the expanded allowlist** — it
      does **not** approve code.
- [ ] Rejecting leaves the chunk failed; scope is **never auto-expanded**.

Detail: [`scope-expansion-recovery-smoke.md`](scope-expansion-recovery-smoke.md).

---

## 8. Validation path — weak / no-test acknowledgement (optional)

Set the verification command to a **weak** command (e.g. `python --version`) and
run a small change.

- [ ] The runtime verdict shows **weak** (or **none**).
- [ ] **Final approval is blocked** until a human **acknowledges** the weak/no-test
      result, **bound to the current diff**.
- [ ] A retry / scope amendment that changes the diff makes a prior acknowledgement
      **stale** and requires a fresh one.

> This makes thin validation visible. It does **not** assert the code is correct.

Detail: [`stronger-test-validation-smoke.md`](stronger-test-validation-smoke.md).

---

## 9. Validation path — strong tests (if available)

Set the verification command to a real suite (e.g. `python -m pytest`) on a repo
that has tests.

- [ ] The runtime verdict shows **strong**.
- [ ] No weak/no-test acknowledgement is required.
- [ ] Final approval is available once all gates are satisfied.

---

## 10. Final approval and completion

- [ ] **Final approval** is explicit and human — it is **not** automatic.
- [ ] **Local only:** run completes with `current_step: local_only_complete`,
      a branch name, and manual push/PR instructions; `remote_action: false`,
      `pr_url: null`.
- [ ] **github_cli (optional):** after final approval Pipewright pushes the approved
      branch and creates / reuses a PR via `gh` — **no token pasted**, **no
      auto-merge**, base is never `main`/`master`/`develop`.
- [ ] Verify locally in the **target repo**:

  ```bash
  git log --oneline -5
  git show --stat HEAD
  ```

---

## Expected UI states (quick reference)

| Stage | Expected panel / state |
| --- | --- |
| After request | Chunk plan proposed; "Review the chunk plan". |
| Executing | Running; actions blocked with reasons. |
| Patch failure | Clean failure copy; guarded retry available; nothing committed. |
| Scope violation | Paused; requested extra files shown; approve-scope vs reject. |
| Weak/no test | Final approval blocked until acknowledgement bound to the diff. |
| Strong test | No acknowledgement required. |
| Ready to finish | "Review final result"; final approval available. |
| Local only done | `local_only_complete`, branch + manual instructions. |
| github_cli done | PR created/reused; no auto-merge. |

---

## Screenshots worth capturing for a demo

- The **chunk plan** with **Files Expected** highlighted.
- The **Operator Attention Panel** at a pause (waiting-on + available action +
  blocked reasons + safety checks).
- A **patch failure** with plain-English recovery copy.
- A **scope expansion** prompt showing requested extra files.
- The **weak-test acknowledgement** gate blocking final approval.
- The **local-only complete** result (branch + manual instructions).
- `git log` / `git show --stat` in the target repo proving the scoped commit.

---

## Related smoke docs

- [`patch-retry-smoke.md`](patch-retry-smoke.md) (#26)
- [`scope-expansion-recovery-smoke.md`](scope-expansion-recovery-smoke.md) (#27)
- [`stronger-test-validation-smoke.md`](stronger-test-validation-smoke.md) (#28)
- [`self-use-stability-smoke.md`](self-use-stability-smoke.md)
- [`../demo/local-self-use-demo.md`](../demo/local-self-use-demo.md) — narrated walkthrough
