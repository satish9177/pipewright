"""
policy.py
Single source of truth for pipeline execution policy (§8b).

Behavioral constants — caps, budgets, timeouts — used to live scattered across
stage modules where they could drift apart (patch_dry_run already carried a
hand-mirrored copy of the coder's large-file cap). This module owns them.
Values are EXACTLY the previous stage-local defaults; moving a constant here
must never change behavior.

Pure constants only: no imports from the rest of the backend, so any module
(including the cycle-free pure layers) can read policy safely. Layered
defaults / per-project overrides are a later phase — do not add them here
piecemeal.

Per-role model selection deliberately does NOT live here:
``backend/llm/role_config.py`` is already its single source (the dead
per-stage model constants were deleted, not moved).
"""

import os
from collections.abc import Mapping

_TRUTHY_ENV_VALUES = frozenset({"true", "1", "yes", "on"})


def _env_flag_enabled(
    name: str,
    *,
    default: bool = False,
    env: Mapping[str, str] | None = None,
) -> bool:
    env = os.environ if env is None else env
    value = env.get(name)
    if value is None:
        return default
    return value.strip().lower() in _TRUTHY_ENV_VALUES


def _env_positive_int(
    name: str,
    *,
    default: int,
    env: Mapping[str, str] | None = None,
) -> int:
    env = os.environ if env is None else env
    value = env.get(name)
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


# --- Tester (tester.py) -----------------------------------------------------
# Hard wall-clock limit for one test-command run.
TESTER_TIMEOUT_ENV = "PIPEWRIGHT_TESTER_TIMEOUT_SECONDS"
TESTER_TIMEOUT_SECONDS = _env_positive_int(TESTER_TIMEOUT_ENV, default=300)
# Stored/displayed test-output cap (tail-preserving; see tester.py).
MAX_OUTPUT_CHARS = 10000
# Bounded autonomous retry budget for infrastructure/harness failures after a
# patch has been rolled back. TIMEOUT is classified as a harness error but is
# explicitly not auto-retried by the orchestrator.
AUTO_RETRY_INFRA_BUDGET = 1
# Phase 1 item 9b policy knob only. Scoped verification remains disabled by
# default and is not wired into execution in this slice.
SCOPED_VERIFICATION_ENABLED = False

# --- Chunk sizing advisories (chunk_sizing.py, row 6b / 18.7) --------------
# Dormant, advisory-only notes appended to chunk rationale. False preserves
# chunk-plan output exactly.
CHUNK_SIZING_ADVISORY_ENV = "PIPEWRIGHT_CHUNK_SIZING_ADVISORY_ENABLED"
CHUNK_SIZING_ADVISORY_ENABLED = _env_flag_enabled(
    CHUNK_SIZING_ADVISORY_ENV,
    default=False,
)
CHUNK_CONTEXT_BUDGET_SHARE = 0.5
CHUNK_SIZING_NEW_FILE_DEFAULT_TOKENS = 800
CHUNK_SIZING_MANY_FILES_THRESHOLD = 6
CHUNK_SIZING_TINY_TOKEN_FLOOR = 200

# --- Chunk isolation advisories (chunk_isolation.py, row 6c / 18.8) --------
# Dormant, advisory-only notes appended to chunk rationale. False preserves
# chunk-plan output exactly.
CHUNK_ISOLATION_ADVISORY_ENV = "PIPEWRIGHT_CHUNK_ISOLATION_ADVISORY_ENABLED"
CHUNK_ISOLATION_ADVISORY_ENABLED = _env_flag_enabled(
    CHUNK_ISOLATION_ADVISORY_ENV,
    default=False,
)

# --- Human attempts on a failed chunk (patch_failures.py, item 13) ----------
# Combined per-chunk budget for human-triggered attempts on a failed chunk:
# plain human retries and steered attempts (recovery_mode "human" /
# "human_with_instruction") share this one budget. Auto attempts
# (recovery_mode="auto") and the initial failed apply never consume it.
# Relocated from patch_failures.MAX_HUMAN_RETRIES with the value preserved;
# the redesign proposal suggests 5 as an alternative — raising it is a
# deliberate maintainer choice, not a default to bury.
HUMAN_ATTEMPT_BUDGET = 2
# Length cap for one steer message. Over-cap steers are rejected with a clear
# error, never silently truncated (truncation changes the user's meaning).
MAX_STEER_TEXT_CHARS = 4000
# Cap for the prior applied-diff text carried into a steered attempt's
# continuation context (head-preserving, like the reviewer's diff cap). The
# diff is prompt context only — never standing working-tree state.
STEER_CONTINUATION_DIFF_MAX_CHARS = 10000

# --- Plan-gate turns (§23 row 7b / §19) -----------------------------------
# Dormant by default. The PR-B route reads PLAN_TURNS_ENABLED and hides itself
# (404) while it is false; the producer enforces the cap so any caller gets the
# same bounded behavior by default.
PLAN_TURNS_ENV = "PIPEWRIGHT_PLAN_TURNS_ENABLED"
PLAN_TURNS_ENABLED = _env_flag_enabled(PLAN_TURNS_ENV, default=False)
PLAN_TURN_CAP = 5
# Length cap for one plan-turn message. Over-cap messages are rejected (422),
# never silently truncated (truncation changes the user's meaning). Kept
# separate from MAX_STEER_TEXT_CHARS so plan-turn and chunk-steer limits can
# diverge without one silently moving the other.
PLAN_TURN_MESSAGE_MAX_CHARS = 2000

# --- Trivial-task stage profile (chunk_driver.py, item 17a) -----------------
# Stable sample percentage for the first soak of the planner-elision profile.
# 0 is the hard off switch and must preserve the standard planner path exactly.
MERGED_PROFILE_SAMPLE_PCT = 50
# Conservative force-standard denylist. These are not write-safety rules
# (scope_guard/path_safety remain the authority); they only prevent planner
# elision on files where the planner may add safety-relevant structure.
TRIVIAL_PROFILE_DENYLIST_PATTERNS = frozenset({
    "*/migrations/*",
    "migrations/*",
    "*/alembic/*",
    "alembic/*",
    "schema.sql",
    "*.sql",
    "auth",
    "auth*",
    "*auth*",
    "security",
    "security*",
    "permission",
    "permissions",
    "login",
    "login*",
    "password",
    "*password*",
    "crypto",
    "crypto*",
    "jwt",
    "jwt*",
    "oauth",
    "oauth*",
    "session",
    "session*",
    "*token*",
    ".env*",
    "*secrets*",
    "*credentials*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "requirements*.txt",
    "pyproject.toml",
    "poetry.lock",
    "Pipfile*",
    "setup.py",
    "setup.cfg",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile*",
    ".github/*",
    ".gitlab-ci.yml",
    ".circleci/*",
    "Jenkinsfile",
    "Dockerfile*",
    "docker-compose*",
    "Makefile",
    "tox.ini",
    "*.config.js",
    "*.config.ts",
    "tsconfig.json",
})

# --- Provider prompt caching (LLM providers, item 17b) ----------------------
# Hard off switch for explicit prompt-cache translation. False must preserve
# provider request shapes exactly. The env override is for controlled local/dev
# soak only; unset, false-ish, and invalid values all keep it disabled.
PROMPT_CACHE_ENV = "PIPEWRIGHT_PROMPT_CACHE_ENABLED"
PROMPT_CACHE_ENABLED = _env_flag_enabled(PROMPT_CACHE_ENV, default=False)
# Disabled seam for future Gemini explicit caching. The 17b slice must not create
# CachedContent handles.
GEMINI_EXPLICIT_CACHE_ENABLED = False

# --- Coder file-context caps (coder.py, patch_dry_run.py) -------------------
# Files over this many lines may not be rewritten wholesale; they must be
# changed with a targeted action="edit". patch_dry_run enforces the same cap
# at apply time (one cap, two enforcement points).
MAX_FILE_LINES = 200
# Absolute cap for including a modify target as edit-grounding context.
# Between MAX_FILE_LINES and this cap a file is included in full so the model
# can target an exact edit; beyond it we refuse rather than truncate.
LARGE_FILE_CONTEXT_LINE_CAP = 1500

# --- Reviewer (reviewer.py) -------------------------------------------------
# Diff prompt cap (head-preserving) so a large diff never bloats the prompt
# or becomes an egress vector.
REVIEWER_MAX_DIFF_CHARS = 6000
# Reviewer informed-approval soft gate (Phase 4 item 15). Only current delivered
# high-severity findings in these categories require acknowledgement by default.
# Future per-project opt-ins for other categories belong in a later policy layer;
# this constant is the default-off seam for that future work.
REVIEW_ACK_REQUIRED_SEVERITY = "high"
REVIEW_ACK_REQUIRED_CATEGORIES = frozenset({
    "requirement_mismatch",
    "security",
})

# --- Memory suggestion quality gate (run_outcome_suggestions.py, M5 7-alpha) -
# Handoff passthrough suggestions are ranked deterministically before insertion.
# These caps apply only to non-floored handoff candidates; deterministic/template
# run-outcome channels are preserved regardless of the total cap.
HANDOFF_SUGGESTION_CAP = 5
RUN_SUGGESTION_TOTAL_CAP = 8

# --- Dormant post-run memory hygiene trigger (pr_orchestrator.py, Row 16 PR-A) -
# Controls automatic run-outcome suggestion generation after successful complete
# runs. False keeps the manual route as the only active path; tests may
# monkeypatch this flag on in isolation. The env override is for controlled
# local/dev soak only; unset, false-ish, and invalid values all keep it disabled.
MEMORY_POSTRUN_HYGIENE_ENV = "PIPEWRIGHT_MEMORY_POSTRUN_HYGIENE_ENABLED"
MEMORY_POSTRUN_HYGIENE_ENABLED = _env_flag_enabled(
    MEMORY_POSTRUN_HYGIENE_ENV,
    default=False,
)

# --- Memory injection budgets (prompt_builder.py, Row 12 PR-A) -------------
# Current effective budgets, relocated without behavior change. The adaptive
# guardrail seam below is dormant by default; while disabled, callers get these
# exact static values.
MEMORY_ROLE_TOKEN_BUDGETS = {
    "triage": 400,
    "planner": 1200,
    "architect": 1200,
    "coder": 1200,
    "reviewer": 800,
    "summary": 800,
}
MEMORY_DEFAULT_TOKEN_BUDGET = 1500


def estimate_memory_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


MEMORY_ADAPTIVE_BUDGET_ENABLED = False
MEMORY_BUDGET_FLOOR = 400
MEMORY_BUDGET_CEILING = 6000
MEMORY_ROLE_BUDGET_SHARE = 0.08
MEMORY_TOKEN_ESTIMATOR_MARGIN = 1.3


def resolve_memory_token_budget(
    role: str | None,
    context_window: int | None = None,
) -> int:
    role_key = (role or "default").strip().lower()
    static_budget = MEMORY_ROLE_TOKEN_BUDGETS.get(
        role_key,
        MEMORY_DEFAULT_TOKEN_BUDGET,
    )
    if not MEMORY_ADAPTIVE_BUDGET_ENABLED:
        return static_budget
    if context_window is None:
        return static_budget

    adaptive_budget = int(
        MEMORY_ROLE_BUDGET_SHARE
        * int(context_window)
        / MEMORY_TOKEN_ESTIMATOR_MARGIN
    )
    return max(
        MEMORY_BUDGET_FLOOR,
        min(adaptive_budget, MEMORY_BUDGET_CEILING),
    )

# --- Request-aware relevance omission + pinning (prompt_builder.py, Row 12 PR-C) --
# Single global off switch (D5). False is the SHIPPED DEFAULT and preserves Row 12
# PR-B behavior byte-for-byte: relevance *ordering* only, no omission, no pinning.
# Flipping it True activates BOTH request-aware relevance omission AND priority-based
# human-pinning together — the pin is the "always inject this" escape hatch that
# makes omission safe, so one switch governs the whole D5 behavior set. Rollback =
# set back to False (selection-time only; nothing persisted to unwind).
MEMORY_RELEVANCE_OMISSION_ENABLED = False
# Small-store grace (D5). Relevance *ordering* always applies, but relevance
# *omission* engages only when the role's count of NON-mandatory in-policy relevance
# facts exceeds this threshold. At or below it, every in-policy fact still injects,
# so enabling the flag is a no-op for today's small stores. Security, forbidden_paths,
# and pinned facts are mandatory and are NOT counted here.
MEMORY_SMALL_STORE_GRACE_THRESHOLD = 12
# Human-pinning (D5). A fact whose priority is at or below this threshold joins the
# mandatory (un-droppable) tier when MEMORY_RELEVANCE_OMISSION_ENABLED is on. Pinning
# rides the existing priority field (no schema change); the UI exposes it by setting
# priority. Lower priority value = higher precedence (see prompt_builder's sort key).
MEMORY_PIN_PRIORITY_THRESHOLD = 10

# --- Memory repo-reality warnings (routes/memory.py, Row 11 PR-B) ----------
# Active advisory repo-reality dimensions for memory-injection analysis. Roll
# back PR-B behavior by setting this to frozenset({"db_engine"}).
REPO_REALITY_SIGNAL_DIMENSIONS = frozenset({
    "db_engine",
    "backend_framework",
    "frontend_framework",
    "test_runner",
    "migration_tool",
    "package_manager",
})

# --- Final-approval summary (chunked_orchestrator.py, item 14) --------------
# Display-only cap for the cumulative branch diff (base..HEAD) shown in the
# final-approval summary. Head+tail preserving, mirroring the reviewer's diff-cap
# discipline: a large diff is truncated, never dropped. The diff is summary/audit
# display only — it is never persisted to the turn log or memory.
FINAL_DIFF_MAX_CHARS = 20000
