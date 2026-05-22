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
Current AI Provider: Google Gemini
  PLANNER_MODEL = "gemini-2.5-flash"
  Reason: Anthropic key not available yet
  Switch back to Claude when key is available
  All pipeline modules use GEMINI_API_KEY from .env
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

pipewright/                     <- repo root
  backend/
    requirements.txt            <- Python deps only
    pytest.ini                  <- pytest config
    main.py                     <- FastAPI entry point
    tests/
      __init__.py
      conftest.py               <- session-level fixtures
      test_foundation.py        <- memory + checkpoint tests
      test_planner.py           <- planner API tests
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
    utils/
      json_helpers.py
    db/
      database.py
      schema.sql
    config/
      keys.py
  frontend/                     <- Phase 2
    package.json                <- JS deps only
    src/
  AGENTS.md
  .env                          <- never committed
  .env.example
  .gitignore
  README.md

## How to Run Tests
Always run from pipewright/ root:
  venv\Scripts\python.exe -m pytest backend/tests/ -v

For foundation tests only (no API calls):
  venv\Scripts\python.exe -m pytest backend/tests/test_foundation.py -v

For planner tests (requires GEMINI_API_KEY):
  venv\Scripts\python.exe -m pytest backend/tests/test_planner.py -v

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
MVP Phase 1 complete — all modules built

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
- backend/pipeline/approval_gate.py
  request_approval, approve_gate,
  reject_gate, get_pending_gates
- backend/tests/test_approval_gate.py
- backend/pipeline/orchestrator.py
  run_pipeline — wires all 5 stages
- backend/tests/test_orchestrator.py

## FastAPI Routes
POST /run           start pipeline run
GET  /runs          list all runs
GET  /runs/{id}     get run status
GET  /gates         list pending approvals
GET  /gates/{id}    get single gate
POST /gates/{id}/approve
POST /gates/{id}/reject

## Additional Rules (added after Day 1 review)

Never install stdlib packages via pip
  (uuid, json, os, pathlib are built-in — never in requirements.txt)

Never silently swallow exceptions
  Either handle explicitly, retry, or raise
  RuntimeError with module name in message

Never pass API keys as function arguments
  Always load from settings internally

Never use 'latest' model strings
  Always pin exact model version as module constant
  Example: PLANNER_MODEL = "claude-sonnet-4-5"

Always set temperature explicitly on AI calls
  Planning/structured output: 0.2
  Creative/review output: 0.4

Always set timeout on Anthropic client
  60 seconds minimum

Always log token usage from every AI call
  input_tokens, output_tokens, model, run_id

Shared utilities live in backend/utils/
  Never copy paste helpers between modules
  clean_json_response() lives in backend/utils/json_helpers.py

## Rules Added After Day 1 Review

Never install stdlib packages via pip
  uuid, json, os, pathlib, datetime are built-in
  Never add them to requirements.txt

Never silently swallow exceptions
  Either handle explicitly, retry, or raise
  RuntimeError with module name in message
  Example: raise RuntimeError("planner.py: failed reason here")

Never pass API keys as function arguments
  Always load from settings internally
  from backend.config.keys import settings
  then use settings.anthropic_api_key

Never use latest model strings
  Always pin exact model version as module-level constant
  Example: PLANNER_MODEL = "claude-sonnet-4-5"

Always set temperature explicitly on AI calls
  Structured output like planning and coding: 0.2
  Review and creative output: 0.4

Always set timeout on all Anthropic client calls
  60 seconds minimum
  httpx.Timeout(60.0) passed to anthropic.Anthropic()

Always log token usage from every AI call
  Log: input_tokens, output_tokens, model, run_id
  Use plain print with [MODULE] prefix
  No special unicode characters (Windows compatibility)

Shared utilities live in backend/utils/
  Never copy paste helpers between pipeline modules
  clean_json_response() -> backend/utils/json_helpers.py
  safe_parse_json() -> backend/utils/json_helpers.py
  Import from there in every module that calls Claude


## Data Privacy — What Gets Sent to Gemini API

Explicitly approved on: 2026-05-22

What IS sent to Gemini:
  - Hard facts from memory store (manually added by founder)
  - File contents from target repo (max 200 lines per file)
  - Only files listed in PlannerHandoff (files_to_read, files_to_modify)
  - The feature description typed by the user

What is NEVER sent to Gemini:
  - .env file or API keys
  - Git history
  - Entire codebase
  - Any file not listed in the plan
  - Anything outside target_repo_path

Why this is acceptable now:
  - ai-workflow-platform is a personal sandbox repo
  - Not customer data, not sensitive business data
  - Google does not train on API data by default
  - Path traversal protection prevents accidental leaks

Long term (Phase 3+):
  - Add local Ollama model option for privacy-sensitive users
  - Add on-premise deployment option for enterprise
  - Add data residency controls


  ## Test Commands

Run unit tests only (no API calls, run anytime):
  venv\Scripts\python.exe -m pytest backend/tests/ -v -m unit

Run API tests only (calls Gemini, rate limited):
  venv\Scripts\python.exe -m pytest backend/tests/ -v -m api -s

Run all tests (careful — Gemini free tier = 20 req/min):
  venv\Scripts\python.exe -m pytest backend/tests/ -v -s

Markers:
  unit = no external API calls, fast, safe to run anytime
  api  = calls Gemini, slow, subject to rate limits
