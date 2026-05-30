# Pipewright

Pipewright is an AI engineering pipeline orchestrator for safely turning feature requests into GitHub pull requests.

It coordinates multiple AI-driven stages: triage, planning, coding, patching, testing, review, and approval around an existing codebase. The goal is not to let an AI freely mutate your repository. The goal is to make AI-assisted engineering **observable, resumable, reviewable, and safe**.

Pipewright is not a fully autonomous coding bot. It is a workflow layer around coding agents: the system that adds chunking, checkpoints, rollback, approval gates, audit trails, and PR safety around LLM-generated code.

---

## Why Pipewright exists

Modern AI coding tools help one engineer move quickly, but they still leave a lot of safety and coordination work to the human:

- Breaking a large feature into safe, independently testable chunks
- Keeping each model constrained to the approved scope
- Applying patches safely instead of letting the model write directly to disk
- Running tests before saving progress
- Rolling back failed chunks
- Pausing for human approval on risky changes
- Creating a PR only after final approval
- Remembering project-specific rules across runs
- Showing a full audit trail of what happened

Pipewright is built for that layer.

The long-term vision is a durable background engineering agent that can survive restarts, approval delays, provider failures, and long-running tasks while still keeping humans in control before risky changes and PR creation.

---

## Core principles

1. **Human approval is mandatory.** Pipewright does not merge code and does not bypass approval gates.
2. **Chunk first, execute second.** Large requests are split into smaller chunks. The human approves the chunk plan before execution starts.
3. **Tests gate progress.** A checkpoint is only trusted when the relevant tests pass.
4. **Models produce contracts, not loose text.** Inter-stage handoffs are typed Pydantic contracts.
5. **The coder does not write directly to disk.** File changes are treated as data and applied through the patch layer.
6. **Rollback is part of the design.** Failed tests, rejected high-risk chunks, and unsafe patches are rolled back.
7. **Auditability matters.** The system should be able to answer what happened, which model did it, what was approved, and what was pushed.

---

## Architecture in 30 seconds

```text
Feature request
    |
    v
Triage / chunk planning
    |
    v
Human approves chunk plan
    |
    v
For each chunk:
    Planner LLM -> structured plan handoff
        |
        v
    Coder LLM -> structured code-change handoff
        |
        v
    Patch applier -> backup, apply, validate diff
        |
        v
    Tester -> run project test command
        |
        +--> tests fail -> rollback chunk
        |
        v
    High-risk approval if needed
        |
        v
    Commit chunk checkpoint
    |
    v
Final human approval
    |
    v
Push branch
    |
    v
Create GitHub PR
```

The current runtime is a custom Pipewright pipeline rather than LangChain or LangGraph. LangGraph may be evaluated later for durable agent orchestration, but the current recommendation is to keep the core runtime custom until the execution, checkpointing, approval, and rollback semantics are stable.

---

## Current stack

- Backend: Python 3.11+, FastAPI
- Validation: Pydantic v2
- Database: SQLite with synchronous SQLAlchemy
- Frontend: React, TypeScript, Vite
- Current AI provider: Google Gemini
- GitHub integration: PyGithub
- Runtime model: custom pipeline with checkpoints, approval gates, local Git operations, live events, and PR creation

Current implementation is Gemini-based. A provider abstraction and role-based model configuration are planned, but multi-provider routing is not fully implemented yet.

---

## Using different LLMs per role

Pipewright resolves an LLM provider/model independently for each pipeline role
(triage, planner, coder, summary), so you can run everything on one model or
assign a different provider/model per role using only environment variables.

```dotenv
DEFAULT_LLM_PROVIDER=gemini
DEFAULT_LLM_MODEL=gemini-2.5-flash-lite
PLANNER_LLM_PROVIDER=anthropic
PLANNER_LLM_MODEL=claude-sonnet-4-5
CODER_LLM_PROVIDER=openai
CODER_LLM_MODEL=gpt-4o-mini
```

Verify your resolved config locally (no secrets printed):

```powershell
venv\Scripts\python.exe scripts\print_role_config.py --validate
```

See [docs/llm/role-based-configuration.md](docs/llm/role-based-configuration.md)
for the full resolution order, supported roles, and current limitations, and
[docs/llm/provider-matrix.md](docs/llm/provider-matrix.md) for supported
providers and models.

---

## Current status

Current milestone completed: **Phase 2D Production Readiness + Frontend Stabilization**.

Phase 2D added backend hardening and frontend stabilization, including:

- Project API token response sanitization
- GitHub token encryption at rest
- Project/repo execution locks for local repo safety
- Request validation for high-risk inputs
- Provider retry fixes
- Startup recovery for interrupted run state
- Safer path handling for coder, patching, and repo indexing
- Checkpoint semantic cleanup
- Logging/config foundations
- Chunked run UI, approval UI, final approval, push/PR UI, status badges, and project GitHub config UI

This does not mean Pipewright is fully production-ready. It means the MVP has been hardened enough to continue toward deployment readiness with known limitations documented.

---

## Current focus

Before deployment, the current focus is:

1. Memory M1: Project State Memory Lite
2. LLM-M1: Manual role-based multi-provider/model configuration
3. Local validation runs
4. Phase 2E: Deployment readiness / GCP MVP

---

## Long-term direction

Pipewright is intended to become:

- An AI engineering pipeline orchestrator
- A safety layer around LLM-generated code
- A chunked execution system for existing repositories
- A human-in-the-loop pull request workflow
- A durable background runtime for long-running coding tasks

The architecture direction is documented in:

- `docs/architecture/memory-architecture.md`
- `docs/architecture/multi-llm-architecture.md`
- `docs/architecture/durable-agent-runtime.md`

---

## Important files

- `AGENTS.md`: project rules and operating context for coding agents
- `DECISIONS.md`: major implementation decisions
- `backend/main.py`: FastAPI application entry point
- `backend/routes/`: backend route modules
- `backend/pipeline/`: planner, coder, patch, tester, approval, chunk execution, and PR orchestration
- `backend/projects/project_store.py`: project persistence and GitHub config storage
- `backend/security/secrets.py`: GitHub token encryption helpers
- `backend/utils/path_safety.py`: path safety validation for repo file access
- `backend/db/schema.sql`: SQLite schema
- `frontend/src/api/client.ts`: frontend API client and shared types
- `frontend/src/pages/`: frontend route pages
- `docs/ops/pre-deployment-checklist.md`: deployment preparation checklist
- `docs/frontend/frontend-smoke-checklist.md`: manual frontend smoke checklist

---

## Run locally

Create and configure environment variables:

```powershell
Copy-Item .env.example .env
```

At minimum, set:

```text
GEMINI_API_KEY=
PIPEWRIGHT_ENCRYPTION_KEY=
```

Generate a local encryption key with:

```powershell
venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Start the backend:

```powershell
venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8001
```

For development reload only:

```powershell
venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8001
```

Avoid `--reload` during active pipeline runs because reload can interrupt background execution.

Start the frontend:

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

Build the frontend:

```powershell
cd frontend
npm.cmd run build
```

---

## Testing

Run backend unit tests from the repository root:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m unit
```

Run API/live provider tests separately. These may call Gemini and may be rate-limited:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\ -v -m api -s
```

Run frontend build validation:

```powershell
cd frontend
npm.cmd run build
```

---

## Known limitations

- SQLite/local DB is single-instance only.
- In-memory live logs are non-durable.
- In-process repo locks are single-instance only.
- Worker queue is not introduced yet.
- PostgreSQL and Alembic are not introduced yet.
- Multi-provider abstraction is designed but not fully implemented yet.
- Project memory architecture is designed but Memory M1 is not fully implemented yet.
- Durable background execution is designed but not implemented yet.
- GitHub token encryption is local Fernet-based, not KMS-backed.
- Completion Summary still renders raw JSON in some UI places.
- Frontend is not fully mobile responsive.
- LangChain and LangGraph are not used in the current runtime.

---

## Roadmap

Near-term:

1. Memory M1: Project State Memory Lite
2. LLM-M1: Manual role-based provider/model configuration
3. Local validation runs against real target repositories
4. Phase 2E: Deployment readiness / GCP MVP

Later:

- PostgreSQL migration and Alembic migrations
- Durable worker queue and background job runtime
- Distributed repo locking
- Durable event/log storage
- Deeper provider abstraction and routing
- Slack/email approval integrations
- Richer project memory and audit views
- Evaluation of LangGraph or similar tools where they fit Pipewright's safety model

---

## What Pipewright is not

Pipewright is not:

- A fully autonomous coding bot
- A replacement for human review
- A tool that merges directly to main
- A general LangChain or LangGraph app
- A promise that LLM-generated code is safe without tests and approval
- A production deployment platform yet

Pipewright is the orchestration and safety layer around AI-assisted engineering.

---

## Contributing notes

Follow `AGENTS.md` before making code changes.

Important project rules include:

- Keep backend code in `backend/`.
- Keep frontend code in `frontend/`.
- Do not store secrets in committed files.
- Do not bypass human approval gates.
- Do not let the coder write directly to disk.
- Use Pydantic contracts for model handoffs.
- Keep Windows-compatible commands and plain ASCII logs.
- Prefer small, testable safety improvements over broad refactors.

---

## License

This project is licensed under the MIT License.

See [LICENSE](LICENSE) for details.
