# Pipewright Ledger Metrics Queries

Docs-only observability guide for maintainers who want evidence from existing
Pipewright SQLite ledger data before deciding whether to activate dormant
features. These queries are read-only and advisory. They do not activate flags,
change runtime behavior, modify schema, or justify a flag flip by themselves.

The table and column names below were checked against
`backend/db/schema.sql` and the additive shape helpers in
`backend/db/database.py`. The queries intentionally avoid raw prompts, raw
diffs, raw provider errors, file contents, secrets, tokens, and free-form user
text. Prefer running them against a copied or dev SQLite database, for example a
copy of `backend/db/pipewright.db`, opened with a read-only SQLite connection.
When sharing results, aggregate further if run ids, project ids, or timestamps
would reveal private workflow details.

Notes:

- All SQL blocks are `SELECT`-only.
- Some metrics are proxies because the current schema does not persist every
  future activation predicate as a separate column.
- `json_each` queries require SQLite JSON1 support. If the local SQLite build
  lacks JSON1, use the adjacent non-JSON aggregate queries instead.
- Metrics are advisory. A human still needs to inspect failures, sample runs,
  and safety invariants before any dormant flag activation.

## 1. Stage-profile / scoped verification cohort health

The current schema stores stage-profile and trivial-profile eligibility on
`chunk_attempts`. It does not store a dedicated scoped-verification flag, so
verification health uses `chunks.test_run_*` metadata as a proxy.

```sql
-- Decision: Compare fresh stage-profile cohorts before expanding a dormant
-- stage-profile or scoped-verification cohort.
SELECT
    COALESCE(ca.stage_profile, 'legacy_or_unrecorded') AS stage_profile,
    CASE ca.trivial_profile_eligible
        WHEN 1 THEN 'eligible'
        WHEN 0 THEN 'not_eligible'
        ELSE 'not_recorded'
    END AS trivial_profile_eligibility,
    COALESCE(c.test_run_verdict, 'not_recorded') AS test_run_verdict,
    COALESCE(c.test_run_command_quality, 'not_recorded') AS test_command_quality,
    COUNT(*) AS attempt_count,
    COUNT(DISTINCT ca.run_id) AS run_count,
    COUNT(DISTINCT ca.run_id || ':' || ca.chunk_number) AS chunk_count,
    SUM(CASE WHEN ca.final_status = 'completed' THEN 1 ELSE 0 END) AS completed_attempts,
    ROUND(
        100.0 * SUM(CASE WHEN ca.final_status = 'completed' THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0),
        1
    ) AS completed_pct
FROM chunk_attempts ca
LEFT JOIN chunks c
    ON c.run_id = ca.run_id
   AND c.chunk_number = ca.chunk_number
WHERE ca.entry_mode = 'fresh'
GROUP BY
    COALESCE(ca.stage_profile, 'legacy_or_unrecorded'),
    CASE ca.trivial_profile_eligible
        WHEN 1 THEN 'eligible'
        WHEN 0 THEN 'not_eligible'
        ELSE 'not_recorded'
    END,
    COALESCE(c.test_run_verdict, 'not_recorded'),
    COALESCE(c.test_run_command_quality, 'not_recorded')
ORDER BY attempt_count DESC;
```

```sql
-- Decision: Find verification-evidence weak spots before narrowing test scope
-- for any cohort. This is a proxy; scoped verification is not separately
-- persisted in the current ledger.
SELECT
    COALESCE(c.test_run_command_quality, 'not_recorded') AS test_command_quality,
    COALESCE(c.test_run_verdict, 'not_recorded') AS test_run_verdict,
    c.test_run_counts_parsed,
    c.test_run_zero_tests_detected,
    COUNT(*) AS chunk_count,
    SUM(CASE WHEN c.status = 'completed' THEN 1 ELSE 0 END) AS completed_chunks,
    SUM(CASE WHEN c.test_run_verdict IN ('weak', 'none', 'unknown') THEN 1 ELSE 0 END)
        AS weak_or_unknown_evidence_chunks
FROM chunks c
GROUP BY
    COALESCE(c.test_run_command_quality, 'not_recorded'),
    COALESCE(c.test_run_verdict, 'not_recorded'),
    c.test_run_counts_parsed,
    c.test_run_zero_tests_detected
ORDER BY chunk_count DESC;
```

## 2. Attempt ledger outcomes by stage/profile

Use this to see where attempts end and whether a profile or entry mode is
concentrating failures, human gates, or completions.

```sql
-- Decision: Compare final attempt outcomes by entry mode and stage profile.
SELECT
    ca.entry_mode,
    COALESCE(ca.stage_profile, 'not_applicable_or_legacy') AS stage_profile,
    COALESCE(ca.final_outcome_class, 'not_recorded') AS final_outcome_class,
    COALESCE(ca.final_status, 'not_recorded') AS final_status,
    COUNT(*) AS attempt_count,
    COUNT(DISTINCT ca.run_id) AS run_count,
    SUM(CASE WHEN ca.head_sha IS NOT NULL THEN 1 ELSE 0 END) AS attempts_with_head,
    MIN(ca.created_at) AS first_seen_at,
    MAX(ca.created_at) AS last_seen_at
FROM chunk_attempts ca
GROUP BY
    ca.entry_mode,
    COALESCE(ca.stage_profile, 'not_applicable_or_legacy'),
    COALESCE(ca.final_outcome_class, 'not_recorded'),
    COALESCE(ca.final_status, 'not_recorded')
ORDER BY attempt_count DESC;
```

```sql
-- Decision: Locate the stage-level outcome classes most associated with each
-- profile without selecting the raw stage_outcomes_json payload.
SELECT
    COALESCE(ca.stage_profile, 'not_applicable_or_legacy') AS stage_profile,
    json_extract(stage.value, '$.stage') AS stage_name,
    json_extract(stage.value, '$.outcome_class') AS stage_outcome_class,
    COUNT(*) AS stage_event_count,
    COUNT(DISTINCT ca.run_id || ':' || ca.chunk_number) AS chunk_count
FROM chunk_attempts ca,
     json_each(ca.stage_outcomes_json) AS stage
WHERE ca.stage_outcomes_json IS NOT NULL
GROUP BY
    COALESCE(ca.stage_profile, 'not_applicable_or_legacy'),
    json_extract(stage.value, '$.stage'),
    json_extract(stage.value, '$.outcome_class')
ORDER BY stage_event_count DESC;
```

## 3. INFRA_ERROR retry frequency and recovery rate

`INFRA_ERROR` is a narrative outcome class. Retry authority still lives in the
runtime policy and failure classifier, not in these metrics.

```sql
-- Decision: Measure how often INFRA_ERROR appears by entry mode and status.
SELECT
    ca.entry_mode,
    COALESCE(ca.stage_profile, 'not_applicable_or_legacy') AS stage_profile,
    COALESCE(ca.final_status, 'not_recorded') AS final_status,
    COUNT(*) AS infra_attempt_count,
    COUNT(DISTINCT ca.run_id || ':' || ca.chunk_number) AS affected_chunk_count,
    MIN(ca.created_at) AS first_seen_at,
    MAX(ca.created_at) AS last_seen_at
FROM chunk_attempts ca
WHERE ca.final_outcome_class = 'INFRA_ERROR'
   OR ca.stage_outcomes_json LIKE '%"outcome_class": "INFRA_ERROR"%'
   OR ca.stage_outcomes_json LIKE '%"outcome_class":"INFRA_ERROR"%'
GROUP BY
    ca.entry_mode,
    COALESCE(ca.stage_profile, 'not_applicable_or_legacy'),
    COALESCE(ca.final_status, 'not_recorded')
ORDER BY infra_attempt_count DESC;
```

```sql
-- Decision: Estimate recovery after the first INFRA_ERROR attempt for each
-- run/chunk pair.
SELECT
    DATE(first_infra.created_at) AS first_infra_day,
    COUNT(*) AS infra_chunk_count,
    SUM(CASE WHEN later_success.id IS NOT NULL THEN 1 ELSE 0 END) AS recovered_chunk_count,
    ROUND(
        100.0 * SUM(CASE WHEN later_success.id IS NOT NULL THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0),
        1
    ) AS recovered_pct
FROM (
    SELECT
        ca.run_id,
        ca.project_id,
        ca.chunk_number,
        MIN(ca.attempt_number) AS first_infra_attempt_number,
        MIN(ca.created_at) AS created_at
    FROM chunk_attempts ca
    WHERE ca.final_outcome_class = 'INFRA_ERROR'
       OR ca.stage_outcomes_json LIKE '%"outcome_class": "INFRA_ERROR"%'
       OR ca.stage_outcomes_json LIKE '%"outcome_class":"INFRA_ERROR"%'
    GROUP BY ca.run_id, ca.project_id, ca.chunk_number
) first_infra
LEFT JOIN chunk_attempts later_success
    ON later_success.run_id = first_infra.run_id
   AND later_success.project_id = first_infra.project_id
   AND later_success.chunk_number = first_infra.chunk_number
   AND later_success.attempt_number > first_infra.first_infra_attempt_number
   AND later_success.final_status = 'completed'
GROUP BY DATE(first_infra.created_at)
ORDER BY first_infra_day DESC;
```

## 4. Human approval / rejection / final approval timing

These queries avoid `diff`, `test_results`, summaries, and rejection text. They
only use approval metadata and timestamps.

```sql
-- Decision: Measure approval latency and rejection/pending rates by gate type.
SELECT
    COALESCE(ag.approval_type, 'legacy') AS approval_type,
    COALESCE(ag.risk_level, 'not_recorded') AS risk_level,
    ag.status,
    COUNT(*) AS gate_count,
    ROUND(
        AVG(
            CASE
                WHEN ag.decided_at IS NOT NULL AND ag.created_at IS NOT NULL
                THEN (julianday(ag.decided_at) - julianday(ag.created_at)) * 24.0
            END
        ),
        2
    ) AS avg_decision_hours,
    ROUND(
        MAX(
            CASE
                WHEN ag.status = 'pending' AND ag.created_at IS NOT NULL
                THEN (julianday('now') - julianday(ag.created_at)) * 24.0
            END
        ),
        2
    ) AS max_pending_hours
FROM approval_gates ag
GROUP BY
    COALESCE(ag.approval_type, 'legacy'),
    COALESCE(ag.risk_level, 'not_recorded'),
    ag.status
ORDER BY gate_count DESC;
```

```sql
-- Decision: See whether final approvals are backing up or taking materially
-- longer than chunk/plan approvals.
SELECT
    pr.status AS run_status,
    ag.status AS final_gate_status,
    COUNT(*) AS final_gate_count,
    ROUND(
        AVG(
            CASE
                WHEN ag.decided_at IS NOT NULL AND ag.created_at IS NOT NULL
                THEN (julianday(ag.decided_at) - julianday(ag.created_at)) * 24.0
            END
        ),
        2
    ) AS avg_final_decision_hours,
    ROUND(
        AVG(
            CASE
                WHEN ag.status = 'pending' AND ag.created_at IS NOT NULL
                THEN (julianday('now') - julianday(ag.created_at)) * 24.0
            END
        ),
        2
    ) AS avg_pending_hours
FROM approval_gates ag
JOIN pipeline_runs pr
    ON pr.id = ag.run_id
WHERE ag.approval_type = 'final'
GROUP BY pr.status, ag.status
ORDER BY final_gate_count DESC;
```

## 5. Steer / retry / post-success-refinement usage

`run_turns.steer_text` is intentionally not selected. Post-success refinement is
estimated as a steered attempt that follows an earlier completed attempt for the
same run/chunk.

```sql
-- Decision: Measure human steer and plan-turn volume without reading steer text.
SELECT
    rt.target_type,
    COALESCE(rt.outcome, 'not_recorded') AS turn_outcome,
    COUNT(*) AS turn_count,
    COUNT(DISTINCT rt.run_id) AS run_count,
    MIN(rt.created_at) AS first_seen_at,
    MAX(rt.created_at) AS last_seen_at
FROM run_turns rt
GROUP BY rt.target_type, COALESCE(rt.outcome, 'not_recorded')
ORDER BY turn_count DESC;
```

```sql
-- Decision: Separate retries, steers, auto retries, and refinement candidates
-- from the append-only attempt ledger.
SELECT
    ca.entry_mode,
    CASE
        WHEN ca.entry_mode = 'steered'
         AND EXISTS (
             SELECT 1
             FROM chunk_attempts prior
             WHERE prior.run_id = ca.run_id
               AND prior.chunk_number = ca.chunk_number
               AND prior.attempt_number < ca.attempt_number
               AND prior.final_status = 'completed'
         )
        THEN 'post_success_refinement_candidate'
        WHEN ca.entry_mode = 'steered'
        THEN 'failed_chunk_steer_or_recovery'
        ELSE 'not_steered'
    END AS steer_context,
    COALESCE(ca.final_status, 'not_recorded') AS final_status,
    COUNT(*) AS attempt_count,
    COUNT(DISTINCT ca.run_id || ':' || ca.chunk_number) AS chunk_count
FROM chunk_attempts ca
WHERE ca.entry_mode IN ('human_retry', 'steered', 'auto_retry')
GROUP BY
    ca.entry_mode,
    CASE
        WHEN ca.entry_mode = 'steered'
         AND EXISTS (
             SELECT 1
             FROM chunk_attempts prior
             WHERE prior.run_id = ca.run_id
               AND prior.chunk_number = ca.chunk_number
               AND prior.attempt_number < ca.attempt_number
               AND prior.final_status = 'completed'
         )
        THEN 'post_success_refinement_candidate'
        WHEN ca.entry_mode = 'steered'
        THEN 'failed_chunk_steer_or_recovery'
        ELSE 'not_steered'
    END,
    COALESCE(ca.final_status, 'not_recorded')
ORDER BY attempt_count DESC;
```

## 6. Reviewer finding acknowledgement / blocking frequency

Exact A1 finding category/severity classification is computed in code from
review findings. This doc does not select `findings_json`, because it can contain
finding bodies. The queries below use persisted review status/verdict and active
acknowledgement rows as metadata-level proxies.

```sql
-- Decision: Track review volume, unavailable/failed reviewer rate, and active
-- acknowledgement coverage without reading finding bodies.
SELECT
    cr.review_status,
    COALESCE(cr.verdict, 'none') AS verdict,
    COUNT(*) AS review_count,
    COUNT(DISTINCT cr.run_id) AS run_count,
    SUM(CASE WHEN rfa.id IS NOT NULL THEN 1 ELSE 0 END) AS active_ack_rows,
    SUM(CASE WHEN rfa.id IS NULL THEN 1 ELSE 0 END) AS rows_without_active_ack
FROM chunk_reviews cr
LEFT JOIN review_finding_acknowledgements rfa
    ON rfa.run_id = cr.run_id
   AND rfa.chunk_number = cr.chunk_number
   AND rfa.status = 'active'
   AND rfa.acknowledged_diff_hash = cr.reviewed_test_checkpoint_hash
GROUP BY cr.review_status, COALESCE(cr.verdict, 'none')
ORDER BY review_count DESC;
```

```sql
-- Decision: Estimate approval gates that may have been waiting on reviewer
-- acknowledgement. This is a conservative proxy; exact blocking depends on
-- current high findings in the policy categories.
SELECT
    ag.approval_type,
    ag.status AS gate_status,
    COUNT(DISTINCT ag.id) AS gate_count,
    SUM(
        CASE
            WHEN EXISTS (
                SELECT 1
                FROM chunk_reviews cr
                WHERE cr.run_id = ag.run_id
                  AND (ag.approval_type = 'final' OR cr.chunk_number = ag.chunk_number)
                  AND cr.review_status = 'completed'
                  AND cr.verdict IN ('needs_human_attention', 'risky')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM review_finding_acknowledgements rfa
                      WHERE rfa.run_id = cr.run_id
                        AND rfa.chunk_number = cr.chunk_number
                        AND rfa.status = 'active'
                        AND rfa.acknowledged_diff_hash = cr.reviewed_test_checkpoint_hash
                  )
            )
            THEN 1 ELSE 0
        END
    ) AS potential_review_ack_blocked_gates
FROM approval_gates ag
WHERE ag.approval_type IN ('chunk', 'final')
GROUP BY ag.approval_type, ag.status
ORDER BY gate_count DESC;
```

## 7. Row 16 post-run hygiene candidate yield

This looks at persisted suggestion metadata only. It does not call the generator
and does not inspect suggestion content, evidence excerpts, or rationales.

```sql
-- Decision: Compare manual and post-run-auto suggestion yield by source and
-- lifecycle status before deciding whether Row 16 activation is worth more soak.
SELECT
    COALESCE(ms.suggested_by, 'unknown') AS generation_path,
    COALESCE(ms.source_type, 'not_recorded') AS source_type,
    ms.status,
    COUNT(*) AS suggestion_count,
    COUNT(DISTINCT ms.source_run_id) AS source_run_count,
    COUNT(DISTINCT ms.project_id) AS project_count,
    ROUND(AVG(ms.quality_score), 1) AS avg_quality_score,
    SUM(CASE WHEN ms.quality_score IS NULL THEN 1 ELSE 0 END) AS unscored_count
FROM memory_suggestions ms
WHERE ms.source_run_id IS NOT NULL
GROUP BY
    COALESCE(ms.suggested_by, 'unknown'),
    COALESCE(ms.source_type, 'not_recorded'),
    ms.status
ORDER BY suggestion_count DESC;
```

```sql
-- Decision: Check whether post-run suggestions are concentrated in terminal
-- run states that match the intended Row 16 evidence collection path.
SELECT
    COALESCE(ms.suggested_by, 'unknown') AS generation_path,
    COALESCE(pr.status, 'missing_run') AS run_status,
    COALESCE(ms.source_type, 'not_recorded') AS source_type,
    ms.status AS suggestion_status,
    COUNT(*) AS suggestion_count,
    COUNT(DISTINCT ms.source_run_id) AS run_count
FROM memory_suggestions ms
LEFT JOIN pipeline_runs pr
    ON pr.id = ms.source_run_id
WHERE ms.source_run_id IS NOT NULL
GROUP BY
    COALESCE(ms.suggested_by, 'unknown'),
    COALESCE(pr.status, 'missing_run'),
    COALESCE(ms.source_type, 'not_recorded'),
    ms.status
ORDER BY suggestion_count DESC;
```

## 8. Row 12 omission impact-if-enabled, metadata only

Row 12 omission remains dormant unless its flag is enabled. The current
`memory_injection_events` table stores included/excluded counts and hashes, but
the raw `entries_json` can contain memory fact text, so these queries do not
select it.

```sql
-- Decision: Estimate how often current memory injection already faces budget or
-- policy pressure before enabling relevance omission.
SELECT
    mie.role,
    COUNT(*) AS injection_event_count,
    COUNT(DISTINCT mie.run_id) AS run_count,
    COUNT(DISTINCT mie.project_id) AS project_count,
    SUM(mie.included_count) AS included_entries,
    SUM(mie.excluded_count) AS excluded_entries,
    SUM(CASE WHEN mie.excluded_count > 0 THEN 1 ELSE 0 END) AS events_with_exclusions,
    ROUND(AVG(mie.token_budget), 1) AS avg_token_budget,
    COUNT(DISTINCT mie.category_policy) AS distinct_category_policy_count
FROM memory_injection_events mie
GROUP BY mie.role
ORDER BY injection_event_count DESC;
```

```sql
-- Decision: Identify low-budget roles where dormant Row 12 omission would be
-- most likely to change which relevance facts fit.
SELECT
    mie.role,
    CASE
        WHEN mie.token_budget IS NULL THEN 'not_recorded'
        WHEN mie.token_budget < 500 THEN 'under_500'
        WHEN mie.token_budget < 1000 THEN '500_to_999'
        WHEN mie.token_budget < 2000 THEN '1000_to_1999'
        ELSE '2000_or_more'
    END AS token_budget_bucket,
    COUNT(*) AS injection_event_count,
    SUM(mie.included_count) AS included_entries,
    SUM(mie.excluded_count) AS excluded_entries,
    ROUND(AVG(mie.excluded_count), 2) AS avg_excluded_per_event
FROM memory_injection_events mie
GROUP BY
    mie.role,
    CASE
        WHEN mie.token_budget IS NULL THEN 'not_recorded'
        WHEN mie.token_budget < 500 THEN 'under_500'
        WHEN mie.token_budget < 1000 THEN '500_to_999'
        WHEN mie.token_budget < 2000 THEN '1000_to_1999'
        ELSE '2000_or_more'
    END
ORDER BY injection_event_count DESC;
```

## 9. Prompt cache eligibility / provider-role cache opportunity

The ledger records provider/model metadata and token counts. It does not persist
whether a provider accepted a cache marker, so these are opportunity estimates.

```sql
-- Decision: Rank provider/role pairs by input-token volume to decide where
-- prompt-cache activation or soak would have the most observable effect.
SELECT
    lcp.role,
    lcp.provider,
    lcp.model,
    COUNT(*) AS call_count,
    COUNT(DISTINCT lcp.run_id) AS run_count,
    SUM(COALESCE(lcp.input_tokens, 0)) AS input_tokens,
    SUM(COALESCE(lcp.output_tokens, 0)) AS output_tokens,
    ROUND(AVG(lcp.input_tokens), 1) AS avg_input_tokens,
    SUM(
        CASE
            WHEN lcp.provider = 'anthropic' THEN COALESCE(lcp.input_tokens, 0)
            ELSE 0
        END
    ) AS anthropic_input_token_opportunity
FROM llm_call_provenance lcp
GROUP BY lcp.role, lcp.provider, lcp.model
ORDER BY input_tokens DESC;
```

```sql
-- Decision: Compare LLM calls with memory-injection variability by role. More
-- distinct memory hashes means less identical prompt surface for cache hits.
SELECT
    lcp.role,
    lcp.provider,
    lcp.model,
    COUNT(*) AS call_count,
    COUNT(DISTINCT mie.entries_hash) AS distinct_memory_hash_count,
    ROUND(AVG(mie.included_count), 1) AS avg_included_memory_count,
    ROUND(AVG(mie.excluded_count), 1) AS avg_excluded_memory_count
FROM llm_call_provenance lcp
LEFT JOIN memory_injection_events mie
    ON mie.run_id = lcp.run_id
   AND mie.role = lcp.role
   AND (
       mie.chunk_number = lcp.chunk_number
       OR (mie.chunk_number IS NULL AND lcp.chunk_number IS NULL)
   )
GROUP BY lcp.role, lcp.provider, lcp.model
ORDER BY call_count DESC;
```

## 10. Plan-turn / plan-version activity

These queries use the append-only plan-version ledger and plan-target turn rows.
They do not select stored plan JSON.

```sql
-- Decision: Measure plan-version lineage activity and approval uptake without
-- reading triage_json or chunk_plan.
SELECT
    pv.source,
    COUNT(*) AS plan_version_count,
    COUNT(DISTINCT pv.run_id) AS run_count,
    SUM(CASE WHEN pr.approved_plan_version = pv.version THEN 1 ELSE 0 END)
        AS versions_that_became_approved,
    ROUND(AVG(pv.version), 2) AS avg_version_number,
    MIN(pv.created_at) AS first_seen_at,
    MAX(pv.created_at) AS last_seen_at
FROM plan_versions pv
JOIN pipeline_runs pr
    ON pr.id = pv.run_id
GROUP BY pv.source
ORDER BY plan_version_count DESC;
```

```sql
-- Decision: Track plan-turn attempts and outcomes from run_turns metadata only.
SELECT
    COALESCE(rt.outcome, 'not_recorded') AS plan_turn_outcome,
    COUNT(*) AS plan_turn_count,
    COUNT(DISTINCT rt.run_id) AS run_count,
    MIN(rt.created_at) AS first_seen_at,
    MAX(rt.created_at) AS last_seen_at
FROM run_turns rt
WHERE rt.target_type = 'plan'
GROUP BY COALESCE(rt.outcome, 'not_recorded')
ORDER BY plan_turn_count DESC;
```

## 11. Run failure / stuck-state overview

These queries avoid run feature text and provider/Git errors. They use status,
step, gate state, and age only.

```sql
-- Decision: Find run states that are accumulating old or failed runs.
SELECT
    pr.status,
    COALESCE(pr.current_step, 'not_recorded') AS current_step,
    COALESCE(pr.intent, 'not_recorded') AS intent,
    COALESCE(pr.chunk_plan_status, 'not_recorded') AS chunk_plan_status,
    COUNT(*) AS run_count,
    ROUND(AVG((julianday('now') - julianday(pr.created_at)) * 24.0), 2)
        AS avg_age_hours,
    ROUND(MAX((julianday('now') - julianday(pr.created_at)) * 24.0), 2)
        AS max_age_hours,
    SUM(CASE WHEN pr.approved_plan_version IS NULL THEN 1 ELSE 0 END)
        AS runs_without_approved_plan_version
FROM pipeline_runs pr
GROUP BY
    pr.status,
    COALESCE(pr.current_step, 'not_recorded'),
    COALESCE(pr.intent, 'not_recorded'),
    COALESCE(pr.chunk_plan_status, 'not_recorded')
ORDER BY run_count DESC;
```

```sql
-- Decision: Find pending human gates that may explain stuck runs.
SELECT
    ag.approval_type,
    pr.status AS run_status,
    COALESCE(pr.current_step, 'not_recorded') AS current_step,
    COUNT(*) AS pending_gate_count,
    COUNT(DISTINCT ag.run_id) AS affected_run_count,
    ROUND(AVG((julianday('now') - julianday(ag.created_at)) * 24.0), 2)
        AS avg_pending_hours,
    ROUND(MAX((julianday('now') - julianday(ag.created_at)) * 24.0), 2)
        AS max_pending_hours
FROM approval_gates ag
JOIN pipeline_runs pr
    ON pr.id = ag.run_id
WHERE ag.status = 'pending'
GROUP BY
    ag.approval_type,
    pr.status,
    COALESCE(pr.current_step, 'not_recorded')
ORDER BY max_pending_hours DESC;
```

## 12. Soak run results (2026-06-18, read-only)

First execution of the queries above, run `SELECT`-only against a throwaway
read-only copy of `backend/db/pipewright.db`. No SQL errors; JSON1 available. No
code, flag, schema, or runtime changes. This section records aggregate findings
only — no run ids, timestamps, raw prompts, diffs, provider/Git errors, secrets,
tokens, or PII.

**Dataset caveat:** ~255 runs over ~3 weeks, but the data reads as seeded /
synthetic rather than live human operation — approval decisions average
sub-minute latency, gate `timeout` states are common, and `run_turns` (5) and
`chunk_attempts` (25) samples are small. Treat the numbers as *shape*, not
representative rates.

- **Run health / stuck states:** failures concentrate at the `plan` and
  first-chunk steps; most `failed`/`awaiting_*_approval` rows are gates that
  timed out, not runtime crashes. No pending gates were stuck at query time.
- **INFRA_ERROR:** zero occurrences in attempts or stage-outcome JSON — the
  retry/recovery path has **no evidence** either way in this dataset.
- **Approvals:** plenty of approve/reject/timeout gate rows, but ~0.01h decision
  latency confirms scripted decisions; not representative of real human timing.
- **Steer / retry / post-success refinement:** near-anecdotal volume (a handful
  of `human_retry` / `steered` attempts; a few plan/chunk turns).
- **Reviewer acknowledgement:** reviews occur (`approve_with_notes`,
  `needs_human_attention`, `risky`), but recorded **active acknowledgement rows
  are very low**.
- **Row 12 omission pressure:** `coder`/`planner` show **0 exclusions** at their
  budget; only `triage` shows existing category-policy exclusions — consistent
  with the prior "proven no-op," no new relevance pressure.
- **Prompt-cache opportunity:** `llm_call_provenance` coverage is incomplete and
  effectively single-provider (DeepSeek/coder); **no Anthropic or Gemini
  provenance** is present, so cache activation has **no observable evidence**.
- **Row 16 hygiene yield:** the auto path has produced nothing (flag dormant, as
  expected); manual `run_outcome` suggestions show a **high rejection rate**,
  which is a quality yellow flag to investigate before any auto activation.

**Conclusion:** the dataset is too synthetic and too sparse to justify activating
any dormant feature. **Keep all dormant flags OFF** and gather real-traffic soak
data (plus broader `llm_call_provenance` coverage) before revisiting activation.
These metrics remain advisory and do not by themselves justify a flag flip.
