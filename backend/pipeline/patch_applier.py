"""
patch_applier.py
Owns all disk writes in the pipeline.
Validates paths, backs up existing files, applies changes,
generates diffs, and supports rollback.

This module also provides a guarded entry point, apply_patch_guarded
(PR #18C), which wraps the existing apply_patch with the #18A safety
lifecycle (clean-tree precondition, post-apply actual changed-file scope
validation, rollback-and-verify) and returns a structured PatchApplyOutcome
built from the #18B patch_failures helpers. The guarded path is intentionally
NOT wired into the orchestrator or tester yet — that is #18D. The original
apply_patch / rollback_patch behavior is unchanged.
"""

import json
import shutil
import difflib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from backend.git import local_git
from backend.models.handoff import CoderHandoff, PatchResult, FileChange
from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.projects.project_context import get_target_repo_path
from backend.pipeline.patch_failures import (
    PatchFailureReport,
    PatchFailureType,
    build_patch_failure_report,
)
from backend.utils.path_safety import (
    normalize_relative_path,
    validate_safe_relative_path,
)
from backend.pipeline.patch_dry_run import (
    EvaluatedChange,
    evaluate_file_change,
)

BACKUP_DIR = Path(__file__).parent.parent / "backups"

# The large-file-modify threshold, the shared precondition evaluator, and the
# exact-match helpers now live in patch_dry_run so the zero-mutation dry-run and
# the real apply pass-1 validation share ONE implementation (#26B). Behavior and
# RuntimeError message strings are unchanged.

def _get_git_hash(repo_path: str) -> str:
    """
    Get current HEAD git hash from target repo.
    Returns 'no-git' if repo has no commits
    or git is not available.
    Never raises - always returns a string.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10
        )
        if result.returncode != 0:
            return "no-git"
        git_hash = result.stdout.strip()
        return git_hash if git_hash else "no-git"
    except Exception:
        return "no-git"


def _backup_root(run_id: str, chunk_number: int = 0) -> Path:
    if chunk_number == 0:
        return BACKUP_DIR / run_id
    return BACKUP_DIR / run_id / f"chunk_{chunk_number}"


def _manifest_path(run_id: str, chunk_number: int = 0) -> Path:
    return _backup_root(run_id, chunk_number) / "manifest.json"


def _original_backup_path(
    run_id: str,
    relative_path: str,
    chunk_number: int = 0
) -> Path:
    return _backup_root(run_id, chunk_number) / "original" / relative_path


def _backup_file(
    relative_path: str,
    full_path: Path,
    run_id: str,
    chunk_number: int = 0
) -> None:
    try:
        print(f"[PATCH] Backing up: {relative_path}")

        if not full_path.exists() or not full_path.is_file():
            raise RuntimeError(
                f"patch_applier.py: cannot backup missing file: {relative_path}"
            )

        backup_path = _original_backup_path(run_id, relative_path, chunk_number)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(full_path, backup_path)
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"patch_applier.py: failed to backup {relative_path}: {error}"
        )


def _generate_file_diff(
    relative_path: str,
    original_content: str,
    new_content: str
) -> str:
    try:
        return "".join(difflib.unified_diff(
            original_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
            lineterm=""
        ))
    except Exception as error:
        raise RuntimeError(
            f"patch_applier.py: failed to generate diff for {relative_path}: {error}"
        )


def _write_manifest(
    run_id: str,
    manifest: list[dict],
    chunk_number: int = 0
) -> None:
    try:
        manifest_path = _manifest_path(run_id, chunk_number)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8"
        )
    except Exception as error:
        raise RuntimeError(
            f"patch_applier.py: failed to write rollback manifest: {error}"
        )


def _load_manifest(run_id: str, chunk_number: int = 0) -> list[dict] | None:
    try:
        manifest_path = _manifest_path(run_id, chunk_number)
        if not manifest_path.exists():
            return None
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as error:
        raise RuntimeError(
            f"patch_applier.py: failed to load rollback manifest: {error}"
        )


def _write_utf8_bytes(full_path: Path, content: str) -> None:
    full_path.write_bytes(content.encode("utf-8"))


def _apply_file_change(
    change: FileChange,
    full_path: Path,
    new_content: str,
) -> None:
    try:
        if change.action == "create":
            print(f"[PATCH] Applying create: {change.path}")
            if change.content is None:
                raise RuntimeError(
                    f"patch_applier.py: create requires content: {change.path}"
                )
            full_path.parent.mkdir(parents=True, exist_ok=True)
            _write_utf8_bytes(full_path, new_content)
            return

        if change.action == "modify":
            print(f"[PATCH] Applying modify: {change.path}")
            if change.content is None:
                raise RuntimeError(
                    f"patch_applier.py: modify requires content: {change.path}"
                )
            _write_utf8_bytes(full_path, new_content)
            return

        if change.action == "edit":
            print(f"[PATCH] Applying edit: {change.path}")
            _write_utf8_bytes(full_path, new_content)
            return

        if change.action == "delete":
            print(f"[PATCH] Applying delete: {change.path}")
            full_path.unlink()
            return

        raise RuntimeError(
            f"patch_applier.py: unsupported action '{change.action}' "
            f"for {change.path}"
        )
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"patch_applier.py: failed to apply {change.action} "
            f"for {change.path}: {error}"
        )


def _rollback_from_manifest(
    run_id: str,
    manifest: list[dict],
    chunk_number: int = 0
) -> bool:
    try:
        target_repo = get_target_repo_path()
        root = Path(target_repo).resolve()

        for entry in reversed(manifest):
            relative_path = normalize_relative_path(entry["path"])
            action = entry["action"]
            full_path = validate_safe_relative_path(relative_path, root)

            if action == "create":
                if full_path.exists():
                    full_path.unlink()
                continue

            backup_path = _original_backup_path(run_id, relative_path, chunk_number)
            if not backup_path.exists():
                raise RuntimeError(
                    f"patch_applier.py: rollback backup missing: {relative_path}"
                )

            full_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, full_path)

        return True
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(f"patch_applier.py: rollback failed: {error}")


def apply_patch(
    coder_output: CoderHandoff,
    run_id: str,
    chunk_number: int = 0
) -> PatchResult:
    """
    Synchronous. No AI calls. Pure file operations.
    Validates, backs up, applies, diffs, checkpoints.
    """
    print(f"[PATCH] Starting | run_id={run_id}")

    target_repo = get_target_repo_path()
    pre_patch_git_hash = _get_git_hash(target_repo)
    print(f"[PATCH] Pre-patch hash: {pre_patch_git_hash}")

    if not coder_output.files_changed:
        print(f"[PATCH] No file changes produced | run_id={run_id}")
        return PatchResult(
            run_id=run_id,
            success=False,
            diff="",
            pre_patch_git_hash=pre_patch_git_hash,
            post_patch_git_hash=pre_patch_git_hash,
            files_applied=[],
            rollback_available=False,
        )

    validated_changes: list[EvaluatedChange] = []
    manifest: list[dict] = []

    try:
        # Pass 1: validate every change in memory (no writes). This shares the
        # exact-match/precondition evaluator with the zero-mutation dry-run via
        # evaluate_file_change (#26B), so the dry-run verdict and the real apply
        # cannot drift. Behavior and RuntimeError message strings are unchanged.
        for change in coder_output.files_changed:
            validated_changes.append(
                evaluate_file_change(change, target_repo)
            )

        # Pass 2: write the exact content computed by pass 1, backing up and
        # recording a manifest per touched file. This keeps dry-run and real
        # apply in lockstep, including newline preservation.
        for evaluated in validated_changes:
            change = evaluated.change
            if change.action in ["modify", "delete", "edit"]:
                _backup_file(
                    change.path, evaluated.full_path, run_id, chunk_number
                )

            manifest.append({
                "path": change.path,
                "action": change.action
            })
            _write_manifest(run_id, manifest, chunk_number)
            _apply_file_change(
                change,
                evaluated.full_path,
                evaluated.new_content,
            )

        diffs = []
        for evaluated in validated_changes:
            diffs.append(
                _generate_file_diff(
                    evaluated.change.path,
                    evaluated.original_content,
                    evaluated.new_content,
                )
            )
        diff = "\n".join(diffs)
        diff_lines = len(diff.splitlines())
        print(f"[PATCH] Diff generated: {diff_lines} lines")

        post_patch_git_hash = _get_git_hash(target_repo)
        print(f"[PATCH] Post-patch hash: {post_patch_git_hash}")

        patch_result = PatchResult(
            run_id=run_id,
            success=True,
            diff=diff,
            pre_patch_git_hash=pre_patch_git_hash,
            post_patch_git_hash=post_patch_git_hash,
            files_applied=[change.path for change in coder_output.files_changed],
            rollback_available=True
        )

        save_checkpoint(
            run_id=run_id,
            step="patch",
            output=patch_result.model_dump(),
            handoff_contract=patch_result.model_dump(),
            git_hash=post_patch_git_hash,
            tests_passed=False,
            step_completed=True,
            chunk_number=chunk_number
        )
        print(f"[PATCH] Checkpoint saved | run_id={run_id}")
        print(f"[PATCH] Complete | run_id={run_id}")
        return patch_result

    except RuntimeError as error:
        if manifest:
            try:
                _rollback_from_manifest(run_id, manifest, chunk_number)
            except Exception as rollback_error:
                raise RuntimeError(
                    f"patch_applier.py: application failed and rollback failed. "
                    f"run_id={run_id} | error={error} | "
                    f"rollback_error={rollback_error}"
                )
        raise RuntimeError(
            f"patch_applier.py: failed to apply patch. "
            f"run_id={run_id} | error={error}"
        )
    except Exception as error:
        if manifest:
            try:
                _rollback_from_manifest(run_id, manifest, chunk_number)
            except Exception as rollback_error:
                raise RuntimeError(
                    f"patch_applier.py: application failed and rollback failed. "
                    f"run_id={run_id} | error={error} | "
                    f"rollback_error={rollback_error}"
                )
        raise RuntimeError(
            f"patch_applier.py: unexpected apply failure. "
            f"run_id={run_id} | error={error}"
        )


def rollback_patch(run_id: str, chunk_number: int = 0) -> bool:
    """
    Restore all backed up files for this run.
    Returns True if rollback succeeded.
    Returns False if no backup found.
    Called by tester.py if tests fail.
    """
    try:
        manifest = _load_manifest(run_id, chunk_number)
        if manifest is None:
            print(f"[PATCH] No backup found | run_id={run_id}")
            return False

        print(f"[PATCH] Rolling back | run_id={run_id}")
        result = _rollback_from_manifest(run_id, manifest, chunk_number)
        print(f"[PATCH] Rollback complete | run_id={run_id}")
        return result
    except Exception as error:
        raise RuntimeError(
            f"patch_applier.py: rollback failed. run_id={run_id} | error={error}"
        )


# --------------------------------------------------------------------------- #
# Guarded patch application (#18C)
#
# Everything below wraps the existing apply_patch with the #18A safety
# lifecycle and produces a structured PatchFailureReport (#18B) instead of a
# bare RuntimeError string. It is pure mechanism: nothing here is wired into the
# orchestrator, tester, routes, or DB yet (that is #18D). apply_patch and
# rollback_patch above are unchanged.
# --------------------------------------------------------------------------- #


# Ordered (lowercased substring, failure type). Most specific first. Matched
# against str(error) to map the existing apply_patch error vocabulary onto the
# closed #18B taxonomy. Mapping confirmed for #18C:
#   - create target already exists      -> PATCH_DOES_NOT_APPLY
#   - large-file wholesale modify block -> PATCH_MALFORMED
#   - unsupported/invalid action        -> PATCH_MALFORMED
#   - old_string missing / not unique   -> PATCH_DOES_NOT_APPLY
#   - unexpected / anything else        -> UNKNOWN_PATCH_FAILURE
_ERROR_SIGNATURE_MAP: tuple[tuple[str, PatchFailureType], ...] = (
    ("forbidden path", PatchFailureType.FORBIDDEN_FILE),
    ("unsafe", PatchFailureType.FORBIDDEN_FILE),
    ("failed to validate path", PatchFailureType.FORBIDDEN_FILE),
    ("cannot be replaced wholesale", PatchFailureType.PATCH_MALFORMED),
    ("requires content", PatchFailureType.PATCH_MALFORMED),
    ("requires old_string", PatchFailureType.PATCH_MALFORMED),
    ("old_string not found", PatchFailureType.PATCH_DOES_NOT_APPLY),
    ("not unique", PatchFailureType.PATCH_DOES_NOT_APPLY),
    ("target already exists", PatchFailureType.PATCH_DOES_NOT_APPLY),
    ("target missing", PatchFailureType.TARGET_MISSING),
    ("invalid action", PatchFailureType.PATCH_MALFORMED),
    ("unsupported action", PatchFailureType.PATCH_MALFORMED),
    ("rollback failed", PatchFailureType.UNKNOWN_PATCH_FAILURE),
)


@dataclass(frozen=True)
class PatchApplyOutcome:
    """
    Result of a guarded patch application.

    Exactly one of `patch_result` (success) or `failure` (a structured
    PatchFailureReport) is populated.
    """

    success: bool
    patch_result: PatchResult | None = None
    failure: PatchFailureReport | None = None

    @classmethod
    def from_success(cls, patch_result: PatchResult) -> "PatchApplyOutcome":
        return cls(success=True, patch_result=patch_result, failure=None)

    @classmethod
    def from_failure(cls, failure: PatchFailureReport) -> "PatchApplyOutcome":
        return cls(success=False, patch_result=None, failure=failure)


def _is_git_work_tree(repo_path: str) -> bool:
    """
    True only when repo_path is inside a real git work tree.

    Used to gate observation-based checks (clean-tree precondition, post-apply
    changed-file detection). Outside a git repo these checks are no-ops, mirror-
    ing the existing 'no-git' tolerance of _get_git_hash. Never raises.
    """
    try:
        result = local_git.run_git(
            ["rev-parse", "--is-inside-work-tree"], repo_path
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except Exception:
        return False


def classify_patch_failure(
    error: Exception,
    *,
    phase: str = "apply",
) -> PatchFailureType:
    """
    Deterministically map an apply/test failure onto the #18B taxonomy.

    `phase="test"` classifies a post-apply test failure (helper for #18D; not
    wired here). Otherwise the error message is matched against the known
    apply_patch error vocabulary. Unknown messages map to UNKNOWN_PATCH_FAILURE.
    """
    if phase == "test":
        return PatchFailureType.TEST_FAILURE_AFTER_APPLY

    message = str(error).lower()
    for signature, failure_type in _ERROR_SIGNATURE_MAP:
        if signature in message:
            return failure_type
    return PatchFailureType.UNKNOWN_PATCH_FAILURE


def actual_changed_files(repo_path: str) -> list[str]:
    """
    Repo-relative paths git reports as changed/untracked (forward slashes).

    Returns [] outside a git work tree (cannot observe). Never raises.
    """
    try:
        if not _is_git_work_tree(repo_path):
            return []
        return local_git.get_dirty_files(repo_path)
    except Exception:
        return []


def validate_changed_files_in_scope(
    actual: list[str],
    allowed: list[str],
) -> bool:
    """
    True iff every actually-changed path is within the approved scope.

    Empty `allowed` is treated as unsafe (False), mirroring scope_guard. Paths
    that cannot be normalized are treated as out of scope (False).
    """
    if not allowed:
        return False
    try:
        allowed_set = {normalize_relative_path(path) for path in allowed}
        for path in actual:
            if normalize_relative_path(path) not in allowed_set:
                return False
        return True
    except Exception:
        return False


def rollback_and_verify(
    run_id: str,
    chunk_number: int,
    repo_path: str,
) -> tuple[bool, bool]:
    """
    Roll back this chunk and verify the work tree is clean afterwards.

    Returns (rollback_performed, working_tree_clean). Idempotent: the manifest
    restore is safe to run even if apply_patch already rolled back internally.
    Never raises; a failed rollback yields (False, <observed cleanliness>) so
    callers can escalate to manual intervention.
    """
    rollback_performed = False
    try:
        rollback_performed = bool(rollback_patch(run_id, chunk_number))
    except Exception:
        rollback_performed = False

    if not _is_git_work_tree(repo_path):
        # Cannot observe cleanliness outside a git repo; do not falsely claim
        # the tree is dirty.
        return rollback_performed, True

    try:
        clean = local_git.is_working_tree_clean(repo_path)
    except Exception:
        clean = False
    return rollback_performed, clean


def apply_patch_guarded(
    coder_output: CoderHandoff,
    run_id: str,
    chunk_number: int = 0,
    *,
    files_expected: list[str],
    repo_path: str | None = None,
) -> PatchApplyOutcome:
    """
    Apply a patch with the full #18A safety lifecycle, returning a structured
    outcome instead of raising on failure.

    Lifecycle:
      0. empty changes               -> NO_CHANGES (no writes)
      1. clean-tree precondition     -> DIRTY_WORKTREE (no writes; git repos only)
      2. apply via apply_patch       -> classify + rollback-and-verify on error
      3. post-apply no-effective     -> NO_CHANGES (rollback + verify)
      4. post-apply scope validation -> SCOPE_VIOLATION (rollback + verify)
      5. success                     -> the unchanged PatchResult

    Pure mechanism: not wired into runtime. Retry budget is out of scope for
    #18C, so attempts/max_attempts are left at their #18B defaults.
    """
    target_repo = repo_path or get_target_repo_path()
    attempted = [change.path for change in coder_output.files_changed]

    # 0. Nothing to apply.
    if not coder_output.files_changed:
        return PatchApplyOutcome.from_failure(
            build_patch_failure_report(
                PatchFailureType.NO_CHANGES,
                changed_files_attempted=attempted,
                allowed_files=files_expected,
                chunk_number=chunk_number,
                failed_step="precondition",
            )
        )

    # 1. Clean-tree precondition (no-op outside a git work tree). Nothing has
    #    been written, so there is nothing to roll back.
    if _is_git_work_tree(target_repo) and not local_git.is_working_tree_clean(
        target_repo
    ):
        return PatchApplyOutcome.from_failure(
            build_patch_failure_report(
                PatchFailureType.DIRTY_WORKTREE,
                changed_files_attempted=attempted,
                allowed_files=files_expected,
                chunk_number=chunk_number,
                failed_step="precondition",
            )
        )

    # 2. Apply. apply_patch validates-then-writes and rolls back its own
    #    manifest on failure; we re-verify cleanliness and classify the error.
    try:
        patch_result = apply_patch(
            coder_output, run_id, chunk_number=chunk_number
        )
    except Exception as error:
        failure_type = classify_patch_failure(error, phase="apply")
        rolled_back, clean = rollback_and_verify(
            run_id, chunk_number, target_repo
        )
        return PatchApplyOutcome.from_failure(
            build_patch_failure_report(
                failure_type,
                technical_details=str(error),
                changed_files_attempted=attempted,
                changed_files_actual=actual_changed_files(target_repo),
                allowed_files=files_expected,
                rollback_performed=rolled_back,
                working_tree_clean=clean,
                chunk_number=chunk_number,
                failed_step="patch",
            )
        )

    # apply_patch returns success=False only for the empty-changes case handled
    # above; treat any defensive falsey result as NO_CHANGES.
    if not patch_result.success:
        return PatchApplyOutcome.from_failure(
            build_patch_failure_report(
                PatchFailureType.NO_CHANGES,
                changed_files_attempted=attempted,
                allowed_files=files_expected,
                chunk_number=chunk_number,
                failed_step="patch",
            )
        )

    # Post-apply observation only applies inside a real git repo.
    if _is_git_work_tree(target_repo):
        observed = actual_changed_files(target_repo)

        # 3. The patch applied but produced no effective change.
        if not observed:
            rolled_back, clean = rollback_and_verify(
                run_id, chunk_number, target_repo
            )
            return PatchApplyOutcome.from_failure(
                build_patch_failure_report(
                    PatchFailureType.NO_CHANGES,
                    changed_files_attempted=attempted,
                    changed_files_actual=observed,
                    allowed_files=files_expected,
                    rollback_performed=rolled_back,
                    working_tree_clean=clean,
                    chunk_number=chunk_number,
                    failed_step="patch",
                )
            )

        # 4. The patch touched a file outside the approved scope.
        if not validate_changed_files_in_scope(observed, files_expected):
            rolled_back, clean = rollback_and_verify(
                run_id, chunk_number, target_repo
            )
            return PatchApplyOutcome.from_failure(
                build_patch_failure_report(
                    PatchFailureType.SCOPE_VIOLATION,
                    changed_files_attempted=attempted,
                    changed_files_actual=observed,
                    allowed_files=files_expected,
                    rollback_performed=rolled_back,
                    working_tree_clean=clean,
                    chunk_number=chunk_number,
                    failed_step="scope_validation",
                )
            )

    # 5. Success — identical PatchResult the unguarded path would return.
    return PatchApplyOutcome.from_success(patch_result)
