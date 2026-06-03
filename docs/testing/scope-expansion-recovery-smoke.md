# Scope Expansion Recovery Smoke Checklist

Manual smoke validation for the full #27 Scope Expansion Recovery flow (backend +
frontend). This is a checklist, not an automated suite — the frontend has no test
framework yet, so the UI steps are manual and complement the focused backend
tests listed below.

Related design doc: [`docs/design/scope-expansion-recovery.md`](../design/scope-expansion-recovery.md).

## Completed Phases

- #27A design/prep audit
- #27B pure scope expansion foundations (models, lifecycle, eligibility, validation, denylist, effective-scope merge)
- #27C persistence + effective-scope overlay foundations
- #27D request creation on eligible clean SCOPE_VIOLATION
- #27 reject / manual-intervention route
- #27E approve-and-retry backend path
- #27F frontend scope expansion approval UI
- #27G this smoke checklist (docs only)

## 1. Purpose

When a chunk fails with an **eligible, clean** `SCOPE_VIOLATION` (the attempt
tried to touch files outside the approved scope, the working tree is clean, and
no manual intervention is required), #27 lets a human recover the chunk without
weakening safety:

1. A single pending `scope_expansion_request` is created for the chunk.
2. The **run** is surfaced as `awaiting_scope_approval`; the **chunk stays
   `failed`**.
3. The human can **reject** (records the decision, settles the run back to
   `failed`, nothing retried/committed) or **approve and retry** (re-drives the
   chunk under an amended file allowlist).
4. A successful amended retry pauses at `awaiting_chunk_approval` — the existing
   chunk-approval path is still the only thing that commits.

Scope approval is **not** code approval. It only authorizes retrying the chunk
with the listed files added to the allowed scope.

## 2. Safety guarantees to verify

- `chunks.files_expected` (raw/stored) is **never** auto-expanded.
- `scope_guard` is **never** weakened.
- A **dirty-tree** or **manual-intervention** `SCOPE_VIOLATION` does **not** offer
  scope approval (no pending request is created).
- A **pending** scope request does **not** affect effective scope.
- Only an **approved/applied** request affects effective scope, and only through
  the single overlay site `chunk_store.get_chunk_plan_status` (original UNION
  approved files).
- **Reject** never retries and never commits.
- **Approve** verifies the branch (verify-only) **before** any mutation.
- **Approve/retry** never commits.
- A successful amended retry pauses at `awaiting_chunk_approval`.
- The final-approval path remains separate and unchanged.
- The #26 **public** retry route still rejects `SCOPE_VIOLATION`
  (`disallowed_failure_type`). #27 does not route through the #26 front door.

## 3. Backend validation commands

Run from the repo root. These are the focused suites exercised during the #27
implementation:

```powershell
python -m pytest backend/tests/test_scope_expansion.py -q
python -m pytest backend/tests/test_scope_expansion_store.py -q
python -m pytest backend/tests/test_scope_expansion_request_creation.py -q
python -m pytest backend/tests/test_scope_expansion_reject_route.py -q
python -m pytest backend/tests/test_scope_expansion_approve_route.py -q
python -m pytest backend/tests/test_chunk_retry_route.py backend/tests/test_patch_failures.py -q
```

Full unit suite (broad regression sweep):

```powershell
python -m pytest backend/tests -q -m unit
```

**Known pre-existing warning:** a pytest cache warning like
`WinError 5 ... C:\Users\Hp\.pytest_cache` may appear. This is a pre-existing
hardcoded-cache-path quirk on this machine, unrelated to #27, and does not affect
pass/fail.

## 4. Frontend validation commands

```powershell
cd frontend
npm.cmd run build
npm.cmd run lint
```

`npm.cmd run build` runs `tsc -b` (type-check) then `vite build`.

`npm.cmd run lint` currently reports **5 pre-existing errors in untouched files**
(baseline): `Layout.tsx`, `ProjectSettingsPanel.tsx`, `ui/badge.tsx`,
`ui/button.tsx`, `useRunEvents.ts`. The scope-expansion files
(`ScopeExpansionBanner.tsx`, `ChunkPlanPanel.tsx`, `RunDetailPage.tsx`,
`api/client.ts`) add **no new** lint errors. Lint does **not** currently pass
clean because of that pre-existing baseline; verify the count has not increased
rather than expecting zero.

## 5. Manual smoke setup

Keep it practical — no special fixtures are required.

1. Start the backend (`http://localhost:8001`) and the frontend dev server.
2. Use a tiny git project/repo with a clean working tree.
3. Start a run and approve a chunk plan whose chunk's approved `files_expected`
   **intentionally excludes** one file the implementation will try to touch, e.g.:
   - approved `files_expected`: `["src/app.py"]`
   - attempt also edits: `["src/utils/math.py"]`
4. Execute the chunk. The coder touching the out-of-scope file with a clean tree
   should produce a clean `SCOPE_VIOLATION` and a pending scope request.

If you cannot easily induce a real `SCOPE_VIOLATION`, the focused backend suites
in §3 already exercise request creation, reject, approve-and-retry, eligibility,
and the read-only overlay deterministically.

## 6. Manual smoke: request creation

After the chunk fails with a clean, eligible `SCOPE_VIOLATION`:

- Run status becomes `awaiting_scope_approval`.
- The chunk stays `failed`.
- `pending_scope_expansion` appears on that chunk's status (chunk-plan payload),
  carrying `request_id`, `failure_report_id`, and `requested_files`.
- `requested_files` contains the extra file (e.g. `src/utils/math.py`).
- The normal #26 **Retry** button is **suppressed** for this chunk.
- The amber **ScopeExpansionBanner** ("Scope expansion required") appears.
- No commit occurs.
- `chunks.files_expected` raw/original scope is unchanged (still `["src/app.py"]`),
  and `get_chunk_plan_status` reports the original scope (pending does not overlay).

## 7. Manual smoke: reject path

Steps:

1. In the banner, click **Reject scope expansion** (optionally add a reason).

Expected:

- The request transitions to `rejected`.
- The run settles back to `failed`.
- The chunk remains `failed`.
- Dependent chunks remain blocked.
- No retry starts.
- No commit occurs.
- Effective scope remains the original `files_expected`.
- The UI refreshes (run/chunks/gates) and no longer looks like it is awaiting
  scope approval.

Edge: a second reject of the same request returns **409** (not pending); the UI
shows the backend message.

## 8. Manual smoke: approve path

Steps:

1. Trigger another eligible clean `SCOPE_VIOLATION` (the prior reject does not
   leave a pending request).
2. In the banner, click **Approve scope expansion and retry**.

Expected:

- The branch is **verified first** (verify-only pre-check before any mutation).
- The request moves `pending → approved`, and `approved → applied` once the retry
  actually runs.
- The retry executes under the **effective scope** (original UNION approved
  files), so the previously out-of-scope file is now in scope.
- On success, the chunk pauses at `awaiting_chunk_approval`.
- The recovered-patch review / normal chunk-approval UI appears.
- **No commit occurs** until final chunk approval through the existing path.

## 9. Manual smoke: failure / error cases

- **Wrong / detached branch:** approve returns **409**; the request remains
  `pending`; no retry runs; the banner shows the message.
- **Stale request / double action:** a stale `request_id` or acting on a
  non-pending request returns **409** (or a clear backend error); nothing mutates.
- **Forbidden / high-risk requested files:** either no scope request is created
  (all candidate extras filtered out by the denylist at creation time) or, if
  forbidden files are submitted to approve, validation rejects with **422**
  (`approved_file_forbidden` / `approved_file_invalid_path` /
  `approved_file_not_in_requested`).
- **Dirty tree / manual_intervention:** no scope request is created; this is a
  manual-intervention situation only, and no scope-approval UI is offered.
- **Retry re-fails:** the UI refreshes and shows the new `failed` state; a fresh
  eligible failure may surface a new pending request (subject to
  `MAX_SCOPE_AMENDMENTS`). Approval success never implies completion.

## 10. Frontend UI checklist

- The amber **"Scope expansion required"** banner appears for a pending request.
- Copy says the previous attempt **"tried to touch"** these files — **not** that
  the files are "required".
- Copy clearly states **scope approval is not code approval**.
- Requested files are listed.
- **Approve** sends `requested_files` **exactly** (no editing/adding files, no
  directory/glob approval in the UI).
- **Reject** works (optional reason input).
- Both buttons disable while an action is pending.
- Backend **409/422** messages are visible in the banner (no raw JSON).
- The normal #26 **Retry** is not shown as the primary action while a scope
  expansion is pending.
- After approve/reject, run/chunk/gates query data refreshes (existing
  invalidation conventions).

## 11. Known limitations / deferred work

- `MAX_SCOPE_AMENDMENTS = 1` in v1 — at most one human-approved amendment per
  chunk; beyond that the chunk falls back to manual intervention.
- No directory/glob approval — approvals are concrete per-file only.
- No high-risk path approval in v1 (denylist covers `.env*`, secrets, `.git/*`,
  lockfiles, `Dockerfile`/compose, `pyproject.toml`, `.github/workflows/*`,
  migrations, `alembic/versions/*`, etc.).
- No frontend editing of approved files (v1 approves the requested set verbatim).
- No semantic migration/workflow approval flow.
- Dedicated manual-intervention UX may improve later.
- This checklist is manual because the frontend has no test framework yet.

## 12. Closeout criteria

#27 can be considered complete when:

- The focused backend tests in §3 pass.
- `npm.cmd run build` passes (frontend type-check + build), with no new lint
  errors beyond the pre-existing baseline.
- The manual **reject** smoke passes (request rejected, run settles to `failed`,
  no retry/commit).
- The manual **approve** smoke reaches `awaiting_chunk_approval` **without** a
  commit.
- The dirty-tree / manual-intervention smoke does **not** offer scope approval.
- The #26 public-retry regression still rejects `SCOPE_VIOLATION`.
