"""
stage_profile.py

Deterministic stage-profile selection for item 17a.

The profile is a cost optimization only: for eligible and sampled trivial
fresh chunks, the driver may synthesize the planner handoff from the already
approved triage/chunk instead of calling the planner LLM. It never grants
scope, never changes approval, and never decides retry/steer/refinement
behavior. Any uncertainty returns STANDARD.
"""

from __future__ import annotations

import fnmatch
import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Callable

from backend.models.chunk import ChunkDefinition, TriageResult
from backend.models.handoff import PlannerHandoff
from backend.pipeline import policy
from backend.utils.path_safety import normalize_relative_path, validate_safe_relative_path


class StageProfile(StrEnum):
    STANDARD = "standard"
    MERGED_PLAN_CODE = "merged_plan_code"


PathExistsFn = Callable[[str, str], bool]


def _default_repo_file_exists(target_repo_path: str, rel_path: str) -> bool:
    """
    Return true only for an existing regular file safely inside the target repo.

    New-file chunks intentionally fail eligibility during the first soak.
    """
    try:
        full_path = validate_safe_relative_path(rel_path, Path(target_repo_path))
    except Exception:
        return False
    return full_path.is_file()


def _normalized_for_policy(path: str) -> str | None:
    try:
        return normalize_relative_path(path).lower()
    except Exception:
        return None


def is_profile_denylisted(path: str) -> bool:
    """
    Conservative force-standard path denylist for trivial profile eligibility.

    This is not the write-safety authority; path_safety/scope_guard still own
    hard enforcement. The denylist only prevents planner elision on risky paths.
    """
    normalized = _normalized_for_policy(path)
    if normalized is None:
        return True
    parts = [part for part in normalized.split("/") if part]
    for pattern in policy.TRIVIAL_PROFILE_DENYLIST_PATTERNS:
        lowered = pattern.lower()
        if fnmatch.fnmatch(normalized, lowered):
            return True
        if fnmatch.fnmatch(parts[-1] if parts else normalized, lowered):
            return True
        if lowered in parts:
            return True
    return False


def is_trivial_eligible(
    triage: TriageResult | None,
    chunk: ChunkDefinition,
    target_repo_path: str,
    *,
    path_exists: PathExistsFn = _default_repo_file_exists,
) -> bool:
    """Return whether a chunk is eligible for planner elision."""
    try:
        if triage is None:
            return False
        if triage.total_chunks != 1:
            return False
        if triage.complexity != "easy":
            return False
        if chunk.risk_level != "low":
            return False
        if chunk.requires_human_review:
            return False
        if chunk.depends_on:
            return False
        if not chunk.files_expected:
            return False
        for rel_path in chunk.files_expected:
            if is_profile_denylisted(rel_path):
                return False
            if not path_exists(target_repo_path, rel_path):
                return False
    except Exception:
        return False
    return True


def sampling_bucket(run_id: str, chunk_number: int) -> int:
    """Stable 0-99 cohort bucket keyed by run and chunk."""
    digest = hashlib.sha256(f"{run_id}:{chunk_number}".encode("utf-8")).hexdigest()
    return int(digest, 16) % 100


def resolve_stage_profile(
    triage: TriageResult | None,
    chunk: ChunkDefinition,
    target_repo_path: str,
    *,
    run_id: str,
    sample_pct: int,
    path_exists: PathExistsFn = _default_repo_file_exists,
) -> StageProfile:
    """
    Resolve the active stage profile for a fresh chunk pass.

    sample_pct <= 0 is the hard off switch and returns before reading repo state.
    """
    if sample_pct <= 0:
        return StageProfile.STANDARD
    if sample_pct > 100:
        sample_pct = 100
    if not is_trivial_eligible(
        triage,
        chunk,
        target_repo_path,
        path_exists=path_exists,
    ):
        return StageProfile.STANDARD
    return (
        StageProfile.MERGED_PLAN_CODE
        if sampling_bucket(run_id, chunk.chunk_number) < sample_pct
        else StageProfile.STANDARD
    )


def synthesize_trivial_plan(
    *,
    run_id: str,
    feature_description: str,
    chunk: ChunkDefinition,
) -> PlannerHandoff:
    """Build the deterministic plan handoff used by MERGED_PLAN_CODE."""
    return PlannerHandoff(
        handoff_from="planner",
        handoff_to="coder",
        run_id=run_id,
        feature_description=feature_description,
        goal=chunk.title,
        steps=[
            f"Implement chunk {chunk.chunk_number}: {chunk.title}",
            chunk.description,
        ],
        files_to_create=[],
        files_to_modify=list(chunk.files_expected),
        files_to_read=list(chunk.files_expected),
        out_of_scope=[],
        risks=[],
        suggested_memory_entries=[],
    )
