"""
patch_applier.py
Owns all disk writes in the pipeline.
Validates paths, backs up existing files, applies changes,
generates diffs, and supports rollback.
"""

import os
import json
import shutil
import difflib
import subprocess
from pathlib import Path
from backend.models.handoff import CoderHandoff, PatchResult, FileChange
from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.projects.project_context import get_target_repo_path
from backend.utils.path_safety import (
    is_forbidden_write_path,
    normalize_relative_path,
    validate_safe_relative_path,
)

BACKUP_DIR = Path(__file__).parent.parent / "backups"

# Mirror of coder.MAX_FILE_LINES. Defined locally so patch_applier stays a pure
# file-ops module with no dependency on the AI/coder import graph. Files larger
# than this may not be modified by wholesale full-content replacement; they must
# be changed with targeted action="edit" instead.
MAX_MODIFY_FILE_LINES = 200

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


def _validate_path(relative_path: str, target_repo: str) -> Path:
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


def _read_existing_content(full_path: Path) -> str:
    try:
        if not full_path.exists() or not full_path.is_file():
            return ""
        return full_path.read_text(encoding="utf-8")
    except Exception as error:
        raise RuntimeError(
            f"patch_applier.py: failed to read existing content: {error}"
        )


def _apply_edit_to_text(
    relative_path: str,
    content: str,
    old_string: str | None,
    new_string: str | None,
) -> str:
    """
    Apply a single targeted edit to file text and return the new text.

    Requires old_string to appear exactly once. Fails safely with a clear
    error if old_string is missing (0 occurrences) or ambiguous (>1).
    No fuzzy matching: matching is exact substring matching.
    """
    if old_string is None or new_string is None:
        raise RuntimeError(
            f"patch_applier.py: edit requires old_string and new_string: "
            f"{relative_path}"
        )

    occurrences = content.count(old_string)
    if occurrences == 0:
        raise RuntimeError(
            f"patch_applier.py: edit old_string not found in {relative_path}. "
            "The text to replace must match the file exactly."
        )
    if occurrences > 1:
        raise RuntimeError(
            f"patch_applier.py: edit old_string is not unique in {relative_path} "
            f"(found {occurrences} occurrences). Provide a larger, unique "
            "old_string so exactly one location matches."
        )

    return content.replace(old_string, new_string, 1)


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


def _apply_file_change(change: FileChange, full_path: Path) -> None:
    try:
        if change.action == "create":
            print(f"[PATCH] Applying create: {change.path}")
            if change.content is None:
                raise RuntimeError(
                    f"patch_applier.py: create requires content: {change.path}"
                )
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(change.content, encoding="utf-8")
            return

        if change.action == "modify":
            print(f"[PATCH] Applying modify: {change.path}")
            if change.content is None:
                raise RuntimeError(
                    f"patch_applier.py: modify requires content: {change.path}"
                )
            full_path.write_text(change.content, encoding="utf-8")
            return

        if change.action == "edit":
            print(f"[PATCH] Applying edit: {change.path}")
            current_content = full_path.read_text(encoding="utf-8")
            updated_content = _apply_edit_to_text(
                change.path,
                current_content,
                change.old_string,
                change.new_string,
            )
            full_path.write_text(updated_content, encoding="utf-8")
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

    validated_changes: list[tuple[FileChange, Path, str, str]] = []
    manifest: list[dict] = []

    try:
        for change in coder_output.files_changed:
            full_path = _validate_path(change.path, target_repo)

            if change.action not in ["create", "modify", "delete", "edit"]:
                raise RuntimeError(
                    f"patch_applier.py: invalid action '{change.action}' "
                    f"for {change.path}"
                )

            if change.action == "create" and full_path.exists():
                raise RuntimeError(
                    f"patch_applier.py: create target already exists: "
                    f"{change.path}"
                )

            if change.action in ["modify", "delete", "edit"] and not full_path.exists():
                raise RuntimeError(
                    f"patch_applier.py: {change.action} target missing: "
                    f"{change.path}"
                )

            original_content = _read_existing_content(full_path)

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
                # Validate the edit up front (fails before any backup/write) and
                # capture the resulting content so the diff reflects the edit.
                new_content = _apply_edit_to_text(
                    change.path,
                    original_content,
                    change.old_string,
                    change.new_string,
                )
            else:
                new_content = change.content or ""

            validated_changes.append((
                change,
                full_path,
                original_content,
                new_content
            ))

        for change, full_path, _original_content, _new_content in validated_changes:
            if change.action in ["modify", "delete", "edit"]:
                _backup_file(change.path, full_path, run_id, chunk_number)

            manifest.append({
                "path": change.path,
                "action": change.action
            })
            _write_manifest(run_id, manifest, chunk_number)
            _apply_file_change(change, full_path)

        diffs = []
        for change, _full_path, original_content, new_content in validated_changes:
            diffs.append(
                _generate_file_diff(change.path, original_content, new_content)
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
