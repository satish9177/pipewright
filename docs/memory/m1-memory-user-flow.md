# Memory M1 User Flow

## Overview

Memory M1 adds human-managed, project-scoped memory to Pipewright.

Memory facts are short, validated pieces of project context such as stack,
test commands, safety rules, architecture notes, and reviewer preferences. Active
project memory is injected into triage, planner, and coder prompts as advisory
context.

Memory suggestions are not active memory. Bootstrap suggestions are generated
from deterministic repo and config signals, then stay pending until a human
approves or rejects them.

Memory is advisory. Current source code and explicit user instructions always
win over memory when they conflict.

## Memory Lifecycle

1. A new project starts with no active memory.
2. The user opens the project Memory page.
3. The user generates bootstrap suggestions from repo and config files.
4. Suggestions are stored as pending suggestions, not active memory.
5. The user reviews each suggestion and its evidence.
6. Approved suggestions become active memory facts.
7. Active memory appears in prompt preview and is eligible for triage, planner,
   and coder injection.
8. The user can edit, verify, or archive memory facts over time.
9. Archived, stale, and historical memory remains visible for management, but is
   not injected into prompts.

## New Project Flow

1. Create the project with its local repo path and test command.
2. Open the project Memory tab or page.
3. Click **Generate bootstrap suggestions**.
4. Review each suggestion's `evidence_path` and `evidence_excerpt`.
5. Approve accurate suggestions.
6. Reject inaccurate suggestions with a reason.
7. Open prompt preview and confirm the expected active memory appears.
8. Start a chunked run from the project dashboard.

## Existing Project Flow

Use manual memory management when the repo already has known conventions or
when bootstrap suggestions are not enough.

Common actions:

- Add manual memory for stack, tests, architecture, security, or style.
- Edit a memory fact when wording is unclear or the repo changes.
- Verify a memory fact after confirming it still matches the source code.
- Archive outdated memory with a reason.
- Regenerate bootstrap suggestions after archiving active memory that previously
  blocked duplicate suggestions.

## Bootstrap Suggestions Behavior

Bootstrap suggestions are deterministic. No LLM is used for stack detection.

The bootstrap scanner:

- Recursively discovers known manifest and config files.
- Uses a bounded scan with `BOOTSTRAP_MAX_DEPTH = 5`.
- Uses `BOOTSTRAP_MAX_MANIFEST_FILES = 100`.
- Ignores heavy or noisy folders such as `.git`, `node_modules`, `venv`,
  `.venv`, `dist`, `build`, `target`, `__pycache__`, `coverage`, `vendor`,
  `generated`, `.next`, `out`, `tmp`, and `logs`.
- Ignores low-confidence paths such as `examples`, `example`, `samples`,
  `sample`, `demo`, `template`, `templates`, and `fixtures`.
- Detects stack facts from file contents first.
- Uses folder names only as weak hints for scope.
- Does not read `.env` files.
- Keeps suggestions pending until approved.
- Skips suggestions that duplicate active memory.
- Skips pending duplicate suggestions.

Examples of nested files that can produce suggestions:

- `backend/requirements.txt`
- `service-main/requirements.txt`
- `apps/web/package.json`
- `services/payroll/package.json`
- `backend/db/schema.sql`

## Prompt Preview

Prompt preview shows the exact advisory memory block returned by the backend for
a project and role.

If preview is empty, no active eligible memory would be injected for that role.

Supported role choices are:

- `triage`
- `planner`
- `architect`
- `coder`
- `reviewer`
- `summary`

Prompt preview only includes active memory. Archived, stale, and historical
memory is excluded.

The memory block uses category/scope prefixes such as:

```text
[stack/backend] Backend uses FastAPI.
[test/tests] Run backend unit tests with pytest.
```

The block also includes source-code-wins advisory wording. It tells the model to
follow current source code and explicit user instructions when they conflict
with memory.

## Safety Rules

Memory M1 follows these safety rules:

- No global memory fallback.
- Every memory query is scoped by `project_id`.
- Memory from one project must never appear in another project.
- There is no hard delete in M1. Archive memory instead.
- Bootstrap suggestions are never auto-saved as active memory.
- AI does not auto-save long-term memory in M1.
- Secrets, credentials, emails, phone numbers, payment card numbers, and
  prompt-injection markers are rejected before storage.
- Archived, stale, and historical memory is not injected.
- Memory is advisory. Source code and explicit user instructions win.

## Known Limitations

- Memory is project-level only, not module-level.
- There is no semantic or vector memory.
- There is no repo-reality conflict blocking yet.
- There are no post-run AI memory suggestions yet.
- PostgreSQL and Alembic are not introduced for memory audit history yet.
- Stale detection is not automatically driven by stack or repo changes.
- Monorepo and module-level memory are deferred to M2.

## Future Phases

- M1.5: repo reality vs. memory conflict detection.
- M2: PostgreSQL, Run/Thread Memory, audit history, and module-level memory.
- M3: Semantic Memory and pgvector with fingerprint filtering.
