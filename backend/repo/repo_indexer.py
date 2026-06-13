"""
repo_indexer.py
Zero-AI repository indexing for deterministic file discovery.

This module scans a project repository on demand and stores lightweight file
metadata in SQLite. It does not call AI APIs, use embeddings, watch files, or
parse ASTs.

Staleness behavior:
  ensure_repo_indexed() only checks whether any index rows exist for the
  project_id. It does not detect changed, deleted, or newly added files yet.
  To force a fresh scan, call build_repo_index() directly.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import text

from backend.db.database import engine
from backend.utils.path_safety import is_forbidden_path

MAX_FILE_SIZE_BYTES = 1_000_000
MAX_IMPORTS_PER_FILE = 50

SKIP_NAMES = {
    ".git",
    "venv",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    "coverage",
    "target",
    ".idea",
    ".vscode",
    ".next",
    ".cache",
    ".mypy_cache",
    ".ruff_cache",
}

INDEX_FORBIDDEN_FILE_STEMS = {
    "credentials",
    "secrets",
}

SUPPORTED_EXTENSIONS = {
    ".cs",
    ".css",
    ".go",
    ".gradle",
    ".html",
    ".py",
    ".php",
    ".js",
    ".jsx",
    ".java",
    ".kt",
    ".kts",
    ".json",
    ".md",
    ".properties",
    ".rb",
    ".rs",
    ".sass",
    ".scala",
    ".scss",
    ".sql",
    ".svelte",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}


def _normalize_relative_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def should_skip_path(path: Path, *, repo_root: Path | None = None) -> bool:
    """
    Return True when a path should not be scanned or indexed.
    Skips generated folders, dependency folders, cache folders, and files > 1MB.
    """
    try:
        parts_for_skip = path.parts
        if repo_root is not None:
            try:
                parts_for_skip = path.resolve().relative_to(
                    repo_root.resolve()
                ).parts
            except ValueError:
                # A caller supplied a repo root, but this path is outside it.
                # Fail closed instead of applying repo-local skip rules to an
                # unrelated absolute path.
                return True

        lower_parts = [part.lower() for part in parts_for_skip]
        lower_name = path.name.lower()

        path_parts_to_check = lower_parts if path.is_dir() else lower_parts[:-1]
        if any(part in SKIP_NAMES for part in path_parts_to_check):
            return True

        if path.is_file() and lower_name in SKIP_NAMES:
            return True

        if path.is_file() and path.stat().st_size > MAX_FILE_SIZE_BYTES:
            return True

        if path.is_file() and is_forbidden_path(path.name):
            return True

        if path.is_file() and path.stem.lower() in INDEX_FORBIDDEN_FILE_STEMS:
            return True

        return False
    except Exception as error:
        print(f"[REPO_INDEXER] Warning: failed to inspect path {path}: {error}")
        return True


def is_supported_file(path: Path) -> bool:
    """
    Return True for deterministic text file types supported by Phase 2A.
    Binary files are skipped later when UTF-8 text reading fails.
    """
    try:
        name = path.name
        lower_name = name.lower()

        if name == "Dockerfile":
            return True
        if lower_name in {".env.example", ".env.sample"}:
            return True
        return path.suffix.lower() in SUPPORTED_EXTENSIONS
    except Exception as error:
        raise RuntimeError(
            f"repo_indexer.py: failed to check supported file {path}: {error}"
        )


def classify_file(relative_path: str) -> str:
    """
    Classify a file by deterministic path and filename rules.
    """
    try:
        normalized = relative_path.replace("\\", "/")
        lower_path = normalized.lower()
        name = Path(normalized).name.lower()
        parts = lower_path.split("/")
        suffix = Path(normalized).suffix.lower()

        if "test" in parts or "tests" in parts or name.startswith("test_"):
            return "test"
        if (
            "routes" in parts
            or "routers" in parts
            or "router" in parts
            or "api" in parts
        ):
            return "route"
        if "controllers" in parts or "controller" in lower_path:
            return "controller"
        if "services" in parts or "service" in lower_path:
            return "service"
        if "models" in parts:
            return "model"
        if "entities" in parts or "entity" in lower_path:
            return "entity"
        if "repositories" in parts or "repository" in lower_path:
            return "repository"
        if "migrations" in parts or "alembic" in parts or suffix == ".sql":
            return "migration"
        if (
            "config" in parts
            or "settings" in name
            or lower_path.endswith(".env.example")
            or name in {"docker-compose.yml", "docker-compose.yaml", "dockerfile"}
        ):
            return "config"
        if (
            "frontend" in parts
            or "components" in parts
            or "pages" in parts
            or suffix in {".tsx", ".jsx"}
        ):
            return "frontend"
        if "scripts" in parts or "script" in lower_path:
            return "script"
        if suffix == ".md":
            return "doc"
        return "unknown"
    except Exception as error:
        raise RuntimeError(
            f"repo_indexer.py: failed to classify file {relative_path}: {error}"
        )


def estimate_tokens(content: str) -> int:
    try:
        return max(1, len(content) // 4)
    except Exception as error:
        raise RuntimeError(f"repo_indexer.py: failed to estimate tokens: {error}")


def extract_imports(relative_path: str, content: str) -> list[str]:
    """
    Extract top-level imports using regex/string parsing only.
    """
    try:
        suffix = Path(relative_path).suffix.lower()
        imports: list[str] = []

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if suffix == ".py":
                if line.startswith("import ") or line.startswith("from "):
                    imports.append(line)
            elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
                if line.startswith("import ") or "require(" in line:
                    imports.append(line)
            elif suffix == ".java":
                if line.startswith("import "):
                    imports.append(line)

            if len(imports) >= MAX_IMPORTS_PER_FILE:
                break

        return imports
    except Exception as error:
        raise RuntimeError(
            f"repo_indexer.py: failed to extract imports from {relative_path}: "
            f"{error}"
        )


def scan_repo(target_repo_path: str) -> list[dict]:
    """
    Recursively scan a repository and return supported file metadata.
    """
    try:
        target_repo = Path(target_repo_path).resolve()
        if not target_repo.exists() or not target_repo.is_dir():
            raise RuntimeError(
                f"repo_indexer.py: target repo does not exist: {target_repo_path}"
            )

        print(f"[REPO_INDEXER] Scanning repo: {target_repo}")
        files: list[dict] = []

        for path in target_repo.rglob("*"):
            if should_skip_path(path, repo_root=target_repo):
                if path.is_dir():
                    continue
                continue
            if not path.is_file() or not is_supported_file(path):
                continue

            try:
                content = path.read_text(encoding="utf-8")
            except Exception as error:
                print(f"[REPO_INDEXER] Warning: skipping unreadable file {path}: {error}")
                continue

            if "\x00" in content:
                print(f"[REPO_INDEXER] Warning: skipping binary-like file {path}")
                continue

            try:
                stat = path.stat()
                relative_path = _normalize_relative_path(path.relative_to(target_repo))
                line_count = len(content.splitlines())
                files.append({
                    "id": str(uuid.uuid4()),
                    "path": relative_path,
                    "file_type": classify_file(relative_path),
                    "summary": None,
                    "key_imports": extract_imports(relative_path, content),
                    "last_modified": datetime.fromtimestamp(
                        stat.st_mtime,
                        tz=timezone.utc,
                    ).isoformat(),
                    "token_estimate": estimate_tokens(content),
                    "line_count": line_count,
                    "size_bytes": stat.st_size,
                })
            except Exception as error:
                print(f"[REPO_INDEXER] Warning: failed to index file {path}: {error}")
                continue

        print(f"[REPO_INDEXER] Scan complete | files={len(files)}")
        return files
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(f"repo_indexer.py: scan_repo failed: {error}")


def save_file_index(project_id: str, files: list[dict]) -> int:
    """
    Replace index rows for a project in one transaction.
    If any insert fails, the delete and all inserts are rolled back.
    """
    if not project_id or not project_id.strip():
        raise RuntimeError("repo_indexer.py: project_id is required")

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                DELETE FROM file_index
                WHERE project_id = :project_id
            """), {"project_id": project_id})

            for file_data in files:
                normalized_path = file_data["path"].replace("\\", "/")
                conn.execute(text("""
                    INSERT INTO file_index
                    (id, project_id, path, file_type, summary, key_imports,
                     last_modified, token_estimate, line_count, size_bytes)
                    VALUES
                    (:id, :project_id, :path, :file_type, :summary, :key_imports,
                     :last_modified, :token_estimate, :line_count, :size_bytes)
                """), {
                    "id": file_data.get("id") or str(uuid.uuid4()),
                    "project_id": project_id,
                    "path": normalized_path,
                    "file_type": file_data["file_type"],
                    "summary": file_data.get("summary"),
                    "key_imports": json.dumps(file_data.get("key_imports", [])),
                    "last_modified": file_data.get("last_modified"),
                    "token_estimate": file_data.get("token_estimate", 0),
                    "line_count": file_data.get("line_count", 0),
                    "size_bytes": file_data.get("size_bytes", 0),
                })

        print(
            f"[REPO_INDEXER] Index saved | "
            f"project_id={project_id} | files={len(files)}"
        )
        return len(files)
    except Exception as error:
        raise RuntimeError(
            f"repo_indexer.py: save_file_index failed. "
            f"project_id={project_id} | error={error}"
        )


def build_repo_index(project_id: str, target_repo_path: str) -> dict:
    """
    Build and save a fresh repository index for one project.
    """
    try:
        target_repo = Path(target_repo_path).resolve()
        if not target_repo.exists() or not target_repo.is_dir():
            raise RuntimeError(
                f"repo_indexer.py: target repo does not exist: {target_repo_path}"
            )

        files = scan_repo(str(target_repo))
        saved_count = save_file_index(project_id, files)
        return {
            "project_id": project_id,
            "target_repo_path": str(target_repo),
            "files_indexed": saved_count,
            "status": "complete",
        }
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"repo_indexer.py: build_repo_index failed. "
            f"project_id={project_id} | error={error}"
        )


def get_file_index_count() -> int:
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT COUNT(*) FROM file_index")).fetchone()
            return int(row[0])
    except Exception as error:
        raise RuntimeError(f"repo_indexer.py: get_file_index_count failed: {error}")


def get_project_index_status(project_id: str) -> dict:
    """
    Return read-only index status for one project. Does NOT scan the repo.

    Reads only the existing ``file_index`` rows for the project (COUNT(*) and
    MAX(indexed_at)); never touches the filesystem and never calls
    build_repo_index. ``indexed_at`` is the most recent index timestamp, or
    None when the project has no index rows. No schema change.
    """
    if not project_id or not project_id.strip():
        raise RuntimeError("repo_indexer.py: project_id is required")

    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT COUNT(*) AS files_indexed, MAX(indexed_at) AS indexed_at
                FROM file_index
                WHERE project_id = :project_id
            """), {"project_id": project_id}).fetchone()
    except Exception as error:
        raise RuntimeError(
            f"repo_indexer.py: get_project_index_status failed. "
            f"project_id={project_id} | error={error}"
        )

    files_indexed = int(row[0]) if row and row[0] is not None else 0
    indexed_at = row[1] if (row and files_indexed > 0) else None
    return {
        "project_id": project_id,
        "files_indexed": files_indexed,
        "indexed_at": indexed_at,
        "status": "indexed" if files_indexed > 0 else "not_indexed",
    }


def ensure_repo_indexed(project_id: str, target_repo_path: str) -> dict:
    """
    Ensure an index exists for project_id.

    Staleness behavior: this only checks whether at least one file_index row
    exists for the project. It does not detect changed files yet. To force a
    fresh scan, call build_repo_index(project_id, target_repo_path) directly.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT COUNT(*) FROM file_index
                WHERE project_id = :project_id
            """), {"project_id": project_id}).fetchone()
            count = int(row[0])

        if count == 0:
            return build_repo_index(project_id, target_repo_path)

        print(
            f"[REPO_INDEXER] Index already exists | "
            f"project_id={project_id} | files={count}"
        )
        return {
            "project_id": project_id,
            "status": "already_indexed",
            "files_indexed": count,
        }
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"repo_indexer.py: ensure_repo_indexed failed. "
            f"project_id={project_id} | error={error}"
        )


def _score_file(row: dict, keywords: list[str]) -> int:
    path = (row.get("path") or "").lower()
    file_type = (row.get("file_type") or "").lower()
    key_imports = (row.get("key_imports") or "").lower()

    score = 0
    for keyword in keywords:
        if keyword in path:
            score += 5
        if keyword in file_type:
            score += 3
        if keyword in key_imports:
            score += 1
    return score


def get_relevant_files(
    project_id: str,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """
    Return files with simple multi-keyword matching against path, type, imports.
    """
    try:
        keywords = [part.strip().lower() for part in query.split() if part.strip()]
        if not keywords:
            return []

        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT * FROM file_index
                WHERE project_id = :project_id
            """), {"project_id": project_id}).fetchall()

        scored: list[tuple[int, dict]] = []
        for row in rows:
            data = dict(row._mapping)
            score = _score_file(data, keywords)
            if score > 0:
                data["score"] = score
                scored.append((score, data))

        scored.sort(key=lambda item: (-item[0], item[1]["path"]))
        return [data for _, data in scored[:limit]]
    except Exception as error:
        raise RuntimeError(
            f"repo_indexer.py: get_relevant_files failed. "
            f"project_id={project_id} | error={error}"
        )
