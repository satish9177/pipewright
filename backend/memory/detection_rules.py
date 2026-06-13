"""
detection_rules.py
Pure detection rules and evaluator for bootstrap memory candidates.

This module has no DB, network, LLM, or clock dependency. It evaluates the
already-discovered repo files in the same order bootstrap.py historically used.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from backend.memory.bootstrap import CandidateSuggestion
from backend.repo.repo_fingerprint import (
    DB_MONGODB_MARKERS,
    DB_MYSQL_MARKERS,
    DB_POSTGRES_MARKERS,
    DB_SQLITE_MARKERS,
    content_has_any as _content_has_any,
)

PYTHON_MANIFEST_SUFFIXES = ("requirements.txt", "pyproject.toml", "Pipfile", "setup.py")
JVM_BUILD_SUFFIXES = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle")
GRADLE_SUFFIXES = ("build.gradle", "build.gradle.kts", "settings.gradle")


@dataclass(frozen=True)
class CandidateTemplate:
    content: str
    category: str
    scope: str
    evidence_excerpt: str
    priority: int


@dataclass(frozen=True)
class ContentRule:
    markers: tuple[str, ...] | frozenset[str]
    candidate: CandidateTemplate


@dataclass(frozen=True)
class PathRule:
    suffix: str
    candidate: CandidateTemplate
    scope_from_path: bool = False


@dataclass(frozen=True)
class FirstEvidenceRule:
    kind: Literal["sqlite", "docker", "prisma_schema"]
    candidate: CandidateTemplate


PYTHON_MANIFEST_RULES = (
    ContentRule(
        markers=("fastapi", "uvicorn"),
        candidate=CandidateTemplate(
            content="Backend uses FastAPI.",
            category="stack",
            scope="backend",
            evidence_excerpt="Detected FastAPI dependency.",
            priority=80,
        ),
    ),
    ContentRule(
        markers=("django",),
        candidate=CandidateTemplate(
            content="Backend uses Django.",
            category="stack",
            scope="backend",
            evidence_excerpt="Detected Django dependency.",
            priority=80,
        ),
    ),
    ContentRule(
        markers=("flask",),
        candidate=CandidateTemplate(
            content="Backend uses Flask.",
            category="stack",
            scope="backend",
            evidence_excerpt="Detected Flask dependency.",
            priority=80,
        ),
    ),
    ContentRule(
        markers=("sqlalchemy",),
        candidate=CandidateTemplate(
            content="Backend uses SQLAlchemy for database access.",
            category="db",
            scope="backend",
            evidence_excerpt="Detected SQLAlchemy dependency.",
            priority=80,
        ),
    ),
    ContentRule(
        markers=("pytest",),
        candidate=CandidateTemplate(
            content="Run backend unit tests with pytest.",
            category="test",
            scope="tests",
            evidence_excerpt="Detected pytest dependency.",
            priority=90,
        ),
    ),
    ContentRule(
        markers=DB_POSTGRES_MARKERS,
        candidate=CandidateTemplate(
            content="Project uses PostgreSQL.",
            category="db",
            scope="backend",
            evidence_excerpt="Detected PostgreSQL dependency or reference.",
            priority=100,
        ),
    ),
    ContentRule(
        markers=DB_MYSQL_MARKERS,
        candidate=CandidateTemplate(
            content="Project uses MySQL.",
            category="db",
            scope="backend",
            evidence_excerpt="Detected MySQL dependency or reference.",
            priority=100,
        ),
    ),
    ContentRule(
        markers=DB_MONGODB_MARKERS,
        candidate=CandidateTemplate(
            content="Project uses MongoDB.",
            category="db",
            scope="backend",
            evidence_excerpt="Detected MongoDB dependency or reference.",
            priority=100,
        ),
    ),
)

PYTEST_INI_RULE = PathRule(
    suffix="pytest.ini",
    candidate=CandidateTemplate(
        content="Run backend unit tests with pytest.",
        category="test",
        scope="tests",
        evidence_excerpt="Detected pytest.ini.",
        priority=90,
    ),
)

JVM_SPRING_BOOT = CandidateTemplate(
    content="Backend uses Spring Boot.",
    category="stack",
    scope="backend",
    evidence_excerpt="Detected Spring Boot dependency.",
    priority=80,
)
JVM_MAVEN = CandidateTemplate(
    content="Java build uses Maven.",
    category="deploy",
    scope="backend",
    evidence_excerpt="Detected Maven project file.",
    priority=140,
)
JVM_GRADLE = CandidateTemplate(
    content="Java build uses Gradle.",
    category="deploy",
    scope="backend",
    evidence_excerpt="Detected Gradle build file.",
    priority=140,
)
JVM_JUNIT = CandidateTemplate(
    content="Java tests use JUnit.",
    category="test",
    scope="tests",
    evidence_excerpt="Detected JUnit dependency.",
    priority=130,
)

GO_RUST_RULES = (
    PathRule(
        suffix="go.mod",
        candidate=CandidateTemplate(
            content="Backend includes a Go module.",
            category="stack",
            scope="backend",
            evidence_excerpt="Detected go.mod.",
            priority=110,
        ),
    ),
    PathRule(
        suffix="Cargo.toml",
        candidate=CandidateTemplate(
            content="Project includes a Rust component.",
            category="stack",
            scope="backend",
            evidence_excerpt="Detected Cargo.toml.",
            priority=120,
        ),
        scope_from_path=True,
    ),
)

FIRST_EVIDENCE_RULES = (
    FirstEvidenceRule(
        kind="sqlite",
        candidate=CandidateTemplate(
            content="Current local database is SQLite.",
            category="db",
            scope="backend",
            evidence_excerpt="Detected SQLite schema or SQLite reference.",
            priority=80,
        ),
    ),
    FirstEvidenceRule(
        kind="docker",
        candidate=CandidateTemplate(
            content="Project includes Docker deployment configuration.",
            category="deploy",
            scope="infra",
            evidence_excerpt="Detected Docker configuration.",
            priority=130,
        ),
    ),
    FirstEvidenceRule(
        kind="prisma_schema",
        candidate=CandidateTemplate(
            content="Project uses Prisma schema configuration.",
            category="db",
            scope="backend",
            evidence_excerpt="Detected Prisma schema.",
            priority=120,
        ),
    ),
)

ALEMBIC_RULE = CandidateTemplate(
    content="Project uses Alembic database migrations.",
    category="db",
    scope="backend",
    evidence_excerpt="Detected Alembic configuration.",
    priority=120,
)

BOOTSTRAP_DEFAULT_RULE = CandidateTemplate(
    content="Never log secrets, API keys, tokens, or .env values.",
    category="security",
    scope="global",
    evidence_excerpt="Default bootstrap safety rule.",
    priority=0,
)

PATCH_APPLIER_RULE = CandidateTemplate(
    content=(
        "Coder output is applied through the patch applier; models should not "
        "write directly to disk."
    ),
    category="architecture",
    scope="backend",
    evidence_excerpt="Detected Pipewright patch applier.",
    priority=70,
)


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
    template: CandidateTemplate,
    evidence_path: str | None,
    *,
    scope: str | None = None,
) -> None:
    candidates.append(CandidateSuggestion(
        content=template.content,
        category=template.category,
        scope=scope or template.scope,
        priority=template.priority,
        evidence_path=evidence_path,
        evidence_excerpt=template.evidence_excerpt,
    ))


def _emit_python_manifest_candidates(
    candidates: list[CandidateSuggestion],
    lowered: dict[str, str],
) -> None:
    # Current behavior is file-outer/rule-inner over discovery-ordered files.
    for path, content in lowered.items():
        if not path.endswith(PYTHON_MANIFEST_SUFFIXES):
            continue
        for rule in PYTHON_MANIFEST_RULES:
            if _content_has_any(content, rule.markers):
                _add_candidate(candidates, rule.candidate, path)


def _emit_python_311_candidate(
    candidates: list[CandidateSuggestion],
    files: dict[str, str],
    lowered: dict[str, str],
) -> None:
    if not _python_311_detected(files):
        return
    evidence_path = next(
        (
            path for path, content in lowered.items()
            if "3.11" in content
        ),
        None,
    )
    _add_candidate(
        candidates,
        CandidateTemplate(
            content="Backend uses Python 3.11.",
            category="stack",
            scope="backend",
            evidence_excerpt="Detected Python 3.11 runtime hint.",
            priority=90,
        ),
        evidence_path,
    )


def _emit_package_json_candidates(
    candidates: list[CandidateSuggestion],
    files: dict[str, str],
) -> None:
    # Current behavior is file-outer over raw discovery-ordered files.
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
        uses_prisma = (
            _package_uses(package_json, "prisma")
            or _package_uses(package_json, "@prisma/client")
        )
        uses_mongoose = (
            _package_uses(package_json, "mongoose")
            or "mongodb" in package_json.lower()
        )
        if uses_react and uses_vite and uses_typescript:
            _add_candidate(
                candidates,
                CandidateTemplate(
                    content="Frontend uses React, Vite, and TypeScript.",
                    category="stack",
                    scope="frontend",
                    evidence_excerpt=(
                        "Detected React, Vite, and TypeScript dependencies."
                    ),
                    priority=80,
                ),
                path,
            )
        elif uses_react and uses_vite:
            _add_candidate(
                candidates,
                CandidateTemplate(
                    content="Frontend uses React and Vite.",
                    category="stack",
                    scope="frontend",
                    evidence_excerpt="Detected React and Vite dependencies.",
                    priority=90,
                ),
                path,
            )
        elif uses_next:
            _add_candidate(
                candidates,
                CandidateTemplate(
                    content="Frontend uses Next.js.",
                    category="stack",
                    scope="frontend",
                    evidence_excerpt="Detected Next.js dependency.",
                    priority=80,
                ),
                path,
            )
        if uses_express:
            _add_candidate(
                candidates,
                CandidateTemplate(
                    content="Backend uses Express.",
                    category="stack",
                    scope="backend",
                    evidence_excerpt="Detected Express dependency.",
                    priority=80,
                ),
                path,
            )
        if uses_nest:
            _add_candidate(
                candidates,
                CandidateTemplate(
                    content="Backend uses NestJS.",
                    category="stack",
                    scope="backend",
                    evidence_excerpt="Detected NestJS dependency.",
                    priority=80,
                ),
                path,
            )
        if uses_fastify:
            _add_candidate(
                candidates,
                CandidateTemplate(
                    content="Backend uses Fastify.",
                    category="stack",
                    scope="backend",
                    evidence_excerpt="Detected Fastify dependency.",
                    priority=80,
                ),
                path,
            )
        if uses_prisma:
            _add_candidate(
                candidates,
                CandidateTemplate(
                    content="Project uses Prisma for database access.",
                    category="db",
                    scope="backend",
                    evidence_excerpt="Detected Prisma dependency.",
                    priority=100,
                ),
                path,
                scope=_scope_from_path(path),
            )
        if uses_mongoose:
            _add_candidate(
                candidates,
                CandidateTemplate(
                    content="Project uses MongoDB.",
                    category="db",
                    scope="backend",
                    evidence_excerpt="Detected MongoDB dependency.",
                    priority=100,
                ),
                path,
            )
        if _package_has_script(package_json, "build") and (
            uses_vite or _package_script_contains(package_json, "build", "vite")
        ):
            _add_candidate(
                candidates,
                CandidateTemplate(
                    content="Frontend build uses npm run build.",
                    category="test",
                    scope="frontend",
                    evidence_excerpt="Detected package.json build script.",
                    priority=100,
                ),
                path,
            )
        if "jest" in package_json.lower():
            _add_candidate(
                candidates,
                CandidateTemplate(
                    content="Frontend tests use Jest.",
                    category="test",
                    scope="frontend",
                    evidence_excerpt="Detected Jest dependency.",
                    priority=120,
                ),
                path,
            )
        if "vitest" in package_json.lower():
            _add_candidate(
                candidates,
                CandidateTemplate(
                    content="Frontend tests use Vitest.",
                    category="test",
                    scope="frontend",
                    evidence_excerpt="Detected Vitest dependency.",
                    priority=120,
                ),
                path,
            )
        if _package_has_script(package_json, "test"):
            scope = "frontend" if uses_react or uses_vite or uses_next else "backend"
            _add_candidate(
                candidates,
                CandidateTemplate(
                    content="Project defines npm test script.",
                    category="test",
                    scope="backend",
                    evidence_excerpt="Detected package.json test script.",
                    priority=130,
                ),
                path,
                scope=scope,
            )


def _emit_pytest_ini_candidates(
    candidates: list[CandidateSuggestion],
    files: dict[str, str],
) -> None:
    for path in files:
        if not path.endswith(PYTEST_INI_RULE.suffix):
            continue
        _add_candidate(candidates, PYTEST_INI_RULE.candidate, path)


def _emit_jvm_candidates(
    candidates: list[CandidateSuggestion],
    lowered: dict[str, str],
) -> None:
    # Current behavior is file-outer/rule-inner over discovery-ordered files.
    for path, content in lowered.items():
        if not path.endswith(JVM_BUILD_SUFFIXES):
            continue
        if "spring-boot" in content:
            _add_candidate(candidates, JVM_SPRING_BOOT, path)
        if path.endswith("pom.xml") and "maven" in content:
            _add_candidate(candidates, JVM_MAVEN, path)
        if path.endswith(GRADLE_SUFFIXES):
            _add_candidate(candidates, JVM_GRADLE, path)
        if "junit" in content:
            _add_candidate(candidates, JVM_JUNIT, path)


def _emit_go_rust_candidates(
    candidates: list[CandidateSuggestion],
    files: dict[str, str],
) -> None:
    for path in files:
        for rule in GO_RUST_RULES:
            if not path.endswith(rule.suffix):
                continue
            scope = (
                _scope_from_path(path, default="backend")
                if rule.scope_from_path
                else None
            )
            _add_candidate(candidates, rule.candidate, path, scope=scope)


def _first_evidence(
    rule: FirstEvidenceRule,
    files: dict[str, str],
    lowered: dict[str, str],
) -> str | None:
    if rule.kind == "sqlite":
        return next(
            (
                path for path, content in lowered.items()
                if _content_has_any(content, DB_SQLITE_MARKERS)
                or path.endswith("schema.sql")
            ),
            None,
        )
    if rule.kind == "docker":
        return next(
            (
                path for path in files
                if path.endswith(("Dockerfile", "docker-compose.yml", "docker-compose.yaml"))
            ),
            None,
        )
    if rule.kind == "prisma_schema":
        return next((path for path in files if path.endswith("prisma/schema.prisma")), None)
    raise AssertionError(f"unknown first-evidence rule: {rule.kind}")


def _emit_first_evidence_candidates(
    candidates: list[CandidateSuggestion],
    files: dict[str, str],
    lowered: dict[str, str],
) -> None:
    for rule in FIRST_EVIDENCE_RULES:
        evidence_path = _first_evidence(rule, files, lowered)
        if not evidence_path:
            continue
        scope = (
            _scope_from_path(evidence_path)
            if rule.kind == "prisma_schema"
            else None
        )
        _add_candidate(candidates, rule.candidate, evidence_path, scope=scope)


def _emit_alembic_candidate(
    candidates: list[CandidateSuggestion],
    root: Path,
    files: dict[str, str],
) -> None:
    alembic_evidence = next((path for path in files if path.endswith("alembic.ini")), None)
    if alembic_evidence or (root / "alembic").is_dir():
        _add_candidate(candidates, ALEMBIC_RULE, alembic_evidence or "alembic")


def _emit_bootstrap_default(candidates: list[CandidateSuggestion]) -> None:
    _add_candidate(candidates, BOOTSTRAP_DEFAULT_RULE, "__bootstrap_default__")


def _emit_patch_applier_candidate(
    candidates: list[CandidateSuggestion],
    root: Path,
) -> None:
    if (root / "backend" / "pipeline" / "patch_applier.py").is_file():
        _add_candidate(
            candidates,
            PATCH_APPLIER_RULE,
            "backend/pipeline/patch_applier.py",
        )


def collect_detection_candidates(
    *,
    root: Path,
    files: dict[str, str],
    lowered: dict[str, str],
) -> list[CandidateSuggestion]:
    """
    Evaluate bootstrap candidates from pre-discovered files.

    The input contract is intentionally narrow: bootstrap owns filesystem
    discovery/loading/caps, while this pure evaluator owns candidate emission.
    """
    candidates: list[CandidateSuggestion] = []
    _emit_python_manifest_candidates(candidates, lowered)
    _emit_python_311_candidate(candidates, files, lowered)
    _emit_package_json_candidates(candidates, files)
    _emit_pytest_ini_candidates(candidates, files)
    _emit_jvm_candidates(candidates, lowered)
    _emit_go_rust_candidates(candidates, files)
    _emit_first_evidence_candidates(candidates, files, lowered)
    _emit_alembic_candidate(candidates, root, files)
    _emit_bootstrap_default(candidates)
    _emit_patch_applier_candidate(candidates, root)
    return candidates
