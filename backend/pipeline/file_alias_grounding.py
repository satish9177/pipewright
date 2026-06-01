"""
file_alias_grounding.py
Deterministic resolution of the explicit edit target a user named (PR #17B).

Purpose
-------
A simple edit request like "add hello in the readme" or "append text to
docs/usage.md" names a concrete file target. This module resolves that target
against the project's repo index so the orchestrator can later (PR #17C) give a
*specific* clarification — "create README.md?" / "which README?" — instead of a
generic "too vague" rejection. PR #17B is the helper only: it is not yet wired
into any route and changes no runtime behavior.

Scope (intentionally narrow)
----------------------------
Only two kinds of target are resolved:
  1. an explicit relative path the user typed (``docs/usage.md``, ``README.md``);
  2. a small fixed set of literal-file aliases (``readme``, ``package json``,
     ``docker compose``, ``requirements``, ``pyproject``).
Fuzzy / entity aliases ("user model", "login button", "auth service") are NOT
resolved here — they return ``NO_TARGET``. There is no fuzzy matching, no LLM,
no filesystem walk, and no file-content reading. Identical inputs + identical
index always produce an identical result.

Index source
------------
The only index access is ``plan_path_grounding.get_indexed_paths_and_dirs``,
which reads the existing ``file_index`` rows for the project and already fails
safe to an empty index. No new DB query, no scan.

Forbidden targets
-----------------
A target is forbidden when ``is_forbidden_path`` matches (``.env*``,
``secrets.json``, ``credentials.json``) OR any path component is ``.git``. This
is deliberately the *read-level* check plus ``.git`` — NOT
``is_forbidden_write_path``. Using the write-level check here would refuse
legitimately-editable infra files the user explicitly named
(``docker-compose.yml``, ``requirements.txt``), which this helper must ground.
The stricter write-time guard in ``patch_applier`` is unchanged and still
applies whenever an edit is actually written. Do not "tighten" this to the
write-level check — it would silently break docker-compose / requirements
grounding.

Note: explicit paths are matched EXACTLY (case-sensitive) against the index.
Case-insensitive convenience is provided only through the alias branch
(e.g. "readme").
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum

from backend.pipeline.plan_path_grounding import get_indexed_paths_and_dirs
from backend.repo.repo_indexer import SUPPORTED_EXTENSIONS
from backend.utils.path_safety import is_forbidden_path, is_forbidden_write_path


class EditTargetOutcome(str, Enum):
    """The kind of resolution produced for a referenced edit target."""

    GROUNDED = "grounded"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    NO_TARGET = "no_target"
    FORBIDDEN = "forbidden"
    CREATE_TARGET = "create_target"


@dataclass(frozen=True)
class EditTargetResolution:
    """
    Outcome of :func:`resolve_explicit_edit_target`.

    - ``GROUNDED``  -> ``path`` is the single indexed file the request targets.
    - ``NOT_FOUND`` -> ``alias`` is the referenced path/alias label; nothing in
                       the index matches it.
    - ``AMBIGUOUS`` -> ``alias`` is the referenced alias label; ``candidates``
                       holds the matching indexed paths (sorted).
    - ``FORBIDDEN`` -> ``alias`` is the referenced forbidden path/alias label.
    - ``CREATE_TARGET`` -> ``path`` is a missing safe docs/text path that the
                           user explicitly asked to create.
    - ``NO_TARGET`` -> no explicit path or known alias was referenced.
    """

    outcome: EditTargetOutcome
    path: str | None = None
    alias: str | None = None
    candidates: tuple[str, ...] = ()

    @classmethod
    def grounded(cls, path: str) -> "EditTargetResolution":
        return cls(outcome=EditTargetOutcome.GROUNDED, path=path)

    @classmethod
    def not_found(cls, alias: str) -> "EditTargetResolution":
        return cls(outcome=EditTargetOutcome.NOT_FOUND, alias=alias)

    @classmethod
    def ambiguous(cls, alias: str, paths) -> "EditTargetResolution":
        return cls(
            outcome=EditTargetOutcome.AMBIGUOUS,
            alias=alias,
            candidates=tuple(sorted(paths)),
        )

    @classmethod
    def forbidden(cls, alias: str) -> "EditTargetResolution":
        return cls(outcome=EditTargetOutcome.FORBIDDEN, alias=alias)

    @classmethod
    def create_target(cls, path: str) -> "EditTargetResolution":
        return cls(outcome=EditTargetOutcome.CREATE_TARGET, path=path)

    @classmethod
    def no_target(cls) -> "EditTargetResolution":
        return cls(outcome=EditTargetOutcome.NO_TARGET)


# Sensitive basenames that mark an explicit-path token as a real file reference
# even when it has no "supported" extension (so ".env" is detected and refused
# rather than ignored). ".env.*" is handled separately by prefix.
_SENSITIVE_BASENAMES = {".env", "secrets.json", "credentials.json"}

# Known file extensions (with leading dot, lowercase) that mark a token as an
# explicit file path. Reused from the indexer so the two stay in lockstep.
_KNOWN_EXTENSIONS = set(SUPPORTED_EXTENSIONS)

# First-slice create support is deliberately narrow: safe documentation/text
# files only, and only when the user explicitly says to create the missing file.
_CREATE_EXTENSIONS = {".md", ".txt"}
_CREATE_WORD_RE = re.compile(r"\bcreate\b")

# Punctuation stripped from the edges of a raw token before inspection. The
# trailing "." is handled separately (repeated strip) so a sentence-final period
# on "README.md." is removed without touching the internal extension dot.
_EDGE_PUNCT = "\"'`(),;:!?[]{}<>"


def _strip_token(raw: str) -> str:
    """
    Strip surrounding quotes/brackets/punctuation and trailing periods. A
    leading "." is preserved so dotfiles like ``.env`` survive intact.
    """
    token = raw.strip().strip(_EDGE_PUNCT)
    # Remove trailing sentence punctuation (including a final period) without
    # disturbing an internal extension dot or a leading dotfile dot.
    while token and token[-1] in _EDGE_PUNCT + ".":
        token = token[:-1]
    return token


def _normalize_path_token(raw: str) -> str | None:
    """
    Normalize a raw token to a candidate relative path, or None if it is not a
    safe groundable path (empty, absolute, or contains parent traversal).
    """
    token = _strip_token(raw).replace("\\", "/")
    if not token:
        return None
    while token.startswith("./"):
        token = token[2:]
    if token.startswith("/"):
        token = token.lstrip("/")
    if not token:
        return None
    parts = [p for p in token.split("/") if p]
    if ".." in parts:
        return None
    return "/".join(parts)


def _looks_like_explicit_path(norm: str) -> bool:
    """
    True when a normalized token is a deliberate file reference: it has a known
    extension, sits under/at a ``.git`` component, or is a sensitive dotfile.
    A bare "/"-token like ``and/or`` or ``TCP/IP`` is NOT a path candidate.
    """
    parts = [p for p in norm.split("/") if p]
    name = parts[-1] if parts else norm
    lower_name = name.lower()
    ext = os.path.splitext(lower_name)[1]
    if ext in _KNOWN_EXTENSIONS:
        return True
    if ".git" in parts:
        return True
    if lower_name in _SENSITIVE_BASENAMES or lower_name.startswith(".env."):
        return True
    return False


def _is_forbidden_target(norm_path: str) -> bool:
    """
    Forbidden = read-level secret/.env check OR any ``.git`` path component.
    Deliberately NOT the write-level check (see module docstring). Fails safe.
    """
    try:
        if is_forbidden_path(norm_path):
            return True
    except Exception:
        return True
    return ".git" in [p for p in norm_path.split("/") if p]


def _has_explicit_create_intent(feature_description: str) -> bool:
    """Return True only for an explicit ``create`` directive."""
    return bool(_CREATE_WORD_RE.search(_normalize_text(feature_description)))


def _is_allowed_create_target(norm_path: str, indexed_dirs: set[str]) -> bool:
    """First-slice create policy: safe missing .md/.txt with known parent."""
    if _is_forbidden_target(norm_path):
        return False
    try:
        if is_forbidden_write_path(norm_path):
            return False
    except Exception:
        return False

    ext = os.path.splitext(norm_path.lower())[1]
    if ext not in _CREATE_EXTENSIONS:
        return False

    if "/" not in norm_path:
        return True

    parent = norm_path.rsplit("/", 1)[0]
    return parent in indexed_dirs


def _basename(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else path


# --------------------------------------------------------------------------
# Literal-file alias table
# --------------------------------------------------------------------------
# Each alias has:
#   - label:   human-readable referenced target (used by NOT_FOUND/AMBIGUOUS).
#   - trigger: a callable(words, normalized_text) -> bool detecting the alias.
#   - matcher: a callable(lower_basename) -> bool selecting indexed files.
# Checked in this fixed order; the first triggered alias is the one resolved.


def _word_trigger(word: str):
    return lambda words, _text: word in words


def _phrase_trigger(phrase: str):
    return lambda _words, text: phrase in text


_DOCKER_COMPOSE_NAMES = {
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
}


@dataclass(frozen=True)
class _Alias:
    label: str
    trigger: object  # callable(words: set[str], text: str) -> bool
    matcher: object  # callable(lower_basename: str) -> bool


def _requirements_matcher(name: str) -> bool:
    return name.startswith("requirements") and name.endswith(".txt")


_ALIASES: tuple[_Alias, ...] = (
    _Alias("readme", _word_trigger("readme"),
           lambda name: name.startswith("readme")),
    _Alias("package.json", _phrase_trigger("package json"),
           lambda name: name == "package.json"),
    _Alias("docker compose", _phrase_trigger("docker compose"),
           lambda name: name in _DOCKER_COMPOSE_NAMES),
    _Alias("requirements", _word_trigger("requirements"),
           _requirements_matcher),
    _Alias("pyproject", _word_trigger("pyproject"),
           lambda name: name == "pyproject.toml"),
)


def _normalize_text(value: str) -> str:
    return " ".join((value or "").lower().replace("’", "'").split())


def _resolve_explicit_path(
    feature_description: str,
    indexed_paths: set[str],
    indexed_dirs: set[str],
    create_requested: bool,
) -> EditTargetResolution | None:
    """Resolve the first explicit-path token, or None if none is present."""
    for raw in (feature_description or "").split():
        norm = _normalize_path_token(raw)
        if norm is None or not _looks_like_explicit_path(norm):
            continue
        if _is_forbidden_target(norm):
            return EditTargetResolution.forbidden(norm)
        if norm in indexed_paths:
            return EditTargetResolution.grounded(norm)
        if create_requested and _is_allowed_create_target(norm, indexed_dirs):
            return EditTargetResolution.create_target(norm)
        return EditTargetResolution.not_found(norm)
    return None


def _resolve_alias(
    feature_description: str,
    indexed_paths: set[str],
    indexed_dirs: set[str],
    create_requested: bool,
) -> EditTargetResolution | None:
    """Resolve the first triggered literal-file alias, or None if none fires."""
    # Treat hyphens as spaces so "docker-compose" / "package-json" trigger the
    # same aliases as the spaced forms. Strip punctuation around tokens so
    # "README, create it if missing" still triggers the readme alias.
    text = " ".join(
        _strip_token(raw)
        for raw in _normalize_text(feature_description).replace("-", " ").split()
        if _strip_token(raw)
    )
    words = set(text.split())
    for alias in _ALIASES:
        if not alias.trigger(words, text):
            continue
        matches = sorted(
            path for path in indexed_paths
            if alias.matcher(_basename(path).lower())
            and not _is_forbidden_target(path)
        )
        if not matches:
            if (
                create_requested
                and alias.label == "readme"
                and _is_allowed_create_target("README.md", indexed_dirs)
            ):
                return EditTargetResolution.create_target("README.md")
            return EditTargetResolution.not_found(alias.label)
        if len(matches) == 1:
            return EditTargetResolution.grounded(matches[0])
        return EditTargetResolution.ambiguous(alias.label, matches)
    return None


def resolve_explicit_edit_target(
    project_id: str,
    feature_description: str,
    *,
    allow_create_target: bool = False,
) -> EditTargetResolution:
    """
    Resolve the explicit edit target referenced by ``feature_description``
    against the project's repo index. Deterministic; no LLM, no fuzzy matching,
    no filesystem walk, no file creation.

    ``CREATE_TARGET`` is opt-in until #17G wires create handling into
    ``/runs/chunked``; existing callers keep the previous missing-target
    behavior by default.

    Resolution order (first match wins):
      1. an explicit relative path token (exact, case-sensitive index match);
      2. a literal-file alias (readme / package json / docker compose /
         requirements / pyproject), matched by indexed basename;
      3. otherwise ``NO_TARGET``.
    """
    index = get_indexed_paths_and_dirs(project_id)
    indexed_paths = index.paths
    indexed_dirs = index.dirs
    create_requested = (
        allow_create_target and _has_explicit_create_intent(feature_description)
    )

    explicit = _resolve_explicit_path(
        feature_description,
        indexed_paths,
        indexed_dirs,
        create_requested,
    )
    if explicit is not None:
        return explicit

    alias = _resolve_alias(
        feature_description,
        indexed_paths,
        indexed_dirs,
        create_requested,
    )
    if alias is not None:
        return alias

    return EditTargetResolution.no_target()
