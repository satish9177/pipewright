"""
risk_scanner.py
Deterministic high-risk gate for chunk plans.

Pure module. No DB, no LLM, no FS reads. Scans each chunk's title,
description, and files_expected for high-risk patterns. Can only upgrade
risk_level to "high" (and force requires_human_review = True). Never
downgrades an AI-marked high-risk chunk. On scan error, fail safe and
treat the affected chunk as high risk.
"""

from __future__ import annotations

import logging

from backend.models.chunk import ChunkDefinition, TriageResult

logger = logging.getLogger(__name__)

HIGH_RISK_KEYWORDS: tuple[str, ...] = (
    # Migration / schema
    "alembic",
    "migration",
    "migrate",
    "schema",
    "alter table",
    "add column",
    "drop column",
    "create table",
    # Auth / security
    "auth",
    "jwt",
    "oauth",
    "token",
    "session",
    "permission",
    "role",
    "rbac",
    "login",
    "password",
    "credential",
    "acl",
    # Secrets / env / encryption
    "secret",
    "api_key",
    "api key",
    "encryption",
    "encrypt",
    "decrypt",
    "fernet",
    ".env",
    "private key",
    "certificate",
    # Git / rollback / checkpoint / resume
    "rollback",
    "checkpoint",
    "resume",
    "git",
    "branch",
    "commit",
    "push",
    "pr_orchestrator",
    "local_git",
    "checkpoint_store",
    # Public API routes
    "routes",
    "router",
    "endpoint",
    "@app.post",
    "@app.get",
    "@router.post",
    "@router.get",
    # CI / workflow / deployment
    ".github/workflows",
    "github actions",
    "dockerfile",
    "docker-compose",
    "deploy",
    " ci ",
    " cd ",
    # Dependencies / lockfiles
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "pom.xml",
    "cargo.lock",
    "go.sum",
)

HIGH_RISK_PATH_PREFIXES: tuple[str, ...] = (
    "backend/routes/",
    ".github/workflows/",
)


def _normalize_for_match(value: str) -> str:
    return value.replace("\\", "/").lower()


def chunk_is_high_risk(chunk: ChunkDefinition) -> bool:
    """
    Return True if any high-risk pattern matches the chunk's title,
    description, or files_expected, or if files_expected is empty.
    """
    if not chunk.files_expected:
        return True

    haystack_parts = [
        _normalize_for_match(chunk.title or ""),
        _normalize_for_match(chunk.description or ""),
    ]
    for path in chunk.files_expected:
        haystack_parts.append(_normalize_for_match(path or ""))
    # Pad title/description so " ci " / " cd " word boundaries match.
    haystack = " " + "\n".join(haystack_parts) + " "

    for keyword in HIGH_RISK_KEYWORDS:
        if keyword in haystack:
            return True

    for path in chunk.files_expected:
        normalized = _normalize_for_match(path or "")
        for prefix in HIGH_RISK_PATH_PREFIXES:
            if normalized.startswith(prefix):
                return True

    return False


def _force_high(chunk: ChunkDefinition) -> ChunkDefinition:
    return chunk.model_copy(update={
        "risk_level": "high",
        "requires_human_review": True,
    })


def scan_triage_result(triage: TriageResult) -> TriageResult:
    """
    Return a new TriageResult with high-risk chunks force-upgraded.

    - Upgrade-only: never downgrades risk.
    - Existing high-risk chunks are passed through unchanged.
    - Per-chunk exception fails safe by forcing the chunk to high.
    """
    upgraded: list[ChunkDefinition] = []
    for chunk in triage.chunks:
        if chunk.risk_level == "high":
            upgraded.append(chunk)
            continue
        try:
            if chunk_is_high_risk(chunk):
                upgraded.append(_force_high(chunk))
                continue
        except Exception as error:
            logger.warning(
                "risk_scanner.py: scan failed for chunk %s; "
                "forcing high risk. error=%s",
                getattr(chunk, "chunk_number", "?"),
                error,
            )
            upgraded.append(_force_high(chunk))
            continue
        upgraded.append(chunk)

    return triage.model_copy(update={"chunks": upgraded})
