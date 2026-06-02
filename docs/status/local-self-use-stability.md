# Local Self-Use Stability — Status

**Status: local self-use reliability phase complete (demo-ready, not production SaaS).**

This page records that the local self-use reliability chain (#17 → #20B) is
complete and manually verified. It complements the earlier stabilization track
(PRs #1–#10) documented in
[`docs/stabilization/final-smoke-status.md`](../stabilization/final-smoke-status.md)
and the manual smoke checklist in
[`docs/testing/self-use-stability-smoke.md`](../testing/self-use-stability-smoke.md).

This is a status/docs page only — #20C changes no runtime code, tests, or schema.

---

## Completed reliability chain

| PR | Area | Outcome |
| --- | --- | --- |
| #17 | Ambiguous file clarification + selection UX | Ambiguous file targets surface a ranked candidate list with a signed `clarification_id`; a follow-up selection ("1" / "yes 1" / exact path) resolves only within that candidate set, never globally. |
| #18 | Patch failure recovery | A dirty worktree / patch failure surfaces a sanitized `PatchFailureBanner`; a failed chunk cannot be approved; the success path is unaffected. |
| #19 | Repo index refresh / stale-index recovery | Manual re-index endpoint + Project Settings UI; explicit on-disk-but-unindexed targets auto re-index once; post-commit index refresh so later requests see created/deleted files. |
| #20A | Self-use stability smoke checklist | Documented the combined #17–#19 manual smoke flow and the two bugs found during dogfood (see [`self-use-stability-smoke.md`](../testing/self-use-stability-smoke.md)). |
| #20B-1 | Case-mismatch clarification selection loop fix | A selected candidate path is now authoritative over a wrong-case alias in the original request (re-validated against the live index), so selecting `MANUAL.md` for a `manual.md` request no longer loops the clarification. |
| #20B-2 | GitHub PR remote base branch preflight | Before any push / `gh pr create` / `create_pull`, the base branch is verified to exist on the remote; a missing remote base now returns a clear recovery message instead of an opaque GitHub GraphQL error. |

---

## What is now verified

Verified by the manual quick smoke (and the automated unit suite,
`python -m pytest backend/tests -q -m unit`):

1. **Clarification selection** — `add hello bro in manual.md` → select `MANUAL.md`
   → the chunk plan opens (no second clarification loop).
2. **Normal edit path** — an exact-path edit proceeds through plan → execute →
   commit → final approval unchanged.
3. **PR creation (happy path)** — with `origin/pipewright-staging` present, push
   and PR creation succeed (reuses an existing PR when possible; never
   auto-merges).
4. **PR preflight (failure path)** — when `pipewright-staging` exists locally but
   not on origin, a clear preflight message is returned
   (`Base branch 'pipewright-staging' is not on 'origin'. Push it with:
   git push -u origin pipewright-staging`) — no opaque GraphQL error, no PR
   attempt.

Safety invariants from the earlier stabilization track (chunk-plan and final
approval gates, scope guard, no empty/no-effective-change commits, no PRs against
`main`/`master`/`develop`, default base `pipewright-staging`, token/secret
sanitization) remain in force and are unchanged by this phase.

---

## Known deferred items (intentionally out of scope)

- **File watcher / continuous auto-indexing** — every refresh is still manual or
  post-commit; there is no filesystem watcher.
- **Re-index-and-retry the failed chunk** — the `PatchFailureBanner` reindex
  action re-indexes only; it does not retry the failed chunk.
- **Incremental / touched-file-only index refresh** — every refresh is a full
  rebuild, not an incremental update of changed files.
- **Memory M2** — deterministic run-outcome suggestions (see Next milestone).
- **Broader PR preflight taxonomy** — #20B-2 added only the remote-base existence
  check. The wider taxonomy (remote head verification after push, origin-base
  commit comparison, etc.) is documented but deferred; no fetch/origin-base
  comparison and no auto-push of the base branch were added.

---

## Current known non-blocking issue

- **Frontend full-repo lint** — a full-repo `npm run lint` / `eslint` may still
  surface a **pre-existing, unrelated** `react-hooks/set-state-in-effect` error
  in `ProjectSettingsPanel.tsx` (predates this phase). It does not block the
  build (`npm run build` is clean) or local self-use, and no runtime behavior
  depends on it. Changed-file-scoped lint on the files touched in this phase was
  clean. (Confirm this still reproduces before acting on it.)

---

## Next milestone

1. **#20D** — Tag the stable local-self-use milestone.
2. **#21 Memory M2** — Deterministic run-outcome suggestions.
