# Pipewright Memory Architecture — M1, M2, M3 Design

**Status:** Design only. Implement M1. Do not implement M2 or M3 yet.
**Audience:** Pipewright maintainers and Codex (implementer).
**Mode:** Adversarial. The point of this document is to find what breaks, not to celebrate what works.

---

## 0. Critical Findings on the Existing Code (Read This First)

Before any new design is layered on, the current `memory_facts` implementation has bugs that violate the M1 safety rules you wrote yourself. M1 cannot ship until these are fixed.

| # | Finding | Severity | Evidence |
|---|---------|----------|----------|
| 1 | **No `project_id` column on `memory_facts`.** All memory is global across every project in the DB. | **Critical** | `backend/db/schema.sql` — `memory_facts` has no FK to `projects`. |
| 2 | **`load_hard_facts()` returns every active fact regardless of project.** Memory written for Project A is injected into Project B's planner/coder prompts. | **Critical** | `backend/memory/memory_store.py` — `SELECT content FROM memory_facts WHERE is_stale=0 AND status='active'`. No `WHERE project_id = ?`. |
| 3 | **`flag_stale_memories(days=90)` ages by `created_at` only.** A stable fact like "Backend is Python 3.11" gets marked stale at day 91 even though nothing changed. Drives correct memory out of the prompt for the wrong reason. | High | Same file. |
| 4 | **No secrets/PII filter on `add_fact`.** A human can paste an API key into the suggestion UI and it will be saved and then re-injected into every model prompt forever. | **Critical** | `add_fact()` accepts arbitrary `content` after a `strip()`. |
| 5 | **No token budget enforcement on `load_hard_facts()`.** Returns every active row joined with `\n`. As facts accumulate, the prompt will silently exceed model limits before any other stage notices. | High | Same file. |
| 6 | **No dedup, no scope, no category.** Two near-identical facts can both be active. Reviewer gets the same fact dumped on it as the coder. | Medium | Schema has no `category`, `scope`, or content-hash. |
| 7 | **No human-confirmation gate.** `add_fact` is a single function. Anyone with the endpoint can write long-term memory. The "AI suggests, human approves" contract from your own planning docs is not enforced anywhere in code. | High | No `memory_suggestions` table or workflow exists. |
| 8 | **No project context loaded inside the chunked orchestrator.** `planner.py` and `coder.py` call `load_hard_facts()`, but no scoping is passed in. Even fixing the column will not flow through unless callers are updated. | High | `backend/pipeline/planner.py`, `backend/pipeline/coder.py`. |

**Implication:** "M1 is small" only if you treat the existing module as a prototype to extend. In practice M1 starts with a schema migration plus a rewrite of `memory_store.py`, then the new features. Estimate accordingly.

---

## 1. Overall Memory Architecture

Three layers, separated by lifetime and trust.

### 1.1 Run / Thread Memory (M2)

**Lifetime:** One run. Discarded or cold-archived after the run ends.
**Trust:** Internal scratch. Never injected verbatim into another run.
**Stores:** Current feature description, approved chunk plan, current chunk number, previous chunk's `CoderHandoff`, files changed so far in this run, test failures, review feedback, approval/rejection decisions, final PR URL.
**Never stores:** Anything intended to outlive this run. Anything a future run is supposed to learn from. (Promotion to Project State Memory is an explicit, audited step.)

### 1.2 Project State Memory (M1 Lite, M2 full)

**Lifetime:** Project lifetime. Survives runs, restarts, and reinstalls.
**Trust:** Advisory. Source code beats memory on conflict. (See §6.)
**Stores:** Tech stack, repo structure rules, test commands, migration tool, style guides, security rules, architectural decisions, forbidden paths, reviewer preferences.
**Never stores:** Secrets, API keys, tokens, customer data, PII, file diffs, code blobs, run-specific details, "we did X in run abc123."

### 1.3 Semantic Memory (M3)

**Lifetime:** Project lifetime, but each entry is recall-on-demand only.
**Trust:** Hint-only. Lower priority than Project State Memory in conflicts.
**Stores:** Past rejected approaches, recurring review findings, old bug fixes, migration history, PR summaries — embedded as vectors.
**Never stores:** Anything that should be a hard rule. Hard rules belong in Project State Memory.

### 1.4 Memory Flow

```
[Run Memory] --(end of successful run + AI suggestion)--> [Suggestion queue]
                                                                |
                                                          Human approval
                                                                |
                                                                v
                                                  [Project State Memory] -> injected
                                                                |
                                            (M3) summarized + embedded
                                                                v
                                                       [Semantic Memory] -> retrieved
```

**The promotion direction is one-way and gated.** Project State Memory cannot be silently written by Run Memory. The human is the bridge.

---

## 2. Phase M1 — Project State Memory Lite (Implement Now)

Goal: Stop the planner, coder, and reviewer from asking or guessing the basic facts of *this specific project*, on the current SQLite stack, with no new infrastructure.

### 2.1 Schema (SQLite, additive migration)

`memory_facts` is extended. We do **not** drop the existing table — we add columns and backfill.

```sql
-- Migration: 0001_memory_lite.sql

ALTER TABLE memory_facts ADD COLUMN project_id TEXT;
ALTER TABLE memory_facts ADD COLUMN category   TEXT DEFAULT 'other';
ALTER TABLE memory_facts ADD COLUMN scope      TEXT DEFAULT 'global';
ALTER TABLE memory_facts ADD COLUMN priority   INTEGER DEFAULT 100;
ALTER TABLE memory_facts ADD COLUMN content_hash TEXT;     -- sha256(lower(trim(content)))
ALTER TABLE memory_facts ADD COLUMN approved_by TEXT;      -- human user id / 'founder'
ALTER TABLE memory_facts ADD COLUMN approved_at DATETIME;
ALTER TABLE memory_facts ADD COLUMN last_verified_at DATETIME;
-- Existing columns retained: id, content, source, added_by, created_at,
-- updated_at, is_stale, status, archived_reason

CREATE INDEX IF NOT EXISTS ix_memory_facts_project_active
    ON memory_facts(project_id, status, is_stale);

CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_facts_project_hash_active
    ON memory_facts(project_id, content_hash)
    WHERE status = 'active';
```

**Backfill rule:** Every existing row is set to `project_id = NULL` and `status = 'archived'`, `archived_reason = 'pre-M1; no project scope'`. Do not auto-assign them to a project. They are unsafe by construction (see Finding #2).

Also add a suggestions table:

```sql
CREATE TABLE IF NOT EXISTS memory_suggestions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT,
    chunk_number INTEGER,
    suggested_by TEXT NOT NULL,           -- 'planner' | 'coder' | 'reviewer' | 'triage'
    category TEXT NOT NULL,
    scope TEXT DEFAULT 'global',
    content TEXT NOT NULL,
    rationale TEXT,
    status TEXT DEFAULT 'pending',        -- pending | approved | rejected | duplicate
    decided_by TEXT,
    decided_at DATETIME,
    rejection_reason TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE INDEX IF NOT EXISTS ix_memory_suggestions_project_status
    ON memory_suggestions(project_id, status);
```

### 2.2 Categories (closed enum in M1)

| Category | Used For | Example |
|----------|----------|---------|
| `stack` | Languages, frameworks, runtimes | "Backend is Python 3.11 + FastAPI; sync SQLAlchemy." |
| `structure` | Where code lives | "Backend lives in `backend/`; routes in `backend/routes/`." |
| `test` | How to run tests | "Run backend tests via `pytest -m unit`." |
| `db` | DB / migration rules | "SQLite via SQLAlchemy. No Alembic yet. Schema in `backend/db/schema.sql`." |
| `style` | Conventions | "Never use `time.sleep()` in async paths; use `await asyncio.sleep()`." |
| `security` | Hard prohibitions | "Never log API keys. Never send `.env` to providers." |
| `architecture` | Decisions | "Coder never writes to disk; `patch_applier` owns disk writes." |
| `deploy` | Deploy / branch rules | "Push to `pipewright/*` branch only; one PR per run." |
| `forbidden_paths` | Paths the system must not touch | "Never modify files in `target_repo/.git/`." |
| `reviewer_pref` | Review style | "Reviewer must flag any new SQL string concatenation as injection risk." |
| `other` | Catch-all (discouraged) | — |

Closed enum because open categories degrade into "untyped" overnight. Stays closed until M2.

### 2.3 Scope

`scope` is one of: `global`, `backend`, `frontend`, `tests`, `infra`. M1 ships with just these five. `global` is the default and what you'll have most of. Folder-level scopes (e.g., `backend/pipeline/`) are M3.

### 2.4 Status values

`active` → injected.
`archived` → not injected. Kept for audit. Reason required.
`stale` → flagged as possibly outdated. **Still injected**, but tagged in the prompt. (See §7.)
`pending` is **not** on `memory_facts`. Pending lives on `memory_suggestions`. This separation prevents the "is this real memory or a pending suggestion?" footgun.

### 2.5 API endpoints (FastAPI, M1)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/projects/{project_id}/memory` | List all facts for a project. Query params: `status`, `category`, `scope`. |
| `POST` | `/projects/{project_id}/memory` | Human creates a fact directly. |
| `PATCH` | `/projects/{project_id}/memory/{id}` | Edit content, category, scope, priority. |
| `POST` | `/projects/{project_id}/memory/{id}/archive` | Archive with required `reason`. |
| `POST` | `/projects/{project_id}/memory/{id}/verify` | Sets `last_verified_at` to now. Resets staleness. |
| `GET` | `/projects/{project_id}/memory/suggestions` | List suggestions. Filter by `status`, `run_id`. |
| `POST` | `/projects/{project_id}/memory/suggestions/{id}/approve` | Promote to `memory_facts` (atomic). |
| `POST` | `/projects/{project_id}/memory/suggestions/{id}/reject` | Mark rejected with required `reason`. |

There is **no** `DELETE` on `memory_facts` in M1. Archive only. (See §6.)

### 2.6 UI surfaces (M1)

1. **Project Memory page** — Table of active facts grouped by category. Edit, archive, verify. "Add fact" button.
2. **Suggestion inbox** — Pending suggestions from the most recent run(s), grouped by run. Approve / Reject (with reason) / Edit-then-approve.
3. **Run detail — Memory tab** — Read-only view showing exactly the memory block that was injected for this run. This is the audit surface; you will need it the first time the model does something weird.

The Memory tab on Run detail is non-obvious and the most important. Without it, debugging "why did the coder think we use Yarn" requires reading server logs.

### 2.7 Prompt injection — token budget and ordering

**Hard budget:** 1500 tokens for the memory block. (Lower than the 2000 mentioned in your planning doc, because handoff contracts also consume the planner/coder budget. 1500 leaves headroom.)

**Selection order (greedy fill until budget):**

1. `category in {security, forbidden_paths}` — always first, never skipped, even if over budget. If these alone exceed 1500 tokens you have a problem larger than memory, and the system should fail loudly.
2. `category in {stack, db, test}` — almost always relevant.
3. Scope match (e.g., for a backend coder, prefer `scope in {global, backend}`).
4. `category in {architecture, style, deploy, reviewer_pref}`.
5. `priority` ascending (lower = more important).
6. `last_verified_at` desc (recently verified beats unverified).
7. Drop `is_stale = 1` last; they go in only if budget remains.

**Per-role differentiation in M1 (keep it simple):**

| Role | Categories included |
|------|---------------------|
| Triage | `stack`, `structure`, `test` only. Triage just needs to size things. |
| Planner | All except `reviewer_pref`. |
| Architect | All except `reviewer_pref`. |
| Coder | All except `reviewer_pref`. Filtered by `scope` matching the chunk's files when possible. |
| Reviewer | All, with `reviewer_pref` boosted to priority 0. |
| Triage (high-risk approval summary) | Top 5 facts only, security + forbidden_paths only. |

### 2.8 Avoiding break of current chunked execution

The current chunked flow already calls `load_hard_facts()` from `planner.py` and `coder.py`. Keep that function name and signature compatible:

```python
def load_hard_facts(project_id: str | None = None) -> str:
    ...
```

If `project_id is None`, return empty string and log a warning. Do **not** fall back to global. The warning is intentional — every caller must be updated to pass `project_id` from the active project runtime, and a silent fallback masks bugs.

Wire the project_id through:
- `backend/projects/runtime.py` (active project context) already exists; expose `current_project_id()`.
- `planner.run_planner`, `coder.run_coder`, `reviewer.run_reviewer` (when it exists), and the chunk plan triage call must pass it.

### 2.9 Pydantic schemas (M1)

```python
from pydantic import BaseModel, Field, field_validator
from typing import Literal
from datetime import datetime

Category = Literal[
    "stack", "structure", "test", "db", "style", "security",
    "architecture", "deploy", "forbidden_paths", "reviewer_pref", "other"
]
Scope = Literal["global", "backend", "frontend", "tests", "infra"]
Status = Literal["active", "archived", "stale"]
SuggestionStatus = Literal["pending", "approved", "rejected", "duplicate"]

class MemoryFactCreate(BaseModel):
    content: str = Field(min_length=4, max_length=400)
    category: Category = "other"
    scope: Scope = "global"
    priority: int = Field(default=100, ge=0, le=1000)
    source: str = Field(max_length=100)        # "human" | "run:<run_id>" | "import"
    added_by: str = Field(max_length=100)

    @field_validator("content")
    @classmethod
    def reject_secrets(cls, v: str) -> str:
        # see §5.1 for the regex set
        ...

class MemoryFact(MemoryFactCreate):
    id: str
    project_id: str
    status: Status
    is_stale: bool
    content_hash: str
    approved_by: str | None
    approved_at: datetime | None
    last_verified_at: datetime | None
    created_at: datetime
    updated_at: datetime | None
    archived_reason: str | None

class MemorySuggestionCreate(BaseModel):
    project_id: str
    run_id: str | None = None
    chunk_number: int | None = None
    suggested_by: Literal["planner", "architect", "coder", "reviewer", "triage"]
    category: Category
    scope: Scope = "global"
    content: str = Field(min_length=4, max_length=400)
    rationale: str | None = Field(default=None, max_length=400)

class MemorySuggestion(MemorySuggestionCreate):
    id: str
    status: SuggestionStatus
    decided_by: str | None
    decided_at: datetime | None
    rejection_reason: str | None
    created_at: datetime
```

**Hard length limit on `content`: 400 chars.** Memory entries that need more than 400 chars are almost always overspecific (see §5, vague vs. overspecific). Force the human to split them.

### 2.10 Example memory entries (a real project profile)

| # | Content | Category | Scope |
|---|---------|----------|-------|
| 1 | "Backend: Python 3.11 + FastAPI, synchronous SQLAlchemy on SQLite. Pydantic v2." | stack | backend |
| 2 | "Frontend: Vite + React + TypeScript + Tailwind + shadcn/ui." | stack | frontend |
| 3 | "Backend lives in `backend/`. Routes in `backend/routes/`. Pipeline stages in `backend/pipeline/`." | structure | backend |
| 4 | "Run backend tests with `pytest -m unit`. Do not run `-m api` unless explicitly asked." | test | tests |
| 5 | "Never use `time.sleep()` in async paths; use `await asyncio.sleep()`." | style | backend |
| 6 | "Coder never writes to disk. `patch_applier` owns all disk writes." | architecture | backend |
| 7 | "Pipewright API runs on port 8001, not 8000." | deploy | infra |
| 8 | "Never modify files in the target repo's `.git/` directory." | forbidden_paths | global |
| 9 | "Reviewer must flag any new raw SQL concatenation as injection risk." | reviewer_pref | backend |
| 10 | "Checkpointing fails if `tests_passed=False`. This rule has zero exceptions." | architecture | backend |

### 2.11 Example injected prompt block

This is the exact format injected into the planner/coder/reviewer system prompt. Stable format so the model learns to read it.

```
=== PROJECT MEMORY (advisory; source code wins on conflict) ===
Project: pipewright (proj-13605886)
Generated: 2026-05-26T10:14:22Z
Entries: 9 active, 0 stale shown
Budget used: 612 / 1500 tokens

[security] Never log API keys or send .env files to providers.
[forbidden_paths] Never modify files in the target repo's .git/ directory.
[stack/backend] Backend: Python 3.11 + FastAPI, sync SQLAlchemy on SQLite. Pydantic v2.
[stack/frontend] Frontend: Vite + React + TypeScript + Tailwind + shadcn/ui.
[structure/backend] Backend lives in `backend/`. Routes in `backend/routes/`. Pipeline stages in `backend/pipeline/`.
[db] SQLite via SQLAlchemy. No Alembic yet. Schema in `backend/db/schema.sql`.
[test] Run backend tests with `pytest -m unit`. Do not run `-m api` unless asked.
[style/backend] Never use time.sleep() in async paths; use await asyncio.sleep().
[architecture/backend] Coder never writes to disk. `patch_applier` owns all disk writes.

If a memory entry conflicts with the current source code or the user's
explicit instruction, follow the source code / user instruction and add a
suggested memory update to your handoff under `suggested_memory_entries`.
=== END PROJECT MEMORY ===
```

Notes on the format:
- The header makes the block self-describing. The model knows what it is.
- `[category/scope]` tags are inside the line so they survive line-wise truncation.
- The footer is a single short instruction: source > memory, suggest updates. Not a paragraph. Long disclaimers get ignored.
- No backticks around the whole block. The block contains code spans for paths; wrapping the whole thing in backticks would break that.

### 2.12 Suggestion schema in handoff contracts

The planner/coder/reviewer already return `suggested_memory_entries` (you have this in `planner.py`). In M1, change it from `list[str]` to a structured list:

```python
class SuggestedMemoryEntry(BaseModel):
    content: str = Field(max_length=400)
    category: Category
    scope: Scope = "global"
    rationale: str | None = Field(default=None, max_length=400)
```

A bare `list[str]` is the wrong type — the human approver needs the category and rationale to decide. Forcing this at the schema level means the model has to think about which bucket the suggestion belongs in.

---

## 3. Phase M2 — PostgreSQL, Run Memory, Audit (Sketch — Do Not Build Now)

### 3.1 PostgreSQL move

- Same logical schema. Add `gen_random_uuid()` for IDs.
- Switch `is_stale INTEGER` to `BOOLEAN`, `created_at` to `TIMESTAMPTZ`.
- Move `category` and `scope` from `TEXT` to `CHECK`-constrained columns or enum types.
- Alembic introduced here, not before. The first Alembic migration **must** be `0001_baseline` matching the current SQLite schema exactly, so the SQLite→PG move is a data load, not a schema redesign.

### 3.2 Run / Thread Memory tables

```sql
CREATE TABLE run_memory (
    run_id UUID PRIMARY KEY REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(id),
    feature_description TEXT NOT NULL,
    approved_chunk_plan JSONB,
    files_touched JSONB DEFAULT '[]'::jsonb,
    test_failures   JSONB DEFAULT '[]'::jsonb,
    review_findings JSONB DEFAULT '[]'::jsonb,
    decisions       JSONB DEFAULT '[]'::jsonb,
    pr_url TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
```

Note: `run_memory` is *one row per run*. It is the working scratchpad. The append-heavy JSONB columns are fine at our scale.

### 3.3 Audit trail fields on `memory_facts`

Add (already partly in M1 schema):
- `created_via` — `'human' | 'suggestion'`
- `source_run_id`, `source_chunk_number` — set when promoted from a suggestion
- `source_pr_url` — set when promoted off a merged PR
- `last_edited_by`, `last_edited_at`
- `version` integer, bumped on each edit, with a `memory_fact_history` table holding prior versions

### 3.4 Promotion workflow

A suggestion is promoted to a fact in a single DB transaction:
1. Insert into `memory_facts` with `created_via='suggestion'` and source fields.
2. Update `memory_suggestions.status='approved'`, set `decided_by`, `decided_at`.
3. Insert a row in `memory_fact_history` (v1).

If any step fails, the whole transaction aborts. This avoids the "suggestion marked approved but no fact created" race.

### 3.5 Stale memory handling (better than M1)

Don't age by `created_at`. Instead:

- A fact becomes stale when **the underlying signal changes**, not on a calendar timer.
- Repo indexer (already in Phase 2A) runs a "stack fingerprint" check on each run: detect language, framework, test command, DB, migration tool. Compare against active `stack`, `test`, `db` memory.
- On mismatch, do **not** silently archive. Mark the conflicting fact `is_stale=true`, write a suggestion to update it, and **fail loudly** at the start of the run with a human gate.

This is the right place to fix Finding #3 fully. M1 can use the naive aging as a transitional measure but **must** also expose the "verify" button per fact (already in §2.5) so humans can refute the stale flag without archiving.

### 3.6 Worker queue interaction

When you introduce a worker queue (Celery/RQ/Arq), memory reads and writes become cross-process. Three rules:

1. **Memory writes go through the API, not directly to the DB**, so concurrency is mediated by one writer path with row-level locks (`SELECT ... FOR UPDATE`).
2. **Memory reads at prompt-build time happen at run start and are snapshotted into `run_memory.injected_memory_snapshot` (JSONB).** Re-reading on every chunk re-runs the risk that memory changes mid-run, which breaks reproducibility.
3. **Suggestion writes from workers are append-only.** No worker ever promotes a suggestion. Promotion is human-only via the API. This rule survives the queue.

### 3.7 Race conditions

Concrete cases that bite once workers exist:

- **Two chunks finish near-simultaneously, both write the same suggestion.** Resolved by the unique index on `(project_id, content_hash, status='pending')`. Second insert collapses to "duplicate."
- **Human approves a suggestion while a worker is still using the snapshot.** Fine — the worker uses the snapshot, the next run uses the new fact. This is *desired* behavior.
- **Human archives a fact during a run.** Same: snapshot wins for the run, archive takes effect next run. Show a banner in the UI: "Archived. Will take effect on next run."
- **Resume of an interrupted run.** Reuses `run_memory.injected_memory_snapshot`. Never re-reads from `memory_facts`. (Otherwise resume sees a different prompt than the original run, which is a debugging nightmare.)

---

## 4. Phase M3 — Semantic Memory (Sketch — Do Not Build Now)

### 4.1 pgvector schema

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE semantic_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id),
    content TEXT NOT NULL,
    summary TEXT NOT NULL,                -- 1-2 sentence summary, used in prompts
    embedding vector(1024),
    kind TEXT NOT NULL,                   -- 'decision' | 'rejection' | 'bug' | 'pattern' | 'pr_summary'
    source_run_id UUID,
    source_pr_url TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    use_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active'          -- active | archived
);
CREATE INDEX ON semantic_memory USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON semantic_memory (project_id, status);
```

### 4.2 Embedding model abstraction

Wrap the embedding call:

```python
class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    dimension: int
```

Pin a default (e.g., a 1024-dim model). **Critical rule:** if the embedding model ever changes, every existing vector must be re-embedded. Store the model name on the row (`embedding_model_id TEXT`), and refuse to retrieve from rows with a different `embedding_model_id` than the current default. Mixed-dim or mixed-model retrieval silently degrades quality and is one of the worst kinds of bug to diagnose.

### 4.3 Retrieval

Query embedding built from: chunk title + plan goal + files touched in chunk + immediate previous chunk's review findings.

```
SELECT id, summary, kind, 1 - (embedding <=> :q) AS sim
FROM semantic_memory
WHERE project_id = :pid
  AND status = 'active'
  AND embedding_model_id = :model
ORDER BY embedding <=> :q
LIMIT 20;
```

Then filter:
- Drop rows with `sim < 0.72` (similarity threshold; tune).
- Drop rows whose `kind` is irrelevant to the current role (e.g., reviewer doesn't need `pr_summary`).
- Keep top 5.
- Inject only summaries, not full content, with `[semantic/<kind>]` prefix to make it visually distinct from Project State Memory.

### 4.4 Combining with Project State Memory

Project State Memory (M1) is **always** injected first and at full priority. Semantic memory is injected last, into the remaining budget. Two reasons:

1. Project State Memory represents human-confirmed rules. Semantic memory is similarity-retrieved hint material.
2. If the budget is tight, semantic memory drops first.

### 4.5 Preventing prompt flood

- Hard cap: 5 semantic entries per call, ever.
- Per-`kind` caps: at most 2 rejections, at most 2 bug fixes, at most 1 decision/pattern/pr_summary. Variety beats redundancy.
- If two retrieved entries have cosine sim > 0.95 with each other, dedup.

---

## 5. Failure Modes — Adversarial Matrix

Treat this as the test plan. Each row is something that *will* happen once the system is real.

| # | Failure mode | Mitigation (where) | Detect / fail-stop |
|---|--------------|--------------------|---------------------|
| 1 | Too many memory entries | Token budget §2.7; per-category cap | Budget overflow → drop tail with warning |
| 2 | Memory token budget overflow | Hard 1500-token ceiling | Log if security+forbidden_paths alone exceed |
| 3 | Contradictory memories | Same project + same category, surface conflict in UI; show both in prompt with `[CONFLICT]` tag | Suggestion that conflicts with an active fact opens a "resolve" modal |
| 4 | Stale memories | M1: `is_stale=true` flag; M2: signal-driven (repo index) | Stale facts shown in prompt with `[stale]` tag, not silently dropped |
| 5 | Wrong memory saved by human mistake | Archive (not delete) + reason; verify button | Run-detail Memory tab shows what was injected |
| 6 | AI suggests unsafe memory | Suggestion never auto-promoted; secret regex on insert | UI shows rationale; human reviews |
| 7 | Memory poisoning (malicious file → AI suggestion → memory) | Human approval gate; content validators | Suggestions over 400 chars rejected; suggestions matching secret regex rejected |
| 8 | Duplicate memories | `UNIQUE(project_id, content_hash) WHERE status='active'` | DB rejects; suggestion marked `duplicate` |
| 9 | Memory applies to one folder/module only | `scope` in M1 (`backend`/`frontend`/`tests`/`infra`); folder-level scope in M3 | Scope mismatch → not injected for that chunk |
| 10 | Memory applies only to frontend or backend | Same as #9 | — |
| 11 | Memory applies only to one branch | Not modeled in M1. Document as known limitation. M2 adds `branch_scope` if needed. | — |
| 12 | Stack changes mid-project | M2 repo-index fingerprint check at run start | Run halted; suggestion to update created |
| 13 | Repo structure changes | Same as #12 | — |
| 14 | Test command changes | Same as #12 | — |
| 15 | Migration tool changes | Same as #12 | — |
| 16 | Old memory conflicts with new repo index | M2 conflict UI; M1: manual review via Memory page | Run halt + human gate |
| 17 | Memory retrieved for wrong project | `project_id` required on every query; raise if missing | Caller passing `project_id=None` logs a loud warning |
| 18 | Cross-project memory leakage | Backfill of pre-M1 rows to `archived`; FK + index | Test must assert no cross-project read |
| 19 | Secrets/API keys stored as memory | Secret regex (§5.1) on `add_fact` AND on suggestion insert | Reject with 422; do not log content |
| 20 | PII stored | Same as #19, plus generic PII regex (email, phone, credit card) — best-effort | Same |
| 21 | Prompt injection inside memory content | Block leading control sequences (`SYSTEM:`, `<|...|>`, `---`, `=== `) at insert; static prefix in injected block makes injection visible | Reject on insert; in injection block, content is line-bounded |
| 22 | Malicious repo file poisoning AI suggestion | Human approval gate is the backstop; never auto-approve | UI shows suggestion content + source file; human reads before approve |
| 23 | Model over-trusts memory vs. code | Prompt footer: "source code wins on conflict" | Reviewer prompt requires flagging code/memory conflict |
| 24 | Human approves bad memory | Verify + archive workflow; audit trail | Run-detail Memory tab makes the chain visible |
| 25 | Memory entry too vague | `min_length=4` is a floor only. Document examples of good vs. bad in UI. | Reviewer in UX flow: "Is this useful for a new contributor?" check |
| 26 | Memory entry too large | `max_length=400` char ceiling | DB / Pydantic rejects |
| 27 | Memory entry too specific (overfits one task) | Category `other` and high priority number (low priority) by default; human edits up | Show "used in N runs" counter (M2) to identify dead weight |
| 28 | Multiple models disagree on suggestions | Each suggestion has `suggested_by`; UI groups identical-content suggestions; human picks | Identical content_hash from different models → single suggestion with multiple `suggested_by` tags (M2) |
| 29 | Failed run tries to write memory | Run status check before persisting suggestions; only `completed` or `awaiting_final_approval` may persist | Other statuses → suggestions discarded |
| 30 | Rejected chunk tries to write memory | Per-chunk: only chunks with `status='completed'` produce suggestions | Same |
| 31 | Rollback after memory suggested | Suggestions live in `memory_suggestions` until promoted. Rollback doesn't touch them — they remain `pending`. Human can still approve later. | Acceptable behavior; document it |
| 32 | PR created but memory write fails | Memory write is non-blocking for PR. Log error. Surface in run detail. | PR succeeds; suggestion creation retried best-effort; if dropped, log loudly |
| 33 | Memory write succeeds but PR fails | Suggestion remains `pending`. Human can still approve or reject. | Acceptable |
| 34 | Concurrent runs suggest conflicting memories | Both land as separate suggestions; human resolves | UI groups by content_hash; suggestions form a queue per project |
| 35 | Same run resumes after memory changed | M1: re-reads at run-start only; resume keeps original block. M2: snapshot in `run_memory`. | Resume path uses cached injection (M2); M1 caveat documented |
| 36 | Live logs claim memory written but write failed | Emit `memory.suggestion.created` event *after* DB commit, not before | If commit fails, no event |
| 37 | UI shows stale memory as active | UI must read from DB on every load, not from cache | Add a cache-bust on archive/edit |
| 38 | User deletes/disables memory during run | Show banner: "Takes effect next run." Current run continues with snapshot. | UX only |
| 39 | Imported repo with many conventions | M2 onboarding wizard: scan repo, generate suggestions, human approves in bulk | M1: human seeds memory manually |
| 40 | First-time project, no memory | `load_hard_facts` returns empty string; prompts handle this already | Tested |
| 41 | Greenfield project, inferred memory | Same — human seeds. Don't auto-infer in M1. | — |
| 42 | Memory exceeds useful context window | Hard 1500-token cap; per-role filters | Logged warning when truncation occurs |

### 5.1 Secret regex set (Finding #4 mitigation)

Reject on insert if `content` matches any of:

```python
SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9]{20,}",                              # OpenAI / Anthropic-style
    r"AIza[0-9A-Za-z_\-]{35}",                           # Google / Gemini
    r"ghp_[A-Za-z0-9]{36,}",                             # GitHub personal token
    r"github_pat_[A-Za-z0-9_]{40,}",                     # GitHub fine-grained
    r"xox[baprs]-[A-Za-z0-9-]{10,}",                     # Slack
    r"AKIA[0-9A-Z]{16}",                                 # AWS access key
    r"-----BEGIN (RSA|EC|OPENSSH|PRIVATE) KEY-----",     # PEM keys
    r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.",    # JWT
    r"\b[A-Fa-f0-9]{32,}\b",                             # long hex (heuristic, last)
]
```

The hex one is a heuristic and **will** false-positive on git commit hashes. The fix is contextual: if `content` starts with "Commit " or contains "hash", allow. Otherwise reject. Document this. False positives are fine; false negatives are not.

Plus PII (best-effort):
- Email: `r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"`
- Phone: `r"\b(\+?\d{1,3}[ -]?)?(\(?\d{3}\)?[ -]?)?\d{3}[ -]?\d{4}\b"`
- Credit card: Luhn check on any 13-19 digit run.

Reject these with a 422 and the error message: "Memory entries cannot contain emails, phone numbers, credit card numbers, or secrets." Do **not** echo the offending substring back in the error. Do not log it.

---

## 6. Memory Safety Rules — Non-Negotiable

1. **AI-suggested memory is never auto-promoted to long-term memory.** Always via the suggestion table + human approval.
2. **Secrets, API keys, JWTs, and PEM blocks are never stored.** Insert path validates.
3. **Archived memory is never injected.** Period.
4. **The 1500-token budget is a hard cap.** Truncation logs a warning.
5. **Semantic memory never overrides current source code.** Prompt footer enforces this textually; reviewer prompt enforces this behaviorally.
6. **Every memory query is scoped by `project_id`.** No exceptions. `project_id=None` is an error, not a fallback.
7. **Long-term writes require human confirmation.** Suggestions are not memory.
8. **No hard delete in M1.** Archive only. Audit trail is the product.
9. **Every memory row records source (`source`, `added_by`) and approver (`approved_by`).**
10. **Memory is advisory, never absolute truth.** The footer says so. The reviewer is told so. The model is reminded so.

---

## 7. Prompt Injection Strategy (Format and Discipline)

The exact block is shown in §2.11. The rules around it:

### 7.1 Structural separation

- The block is wrapped by clear sentinels: `=== PROJECT MEMORY ... ===` and `=== END PROJECT MEMORY ===`. The model has been told these sentinels mean "this is memory, not instructions."
- The block sits **between** the system prompt and the user prompt:
  ```
  <system>
  You are a senior engineering planner. ...
  </system>
  <memory_block />
  <user>
  FEATURE REQUEST: ...
  </user>
  ```

### 7.2 Stale tagging

A `[stale]` token in front of the `[category/scope]` tag signals possibly outdated. The model is instructed to treat stale entries as soft hints only, and to add a `suggested_memory_entries` update if it can confirm or refute the staleness from code.

### 7.3 Source > memory enforcement

Single line at the bottom of the block: *"If a memory entry conflicts with the current source code or the user's explicit instruction, follow the source code / user instruction and add a suggested memory update to your handoff under `suggested_memory_entries`."*

Don't make it a paragraph. Long disclaimers tune out.

### 7.4 Category and scope visibility

`[category/scope]` prefix per line. This is what lets the model down-weight irrelevant entries on its own.

### 7.5 Compactness

- One fact per line.
- Newline between sections, not blank lines.
- No bullets, no numbering — adds tokens for no information.
- 9–12 lines is the sweet spot. 30+ lines is a signal that the project has overcollected memory; the cap is the forcing function.

---

## 8. M1 Retrieval Strategy (No Vector DB)

For M1, retrieval is rule-based selection from `memory_facts WHERE project_id = ? AND status = 'active'`.

**Algorithm:**

1. Fetch all active facts for `project_id`.
2. Sort by:
   1. `category` rank: `security` and `forbidden_paths` first, then `stack`, `db`, `test`, then `architecture`, `structure`, `style`, `deploy`, then `reviewer_pref`, then `other`.
   2. Scope match for the current role (boost matching scope).
   3. `priority` ascending.
   4. `last_verified_at` desc (recently verified wins ties).
   5. `is_stale` ascending (non-stale first).
3. Per-role filter (§2.7).
4. Greedy fill up to 1500 tokens using the `tiktoken`-equivalent estimate from the existing token utils.
5. Render with the format in §2.11.

**Reviewer-specific:** boost any `reviewer_pref` to priority 0. Otherwise same algorithm.
**Triage-specific:** clamp to `category in {stack, structure, test}` and a 300-token budget. Triage is just sizing.

**Repo/task keyword matching in M1:** keep it simple. If the chunk's `files_to_modify` includes paths matching `^backend/`, boost `scope='backend'` facts; if `^frontend/`, boost `scope='frontend'`. No fuzzy matching; no NLP; M3 handles that with embeddings.

---

## 9. Memory Suggestion Strategy

### 9.1 When to suggest

| Stage | Suggest? | Rationale |
|-------|----------|-----------|
| After planner | Yes (max 2) | Sees architecture-level facts |
| After architect | Yes (max 2) | Architectural decisions |
| After coder | No | Coder is too close to local detail; high-noise |
| After reviewer | Yes (max 3) | Best signal — reviewer is adversarial |
| After tests pass on a chunk | Implicitly via above | — |
| After failed run | **No** | Failures aren't lessons until human inspects |
| After rejected chunk | **No** | Same |
| After successful PR creation | Yes (max 1) — only the planner/reviewer is asked, "If you had to leave one fact for the next contributor, what would it be?" | Single most valuable signal |

Hard cap per run: 8 pending suggestions. More than that is noise.

### 9.2 Suggestion schema

See §2.12.

### 9.3 What must never become a suggestion

- Anything containing a secret (validator rejects).
- Anything specific to the current run (`run_id`, chunk number, PR number).
- File paths the system saw once.
- Test output. Diffs. Stack traces.
- Anything over 400 chars (validator rejects).
- Anything where `category='other'` and `scope='global'` simultaneously. Almost certainly junk. (Validator warning, not reject.)

### 9.4 Human approval UX

For each pending suggestion the UI shows: content, category, scope, rationale, who suggested it, what run, what chunk. Three buttons: **Approve**, **Edit & Approve**, **Reject (reason required)**.

"Edit & Approve" is the most-used button. Models will phrase things sloppily; humans clean and approve.

---

## 10. Database / API / UI Design for M1 (Concrete)

Schemas and Pydantic shown in §2. This section covers request/response shapes and tests.

### 10.1 Example API request/response

**Create a fact:**

```http
POST /projects/proj-13605886/memory
Content-Type: application/json

{
  "content": "Backend tests run with `pytest -m unit`.",
  "category": "test",
  "scope": "tests",
  "priority": 50,
  "source": "human",
  "added_by": "founder"
}
```

```http
201 Created

{
  "id": "mem-9c1f...",
  "project_id": "proj-13605886",
  "content": "Backend tests run with `pytest -m unit`.",
  "category": "test",
  "scope": "tests",
  "priority": 50,
  "status": "active",
  "is_stale": false,
  "source": "human",
  "added_by": "founder",
  "approved_by": "founder",
  "approved_at": "2026-05-26T10:14:22Z",
  "content_hash": "5fa1...",
  "created_at": "2026-05-26T10:14:22Z"
}
```

**Approve a suggestion:**

```http
POST /projects/proj-13605886/memory/suggestions/sug-44b/approve
Content-Type: application/json

{
  "decided_by": "founder",
  "content": "Backend tests run with `pytest -m unit`.",  // optional edit-on-approve
  "category": "test",
  "scope": "tests",
  "priority": 50
}
```

**Rejection requires a reason:**

```http
POST /projects/proj-13605886/memory/suggestions/sug-44b/reject

{ "decided_by": "founder", "rejection_reason": "Too specific to one PR" }
```

### 10.2 Validation rules

- `content`: 4–400 chars after strip; no secret regex match; no leading `SYSTEM:`/`---`/`=== `/`<|`.
- `category`: must be in closed enum.
- `scope`: must be in closed enum.
- `priority`: 0–1000.
- `source`: free text, ≤100 chars.
- `archived_reason`: required on archive endpoint; ≥4 chars.
- `rejection_reason`: required on suggestion reject; ≥4 chars.

### 10.3 Tests (M1)

Add to `backend/tests/test_memory.py`:

1. `test_load_hard_facts_requires_project_id` — calling with `None` returns empty and logs warning.
2. `test_load_hard_facts_scopes_to_project` — facts in project A are not returned for project B.
3. `test_add_fact_rejects_secret_openai_style`.
4. `test_add_fact_rejects_secret_gemini_style`.
5. `test_add_fact_rejects_pem_block`.
6. `test_add_fact_rejects_pii_email`.
7. `test_add_fact_rejects_overlong_content`.
8. `test_add_fact_dedup_by_content_hash` — two active facts with same content collapse to one.
9. `test_archive_requires_reason`.
10. `test_archived_fact_not_injected`.
11. `test_stale_fact_still_injected_with_tag`.
12. `test_token_budget_truncation_keeps_security_first`.
13. `test_suggestion_promotion_atomic` — DB transaction.
14. `test_failed_run_does_not_persist_suggestions`.
15. `test_rejected_chunk_does_not_persist_suggestions`.
16. `test_per_role_filter_triage_only_stack_structure_test`.
17. `test_per_role_filter_reviewer_pref_boosted`.
18. **Smoke:** existing chunked-run smoke test (1-chunk and 2-chunk from `docs/phase2b-smoke-tests.md`) must pass unchanged with M1 wired in.

---

## 11. Integration with Current Pipewright Flow

Where each pipeline stage loads or writes memory in M1:

| Stage | Memory action | Code location |
|-------|---------------|---------------|
| Chunk plan creation (triage) | Read: triage-filtered memory (300 token budget) | `backend/pipeline/triage.py` (or wherever triage runs) |
| Chunk plan approval (human) | Read-only (audit display) | `backend/routes/runs_chunks.py` |
| Per-chunk planner | Read: planner-filtered memory | `backend/pipeline/planner.py` (already calls `load_hard_facts`) |
| Per-chunk architect (if used) | Read: architect-filtered memory | `backend/pipeline/architect.py` |
| Per-chunk coder | Read: coder-filtered memory, scope-boosted by chunk's file paths | `backend/pipeline/coder.py` (already calls `load_hard_facts`) |
| Per-chunk tests | None | — |
| Per-chunk reviewer | Read: reviewer-filtered memory with `reviewer_pref` boost | `backend/pipeline/reviewer.py` |
| High-risk per-chunk approval | Read: top-5 security + forbidden_paths only, in the approval summary shown to human | `backend/pipeline/chunked_orchestrator.py:_chunk_approval_summary` |
| Final review | Read: same as reviewer | reviewer + final approval gate |
| Final approval gate | Read-only (audit display) | `backend/routes/runs.py` |
| Push + PR creation | None | — |
| **After successful PR** | Write: persist `suggested_memory_entries` from planner / architect / reviewer handoffs into `memory_suggestions` (status='pending') | `backend/pipeline/chunked_orchestrator.py` post-PR hook |
| Run resume/recovery | **M1 limitation:** re-loads memory at resume time. Document this. Memory may differ from original run. | resume path in `chunked_orchestrator.py` |

**The post-PR hook is the only write site for AI suggestions.** Centralizing it makes the "failed runs don't write" rule trivially enforceable (Failure Mode #29).

---

## 12. What NOT to Build Now — Strict List

The following are **out of scope for M1**. Do not start any of them, even partially, even "just the scaffolding."

- pgvector and any vector storage
- Embeddings of any kind
- PostgreSQL migration
- Alembic
- Celery / RQ / Arq / any worker queue
- Redis
- Slack / email approval flows
- Automatic (non-human-approved) writes to long-term memory
- Cross-project memory or shared organization memory
- Complex memory ranking (BM25, learned-to-rank, etc.)
- Auto-routing models based on memory contents
- Branch-scoped memory
- Folder-scoped memory beyond the 5 scope values in §2.3
- Run / Thread Memory tables (those are M2)
- Memory versioning / `memory_fact_history` table (M2)
- Stack-fingerprint conflict detection (M2)
- Onboarding wizard for imported repos (M2)

If a feature seems borderline, default to "out." M1's only job is: scope memory per project, inject it correctly, let humans approve AI suggestions, never store secrets.

---

## 13. Deliverables Breakdown

| Milestone | Deliverable | Notes |
|-----------|-------------|-------|
| **M0 (done after this doc)** | This design document committed to `docs/memory-architecture.md`. | No code. |
| **M1.0 — Migration & fix** | `0001_memory_lite.sql` migration. Backfill of pre-M1 rows to archived. Rewrite of `backend/memory/memory_store.py` with `project_id` required. | Touches `db/schema.sql`, `memory/memory_store.py`. Breaking change to function signatures. |
| **M1.1 — Injection wired** | `load_hard_facts(project_id)` flowing through planner, coder, reviewer (when added), with token budget, ordering, and per-role filters. Audit-block format from §2.11. | Touches `pipeline/planner.py`, `pipeline/coder.py`, `pipeline/reviewer.py`, `projects/runtime.py`. |
| **M1.2 — Management API + UI** | All endpoints from §2.5. Memory page (list/create/edit/archive/verify). | Touches `backend/routes/`, `frontend/src/pages/Memory.tsx`. |
| **M1.3 — Suggestions** | `memory_suggestions` table. Post-PR hook writes pending suggestions from handoffs. Suggestion inbox UI. Approve / Edit & Approve / Reject. | Touches `pipeline/chunked_orchestrator.py`, `routes/memory_suggestions.py`, `frontend/src/pages/Suggestions.tsx`. |
| **M1.4 — Run-detail Memory tab** | Read-only audit display of the exact memory block injected for the run. Requires snapshotting at run start. | Stores the snapshot as JSON in a new column on `pipeline_runs` (`injected_memory_snapshot`). Cheap. |
| **M2 (deferred)** | PostgreSQL move + Alembic baseline. Run Memory tables. Audit trail v2. Stack fingerprint. | — |
| **M3 (deferred)** | pgvector. Semantic memory. Embedding abstraction. Retrieval. | — |

---

## 14. Acceptance Criteria for M1

M1 is shippable when **all** of the following are true. Each is a test, a smoke step, or a manual check.

1. `memory_facts` has `project_id`, `category`, `scope`, `priority`, `content_hash`, `approved_by`, `approved_at`, `last_verified_at` columns.
2. All pre-M1 rows are archived; none are injected.
3. `load_hard_facts(project_id=None)` returns `""` and logs a warning. (Test #1 in §10.3.)
4. Memory created in project A is **not** visible to project B in any code path. (Test #2.)
5. Secret regex rejects: OpenAI/Anthropic keys, Gemini keys, GitHub tokens, PEM blocks, JWTs. (Tests #3–#6.)
6. Memory entries over 400 chars are rejected. (Test #7.)
7. Duplicate active facts (same `content_hash`, same `project_id`) are impossible. (Test #8.)
8. Archive requires `reason`; archived facts are not injected. (Tests #9, #10.)
9. Stale facts are injected with a `[stale]` tag and never silently dropped. (Test #11.)
10. The injected memory block never exceeds 1500 tokens; if it would, `security` and `forbidden_paths` are preserved first. (Test #12.)
11. AI suggestions are never auto-promoted. Promotion is one transaction. (Test #13.)
12. Failed runs and rejected chunks do not create suggestions. (Tests #14, #15.)
13. Per-role filters: triage gets only `stack`/`structure`/`test`; reviewer gets `reviewer_pref` boosted. (Tests #16, #17.)
14. The existing 1-chunk and 2-chunk smoke tests in `docs/phase2b-smoke-tests.md` pass unchanged. (Test #18.)
15. On a fresh project with 8–10 seeded memory entries, the planner and coder no longer ask basic stack/test/structure questions in their output for a small smoke feature. **Manual** check — document the before/after.
16. Frontend Memory page can list, create, edit, archive, and verify. Suggestion inbox can approve / edit-approve / reject.
17. Run detail page has a Memory tab showing the exact injected block for that run.
18. No PostgreSQL, no Alembic, no pgvector, no worker queue, no Redis, no embedding library introduced.

---

## 15. Adversarial Closing Notes

A few things that will go wrong even if M1 is built exactly as designed:

- **The 1500-token cap will be hit faster than expected**, because well-meaning humans will add 20-line memory entries describing entire conventions. The 400-char cap helps, but expect to tighten or split entries within the first few weeks. Build the "Edit & Approve" UI for this reason, not for cosmetics.
- **`source > memory` will be ignored by models under pressure.** The footer line helps but is not magic. Watch for cases where the reviewer "approves" code that violates memory because memory wasn't visible enough in the prompt. The fix is to also have the reviewer system prompt say "Cross-check the diff against PROJECT MEMORY; flag any conflict explicitly." Bake this into the reviewer prompt template.
- **Resume runs will diverge from original runs** because M1 re-reads memory on resume. This is acceptable for a single-instance system but will cause real confusion during debugging. Document it in the resume code. M2 fixes it with the run snapshot.
- **The first time a memory entry "becomes wrong"** (because the repo changed), no part of M1 will detect it automatically. The signal will be the model producing weird output. The Memory tab on Run detail is your only debugging tool. Make sure it is good.
- **Cross-project leakage will not return** as long as nobody writes a new query that omits `project_id`. The defense is the warning log in `load_hard_facts(None)` and the test in §10.3. Both are necessary; neither is sufficient on its own.

If any of the above feels uncomfortable to ship, the answer is not to expand M1 — it is to ship M1 fast, see which adversarial case actually bites, and let that drive M2 priorities.

---

## Recommended M1 Implementation Order

Codex should implement in this order, with a working commit at each step:

1. **Migration `0001_memory_lite.sql`** — add columns, indexes, `memory_suggestions` table. Backfill pre-M1 rows to `archived`.
2. **Rewrite `backend/memory/memory_store.py`** — new signatures (`project_id` required), secret/PII validators, content hash + dedup, archive/verify functions, `is_stale` tag preserved in output.
3. **Pydantic models** — `MemoryFact`, `MemoryFactCreate`, `MemorySuggestion`, `MemorySuggestionCreate`, `SuggestedMemoryEntry`, enum types.
4. **Unit tests** — all tests in §10.3 up to #15. Run before any wiring.
5. **Wire `project_id` through pipeline** — `planner.py`, `coder.py`, `reviewer.py`, `triage`; thread from `active_project` runtime.
6. **Prompt block builder** — `backend/memory/prompt_builder.py` implementing §8 algorithm and §2.11 format. Unit-tested in isolation.
7. **Run-start snapshot** — add `injected_memory_snapshot` JSON column on `pipeline_runs`; orchestrator writes it once at the start of every run.
8. **Memory API routes** — endpoints in §2.5.
9. **Memory page (frontend)** — list / create / edit / archive / verify.
10. **Post-PR suggestion hook** — orchestrator writes pending suggestions from handoff `suggested_memory_entries` after a successful PR. Gate on run status.
11. **Suggestion API routes** — list / approve / edit-approve / reject.
12. **Suggestion inbox (frontend)**.
13. **Run-detail Memory tab (frontend)** — display the `injected_memory_snapshot`.
14. **Smoke run** — full 1-chunk and 2-chunk smoke tests from `docs/phase2b-smoke-tests.md`. Then a manual feature on a freshly-seeded project, confirming the planner and coder stop asking basic facts.
15. **Tag `phase-m1-memory`.** Do not merge to main until all acceptance criteria in §14 pass.

Steps 1–4 are non-negotiable prerequisites; steps 5–13 can be re-ordered if convenient but each must remain individually testable.