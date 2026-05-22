# Pipewright

## What This Project Is
AI pipeline that orchestrates multiple models to plan,
code, test, and review — with human approval before every merge.
Never autonomous. Human always in control.

## Tech Stack
Backend:    Python 3.11 / FastAPI
Validation: Pydantic v2 — ALL handoff contracts are Pydantic models
Database:   SQLite via SQLAlchemy (synchronous only)
Frontend:   React 18 / TypeScript / shadcn/ui (Phase 2)
AI:         Anthropic Python SDK
GitHub:     PyGithub (Phase 2)

## MVP Pipeline (Phase 1 only)
Feature Request
→ planner.py
→ coder.py
→ patch_applier.py
→ tester.py
→ approval (CLI)

NOT in Phase 1:
  architect.py (added later for hard tasks only)
  GitHub PR (Week 2)
  React UI (Phase 2)
  Multi-model routing (Phase 2)
  Semantic memory (Phase 3)

## Project Structure
backend/
  main.py
  pipeline/
    orchestrator.py
    planner.py
    coder.py
    patch_applier.py
    tester.py
  memory/
    memory_store.py
  checkpoint/
    checkpoint_store.py
  human/
    approval_gate.py
  models/
    handoff.py
  db/
    database.py
    schema.sql
  config/
    keys.py

## Critical Rules — Never Break
1. Never checkpoint without tests_passed = True
2. All SQLAlchemy operations are synchronous
3. Never store API keys plaintext — .env only
4. Every function has try/except — no unhandled errors
5. All handoff contracts are Pydantic models
6. Coder never writes to disk directly — always via patch_applier
7. patch_applier always backs up before applying
8. patch_applier always rolls back if tests fail

## Current Status
Day 1 — Project setup in progress

## Completed Modules
None yet

## Environment Variables Required
ANTHROPIC_API_KEY=
TARGET_REPO_PATH=
TEST_COMMAND=

## Windows Compatibility
- Never use special unicode characters in print() statements
- Windows default shell encoding crashes on checkmarks and arrows
- Use plain text only: [OK], [DONE], [FAIL], [ERROR]

## Completed Modules
- backend/db/database.py        -- SQLite init, engine, session
- backend/db/schema.sql         -- 4 tables: memory_facts, pipeline_runs, checkpoints, approval_gates
- backend/memory/memory_store.py -- load_hard_facts, add_fact, flag_stale_memories, list_all_facts
- backend/checkpoint/checkpoint_store.py -- save_checkpoint, load_last_checkpoint, load_step_checkpoint
- backend/models/handoff.py     -- PlannerHandoff, CoderHandoff, PatchResult, TestResult, ApprovalRequest
- backend/main.py               -- FastAPI app, /health endpoint
- backend/config/keys.py        -- Pydantic settings, .env loader