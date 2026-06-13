# Pipewright

An AI engineering pipeline orchestrator for existing codebases. It plans,
chunks, patches, tests, requires approval, and creates local commits or PRs
safely.

Pipewright is a workflow and safety layer *around* coding LLMs, not a free-roaming
agent. It breaks a feature request into reviewable chunks, asks a human to
approve the plan, applies changes through a guarded patch layer, runs your
verification command, and only then commits locally or opens a pull request —
always behind a final human approval gate.

---

## What Pipewright is **not**

- **Not** an IDE autocomplete (it is not Copilot / inline completion).
- **Not** a fully autonomous AI coder — every run stops for human approval.
- **Not** a greenfield app generator — it works on repos you already have.
- **Not** a tool that auto-merges to `main` (or `master` / `develop`).

---

## Current status

Pipewright is **local self-use / demo-ready**. It runs on one developer's
machine against a local repo and is suitable for a guided demo, not for
production multi-user use.

Honest scope today:

- ✅ Local single-user self-use and demo flow work end to end.
- ⏸️ Not a production SaaS.
- ⏸️ Deployment — paused.
- ⏸️ Ollama / local-model provider — paused.
- ⏸️ Provider Settings UI — paused (LLM config is env-based only).
- ⏸️ BYOK API keys stored in the database — paused (keys come from `.env`).
- ⏸️ Execution modes (Fast / Balanced / Safe) — paused.

See [`docs/llm/role-based-configuration.md`](docs/llm/role-based-configuration.md)
(*Intentionally paused*) and `docs/architecture/` for the rationale behind each
paused item.

---

## Why Pipewright exists

AI coding tools help one engineer move fast, but they leave three gaps:

- **The copy-paste gap.** People manually shuttle plans and code between
  ChatGPT, Claude, and Codex. There is no single orchestrated flow.
- **The memory / context gap.** Tools lose project context between steps and
  re-ask for the same information.
- **The trust gap.** Fully autonomous agents can mutate a repo in risky ways
  without a human in the loop before the dangerous step.

Pipewright closes these by being the orchestration layer: one flow, chunked
execution, and mandatory human approval before anything risky happens.

---

## Safety & Trust

Safety is the product. The guarantees below are enforced in backend code and
locked by tests (see `docs/stabilization/final-smoke-status.md`).

- **Legacy `/run` endpoint disabled.** The old single-shot path returns HTTP 410;
  `POST /runs/chunked` is the only supported implementation path.
  (`docs/decisions/legacy-run-endpoint-retired.md`)
- **Chunk plan approval before execution.** Large requests are split into chunks
  and a human approves the plan before any code is written.
- **Scope guard / `files_expected` guard.** Each chunk declares the files it may
  touch; coder output outside that set is blocked before patch/test/commit.
- **Out-of-scope edits blocked.** Edits drifting beyond the approved scope are
  rejected, not silently applied.
- **Empty / no-change Git safety.** Coder output with no real file changes never
  reaches patch, test, commit, or push.
- **No-effective-change commit guard.** A chunk that produces no effective diff
  does not create an empty commit.
- **Large-file targeted edits.** Big files get safe targeted edits instead of
  wholesale rewrites.
- **Forbidden paths blocked.** `.env` (and any `.env.*`), `secrets.json`, and
  `credentials.json` can never be read or written by the model; absolute paths and
  `..` traversal that escapes the repo root are also rejected
  (`backend/utils/path_safety.py`). `.env.example` / `.env.sample` are allowed.
- **Branch safety.** Pipewright never opens a PR against `main`, `master`, or
  `develop`; the default base branch is `pipewright-staging`
  (`backend/github/branch_safety.py`).
- **`local_only` by default.** New projects do no remote Git action at all.
- **No auto-merge.** Pipewright opens PRs; humans merge them.
- **Final approval before completion.** A run reaches a local commit or a PR only
  after an explicit final human approval.

### Recovery & validation gates (#26–#28, Operator Panel)

These build on the guarantees above and are complete and manually smoke-validated.

- **Patch failure recovery (guarded retry).** When a generated change cannot be
  applied, the chunk fails cleanly with a plain-English explanation; nothing is
  committed and a guarded retry is offered. Retry eligibility is revalidated
  server-side (`docs/architecture/patch-failure-recovery-v2.md`).
- **Scope expansion requires human approval.** If a chunk tries to touch files
  outside its approved `files_expected`, the run pauses and shows the requested
  extra files. A human must explicitly approve the expanded set before any retry;
  scope is never auto-expanded and `scope_guard` is never weakened
  (`docs/design/scope-expansion-recovery.md`).
- **Weak / no-test acknowledgement gate.** Pipewright classifies whether the
  verification command actually exercised tests (`strong` / `weak` / `none` /
  `unknown`), joining the command string with runtime evidence. A weak or absent
  result must be explicitly acknowledged by a human — bound to the exact diff —
  before final approval. This does **not** prove the code is correct; it only makes
  thin validation visible and forces an acknowledgement
  (`docs/design/stronger-test-validation.md`).
- **Operator Attention Panel (display-only).** A computed, read-only
  `operator_state` surfaces what Pipewright is waiting for, what action is
  available, what is blocked and why, and which process safety checks passed /
  failed / are weak / have not run. It is a display surface only — the existing
  controls remain the real controls, and routes still revalidate every action
  (`docs/design/operator-state-attention-panel.md`).

Project status, what is deferred, and the next roadmap choices live in
[`docs/status/current-state.md`](docs/status/current-state.md) and
[`docs/roadmap/next-phase.md`](docs/roadmap/next-phase.md).

---

## Quick local setup

Full details in [`docs/setup/local-dev.md`](docs/setup/local-dev.md).

### Recommended: one command

These scripts verify prerequisites, create the venv, install dependencies (only
when needed), and start the backend and frontend together:

```powershell
# Windows (PowerShell)
.\scripts\dev.ps1
```

```bash
# macOS / Linux
chmod +x scripts/dev.sh   # first time only
./scripts/dev.sh
```

They print the URLs (backend `http://127.0.0.1:8001`, frontend
`http://127.0.0.1:5173`) and how to stop. The scripts do **not** touch `.env` —
set your keys first (see below). Prefer to run each step yourself? Use the manual
setup below.

### Manual setup

#### 1. Backend

**Windows (PowerShell):**

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r backend\requirements.txt
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

**macOS / Linux:**

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

> Avoid `--reload` during active pipeline runs — reload can interrupt background
> execution. Use it only for development.

Before the first run, copy the env template and set an encryption key:

```powershell
Copy-Item .env.example .env
```

Generate `PIPEWRIGHT_ENCRYPTION_KEY` with:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

#### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

**Windows note:** if PowerShell blocks `npm.ps1` with an execution-policy error,
use the `.cmd` shims instead:

```powershell
npm.cmd install
npm.cmd run dev
```

---

## LLM configuration

Pipewright resolves an LLM **provider** and **model** independently per pipeline
role, configured entirely through environment variables — no UI, no database.
Full reference: [`docs/llm/role-based-configuration.md`](docs/llm/role-based-configuration.md)
and the support matrix in [`docs/llm/provider-matrix.md`](docs/llm/provider-matrix.md).

Simplest config — one model for everything:

```dotenv
DEFAULT_LLM_PROVIDER=gemini
DEFAULT_LLM_MODEL=gemini-2.5-flash-lite
```

Different model per role (each role falls back to the default if unset):

```dotenv
DEFAULT_LLM_PROVIDER=gemini
DEFAULT_LLM_MODEL=gemini-2.5-flash-lite

PLANNER_LLM_PROVIDER=anthropic
PLANNER_LLM_MODEL=claude-sonnet-4-5

CODER_LLM_PROVIDER=openai
CODER_LLM_MODEL=gpt-4o-mini
```

Roles wired into the pipeline today: **triage, planner, coder, summary**.
`REVIEWER_LLM_*` and `ARCHITECT_LLM_*` are accepted and validated but **not yet
invoked** by any stage — setting them is harmless but currently inert.

Notes:

- The hardcoded fallback (no LLM env vars set) is **`gemini` / `gemini-2.5-flash-lite`**.
- Provider API keys are required **only** for providers a role actually selects.
  A non-Gemini-only config never needs `GEMINI_API_KEY`; the all-default config
  still does, because the fallback is Gemini.
- Inspect the resolved config (no secrets, no network):

  ```powershell
  python scripts\print_role_config.py
  ```

- Validate selected providers/models/keys:

  ```powershell
  python scripts\print_role_config.py --validate
  ```

---

## Create a project

1. Open the frontend and start **New Project**.
2. Paste the **local repo path** of the codebase you want Pipewright to work on.
   To find it, open the project folder in a terminal and run `pwd`:
   - Windows: `C:\Users\satis\Projects\pipewright`
   - macOS / Linux: `/Users/satish/projects/pipewright`
3. Click **Detect project**. Detection is read-only — it reports whether the path
   is a git repo, the current branch, any GitHub remote, and whether `gh` is
   installed and authenticated. It never mutates git state.
4. Choose a **verification command** (see below).
5. Choose a **PR mode** (see below). New projects default to `local_only`.

---

## Verification command

Pipewright runs this command after applying changes, to confirm the repo still
works. (This is the field historically called the "test command.") Use your test
suite if you have one; otherwise use a build / typecheck / compile step.

Pipewright can execute any configured verification command. Non-pytest commands
are supported when they pass at baseline, but only pytest currently supports
baseline-aware tolerance of pre-existing failing tests. For non-pytest projects,
start from a green suite or use a build / typecheck / check command that is
expected to pass.

| Stack | Examples |
|-------|----------|
| Python | `python -m pytest` (best-supported baseline-aware runner) or `python -m compileall .` |
| Node / React | `npm test` when green, or `npm run build` / `npm run typecheck` / `npm run lint` if configured |
| Java / Maven | `mvn test` when green, or `mvn -DskipTests package` / `mvn compile` |
| Gradle | `./gradlew test` when green, or `./gradlew build -x test` / `./gradlew compileJava` |
| Go | `go test ./...` when green |
| Rust | `cargo check` or `cargo test` when green |

---

## PR creation modes

Chosen per project; PR creation always happens **only after final human
approval**, never auto-merged.

- **`local_only` (default).** No GitHub required. After final approval the run is
  marked complete with `current_step: local_only_complete` and `manual_instructions`
  (`git checkout <branch>`, `git push origin <branch>`, open a PR by hand). This is
  a successful, no-remote-action outcome (`remote_action: false`, no `push_failed`).
- **`github_cli` (recommended when available).** Used when the repo has a GitHub
  remote and `gh` is installed and authenticated. Pipewright pushes the approved
  branch and creates / reuses a PR via `gh` — **no token is pasted into
  Pipewright**. If `gh` is missing or unauthenticated it fails safely *before*
  pushing.
- **`manual_token` (advanced fallback only).** The legacy PyGithub path using a
  stored token + owner/repo. Hidden behind an *Advanced* toggle.

Details: `docs/decisions/project-pr-modes-and-detection.md` and
`docs/decisions/github-cli-pr-mode.md`.

---

## First demo flow

A complete, copy-pasteable walkthrough (with troubleshooting) lives in
[`docs/demo/local-self-use-demo.md`](docs/demo/local-self-use-demo.md). The short
version:

1. Create or select a small test repo.
2. Introduce a typo in its `README.md`, e.g. `Pipewirght`.
3. In Pipewright, submit:
   `Fix the typo "Pipewirght" to "Pipewright" in README.md`
4. Review the chunk plan.
5. Approve the chunk.
6. Execute.
7. See the `local_only_complete` result.
8. Verify with `git log` in the target repo.

---

## Known limitations

- Local, single-user tool right now — no hosted auth / multi-tenant.
- No visual diff editor yet.
- No per-file approval; approvals are chunk-level and final.
- `reviewer` / `architect` roles are configurable but **not yet invoked** by the
  pipeline.
- **The Adversarial Reviewer Stage is design-only**
  ([`docs/design/adversarial-reviewer-stage.md`](docs/design/adversarial-reviewer-stage.md)).
  No AI review runs in the pipeline today; the design is merged but implementation
  is intentionally deferred pending a priority decision.
- The Operator Attention Panel is **display-only**: it surfaces state but the
  existing run controls remain the real controls. `operator_state` is currently
  computed from chunk read data, so a legacy run with no chunk plan may not surface
  a panel.
- No durable audit trail / run-history table yet.
- No role-based PM / manager views yet.
- No multi-model routing UI — model selection is env-based per role only.
- Repo indexing and verification-command detection are still improving.
- SQLite / in-memory live logs / in-process repo locks are single-instance only.
- GitHub App support is future work (today: `local_only`, `github_cli`,
  `manual_token`).
- Docker Compose local setup is future work — there is no Docker in this repo.

---

## Architecture in 30 seconds

```text
Feature request
   -> Triage / chunk planning
   -> Human approves chunk plan
   -> For each chunk:
        Planner LLM  -> structured plan handoff
        Coder LLM    -> structured code-change handoff
        Patch applier-> backup, apply, validate (scope + forbidden-path guards)
        Tester       -> run the verification command
                        (tests fail -> rollback chunk)
        High-risk approval if needed
        Commit chunk checkpoint
   -> Final human approval
   -> local_only commit  OR  push branch + create PR (github_cli / manual_token)
```

The runtime is a custom Pipewright pipeline (not LangChain / LangGraph) so the
execution, checkpoint, approval, and rollback semantics stay explicit.

---

## Current stack

- Backend: Python 3.11+, FastAPI, Pydantic v2, SQLite via SQLAlchemy
- Frontend: React, TypeScript, Vite
- LLM providers: Gemini (default), Anthropic, OpenAI, DeepSeek — selected per role
- GitHub integration: `gh` CLI (`github_cli`) or PyGithub (`manual_token`)

---

## Important files & docs

- `AGENTS.md` — project rules and operating context for coding agents
- `DECISIONS.md` — major implementation decisions
- `backend/main.py` — FastAPI entry point
- `backend/pipeline/` — planner, coder, patch, tester, approval, chunk execution, PR orchestration
- `backend/utils/path_safety.py` — forbidden-path / traversal protection
- `backend/github/branch_safety.py` — protected base-branch rules
- `docs/setup/local-dev.md` — detailed local setup
- `docs/setup/local-state-reset-backup.md` — what local state is stored, plus backup, reset, interruption, and PR-mode guidance
- `docs/demo/local-self-use-demo.md` — full demo walkthrough + troubleshooting
- `docs/testing/demo-smoke-checklist.md` — demo / readiness smoke checklist
- `docs/status/current-state.md` — current project status (completed / deferred)
- `docs/roadmap/next-phase.md` — recommended next phase and roadmap options
- `docs/llm/role-based-configuration.md` — per-role LLM configuration
- `docs/stabilization/final-smoke-status.md` — safety guarantees and their tests
- `docs/troubleshooting.md` — operational troubleshooting

---

## Testing

Backend unit tests (from the repo root, with the venv active):

```powershell
python -m pytest backend\tests -q -m unit
```

Frontend build validation:

```bash
cd frontend
npm run build
```

---

## Contributing notes

Follow `AGENTS.md` before making code changes. Key rules: keep backend code in
`backend/` and frontend code in `frontend/`, never commit secrets, never bypass
human approval gates, never let the coder write directly to disk, use Pydantic
contracts for model handoffs, and prefer small testable safety improvements over
broad refactors.

---

## License

MIT — see [LICENSE](LICENSE).
