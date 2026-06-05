# Local state, reset, and backup

Pipewright is a **local, single-user** tool. Everything it knows lives on your
machine — there is no hosted account, no remote database, and no cloud sync. This
guide explains exactly what local state Pipewright stores, how to back it up, how
to reset it safely, and what to do when an interrupted run leaves a target repo
dirty.

It is safety-first and human-gated: Pipewright never auto-resets, auto-stashes,
or auto-deletes anything described here. You do.

> Related docs: [`local-dev.md`](local-dev.md) (setup), the per-mode details in
> [`../decisions/project-pr-modes-and-detection.md`](../decisions/project-pr-modes-and-detection.md)
> and [`../decisions/github-cli-pr-mode.md`](../decisions/github-cli-pr-mode.md),
> backup-directory cleanup in [`../ops/backup-retention.md`](../ops/backup-retention.md),
> and operational issues in [`../troubleshooting.md`](../troubleshooting.md).

---

## 1. What local state Pipewright stores

Pipewright state is split between **the Pipewright app's own data** and **your
target repositories**, which are separate.

### The Pipewright SQLite database

- **Default path:** `backend/db/pipewright.db`.
- **Override:** set `PIPEWRIGHT_DB_PATH` to point at a different SQLite file (for
  example a mounted volume or an isolated test DB). When unset, the default above
  is used. Missing parent directories for an override path are created
  automatically; the DB file itself is created on first startup.

The database holds:

- **Project config** — repo path, verification ("test") command, branch, PR mode,
  and GitHub owner/repo metadata.
- **Run history** — pipeline runs, chunk plans, chunk results, approval gates, and
  checkpoints.
- **Memory** — approved memory facts and pending memory suggestions.
- **Encrypted GitHub tokens** — only for projects using `manual_token` mode. Tokens
  are encrypted at rest with `PIPEWRIGHT_ENCRYPTION_KEY` (Fernet). They are never
  stored in plaintext and never returned in API responses.

### SQLite WAL sidecar files (expected runtime files)

Pipewright runs SQLite in **WAL (write-ahead logging) mode** for local reliability
(see #32D / `docs/design/local-first-hardening.md`). Next to the database file you
will see transient sidecar files such as:

- `pipewright.db-wal`
- `pipewright.db-shm`
- `pipewright.db-journal` (only with the older rollback-journal mode)

These are **normal and expected** while the backend is running. They are part of
the database, not separate data. They are gitignored (`*.db-wal`, `*.db-shm`,
`*.db-journal`) and must never be committed. Do not hand-edit or delete them while
the backend is running — stop the backend first, and prefer to back up / move the
whole set together (see below).

### `.env` and environment variables

Pipewright reads configuration and secrets from environment variables, typically
via a local `.env` file (gitignored). This includes:

- `PIPEWRIGHT_ENCRYPTION_KEY` — the Fernet key that encrypts stored GitHub tokens.
- **Provider API keys** — e.g. `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` (only the ones a role actually selects). See
  [`../llm/role-based-configuration.md`](../llm/role-based-configuration.md).
- Optional `PIPEWRIGHT_DB_PATH` and `PIPEWRIGHT_BACKEND_HOST`.

`.env` is **not** in the database. The encryption key in particular lives only in
your environment — if you lose it, encrypted tokens in the DB cannot be decrypted.

### Your target repositories (outside the Pipewright DB)

The repos Pipewright works on are **separate from Pipewright's state**. Pipewright:

- may create branches in a target repo,
- applies patches and local commits there (always behind approval), and
- may leave **uncommitted changes** in a target repo if a run is interrupted
  (crash or `Ctrl+C`) mid-execution.

Target-repo state is managed with normal Git, not the Pipewright database. Backing
up or resetting Pipewright's DB does **not** back up or reset your target repos,
and vice-versa.

> Rollback backups Pipewright writes while patching live in `backend/backups/`
> (gitignored) and have their own retention/cleanup flow — see
> [`../ops/backup-retention.md`](../ops/backup-retention.md).

---

## 2. Backup guidance

- **Stop the backend first** (and the frontend) before copying the database. WAL
  sidecar files can change under you while the backend runs; copying a live DB can
  produce an inconsistent snapshot. With the backend stopped, copy the whole set:
  `pipewright.db`, `pipewright.db-wal`, and `pipewright.db-shm` if present.
- **Back up the database and `.env` together.** The DB and
  `PIPEWRIGHT_ENCRYPTION_KEY` are a pair.
- **The DB and `PIPEWRIGHT_ENCRYPTION_KEY` must travel together.** If you restore
  the database on another machine (or after a reset) **without the matching
  encryption key**, any encrypted GitHub tokens stored in the DB become
  **unreadable**. Everything else still works; you would just need to re-enter the
  token for affected `manual_token` projects. (`local_only` and `github_cli`
  projects store no token, so they are unaffected.)
- **Never commit secrets or local state to Git** — not `.env`, not `*.db`, not the
  WAL/journal sidecars, not anything secret-like. These are already gitignored;
  keep them that way.
- **For target repos, use normal Git.** Back them up the way you back up any repo
  (push to a remote, branch, tag). Use `git stash`, `git commit`, or `git reset`
  only when *you* intentionally choose to — Pipewright will not do this for you.

---

## 3. Reset guidance (clearing local Pipewright state)

A "reset" here means clearing **Pipewright's own database** — project config, run
history, memory, and stored tokens. It does **not** mean touching your target repos.

1. **Stop the backend and frontend.** Do not delete or move the DB while the
   backend is running.
2. **Back up first if you want to keep anything.** If you care about run history,
   memory facts, or project config, copy `pipewright.db` (+ `.env`) somewhere safe
   before deleting. A reset is not reversible without a backup.
3. **Move or delete the DB intentionally.** Delete (or rename) `backend/db/pipewright.db`
   — or the file pointed to by `PIPEWRIGHT_DB_PATH` — along with any
   `*.db-wal` / `*.db-shm` / `*.db-journal` sidecars next to it.
4. **Restart the backend.** Pipewright recreates and initializes a fresh database
   on startup (`init_db()` runs the schema; it is safe to run against a new or
   existing DB).
5. **Recreate config.** With a deleted DB you start clean: re-add your projects,
   re-select verification commands and PR modes, and re-enter any GitHub token for
   `manual_token` projects. Your `.env` (keys) is untouched by a DB reset.

**Do not blindly delete target-repo files.** Resetting Pipewright's DB has no
effect on your codebases. If a target repo looks wrong, inspect it separately with
`git status` and decide what to do there using normal Git — see the next section.

---

## 4. Interruption / dirty working tree guidance

If a run is interrupted — the backend crashes, the machine restarts, or you press
`Ctrl+C` mid-execution — a target repo may be left with **uncommitted changes**.
On the next startup, Pipewright detects this and surfaces **read-only, human-gated
guidance** (#32E).

What Pipewright does on startup:

- It reconciles its **own** DB state (a running chunk is reset to pending; a
  running run is marked `interrupted`).
- It then **inspects** the interrupted run's target repo **read-only** (using only
  `git status` / `git rev-parse`) and logs a warning if the working tree is dirty
  or could not be inspected.

What Pipewright **does not** do — by design:

- It does **not** auto-reset, auto-stash, auto-checkout, auto-clean, auto-resume,
  or auto-commit. It takes **no Git action** on your repo. The decision is yours.

What you should do when you see this warning:

1. Open the target repo in a terminal.
2. Run `git status` to see exactly what changed.
3. Inspect the changes (`git diff`).
4. Decide manually whether to **keep**, **commit**, **stash**, or **discard** them.

> **Destructive-command warning.** `git reset --hard` and `git checkout -- .`
> permanently discard uncommitted work. Run them only if you have looked at the
> changes and are certain you want them gone. When in doubt, `git stash` is the
> reversible option.

This is intentionally safety-first: Pipewright shows you the situation and the
options, but never decides for you.

---

## 5. PR mode clarity

PR mode is chosen **per project**. PR creation always happens **only after final
human approval**, and Pipewright **never auto-merges** — it opens PRs; humans merge
them. New projects default to `local_only`.

- **`local_only` (default, safest).** No GitHub required, no push, no PR creation.
  After final approval Pipewright commits locally and shows you manual instructions
  (`git checkout <branch>`, `git push origin <branch>`, open a PR by hand). You
  handle all remote Git yourself.
- **`github_cli`.** Requires the GitHub CLI (`gh`) installed and authenticated
  (`gh auth login`). After approval, Pipewright pushes the approved branch and
  creates / reuses a PR via `gh` — **no token is pasted into Pipewright**. The
  push/PR flow depends on your local Git and `gh` setup; if `gh` is missing or
  unauthenticated, it fails safely *before* pushing.
- **`manual_token` (advanced fallback).** Uses a stored, encrypted GitHub token for
  the PR API call. The token is encrypted at rest with `PIPEWRIGHT_ENCRYPTION_KEY`
  and never returned in responses. Pushing the branch still relies on your local
  Git credentials, so you may still need `git push` credentials configured for the
  remote.

Per-mode details: [`../decisions/project-pr-modes-and-detection.md`](../decisions/project-pr-modes-and-detection.md)
and [`../decisions/github-cli-pr-mode.md`](../decisions/github-cli-pr-mode.md).

---

## 6. Local-only safety boundary

- **Pipewright has no hosted/team authentication today.** The API is unauthenticated
  and trusts whoever can reach it.
- **Run the backend on `127.0.0.1`** (loopback). That keeps the API on your machine
  only. The setup docs and scripts all use `--host 127.0.0.1`.
- **Do not expose the backend publicly or bind to a non-loopback address**
  (including `0.0.0.0`) unless you fully understand that you are exposing an
  unauthenticated API to your network.
- **Startup diagnostics warn about a non-loopback host only when configured via
  `PIPEWRIGHT_BACKEND_HOST`** (#32B). The app cannot see the host that `uvicorn`
  was launched with on the command line, so a CLI-only `--host` bind may **not**
  trigger the warning. Treat the warning as a helpful safety net, not a guarantee —
  you are responsible for how you bind the server.

---

## 7. Troubleshooting checklist

Common local issues and where they come from. For operational specifics (Windows
pytest temp errors, `.git/index.lock`, etc.), see
[`../troubleshooting.md`](../troubleshooting.md).

- **Invalid `PIPEWRIGHT_ENCRYPTION_KEY`.** Startup diagnostics warn if the key is
  missing or not a valid Fernet key (the value is never logged). Generate a valid
  one with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  If you changed the key, previously stored `manual_token` tokens can no longer be
  decrypted — re-enter them.
- **Missing provider API key.** A run using a provider whose key is unset will fail.
  Diagnostics warn for any provider selected by role config that lacks a key. Set
  the matching key (e.g. `GEMINI_API_KEY`) in `.env`.
- **`gh` not installed / not authenticated.** Only relevant for `github_cli`
  projects. Install `gh` and run `gh auth login`; PR creation fails safely until
  then.
- **Repo path missing or not a Git repo.** Diagnostics warn when a project's
  `repo_path` does not exist or has no `.git`. Fix the project's repo path.
- **No verification ("test") command configured.** Runtime test validation has
  nothing to run. Set a verification command on the project.
- **`database is locked`.** WAL + a 5s busy timeout (#32D) make this far less
  likely. If you still see it, you almost certainly have **two backend processes**
  pointing at the same DB — stop the duplicate. Do not delete WAL sidecar files to
  "fix" a lock while the backend is running.
- **Dirty target repo after an interruption.** Expected after a crash/`Ctrl+C`
  mid-run. Follow [section 4](#4-interruption--dirty-working-tree-guidance):
  inspect with `git status` and decide manually. Pipewright takes no Git action.
