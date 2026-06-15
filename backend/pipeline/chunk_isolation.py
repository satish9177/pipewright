"""
chunk_isolation.py
Dormant advisory chunk-isolation notes for chunk plans.

This module is deterministic and advisory-only. When enabled by policy, it
returns copied chunk-plan models with extra ``[ISOLATION]`` rationale notes. It
never changes scope, risk, review requirements, dependencies, execution state,
or approval behavior.
"""

from __future__ import annotations

from collections import defaultdict

from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import policy

ISOLATION_NOTE_PREFIX = "[ISOLATION]"
_MIGRATION_SEGMENTS = frozenset({"alembic", "migration", "migrations"})


def _normalize_path(path: str) -> str | None:
    if not path or not isinstance(path, str):
        return None
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.strip("/")
    return normalized or None


def _is_migration_path(path: str) -> bool:
    normalized = _normalize_path(path)
    if normalized is None:
        return False
    segments = [segment.lower() for segment in normalized.split("/") if segment]
    return any(segment in _MIGRATION_SEGMENTS for segment in segments)


def _shared_paths_by_chunk(
    chunks: list[ChunkDefinition],
) -> dict[int, list[tuple[str, list[int]]]]:
    path_to_chunks: dict[str, set[int]] = defaultdict(set)
    display_path: dict[str, str] = {}

    for chunk in chunks:
        for path in chunk.files_expected:
            normalized = _normalize_path(path)
            if normalized is None:
                continue
            path_to_chunks[normalized].add(chunk.chunk_number)
            display_path.setdefault(normalized, normalized)

    overlaps: dict[int, list[tuple[str, list[int]]]] = defaultdict(list)
    for normalized, chunk_numbers in sorted(path_to_chunks.items()):
        if len(chunk_numbers) < 2:
            continue
        ordered = sorted(chunk_numbers)
        for chunk_number in ordered:
            others = [number for number in ordered if number != chunk_number]
            overlaps[chunk_number].append((display_path[normalized], others))
    return overlaps


def _overlap_note(shared_paths: list[tuple[str, list[int]]]) -> str:
    parts: list[str] = []
    for path, other_chunks in shared_paths:
        label = "chunk" if len(other_chunks) == 1 else "chunks"
        numbers = ", ".join(str(number) for number in other_chunks)
        parts.append(f"{path} (also in {label} {numbers})")
    return (
        "shares file(s) with other chunks: "
        + "; ".join(parts)
        + ". Consider clarifying ownership before approval."
    )


def _migration_mixing_note(chunk: ChunkDefinition) -> str | None:
    migration_paths: list[str] = []
    non_migration_paths: list[str] = []

    for path in chunk.files_expected:
        normalized = _normalize_path(path)
        if normalized is None:
            continue
        if _is_migration_path(normalized):
            migration_paths.append(normalized)
        else:
            non_migration_paths.append(normalized)

    if not migration_paths or not non_migration_paths:
        return None

    return (
        "mixes migration file(s) "
        + ", ".join(migration_paths)
        + " with non-migration file(s) "
        + ", ".join(non_migration_paths)
        + ". Consider isolating migration work before approval."
    )


def _append_isolation_notes(
    chunk: ChunkDefinition,
    notes: list[str],
) -> ChunkDefinition:
    if not notes:
        return chunk
    rationale = chunk.rationale or ""
    if ISOLATION_NOTE_PREFIX in rationale:
        return chunk
    note_text = " ".join(f"{ISOLATION_NOTE_PREFIX} {note}" for note in notes)
    rationale = f"{rationale.rstrip()} {note_text}".strip() if rationale else note_text
    return chunk.model_copy(update={"rationale": rationale})


def annotate_triage_result_isolation(triage_result: TriageResult) -> TriageResult:
    """
    Return a triage result with advisory ``[ISOLATION]`` rationale notes.

    Flag-off is a passthrough. Flag-on may copy chunks, but the only field this
    function is allowed to change is ``rationale``.
    """
    if not policy.CHUNK_ISOLATION_ADVISORY_ENABLED:
        return triage_result

    overlaps = _shared_paths_by_chunk(triage_result.chunks)
    annotated: list[ChunkDefinition] = []
    for chunk in triage_result.chunks:
        notes: list[str] = []
        shared_paths = overlaps.get(chunk.chunk_number)
        if shared_paths:
            notes.append(_overlap_note(shared_paths))
        migration_note = _migration_mixing_note(chunk)
        if migration_note:
            notes.append(migration_note)
        annotated.append(_append_isolation_notes(chunk, notes))

    return triage_result.model_copy(update={"chunks": annotated})
