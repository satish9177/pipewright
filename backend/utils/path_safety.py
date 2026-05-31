"""
path_safety.py
Shared path validation for target repository reads and writes.

Two levels of forbidden-path checking are exported:

  is_forbidden_path        — read-path check (coder, indexer, scope guard).
                             Blocks secrets and .env files.

  is_forbidden_write_path  — write-path check (patch_applier only).
                             Strict superset: also blocks .git internals,
                             infrastructure files, and sensitive-substring
                             patterns (secrets, private).
"""

from pathlib import Path, PurePosixPath


FORBIDDEN_PATHS = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    "secrets.json",
    "credentials.json",
}

ALLOWED_ENV_SAMPLE_FILES = {
    ".env.example",
    ".env.sample",
}

# Additional names forbidden for write operations only.  Stored lowercase so
# they can be compared directly against the lowercased final path component.
_WRITE_FORBIDDEN_NAMES: frozenset[str] = frozenset({
    ".git",
    ".gitignore",
    "docker-compose.yml",
    "dockerfile",
    "requirements.txt",
    "package-lock.json",
})

# Substrings that, if present anywhere in the lowercased normalized path,
# mark it forbidden for writes.  Matches existing patch_applier.py behavior.
_WRITE_FORBIDDEN_PATTERNS: frozenset[str] = frozenset({".env", "secrets", "private"})


def normalize_relative_path(path: str) -> str:
    if path is None:
        raise RuntimeError("path_safety.py: path is required")
    normalized = path.replace("\\", "/").strip()
    if not normalized:
        raise RuntimeError("path_safety.py: empty path rejected")
    return str(PurePosixPath(normalized))


def is_forbidden_path(path: str) -> bool:
    """Read-path check. Blocks secrets and .env files."""
    normalized = normalize_relative_path(path).lower()
    parts = [part for part in normalized.split("/") if part]
    name = parts[-1] if parts else normalized
    if name in ALLOWED_ENV_SAMPLE_FILES:
        return False
    if name in FORBIDDEN_PATHS:
        return True
    return name.startswith(".env.")


def is_forbidden_write_path(path: str) -> bool:
    """
    Strict forbidden-path check for write operations (patch application).
    Superset of is_forbidden_path: covers all base read-path restrictions plus
    .git internals, infrastructure lockfiles, and sensitive-substring patterns.
    """
    if is_forbidden_path(path):
        return True

    normalized = normalize_relative_path(path).lower()
    parts = [part for part in normalized.split("/") if part]
    name = parts[-1] if parts else normalized

    # Block any path that has .git as a component (e.g. .git/config).
    if ".git" in parts:
        return True

    # Block by exact final path component name.
    if name in _WRITE_FORBIDDEN_NAMES:
        return True

    # Block by sensitive substrings anywhere in the path.
    for pattern in _WRITE_FORBIDDEN_PATTERNS:
        if pattern in normalized:
            return True

    return False


def validate_safe_relative_path(path: str, root: Path) -> Path:
    normalized = normalize_relative_path(path)
    candidate = Path(normalized)

    if candidate.is_absolute() or normalized.startswith("/"):
        raise RuntimeError(f"path_safety.py: absolute path rejected: {path}")
    if ".." in normalized.split("/"):
        raise RuntimeError(f"path_safety.py: path traversal rejected: {path}")
    if is_forbidden_path(normalized):
        raise RuntimeError(f"path_safety.py: forbidden path rejected: {path}")

    root_path = root.resolve()
    full_path = (root_path / normalized).resolve()
    try:
        full_path.relative_to(root_path)
    except ValueError as error:
        raise RuntimeError(f"path_safety.py: path traversal rejected: {path}") from error
    return full_path
