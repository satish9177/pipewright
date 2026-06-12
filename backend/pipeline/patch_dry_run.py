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
  - preserves a target file's detected line-ending style for edit/modify,
  - defaults new files to LF,
  - never normalizes mixed-EOL files or does fuzzy matching.

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
from backend.pipeline.policy import MAX_FILE_LINES
from backend.utils.path_safety import (
    is_forbidden_write_path,
    validate_safe_relative_path,
)

# Files larger than this may not be modified by wholesale full-content
# replacement; they must be changed with a targeted action="edit" instead.
# Single-sourced from policy (§8b) — previously a hand-mirrored copy of the
# coder's cap that could drift. The local name is kept for existing readers;
# policy.py is a constants-only module, so this import keeps the cycle-free
# read-only contract.
MAX_MODIFY_FILE_LINES = MAX_FILE_LINES


class MatchStatus(str, Enum):
    """Result of an exact-once old_string match attempt."""

    OK = "ok"
    ABSENT = "absent"
    NON_UNIQUE = "non_unique"


class LineEndingStyle(str, Enum):
    """Detected line-ending style for an existing file."""

    NONE = "none"
    LF = "lf"
    CRLF = "crlf"
    MIXED = "mixed"


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


def detect_line_ending_style(content: str) -> LineEndingStyle:
    """
    Detect whether a file has one clear line-ending style.

    Mixed files intentionally have no inferred style: edits preserve untouched
    bytes and use the replacement exactly as supplied rather than normalizing
    the whole file.
    """
    crlf_count = content.count("\r\n")
    without_crlf = content.replace("\r\n", "")
    lf_count = without_crlf.count("\n")
    cr_count = without_crlf.count("\r")

    styles_present = sum(1 for count in (crlf_count, lf_count, cr_count) if count)
    if styles_present == 0:
        return LineEndingStyle.NONE
    if styles_present > 1 or cr_count:
        return LineEndingStyle.MIXED
    if crlf_count:
        return LineEndingStyle.CRLF
    return LineEndingStyle.LF


def _to_lf(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def normalize_new_file_content(content: str) -> str:
    """New files have no existing style, so default their text to LF."""
    return _to_lf(content)


def convert_line_endings(content: str, style: LineEndingStyle) -> str:
    """Convert content to a single detected style; leave mixed/none unchanged."""
    if style == LineEndingStyle.LF:
        return _to_lf(content)
    if style == LineEndingStyle.CRLF:
        return _to_lf(content).replace("\n", "\r\n")
    return content


def compute_edited_content(
    relative_path: str,
    content: str,
    old_string: str | None,
    new_string: str | None,
    line_ending_style: LineEndingStyle = LineEndingStyle.NONE,
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

    old_string = convert_line_endings(old_string, line_ending_style)
    new_string = convert_line_endings(new_string, line_ending_style)

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
        return full_path.read_bytes().decode("utf-8")
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

    Order and precondition behavior mirror the previous apply_patch pass-1 loop:
      1. path validation (forbidden + safe relative path),
      2. valid action whitelist,
      3. create target must not exist; edit/modify/delete target must exist,
      4. large-file wholesale-modify guard,
      5. compute new content (delete -> '', edit -> exact-once replace,
         create -> LF content, modify -> existing file EOL style when clear).

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
    line_ending_style = detect_line_ending_style(original_content)

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
            line_ending_style,
        )
    elif change.action == "create":
        new_content = normalize_new_file_content(change.content or "")
    elif change.action == "modify":
        new_content = convert_line_endings(
            change.content or "",
            line_ending_style,
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
