# Pipewright — Decision Log

All significant architectural, security, and product
decisions are logged here with date and reason.
Never delete entries. Add new ones at the top.

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
