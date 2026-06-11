# Pipewright Architecture Review
**Date:** 2026-06-10  
**Scope:** Pipeline execution engine + Memory system  
**Purpose:** Deep architectural analysis — problems, root causes, and recommended improvements for Phase 3 work.

---

## How to Use This Document

This review is written to be given directly to Claude Fable 5 (via Claude Code) as the design brief for improvement PRs. Each section ends with a **"What Fable 5 Should Build"** block that is a self-contained implementation brief.

---

## Part 1 — Pipeline Architecture

### Current Flow (Happy Path)

```
User request
  → POST /runs/chunked
  → triage.py        [LLM] → TriageResult (chunk plan + files_expected)
  → approval_gate    [HUMAN waits] → approved
  → chunked_orchestrator.py
      for each chunk:
        → planner.py   [LLM] → PlannerHandoff
        → coder.py     [LLM] → CoderHandoff
        → scope_guard  [assert files in scope]
        → patch_applier [apply to disk]
        → test runner  [run test command]
        → reviewer.py  [LLM, advisory]
        → commit       [git]
        → if requires_human_review → pause for approval
  → final approval gate [HUMAN]
  → PR creation (optional)
```

---

### Problem P1: `_execute_single_chunk` is a 140-line monolith

**File:** `pipeline/chunked_orchestrator.py`

**What it does (all in one function):**
dependency resolution → dirty-tree check → planner LLM call → coder LLM call → scope guard → patch apply → test run → reviewer LLM call → commit → approval pause

**Why this is bad:**
- Adding any new stage (e.g. a linter, a security scan) requires editing this one function.
- Failure modes are handled with nested `if/else` and early returns — hard to trace which path was taken.
- Retry logic (for scope expansion, for patch failures) re-enters the same function via `_execute_retry_attempt`, creating two barely-different code paths.
- Tests for individual stages cannot be isolated cleanly.

**What Fable 5 Should Build — PR: Stage Machine Refactor**

Decompose `_execute_single_chunk` into a proper stage machine:

```python
class ChunkStage(StrEnum):
    PLAN = "plan"
    CODE = "code"
    SCOPE_CHECK = "scope_check"
    APPLY = "apply"
    TEST = "test"
    REVIEW = "review"
    COMMIT = "commit"
    AWAIT_APPROVAL = "await_approval"

@dataclass
class StageContext:
    run_id: str
    project_id: str
    chunk: ChunkDefinition
    plan: PlannerHandoff | None = None
    code: CoderHandoff | None = None
    patch_result: PatchResult | None = None
    test_result: TestRunResult | None = None
    review: ChunkReviewRecord | None = None

async def _execute_stage(stage: ChunkStage, ctx: StageContext) -> StageContext:
    """Each stage is a pure function: takes context, returns enriched context."""
    ...

STAGE_SEQUENCE = [
    ChunkStage.PLAN,
    ChunkStage.CODE,
    ChunkStage.SCOPE_CHECK,
    ChunkStage.APPLY,
    ChunkStage.TEST,
    ChunkStage.REVIEW,
    ChunkStage.COMMIT,
]
```

Benefits: each stage is independently testable, resumable from any stage boundary, and a new stage is just a new enum value + handler.

---

### Problem P2: No Saga Pattern — Git and DB Can Diverge

**File:** `pipeline/chunked_orchestrator.py`, `_commit_and_complete_chunk`

**The race:**
1. `git commit` succeeds → commit is on the branch.
2. DB `UPDATE chunks SET status='completed'` fails (disk I/O, lock timeout, etc.).
3. Run resumes → `_reset_stale_running_chunks` sets chunk back to `pending`.
4. Chunk re-executes → second `git commit` of the same change.

**Result:** duplicate commits on the branch. The no-empty-commit guard catches the case where the patch produces no diff, but if the file was modified between the two runs, a second real commit is made.

**What Fable 5 Should Build — PR: Outbox/Saga for Git+DB Atomicity**

Before each `git commit`, write a pending record to a `commit_outbox` table:

```sql
CREATE TABLE commit_outbox (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    chunk_number INTEGER NOT NULL,
    branch TEXT NOT NULL,
    commit_sha TEXT,           -- NULL until committed
    db_written INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
```

Sequence:
1. `INSERT INTO commit_outbox (id, run_id, chunk_number, branch)` — pending
2. `git commit` → get SHA
3. `UPDATE commit_outbox SET commit_sha=?` — recorded
4. `UPDATE chunks SET status='completed'` — DB write
5. `UPDATE commit_outbox SET db_written=1` — finalized

On resume: `SELECT * FROM commit_outbox WHERE db_written=0` → for each row, check git log for the SHA. If it exists, skip re-commit and just update DB. If it doesn't exist, re-apply.

---

### Problem P3: Triage File Hallucination Propagates Too Far

**File:** `pipeline/triage.py`

**The problem:**
Triage returns `files_expected: list[str]` from an LLM call. These strings are the approved scope for the entire run. If the LLM invents a file path that looks plausible (`backend/models/user.py` when the file is `backend/models/users.py`), the error is only caught at `scope_guard` or `apply_patch_guarded` — **after** planner and coder have both made LLM calls and consumed tokens.

**What Fable 5 Should Build — PR: Triage File Grounding Validation**

After triage parses its `TriageResult`, validate every path in `files_expected` against the repo index:

```python
def validate_triage_files(
    files_expected: list[str], repo_path: Path, indexed_files: list[str]
) -> tuple[list[str], list[str]]:
    """
    Returns (valid_paths, unknown_paths).
    Unknown paths are files not present in the repo index.
    """
    indexed_set = {normalize_relative_path(f) for f in indexed_files}
    valid, unknown = [], []
    for path in files_expected:
        norm = normalize_relative_path(path)
        if norm in indexed_set:
            valid.append(norm)
        else:
            unknown.append(path)
    return valid, unknown
```

If `unknown_paths` is non-empty:
- Option A (safe): fail triage with a message listing the unknown paths and suggesting corrections.
- Option B (better UX): include unknown paths in a correction prompt with the actual indexed file list and let the LLM self-correct in attempt 2.

Option B is the right approach. The correction prompt should include the top 20 closest matches from the index (Levenshtein or simple prefix match) to guide the LLM.

---

### Problem P4: Planner Is Unaware of `files_expected` Scope

**File:** `pipeline/planner.py`

**The problem:**
The planner receives the `feature_description` and memory block but does NOT receive the `files_expected` set from triage. It can freely plan changes to files outside the approved scope. The divergence surfaces at `scope_guard` — after the full coder LLM call.

**What Fable 5 Should Build — PR: Inject Scope Contract into Planner Prompt**

Pass `files_expected` from the orchestrator into `run_planner` and include it in the planner system prompt:

```
APPROVED SCOPE (do not plan changes to any other files):
  - backend/routes/users.py
  - backend/models/user.py
  - backend/tests/test_users.py

If you believe additional files are needed, list them in `out_of_scope_requests` 
with justification. Do NOT include them in `files_to_modify`.
```

Add `out_of_scope_requests: list[dict]` to `PlannerHandoff` so the orchestrator can surface scope expansion requests proactively (before the coder stage) instead of reactively (after apply fails).

---

### Problem P5: `old_string` Validity Only Checked at Apply Time

**File:** `pipeline/coder.py`, `pipeline/patch_applier.py`

**The problem:**
When the LLM generates an `action="edit"` with `old_string="..."`, the `old_string` is not checked against the actual file until `apply_patch`. If `old_string` doesn't exist in the file, the apply fails with `STALE_INDEX_OR_FILE_CHANGED` after an entire LLM round-trip (planner + coder) was consumed.

**What Fable 5 Should Build — PR: Pre-Apply `old_string` Validation**

After parsing the `CoderHandoff` and before leaving the coder stage, validate every `action="edit"` change:

```python
def validate_edit_strings(
    changes: list[FileChange], repo_path: Path
) -> list[str]:
    """
    Returns list of validation errors. Empty = all good.
    Called after coder parse, before scope_guard.
    """
    errors = []
    for change in changes:
        if change.action != "edit":
            continue
        target = repo_path / change.path
        if not target.exists():
            errors.append(f"{change.path}: file does not exist for edit action")
            continue
        content = target.read_text(encoding="utf-8", errors="replace")
        count = content.count(change.old_string)
        if count == 0:
            errors.append(f"{change.path}: old_string not found in file")
        elif count > 1:
            errors.append(f"{change.path}: old_string appears {count} times (must be exactly 1)")
    return errors
```

If errors are found, trigger a correction prompt to the coder with the actual file content sections around where the edit was expected, rather than failing the entire run.

---

### Problem P6: Rate Limit Handling is Naive

**Files:** `pipeline/planner.py`, `pipeline/coder.py`

**The problem:**
On `429 ProviderRateLimitError`, the code sleeps `asyncio.sleep(60)` and retries once. This:
- Ignores the `Retry-After` header if the provider sends one.
- Has no exponential backoff — a second 429 fails permanently.
- The 60-second value is arbitrary and provider-specific.
- Sleeps the asyncio event loop, blocking other concurrent runs.

**What Fable 5 Should Build — PR: Centralized Retry with Backoff in `BaseLLMProvider`**

```python
async def complete_with_retry(
    self,
    request: LLMRequest,
    max_attempts: int = 3,
    base_delay: float = 10.0,
    max_delay: float = 120.0,
) -> LLMResponse:
    for attempt in range(max_attempts):
        try:
            return await self.complete(request)
        except ProviderRateLimitError as e:
            if attempt == max_attempts - 1:
                raise
            retry_after = getattr(e, "retry_after_seconds", None)
            delay = retry_after or min(base_delay * (2 ** attempt), max_delay)
            delay += random.uniform(0, delay * 0.1)  # jitter
            await asyncio.sleep(delay)
```

Parse `Retry-After` from provider error responses and store it in `ProviderRateLimitError.retry_after_seconds`.

---

### Problem P7: Drift Check Only on Fresh Execution, Not Resume

**File:** `pipeline/chunked_orchestrator.py`, `_verify_start_context_for_fresh_execution`

**The problem:**
`_verify_start_context_for_fresh_execution` checks that repo branch/SHA haven't changed since the plan was created. This runs only during fresh execution. When `resume_chunked_pipeline` is called (e.g. after a server restart), the drift check is skipped entirely. A resumed run can apply patches on top of a repo that has moved forward.

**What Fable 5 Should Build:**
Call a lighter version of the drift check at the start of `_resume_chunked_pipeline_locked` that verifies the current branch HEAD SHA matches the branch HEAD recorded when the last chunk was committed. If HEAD has moved (someone pushed or merged), require explicit re-approval.

---

### Problem P8: Reviewer Diff Cap Cuts the Wrong End

**File:** `pipeline/reviewer.py`

**The problem:**
`REVIEWER_MAX_DIFF_CHARS = 6000`. The diff is truncated from the tail — the reviewer sees the beginning of the diff. For typical patches adding new code (appended functions, new test cases), the most security-sensitive additions are at the **end** of the diff, not the beginning.

**What Fable 5 Should Build:**
Change truncation to keep the **tail** of the diff, not the head. Or better: keep a balanced sample — first 2000 chars + last 2000 chars with a `[... N lines omitted ...]` marker in the middle. This ensures both the removal context and the new-code additions are always visible to the reviewer.

---

## Part 2 — Memory Architecture

### Current Memory Flow

```
Project created
  → bootstrap.py       scan repo files → CandidateSuggestion[]
  → human approves suggestions
  → memory_store.py    active facts stored in SQLite

Each LLM role call:
  → prompt_builder.py  load_active_rows → token-budget greedy select → formatted block
  → inject into LLM prompt
  → capture_memory_injection (best-effort provenance write)

After run completes:
  → run_outcome_suggestions.py  generate new suggestions from run artifacts
  → human approves → new facts stored

Periodic:
  → injection_analysis.py       detect duplicates, contradictions, reality mismatches
```

---

### Problem M1: Migration Code on the Hot Read Path

**File:** `memory/memory_store.py`, `_archive_unscoped_pre_m1_memory`

**The problem:**
`_archive_unscoped_pre_m1_memory()` is called on `load_hard_facts`, `add_fact`, and `list_facts`. It runs a `WHERE project_id IS NULL` scan on the entire `memory_facts` table to archive old pre-migration rows. This was a one-time migration that was never extracted from the read/write path. On any deployment that has already been migrated, this is a guaranteed no-op table scan on every memory access.

**What Fable 5 Should Build:**
Add a `schema_migrations` table (or a single boolean flag in a `metadata` table). Run `_archive_unscoped_pre_m1_memory` exactly once at startup (inside `startup_recovery.py`), record the migration as complete, and remove the call from all read/write paths.

---

### Problem M2: Token Budget Greedy Selection Can Drop High-Value Facts

**File:** `memory/prompt_builder.py`

**The problem:**
The greedy selector iterates facts in `(category_rank, scope_rank, priority, created_at)` order and skips any fact that would exceed the token budget. A single large fact (up to 400 chars, the write-path limit) early in the list can fill most of the budget and silently exclude facts that follow.

Security and forbidden-path facts have `CATEGORY_ORDER` 0 and 1 (lowest = highest priority), so they sort first — they're protected. But `stack` (3), `structure` (4), and `test` (5) facts could be crowded out by a verbose security fact.

**What Fable 5 Should Build — PR: Category Budget Allocation**

Reserve a token budget share per category:

```python
CATEGORY_BUDGETS = {
    "security":       0.25,  # always gets at least 25% of budget
    "forbidden_paths": 0.10,
    "stack":          0.20,
    "structure":      0.20,
    "test":           0.15,
    "db":             0.10,
}
```

Allocate per-category before running the greedy selection. Any category that doesn't fill its allocation donates the remainder to a shared overflow pool.

---

### Problem M3: Memory Injection Provenance is Fire-and-Forget

**File:** `memory/memory_store.py`, `pipeline/chunked_orchestrator.py`

**The problem:**
`capture_memory_injection` is called with `asyncio.create_task` and the error is swallowed. If it fails (DB error, serialization issue), the fact that memory was injected into that prompt has no audit record. `injection_analysis.py`'s output is then incomplete because it operates on the injection event stream.

**What Fable 5 Should Build:**
Change `capture_memory_injection` to be awaited (not fire-and-forget) inside the pipeline stages. If it fails, log a warning but do NOT fail the pipeline stage — instead write a tombstone record to a `failed_injections` table so at least the failure is auditable. This preserves the "best-effort" principle while giving observability.

---

### Problem M4: Reviewer and Summary Roles Have Memory But Don't Use It

**File:** `memory/prompt_builder.py`

**The problem:**
`ROLE_TOKEN_BUDGETS` and `ROLE_CATEGORIES` define memory injection policies for `reviewer` and `summary` roles, but the comment in the module explicitly notes these are "not yet wired into reviewer/summary execution." The reviewer (`reviewer.py`) builds its prompt without injecting any memory block.

This means the reviewer doesn't know about:
- Known forbidden patterns for this project
- Known risky files (`forbidden_paths` category)
- Past patch failure patterns (`stack` category)

**What Fable 5 Should Build:**
Wire `build_project_memory_block_detailed(role="reviewer")` into the reviewer prompt build function. The reviewer prompt already has `REVIEWER_MAX_DIFF_CHARS` compression — the memory block adds at most 800 tokens (the defined budget), which is a reasonable addition given the value of security and forbidden-path context during code review.

---

### Problem M5: LLM-Generated `suggested_memory_entries` Are Trusted Without Quality Scoring

**File:** `memory/run_outcome_suggestions.py`, `_handoff_candidates`

**The problem:**
The coder and planner LLMs can suggest memory facts via `suggested_memory_entries` in their handoff output. These pass through `validate_memory_content` (secret/path/code-block filtering) and content-hash dedup, but there is no quality signal. A hallucinated or low-quality fact ("This codebase uses Python") that passes validation becomes a pending suggestion a human must review.

Over many runs, the suggestion queue fills with low-signal LLM-generated noise, increasing human review burden.

**What Fable 5 Should Build — PR: LLM-Suggestion Quality Filter**

Before inserting LLM-generated suggestions, apply a heuristic quality score:

```python
def _score_suggestion(content: str, existing_facts: list[str]) -> float:
    """
    Returns 0.0–1.0. Low scores are filtered before insertion.
    Heuristics:
    - Too short or too generic → low score
    - Overlaps heavily with existing active facts → low score (near-duplicate)
    - Contains specific file paths, version numbers, or test names → high score
    - Vague language patterns ("this codebase uses", "the project has") → low score
    """
```

Facts below a threshold (e.g. 0.3) are discarded, not inserted as pending suggestions. Log a count of discarded low-quality suggestions per run for observability.

---

### Problem M6: No Memory Staleness TTL

**File:** `memory/memory_store.py`

**The problem:**
Facts can be `active` indefinitely. A fact like `"Backend uses Flask."` stays active even after the codebase migrated to FastAPI — unless a human explicitly runs `mark_fact_stale` or `injection_analysis` surfaces a reality mismatch.

For a codebase that evolves over many Pipewright runs, the memory accumulates contradictions silently. The `memory_trust` and `injection_analysis` modules detect some of these, but only if they're run and reviewed.

**What Fable 5 Should Build:**
Add a `staleness_policy` field to `memory_facts`:
- `permanent` — never auto-stale (for security and forbidden-path facts)
- `run_bounded` (default) — stale after `N` runs without a confirming re-observation
- `ttl_days` — stale after a fixed number of days

Implement a background job (run at server startup or weekly) that marks TTL-expired facts as stale and creates a review notification.

---

### Problem M7: Bootstrap Detection is an `if/elif` Chain — Not Extensible

**File:** `memory/bootstrap.py`

**The problem:**
Framework/stack detection is a series of hardcoded substring checks: `if "fastapi" in content`, `if "django" in content`, etc. Adding a new framework means editing this function. The self-detection rule for Pipewright's own directory structure is hardcoded inside a general-purpose module (`if (root / "backend" / "pipeline" / "patch_applier.py").is_file()`), coupling bootstrap to the application.

**What Fable 5 Should Build — PR: Detection Rules as Data**

```python
@dataclass
class DetectionRule:
    file_pattern: str          # glob pattern, e.g. "requirements*.txt"
    content_pattern: str       # substring to find in file
    suggestion_template: str   # e.g. "Backend uses {match}."
    category: str
    scope: str
    priority: int

DETECTION_RULES: list[DetectionRule] = [
    DetectionRule("requirements*.txt", "fastapi", "Backend uses FastAPI.", "stack", "backend", 8),
    DetectionRule("requirements*.txt", "django",  "Backend uses Django.",  "stack", "backend", 8),
    DetectionRule("package.json",      "react",   "Frontend uses React.",  "stack", "frontend", 8),
    # ... etc.
]
```

The self-detection rule becomes just another entry in the list, and new frameworks require no code changes.

---

## Part 3 — Summary: Priority Order for Fable 5

### Critical (breaks correctness or safety)

| # | Problem | File | Risk |
|---|---------|------|------|
| P2 | No saga — git+DB divergence → double commit | chunked_orchestrator | HIGH |
| P3 | Triage file hallucination propagates too far | triage.py | HIGH |
| P7 | Drift check skipped on resume | chunked_orchestrator | MEDIUM-HIGH |

### High Impact on UX / Quality

| # | Problem | File | Impact |
|---|---------|------|--------|
| P4 | Planner unaware of files_expected scope | planner.py | Wasted LLM calls |
| P5 | old_string checked at apply, not at parse | coder.py | Wasted LLM calls |
| M4 | Reviewer/summary roles don't use memory | reviewer.py | Poor review quality |
| P8 | Reviewer diff cap cuts wrong end | reviewer.py | Misses real issues |
| M2 | Token budget drops facts silently | prompt_builder.py | Inconsistent context |

### Stabilization / Maintenance

| # | Problem | File | Impact |
|---|---------|------|--------|
| M1 | Migration on hot read path | memory_store.py | Perf regression |
| P1 | _execute_single_chunk monolith | chunked_orchestrator | Maintainability |
| P6 | Naive rate limit handling | planner/coder | Reliability |
| M3 | Provenance fire-and-forget | memory_store | Observability |
| M5 | LLM suggestion quality unfiltered | run_outcome_suggestions | UX noise |
| M6 | No memory staleness TTL | memory_store | Accuracy over time |
| M7 | Bootstrap is if/elif chain | bootstrap.py | Extensibility |

---

## Part 4 — How to Use This With Fable 5

Each problem above has a self-contained "What Fable 5 Should Build" block. The recommended approach:

1. Start with **P2 (saga pattern)** and **P3 (triage grounding)** — these fix correctness bugs.
2. Then **P4 + P5** together — these eliminate the most common wasted LLM calls and directly improve output quality.
3. Then **M4 (reviewer memory)** — quick wire-up with immediate quality improvement.
4. Then **M1 (migration cleanup)** and **P6 (retry backoff)** — stabilization.
5. **P1 (stage machine)** last — it's a large refactor that touches many tests and should come after the correctness fixes are in.

Each item should be its own PR scoped to the exact files listed. Do not combine items.

When giving a problem brief to Fable 5, paste the problem header, the "What Fable 5 Should Build" block, and the relevant file paths. Include the CLAUDE.md safety rules so it doesn't accidentally bypass approval gates or weaken scope guards while refactoring.
