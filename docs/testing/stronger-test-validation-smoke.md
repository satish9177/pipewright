# Stronger Test Validation Smoke Checklist

Manual smoke validation for the full **#28 Stronger Test Validation** flow
(backend + frontend). This is a checklist, not an automated suite — the frontend
has no test framework yet, so the UI steps are manual and complement the focused
backend tests listed below.

Related design doc:
[`docs/design/stronger-test-validation.md`](../design/stronger-test-validation.md).

## Completed Phases

- #28A — design / prep audit (merged)
- #28B — pure runtime test-evidence classifier (merged)
- #28C — tester output parsing / truncation fix (merged)
- #28D — persist runtime test verdict, display-only (merged)
- #28E — API + frontend runtime test-validation surfacing (merged)
- #28F — backend final-approval weak/no-test acknowledgement gate (merged)
- #28G — frontend / read-model acknowledgement UI (merged)
- #28H — this smoke checklist (docs only)

## 1. Purpose

Pipewright's safety model historically treated **"the configured test command
exited 0"** as **"tests passed."** That is dishonest whenever a command exits 0
while validating nothing (`python --version`, `pytest` collecting 0 items, an
`npm test` stub, deleted/undiscovered tests).

#28 prevents this **fake-green** validation by joining two signals
deterministically:

- **Command intent** (the string classifier:
  [`classify_test_command`](../../backend/pipeline/test_command_quality.py)) —
  weak / likely-test / unknown.
- **Runtime execution evidence** — exit code plus best-effort parsed counts and
  zero-test markers from the command's captured output.

The join yields a per-chunk **verdict** — `strong` / `weak` / `none` /
`unknown` — surfaced read-only on the run review. For `weak` / `none`, final
approval requires an explicit **human acknowledgement bound to the current diff
hash** before any commit / push / PR. The acknowledgement is a *precondition on*
the existing final gate, never a replacement for it.

This is strictly **additive and orthogonal** to #26 (Patch Failure Recovery v2)
and #27 (Scope Expansion Recovery). It changes no scope, approval, or rollback
behavior those slices own.

## 2. Safety guarantees to verify

- Weak commands like `python --version` are **not** treated as strong validation.
- A recognized test runner that ran **zero tests** is `weak` (collected 0 / no
  tests ran / total == 0).
- Unknown custom commands are **not** falsely called weak — they are `unknown`,
  informational only.
- `strong` test runs require **no** acknowledgement and add zero friction.
- `weak` / `none` runs require acknowledgement **before** final approval.
- Acknowledgement is **diff-hash-bound** (tied to the exact code it covers).
- A **stale** acknowledgement after a retry / diff change does **not** satisfy
  final approval; a fresh acknowledgement is re-required.
- The **backend remains the source of truth** even if frontend state is stale —
  the gate enforces server-side regardless of what the UI shows.
- **Chunk approval is never blocked** by test-validation quality.
- Test **pass/fail remains exit-code based**; #28 only acts on the success path.
- **No auto-rollback / auto-fail** is introduced for weak tests. Weak is not
  failure.
- **No #26 / #27 behavior changes.**

## 3. Backend validation commands

Run from the repo root. These are the focused suites exercised across the #28
implementation:

```powershell
python -m pytest backend/tests/test_test_run_validation.py backend/tests/test_test_command_quality.py -q
python -m pytest backend/tests/test_tester.py -q
python -m pytest backend/tests/test_chunk_test_run_verdict_persistence.py -q
python -m pytest backend/tests/test_test_validation_ack_gate.py -q
python -m pytest backend/tests/test_chunk_ack_read_model.py -q
python -m pytest backend/tests/test_chunk_routes.py -q
```

Full unit suite (broad regression sweep — confirms #26/#27 stay green):

```powershell
python -m pytest backend/tests -q -m unit
```

**Known pre-existing warning:** a pytest cache warning like
`WinError 5 ... .pytest_cache` may still appear on this machine. It is a
pre-existing hardcoded-cache-path quirk, unrelated to #28, and does not affect
pass/fail.

## 4. Frontend validation commands

```powershell
cd frontend
npm.cmd run build
npm.cmd run lint
```

`npm.cmd run build` runs `tsc -b` (type-check) then `vite build`.

`npm.cmd run lint` may still report **5 pre-existing errors in untouched files**
(baseline). The expected check is that the #28 touched files
(`RuntimeTestValidationBanner.tsx`, `TestValidationAckPanel.tsx`,
`RunDetailPage.tsx`, `ChunkPlanPanel.tsx`, `api/client.ts`) add **no new** lint
errors. Verify the error count has **not increased** rather than expecting zero.

## 5. Manual smoke setup

Keep it practical — no special fixtures are required.

1. Start the backend (`http://localhost:8001`) and the frontend dev server.
2. Use a tiny git project/repo with a clean working tree, configured in
   Pipewright with a chosen **test command** (Project Settings).
3. The verdict is computed on a chunk's **successful** test run, so each case
   below configures a different test command, then runs a small successful change.

Smoke cases to cover:

- **weak command:** `python --version` (exits 0, runs no tests).
- **strong command:** `python -m pytest` (or any real passing test command) in a
  repo that has at least one real, passing test.
- **zero-test command (if easy):** `pytest` in a repo with **zero** test files
  (exits 0, collected 0 items).
- **unknown command (if practical):** a custom script Pipewright does not
  recognize (e.g. `./run-ci.sh`).
- **stale acknowledgement:** acknowledge a weak verdict, then make / retry a
  changed diff and confirm the acknowledgement becomes stale.

If you cannot easily induce a given runtime state, the focused backend suites in
§3 exercise the classifier, persistence, ack gate, and read-model
deterministically.

## 6. Manual smoke: weak command acknowledgement flow

Steps:

1. Configure the project test command as `python --version`.
2. Run a small successful change through the chunked flow.

Expected:

- The **RuntimeTestValidationBanner** shows **weak** (amber).
- The reason explains a version command is not meaningful tests.
- The final-approval area shows the **TestValidationAckPanel**
  ("Weak test validation requires acknowledgement before final approval").
- Approve Final is **pre-disabled**, **or** the backend returns **409** if
  attempted, until acknowledgement.
- Click **Acknowledge weak test validation** for the affected chunk.
- The acknowledgement status becomes **current**.
- Approve Final then succeeds.
- **No commit occurs before final approval.**

## 7. Manual smoke: no-test / zero-test flow

Steps:

1. Configure a test command that runs **zero tests**, e.g. `pytest` in a repo with
   no test files available.

Expected:

- The chunk can still **pass** if the exit code is 0 (zero tests is not failure).
- The runtime verdict is **weak** / **none** per classifier behavior
  (a blank command → `none`; a test runner that collected 0 → `weak`).
- Final approval **requires acknowledgement**.
- Acknowledgement then allows final approval.
- Zero tests do **not** auto-fail the run.

## 8. Manual smoke: strong test flow

Steps:

1. Configure a real passing test command (e.g. `python -m pytest`).

Expected:

- The **RuntimeTestValidationBanner** shows **strong** (quiet/positive green).
- Passed counts are shown if parseable.
- **No** acknowledgement panel is rendered.
- Approve Final is available normally — zero friction, unchanged from today.

## 9. Manual smoke: unknown command flow

Steps:

1. Configure an unrecognized custom script if practical (e.g. `./run-ci.sh`).

Expected:

- The runtime validation shows **unknown** ("could not confirm whether this
  command ran real tests").
- The UI does **not** falsely call it weak.
- **No** acknowledgement is required in v1.
- The human can still review the captured output manually.

## 10. Manual smoke: stale acknowledgement flow

Steps:

1. Run weak validation (e.g. `python --version`) and reach the ack panel.
2. **Acknowledge** the weak validation (status becomes current).
3. Trigger a retry / recovery or otherwise create a **new changed diff / test
   checkpoint** for that chunk (e.g. a #26 retry or #27 scope amendment).

Expected:

- The previous acknowledgement becomes **stale** (its bound diff hash no longer
  matches the current diff).
- The UI shows a **stale / missing** acknowledgement and re-requires a fresh one
  ("A previous acknowledgement is stale because this chunk's diff changed").
- Approve Final is **blocked** until a fresh acknowledgement is recorded.
- The backend returns **409** if final approval is attempted while the
  acknowledgement is stale.

## 11. Frontend UI checklist

- **RuntimeTestValidationBanner** appears per chunk on the run review.
- **strong** state is quiet / positive (green, no escalation).
- **weak** / **none** state is an **amber** warning card with the backend reason.
- **unknown** state is neutral / manual-review copy ("could not confirm"), never
  an accusation of weakness.
- **TestValidationAckPanel** appears **only** when a chunk's verdict requires
  acknowledgement and it is **missing** or **stale**.
- The **Acknowledge** button calls the backend acknowledge route and refreshes
  run / chunks / gates (existing invalidation conventions).
- Approve Final is **pre-disabled** when a missing / stale required
  acknowledgement exists.
- **Reject Final** remains available.
- The backend **409** message ("weak test validation requires acknowledgement")
  is visible (no raw JSON) if final approval is attempted without a current
  acknowledgement.
- After acknowledging, the panel shows the chunk as acknowledged for the current
  diff.

## 12. Known limitations / deferred work

- **Unknown custom commands do not require acknowledgement in v1** — Pipewright
  accepts a miss (a genuinely weak custom script slipping through as `unknown`)
  over a false alarm against a real custom suite.
- **No LLM classification** — all gating is deterministic.
- **No coverage-percentage threshold enforcement.**
- **No non-pytest count parsers** beyond what already ships (jest / go / cargo /
  mocha count parsing is deferred unless already added).
- **No silent docs-only auto-exemption** — a docs change still surfaces the honest
  framing and lets the human own committing.
- **No automatic test-command recommendation** — Pipewright never edits or
  suggests rewriting the project's test command.
- **A per-command lint / typecheck / build / smoke taxonomy is deferred** — for
  v1 these collapse to "not confirmed tests" and earn no `strong` verdict.
- **The frontend has no test framework yet**, so the UI steps here are manual.

## 13. Closeout criteria

#28 can be considered complete when:

- The focused backend tests in §3 pass.
- `npm.cmd run build` passes (frontend type-check + build), with no new lint
  errors beyond the pre-existing baseline.
- The manual **weak command** smoke passes (banner shows weak, ack required, final
  approval blocked until acknowledged).
- The manual **strong test** smoke passes (banner shows strong, no ack panel,
  final approval normal).
- The manual **stale acknowledgement** smoke passes (where practical): a changed
  diff invalidates the prior acknowledgement and re-requires one.
- Final approval is **blocked before any commit** when a `weak` / `none`
  acknowledgement is missing or stale.
- The #26 / #27 regression tests still pass.
