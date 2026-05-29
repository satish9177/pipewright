# Stabilization Status — Final Smoke

Status summary for the local stabilization track (PRs #1–#9B) plus the
final smoke/regression gate (PR #10). This is the single page that says what
each stabilization PR guaranteed and how that guarantee is held in place by
automated tests.

The consolidated regression gate lives in
`backend/tests/test_stabilization_smoke.py`. Each checklist item below maps to
one or more tests there; deeper, exhaustive coverage stays in the dedicated
module test files named in parentheses.

## Completed stabilization PRs

| PR | Area | Guarantee |
| --- | --- | --- |
| #1 | Empty / no-change Git safety | Coder output with no `files_changed` never reaches patch, test, commit, or push. |
| #2 | Intent boundary | A request is classified into `report_only` / `plan_only` / `implementation` before any execution path runs. |
| #3 | Project memory isolation | Memory facts are project-scoped; a blank/`None` project_id returns no memory and cannot read another project's facts. |
| #4 | Deterministic high-risk + scope-drift guards | Risk-bearing chunks are force-upgraded to high risk + human review; coder output outside `files_expected` is blocked before patch/test/commit; empty `files_expected` is unsafe and blocked. |
| #5 | Read-only result views | `report_only` and `plan_only` runs render read-only result views and cannot execute mutating routes. |
| #6 | Hybrid intent classifier fallback | Deterministic layers decide first; the LLM fallback runs only for ambiguous input and degrades safely to `plan_only` / clarification. |
| #7 | Plan-to-implementation handoff | `start-implementation` creates a *separate* implementation run with `source_plan_run_id` set; the source plan run is never mutated. |
| #8A | Real report-only analyzer | `report_only` runs a strictly read-only analyzer (no coder/patch/test/git/PR), degrading to a limited report on failure. |
| #8B | Structured `report_json` + ReportView | Analyzer also emits a structured `report_json`; ReportView renders it and falls back to `plain_english_summary`. |
| #8C | Report intent specialization | `report_kind` (`project_explanation` / `issue_review` / `feature_discovery` / `general_analysis`) is classified deterministically and specializes the analyzer prompt. |
| #9A | Hybrid request routing / ambiguous-implementation guard | Greetings/noise and vague implementation requests return `needs_clarification` and create no run; discovery questions route read-only. |
| #9B | Repo-aware chunk plan quality | Ungrounded/invented `files_expected` paths are removed against the repo index; affected chunks are hardened and never backfilled with invented paths. |

## Final smoke checklist (PR #10)

Each line is asserted by `backend/tests/test_stabilization_smoke.py`.

- [x] Non-action/greeting guard returns `needs_clarification` and creates no run. *(PR #9A — also `test_chunk_routes.py`)*
- [x] Greeting + a real implementation request still routes to implementation. *(gap filled by PR #10)*
- [x] `report_only` requests execute the read-only analyzer only (no triage, no chunk creation, no coder). *(PR #8A/#8B/#8C — also `test_report_analyzer.py`)*
- [x] `plan_only` requests create `plan_ready` and do not execute code (mutating routes are rejected). *(PR #5 — also `test_chunk_routes.py`)*
- [x] Plan-to-implementation creates a separate implementation run with `source_plan_run_id`; source plan run unchanged. *(PR #7 — also `test_plan_handoff.py`)*
- [x] Vague implementation requests return `needs_clarification`. *(PR #9A — also `test_implementation_guard.py`)*
- [x] Specific implementation requests proceed to chunk approval. *(PR #9A — also `test_chunk_routes.py`)*
- [x] Empty/no-change coder output never reaches patch/test/commit/push. *(PR #1 — also `test_chunked_orchestrator.py`)*
- [x] Scope drift fails before patch/test/commit. *(PR #4 — also `test_scope_guard.py`, `test_chunked_orchestrator.py`)*
- [x] Fake/unindexed `files_expected` paths are removed by plan-path grounding. *(PR #9B — also `test_plan_path_grounding.py`)*
- [x] Empty `files_expected` forces high risk and human review. *(PR #4 — also `test_risk_scanner.py`)*
- [x] Deterministic high-risk triggers for migration/auth/secrets/git/checkpoint/routes/CI/dependency changes. *(PR #4 — also `test_risk_scanner.py`)*
- [x] Memory facts are project-scoped; blank/`None` project_id returns no memory. *(PR #3 — also `test_memory.py`)*

## Running the gate

```powershell
# Full unit suite (includes the smoke gate)
python -m pytest backend\tests -q -m unit

# Just the consolidated stabilization smoke gate
python -m pytest backend\tests\test_stabilization_smoke.py -q -m unit
```

Manual end-to-end smoke flows for the chunked pipeline are documented separately
in `docs/phase2b-smoke-tests.md`.

## Safety invariants (do not weaken)

- `report_only` and `plan_only` runs must never call coder, patch, test, commit,
  or PR creation.
- Empty `touched_files` / `files_changed` must never reach Git operations.
- Coder output outside the approved `files_expected` scope must never be patched.
- These invariants are enforced in code (intent routing, `scope_guard`,
  `risk_scanner`, `plan_path_grounding`, the orchestrator's no-change guard) and
  regression-locked by the smoke gate above.

## Paused (out of scope for stabilization)

Ollama, deployment, provider settings UI, BYOK DB storage, and execution modes
are intentionally paused and were not touched by the stabilization track.


## Follow-up Stabilization Items

### Empty `files_expected` execution UX

During final smoke testing, a chunk with empty `files_expected` was correctly marked high-risk and required human review. After approval, execution started but was safely blocked by `scope_guard.py` with:

```text
scope_guard.py: chunk has empty files_expected; cannot safely apply patch.