"""
index_freshness.py
Working-tree/index freshness fingerprint helpers for repo index trust.

This module is deliberately separate from repo_fingerprint.py. RepoFingerprint
is already used for memory/repo-reality DB-engine detection; this module deals
only with cheap Git checkout identity for the project repo index.

#34B scope:
  - compute a cheap working-tree identity from Git plumbing
  - compare that identity with the last persisted index-built-at snapshot
  - persist/load one project-level snapshot

It does not gate run creation, auto-reindex, switch branches, scan file content,
watch files, or run on every hot grounding read.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from sqlalchemy import text

from backend.db.database import engine


DETACHED_HEAD_PREFIX = "DETACHED@"
HARD_STALE_REASONS = frozenset({
    "repo_path_mismatch",
    "branch_name_mismatch",
    "branch_detached_state_mismatch",
    "detached_head_label_mismatch",
    "head_sha_mismatch",
})


class IndexFreshnessState(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorkingTreeFingerprint:
    repo_path_resolved: str
    is_git_repo: bool
    git_available: bool
    branch_name: str | None
    branch_is_detached: bool
    detached_head_label: str | None
    head_sha: str | None
    dirty_digest: str | None
    dirty_files_count: int
    captured_at: str
    git_error: str | None = None

    @property
    def is_known(self) -> bool:
        return (
            self.git_available
            and self.is_git_repo
            and self.head_sha is not None
            and self.dirty_digest is not None
            and self.git_error is None
        )


@dataclass(frozen=True)
class StoredIndexFingerprint:
    project_id: str
    repo_path_resolved: str
    branch_name: str | None
    branch_is_detached: bool
    detached_head_label: str | None
    head_sha: str
    dirty_digest: str
    dirty_files_count: int
    index_row_count: int
    captured_at: str
    updated_at: str
    snapshot_state: str = IndexFreshnessState.CURRENT.value
    snapshot_reason: str | None = None


@dataclass(frozen=True)
class IndexFreshnessComparison:
    state: IndexFreshnessState
    reasons: tuple[str, ...]
    current: WorkingTreeFingerprint | None = None
    stored: StoredIndexFingerprint | None = None


@dataclass(frozen=True)
class ReindexAndRecordResult:
    index_result: dict
    snapshot: StoredIndexFingerprint | None
    state: IndexFreshnessState
    reasons: tuple[str, ...]
    before: WorkingTreeFingerprint | None = None
    after: WorkingTreeFingerprint | None = None


def is_hard_stale(comparison_or_read_model) -> bool:
    """
    True when freshness reasons represent a checkout identity mismatch.

    Dirty worktree and index row-count differences are still stale/warning
    signals, but they do not block implementation run creation; execution's
    existing clean-tree guards remain responsible for dirty-tree blocking.
    """
    state = getattr(comparison_or_read_model, "state", None)
    reasons = getattr(comparison_or_read_model, "reasons", None)
    if isinstance(comparison_or_read_model, dict):
        state = comparison_or_read_model.get("state")
        reasons = comparison_or_read_model.get("reasons")
    if isinstance(state, IndexFreshnessState):
        state_value = state.value
    else:
        state_value = state
    return state_value == IndexFreshnessState.STALE.value and bool(
        HARD_STALE_REASONS.intersection(reasons or ())
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_git_status_output(status_output: str) -> tuple[str, int]:
    """
    Normalize porcelain status output before hashing.

    The raw status output may contain user file paths and should not be exposed
    in API models or reprs. This helper returns normalized text only so callers
    can hash it immediately. Sorting makes the digest stable even if Git output
    ordering changes.
    """
    normalized_newlines = status_output.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized_newlines.split("\n") if line]
    lines.sort()
    return "\n".join(lines), len(lines)


def dirty_digest_from_status(status_output: str) -> tuple[str, int]:
    normalized, count = normalize_git_status_output(status_output)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest(), count


def _run_git(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _git_available() -> bool:
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


def _unknown_fingerprint(
    repo_path_resolved: str,
    *,
    git_available: bool,
    is_git_repo: bool,
    git_error: str,
    branch_name: str | None = None,
    branch_is_detached: bool = False,
    detached_head_label: str | None = None,
    head_sha: str | None = None,
) -> WorkingTreeFingerprint:
    return WorkingTreeFingerprint(
        repo_path_resolved=repo_path_resolved,
        is_git_repo=is_git_repo,
        git_available=git_available,
        branch_name=branch_name,
        branch_is_detached=branch_is_detached,
        detached_head_label=detached_head_label,
        head_sha=head_sha,
        dirty_digest=None,
        dirty_files_count=0,
        captured_at=_utc_now(),
        git_error=git_error,
    )


def compute_working_tree_fingerprint(repo_path: str | Path) -> WorkingTreeFingerprint:
    """
    Capture cheap Git checkout identity for a repo path.

    This does not scan file contents and does not use mtime for correctness. Raw
    `git status` output is hashed and never stored on the returned model.
    """
    try:
        requested_path = Path(repo_path).resolve()
    except Exception:
        requested_path = Path(str(repo_path))
    repo_path_resolved = str(requested_path)

    git_available = _git_available()
    if not git_available:
        return _unknown_fingerprint(
            repo_path_resolved,
            git_available=False,
            is_git_repo=False,
            git_error="git is unavailable",
        )

    if not requested_path.exists() or not requested_path.is_dir():
        return _unknown_fingerprint(
            repo_path_resolved,
            git_available=True,
            is_git_repo=False,
            git_error="repo path is missing or not a directory",
        )

    try:
        top_level = _run_git(requested_path, ["rev-parse", "--show-toplevel"])
    except Exception:
        return _unknown_fingerprint(
            repo_path_resolved,
            git_available=True,
            is_git_repo=False,
            git_error="git rev-parse failed",
        )
    if top_level.returncode != 0 or not top_level.stdout.strip():
        return _unknown_fingerprint(
            repo_path_resolved,
            git_available=True,
            is_git_repo=False,
            git_error="not a git repository",
        )

    try:
        git_root = Path(top_level.stdout.strip()).resolve()
    except Exception:
        git_root = requested_path
    repo_path_resolved = str(git_root)

    head = _run_git(git_root, ["rev-parse", "HEAD"])
    if head.returncode != 0 or not head.stdout.strip():
        return _unknown_fingerprint(
            repo_path_resolved,
            git_available=True,
            is_git_repo=True,
            git_error="git rev-parse HEAD failed",
        )
    head_sha = head.stdout.strip()

    branch = _run_git(git_root, ["branch", "--show-current"])
    if branch.returncode != 0:
        return _unknown_fingerprint(
            repo_path_resolved,
            git_available=True,
            is_git_repo=True,
            git_error="git branch --show-current failed",
            head_sha=head_sha,
        )

    branch_name = branch.stdout.strip() or None
    branch_is_detached = branch_name is None
    detached_head_label = (
        f"{DETACHED_HEAD_PREFIX}{head_sha[:12]}" if branch_is_detached else None
    )

    status = _run_git(
        git_root,
        ["-c", "core.quotepath=false", "status", "--porcelain", "-uall"],
    )
    if status.returncode != 0:
        return _unknown_fingerprint(
            repo_path_resolved,
            git_available=True,
            is_git_repo=True,
            git_error="git status failed",
            branch_name=branch_name,
            branch_is_detached=branch_is_detached,
            detached_head_label=detached_head_label,
            head_sha=head_sha,
        )

    dirty_digest, dirty_files_count = dirty_digest_from_status(status.stdout)
    return WorkingTreeFingerprint(
        repo_path_resolved=repo_path_resolved,
        is_git_repo=True,
        git_available=True,
        branch_name=branch_name,
        branch_is_detached=branch_is_detached,
        detached_head_label=detached_head_label,
        head_sha=head_sha,
        dirty_digest=dirty_digest,
        dirty_files_count=dirty_files_count,
        captured_at=_utc_now(),
        git_error=None,
    )


def compare_index_freshness(
    current: WorkingTreeFingerprint | None,
    stored: StoredIndexFingerprint | None,
    *,
    current_index_row_count: int | None = None,
) -> IndexFreshnessComparison:
    if stored is None:
        return IndexFreshnessComparison(
            state=IndexFreshnessState.MISSING,
            reasons=("missing_snapshot",),
            current=current,
            stored=stored,
        )
    if current is None or not current.is_known:
        return IndexFreshnessComparison(
            state=IndexFreshnessState.UNKNOWN,
            reasons=("current_fingerprint_unknown",),
            current=current,
            stored=stored,
        )
    if stored.snapshot_state != IndexFreshnessState.CURRENT.value:
        reasons = ["stored_snapshot_not_current"]
        if stored.snapshot_reason:
            reasons.append(stored.snapshot_reason)
        return IndexFreshnessComparison(
            state=IndexFreshnessState.UNKNOWN,
            reasons=tuple(reasons),
            current=current,
            stored=stored,
        )

    reasons: list[str] = []
    if current.repo_path_resolved != stored.repo_path_resolved:
        reasons.append("repo_path_mismatch")
    if current.branch_is_detached != stored.branch_is_detached:
        reasons.append("branch_detached_state_mismatch")
    if current.branch_name != stored.branch_name:
        reasons.append("branch_name_mismatch")
    if current.detached_head_label != stored.detached_head_label:
        reasons.append("detached_head_label_mismatch")
    if current.head_sha != stored.head_sha:
        reasons.append("head_sha_mismatch")
    if current.dirty_digest != stored.dirty_digest:
        reasons.append("dirty_digest_mismatch")
    if (
        current_index_row_count is not None
        and current_index_row_count != stored.index_row_count
    ):
        reasons.append("index_row_count_mismatch")

    if reasons:
        return IndexFreshnessComparison(
            state=IndexFreshnessState.STALE,
            reasons=tuple(reasons),
            current=current,
            stored=stored,
        )
    return IndexFreshnessComparison(
        state=IndexFreshnessState.CURRENT,
        reasons=(),
        current=current,
        stored=stored,
    )


def _snapshot_from_row(row) -> StoredIndexFingerprint:
    data = dict(row._mapping)
    return StoredIndexFingerprint(
        project_id=data["project_id"],
        repo_path_resolved=data["repo_path_resolved"],
        branch_name=data["branch_name"],
        branch_is_detached=bool(data["branch_is_detached"]),
        detached_head_label=data["detached_head_label"],
        head_sha=data["head_sha"],
        dirty_digest=data["dirty_digest"],
        dirty_files_count=int(data["dirty_files_count"] or 0),
        index_row_count=int(data["index_row_count"] or 0),
        captured_at=data["captured_at"],
        updated_at=data["updated_at"],
        snapshot_state=data.get("snapshot_state") or IndexFreshnessState.CURRENT.value,
        snapshot_reason=data.get("snapshot_reason"),
    )


def save_index_fingerprint_snapshot(
    project_id: str,
    fingerprint: WorkingTreeFingerprint,
    index_row_count: int,
    *,
    snapshot_state: IndexFreshnessState | str = IndexFreshnessState.CURRENT,
    snapshot_reason: str | None = None,
) -> StoredIndexFingerprint:
    if not project_id or not project_id.strip():
        raise RuntimeError("index_freshness.py: project_id is required")
    if index_row_count < 0:
        raise RuntimeError("index_freshness.py: index_row_count must be >= 0")
    if not fingerprint.is_known:
        raise RuntimeError(
            "index_freshness.py: cannot persist unknown working-tree fingerprint"
        )
    state_value = (
        snapshot_state.value
        if isinstance(snapshot_state, IndexFreshnessState)
        else str(snapshot_state)
    )
    if state_value not in {state.value for state in IndexFreshnessState}:
        raise RuntimeError("index_freshness.py: invalid snapshot_state")

    updated_at = _utc_now()
    params = {
        "project_id": project_id,
        "repo_path_resolved": fingerprint.repo_path_resolved,
        "branch_name": fingerprint.branch_name,
        "branch_is_detached": 1 if fingerprint.branch_is_detached else 0,
        "detached_head_label": fingerprint.detached_head_label,
        "head_sha": fingerprint.head_sha,
        "dirty_digest": fingerprint.dirty_digest,
        "dirty_files_count": fingerprint.dirty_files_count,
        "index_row_count": index_row_count,
        "captured_at": fingerprint.captured_at,
        "updated_at": updated_at,
        "snapshot_state": state_value,
        "snapshot_reason": snapshot_reason,
    }
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO project_index_fingerprints
            (project_id, repo_path_resolved, branch_name, branch_is_detached,
             detached_head_label, head_sha, dirty_digest, dirty_files_count,
             index_row_count, captured_at, updated_at, snapshot_state,
             snapshot_reason)
            VALUES
            (:project_id, :repo_path_resolved, :branch_name,
             :branch_is_detached, :detached_head_label, :head_sha,
             :dirty_digest, :dirty_files_count, :index_row_count,
             :captured_at, :updated_at, :snapshot_state, :snapshot_reason)
            ON CONFLICT(project_id) DO UPDATE SET
                repo_path_resolved = excluded.repo_path_resolved,
                branch_name = excluded.branch_name,
                branch_is_detached = excluded.branch_is_detached,
                detached_head_label = excluded.detached_head_label,
                head_sha = excluded.head_sha,
                dirty_digest = excluded.dirty_digest,
                dirty_files_count = excluded.dirty_files_count,
                index_row_count = excluded.index_row_count,
                captured_at = excluded.captured_at,
                updated_at = excluded.updated_at,
                snapshot_state = excluded.snapshot_state,
                snapshot_reason = excluded.snapshot_reason
        """), params)

    snapshot = get_index_fingerprint_snapshot(project_id)
    if snapshot is None:
        raise RuntimeError("index_freshness.py: saved snapshot could not be loaded")
    return snapshot


def get_index_fingerprint_snapshot(project_id: str) -> StoredIndexFingerprint | None:
    if not project_id or not project_id.strip():
        raise RuntimeError("index_freshness.py: project_id is required")

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT project_id, repo_path_resolved, branch_name,
                   branch_is_detached, detached_head_label, head_sha,
                   dirty_digest, dirty_files_count, index_row_count,
                   captured_at, updated_at, snapshot_state, snapshot_reason
            FROM project_index_fingerprints
            WHERE project_id = :project_id
        """), {"project_id": project_id}).fetchone()
    if row is None:
        return None
    return _snapshot_from_row(row)


def record_index_fingerprint_after_reindex(
    project_id: str,
    repo_path: str | Path,
    index_row_count: int,
) -> StoredIndexFingerprint:
    """
    Convenience helper for a future reindex wiring slice.

    Captures after the caller has rebuilt/saved file_index rows, so the stored
    identity is closer to the checkout that produced those rows. #34B does not
    call this from build_repo_index or routes.
    """
    fingerprint = compute_working_tree_fingerprint(repo_path)
    return save_index_fingerprint_snapshot(project_id, fingerprint, index_row_count)


def _fingerprint_identity_matches(
    left: WorkingTreeFingerprint,
    right: WorkingTreeFingerprint,
) -> bool:
    if not left.is_known or not right.is_known:
        return False
    return (
        left.repo_path_resolved == right.repo_path_resolved
        and left.branch_name == right.branch_name
        and left.branch_is_detached == right.branch_is_detached
        and left.detached_head_label == right.detached_head_label
        and left.head_sha == right.head_sha
        and left.dirty_digest == right.dirty_digest
    )


def _save_unknown_snapshot_if_known(
    project_id: str,
    fingerprint: WorkingTreeFingerprint,
    index_row_count: int,
    reason: str,
) -> StoredIndexFingerprint | None:
    if not fingerprint.is_known:
        return None
    return save_index_fingerprint_snapshot(
        project_id,
        fingerprint,
        index_row_count,
        snapshot_state=IndexFreshnessState.UNKNOWN,
        snapshot_reason=reason,
    )


def reindex_and_record(project_id: str, repo_path: str | Path) -> ReindexAndRecordResult:
    """
    Rebuild file_index through the pure indexer, then record freshness metadata.

    To avoid claiming freshness across a branch/dirty-state race, capture Git
    identity before and after the scan. If the identity changes, retry once. If
    it still changes, persist the final known identity as an unknown snapshot so
    future reads do not report it as current.
    """
    from backend.repo.repo_indexer import build_repo_index

    before = compute_working_tree_fingerprint(repo_path)
    index_result = build_repo_index(project_id, str(repo_path))
    files_indexed = int(index_result.get("files_indexed") or 0)
    after = compute_working_tree_fingerprint(repo_path)

    if _fingerprint_identity_matches(before, after):
        snapshot = save_index_fingerprint_snapshot(project_id, after, files_indexed)
        return ReindexAndRecordResult(
            index_result=index_result,
            snapshot=snapshot,
            state=IndexFreshnessState.CURRENT,
            reasons=(),
            before=before,
            after=after,
        )
    if not before.is_known or not after.is_known:
        reason = "fingerprint_unknown_during_scan"
        snapshot = _save_unknown_snapshot_if_known(
            project_id,
            after,
            files_indexed,
            reason,
        )
        return ReindexAndRecordResult(
            index_result=index_result,
            snapshot=snapshot,
            state=IndexFreshnessState.UNKNOWN,
            reasons=(reason,),
            before=before,
            after=after,
        )

    retry_before = after
    retry_result = build_repo_index(project_id, str(repo_path))
    retry_files_indexed = int(retry_result.get("files_indexed") or 0)
    retry_after = compute_working_tree_fingerprint(repo_path)
    if _fingerprint_identity_matches(retry_before, retry_after):
        snapshot = save_index_fingerprint_snapshot(
            project_id, retry_after, retry_files_indexed
        )
        return ReindexAndRecordResult(
            index_result=retry_result,
            snapshot=snapshot,
            state=IndexFreshnessState.CURRENT,
            reasons=(),
            before=retry_before,
            after=retry_after,
        )

    reasons = ("fingerprint_changed_during_scan",)
    snapshot = _save_unknown_snapshot_if_known(
        project_id,
        retry_after,
        retry_files_indexed,
        reasons[0],
    )
    return ReindexAndRecordResult(
        index_result=retry_result,
        snapshot=snapshot,
        state=IndexFreshnessState.UNKNOWN,
        reasons=reasons,
        before=retry_before,
        after=retry_after,
    )


def get_project_index_row_count(project_id: str) -> int:
    if not project_id or not project_id.strip():
        raise RuntimeError("index_freshness.py: project_id is required")
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT COUNT(*) FROM file_index
            WHERE project_id = :project_id
        """), {"project_id": project_id}).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def ensure_repo_indexed_and_record(
    project_id: str,
    repo_path: str | Path,
) -> dict:
    """
    Preserve lazy index behavior while stamping cold-start indexes.

    Existing non-empty indexes are left alone; missing snapshot on existing rows
    remains an unknown/bootstrap state that #34C surfaces without blocking.
    """
    from backend.repo.repo_indexer import ensure_repo_indexed

    if get_project_index_row_count(project_id) == 0:
        return reindex_and_record(project_id, repo_path).index_result
    return ensure_repo_indexed(project_id, str(repo_path))


def _short_sha(value: str | None) -> str | None:
    if not value:
        return None
    return value[:12]


def _current_summary(fingerprint: WorkingTreeFingerprint | None) -> dict | None:
    if fingerprint is None:
        return None
    return {
        "branch_name": fingerprint.branch_name,
        "detached": fingerprint.branch_is_detached,
        "detached_head_label": fingerprint.detached_head_label,
        "head_sha_short": _short_sha(fingerprint.head_sha),
        "dirty_files_count": fingerprint.dirty_files_count,
        "git_available": fingerprint.git_available,
        "is_git_repo": fingerprint.is_git_repo,
    }


def _stored_summary(snapshot: StoredIndexFingerprint | None) -> dict | None:
    if snapshot is None:
        return None
    return {
        "branch_name": snapshot.branch_name,
        "detached": snapshot.branch_is_detached,
        "detached_head_label": snapshot.detached_head_label,
        "head_sha_short": _short_sha(snapshot.head_sha),
        "dirty_files_count": snapshot.dirty_files_count,
        "index_row_count": snapshot.index_row_count,
        "captured_at": snapshot.captured_at,
        "updated_at": snapshot.updated_at,
        "snapshot_state": snapshot.snapshot_state,
    }


def get_project_index_freshness(project_id: str, repo_path: str | Path) -> dict:
    """
    Project-level freshness read model.

    This is intentionally separate from GET /projects/{id}/index so the existing
    count/age endpoint stays pure DB and cheap.
    """
    index_row_count = get_project_index_row_count(project_id)
    current = compute_working_tree_fingerprint(repo_path)
    stored = get_index_fingerprint_snapshot(project_id)

    if index_row_count == 0:
        state = IndexFreshnessState.MISSING
        reasons = ("missing_file_index",)
    elif stored is None:
        state = IndexFreshnessState.UNKNOWN
        reasons = ("missing_snapshot",)
    else:
        comparison = compare_index_freshness(
            current,
            stored,
            current_index_row_count=index_row_count,
        )
        state = comparison.state
        reasons = comparison.reasons

    return {
        "state": state.value,
        "reasons": list(reasons),
        "current": _current_summary(current),
        "indexed": _stored_summary(stored),
        "index_row_count": index_row_count,
        "has_index_rows": index_row_count > 0,
        "has_snapshot": stored is not None,
    }
