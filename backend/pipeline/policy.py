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

# --- Tester (tester.py) -----------------------------------------------------
# Hard wall-clock limit for one test-command run.
TESTER_TIMEOUT_SECONDS = 300
# Stored/displayed test-output cap (tail-preserving; see tester.py).
MAX_OUTPUT_CHARS = 10000
# Bounded autonomous retry budget for infrastructure/harness failures after a
# patch has been rolled back. TIMEOUT is classified as a harness error but is
# explicitly not auto-retried by the orchestrator.
AUTO_RETRY_INFRA_BUDGET = 1
# Phase 1 item 9b policy knob only. Scoped verification remains disabled by
# default and is not wired into execution in this slice.
SCOPED_VERIFICATION_ENABLED = False

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
    "security",
    "permission",
    "permissions",
    "login",
    "password",
    "crypto",
    "jwt",
    "oauth",
    "session",
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
# provider request shapes exactly.
PROMPT_CACHE_ENABLED = False
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

# --- Final-approval summary (chunked_orchestrator.py, item 14) --------------
# Display-only cap for the cumulative branch diff (base..HEAD) shown in the
# final-approval summary. Head+tail preserving, mirroring the reviewer's diff-cap
# discipline: a large diff is truncated, never dropped. The diff is summary/audit
# display only — it is never persisted to the turn log or memory.
FINAL_DIFF_MAX_CHARS = 20000
