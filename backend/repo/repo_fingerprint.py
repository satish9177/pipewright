"""
repo_fingerprint.py
Deterministic, zero-AI extraction of repository "reality" signals from capped
manifest/config files.

This module is the single source of truth for two things that used to live only
inside backend/memory/bootstrap.py:

  1. Safe manifest discovery and loading (depth/count/size caps, skip-list of
     heavy dirs, path-traversal safety via validate_safe_relative_path).
  2. Deterministic technology-signal detection vocabulary.

backend/memory/bootstrap.py now consumes this module so the detection vocabulary
and the safe-extraction logic are not duplicated (the same divergence risk PR
#15F fixed for forbidden paths).

M1.5 scope (this PR): DB engine signal only
(postgresql | mysql | mongodb | sqlite). This module performs NO conflict
detection, NO memory mutation, NO run blocking, NO prompt injection. It is pure
extraction. Those behaviors land in later M1.5 slices (#16C/#16D).

Safety guarantees:
  - Reads only capped manifest/config files (depth, count, and size caps).
  - Resolves every path through validate_safe_relative_path (traversal-safe).
  - Never reads .env. Only .env.example / .env.sample are read, and only the
    variable NAMES (left of '=') are used — values are never read or stored.
  - Evidence excerpts are fixed, human-written strings, never raw file content.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from backend.utils.path_safety import validate_safe_relative_path

# --- Caps and skip rules (shared defaults) ---------------------------------

MAX_DEPTH = 5
MAX_MANIFEST_FILES = 100
MAX_FILE_SIZE_BYTES = 200_000

IGNORED_DIRS = {
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "dist",
    "build",
    "target",
    "__pycache__",
    "coverage",
    "vendor",
    "generated",
    ".next",
    "out",
    "tmp",
    "logs",
    "examples",
    "example",
    "samples",
    "sample",
    "demo",
    "template",
    "templates",
    "fixtures",
}

MANIFEST_FILENAMES = {
    "package.json",
    "requirements.txt",
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
    "alembic.ini",
    "schema.sql",
}

SPECIAL_MANIFEST_SUFFIXES = {
    "prisma/schema.prisma",
}

ENV_EXAMPLE_FILENAMES = {".env.example", ".env.sample"}

# --- DB detection vocabulary (single source of truth) ----------------------
#
# These marker sets are exactly the sets bootstrap.py historically used inline,
# so bootstrap behavior is preserved when it imports them. The fingerprint's own
# scan augments them slightly (see _FP_DB_MARKERS) to also catch docker-compose
# service image names; that augmentation is fingerprint-only and never changes
# bootstrap output.

DB_POSTGRES_MARKERS = frozenset({"postgresql", "psycopg", "asyncpg"})
DB_MYSQL_MARKERS = frozenset({"mysql", "pymysql", "mysqlclient"})
DB_MONGODB_MARKERS = frozenset({"mongodb", "mongoose"})
DB_SQLITE_MARKERS = frozenset({"sqlite", "sqlite3"})

# Deterministic engine ordering for stable output.
DB_ENGINE_ORDER = ("postgresql", "mysql", "mongodb", "sqlite")

# Fingerprint-only scan markers: base markers plus image/short tokens for
# docker-compose service images (e.g. "postgres:16", "mongo:7").
_FP_DB_MARKERS = {
    "postgresql": set(DB_POSTGRES_MARKERS) | {"postgres"},
    "mysql": set(DB_MYSQL_MARKERS),
    "mongodb": set(DB_MONGODB_MARKERS) | {"mongo"},
    "sqlite": set(DB_SQLITE_MARKERS),
}

# Tokens checked against .env.example variable NAMES only (never values).
# Kept precise to avoid false positives in a weak hint source.
_DB_ENV_NAME_TOKENS = {
    "postgresql": ("postgres",),
    "mysql": ("mysql",),
    "mongodb": ("mongo",),
    "sqlite": ("sqlite",),
}

_DB_EVIDENCE = {
    "postgresql": "Detected PostgreSQL dependency or reference.",
    "mysql": "Detected MySQL dependency or reference.",
    "mongodb": "Detected MongoDB dependency or reference.",
    "sqlite": "Detected SQLite dependency or reference.",
}

_DB_ENV_EVIDENCE = {
    "postgresql": "Detected PostgreSQL environment variable name (value not read).",
    "mysql": "Detected MySQL environment variable name (value not read).",
    "mongodb": "Detected MongoDB environment variable name (value not read).",
    "sqlite": "Detected SQLite environment variable name (value not read).",
}

# --- Advisory repo-reality producer vocabulary -----------------------------
#
# PR-B scope: producer-only, advisory-only signals for the non-DB dimensions the
# memory trust layer already understands. The repo layer deliberately does not
# import backend.memory; tests assert these canonical values stay compatible.

REPO_REALITY_SIGNAL_DIMENSION_ORDER = (
    "backend_framework",
    "frontend_framework",
    "test_runner",
    "migration_tool",
    "package_manager",
)

DEFAULT_REPO_REALITY_SIGNAL_DIMENSIONS = frozenset(
    REPO_REALITY_SIGNAL_DIMENSION_ORDER
)

_PRODUCIBLE_REPO_REALITY_VALUES = {
    "backend_framework": frozenset({
        "fastapi",
        "django",
        "flask",
        "express",
        "nestjs",
        "fastify",
        "spring_boot",
    }),
    "frontend_framework": frozenset({
        "react",
        "vue",
        "angular",
        "svelte",
        "nextjs",
    }),
    "test_runner": frozenset({
        "pytest",
        "jest",
        "vitest",
        "mocha",
        "junit",
    }),
    "migration_tool": frozenset({
        "alembic",
        "prisma",
        "knex",
    }),
    "package_manager": frozenset({
        "npm",
        "yarn",
        "pnpm",
        "poetry",
        "pipenv",
        "cargo",
        "go_modules",
    }),
}

_REALITY_EVIDENCE = {
    "backend_framework": {
        "fastapi": "Detected FastAPI dependency.",
        "django": "Detected Django dependency.",
        "flask": "Detected Flask dependency.",
        "express": "Detected Express dependency.",
        "nestjs": "Detected NestJS dependency.",
        "fastify": "Detected Fastify dependency.",
        "spring_boot": "Detected Spring Boot dependency.",
    },
    "frontend_framework": {
        "react": "Detected React dependency.",
        "vue": "Detected Vue dependency.",
        "angular": "Detected Angular dependency.",
        "svelte": "Detected Svelte dependency.",
        "nextjs": "Detected Next.js dependency.",
    },
    "test_runner": {
        "pytest": "Detected pytest test runner.",
        "jest": "Detected Jest test runner.",
        "vitest": "Detected Vitest test runner.",
        "mocha": "Detected Mocha test runner.",
        "junit": "Detected JUnit test runner.",
    },
    "migration_tool": {
        "alembic": "Detected Alembic migration tool.",
        "prisma": "Detected Prisma migration tool.",
        "knex": "Detected Knex migration tool.",
    },
    "package_manager": {
        "npm": "Detected npm lockfile.",
        "yarn": "Detected Yarn lockfile.",
        "pnpm": "Detected pnpm lockfile.",
        "poetry": "Detected Poetry lockfile or project section.",
        "pipenv": "Detected Pipenv manifest.",
        "cargo": "Detected Cargo manifest.",
        "go_modules": "Detected Go modules manifest.",
    },
}

_PYTHON_MANIFEST_NAMES = frozenset({
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
    "setup.py",
})

_JVM_MANIFEST_NAMES = frozenset({
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
})

_PACKAGE_JSON_SECTIONS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)

_PACKAGE_BACKEND_DEPS = {
    "express": frozenset({"express"}),
    "nestjs": frozenset({"nestjs", "@nestjs/core", "@nestjs/common"}),
    "fastify": frozenset({"fastify"}),
}

_PACKAGE_FRONTEND_DEPS = {
    "react": frozenset({"react"}),
    "vue": frozenset({"vue"}),
    "angular": frozenset({"angular", "@angular/core"}),
    "svelte": frozenset({"svelte"}),
    "nextjs": frozenset({"next"}),
}

_PACKAGE_TEST_DEPS = {
    "jest": frozenset({"jest"}),
    "vitest": frozenset({"vitest"}),
    "mocha": frozenset({"mocha"}),
}

_PACKAGE_MIGRATION_DEPS = {
    "prisma": frozenset({"prisma", "@prisma/client"}),
    "knex": frozenset({"knex"}),
}

_ROOT_PACKAGE_MANAGER_PROBES = (
    ("yarn", "yarn.lock"),
    ("pnpm", "pnpm-lock.yaml"),
    ("npm", "package-lock.json"),
    ("poetry", "poetry.lock"),
)


# --- Data model ------------------------------------------------------------

@dataclass(frozen=True)
class RepoSignal:
    """One deterministic signal extracted from the repo."""
    category: str           # "db"
    value: str              # "postgresql" | "mysql" | "mongodb" | "sqlite"
    evidence_path: str | None
    evidence_excerpt: str   # fixed human string; never raw file content


@dataclass(frozen=True)
class RealitySignal:
    """One deterministic advisory repo-reality signal."""
    dimension: str
    value: str
    evidence_path: str | None
    evidence_excerpt: str   # fixed human string; never raw file content


@dataclass(frozen=True)
class RepoFingerprint:
    """
    Deterministic snapshot of repo reality. M1.5 db-only slice.

    db:           the single confident DB engine signal, or None when unknown or
                  ambiguous (multiple distinct engines detected).
    db_signals:   every distinct engine signal found (one per engine), in
                  DB_ENGINE_ORDER.
    db_ambiguous: True when more than one distinct engine was detected.
    """
    db: RepoSignal | None
    db_signals: tuple[RepoSignal, ...]
    db_ambiguous: bool


# --- Safe extraction helpers -----------------------------------------------

def content_has_any(content: str, needles) -> bool:
    """True if any needle is a substring of content. Callers lowercase first."""
    return any(needle in content for needle in needles)


def producible_repo_reality_signal_values() -> dict[str, frozenset[str]]:
    """Return the canonical values this module can emit for PR-B dimensions."""
    return dict(_PRODUCIBLE_REPO_REALITY_VALUES)


def relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def is_ignored_relative_path(relative_path: str, ignored_dirs=IGNORED_DIRS) -> bool:
    parts = {part.lower() for part in relative_path.split("/") if part}
    return bool(parts & ignored_dirs)


def is_manifest_path(relative_path: str) -> bool:
    name = relative_path.rsplit("/", 1)[-1]
    if name in MANIFEST_FILENAMES:
        return True
    return any(relative_path.endswith(suffix) for suffix in SPECIAL_MANIFEST_SUFFIXES)


def load_repo_file(
    root: Path,
    relative_path: str,
    *,
    max_size: int = MAX_FILE_SIZE_BYTES,
) -> str | None:
    """
    Safely read a repo file. Returns None for anything unsafe, missing, oversized,
    or unreadable. Path is resolved through validate_safe_relative_path, which
    rejects traversal and forbidden paths such as .env.
    """
    try:
        path = validate_safe_relative_path(relative_path, root)
    except RuntimeError:
        return None
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > max_size:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _iter_repo_files(
    root: Path,
    *,
    ignored_dirs=IGNORED_DIRS,
    max_depth: int = MAX_DEPTH,
):
    """
    Yield relative POSIX paths for files under root, pruning ignored/heavy dirs
    and respecting the depth cap. Shared by manifest discovery and .env.example
    name extraction so the walk logic exists in exactly one place.
    """
    root = root.resolve()
    for current_root, dirnames, filenames in os.walk(root):
        current_path = Path(current_root)
        relative_dir = current_path.relative_to(root)
        depth = 0 if str(relative_dir) == "." else len(relative_dir.parts)
        dirnames[:] = [
            dirname for dirname in dirnames
            if dirname.lower() not in ignored_dirs and depth < max_depth
        ]
        if depth > max_depth:
            continue
        for filename in sorted(filenames):
            relative_path = relative_posix(current_path / filename, root)
            if is_ignored_relative_path(relative_path, ignored_dirs):
                continue
            yield relative_path


def discover_manifest_files(
    root: Path,
    *,
    ignored_dirs=IGNORED_DIRS,
    max_depth: int = MAX_DEPTH,
    max_files: int = MAX_MANIFEST_FILES,
) -> list[str]:
    """Discover capped manifest/config files under root (relative POSIX paths)."""
    discovered: list[str] = []
    for relative_path in _iter_repo_files(
        root, ignored_dirs=ignored_dirs, max_depth=max_depth
    ):
        if not is_manifest_path(relative_path):
            continue
        discovered.append(relative_path)
        if len(discovered) >= max_files:
            break
    return discovered


def load_manifest_files(
    root: Path,
    *,
    ignored_dirs=IGNORED_DIRS,
    max_depth: int = MAX_DEPTH,
    max_files: int = MAX_MANIFEST_FILES,
) -> dict[str, str]:
    """Discover and load manifest files into {relative_posix_path: raw_text}."""
    files: dict[str, str] = {}
    for relative_path in discover_manifest_files(
        root, ignored_dirs=ignored_dirs, max_depth=max_depth, max_files=max_files
    ):
        content = load_repo_file(root, relative_path)
        if content is not None:
            files[relative_path] = content
    return files


def collect_env_example_var_names(
    root: Path,
    *,
    ignored_dirs=IGNORED_DIRS,
    max_depth: int = MAX_DEPTH,
    max_files: int = MAX_MANIFEST_FILES,
) -> list[tuple[str, str]]:
    """
    Read .env.example / .env.sample files and return (variable_name_lower,
    evidence_path) pairs. Only the NAME (left of '=') is read; values are never
    read or returned. .env itself is never opened (validate_safe_relative_path
    rejects it).
    """
    results: list[tuple[str, str]] = []
    seen_files = 0
    for relative_path in _iter_repo_files(
        root, ignored_dirs=ignored_dirs, max_depth=max_depth
    ):
        name = relative_path.rsplit("/", 1)[-1]
        if name not in ENV_EXAMPLE_FILENAMES:
            continue
        seen_files += 1
        if seen_files > max_files:
            break
        content = load_repo_file(root, relative_path)
        if content is None:
            continue
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            var_name = stripped.split("=", 1)[0].strip().lower()
            # 'export FOO=bar' -> drop the leading 'export ' keyword.
            if var_name.startswith("export "):
                var_name = var_name[len("export "):].strip()
            if var_name:
                results.append((var_name, relative_path))
    return results


# --- Advisory repo-reality signal detection --------------------------------

def _manifest_name(relative_path: str) -> str:
    return relative_path.rsplit("/", 1)[-1]


def _is_python_manifest(relative_path: str) -> bool:
    return _manifest_name(relative_path) in _PYTHON_MANIFEST_NAMES


def _is_jvm_manifest(relative_path: str) -> bool:
    return _manifest_name(relative_path) in _JVM_MANIFEST_NAMES


def _package_dependency_names(raw: str) -> frozenset[str]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return frozenset()
    if not isinstance(parsed, dict):
        return frozenset()

    names: set[str] = set()
    for section in _PACKAGE_JSON_SECTIONS:
        deps = parsed.get(section)
        if not isinstance(deps, dict):
            continue
        names.update(str(name).strip().lower() for name in deps if str(name).strip())
    return frozenset(names)


def _has_dependency(dependency_names: frozenset[str], markers: frozenset[str]) -> bool:
    return bool(dependency_names & markers)


def _root_file_exists(root: Path, relative_path: str) -> bool:
    try:
        path = validate_safe_relative_path(relative_path, root)
    except RuntimeError:
        return False
    return path.is_file()


def _add_reality_hit(
    hits: dict[str, dict[str, RealitySignal]],
    *,
    dimension: str,
    value: str,
    evidence_path: str | None,
) -> None:
    if dimension not in _PRODUCIBLE_REPO_REALITY_VALUES:
        return
    if value not in _PRODUCIBLE_REPO_REALITY_VALUES[dimension]:
        return
    by_value = hits.setdefault(dimension, {})
    if value in by_value:
        return
    by_value[value] = RealitySignal(
        dimension=dimension,
        value=value,
        evidence_path=evidence_path,
        evidence_excerpt=_REALITY_EVIDENCE[dimension][value],
    )


def _detect_python_manifest_reality(
    hits: dict[str, dict[str, RealitySignal]],
    path: str,
    content: str,
) -> None:
    if not _is_python_manifest(path):
        return
    lowered = content.lower()
    for value, markers in (
        ("fastapi", ("fastapi",)),
        ("django", ("django",)),
        ("flask", ("flask",)),
    ):
        if content_has_any(lowered, markers):
            _add_reality_hit(
                hits,
                dimension="backend_framework",
                value=value,
                evidence_path=path,
            )
    if "pytest" in lowered:
        _add_reality_hit(
            hits,
            dimension="test_runner",
            value="pytest",
            evidence_path=path,
        )
    if "[tool.poetry]" in lowered:
        _add_reality_hit(
            hits,
            dimension="package_manager",
            value="poetry",
            evidence_path=path,
        )


def _detect_package_json_reality(
    hits: dict[str, dict[str, RealitySignal]],
    path: str,
    content: str,
) -> None:
    if _manifest_name(path) != "package.json":
        return
    dependencies = _package_dependency_names(content)
    if not dependencies:
        return

    for value, markers in _PACKAGE_BACKEND_DEPS.items():
        if _has_dependency(dependencies, markers):
            _add_reality_hit(
                hits,
                dimension="backend_framework",
                value=value,
                evidence_path=path,
            )
    for value, markers in _PACKAGE_FRONTEND_DEPS.items():
        if _has_dependency(dependencies, markers):
            _add_reality_hit(
                hits,
                dimension="frontend_framework",
                value=value,
                evidence_path=path,
            )
    for value, markers in _PACKAGE_TEST_DEPS.items():
        if _has_dependency(dependencies, markers):
            _add_reality_hit(
                hits,
                dimension="test_runner",
                value=value,
                evidence_path=path,
            )
    for value, markers in _PACKAGE_MIGRATION_DEPS.items():
        if _has_dependency(dependencies, markers):
            _add_reality_hit(
                hits,
                dimension="migration_tool",
                value=value,
                evidence_path=path,
            )


def _detect_jvm_reality(
    hits: dict[str, dict[str, RealitySignal]],
    path: str,
    content: str,
) -> None:
    if not _is_jvm_manifest(path):
        return
    lowered = content.lower()
    if content_has_any(lowered, ("spring-boot", "spring boot", "springframework.boot")):
        _add_reality_hit(
            hits,
            dimension="backend_framework",
            value="spring_boot",
            evidence_path=path,
        )
    if "junit" in lowered:
        _add_reality_hit(
            hits,
            dimension="test_runner",
            value="junit",
            evidence_path=path,
        )


def _detect_named_manifest_reality(
    hits: dict[str, dict[str, RealitySignal]],
    path: str,
) -> None:
    name = _manifest_name(path)
    if name == "pytest.ini":
        _add_reality_hit(
            hits,
            dimension="test_runner",
            value="pytest",
            evidence_path=path,
        )
    elif name in {"jest.config.js", "jest.config.ts"}:
        _add_reality_hit(
            hits,
            dimension="test_runner",
            value="jest",
            evidence_path=path,
        )
    elif name in {"vitest.config.js", "vitest.config.ts"}:
        _add_reality_hit(
            hits,
            dimension="test_runner",
            value="vitest",
            evidence_path=path,
        )
    elif name == "alembic.ini":
        _add_reality_hit(
            hits,
            dimension="migration_tool",
            value="alembic",
            evidence_path=path,
        )
    elif name == "Pipfile":
        _add_reality_hit(
            hits,
            dimension="package_manager",
            value="pipenv",
            evidence_path=path,
        )
    elif name == "Cargo.toml":
        _add_reality_hit(
            hits,
            dimension="package_manager",
            value="cargo",
            evidence_path=path,
        )
    elif name == "go.mod":
        _add_reality_hit(
            hits,
            dimension="package_manager",
            value="go_modules",
            evidence_path=path,
        )

    if path.endswith("prisma/schema.prisma"):
        _add_reality_hit(
            hits,
            dimension="migration_tool",
            value="prisma",
            evidence_path=path,
        )


def _detect_root_probe_reality(
    hits: dict[str, dict[str, RealitySignal]],
    root: Path,
) -> None:
    for value, evidence_path in _ROOT_PACKAGE_MANAGER_PROBES:
        if _root_file_exists(root, evidence_path):
            _add_reality_hit(
                hits,
                dimension="package_manager",
                value=value,
                evidence_path=evidence_path,
            )
    if (root / "alembic").is_dir():
        _add_reality_hit(
            hits,
            dimension="migration_tool",
            value="alembic",
            evidence_path="alembic",
        )


def _collapse_reality_hits(
    hits: dict[str, dict[str, RealitySignal]],
    dimensions: frozenset[str],
) -> dict[str, RealitySignal]:
    result: dict[str, RealitySignal] = {}
    for dimension in REPO_REALITY_SIGNAL_DIMENSION_ORDER:
        if dimension not in dimensions:
            continue
        by_value = hits.get(dimension) or {}
        if len(by_value) == 1:
            result[dimension] = next(iter(by_value.values()))
    return result


def collect_repo_reality_signals(
    repo_path,
    *,
    dimensions: frozenset[str] = DEFAULT_REPO_REALITY_SIGNAL_DIMENSIONS,
) -> dict[str, RealitySignal]:
    """
    Build deterministic advisory reality signals for non-DB dimensions.

    Returns one confident ``RealitySignal`` per requested dimension. Unknown,
    weak, or ambiguous dimensions are omitted. Any unexpected failure degrades to
    an empty result so the read-only analysis endpoint never fails because a
    repo signal could not be produced.
    """
    requested = frozenset(dimensions or ())
    requested &= DEFAULT_REPO_REALITY_SIGNAL_DIMENSIONS
    if not requested:
        return {}

    try:
        root = Path(repo_path).resolve()
    except (OSError, ValueError):
        return {}
    if not root.exists() or not root.is_dir():
        return {}

    try:
        files = load_manifest_files(root)
        hits: dict[str, dict[str, RealitySignal]] = {}

        for path, content in files.items():
            _detect_named_manifest_reality(hits, path)
            _detect_python_manifest_reality(hits, path, content)
            _detect_package_json_reality(hits, path, content)
            _detect_jvm_reality(hits, path, content)

        if "package_manager" in requested or "migration_tool" in requested:
            _detect_root_probe_reality(hits, root)

        return _collapse_reality_hits(hits, requested)
    except Exception:
        return {}


# --- DB signal detection ---------------------------------------------------

def detect_db_signals(
    files: dict[str, str],
    *,
    env_signals: list[tuple[str, str]] | None = None,
) -> list[RepoSignal]:
    """
    Detect database ENGINE signals from already-loaded manifest content and (weakly)
    from .env.example variable names. Returns at most one RepoSignal per distinct
    engine, attributed to the first file that evidenced it, in DB_ENGINE_ORDER.

    files: {relative_posix_path: raw_text}. Content is lowercased internally.
    env_signals: (variable_name_lower, evidence_path) pairs from .env.example.
    """
    hits: dict[str, RepoSignal] = {}

    for path, raw in files.items():
        content = raw.lower()
        for engine in DB_ENGINE_ORDER:
            if engine in hits:
                continue
            if content_has_any(content, _FP_DB_MARKERS[engine]):
                hits[engine] = RepoSignal(
                    category="db",
                    value=engine,
                    evidence_path=path,
                    evidence_excerpt=_DB_EVIDENCE[engine],
                )

    for var_name, evidence_path in (env_signals or []):
        for engine in DB_ENGINE_ORDER:
            if engine in hits:
                continue
            if any(token in var_name for token in _DB_ENV_NAME_TOKENS[engine]):
                hits[engine] = RepoSignal(
                    category="db",
                    value=engine,
                    evidence_path=evidence_path,
                    evidence_excerpt=_DB_ENV_EVIDENCE[engine],
                )

    return [hits[engine] for engine in DB_ENGINE_ORDER if engine in hits]


def build_repo_fingerprint(repo_path) -> RepoFingerprint:
    """
    Build a deterministic DB-only fingerprint for a repository.

    Returns an empty fingerprint (db=None, no signals) when the path is missing,
    not a directory, or yields no DB signal. Multiple distinct engines are
    reported as ambiguous (db=None, db_ambiguous=True) — never as a conflict.
    """
    empty = RepoFingerprint(db=None, db_signals=(), db_ambiguous=False)
    try:
        root = Path(repo_path).resolve()
    except (OSError, ValueError):
        return empty
    if not root.exists() or not root.is_dir():
        return empty

    files = load_manifest_files(root)
    env_signals = collect_env_example_var_names(root)
    signals = detect_db_signals(files, env_signals=env_signals)

    if not signals:
        return empty
    if len(signals) > 1:
        return RepoFingerprint(db=None, db_signals=tuple(signals), db_ambiguous=True)
    return RepoFingerprint(db=signals[0], db_signals=(signals[0],), db_ambiguous=False)
