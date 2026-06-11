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
