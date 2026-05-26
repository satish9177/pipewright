"""
bootstrap.py
Deterministic project memory bootstrap suggestions.

Suggestions are proposals only. They become active memory only after a human
approves them through the Memory API.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.db.database import engine
from backend.memory.memory_store import (
    DEFAULT_PRIORITY,
    add_fact,
    compute_content_hash,
    validate_memory_content,
)
from backend.projects.project_store import get_project
from backend.utils.path_safety import validate_safe_relative_path

logger = logging.getLogger(__name__)

ALLOWED_SUGGESTION_STATUSES = {"pending", "approved", "rejected", "archived"}
BOOTSTRAP_SOURCE = "bootstrap"

CONFIG_FILES = [
    "package.json",
    "frontend/package.json",
    "requirements.txt",
    "backend/requirements.txt",
    "pyproject.toml",
    "Pipfile",
    "setup.py",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "go.mod",
    "Cargo.toml",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "pytest.ini",
    "jest.config.js",
    "jest.config.ts",
    "vitest.config.js",
    "vitest.config.ts",
    "tsconfig.json",
    "vite.config.js",
    "vite.config.ts",
    "next.config.js",
    "next.config.ts",
    "prisma/schema.prisma",
    "alembic.ini",
    "backend/db/schema.sql",
]


@dataclass(frozen=True)
class CandidateSuggestion:
    content: str
    category: str
    scope: str
    priority: int = DEFAULT_PRIORITY
    evidence_path: str | None = None
    evidence_excerpt: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    return dict(row._mapping)


def _sanitize_suggestion(row: dict) -> dict:
    return {
        key: value
        for key, value in row.items()
        if key != "content_hash"
    }


def _load_repo_file(root: Path, relative_path: str) -> str | None:
    try:
        path = validate_safe_relative_path(relative_path, root)
    except RuntimeError:
        return None
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > 200_000:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _lower_files(files: dict[str, str]) -> dict[str, str]:
    return {path: content.lower() for path, content in files.items()}


def _package_uses(package_json: str, package_name: str) -> bool:
    try:
        data = json.loads(package_json)
    except json.JSONDecodeError:
        return package_name.lower() in package_json.lower()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        deps = data.get(key) or {}
        if package_name in deps:
            return True
    return False


def _package_has_script(package_json: str, script_name: str) -> bool:
    try:
        data = json.loads(package_json)
    except json.JSONDecodeError:
        return False
    scripts = data.get("scripts") or {}
    return script_name in scripts


def _python_311_detected(files: dict[str, str]) -> bool:
    patterns = [
        r"python\s*=\s*[\"']\^?3\.11",
        r"requires-python\s*=\s*[\"'][^\"']*3\.11",
        r"python:3\.11",
        r"python_version\s*==\s*[\"']3\.11",
    ]
    combined = "\n".join(files.values()).lower()
    return any(re.search(pattern, combined) for pattern in patterns)


def _add_candidate(
    candidates: list[CandidateSuggestion],
    content: str,
    category: str,
    scope: str,
    evidence_path: str | None,
    evidence_excerpt: str,
    priority: int = DEFAULT_PRIORITY,
) -> None:
    candidates.append(CandidateSuggestion(
        content=content,
        category=category,
        scope=scope,
        priority=priority,
        evidence_path=evidence_path,
        evidence_excerpt=evidence_excerpt,
    ))


def _collect_candidates(root: Path) -> list[CandidateSuggestion]:
    files = {
        relative_path: content
        for relative_path in CONFIG_FILES
        if (content := _load_repo_file(root, relative_path)) is not None
    }
    lowered = _lower_files(files)
    candidates: list[CandidateSuggestion] = []

    for path, content in lowered.items():
        if path.endswith("requirements.txt") or path in {"pyproject.toml", "Pipfile", "setup.py"}:
            if "fastapi" in content:
                _add_candidate(
                    candidates,
                    "Backend uses FastAPI.",
                    "stack",
                    "backend",
                    path,
                    "Detected FastAPI dependency.",
                    priority=80,
                )
            if "sqlalchemy" in content:
                _add_candidate(
                    candidates,
                    "Backend uses SQLAlchemy for database access.",
                    "db",
                    "backend",
                    path,
                    "Detected SQLAlchemy dependency.",
                    priority=80,
                )
            if "pytest" in content:
                _add_candidate(
                    candidates,
                    "Run backend unit tests with pytest.",
                    "test",
                    "tests",
                    path,
                    "Detected pytest dependency.",
                    priority=90,
                )

    if _python_311_detected(files):
        _add_candidate(
            candidates,
            "Backend uses Python 3.11.",
            "stack",
            "backend",
            "pyproject.toml",
            "Detected Python 3.11 runtime hint.",
            priority=90,
        )

    for path in ("package.json", "frontend/package.json"):
        package_json = files.get(path)
        if not package_json:
            continue
        uses_react = _package_uses(package_json, "react")
        uses_vite = _package_uses(package_json, "vite")
        uses_typescript = _package_uses(package_json, "typescript")
        if uses_react and uses_vite and uses_typescript:
            _add_candidate(
                candidates,
                "Frontend uses React, Vite, and TypeScript.",
                "stack",
                "frontend",
                path,
                "Detected React, Vite, and TypeScript dependencies.",
                priority=80,
            )
        if _package_has_script(package_json, "build"):
            _add_candidate(
                candidates,
                "Frontend build uses npm run build.",
                "test",
                "frontend",
                path,
                "Detected package.json build script.",
                priority=100,
            )
        if "jest" in package_json.lower() or "vitest" in package_json.lower():
            _add_candidate(
                candidates,
                "Frontend tests use a JavaScript test runner.",
                "test",
                "frontend",
                path,
                "Detected frontend test dependency.",
                priority=120,
            )

    if "pytest.ini" in files:
        _add_candidate(
            candidates,
            "Run backend unit tests with pytest.",
            "test",
            "tests",
            "pytest.ini",
            "Detected pytest.ini.",
            priority=90,
        )

    if "backend/db/schema.sql" in files or "sqlite" in "\n".join(lowered.values()):
        _add_candidate(
            candidates,
            "Current local database is SQLite.",
            "db",
            "backend",
            "backend/db/schema.sql" if "backend/db/schema.sql" in files else None,
            "Detected SQLite schema or SQLite reference.",
            priority=80,
        )

    if "Dockerfile" in files or "docker-compose.yml" in files or "docker-compose.yaml" in files:
        _add_candidate(
            candidates,
            "Project includes Docker deployment configuration.",
            "deploy",
            "infra",
            "Dockerfile" if "Dockerfile" in files else "docker-compose.yml",
            "Detected Docker configuration.",
            priority=130,
        )

    if "prisma/schema.prisma" in files:
        _add_candidate(
            candidates,
            "Project uses Prisma schema configuration.",
            "db",
            "backend",
            "prisma/schema.prisma",
            "Detected Prisma schema.",
            priority=120,
        )

    if "alembic.ini" in files or (root / "alembic").is_dir():
        _add_candidate(
            candidates,
            "Project uses Alembic database migrations.",
            "db",
            "backend",
            "alembic.ini",
            "Detected Alembic configuration.",
            priority=120,
        )

    _add_candidate(
        candidates,
        "Never log secrets, API keys, tokens, or .env values.",
        "security",
        "global",
        None,
        "Default bootstrap safety rule.",
        priority=0,
    )

    if (root / "backend" / "pipeline" / "patch_applier.py").is_file():
        _add_candidate(
            candidates,
            "Coder output is applied through the patch applier; models should not write directly to disk.",
            "architecture",
            "backend",
            "backend/pipeline/patch_applier.py",
            "Detected Pipewright patch applier.",
            priority=70,
        )

    return candidates


def _active_memory_exists(project_id: str, content_hash: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id FROM memory_facts
            WHERE project_id = :project_id
              AND content_hash = :content_hash
              AND status = 'active'
            LIMIT 1
        """), {
            "project_id": project_id,
            "content_hash": content_hash,
        }).fetchone()
    return row is not None


def _pending_suggestion_exists(project_id: str, content_hash: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id FROM memory_suggestions
            WHERE project_id = :project_id
              AND content_hash = :content_hash
              AND status = 'pending'
            LIMIT 1
        """), {
            "project_id": project_id,
            "content_hash": content_hash,
        }).fetchone()
    return row is not None


def _get_suggestion(project_id: str, suggestion_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id, project_id, content, category, scope, priority, source,
                   evidence_path, evidence_excerpt, status, created_at,
                   updated_at, approved_by, approved_at, rejected_by,
                   rejected_at, rejection_reason, content_hash
            FROM memory_suggestions
            WHERE project_id = :project_id
              AND id = :suggestion_id
        """), {
            "project_id": project_id,
            "suggestion_id": suggestion_id,
        }).fetchone()
    return _row_to_dict(row)


def _insert_suggestion(project_id: str, candidate: CandidateSuggestion) -> dict:
    content = validate_memory_content(candidate.content)
    content_hash = compute_content_hash(content)
    suggestion_id = str(uuid.uuid4())
    now = _utc_now()
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO memory_suggestions
            (id, project_id, content, category, scope, priority, source,
             evidence_path, evidence_excerpt, status, created_at, updated_at,
             content_hash)
            VALUES
            (:id, :project_id, :content, :category, :scope, :priority,
             :source, :evidence_path, :evidence_excerpt, 'pending', :now,
             :now, :content_hash)
        """), {
            "id": suggestion_id,
            "project_id": project_id,
            "content": content,
            "category": candidate.category,
            "scope": candidate.scope,
            "priority": candidate.priority,
            "source": BOOTSTRAP_SOURCE,
            "evidence_path": candidate.evidence_path,
            "evidence_excerpt": candidate.evidence_excerpt,
            "now": now,
            "content_hash": content_hash,
        })
        conn.commit()
    return _sanitize_suggestion(_get_suggestion(project_id, suggestion_id) or {})


def generate_bootstrap_suggestions(project_id: str, force: bool = False) -> list[dict]:
    project = get_project(project_id)
    if project is None:
        raise ValueError("bootstrap.py: project not found")

    repo_path = project.get("repo_path")
    if not repo_path:
        raise RuntimeError("bootstrap.py: project repo_path is missing")
    root = Path(repo_path).resolve()
    if not root.exists() or not root.is_dir():
        raise RuntimeError("bootstrap.py: project repo_path is invalid")

    created: list[dict] = []
    seen_hashes: set[str] = set()
    for candidate in _collect_candidates(root):
        try:
            content = validate_memory_content(candidate.content)
            content_hash = compute_content_hash(content)
        except ValueError:
            logger.warning("bootstrap.py: skipped unsafe memory suggestion")
            continue
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)
        if _active_memory_exists(project_id, content_hash):
            continue
        if _pending_suggestion_exists(project_id, content_hash):
            continue
        try:
            created.append(_insert_suggestion(project_id, candidate))
        except IntegrityError:
            if force:
                continue
            continue
    return created


def list_suggestions(project_id: str, status: str | None = None) -> list[dict]:
    if status is not None and status not in ALLOWED_SUGGESTION_STATUSES:
        raise ValueError("bootstrap.py: invalid suggestion status")

    filters = ["project_id = :project_id"]
    params = {"project_id": project_id}
    if status is not None:
        filters.append("status = :status")
        params["status"] = status

    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT id, project_id, content, category, scope, priority, source,
                   evidence_path, evidence_excerpt, status, created_at,
                   updated_at, approved_by, approved_at, rejected_by,
                   rejected_at, rejection_reason, content_hash
            FROM memory_suggestions
            WHERE {" AND ".join(filters)}
            ORDER BY created_at DESC
        """), params).fetchall()
    return [_sanitize_suggestion(dict(row._mapping)) for row in rows]


def approve_suggestion(
    project_id: str,
    suggestion_id: str,
    approved_by: str = "api",
) -> dict:
    suggestion = _get_suggestion(project_id, suggestion_id)
    if suggestion is None:
        raise ValueError("bootstrap.py: suggestion not found")
    if suggestion["status"] != "pending":
        raise ValueError("bootstrap.py: suggestion is not pending")
    if _active_memory_exists(project_id, suggestion["content_hash"]):
        raise ValueError("bootstrap.py: active duplicate memory fact already exists")

    fact = add_fact(
        project_id=project_id,
        content=suggestion["content"],
        category=suggestion["category"],
        scope=suggestion["scope"],
        priority=suggestion["priority"],
        source=BOOTSTRAP_SOURCE,
        added_by="api",
        approved_by=approved_by,
    )

    now = _utc_now()
    with engine.connect() as conn:
        conn.execute(text("""
            UPDATE memory_suggestions
            SET status = 'approved',
                approved_by = :approved_by,
                approved_at = :now,
                updated_at = :now
            WHERE project_id = :project_id
              AND id = :suggestion_id
        """), {
            "project_id": project_id,
            "suggestion_id": suggestion_id,
            "approved_by": approved_by,
            "now": now,
        })
        conn.commit()

    return {
        "suggestion": _sanitize_suggestion(_get_suggestion(project_id, suggestion_id) or {}),
        "fact": {
            key: value
            for key, value in fact.items()
            if key != "content_hash"
        },
    }


def reject_suggestion(
    project_id: str,
    suggestion_id: str,
    reason: str,
    rejected_by: str = "api",
) -> dict:
    reason_value = (reason or "").strip()
    if len(reason_value) < 4:
        raise ValueError("bootstrap.py: rejection reason is required")
    suggestion = _get_suggestion(project_id, suggestion_id)
    if suggestion is None:
        raise ValueError("bootstrap.py: suggestion not found")
    if suggestion["status"] != "pending":
        raise ValueError("bootstrap.py: suggestion is not pending")

    now = _utc_now()
    with engine.connect() as conn:
        conn.execute(text("""
            UPDATE memory_suggestions
            SET status = 'rejected',
                rejected_by = :rejected_by,
                rejected_at = :now,
                rejection_reason = :reason,
                updated_at = :now
            WHERE project_id = :project_id
              AND id = :suggestion_id
        """), {
            "project_id": project_id,
            "suggestion_id": suggestion_id,
            "rejected_by": rejected_by,
            "reason": reason_value,
            "now": now,
        })
        conn.commit()

    return _sanitize_suggestion(_get_suggestion(project_id, suggestion_id) or {})
