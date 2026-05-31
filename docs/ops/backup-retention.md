# Backup Retention

## What is backend/backups/?

`backend/backups/` is Pipewright's patch-rollback scratch space. When the
pipeline applies changes to your target repository, it saves the original
versions of modified and deleted files before writing anything. If tests fail,
`patch_applier` uses these saved copies to restore your repository automatically.

The directory is gitignored and never appears in your commits. It exists only on
the local machine running Pipewright.

## When are backups created?

A backup directory is created each time `apply_patch` runs successfully:

```
backend/backups/
  <run_id>/                     # single-chunk run
    manifest.json               # [{path, action}, ...]
    original/
      src/app.py                # original copy of each modified/deleted file

  <run_id>/
    chunk_1/                    # multi-chunk run
      manifest.json
      original/...
    chunk_2/
      manifest.json
      original/...
```

Only `modify`, `delete`, and `edit` actions create backup entries. `create`
actions have no prior file to preserve.

## Why do backups grow?

Pipewright never automatically deletes backup directories. They accumulate over
time:

- One directory per pipeline run (plus chunk subdirectories for multi-chunk runs).
- Runs that fail or are rejected still leave their backup directories after
  automatic rollback completes.
- Unit tests also create backup directories in `backend/backups/` using random
  run IDs that do not appear in the production database.

## When are backups safe to delete?

After a run is in a terminal state (`complete`, `failed`, `rejected`,
`push_failed`, etc.) and old enough that you no longer need its debug material:

- Rollback, if it was going to happen, ran synchronously inside the pipeline
  before the run reached its terminal status.
- Git history is a better long-term record of what changed and how to recover it.

**Backups for active runs are never eligible for cleanup.** The cleanup script
always protects runs in non-terminal states.

## Manual cleanup

Use `scripts/cleanup_backups.py`. It is **dry-run by default** — it shows what
would be deleted without touching anything.

### Inspect first (dry-run)

```powershell
python scripts/cleanup_backups.py
```

```bash
python scripts/cleanup_backups.py
```

### Delete backups older than 14 days (default threshold)

```powershell
python scripts/cleanup_backups.py --delete
```

### Delete backups older than 30 days

```powershell
python scripts/cleanup_backups.py --older-than-days 30 --delete
```

### Keep only the 20 most recent backup directories

```powershell
python scripts/cleanup_backups.py --keep-last 20 --delete
```

### Verbose output — see why each directory is kept or skipped

```powershell
python scripts/cleanup_backups.py --verbose
python scripts/cleanup_backups.py --older-than-days 30 --delete --verbose
```

### Aggressive cleanup — remove all non-active backups regardless of age

```powershell
python scripts/cleanup_backups.py --older-than-days 0 --delete
```

## Active-run protection

The script queries the Pipewright database (`backend/db/pipewright.db`) to find
runs that are still in-progress or awaiting approval. Backup directories whose
`run_id` matches one of these runs are **always skipped**, even with
`--older-than-days 0`.

Protected statuses:

| Status | Reason |
|---|---|
| `running`, `running_chunks`, `started` | Actively executing |
| `awaiting_chunk_plan_approval` | Waiting for plan approval |
| `chunk_plan_approved`, `awaiting_chunk_approval`, `chunk_approved` | Mid-execution approval flow |
| `awaiting_final_approval`, `final_approved` | Waiting for final approval or push |
| `pushing` | Push in progress |
| `paused`, `interrupted` | May be resumed by startup recovery |

## Test artifacts

Unit tests create backup directories using random UUIDs that are not in the
production database. The cleanup script treats these as "unknown" (not in DB) and
makes them eligible by age. Running `python scripts/cleanup_backups.py --delete`
after a test session will clean these up along with old production backups — this
is the intended behavior.

## Safety guarantees

- The script resolves all paths and verifies each deletion target is inside
  `backend/backups/` before calling `shutil.rmtree`. It will never delete outside
  the backup root.
- Symlinked directories inside `backend/backups/` are skipped, not followed.
- Without `--delete`, the script is entirely read-only.
- Active runs are always protected regardless of the age threshold.

## Warnings

- **Backup directories contain original file content.** Deleting them removes the
  ability to manually inspect originals for debugging.
- **Git is the right long-term archive.** After a successful commit, `git log`,
  `git diff`, and `git show` provide everything you need to understand what the
  pipeline changed.
- **Always dry-run first.** `python scripts/cleanup_backups.py` is safe to run at
  any time and shows exactly what would be removed.
