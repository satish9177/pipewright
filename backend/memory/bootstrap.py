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
from backend.repo.repo_fingerprint import (
    DB_MONGODB_MARKERS,
    DB_MYSQL_MARKERS,
    DB_POSTGRES_MARKERS,
    DB_SQLITE_MARKERS,
    IGNORED_DIRS,
    MAX_DEPTH,
    MAX_MANIFEST_FILES,
    content_has_any as _content_has_any,
    discover_manifest_files,
    load_repo_file,
)

logger = logging.getLogger(__name__)

ALLOWED_SUGGESTION_STATUSES = {"pending", "approved", "rejected", "archived"}
BOOTSTRAP_SOURCE = "bootstrap"

# Caps remain bootstrap-local module constants so existing tests can monkeypatch
# bootstrap.BOOTSTRAP_MAX_MANIFEST_FILES; their default values come from the
# shared repo_fingerprint module so there is one source of truth. The discovery
# wrapper below reads these at call time, preserving the monkeypatch behavior.
BOOTSTRAP_MAX_DEPTH = MAX_DEPTH
BOOTSTRAP_MAX_MANIFEST_FILES = MAX_MANIFEST_FILES

# Skip list and manifest sets are now owned by repo_fingerprint (single source of
# truth). Re-exported under the historical bootstrap name for compatibility.
BOOTSTRAP_IGNORED_DIRS = IGNORED_DIRS


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
    return load_repo_file(root, relative_path)


def _discover_manifest_files(root: Path) -> list[str]:
    # Reads bootstrap's module-level caps at call time so existing tests that
    # monkeypatch BOOTSTRAP_MAX_MANIFEST_FILES continue to take effect.
    return discover_manifest_files(
        root,
        ignored_dirs=BOOTSTRAP_IGNORED_DIRS,
        max_depth=BOOTSTRAP_MAX_DEPTH,
        max_files=BOOTSTRAP_MAX_MANIFEST_FILES,
    )


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


def _package_script_contains(package_json: str, script_name: str, fragment: str) -> bool:
    try:
        data = json.loads(package_json)
    except json.JSONDecodeError:
        return False
    scripts = data.get("scripts") or {}
    return fragment.lower() in str(scripts.get(script_name, "")).lower()


def _scope_from_path(path: str, default: str = "backend") -> str:
    parts = {part.lower() for part in path.split("/") if part}
    if parts & {"frontend", "web", "client", "ui"}:
        return "frontend"
    if parts & {"tests", "test", "spec"}:
        return "tests"
    if parts & {"infra", "deploy", "docker"}:
        return "infra"
    if parts & {"backend", "api", "server", "service", "services"}:
        return "backend"
    return default


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
        for relative_path in _discover_manifest_files(root)
        if (content := _load_repo_file(root, relative_path)) is not None
    }
    lowered = _lower_files(files)
    candidates: list[CandidateSuggestion] = []

    for path, content in lowered.items():
        if path.endswith(("requirements.txt", "pyproject.toml", "Pipfile", "setup.py")):
            if "fastapi" in content or "uvicorn" in content:
                _add_candidate(
                    candidates,
                    "Backend uses FastAPI.",
                    "stack",
                    "backend",
                    path,
                    "Detected FastAPI dependency.",
                    priority=80,
                )
            if "django" in content:
                _add_candidate(
                    candidates,
                    "Backend uses Django.",
                    "stack",
                    "backend",
                    path,
                    "Detected Django dependency.",
                    priority=80,
                )
            if "flask" in content:
                _add_candidate(
                    candidates,
                    "Backend uses Flask.",
                    "stack",
                    "backend",
                    path,
                    "Detected Flask dependency.",
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
            if _content_has_any(content, DB_POSTGRES_MARKERS):
                _add_candidate(
                    candidates,
                    "Project uses PostgreSQL.",
                    "db",
                    "backend",
                    path,
                    "Detected PostgreSQL dependency or reference.",
                    priority=100,
                )
            if _content_has_any(content, DB_MYSQL_MARKERS):
                _add_candidate(
                    candidates,
                    "Project uses MySQL.",
                    "db",
                    "backend",
                    path,
                    "Detected MySQL dependency or reference.",
                    priority=100,
                )
            if _content_has_any(content, DB_MONGODB_MARKERS):
                _add_candidate(
                    candidates,
                    "Project uses MongoDB.",
                    "db",
                    "backend",
                    path,
                    "Detected MongoDB dependency or reference.",
                    priority=100,
                )

    if _python_311_detected(files):
        evidence_path = next(
            (
                path for path, content in lowered.items()
                if "3.11" in content
            ),
            None,
        )
        _add_candidate(
            candidates,
            "Backend uses Python 3.11.",
            "stack",
            "backend",
            evidence_path,
            "Detected Python 3.11 runtime hint.",
            priority=90,
        )

    for path, package_json in files.items():
        if not path.endswith("package.json"):
            continue
        uses_react = _package_uses(package_json, "react")
        uses_vite = _package_uses(package_json, "vite")
        uses_typescript = _package_uses(package_json, "typescript")
        uses_next = _package_uses(package_json, "next")
        uses_express = _package_uses(package_json, "express")
        uses_nest = (
            _package_uses(package_json, "@nestjs/core")
            or "@nestjs/core" in package_json.lower()
            or "nestjs" in package_json.lower()
        )
        uses_fastify = _package_uses(package_json, "fastify")
        uses_prisma = _package_uses(package_json, "prisma") or _package_uses(package_json, "@prisma/client")
        uses_mongoose = _package_uses(package_json, "mongoose") or "mongodb" in package_json.lower()
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
        elif uses_react and uses_vite:
            _add_candidate(
                candidates,
                "Frontend uses React and Vite.",
                "stack",
                "frontend",
                path,
                "Detected React and Vite dependencies.",
                priority=90,
            )
        elif uses_next:
            _add_candidate(
                candidates,
                "Frontend uses Next.js.",
                "stack",
                "frontend",
                path,
                "Detected Next.js dependency.",
                priority=80,
            )
        if uses_express:
            _add_candidate(
                candidates,
                "Backend uses Express.",
                "stack",
                "backend",
                path,
                "Detected Express dependency.",
                priority=80,
            )
        if uses_nest:
            _add_candidate(
                candidates,
                "Backend uses NestJS.",
                "stack",
                "backend",
                path,
                "Detected NestJS dependency.",
                priority=80,
            )
        if uses_fastify:
            _add_candidate(
                candidates,
                "Backend uses Fastify.",
                "stack",
                "backend",
                path,
                "Detected Fastify dependency.",
                priority=80,
            )
        if uses_prisma:
            _add_candidate(
                candidates,
                "Project uses Prisma for database access.",
                "db",
                _scope_from_path(path),
                path,
                "Detected Prisma dependency.",
                priority=100,
            )
        if uses_mongoose:
            _add_candidate(
                candidates,
                "Project uses MongoDB.",
                "db",
                "backend",
                path,
                "Detected MongoDB dependency.",
                priority=100,
            )
        if _package_has_script(package_json, "build") and (
            uses_vite or _package_script_contains(package_json, "build", "vite")
        ):
            _add_candidate(
                candidates,
                "Frontend build uses npm run build.",
                "test",
                "frontend",
                path,
                "Detected package.json build script.",
                priority=100,
            )
        if "jest" in package_json.lower():
            _add_candidate(
                candidates,
                "Frontend tests use Jest.",
                "test",
                "frontend",
                path,
                "Detected Jest dependency.",
                priority=120,
            )
        if "vitest" in package_json.lower():
            _add_candidate(
                candidates,
                "Frontend tests use Vitest.",
                "test",
                "frontend",
                path,
                "Detected Vitest dependency.",
                priority=120,
            )
        if _package_has_script(package_json, "test"):
            scope = "frontend" if uses_react or uses_vite or uses_next else "backend"
            _add_candidate(
                candidates,
                "Project defines npm test script.",
                "test",
                scope,
                path,
                "Detected package.json test script.",
                priority=130,
            )

    for path in files:
        if not path.endswith("pytest.ini"):
            continue
        _add_candidate(
            candidates,
            "Run backend unit tests with pytest.",
            "test",
            "tests",
            path,
            "Detected pytest.ini.",
            priority=90,
        )

    for path, content in lowered.items():
        if not path.endswith(("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle")):
            continue
        if "spring-boot" in content:
            _add_candidate(
                candidates,
                "Backend uses Spring Boot.",
                "stack",
                "backend",
                path,
                "Detected Spring Boot dependency.",
                priority=80,
            )
        if path.endswith("pom.xml") and "maven" in content:
            _add_candidate(
                candidates,
                "Java build uses Maven.",
                "deploy",
                "backend",
                path,
                "Detected Maven project file.",
                priority=140,
            )
        if path.endswith(("build.gradle", "build.gradle.kts", "settings.gradle")):
            _add_candidate(
                candidates,
                "Java build uses Gradle.",
                "deploy",
                "backend",
                path,
                "Detected Gradle build file.",
                priority=140,
            )
        if "junit" in content:
            _add_candidate(
                candidates,
                "Java tests use JUnit.",
                "test",
                "tests",
                path,
                "Detected JUnit dependency.",
                priority=130,
            )

    for path in files:
        if path.endswith("go.mod"):
            _add_candidate(
                candidates,
                "Backend includes a Go module.",
                "stack",
                "backend",
                path,
                "Detected go.mod.",
                priority=110,
            )
        if path.endswith("Cargo.toml"):
            _add_candidate(
                candidates,
                "Project includes a Rust component.",
                "stack",
                _scope_from_path(path, default="backend"),
                path,
                "Detected Cargo.toml.",
                priority=120,
            )

    sqlite_evidence = next(
        (
            path for path, content in lowered.items()
            if _content_has_any(content, DB_SQLITE_MARKERS) or path.endswith("schema.sql")
        ),
        None,
    )
    if sqlite_evidence:
        _add_candidate(
            candidates,
            "Current local database is SQLite.",
            "db",
            "backend",
            sqlite_evidence,
            "Detected SQLite schema or SQLite reference.",
            priority=80,
        )

    docker_evidence = next(
        (
            path for path in files
            if path.endswith(("Dockerfile", "docker-compose.yml", "docker-compose.yaml"))
        ),
        None,
    )
    if docker_evidence:
        _add_candidate(
            candidates,
            "Project includes Docker deployment configuration.",
            "deploy",
            "infra",
            docker_evidence,
            "Detected Docker configuration.",
            priority=130,
        )

    prisma_evidence = next((path for path in files if path.endswith("prisma/schema.prisma")), None)
    if prisma_evidence:
        _add_candidate(
            candidates,
            "Project uses Prisma schema configuration.",
            "db",
            _scope_from_path(prisma_evidence),
            prisma_evidence,
            "Detected Prisma schema.",
            priority=120,
        )

    alembic_evidence = next((path for path in files if path.endswith("alembic.ini")), None)
    if alembic_evidence or (root / "alembic").is_dir():
        _add_candidate(
            candidates,
            "Project uses Alembic database migrations.",
            "db",
            "backend",
            alembic_evidence or "alembic",
            "Detected Alembic configuration.",
            priority=120,
        )

    _add_candidate(
        candidates,
        "Never log secrets, API keys, tokens, or .env values.",
        "security",
        "global",
        "__bootstrap_default__",
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
