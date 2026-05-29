"""
plan_path_grounding.py
Deterministic repo-aware grounding for chunk-plan ``files_expected`` (PR #9B).

Triage/plan generation can hallucinate plausible-but-fake paths
(``frontend/src/components/LoginPage.js``, ``src/features/extra_feature.py``).
``files_expected`` is later enforced as hard scope by the scope-drift guard, so
a fake path is not a harmless hint — it is invented scope. This module removes
ungrounded paths from ``files_expected`` *after* triage/plan generation and
*before* persisting the run/chunks.

Pure and deterministic: no LLM, no filesystem walk. It reads only the existing
repo index (``file_index`` rows) for the project.

Grounding rules — a path is kept only if:
  1. it exactly matches an indexed file path, OR
  2. it is a NEW file under an already-indexed directory AND its extension
     matches an extension already present under that directory (so a ``.js``
     file is rejected in a ``.tsx``/``.ts`` area).

Everything else is removed: generic placeholders (``src/...``,
``backend/src/...``, ``frontend/src/...``) whose directory is not indexed,
paths whose parent directory is unknown, extension mismatches, and names that
look invented from the feature text. Root-level new files are only kept on an
exact index match (we never invent root files either).

When any path is removed from a chunk, the chunk is hardened: ``risk_level`` is
forced to ``high``, ``requires_human_review`` to ``True``, and the rationale is
annotated that exact files were uncertain. If ``files_expected`` becomes empty
it is left empty — never backfilled with an invented path.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from sqlalchemy import text

from backend.db.database import engine
from backend.models.chunk import ChunkDefinition, TriageResult

logger = logging.getLogger(__name__)

GROUNDING_UNCERTAINTY_NOTE = (
    "Exact files could not be determined from the current repository index, so "
    "ungrounded/invented paths were removed from files_expected. A human must "
    "confirm the real target files before implementation."
)


@dataclass
class GroundingIndex:
    """A read-only snapshot of a project's indexed paths for grounding."""

    paths: set[str] = field(default_factory=set)
    dirs: set[str] = field(default_factory=set)
    exts_by_dir: dict[str, set[str]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.paths


def _normalize_path(path: str) -> str | None:
    """
    Normalize to forward slashes and reject anything unsafe to ground:
    absolute paths and parent traversal. Returns None when not groundable.
    """
    if not path or not isinstance(path, str):
        return None
    norm = path.replace("\\", "/").strip()
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.strip("/") if norm.startswith("/") else norm
    if not norm:
        return None
    if norm.startswith("/") or os.path.isabs(path):
        return None
    if ".." in norm.split("/"):
        return None
    return norm


def get_indexed_paths_and_dirs(project_id: str) -> GroundingIndex:
    """
    Build a :class:`GroundingIndex` from the project's ``file_index`` rows.

    ``dirs`` holds every ancestor directory of an indexed file (excluding the
    repo root). ``exts_by_dir`` maps each such directory to the set of file
    extensions present anywhere beneath it.
    """
    index = GroundingIndex()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT path FROM file_index WHERE project_id = :project_id
            """), {"project_id": project_id}).fetchall()
    except Exception as error:
        logger.warning(
            "plan_path_grounding.py: index load failed; treating as empty. "
            "project_id=%s | error=%s",
            project_id,
            error,
        )
        return index

    for row in rows:
        norm = _normalize_path(row[0])
        if norm is None:
            continue
        index.paths.add(norm)
        ext = os.path.splitext(norm)[1].lower()
        parts = norm.split("/")
        for depth in range(1, len(parts)):
            directory = "/".join(parts[:depth])
            index.dirs.add(directory)
            if ext:
                index.exts_by_dir.setdefault(directory, set()).add(ext)
    return index


def _is_grounded(norm: str, index: GroundingIndex) -> bool:
    # Rule 1: exact match against an indexed file.
    if norm in index.paths:
        return True
    # Root-level new files are never invented; require an exact match above.
    if "/" not in norm:
        return False
    parent = norm.rsplit("/", 1)[0]
    # Rule 2: new file under a known directory with a matching extension.
    if parent not in index.dirs:
        return False
    ext = os.path.splitext(norm)[1].lower()
    if not ext:
        return False
    return ext in index.exts_by_dir.get(parent, set())


def is_grounded_path(project_id: str, path: str) -> bool:
    """Public single-path check; builds the index on demand."""
    norm = _normalize_path(path)
    if norm is None:
        return False
    return _is_grounded(norm, get_indexed_paths_and_dirs(project_id))


def _ground_chunk_with_index(
    chunk: ChunkDefinition,
    index: GroundingIndex,
) -> ChunkDefinition:
    kept: list[str] = []
    removed: list[str] = []
    seen: set[str] = set()
    for original in chunk.files_expected:
        norm = _normalize_path(original)
        if norm is not None and _is_grounded(norm, index):
            if norm not in seen:
                seen.add(norm)
                kept.append(norm)
        else:
            removed.append(original)

    if not removed:
        return chunk

    rationale = chunk.rationale or ""
    note = GROUNDING_UNCERTAINTY_NOTE
    if removed:
        note += " Removed ungrounded paths: " + ", ".join(removed) + "."
    rationale = f"{rationale.rstrip()} {note}".strip() if rationale else note

    logger.info(
        "[GROUNDING] Chunk %s: removed %d ungrounded path(s); forcing high risk "
        "+ human review. removed=%s",
        getattr(chunk, "chunk_number", "?"),
        len(removed),
        removed,
    )

    return chunk.model_copy(update={
        "files_expected": kept,
        "risk_level": "high",
        "requires_human_review": True,
        "rationale": rationale,
    })


def ground_chunk_files_expected(
    project_id: str,
    chunk: ChunkDefinition,
) -> ChunkDefinition:
    """Ground a single chunk's ``files_expected`` (builds the index on demand)."""
    return _ground_chunk_with_index(
        chunk,
        get_indexed_paths_and_dirs(project_id),
    )


def ground_triage_result_paths(
    project_id: str,
    triage_result: TriageResult,
) -> TriageResult:
    """
    Return a new :class:`TriageResult` with every chunk's ``files_expected``
    grounded against the project's repo index. Chunk count, numbering, and
    dependencies are unchanged; only files_expected/risk/review/rationale may
    change. Used for both implementation and plan_only flows.
    """
    index = get_indexed_paths_and_dirs(project_id)
    grounded = [_ground_chunk_with_index(chunk, index) for chunk in triage_result.chunks]
    return triage_result.model_copy(update={"chunks": grounded})
