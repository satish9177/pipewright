"""
llm.py
Read-only LLM provider diagnostics route (#33E).

Exposes GET /llm/diagnostics: per-role resolved provider/model and a sanitized
availability status. It is observability only — it performs no LLM completion,
mutates nothing, and never changes provider routing.
"""

from fastapi import APIRouter

from backend.llm.diagnostics import LLMDiagnosticsReport, diagnose_all_roles

router = APIRouter()


@router.get("/llm/diagnostics", response_model=LLMDiagnosticsReport)
def get_llm_diagnostics() -> LLMDiagnosticsReport:
    """Per-role provider/model availability diagnostics. Read-only."""
    return diagnose_all_roles()
