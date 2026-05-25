"""
path_safety.py
Shared path validation for target repository reads and writes.
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


def normalize_relative_path(path: str) -> str:
    if path is None:
        raise RuntimeError("path_safety.py: path is required")
    normalized = path.replace("\\", "/").strip()
    if not normalized:
        raise RuntimeError("path_safety.py: empty path rejected")
    return str(PurePosixPath(normalized))


def is_forbidden_path(path: str) -> bool:
    normalized = normalize_relative_path(path).lower()
    parts = [part for part in normalized.split("/") if part]
    name = parts[-1] if parts else normalized
    if name in ALLOWED_ENV_SAMPLE_FILES:
        return False
    if name in FORBIDDEN_PATHS:
        return True
    return name.startswith(".env.")


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
