# Spec E2 — `stdin=subprocess.DEVNULL` for the tester subprocess

**Date:** 2026-06-11
**Source:** `FABLE5_IMPL_SPEC_BRIEF.md` task block; `PIPEWRIGHT_REDESIGN_PROPOSAL.md` §6 Phase 0, item E2.
**Mode:** Spec only. The implementer makes the production edit described in §4 and pastes the tests in §5 verbatim.
**Seed-pointer audit:** every `file:line` below was re-verified against the live files on 2026-06-11. Zero drift from the brief's seeds.

---

## 1. Summary & scope

`run_tests` in `backend/pipeline/tester.py` launches the project's test command via `subprocess.run(...)` with no `stdin=` argument, so the child inherits the parent process's stdin. When Pipewright runs as a server (no console, stdin closed or invalid), the child Python interpreter can crash at startup (`init_sys_streams`) or block reading an open inherited pipe until the 300-second timeout. Both exit nonzero, and `passed = completed.returncode == 0` then misreports a harness/infrastructure failure as "your tests failed" — and rolls the patch back. The fix is one inserted kwarg: `stdin=subprocess.DEVNULL` on the single `subprocess.run` call. The child then reads immediate EOF, deterministically, on every platform.

**In scope**

- `backend/pipeline/tester.py` — the `subprocess.run(...)` call inside `run_tests` (`tester.py:150-157`). Exactly one inserted line.
- `backend/tests/test_tester.py` — five new tests plus one helper and one `import os`, appended to the existing file (§5).

**Explicitly out of scope** (each named with its owner)

- The `passed = completed.returncode == 0` semantics at `tester.py:164`, and any infra-vs-regression outcome classifier — **Phase 1** (outcome reclassification). Do not touch line 164.
- Relocating the rollback calls out of `tester.py` (`tester.py:201`, `:226`, `:247`) — **Phase 2**.
- Extracting `TESTER_TIMEOUT_SECONDS` / `MAX_OUTPUT_CHARS` (`tester.py:16-17`) into a policy object — **Phase 0, item 6**.
- The `TimeoutExpired` and generic-exception branches' logic (`tester.py:218-261`) — unchanged.
- `_parse_test_counts` (`tester.py:80-101`) and the rest of the output pipeline — unchanged.

Touch exactly one call's kwargs. Nothing else in production code.

## 2. Verified current behavior

All references re-verified 2026-06-11 against the live `backend/pipeline/tester.py` (261 lines, read in full):

- `tester.py:150-157` — the **only** `subprocess.run` in the file (grep-confirmed). Today's exact call:

  ```python
  completed = subprocess.run(
      resolved_command,
      cwd=target_repo_path,
      shell=True,
      capture_output=True,
      text=True,
      timeout=TESTER_TIMEOUT_SECONDS
  )
  ```

  The command is positional; kwargs are `cwd`, `shell`, `capture_output`, `text`, `timeout`. **No `stdin=`.** `capture_output=True` redirects only stdout/stderr; stdin is inherited.
- `tester.py:164` — `passed = completed.returncode == 0`. This is where an infra crash becomes "tests failed". Out of scope; the fix removes the *self-inflicted* infra failure mode so this line stops being fed garbage.
- `tester.py:10` — `import subprocess` already present. No import change needed; `subprocess.DEVNULL` is cross-platform (opens `nul` on Windows, `/dev/null` on POSIX).
- `tester.py:129-139` — early return when `not patch_result.success or not patch_result.files_applied`. A test must supply `success=True` **and** non-empty `files_applied` to reach line 150 at all.
- `tester.py:20-33` — `_resolve_command`: a command starting `python ` is rewritten to `"<sys.executable>" <rest>`; anything else passes through unchanged. The tests in §5 lean on both branches.
- `tester.py:16` — `TESTER_TIMEOUT_SECONDS = 300`: the bound the inherited-stdin hang runs into.
- `tester.py:201`, `:226`, `:247` — rollback on the failed/timeout/exception paths. So today, an stdin-induced infra failure **also rolls back a possibly good patch**.
- `tester.py:185-194` — on the passed path, `save_checkpoint` writes to the (test-isolated) SQLite DB; existing passing-path tests run it unmocked (`backend/tests/test_tester.py:72-97`), and `backend/tests/conftest.py:25-27` points the engine at a throwaway temp DB for the whole session.

**The defect, precisely:** with no `stdin=`, the child's stdin is whatever the parent has. Two interpreter-level facts make this environment-dependent and nasty:

1. On Windows, when any of stdout/stderr are redirected (they are: `capture_output=True`) and stdin is not, CPython's `subprocess` fills the child's stdin from the **process standard handle** (`GetStdHandle(STD_INPUT_HANDLE)`) — not from CRT fd 0. A console-less or stdin-closed parent (the FastAPI server, a service, a scheduler) hands the child an invalid/absent handle, and the child interpreter can fail in `init_sys_streams` before running a single test.
2. If the inherited stdin is instead an open pipe whose writer never closes, any test command that reads stdin blocks until the 300 s timeout (`tester.py:218`).

Either way `returncode != 0` → `passed=False` at `:164` → rollback at `:201`/`:226`. A harness defect is reported as a test regression.

Test-file conventions (verified): `backend/tests/test_tester.py:20` sets `pytestmark = pytest.mark.unit` module-wide; `:23-32` defines `make_patch_result(run_id)` (success=True, `files_applied=["test.py"]`); `:282-285` defines `_set_command(monkeypatch, tmp_path, command)` which monkeypatches `keys.settings.test_command` and `keys.settings.target_repo_path` (the fallback path `project_context.py:49-55` resolves, since no active project ContextVar is set in tests). New tests appended to this file inherit the `unit` marker.

## 3. Approach

Add `stdin=subprocess.DEVNULL` to the single `subprocess.run` call. That is the entire production change. The child gets the null device: reads return EOF instantly, the interpreter always has a valid stream at startup, and behavior stops depending on how the parent process was launched. The import already exists; the constant is cross-platform; no behavior on the success, failure, timeout, or exception paths changes for any test command that does not read stdin — and a command that *does* read stdin now gets deterministic EOF instead of a crash, a hang, or accidental console input.

Alternatives rejected:

- `input=""` — also yields EOF but switches stdin to `PIPE` and routes through `communicate()` write/close machinery; allocates a pipe for nothing and states the intent ("no stdin") less directly.
- `stdin=subprocess.PIPE` with no input — `subprocess.run` happens to close it immediately, but it reads as "we intend to send input", and a future refactor to raw `Popen` would silently re-create the hang.
- Reclassifying infra vs. test failure at `:164` — correct goal, wrong PR; that is Phase 1 and needs the outcome-class design.

## 4. The change, precisely

File: `backend/pipeline/tester.py`. In `run_tests`, insert **one line** into the `subprocess.run` call at `tester.py:150-157`, after `text=True,` (line 155) and before `timeout=TESTER_TIMEOUT_SECONDS` (line 156):

```python
        completed = subprocess.run(
            resolved_command,
            cwd=target_repo_path,
            shell=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TESTER_TIMEOUT_SECONDS
        )
```

The diff is exactly one inserted line: `            stdin=subprocess.DEVNULL,` (12-space indent, trailing comma, matching the surrounding kwarg-per-line style). No import changes (`tester.py:10`). No other file, function, line, or kwarg changes. The anchor test pins the full kwarg set, so any extra "while I'm here" edit to this call fails the suite.

## 5. Tests to add

Append the following to the end of `backend/tests/test_tester.py` (after `test_stderr_only_output_is_captured`, currently line 339). Also add `import os` to the module's import block — place it before `import uuid` at `test_tester.py:9`. Everything else used below (`uuid`, `pytest`, `run_tests`, `make_patch_result`, `_set_command`, the module-level `unit` marker) already exists in the file.

```python
# --------------------------------------------------------------------------
# E2 — stdin=DEVNULL: the parent's stdin must never leak into the test
# subprocess. The anchor pins the subprocess.run call contract; the probe
# tests prove EOF semantics and inheritance override with real subprocesses.
# Probes are written into the fake repo (tmp_path) and referenced by BARE
# FILENAME: run_tests executes with cwd=target_repo_path, so the relative
# name resolves there and no absolute path ever needs quoting under
# shell=True on Windows.
# --------------------------------------------------------------------------


def _write_probe(tmp_path, name: str, body: str) -> None:
    (tmp_path / name).write_text(body, encoding="utf-8")


def test_subprocess_run_called_with_stdin_devnull_and_contract_unchanged(
    monkeypatch, tmp_path
):
    """
    Anchor: run_tests must call subprocess.run with stdin=subprocess.DEVNULL
    and leave the rest of the call contract exactly as it was. This is the
    red/green pin for E2 — it fails on the pre-fix call in every environment,
    independent of how the test harness's own stdin is wired.
    """
    import subprocess
    import backend.pipeline.tester as tester

    _set_command(monkeypatch, tmp_path, "echo anchor")

    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="1 passed in 0.01s", stderr=""
        )

    monkeypatch.setattr(tester.subprocess, "run", fake_run)
    monkeypatch.setattr(tester, "save_checkpoint", lambda **kwargs: {"id": "x"})

    run_id = str(uuid.uuid4())
    result = run_tests(make_patch_result(run_id), run_id)

    assert result.passed is True
    # The new kwarg: stdin is redirected to the null device.
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    # Everything else about the call is unchanged by E2.
    assert captured["args"] == ("echo anchor",)
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["shell"] is True
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["timeout"] == tester.TESTER_TIMEOUT_SECONDS
    assert set(captured["kwargs"]) == {
        "cwd", "shell", "capture_output", "text", "timeout", "stdin"
    }


def test_devnull_gives_stdin_reading_command_immediate_eof(
    monkeypatch, tmp_path
):
    """
    EOF semantics, proven with a real subprocess: a test command that reads
    stdin and exits 0 only on empty input must be classified passed, because
    DEVNULL makes the read return '' immediately.
    """
    _write_probe(
        tmp_path,
        "read_stdin_probe.py",
        "import sys\n"
        "data = sys.stdin.read()\n"
        "print('1 passed' if data == '' else '1 failed')\n"
        "sys.exit(0 if data == '' else 1)\n",
    )
    _set_command(monkeypatch, tmp_path, "python read_stdin_probe.py")

    run_id = str(uuid.uuid4())
    result = run_tests(make_patch_result(run_id), run_id)

    assert result.passed is True
    assert result.passed_tests == 1


def test_unbounded_stdin_read_completes_well_under_timeout(
    monkeypatch, tmp_path
):
    """
    The extreme case — the production hang: an unbounded sys.stdin.read()
    blocks to the 300s TESTER_TIMEOUT_SECONDS if the child inherits an open
    stdin. With DEVNULL it returns at once. The 60s bound is generous
    (interpreter startup is ~1s) so slow CI cannot flake it, while the
    pre-fix hang path returns passed=False after ~300s and fails every
    assert here.
    """
    _write_probe(
        tmp_path,
        "drain_stdin_probe.py",
        "import sys\n"
        "sys.stdin.read()\n"
        "print('1 passed in 0.01s')\n",
    )
    _set_command(monkeypatch, tmp_path, "python drain_stdin_probe.py")

    run_id = str(uuid.uuid4())
    result = run_tests(make_patch_result(run_id), run_id)

    assert result.passed is True
    assert result.duration_seconds < 60
    assert result.passed_tests == 1


def test_child_stdin_is_null_device_even_when_parent_stdin_is_a_pipe(
    monkeypatch, tmp_path
):
    """
    Adversarial: wire the parent's fd 0 to a held-open pipe — the exact
    inherited-stdin hang condition — and prove the child still sees the null
    character device, i.e. DEVNULL overrides inheritance. The probe inspects
    its stdin's device type instead of reading from it, so a wrong answer is
    a fast exit(1), never a 300s block.
    """
    _write_probe(
        tmp_path,
        "stdin_device_probe.py",
        "import os, stat, sys\n"
        "is_chr = stat.S_ISCHR(os.fstat(0).st_mode)\n"
        "print('1 passed' if is_chr else '1 failed')\n"
        "sys.exit(0 if is_chr else 1)\n",
    )
    _set_command(monkeypatch, tmp_path, "python stdin_device_probe.py")

    try:
        saved_stdin_fd = os.dup(0)
    except OSError:
        pytest.skip("fd 0 is not duplicable in this environment")

    read_end, write_end = os.pipe()
    os.dup2(read_end, 0)
    try:
        run_id = str(uuid.uuid4())
        result = run_tests(make_patch_result(run_id), run_id)
    finally:
        os.dup2(saved_stdin_fd, 0)
        os.close(saved_stdin_fd)
        os.close(read_end)
        os.close(write_end)

    assert result.passed is True
    assert result.passed_tests == 1


def test_devnull_does_not_mask_real_test_failures(monkeypatch, tmp_path):
    """
    Regression guard: a genuinely failing test command is still classified
    failed after E2 — DEVNULL must not make the world look green. (Rollback
    wiring on this path is already pinned by
    test_failing_command_rolls_back_chunk.)
    """
    _write_probe(
        tmp_path,
        "always_fail_probe.py",
        "import sys\n"
        "print('1 failed')\n"
        "sys.exit(1)\n",
    )
    _set_command(monkeypatch, tmp_path, "python always_fail_probe.py")

    run_id = str(uuid.uuid4())
    result = run_tests(make_patch_result(run_id), run_id)

    assert result.passed is False
    assert result.failed_tests == 1
```

Design notes the implementer should not "improve" away:

- The probe commands use the `python <file>` form on purpose: `_resolve_command` (`tester.py:29-30`) rewrites them to `"<sys.executable>" <file>`, exactly as production commands flow, and the quoted-executable-plus-bare-relative-filename shape is the one quoting pattern that is safe under `shell=True` on both `cmd.exe` and `/bin/sh`.
- `assert set(captured["kwargs"]) == {...}` is an intentional exact pin. It is the regression guard that E2 changed *only* stdin.
- The probes print pytest-style `1 passed` / `1 failed` lines so `_parse_test_counts` (`tester.py:80-101`) is exercised end-to-end (`passed_tests` / `failed_tests` asserts), proving output capture still flows with the new kwarg.

### Expected red/green behavior (run the anchor first)

| Test | Pre-fix, Windows interactive console | Pre-fix, POSIX under default pytest capture | Post-fix, everywhere |
|---|---|---|---|
| anchor (call contract) | **RED** (fast) | **RED** (fast) | GREEN |
| immediate EOF | **RED** (~300 s: child reads the console) | green — masked, see §6 | GREEN |
| unbounded read under timeout | **RED** (~300 s) | green — masked, see §6 | GREEN |
| parent-stdin-is-a-pipe | green (console is a char device; Windows ignores the fd-0 rewire, see §6) | **RED** (fast) | GREEN |
| does-not-mask-failures | GREEN | GREEN | GREEN |

Only the anchor is red in every environment — that is by design (the brief's "assert the call contract"). Every test is deterministically green post-fix on both platforms, which is the property the suite needs.

## 6. Where it can go wrong

- **Scope creep at the call site.** The temptation is to "also" fix the misclassification at `tester.py:164`, hoist `TESTER_TIMEOUT_SECONDS`, or touch the rollback calls. All are owned by later phases (§1). The anchor's exact kwarg-set assertion will fail if anything beyond `stdin` is added to the call — that is intended. Add one line, stop.
- **False-confidence trap: never reaching line 150.** A test whose `PatchResult` has `success=False` or empty `files_applied` early-returns at `tester.py:129` and proves nothing about the subprocess call. Every test above uses `make_patch_result` (success=True, non-empty `files_applied`). The anchor fails loudly (`KeyError: 'kwargs'`) if the call was never made, rather than silently passing.
- **Don't test the crash.** The Windows `init_sys_streams` startup crash needs a console-less parent with invalid stdin and will not reproduce under pytest or on POSIX. No test here attempts it; the anchor pins the cause (the missing kwarg), the probes pin the cure (EOF semantics). Do not add a test that tries to reproduce the crash — it will be flaky by construction.
- **pytest masks the bug on POSIX — don't conclude the fix is unnecessary.** Default pytest capture (`--capture=fd`) redirects the runner's own fd 0 to `/dev/null`, so on POSIX the two EOF/hang probes are green even *before* the fix. They are semantic proofs and regression guards, not the red/green pin; the anchor is. If you want to see them red pre-fix, run them on Windows from an interactive console (or with `pytest -s` attached to a console) — and expect each to take the full ~300 s timeout. That slowness *is* the production symptom, not test flakiness.
- **Windows fd-0 subtlety in the pipe test.** `os.dup2(read_end, 0)` rewires CRT fd 0, but on Windows CPython's `subprocess` takes the child's stdin from the *process standard handle* (`GetStdHandle`), so pre-fix the pipe never reaches the child there and the test may be green pre-fix on Windows. Post-fix it is deterministic everywhere (DEVNULL → `nul`/`/dev/null` → `S_ISCHR` true). Do not "fix" this by forcing `stdin=subprocess.PIPE` or by asserting the pre-fix failure — post-fix determinism is the requirement.
- **Quoting under `shell=True` on Windows.** Do not rewrite the probes as `python -c "..."` — nested quotes inside a `cmd.exe /c` string are fragile (cmd's quote-stripping rule kicks in at the third quote character). Likewise do not pass the probe's *absolute* path: `_resolve_command` already quote-wraps `sys.executable`, and a second quoted token re-enters quote-stripping territory. The bare-relative-filename-with-`cwd` pattern used above stays at exactly two quote characters, the one shape `cmd.exe` preserves verbatim.
- **Flaky-bound temptation.** `result.duration_seconds < 60` is deliberately loose (real cost is ~1 s of interpreter startup; the failure mode it guards against is ~300 s). Do not tighten it to single digits — a cold antivirus-scanned Python launch on a loaded Windows CI box can take several seconds.
- **fd hygiene in the pipe test.** Restore fd 0 *before* closing the saved descriptor, and close both pipe ends in the `finally` (the code above does). Leaking the write end keeps a pipe object alive for the session; clobbering fd 0 without restoring breaks pytest's own capture teardown.
- **`save_checkpoint` on the passed path.** The three green-path probes write a real checkpoint row into the session's isolated temp DB (`conftest.py:25-27`), same as the existing passing-path tests — do not mock it there; only the anchor mocks it (its fake `subprocess.run` makes the rest of the pipeline meaningless).

## 7. Verification commands

From the repo root, in PowerShell:

```powershell
# 1. Targeted: the tester test file (existing 17 tests + 5 new ones)
python -m pytest backend/tests/test_tester.py -q

# 2. Full unit suite (live-API tests excluded by marker)
python -m pytest backend/tests -q -m unit

# 3. Lint (never ruff format)
ruff check
```

Optional red-phase proof before making the edit: run only the anchor and watch it fail on the missing kwarg —

```powershell
python -m pytest backend/tests/test_tester.py -q -k stdin_devnull_and_contract
```

(Avoid running the two EOF/hang probes pre-fix from an interactive Windows console unless you want to sit through two ~300 s timeouts — see §6.)

## 8. Safety-contract check

Touches none of the nine invariants directly; net effect is strengthening invariant 9.

- **Invariant 9 (fail safely, don't guess):** today an stdin-induced harness failure is *misattributed* — reported as a test regression and triggering rollback of a possibly good patch. The fix removes that self-inflicted failure mode without loosening anything: `passed=True` still requires the project's suite to genuinely exit 0 (`tester.py:164`, untouched), and a test command that truly needs interactive stdin now fails fast at EOF instead of hanging — still a failure, still safe.
- **Invariants 1–2 (gates, scope):** untouched — tester runs after approval and edits nothing; `scope_guard` and approval flows are not in this diff.
- **Invariant 3 (no empty commits):** untouched — tester does not commit.
- **Rollback behavior:** unchanged and still pinned by the existing `test_failing_command_rolls_back_chunk` (`backend/tests/test_tester.py:246-274`) plus the new does-not-mask-failures guard.
- **Invariant 6 (secrets/PII):** no new data captured, stored, or logged; the change only redirects what the child *reads*.

The one question worth asking adversarially — "could DEVNULL flip a genuinely failing suite to green?" — answers no: it changes only what the child sees on stdin (immediate EOF instead of console/pipe/invalid handle). A suite that passed only because it read interactive input cannot exist under the server deployment this code targets; a suite that exits 0 with EOF stdin is simply a passing suite, now classified correctly.

---

## Validation record (spec author, 2026-06-11, Windows 11 / Python 3.11)

The §5 tests were validated end-to-end on the spec author's machine and then **fully reverted** — the working tree contains only this spec. The implementer re-runs the same sequence.

1. Tests pasted into `backend/tests/test_tester.py`, production code untouched → anchor run alone: **failed as predicted** (`KeyError: 'stdin'` on the captured kwargs, 0.48 s). Red phase confirmed.
2. §4 one-line edit applied to `tester.py` → `python -m pytest backend/tests/test_tester.py -q`: **22 passed** (17 existing + 5 new) in 0.94 s. The pipe test ran for real (fd 0 was duplicable; no skip).
3. `ruff check backend/pipeline/tester.py backend/tests/test_tester.py`: **clean**. (Repo-wide `ruff check` reports 51 pre-existing findings in *other* files — none introduced by E2; do not fix them in this PR.)
4. `python -m pytest backend/tests -q -m unit` with fix + tests in place: **2628 passed, 1 skipped, 4 deselected** in 14 min 27 s.
5. Both files restored via `git restore`; `git status` confirms pristine.

POSIX behavior (the masked-green / pipe-test-red columns in §5's matrix) is reasoned from CPython and pytest source, not executed — there is no POSIX box in this environment. Post-fix determinism on POSIX rests on `subprocess.DEVNULL` opening `/dev/null`, which is interpreter-guaranteed.
