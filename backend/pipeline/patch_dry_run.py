"""
patch_dry_run.py
Shared, pure structured-FileChange evaluator (#26B).

This module owns the exact-match and precondition logic that decides whether a
set of structured FileChange actions can be applied to the current working tree.
It is used by BOTH:

  - the real apply path (patch_applier.apply_patch pass 1), and
  - a zero-mutation dry-run diagnostic (dry_run_changes),

so the dry-run verdict and the real apply outcome share ONE implementation and
cannot drift.

Strictly read-only. This module:
  - never writes files,
  - never creates backups or manifests,
  - never checkpoints,
  - never calls git,
  - never normalizes CRLF/whitespace and never does fuzzy matching.

It may read file contents/existence (a read is not a mutation) and use the
shared safe-path helpers.

IMPORTANT: the RuntimeError message strings raised here are a de-facto contract.
patch_applier.classify_patch_failure maps failures onto the closed taxonomy by
substring-matching these messages, and tests assert exact phrases. Do not
"improve" the wording. The "patch_applier.py:" message prefix is preserved
verbatim for that reason.

Classification deliberately lives at the caller/test layer (patch_failures /
patch_applier.classify_patch_failure) so this module stays a pure, cycle-free
read-only layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from backend.models.handoff import CoderHandoff, FileChange
from backend.utils.path_safety import (
    is_forbidden_write_path,
    validate_safe_relative_path,
)

# Mirror of coder.MAX_FILE_LINES. Files larger than this may not be modified by
# wholesale full-content replacement; they must be changed with a targeted
# action="edit" instead. Kept here so the shared evaluator owns the single
# large-file-modify policy (value unchanged from the previous patch_applier
# definition).
MAX_MODIFY_FILE_LINES = 200


class MatchStatus(str, Enum):
    """Result of an exact-once old_string match attempt."""

    OK = "ok"
    ABSENT = "absent"
    NON_UNIQUE = "non_unique"


@dataclass(frozen=True)
class MatchResult:
    """Outcome of find_unique_match: a status plus the raw occurrence count."""

    status: MatchStatus
    count: int

    @property
    def ok(self) -> bool:
        return self.status == MatchStatus.OK


def find_unique_match(content: str, old_string: str) -> MatchResult:
    """
    Count exact occurrences of old_string in content using the current
    semantics (str.count). No fuzzy matching, no normalization; EOL/whitespace
    sensitive.

      count == 0 -> ABSENT
      count  > 1 -> NON_UNIQUE
      count == 1 -> OK
    """
    count = content.count(old_string)
    if count == 0:
        return MatchResult(MatchStatus.ABSENT, 0)
    if count > 1:
        return MatchResult(MatchStatus.NON_UNIQUE, count)
    return MatchResult(MatchStatus.OK, 1)


def compute_edited_content(
    relative_path: str,
    content: str,
    old_string: str | None,
    new_string: str | None,
) -> str:
    """
    Validate a targeted edit (old_string must match exactly once) and return the
    edited text. Raises RuntimeError with the exact existing message strings.

    Matching is delegated to find_unique_match so the dry-run, the real apply
    pass-1 validation, and the pass-2 write all use one matcher.
    """
    if old_string is None or new_string is None:
        raise RuntimeError(
            f"patch_applier.py: edit requires old_string and new_string: "
            f"{relative_path}"
        )

    match = find_unique_match(content, old_string)
    if match.status == MatchStatus.ABSENT:
        raise RuntimeError(
            f"patch_applier.py: edit old_string not found in {relative_path}. "
            "The text to replace must match the file exactly."
        )
    if match.status == MatchStatus.NON_UNIQUE:
        raise RuntimeError(
            f"patch_applier.py: edit old_string is not unique in {relative_path} "
            f"(found {match.count} occurrences). Provide a larger, unique "
            "old_string so exactly one location matches."
        )

    return content.replace(old_string, new_string, 1)


def validate_write_path(relative_path: str, target_repo: str) -> Path:
    """
    Validate a write path: reject forbidden paths, then resolve to a safe
    absolute path inside the repo root. Read-only; preserves the existing
    message strings exactly.
    """
    try:
        print(f"[PATCH] Validating path: {relative_path}")

        if is_forbidden_write_path(relative_path):
            raise RuntimeError(
                f"patch_applier.py: [SECURITY] forbidden path rejected: "
                f"{relative_path}"
            )

        root = Path(target_repo).resolve()
        return validate_safe_relative_path(relative_path, root)
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"patch_applier.py: failed to validate path {relative_path}: {error}"
        )


def read_existing_content(full_path: Path) -> str:
    """Read current file text, or '' if the file is missing. Read-only."""
    try:
        if not full_path.exists() or not full_path.is_file():
            return ""
        return full_path.read_text(encoding="utf-8")
    except Exception as error:
        raise RuntimeError(
            f"patch_applier.py: failed to read existing content: {error}"
        )


@dataclass(frozen=True)
class EvaluatedChange:
    """
    A validated FileChange plus everything pass-2 (write/diff) needs: the
    resolved absolute path, the original content, and the computed new content.
    """

    change: FileChange
    full_path: Path
    original_content: str
    new_content: str

    @property
    def path(self) -> str:
        return self.change.path

    @property
    def action(self) -> str:
        return self.change.action


def evaluate_file_change(change: FileChange, target_repo: str) -> EvaluatedChange:
    """
    Validate a single FileChange against the current working tree and compute
    its resulting content WITHOUT writing anything. Shared by the real apply
    (pass 1) and the dry-run.

    Order and behavior mirror the previous apply_patch pass-1 loop exactly:
      1. path validation (forbidden + safe relative path),
      2. valid action whitelist,
      3. create target must not exist; edit/modify/delete target must exist,
      4. large-file wholesale-modify guard,
      5. compute new content (delete -> '', edit -> exact-once replace,
         create/modify -> provided content).

    Raises RuntimeError with the existing message strings on any precondition
    failure. A no-op edit (new_string == old_string) is appliable here; the
    NO_CHANGES decision is made later, outside this evaluator.
    """
    full_path = validate_write_path(change.path, target_repo)

    if change.action not in ("create", "modify", "delete", "edit"):
        raise RuntimeError(
            f"patch_applier.py: invalid action '{change.action}' "
            f"for {change.path}"
        )

    if change.action == "create" and full_path.exists():
        raise RuntimeError(
            f"patch_applier.py: create target already exists: {change.path}"
        )

    if change.action in ("modify", "delete", "edit") and not full_path.exists():
        raise RuntimeError(
            f"patch_applier.py: {change.action} target missing: {change.path}"
        )

    original_content = read_existing_content(full_path)

    if change.action == "modify":
        original_line_count = len(original_content.splitlines())
        if original_line_count > MAX_MODIFY_FILE_LINES:
            raise RuntimeError(
                f"patch_applier.py: Large files cannot be replaced "
                f"wholesale automatically. Use targeted edits. "
                f"({change.path}: {original_line_count} lines exceeds "
                f"{MAX_MODIFY_FILE_LINES})"
            )

    if change.action == "delete":
        new_content = ""
    elif change.action == "edit":
        new_content = compute_edited_content(
            change.path,
            original_content,
            change.old_string,
            change.new_string,
        )
    else:
        new_content = change.content or ""

    return EvaluatedChange(
        change=change,
        full_path=full_path,
        original_content=original_content,
        new_content=new_content,
    )


@dataclass(frozen=True)
class DryRunResult:
    """
    Read-only verdict for a CoderHandoff against the current working tree.

    Covers ONLY pre-write structured-FileChange validation. It deliberately does
    NOT classify (or pretend to classify) post-apply / runtime conditions such
    as TEST_FAILURE_AFTER_APPLY, DIRTY_WORKTREE, post-apply actual-dirty-set
    scope violations, rollback/manual-intervention, NO_CHANGES, or commit/
    checkpoint failures. Those are decided elsewhere.

    All-or-nothing: on the first failing change, evaluation stops and the raw
    failure details are returned. `error_message` is the verbatim RuntimeError
    text so the caller/test layer can classify it via
    patch_applier.classify_patch_failure (kept out of this pure module to avoid
    an import cycle).
    """

    ok: bool
    evaluated: tuple[EvaluatedChange, ...] = ()
    failed_path: str | None = None
    failed_action: str | None = None
    error_message: str | None = None


def dry_run_changes(coder_output: CoderHandoff, repo_path: str) -> DryRunResult:
    """
    Evaluate every FileChange against current disk state without writing
    anything. Mirrors apply_patch pass-1 all-or-nothing semantics: the first
    failure short-circuits and nothing is (or would be) written.

    Zero disk mutation: only reads file existence/content via the shared
    evaluator. Empty files_changed evaluates as ok (there is no structured
    validation failure to report; NO_CHANGES is decided by callers).
    """
    evaluated: list[EvaluatedChange] = []
    for change in coder_output.files_changed:
        try:
            evaluated.append(evaluate_file_change(change, repo_path))
        except RuntimeError as error:
            return DryRunResult(
                ok=False,
                evaluated=tuple(evaluated),
                failed_path=change.path,
                failed_action=change.action,
                error_message=str(error),
            )
    return DryRunResult(ok=True, evaluated=tuple(evaluated))
