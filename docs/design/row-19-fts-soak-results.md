# Row 19 FTS Soak Results

Date: 2026-06-16

## Seeded Corpus

Command: `compare --seed`

```text
Database initialized successfully.
Database path: C:\Users\satis\Projects\pipewright\backend\db\pipewright.db
mode=compare
project_id=fts-seed-2bfb5c830f7f4ee6a01d4367d9991dd8
role=planner
included_set_identical=True
mandatory_tier_identical=True
only_relevance_tier_may_reorder=True
relevance_order_delta=2
fts_coverage_count=1
fallback_count=0
no_cross_project_facts=True
deterministic_output=True
```

## Real Project

Command: `compare --project-id 35f64222-a92f-4010-97d9-8e9ea491e35e --role planner --title uses --token-budget 4000`

```text
mode=compare
project_id=35f64222-a92f-4010-97d9-8e9ea491e35e
role=planner
included_set_identical=True
mandatory_tier_identical=True
only_relevance_tier_may_reorder=True
relevance_order_delta=0
fts_coverage_count=0
fallback_count=1
no_cross_project_facts=True
deterministic_output=True
```

## Safety Notes

- Safety invariants confirmed: included fact set stayed identical, mandatory tier stayed identical, cross-project facts did not appear, and output was deterministic.
- The real-project run was read-only and fell back because the project had no FTS coverage for the sampled request.
- No activation trigger was added or exercised.
- No rebuild-on-write or lazy rebuild-on-read was added.
- No default-on flag flip was made.
- No Row 23 vector or embedding work was started.
