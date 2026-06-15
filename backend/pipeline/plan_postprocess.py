"""
plan_postprocess.py

Shared deterministic post-triage processing for chunk plans.

This helper owns the behavior-preserving chain that runs after LLM triage and
before a plan is persisted: path grounding, risk scan, explicit file-scope
reconciliation, and dormant advisory annotations. It does not execute chunks,
approve plans, grant scope, or mutate the database.
"""

from __future__ import annotations

from backend.models.chunk import TriageResult
from backend.pipeline.chunk_isolation import annotate_triage_result_isolation
from backend.pipeline.chunk_sizing import annotate_triage_result_sizing
from backend.pipeline.file_scope_intent import UserFileConstraints, reconcile_file_scope
from backend.pipeline.plan_path_grounding import ground_triage_result_paths
from backend.pipeline.risk_scanner import scan_triage_result


def apply_plan_postprocess(
    *,
    project_id: str,
    triage_result: TriageResult,
    request_file_constraints: UserFileConstraints,
    sanctioned_new_paths: set[str] | None = None,
) -> TriageResult:
    """
    Apply the same deterministic post-triage pipeline used by initial plans.

    ``sanctioned_new_paths`` exists for initial create-target grounding only.
    Plan-turn revisions pass ``None``: v2+ can revise an already produced plan,
    but this PR does not rerun the route-level explicit-create resolver.
    """
    triage_result = ground_triage_result_paths(
        project_id,
        triage_result,
        sanctioned_new_paths=sanctioned_new_paths,
    )
    triage_result = scan_triage_result(triage_result)
    triage_result = reconcile_file_scope(
        project_id,
        triage_result,
        request_file_constraints,
        sanctioned_new_paths=sanctioned_new_paths,
    )
    triage_result = annotate_triage_result_sizing(project_id, triage_result)
    return annotate_triage_result_isolation(triage_result)
