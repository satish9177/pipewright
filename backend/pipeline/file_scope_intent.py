"""
file_scope_intent.py
Deterministic user file-scope constraints + reconciliation (PR #22A).

Why this exists
---------------
``files_expected`` is enforced as hard scope by ``scope_guard``. When a user
explicitly names files ("...using only src/app.py and tests/test_app.py"), the
prior flow could silently drop all but the first named file: the explicit-target
resolver returns only the first path and the single-file pin keeps exactly one.
The approved plan then contradicts its own description and the coder trips
``SCOPE_VIOLATION`` at execution.

This module makes explicit user file constraints first-class and reconciles them
against the planner's ``files_expected`` BEFORE approval/execution. It is pure
and deterministic: no LLM, no filesystem walk. The only index access is via
``plan_path_grounding`` (existing ``file_index`` rows).

It NEVER weakens ``scope_guard`` and NEVER auto-expands scope from planner prose:
prose-only mentions are flagged for human review, not silently added.

Two stages
----------
1. ``extract_user_file_constraints`` — classify the user request's file mentions
   into hard_allowlist / preferred / forbidden / reference_only / uncertain.
2. ``reconcile_file_scope`` — apply those constraints to a (already repo-grounded)
   ``TriageResult``: seed/cap to a hard allowlist, drop forbidden files, never
   auto-add reference-only or prose-only mentions. Scope mutations (drops/caps)
   harden the chunk ([SCOPE] note + requires_human_review + high risk); a
   note-only prose mismatch is annotated + requires_human_review without risk
   escalation, and a prose path scoped in another chunk of the run is no
   mismatch at all (E9).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline.file_alias_grounding import (
    _looks_like_explicit_path,
    _normalize_path_token,
)
from backend.pipeline.plan_path_grounding import (
    GroundingIndex,
    _is_grounded,
    _normalize_path,
    get_indexed_paths_and_dirs,
)

logger = logging.getLogger(__name__)

SCOPE_NOTE_PREFIX = "[SCOPE]"

# Roles, highest authority first. A path mentioned multiple ways takes its
# highest-priority role (forbidden wins, reference beats preferred, etc.).
_FORBIDDEN = "forbidden"
_REFERENCE = "reference"
_ALLOWLIST = "allowlist"
_PREFERRED = "preferred"
_UNCERTAIN = "uncertain"
_ROLE_PRIORITY = [_FORBIDDEN, _REFERENCE, _ALLOWLIST, _PREFERRED, _UNCERTAIN]

# Verbs whose object is a file the user wants edited. With "only" adjacent these
# mark a HARD ALLOWLIST; alone they mark PREFERRED files.
_MODIFY_VERBS = {
    "use", "using", "modify", "modifying", "update", "updating", "change",
    "changing", "touch", "touching", "edit", "editing",
}

# Verbs that may follow a forbid lead ("do not <verb> A", "without <verb> A").
_FORBID_VERBS = _MODIFY_VERBS | {"alter", "altering"}

# List separators between file tokens ("A and B", "A, B", "A & B").
_CONNECTORS = {"and", "&", ",", "and/or", "or"}

# Filler tokens tolerated between a cue and the first file ("modify only the
# files A and B") and between connectors.
_FILLERS = {
    "the", "a", "an", "both", "these", "those", "files", "file", "to", "in",
    "into", "within", "of", "just", "only",
}

# Punctuation stripped from the edges of a lowered token for cue matching.
_CUE_STRIP = ",.;:!?()[]{}\"'"

# A list marker at the START of a line: "-", "*", "+", "1.", "2)". Markers are
# bare tokens only — "*.py" and "3.11" never match (digits capped at 3 so a
# year like "2026." is not a marker either).
_BULLET_RE = re.compile(r"^(?:[-*+]|\d{1,3}[.)])$")


def _tokenize(feature_description: str) -> list[str]:
    """
    Line-aware whitespace tokenization (E9). A bullet/number marker at the
    start of a line is dropped, so a bulleted or numbered list after a cue
    ("Only modify:") parses exactly like the inline comma form. Mid-line
    hyphens/asterisks are untouched and still terminate a file list.
    """
    tokens: list[str] = []
    for line in feature_description.splitlines():
        parts = line.split()
        if parts and _BULLET_RE.match(parts[0]):
            parts = parts[1:]
        tokens.extend(parts)
    return tokens


@dataclass(frozen=True)
class UserFileConstraints:
    """Deterministic classification of file mentions in a user request."""

    hard_allowlist: tuple[str, ...] = ()
    preferred_files: tuple[str, ...] = ()
    forbidden_files: tuple[str, ...] = ()
    reference_only_files: tuple[str, ...] = ()
    uncertain_mentions: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.hard_allowlist
            or self.preferred_files
            or self.forbidden_files
            or self.reference_only_files
            or self.uncertain_mentions
        )


def _path_of(raw_token: str) -> str | None:
    """Return the normalized path if ``raw_token`` is a deliberate file ref."""
    norm = _normalize_path_token(raw_token)
    if norm is None or not _looks_like_explicit_path(norm):
        return None
    return norm


def _lower(raw_token: str) -> str:
    return raw_token.lower().replace("'", "").replace("’", "").strip(_CUE_STRIP)


def _match_cue(lower: list[str], i: int) -> tuple[str | None, int]:
    """
    Detect a constraint cue starting at index ``i``.

    Returns ``(role, tokens_consumed)``; ``(None, 0)`` when no cue starts here.
    Forbid leads optionally consume a following forbid verb so the file list
    that follows is collected correctly ("do not touch A" -> consume 3).
    """
    n = len(lower)
    tok = lower[i]
    nxt = lower[i + 1] if i + 1 < n else ""

    # --- Forbidden ---
    if tok == "do" and nxt == "not":
        after = lower[i + 2] if i + 2 < n else ""
        return _FORBIDDEN, (3 if after in _FORBID_VERBS else 2)
    if tok in {"dont", "doesnt", "wont"}:
        return _FORBIDDEN, (2 if nxt in _FORBID_VERBS else 1)
    if tok == "without":
        return _FORBIDDEN, (2 if nxt in _FORBID_VERBS else 1)
    if tok == "never":
        return _FORBIDDEN, (2 if nxt in _FORBID_VERBS else 1)
    if tok == "except":
        return _FORBIDDEN, 1

    # --- Reference-only (never becomes scope) ---
    if tok == "similar" and nxt == "to":
        return _REFERENCE, 2
    if tok == "look" and nxt == "at":
        return _REFERENCE, 2
    if tok == "based" and nxt == "on":
        return _REFERENCE, 2
    if tok == "refer" and nxt == "to":
        return _REFERENCE, 2
    if tok == "as" and nxt == "in":
        return _REFERENCE, 2
    if tok in {"like", "see"}:
        return _REFERENCE, 1

    # --- Hard allowlist ("only" adjacent to a modify verb) ---
    if tok == "only" and nxt in _MODIFY_VERBS:
        return _ALLOWLIST, 2
    if tok in _MODIFY_VERBS and nxt == "only":
        return _ALLOWLIST, 2

    # --- Preferred (modify verb without "only") ---
    if tok in _MODIFY_VERBS:
        return _PREFERRED, 1

    return None, 0


def _collect_files(
    raw_tokens: list[str],
    lower: list[str],
    start: int,
    role: str,
    roles: dict[str, set[str]],
) -> int:
    """
    Assign ``role`` to the run of file tokens beginning at ``start`` (after a
    cue). Tolerates a few fillers before the first file and connectors/fillers
    between files. Returns the index just past the collected list.
    """
    n = len(raw_tokens)
    j = start
    started = False
    skipped = 0
    while j < n:
        path = _path_of(raw_tokens[j])
        if path is not None:
            roles.setdefault(path, set()).add(role)
            started = True
            j += 1
            continue
        low = lower[j]
        if low in _CONNECTORS or low in _FILLERS:
            if not started and low not in _FILLERS:
                # a connector before any file is not part of a list
                break
            if not started:
                skipped += 1
                if skipped > 3:
                    break
            j += 1
            continue
        break
    return j


def extract_user_file_constraints(feature_description: str) -> UserFileConstraints:
    """
    Classify the file paths a user explicitly mentioned in ``feature_description``.

    Deterministic; no LLM. Paths are normalized but NOT grounded here (grounding
    happens in :func:`reconcile_file_scope`). Only tokens that look like real
    file references (known extension / sensitive dotfile / ``.git``) are
    considered — bare ``and/or`` or ``TCP/IP`` are ignored.
    """
    if not feature_description:
        return UserFileConstraints()

    raw_tokens = _tokenize(feature_description)
    lower = [_lower(t) for t in raw_tokens]
    roles: dict[str, set[str]] = {}

    i = 0
    n = len(raw_tokens)
    while i < n:
        role, consumed = _match_cue(lower, i)
        if role is not None:
            i = _collect_files(raw_tokens, lower, i + consumed, role, roles)
            continue
        path = _path_of(raw_tokens[i])
        if path is not None:
            roles.setdefault(path, set()).add(_UNCERTAIN)
        i += 1

    buckets: dict[str, list[str]] = {r: [] for r in _ROLE_PRIORITY}
    for path, path_roles in roles.items():
        for role in _ROLE_PRIORITY:
            if role in path_roles:
                buckets[role].append(path)
                break

    return UserFileConstraints(
        hard_allowlist=tuple(sorted(buckets[_ALLOWLIST])),
        preferred_files=tuple(sorted(buckets[_PREFERRED])),
        forbidden_files=tuple(sorted(buckets[_FORBIDDEN])),
        reference_only_files=tuple(sorted(buckets[_REFERENCE])),
        uncertain_mentions=tuple(sorted(buckets[_UNCERTAIN])),
    )


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _grounded(norm: str, index: GroundingIndex, sanctioned: set[str]) -> bool:
    return norm in sanctioned or _is_grounded(norm, index)


def _norm_set(paths) -> set[str]:
    out: set[str] = set()
    for path in paths:
        norm = _normalize_path(path)
        if norm is not None:
            out.add(norm)
    return out


def _grounded_prose_paths(
    chunk: ChunkDefinition,
    index: GroundingIndex,
    sanctioned: set[str],
) -> list[str]:
    """Grounded, real file paths named in a chunk's title/description/rationale."""
    text = " ".join([chunk.title or "", chunk.description or "", chunk.rationale or ""])
    found: list[str] = []
    for raw in text.split():
        path = _path_of(raw)
        if path is None:
            continue
        norm = _normalize_path(path)
        if norm is not None and _grounded(norm, index, sanctioned):
            found.append(norm)
    return _dedupe(found)


def _apply_scope_notes(
    chunk: ChunkDefinition,
    files: list[str],
    notes: list[str],
    *,
    harden: bool,
    require_review: bool = False,
) -> ChunkDefinition:
    update: dict = {"files_expected": files}
    if notes:
        note_text = " ".join(f"{SCOPE_NOTE_PREFIX} {n}" for n in notes)
        rationale = chunk.rationale or ""
        if note_text not in rationale:
            rationale = (
                f"{rationale.rstrip()} {note_text}".strip() if rationale else note_text
            )
        update["rationale"] = rationale
    if harden:
        update["requires_human_review"] = True
        update["risk_level"] = "high"
    elif require_review:
        # Note-only mismatch (E9): a human must look, but the chunk's scope
        # was not changed, so risk_level is never inflated to "high".
        update["requires_human_review"] = True
    return chunk.model_copy(update=update)


def reconcile_file_scope(
    project_id: str,
    triage_result: TriageResult,
    constraints: UserFileConstraints,
    *,
    sanctioned_new_paths: set[str] | None = None,
) -> TriageResult:
    """
    Reconcile a repo-grounded ``TriageResult`` against explicit user file
    constraints. Run AFTER ``ground_triage_result_paths`` / ``scan_triage_result``.

    The user-constraint logic (allowlist seed/cap, forbidden removal, preferred
    include) only fires when the user named files. The plan-consistency check
    (planner prose names a grounded file missing from ``files_expected``) always
    runs, since that mismatch is independent of what the user typed — but it is
    run-aware (E9): a prose path that lives in another chunk's
    ``files_expected`` in the same run is planned work elsewhere, not a
    mismatch, and produces no note. Never auto-adds reference-only or
    planner-prose-only paths to ``files_expected``; a genuinely unscoped
    mention is surfaced as a [SCOPE] warning with ``requires_human_review``
    (note-only — it never escalates ``risk_level``). Scope mutations
    (forbidden/allowlist removals) keep full hardening.
    """
    index = get_indexed_paths_and_dirs(project_id)
    sanctioned = _norm_set(sanctioned_new_paths or set())

    forbidden = _norm_set(constraints.forbidden_files)
    reference = _norm_set(constraints.reference_only_files)

    # Run-level scope for the consistency check, computed over the incoming
    # plan. Forbidden/allowlist removals below still produce their own loud
    # notes on the chunk that owns the removed file.
    run_scope: set[str] = set()
    for chunk in triage_result.chunks:
        run_scope.update(_norm_set(chunk.files_expected))

    allow_all = _dedupe([
        norm for path in constraints.hard_allowlist
        if (norm := _normalize_path(path)) is not None and norm not in forbidden
    ])
    grounded_allow = [n for n in allow_all if _grounded(n, index, sanctioned)]
    ungrounded_allow = [n for n in allow_all if n not in grounded_allow]
    has_allowlist = bool(allow_all)

    preferred_grounded = _dedupe([
        norm for path in constraints.preferred_files
        if (norm := _normalize_path(path)) is not None
        and norm not in forbidden
        and _grounded(norm, index, sanctioned)
    ])

    single_chunk = triage_result.total_chunks == 1 and len(triage_result.chunks) == 1

    new_chunks: list[ChunkDefinition] = []
    for idx, chunk in enumerate(triage_result.chunks):
        is_first = idx == 0
        files = _dedupe([
            _normalize_path(f) or f for f in chunk.files_expected
        ])
        prose_paths = _grounded_prose_paths(chunk, index, sanctioned)
        notes: list[str] = []
        harden = False
        needs_review = False

        # 1. Forbidden files must never be in scope.
        removed_forbidden = [f for f in files if f in forbidden]
        if removed_forbidden:
            files = [f for f in files if f not in forbidden]
            notes.append(
                "removed file(s) the user asked not to touch: "
                + ", ".join(sorted(set(removed_forbidden)))
            )
            harden = True

        # 2. Hard allowlist: seed (single chunk) or cap (multi chunk) to it.
        if has_allowlist:
            allow_set = set(grounded_allow)
            extras = [f for f in files if f not in allow_set]
            if single_chunk:
                files = list(grounded_allow)
            elif extras:
                files = [f for f in files if f in allow_set]
            if extras:
                notes.append(
                    "removed file(s) outside the user's explicit allowlist: "
                    + ", ".join(sorted(set(extras)))
                )
                harden = True
            if is_first and ungrounded_allow:
                notes.append(
                    "user explicitly listed file(s) not found in the repo index; "
                    "verify the path or reindex: "
                    + ", ".join(sorted(set(ungrounded_allow)))
                )
                harden = True

        # 3. Preferred files (no allowlist): include grounded user-named files.
        if single_chunk and preferred_grounded and not has_allowlist:
            added = [f for f in preferred_grounded if f not in files]
            if added:
                files = files + added
                notes.append(
                    "included user-requested file(s): "
                    + ", ".join(sorted(set(added)))
                )

        # 4. Plan consistency: prose names a grounded file missing from scope.
        #    Run-aware (E9): a path scoped in another chunk of this run is not
        #    a mismatch and produces no note. A genuinely unscoped mention is
        #    never auto-added; it is flagged for human review only — risk_level
        #    is not escalated for a note-only mismatch.
        missing_prose = [
            p for p in prose_paths
            if p not in set(files)
            and p not in forbidden
            and p not in reference
            and p not in run_scope
        ]
        if missing_prose:
            notes.append(
                "description references file(s) not in files_expected "
                "(not auto-added; confirm scope): "
                + ", ".join(sorted(set(missing_prose)))
            )
            needs_review = True

        files = _dedupe(files)
        if notes or files != list(chunk.files_expected):
            chunk = _apply_scope_notes(
                chunk, files, notes, harden=harden, require_review=needs_review
            )
        new_chunks.append(chunk)

    # Multi-chunk: ensure every grounded allowlist file appears somewhere.
    if not single_chunk and grounded_allow and new_chunks:
        present: set[str] = set()
        for chunk in new_chunks:
            present.update(_norm_set(chunk.files_expected))
        missing = [f for f in grounded_allow if f not in present]
        if missing:
            new_chunks[0] = _apply_scope_notes(
                new_chunks[0],
                list(new_chunks[0].files_expected),
                [
                    "user explicitly allowed file(s) not present in any chunk: "
                    + ", ".join(sorted(set(missing)))
                ],
                harden=True,
            )

    return triage_result.model_copy(update={"chunks": new_chunks})
