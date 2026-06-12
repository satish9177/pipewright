# Trivial-task stage profile — soak monitoring (item 17a)

**Audience:** operator / maintainer running the 17a soak.
**Scope:** how to read the soak cohorts from the `chunk_attempts` ledger and how to roll the feature back. SQL only — no code controller, no metrics endpoint (proposal §18.3: "instrumentation is the ledger").

## What is active

Item 17a (trivial-task stage profile) is **live on `develop`** at the soak default `policy.MERGED_PROFILE_SAMPLE_PCT = 50`. For a *provably trivial, eligible, sampled* **fresh** chunk, the driver synthesizes a deterministic `PlannerHandoff` from the already-approved triage instead of calling the planner LLM. Nothing else moves: triage, the coder, the reviewer (on every chunk), `scope_guard`, preflight, baseline verification, all gates, and commit/rollback are unchanged. The profile is **never an authority channel** — it cannot change scope, approval, which memory is injected, reviewer independence, or Git/merge behavior.

> **Item 17b (provider prompt caching) is NOT implemented yet.** This note covers 17a only. `PROMPT_CACHE_ENABLED` does not exist on `develop`.

## `stage_profile` values (`chunk_attempts.stage_profile`)

Recorded once per driver attempt. It is a **closed audit label, never read as authority** (retry/steer/refine eligibility stays keyed on `patch_failures` types + `ExecutionIntegrity`, never on this column).

| Value | Meaning |
|---|---|
| `NULL` | Feature off (`MERGED_PROFILE_SAMPLE_PCT = 0`), **or** any non-fresh pass (`human_retry` / `steered` / refinement / `resume`), **or** a legacy row written before 17a. The standard planner path ran (or the concept doesn't apply). |
| `standard` | Fresh pass, feature on (nonzero sampling), chunk **eligible but not sampled** into the profiled cohort — the soak **control**. The planner LLM ran normally. |
| `merged_plan_code` | Fresh pass, chunk **eligible and sampled** — the **profiled** path. The planner LLM call was skipped and the handoff synthesized from triage. |

`standard` vs `merged_plan_code` are the two soak cohorts; both are fresh, feature-on passes. Ineligible fresh chunks under nonzero sampling also record `standard` (they share the control's standard path).

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
WHERE stage_profile IN ('standard', 'merged_plan_code')
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
WHERE stage_profile IN ('standard', 'merged_plan_code')
GROUP BY stage_profile, final_outcome_class
ORDER BY stage_profile, n DESC;
```

Watch `CODE_REJECTED` (the generated change was wrong): a materially higher rate for `merged_plan_code` than `standard` is the signal that dropping the planner hurt quality.

### 3. Rework rate (attempts per chunk, all entry modes)

A profiled chunk that needed retries/steers shows up as extra `chunk_attempts` rows under non-fresh entry modes (with `stage_profile = NULL`). Classify each chunk by its **fresh-pass** cohort, then count every attempt that chunk accumulated:

```sql
WITH fresh AS (
    SELECT run_id, chunk_number, stage_profile
    FROM chunk_attempts
    WHERE entry_mode = 'fresh'
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

With `0`, the orchestrator returns before reading any repo state, every fresh chunk takes the standard planner path, and new attempts record `stage_profile = NULL` — i.e. **byte-identical to pre-17a**. This is a config flip, never a code revert; restart the backend to pick it up. Existing ledger rows are unchanged (history stays auditable).

## Caveats

- `stage_profile` is **audit metadata only**. Nothing branches on it for retry, eligibility, scope, approval, or Git.
- The column is additive and nullable; legacy `NULL` rows are expected and never perturb resume / `get_latest_completed_attempt_head`.
- These are operator queries against a live SQLite ledger — read-only; do not `UPDATE` `chunk_attempts`.
