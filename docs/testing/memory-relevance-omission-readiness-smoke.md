# Memory Relevance Omission Readiness Smoke

Manual UI smoke checklist for the dormant request-aware relevance omission path.
This document does **not** activate `MEMORY_RELEVANCE_OMISSION_ENABLED`.

## Purpose

Stage 2 proves that omitted memory will be visible when a later activation turns
the flag on. Because the shipped flag remains `False`, normal local runs should
not produce `not_relevant_to_request` entries. This smoke uses a synthetic
`memory_injection_events` row to exercise the Run Detail UI contract without
changing prompt selection behavior.

## Setup

- Start the backend and frontend with the standard local setup from
  [README](../../README.md#quick-local-setup).
- Use an existing project or create a throwaway project.
- Create or identify a run row whose Run Detail page you can open.
- Keep `MEMORY_RELEVANCE_OMISSION_ENABLED = False`.

## Seed A Synthetic Omission Snapshot

Use a local script or SQL console against the local SQLite database to insert one
`memory_injection_events` row for the run. The row should include:

- `run_id`: the run you will open.
- `project_id`: the run's project.
- `role`: `coder`.
- `included_entries`: at least one safety or pinned-looking memory.
- `excluded_entries`: at least one entry with
  `exclusion_reason: "not_relevant_to_request"`.

The stored entry shape should mirror the existing provenance snapshots:

```json
{
  "included": [
    {
      "fact_id": "synthetic-included",
      "content": "Never leak tokens.",
      "content_hash": "synthetic-hash-included",
      "category": "security",
      "scope": "global",
      "priority": 100,
      "status_at_injection": "active",
      "exclusion_reason": null
    }
  ],
  "excluded": [
    {
      "fact_id": "synthetic-excluded",
      "content": "Database migration note number 1 stays small.",
      "content_hash": "synthetic-hash-excluded",
      "category": "style",
      "scope": "global",
      "priority": 100,
      "status_at_injection": "active",
      "exclusion_reason": "not_relevant_to_request"
    }
  ]
}
```

## UI Smoke

Open Run Detail -> Details & audit -> What Pipewright told the AI.

Confirm:

- The excluded row reason renders as `Not relevant to this request.`
- The aggregate copy appears:
  `N memories were left out as not relevant to this request.`
- The aggregate surface is neutral/slate and not the amber budget-drop banner.
- The aggregate surface is not the red safety warning.
- Existing `budget_dropped` copy remains unchanged.
- Existing `category_not_allowed_for_role` copy remains unchanged.
- The aggregate copy includes:
  `Raise a memory's priority to keep it in front of the AI.`
- Project memory priority guidance appears:
  `Lower priority numbers are offered to the AI first. The lowest band (10 or below) marks a memory as pinned — highest precedence.`
- The UI does not promise always-included, guaranteed, or force-included memory.

## Expected Result

The synthetic omitted memory is visible and understandable, memory is not
mutated, and the system remains dormant by default. A normal run with the flag
still `False` should not emit `not_relevant_to_request`.
