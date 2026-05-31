"""
conflict_scope.py
Pure, deterministic classifier for whether a run is "DB-sensitive".

Part of Memory M1.5 (design: docs/architecture/memory-conflict-run-gate.md §2).
The DB memory conflict run-scope gate (#16D-3/#16D-4) will use this to decide whether
a clear DB conflict is relevant enough to a run to warn or block. This module is the
pure scope classifier only — it does NOT gate, warn, mutate memory, or touch the
pipeline. Nothing in runtime code imports it yet (#16D-2).

Purity: no DB calls, no filesystem reads, no LLM, no side effects. Classification is
based solely on the deterministic shape of the expected file paths.
"""

from __future__ import annotations

from pathlib import Path

from backend.repo.repo_indexer import classify_file

# Dependency/manifest/config files that define the DB stack. Matched by basename.
_DB_MANIFEST_BASENAMES = frozenset({
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "alembic.ini",
    "schema.prisma",
})

# Directory path segments that strongly indicate DB/model/migration work. Matched as
# whole path segments (not substrings) so e.g. "dbutils/" does not match "db".
_DB_DIR_SEGMENTS = frozenset({
    "models",
    "model",
    "migrations",
    "migration",
    "alembic",
    "prisma",
    "db",
    "database",
    "repositories",
    "repository",
    "schemas",
    "schema",
    "entities",
    "entity",
    "queries",
    "query",
})

# classify_file results that count as DB-sensitive.
_DB_CLASSIFICATIONS = frozenset({"model", "migration"})


def _normalize(path: str) -> str:
    """Normalize to lowercase forward-slash form with any leading ./ stripped."""
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lower()


def get_db_sensitivity_reason(path: str) -> str | None:
    """
    Return a short reason string when the path is a strong DB-sensitive indicator,
    else None. Pure and never raises for ordinary strings.

    Reasons (first match wins):
      "manifest:<name>"   - a dependency/manifest/config file that defines the DB stack
      "classify:<result>" - repo_indexer.classify_file returned model/migration
      "db-path:<segment>" - a DB-related directory segment in the path
    """
    if not isinstance(path, str):
        return None
    normalized = _normalize(path)
    if not normalized:
        return None

    basename = normalized.rsplit("/", 1)[-1]
    if basename in _DB_MANIFEST_BASENAMES:
        return f"manifest:{basename}"

    # classify_file is pure (string-only). Guard anyway so this helper never throws.
    try:
        classification = classify_file(normalized)
    except Exception:
        classification = "unknown"
    if classification in _DB_CLASSIFICATIONS:
        return f"classify:{classification}"

    segments = [segment for segment in normalized.split("/") if segment]
    for segment in segments:
        if segment in _DB_DIR_SEGMENTS:
            return f"db-path:{segment}"

    return None


def is_db_sensitive_run(files_expected: list[str]) -> bool:
    """
    Return True only when the expected files strongly indicate DB/model/migration/
    dependency work. Uses files_expected only — never feature text or chunk titles.

    Defensive: returns False for empty/None/non-list input; non-string items are
    skipped. Pure: no I/O, no side effects.
    """
    if not files_expected or not isinstance(files_expected, (list, tuple)):
        return False
    return any(
        get_db_sensitivity_reason(path) is not None
        for path in files_expected
        if isinstance(path, str)
    )
