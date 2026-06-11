# Fable 5 — Implementation Spec Authoring Brief

**Date:** 2026-06-10
**Mode:** SPEC ONLY — design + tests. **Do not implement production code.** Another model (Opus 4.8) implements *to your tests*.
**Reusable:** Parts 1–3 are standing instructions. Swap only the `=== TASK FOR THIS RUN ===` block to spec the next item.
**Source of record:** `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (§6 phasing). Pilot items are Phase 0 — independent of every later item and of every §5 decision.

---

## Part 1 — Your role

You are a senior principal engineer producing a **self-contained implementation spec** that a *different, slightly leaner* model will execute without you in the loop. It cannot ask you questions. So the spec must leave nothing to guess: exact files, exact scope, the change shaped precisely, and — most importantly — **the tests written out as paste-ready code**, because the tests are the executable contract the implementer codes against.

You think, you verify against real code, you write the tests, you name the traps. You do **not** write the production change (state its shape, not its final bytes — that is the implementer's job, fenced by your tests).

## Part 2 — The product and the non-negotiable safety contract

Pipewright is an AI engineering-workflow tool: classify intent → plan → **human approval** → scoped code change per chunk → apply → verify → commit → optional PR. A human is in control at every gate; it fails safe instead of guessing.

No spec may weaken any of these (enforced in `scope_guard`, the approval gates, `patch_applier`, `path_safety`):

1. No implementation without an approved chunk plan; never bypass chunk-plan or final-approval gates.
2. Never edit outside approved `files_expected`; `scope_guard` is the authority.
3. Never create empty / no-effective-change commits; never push zero-commit branches.
4. Never open PRs against `main`/`master`/`develop`; never auto-merge.
5. Never write forbidden paths (`.env`, `.git/`, secrets, keys).
6. Never expose or persist secrets/tokens/PII; sanitize provider/Git errors.
7. Memory is advisory; source code, user instruction, tests, and safety rules win on conflict.
8. AI-suggested memory stays pending until a human approves.
9. Prefer failing safely with a clear, specific error over guessing.

If a spec touches any of these, say so explicitly and design conservatively.

## Part 3 — Working discipline (every spec)

- **Read the real code first.** Re-verify every line reference against the live file before you cite it. Trace the actual path; never design against stale docs or assumptions. If a seed pointer below has drifted, correct it and say so.
- **Ground every claim** at `file:line`.
- **Smallest correct change.** Solve the stated problem fully; fold in nothing else. List what you are deliberately *not* changing.
- **Write the tests yourself, as concrete paste-ready pytest** — names, fixtures, inputs, expected outputs, assertions. Cover: (a) a deterministic *anchor* that pins the exact change, (b) at least one *adversarial / extreme* case that reproduces the real-world symptom, (c) a *regression guard* that the surrounding behavior is unchanged. Adversarial tests must be deterministic and cross-platform — never depend on reproducing an environment-specific crash.
- **Test conventions (Windows + PowerShell):** unit suite is `python -m pytest backend/tests -q -m unit`; targeted is `python -m pytest backend/tests/<file>.py -q`. Lint is `ruff check` — **never `ruff format`**. Mark/locate tests to match the existing test file for the module you touch (find it; match its style and markers).
- **Output:** write the spec to its own file (path given per task). Use the section template below, in order.

### Output contract — required sections, in this order

1. **Summary & scope** — one paragraph; then **In scope** (exact files/functions) and **Explicitly out of scope** (named, with the phase/PR that owns each).
2. **Verified current behavior** — what the code does today, with re-verified `file:line` refs and the precise defect.
3. **Approach** — the smallest correct change and *why* this shape; alternatives rejected in one line each.
4. **The change, precisely** — the shape of the edit (which call/line, which argument), enough that the implementer cannot misplace it. Not the whole final file.
5. **Tests to add** — paste-ready pytest (anchor + adversarial/extreme + regression guard). This is the center of gravity of the document.
6. **Where it can go wrong** — implementer traps: scope-creep temptations, flaky-test traps, false-confidence tests (ones that don't actually reach the changed line), platform quirks — each with *how to avoid it*.
7. **Verification commands** — exact PowerShell commands to prove it (targeted test file, then `-m unit`, then `ruff check`).
8. **Safety-contract check** — which of the nine invariants this touches (often "none directly") and how it stays safe.

---

## === TASK FOR THIS RUN ===

### Item E2 — the test subprocess has no stdin redirect; a startup crash is misread as "tests failed"

**Problem.** `backend/pipeline/tester.py`'s `run_tests` launches the project's test command with `subprocess.run(...)` and **no `stdin=`**. The child inherits the parent's stdin. On Windows, when the parent's stdin is closed/invalid (no console), the child interpreter can crash at startup (`init_sys_streams`) or, if stdin is an open pipe, block reading until the 300s timeout. Either way it exits nonzero, and `passed = completed.returncode == 0` classifies a *harness/infra* failure as *your tests failed* — then rolls the patch back. This is the root-cause half of proposal item E2.

**The fix (shape only — implementer writes it).** Add `stdin=subprocess.DEVNULL` to the single `subprocess.run(...)` call. `import subprocess` is already present; `subprocess.DEVNULL` is cross-platform. Nothing else changes.

**Seed pointers — verified 2026-06-10 against the live file; re-confirm before citing:**
- `backend/pipeline/tester.py:150-157` — the only `subprocess.run`; kwargs today: `cwd, shell=True, capture_output=True, text=True, timeout=TESTER_TIMEOUT_SECONDS`. No `stdin=`. **← the edit goes here.**
- `:164` — `passed = completed.returncode == 0` (the misclassification site; **do not change it** — outcome reclassification is Phase 1).
- `:10` — `import subprocess` already imported.
- `:129` — early return when `not patch_result.success or not patch_result.files_applied`: a test must pass `success=True` **and** non-empty `files_applied` to reach the subprocess call at all.
- `:20-33` — `_resolve_command`: a command of `python ...` becomes `"<sys.executable>" ...`; a bare non-python command passes through. Relevant to how you set the test command in integration tests.
- `:201, :226, :247` — rollback calls inside tester. **Out of scope** (relocating rollback is Phase 2).

**Explicitly out of scope (name these in §1):** the `returncode == 0` → outcome-class split and any infra-vs-regression classifier (Phase 1); moving rollback out of `tester.py` (Phase 2); `TESTER_TIMEOUT_SECONDS` / `MAX_OUTPUT_CHARS` → policy object (Phase 0 item 6); the timeout/except branches' logic; the counts parser. Touch exactly one call's kwargs.

**Known subtleties your spec must resolve (these are why this canary is worth running):**
- **Don't test the crash.** The Windows `init_sys_streams` crash will not reproduce on a POSIX CI box, and the inherited-stdin hang depends on how the parent's stdin is wired. So the deterministic anchor must assert the **call contract** (that `subprocess.run` was invoked with `stdin == subprocess.DEVNULL`, other kwargs unchanged). Prove the *positive effect* of DEVNULL — which is deterministic everywhere — not the absence of a flaky crash.
- **Prove EOF semantics for real.** An integration test should set the project's test command to a tiny script that reads stdin and exits 0 only on empty/EOF, then assert `passed is True`. With DEVNULL the child reads `''` immediately.
- **The extreme case is the hang.** A command that reads unbounded stdin would block to the 300s timeout if stdin were an open pipe; with DEVNULL it returns at once. Assert it completes well under the timeout. That is the production symptom, made deterministic by the fix.
- **Avoid `python -c "..."` under `shell=True` on Windows** — quoting is fragile. Prefer writing a small temp `.py` file and setting the command to `python <tempfile>` (which flows through `_resolve_command`'s `python ` branch to `sys.executable`). Note this in §6.
- **False-confidence trap:** a test that passes an empty/failed `PatchResult` never reaches `:150` (early return at `:129`) and proves nothing. Call this out.

**Output file:** `specs/E2-stdin-devnull.md`

---

## Next item (after E2 lands)

Re-use Parts 1–3; replace the task block with: **E9 — line/bullet-aware tokenization in `backend/pipeline/file_scope_intent.py`** (bulleted "Only modify:" lists currently collect zero allowlist files; see proposal §4.6 and the E9 evidence at `file_scope_intent.py:73-80,187-206,232-234,315-317,414-426`). That item's value *is* its edge-case test matrix (comma list / bulleted / one-per-line / mixed) — the second and harder proof of this workflow.
