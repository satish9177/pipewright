"""
chunk_sizing.py
Dormant advisory chunk-size notes for chunk plans.

This module is deterministic and advisory-only. It reads token estimates from
the disposable repo index and, when enabled by policy, returns copied chunk-plan
models with extra ``[SIZE]`` rationale notes. It never changes scope, risk,
review requirements, dependencies, execution state, or approval behavior.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from backend.db.database import engine
from backend.llm.role_config import Role, resolve_context_window, resolve_role_config
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import policy

logger = logging.getLogger(__name__)

SIZE_NOTE_PREFIX = "[SIZE]"


def load_file_token_estimates(project_id: str) -> dict[str, int]:
    """Read indexed token estimates for a project from ``file_index``."""
    estimates: dict[str, int] = {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT path, token_estimate
                FROM file_index
                WHERE project_id = :project_id
            """), {"project_id": project_id}).fetchall()
    except Exception as error:
        logger.warning(
            "chunk_sizing.py: token estimate load failed; using defaults. "
            "project_id=%s | error=%s",
            project_id,
            error,
        )
        return estimates

    for row in rows:
        path = row[0]
        if not path:
            continue
        try:
            estimate = int(row[1])
        except (TypeError, ValueError):
            estimate = policy.CHUNK_SIZING_NEW_FILE_DEFAULT_TOKENS
        estimates[str(path)] = max(0, estimate)
    return estimates


def _coder_context_window() -> int:
    config = resolve_role_config(Role.CODER)
    return resolve_context_window(config.model)


def _chunk_token_total(
    files_expected: list[str],
    estimates: dict[str, int],
) -> tuple[int, list[str]]:
    total = 0
    missing: list[str] = []
    for path in files_expected:
        if path in estimates:
            total += estimates[path]
        else:
            total += policy.CHUNK_SIZING_NEW_FILE_DEFAULT_TOKENS
            missing.append(path)
    return total, missing


def _size_notes_for_chunk(
    chunk: ChunkDefinition,
    *,
    total_chunks: int,
    token_estimates: dict[str, int],
    budget_tokens: int,
) -> list[str]:
    if not chunk.files_expected:
        return []

    token_total, missing_paths = _chunk_token_total(
        chunk.files_expected,
        token_estimates,
    )
    notes: list[str] = []

    if token_total > budget_tokens:
        note = (
            f"estimated file context is {token_total} tokens, above the "
            f"advisory chunk budget of {budget_tokens} tokens; consider "
            "splitting before approval."
        )
        if missing_paths:
            note += (
                " Unindexed file(s) used the default estimate: "
                + ", ".join(missing_paths)
                + "."
            )
        notes.append(note)

    if len(chunk.files_expected) > policy.CHUNK_SIZING_MANY_FILES_THRESHOLD:
        notes.append(
            f"chunk touches {len(chunk.files_expected)} files, above the "
            f"advisory threshold of {policy.CHUNK_SIZING_MANY_FILES_THRESHOLD}; "
            "consider splitting if review feels broad."
        )

    if (
        total_chunks > 1
        and token_total < policy.CHUNK_SIZING_TINY_TOKEN_FLOOR
    ):
        notes.append(
            f"chunk is very small at {token_total} estimated tokens in a "
            "multi-chunk plan; consider merging if it is not a distinct "
            "dependency."
        )

    return notes


def _append_size_notes(chunk: ChunkDefinition, notes: list[str]) -> ChunkDefinition:
    if not notes:
        return chunk
    rationale = chunk.rationale or ""
    if SIZE_NOTE_PREFIX in rationale:
        return chunk
    note_text = " ".join(f"{SIZE_NOTE_PREFIX} {note}" for note in notes)
    rationale = f"{rationale.rstrip()} {note_text}".strip() if rationale else note_text
    return chunk.model_copy(update={"rationale": rationale})


def annotate_triage_result_sizing(
    project_id: str,
    triage_result: TriageResult,
    *,
    coder_context_window: int | None = None,
) -> TriageResult:
    """
    Return a triage result with advisory ``[SIZE]`` rationale notes when enabled.

    Flag-off is a passthrough. Flag-on may copy chunks, but the only field this
    function is allowed to change is ``rationale``.
    """
    if not policy.CHUNK_SIZING_ADVISORY_ENABLED:
        return triage_result

    context_window = (
        int(coder_context_window)
        if coder_context_window is not None
        else _coder_context_window()
    )
    budget_tokens = int(context_window * policy.CHUNK_CONTEXT_BUDGET_SHARE)
    token_estimates = load_file_token_estimates(project_id)

    annotated = [
        _append_size_notes(
            chunk,
            _size_notes_for_chunk(
                chunk,
                total_chunks=triage_result.total_chunks,
                token_estimates=token_estimates,
                budget_tokens=budget_tokens,
            ),
        )
        for chunk in triage_result.chunks
    ]
    return triage_result.model_copy(update={"chunks": annotated})
