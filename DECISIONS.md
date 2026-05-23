# Pipewright — Decision Log

## 2026-05-23 -- GitHub credentials stored per project

Decision: GitHub token, owner, repo stored
          in projects table per project.
          Not in .env globally.

Reason: Different projects may have different
        GitHub accounts or repos.
        Per-project credentials enable
        multi-tenant support later.

Security note: Tokens stored in SQLite.
        Phase 3: encrypt with Fernet before storing.
        Phase 4: replace with GitHub OAuth flow.

## 2026-05-23 -- GitHub PR is non-fatal

Decision: If GitHub PR creation fails the
          pipeline run still completes.
          Error is logged clearly.
          Files are already on disk and approved.
          Human can create PR manually.

Reason: GitHub API can be unavailable.
        Token can expire.
        These should not invalidate
        an already-approved pipeline run.

---

## 2026-05-23 -- Project configuration moved to SQLite

Decision: Pipewright supports multiple projects through
          persisted project records instead of TARGET_REPO_PATH
          and TEST_COMMAND environment edits.

Reason: Phase 2 needs a React project selector.
        Editing .env for every target repo does not scale,
        blocks multi-project workflows, and is easy to forget.

How it works:
  Human creates a project with POST /projects.
  Project stores name, repo_path, and test_command.
  POST /run now receives project_id plus feature_description.
  Pipeline stages resolve repo path and test command from
  the selected project runtime context.

Rule: .env stores secrets only.
      Project-specific runtime config belongs in SQLite.

---

All significant architectural, security, and product
decisions are logged here with date and reason.
Never delete entries. Add new ones at the top.

---

## 2026-05-23 — Approval gate must be async

Issue: time.sleep() in request_approval() blocked
       the FastAPI event loop during pipeline execution.
       The approve endpoint returned no response
       while the pipeline was waiting for human decision.
       Server appeared completely frozen.

Fix: Changed request_approval() to async function.
     Replaced all time.sleep() with await asyncio.sleep().
     FastAPI event loop now free during the polling wait.
     Approve and reject endpoints respond instantly.

Rule: Never use time.sleep() in any async context.
      Always use await asyncio.sleep() instead.

## 2026-05-23 — Pipeline runs on port 8001

Issue: ai-workflow-platform Docker container runs
       on port 8000. Pipewright also defaulted to 8000.
       Both cannot use the same port simultaneously.
       Swagger showed wrong app when both running.

Fix: Always run Pipewright on port 8001.
     uvicorn backend.main:app --host 0.0.0.0 --port 8001
     Swagger at http://localhost:8001/docs

---

## 2026-05-22 — MVP Phase 1 Complete

All 5 pipeline stages built and unit tested:
  planner, coder, patch_applier, tester, approval_gate
Orchestrator wires them together.
First real end-to-end run happens after this commit.

Next milestone: Run pipeline on real feature
in ai-workflow-platform and verify it saves time.

---

## 2026-05-22 — Approval Gate is CLI-based for MVP

Decision: MVP approval gate is CLI + REST API only.
          No UI. Human approves via curl or Postman
          or a second terminal hitting FastAPI.

Reason: Building a full approval UI before the
        pipeline loop works is premature.
        CLI approval proves the concept.
        React UI comes in Phase 2.

How it works:
  Pipeline prints gate details to terminal.
  Human calls POST /gates/{gate_id}/approve
  Pipeline detects approval via DB polling.
  Nothing merges without this step.

---

## 2026-05-22 — Switch from Anthropic to Gemini API

Decision: Use Google Gemini (gemini-2.5-flash) as primary
          AI provider instead of Anthropic Claude.

Reason: Anthropic API key not available during initial
        development. Gemini key available immediately.

Impact: All pipeline modules use GEMINI_API_KEY from .env.
        Model pinned to gemini-2.5-flash.

Reverting: When Anthropic key available, change:
           PLANNER_MODEL = "claude-sonnet-4-5"
           CODER_MODEL = "claude-sonnet-4-5"
           Replace google.generativeai with anthropic SDK.
           Architecture supports this in 10 minutes.

---

## 2026-05-22 — Data Privacy: What Gets Sent to Gemini

Decision: Approved sending target repo file contents
          and project memory to Google Gemini API.

Reason: ai-workflow-platform is a personal sandbox repo.
        Not customer data. Not sensitive business data.
        Google does not train on API data by default.
        Path traversal protection prevents leaks.

What is sent:
  - Memory hard facts (manually added by founder)
  - File contents from target repo (max 200 lines per file)
  - Only files listed in PlannerHandoff
  - Feature description typed by user

What is never sent:
  - .env file or API keys
  - Git history or entire codebase
  - Files outside target_repo_path

Future mitigation (Phase 3+):
  - Local Ollama model option
  - On-premise deployment for enterprise
  - Data residency controls

---

## 2026-05-22 — SQLite for Phase 1-2, PostgreSQL for Phase 3+

Decision: Use SQLite via SQLAlchemy for MVP.
          Migrate to PostgreSQL when multi-user needed.

Reason: Zero setup, single file, works locally.
        PostgreSQL adds Docker dependency before
        first user exists. Unnecessary friction.

Impact: All SQLAlchemy operations are synchronous.
        Migration to PostgreSQL is a config change
        because SQLAlchemy abstracts the DB layer.

---

## 2026-05-22 — Coder Never Writes to Disk

Decision: coder.py returns file changes as data only.
          patch_applier.py owns all disk writes.

Reason: Separates concerns cleanly.
        patch_applier validates paths, backs up files,
        generates diff, and handles rollback.
        Coder cannot corrupt the repo directly.

---

## 2026-05-22 — Checkpoint Only When Tests Pass

Decision: save_checkpoint() raises ValueError
          if tests_passed=False is passed.

Reason: Checkpointing a failed state means resuming
        continues building on top of the mistake.
        This rule has zero exceptions.

---

## 2026-05-22 — Branch Naming Convention

Decision: Use feature/module-name not feature/day-N-name.

Reason: Day-based names are meaningless 6 months later.
        Module names describe what the branch contains.

Examples:
  feature/coder
  feature/patch-applier
  feature/tester
  feature/approval-gate
  feature/orchestrator


## 2026-05-22 — Gemini Free Tier Rate Limit

Issue: Gemini free tier = 20 requests per day.
       Running full test suite consumes daily quota.
       Pipeline cannot run same day as full test suite.

Decision: Add 60-second retry on 429 errors in
          planner.py and coder.py.

Long term: Get paid Gemini API key before launch.
           ~$0.003 per pipeline run on paid tier.
           $5 credit = ~1600 runs.

Rule added to AGENTS.md:
  Never run API test suite and pipeline on same day.
  Run unit tests freely. API tests sparingly.


  ## 2026-05-22 — Never run uvicorn with --reload during pipeline execution

Issue: uvicorn --reload watches filesystem changes.
       patch_applier writes backup files to backend/backups/
       uvicorn detects backup writes and reloads server.
       Reload kills the background pipeline task mid-execution.

Decision: Use --reload only during code editing.
          Use plain uvicorn (no --reload) when running pipeline.

Commands:
  Development: uvicorn backend.main:app --reload
  Pipeline:    uvicorn backend.main:app --host 0.0.0.0 --port 8000

## 2026-05-23 — Rollback on human rejection

Issue: patch_applier only rolled back when tests failed.
       When human rejected at approval gate,
       files stayed on disk with no cleanup.

Fix: orchestrator calls rollback_patch() immediately
     after human rejection before returning.

Rule: Any pipeline exit that is not 'complete'
      must trigger rollback if patch was applied.

## 2026-05-23 — Local disk changes are temporary

Pipeline writes files to local disk for testing.
After approval and PR creation these local
changes should be discarded.
The GitHub PR is the real deliverable.

After every pipeline run:
  1. Review PR on GitHub
  2. Merge if satisfied
  3. git pull to get changes locally
  4. Discard local pipeline working copy:
     git checkout <changed-files>