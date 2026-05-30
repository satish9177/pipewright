# Local self-use demo

A 10–15 minute end-to-end walkthrough of Pipewright on your own machine, against
a local repo, with no GitHub required. It ends with a real local commit you can
verify with `git log`.

## Prerequisites

- Pipewright set up locally — see [`../setup/local-dev.md`](../setup/local-dev.md).
- At least one working LLM provider key (Gemini by default).
- A small **test repo** to point at — any local git repo you don't mind
  experimenting in. Do **not** use a repo with uncommitted work you care about.
- Optional: GitHub CLI (`gh`) installed and authenticated, only for the
  `github_cli` part below.

## 1. Run the backend

From the repo root with the venv active:

```powershell
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

(Drop `--reload` once you start an actual run.)

## 2. Run the frontend

```bash
cd frontend
npm run dev
```

Windows, if `npm.ps1` is blocked:

```powershell
npm.cmd run dev
```

Open the printed URL (typically `http://localhost:5173`).

## 3. Confirm LLM configuration

```powershell
python scripts\print_role_config.py --validate
```

Every role you plan to use should show `[OK]`. If a role shows `[FAIL]`, fix the
provider/model/key in `.env` before continuing.

## 4. Create a project

1. In the UI, start **New Project**.
2. Paste the **local repo path** of your test repo. Find it by opening that
   folder in a terminal and running `pwd`:
   - Windows: `C:\Users\satis\Projects\my-test-repo`
   - macOS / Linux: `/Users/satish/projects/my-test-repo`
3. Click **Detect project** (read-only — reports git repo, branch, GitHub remote,
   and whether `gh` is installed/authenticated; it changes nothing).
4. Set a **verification command** Pipewright runs after applying changes. Pick the
   simplest thing that proves the repo still works, e.g.:
   - Python: `python -m pytest` or `python -m compileall .`
   - Node/React: `npm test` or `npm run build`
   - Go: `go test ./...`
   - Rust: `cargo check`
5. Choose a **PR mode**. Leave it on **Local only** for this demo.

## 5. `local_only` demo (no GitHub)

1. Introduce a typo in your test repo's `README.md`, for example change
   `Pipewright` to `Pipewirght`, and commit it (so the repo is clean).
2. In Pipewright, submit the request:

   ```text
   Fix the typo "Pipewirght" to "Pipewright" in README.md
   ```

3. **Review the chunk plan.** Pipewright triages the request and proposes a chunk
   plan with the files it expects to touch.
4. **Approve the chunk.**
5. **Execute.** Pipewright plans, applies a guarded patch, and runs your
   verification command. The scope guard ensures only `README.md` is touched.
6. **Final approval.** Approve to complete the run.
7. You should see the **local-only complete** result: a success outcome with a
   branch name and manual instructions (`git checkout <branch>`,
   `git push origin <branch>`, open a PR by hand). No remote action is taken.

### Expected success output

- Run status reaches **complete** with `current_step: local_only_complete`.
- The result includes the working `branch_name` and `manual_instructions`.
- `remote_action: false`, no `push_failed`, and `pr_url: null` (none is expected
  in `local_only`).

### Verify the commit

In the **target repo**:

```bash
git log --oneline -5
git checkout <branch-from-the-result>
git show --stat HEAD
```

You should see the typo fix committed on the Pipewright branch, touching only
`README.md`.

## 6. `github_cli` demo (optional)

Only if `gh` is installed and authenticated and your test repo has a GitHub
remote.

1. Confirm `gh` works:

   ```bash
   gh auth status
   ```

2. Re-detect the project; detection should recommend **GitHub CLI**.
3. Switch the project's PR mode to **GitHub CLI**.
4. Run the same typo-fix request and approve through to final approval.
5. After final approval, Pipewright pushes the approved branch and creates (or
   reuses) a PR via `gh` — **no token is pasted into Pipewright**.

Safety notes that still apply:

- The PR base is never `main` / `master` / `develop`; the default base is
  `pipewright-staging`.
- No auto-merge — you merge the PR yourself.
- If `gh` is missing or unauthenticated, the run fails safely **before** any push
  with a message telling you to run `gh auth login` and retry.

## Troubleshooting

**`npm.ps1` blocked (Windows PowerShell).**
Use the `.cmd` shims: `npm.cmd install`, `npm.cmd run dev`. These bypass the
PowerShell script execution policy.

**Missing provider key.**
`print_role_config.py --validate` shows `[FAIL]` for a role, or a run errors with
`<PROVIDER>_API_KEY is not configured`. Set the key for the provider that role
selects in `.env`. Remember the default fallback is Gemini, so an all-default
config needs `GEMINI_API_KEY`.

**"No effective changes."**
If the file already matches the requested state (e.g. the typo isn't actually
present, or a previous run already fixed it), the no-change / no-effective-change
guards correctly stop before creating an empty commit. Re-introduce the typo and
commit a clean state, then retry.

**No GitHub CLI.**
`github_cli` mode requires `gh` installed and authenticated. Either install and
run `gh auth login`, or stay in **Local only** mode (the default) — the demo
works fully without GitHub.

**Wrong repo path.**
If detection reports the path is not a git repo, you likely pasted a subfolder or
a non-git directory. Use `pwd` at the repo root (the folder containing `.git`) and
paste that exact path. On Windows, paste the full `C:\...` path.

**Target repo on an old `pipewright/*` branch, dirty repo, or `.git/index.lock`.**
See [`../troubleshooting.md`](../troubleshooting.md) for safe cleanup steps.
Pipewright never auto-checks-out, force-pushes, deletes branches, or runs
`git reset --hard` for you.
