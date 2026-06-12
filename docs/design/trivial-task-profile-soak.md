# Trivial-task stage profile — soak monitoring (item 17a)

**Audience:** operator / maintainer running the 17a soak.
**Scope:** how to read the soak cohorts from the `chunk_attempts` ledger and how to roll the feature back. SQL only — no code controller, no metrics endpoint (proposal §18.3: "instrumentation is the ledger").

## What is active

Item 17a (trivial-task stage profile) is **live on `develop`** at the soak default `policy.MERGED_PROFILE_SAMPLE_PCT = 50`. For a *provably trivial, eligible, sampled* **fresh** chunk, the driver synthesizes a deterministic `PlannerHandoff` from the already-approved triage instead of calling the planner LLM. Nothing else moves: triage, the coder, the reviewer (on every chunk), `scope_guard`, preflight, baseline verification, all gates, and commit/rollback are unchanged. The profile is **never an authority channel** — it cannot change scope, approval, which memory is injected, reviewer independence, or Git/merge behavior.

> **Item 17b (provider prompt caching) is implemented but off by default.** `PROMPT_CACHE_ENABLED = False` keeps provider behavior unchanged unless deliberately enabled; it is separate from this 17a soak.

## `stage_profile` values (`chunk_attempts.stage_profile`)

Recorded once per driver attempt. It is a **closed audit label, never read as authority** (retry/steer/refine eligibility stays keyed on `patch_failures` types + `ExecutionIntegrity`, never on this column).

| Value | Meaning |
|---|---|
| `NULL` | Feature off (`MERGED_PROFILE_SAMPLE_PCT = 0`), **or** any non-fresh pass (`human_retry` / `steered` / refinement / `resume`), **or** a legacy row written before 17a. The standard planner path ran (or the concept doesn't apply). |
| `standard` | Fresh pass, feature on (nonzero sampling), using the standard planner path. Use `trivial_profile_eligible` to distinguish eligible controls from ineligible standard chunks. |
| `merged_plan_code` | Fresh pass, chunk **eligible and sampled** — the **profiled** path. The planner LLM call was skipped and the handoff synthesized from triage. |

`stage_profile` alone is not enough to read the control cohort: ineligible fresh chunks under nonzero sampling may also record `standard` because they use the normal planner path.

## `trivial_profile_eligible` values (`chunk_attempts.trivial_profile_eligible`)

This nullable audit flag separates eligible controls from ineligible standard chunks. It is never an authority channel.

| Value | Meaning |
|---|---|
| `NULL` | Feature off (`MERGED_PROFILE_SAMPLE_PCT = 0`), legacy rows, or rows where the 17a experiment does not apply. |
| `1` / `true` | Fresh feature-on chunk was eligible for the trivial profile. It is in the profiled cohort if `stage_profile = 'merged_plan_code'`, or the eligible control cohort if `stage_profile = 'standard'`. |
| `0` / `false` | Fresh feature-on chunk was not eligible for the trivial profile. It may still have `stage_profile = 'standard'`, but it is not an eligible control chunk. |

The eligible control cohort is therefore:

- `entry_mode = 'fresh'`
- `trivial_profile_eligible = true`
- `stage_profile = 'standard'`

The profiled cohort is:

- `entry_mode = 'fresh'`
- `trivial_profile_eligible = true`
- `stage_profile = 'merged_plan_code'`

## Reading the cohorts

Sampling is **stable per `run_id + chunk_number`**, so a chunk's cohort is fixed across re-reads — these queries are replay-safe.

### 1. Cohort sizes (sanity / soak window)

```sql
SELECT stage_profile,
       COUNT(*) AS attempts,
       COUNT(DISTINCT run_id || ':' || chunk_number) AS chunks,
       MIN(created_at) AS first_seen,
       MAX(created_at) AS last_seen
FROM chunk_attempts
WHERE entry_mode = 'fresh'
  AND trivial_profile_eligible = true
  AND stage_profile IN ('standard', 'merged_plan_code')
GROUP BY stage_profile;
```

The §18.3 soak window is ≥ 30 profiled (`merged_plan_code`) chunks **or** 30 days — check `chunks` for `merged_plan_code` here before drawing conclusions.

### 2. Outcome-class distribution per cohort

The core comparison: is the profiled cohort's first (fresh) pass landing worse outcomes than the control? `final_outcome_class` is the closed taxonomy `SUCCESS | CODE_REJECTED | INFRA_ERROR | POLICY_BLOCKED | NEEDS_HUMAN`.

```sql
SELECT stage_profile,
       final_outcome_class,
       COUNT(*) AS n,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY stage_profile), 1) AS pct
FROM chunk_attempts
WHERE entry_mode = 'fresh'
  AND trivial_profile_eligible = true
  AND stage_profile IN ('standard', 'merged_plan_code')
GROUP BY stage_profile, final_outcome_class
ORDER BY stage_profile, n DESC;
```

Watch `CODE_REJECTED` (the generated change was wrong): a materially higher rate for `merged_plan_code` than `standard` is the signal that dropping the planner hurt quality.

### 3. Rework rate (attempts per chunk, all entry modes)

A profiled chunk that needed retries/steers shows up as extra `chunk_attempts` rows under continuation/non-fresh entry modes. Auto-retry continuation rows may carry `stage_profile` from the original fresh attempt, so classify each chunk by its **fresh-pass eligible cohort**, then count every attempt that chunk accumulated:

```sql
WITH fresh AS (
    SELECT run_id, chunk_number, stage_profile
    FROM chunk_attempts
    WHERE entry_mode = 'fresh'
      AND trivial_profile_eligible = true
      AND stage_profile IN ('standard', 'merged_plan_code')
)
SELECT f.stage_profile,
       COUNT(DISTINCT f.run_id || ':' || f.chunk_number) AS chunks,
       COUNT(a.id) AS total_attempts_all_modes,
       ROUND(1.0 * COUNT(a.id)
             / COUNT(DISTINCT f.run_id || ':' || f.chunk_number), 2) AS avg_attempts_per_chunk
FROM fresh f
JOIN chunk_attempts a
  ON a.run_id = f.run_id
 AND a.chunk_number = f.chunk_number
GROUP BY f.stage_profile;
```

A higher `avg_attempts_per_chunk` for `merged_plan_code` means the profiled path is driving more rework.

> **High-severity finding rate (the §18.3 / D12 trigger) is not in this table.** Findings live in `chunk_reviews.findings_json`, joinable to a cohort by `run_id` + `chunk_number` against the `fresh` CTE above. That comparison is a separate query over `chunk_reviews` and is out of scope for this `chunk_attempts` note.

## Rollback (single config flip, no code change)

The §18.3 D12 trigger — profiled high-severity finding rate > 5pp over control, **or** chunk-gate rejection rate roughly doubles — disables the profile by setting the sample percentage to zero:

```python
# backend/pipeline/policy.py
MERGED_PROFILE_SAMPLE_PCT = 0
```

With `0`, the orchestrator returns before reading any repo state, every fresh chunk takes the standard planner path, and new attempts record `stage_profile = NULL` and `trivial_profile_eligible = NULL` — i.e. **byte-identical to pre-17a behavior plus nullable audit columns**. This is a config flip, never a code revert; restart the backend to pick it up. Existing ledger rows are unchanged (history stays auditable).

## Caveats

- `stage_profile` and `trivial_profile_eligible` are **audit metadata only**. Nothing branches on them for retry, eligibility, scope, approval, or Git.
- Both columns are additive and nullable; legacy `NULL` rows are expected and never perturb resume / `get_latest_completed_attempt_head`.
- Ineligible standard chunks must not be included in the eligible control cohort. Filter on `entry_mode = 'fresh'`, `trivial_profile_eligible = true`, and `stage_profile = 'standard'`.
- Auto-retry continuation rows may carry the original attempt's `stage_profile`; do not count them as fresh soak cohort rows unless the query intentionally studies continuation behavior.
- These are operator queries against a live SQLite ledger — read-only; do not `UPDATE` `chunk_attempts`.
