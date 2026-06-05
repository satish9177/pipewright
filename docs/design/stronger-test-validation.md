# Stronger Test Validation Design (#28A)

> Status: **Design only (#28A).** This document defines the design for **honest
> test validation**: combining command-string intent with runtime execution
> evidence so a run can never look validated when it validated nothing, and
> requiring a **human acknowledgement** before final commit/PR when validation is
> weak or absent. **No runtime code, route, model, migration, schema change,
> frontend component, or test ships with #28A.** Implementation is phased after
> this doc (see *Suggested Implementation Slices*).
>
> This builds **on top of** the existing deterministic command-string classifier
> ([`backend/pipeline/test_command_quality.py`](../../backend/pipeline/test_command_quality.py),
> #23A/#23B) and is strictly **additive and orthogonal** to #26 (Patch Failure
> Recovery v2) and #27 (Scope Expansion Recovery). #28 does **not** replace or
> weaken any existing safety, scope, or approval behavior.

---

## 1. Problem Statement (what #28 solves)

Pipewright's safety model treats **"the configured test command exited 0"** as
**"tests passed."** [`backend/pipeline/tester.py`](../../backend/pipeline/tester.py)
saves a checkpoint with `tests_passed=True` purely on `completed.returncode == 0`
(see `run_tests`). That is dishonest in a whole family of common cases where a
command exits 0 while validating nothing:

- `python --version`, `node --version`, `npm --version`, `pip --version` — print
  a version string, run no tests. (Observed during the #27 manual smoke; the
  configured command was literally `python --version`.)
- `echo ok`, `true`, `pwd`, `ls`, `whoami` — no-ops.
- `pytest` on a repo with **zero test files** → exits 0, **collected 0 items**.
- `pytest -k some_filter_that_matches_nothing` → exits 0, ran 0 tests.
- `pytest tests/` after the tests were deleted or never discovered → 0 collected,
  exit 0.
- `npm test` where `package.json` has the default `create-react-app` /
  `npm init` stub (`"test": "echo \"Error: no test specified\" && exit 0"` or a
  passing echo) → exits 0, runs no real tests. This is extremely common in young
  repos.

Each of these saves `tests_passed=True` today. A run then looks validated and can
proceed to final approval, commit, and PR carrying **false confidence**.
Pipewright's product promise is safety; silent fake-green validation directly
undermines it.

**#28 makes weak or absent validation visible and requires an explicit human
acknowledgement before the final commit/PR** — without hard-blocking early-stage
repos, docs-only work, or legitimate custom test scripts.

### What #28 is not

- Not a hard block. A weak-but-passing command must still let the run proceed
  after acknowledgement; early/small repos depend on this.
- Not an auto-failure or auto-rollback. **Weak is not the same as failed.** The
  rollback path (tests exit non-zero) is unchanged and is **not** #28's concern.
- Not LLM-based. v1 classification is purely deterministic.
- Not a richer command-string taxonomy. The high-value signal #28 adds is
  *runtime execution evidence*, not more string buckets (see section 2).
- Not a new approval that replaces final approval. The acknowledgement is a
  *precondition on* the existing final gate, never a substitute for it.

---

## 2. Why the string classifier is useful but insufficient

[`classify_test_command`](../../backend/pipeline/test_command_quality.py) already
ships and is genuinely valuable. It deterministically labels a configured command
string as:

- `WEAK` — a recognized no-op / version / inspection command (`python --version`,
  `echo ok`, `true`, `git status`, bare REPLs).
- `LIKELY_TEST` — a recognized test runner (`pytest`, `npm test`, `go test`, …).
- `UNKNOWN` — anything else, including custom scripts (deliberately **not** weak).

It is pure, deterministic, string-only, and surfaced today as a display-only
banner ([`frontend/src/components/TestCommandQualityWarning.tsx`](../../frontend/src/components/TestCommandQualityWarning.tsx))
in project settings and the run review. That banner is exactly what fired on
`python --version` during the #27 smoke.

**But the string is necessary and not sufficient.** The string classifier cannot
see what actually happened when the command ran. The dangerous cases in section 1 —
`pytest` collecting 0 items, the npm test stub, deleted tests — all classify as
`LIKELY_TEST` at the string level and still validate nothing at runtime. A banner
that only reads the string will *reassure* the user on exactly these cases.

The structural gap is therefore not "the buckets are too coarse." It is "there is
no runtime signal." #28's core contribution is a second signal — **execution
evidence** — joined deterministically with the existing string intent.

---

## 3. Runtime evidence model: command intent + execution evidence

#28 introduces a **two-signal model**, not a one-axis classifier:

- **Signal A — command intent** (string, deterministic, *already built*): the
  output of `classify_test_command` (`WEAK` / `LIKELY_TEST` / `UNKNOWN`).
- **Signal B — execution evidence** (runtime): the test command's exit code plus
  best-effort parsed counts and zero-test markers from its captured output
  ("collected 0 items", "no tests ran", "0 passed", a parseable "N passed").

The **verdict** is a deterministic *join* of A and B. Intent alone can be fooled
(strong string, zero tests). Evidence alone can be fooled (an echo stub prints
nothing test-like but exits 0). Joining them catches the cases neither catches
alone, which is the entire reason a string-only banner was not enough.

Both signals are computed **only on the success path** (the command exited 0).
When the command exits non-zero, the existing tester already fails and rolls back;
#28 never runs on that branch.

---

## 4. Proposed verdicts: strong / weak / none / unknown

A single runtime verdict per chunk's test run:

| Verdict | Meaning |
| --- | --- |
| `strong` | A recognized test runner ran **and** evidence shows tests actually executed and passed (parsed total ≥ 1, no zero-test markers). |
| `weak` | The command is a recognized weak/no-op/version command, **or** a recognized test runner that demonstrably ran **0 tests** (collected 0 / no tests ran / total == 0). Looks green, validated nothing. |
| `none` | No test command configured (blank/empty), so nothing could have validated the change. |
| `unknown` | An unrecognized custom command that exited 0. Pipewright cannot confirm it ran tests, and **must not accuse it of being weak** (see section 16). Informational only. |

Four verdicts is deliberate. We explicitly **reject** adding a "moderate" tier in
v1 (see section 25): a middle bucket invites "is this moderate or strong" bikeshedding
and false alarms *before* runtime evidence exists to justify the distinction.

---

## 5. Exact v1 behavior for each verdict

Deterministic join rule (pseudocode; the real function lands in #28B):

```
classify_test_run(command, exit_code, output) -> verdict
  # #28 only acts on the success path:
  if exit_code != 0:        -> out of scope (existing failure/rollback path owns it)

  string_quality = classify_test_command(command)   # reuse #23A

  if command is blank/empty:                         -> none
  if string_quality == WEAK:                         -> weak
  if string_quality == LIKELY_TEST:
      if evidence shows tests ran (total >= 1, no zero-test markers): -> strong
      else (0 collected / "no tests ran" / total == 0):              -> weak
  if string_quality == UNKNOWN:                      -> unknown
```

Behavior per verdict:

- **strong** — no friction. No banner escalation, no acknowledgement. Final
  approval behaves exactly as today.
- **weak** — proceed (no auto-rollback), persist the verdict on the chunk, and
  **require a human acknowledgement at the final gate** before commit/PR.
- **none** — same as `weak`: proceed, persist, require acknowledgement. Copy is
  tuned for "no tests configured" rather than "command ran nothing."
- **unknown** — proceed, show a **non-blocking note** ("Pipewright could not
  confirm this command runs your test suite"), **no acknowledgement required in
  v1**. Never call a custom script weak.

---

## 6. Should weak/none block chunk approval, final approval, PR, or only warn?

| Gate | Behavior |
| --- | --- |
| Chunk approval | **Never blocked.** The chunk gate is about *scope*, not test quality. Blocking it would punish every early-stage repo and violate principle 3/4. |
| Final approval | **Acknowledgement required** when verdict is `weak`/`none`. Not a hard block — a required, audited human affirmation. |
| PR creation | Inherits the final gate. PR mode (`local_only` / `github_cli` / `manual_token`) is downstream of final approval, so an unacknowledged weak verdict stops before any push/PR. |
| Warn-only | Rejected as the *primary* mechanism. We already ship a warn-only banner; this design exists because warn-only is ignorable. |
| Hard block | Rejected (see section 25). Breaks docs-only and no-test repos. |

The acknowledgement at the final gate is the only altitude that satisfies "make
risk visible" + "don't weaken existing gates" + "don't block all workflows"
simultaneously.

---

## 7. Human acknowledgement design

When the final-gate verdict is `weak` or `none`, the human must record an explicit
affirmative acknowledgement before the run can commit/PR. Properties:

- **Affirmative and specific.** "Tests did not meaningfully run (`<reason>`). I
  understand and want to commit anyway." Not a pre-checked box, not a silent
  default.
- **Audited.** Stored with who, when, the verdict acknowledged, and the diff hash
  it was made against (see section 9). Mirrors the audit columns on
  `scope_expansion_requests` (#27).
- **Not a new approval type.** It is a *precondition* on the existing final
  approval, never a replacement. The real final approval still happens.
- **Only when needed.** Never shown or required for `strong`; not required for
  `unknown` in v1. Attempting to acknowledge a `strong` run is a conflict (nothing
  to acknowledge), handled like #27's 409 conflict mapping.
- **Idempotent.** A double-submit (double-click) records one active
  acknowledgement and never produces a second commit; reuse existing run locks.

---

## 8. Why acknowledgement is at final approval, not chunk approval

- **Altitude of the decision.** "Is this code adequately tested before it becomes
  a commit/PR?" is a *release* decision, made once, against the final state — not
  a per-chunk decision.
- **Don't nag.** Requiring acknowledgement per chunk would make a multi-chunk run
  prompt repeatedly for the same systemic fact ("this repo's test command is
  weak"), training users to click through.
- **Final state is what ships.** The verdict that matters is the one describing
  the exact code about to be committed, after all chunks and any retries settle.
- **Keeps the chunk gate single-purpose.** The chunk gate stays about scope and
  diff review; test-validation debt is *collected* across chunks and *resolved*
  once at the final gate.

The run carries forward a "has unacknowledged weak validation" state collected
from its chunks; the final gate is where that debt is presented and cleared.

---

## 9. Binding acknowledgement to the current diff/hash

An acknowledgement records the **hash of the exact diff it was made against**
(e.g. the post-patch git hash already tracked in checkpoints /
`patch_result.post_patch_git_hash`, or an equivalent stable hash of the final
staged diff). The final gate requires an **active** acknowledgement *whose hash
matches the current diff hash*.

This binding is the single subtle safety property an obvious implementation will
miss. Without it, a human could acknowledge weak validation on diff A, then a
retry or amendment produce diff B, and the stale acknowledgement would silently
cover never-reviewed code.

---

## 10. What happens if retry recovery changes the diff after acknowledgement

If a retry (#26) or scope amendment (#27) changes the diff **after** an
acknowledgement was recorded:

- The stored acknowledgement's `acknowledged_diff_hash` no longer matches the
  current diff hash, so it is **stale** and **not active** for the new state.
- The final gate therefore **re-requires acknowledgement** against the new diff.
- The runtime verdict is **recomputed on the retry's own test run** and
  **overwrites** the prior chunk verdict. A successful retry must not inherit the
  failed attempt's evidence, and must not launder a weak retry into strong.

Stale-acknowledgement-survives-changed-diff is an explicitly rejected behavior
(see section 25). This is the key race the design must defend.

---

## 11. Interaction with #26 Patch Failure Recovery

Strictly additive and orthogonal:

- #26 acts on the **failure** path (a patch failed; a human triggers a retry
  inside the already-approved scope). #28 acts only on the **success** path
  (tests exited 0). They never touch the same branch.
- When a #26 retry **succeeds**, #28 computes the verdict on that retry's test
  run and overwrites the chunk verdict; any prior acknowledgement is invalidated
  by the diff-hash change (section 10).
- #28 reuses #26/#27's proven architecture shape: a pure deterministic core, a
  thin store, a separate route, "mutates nothing on failure," conflict → 409.

## 12. Interaction with #27 Scope Expansion Recovery

- #27 amends the effective allowlist and retries under it; a successful expanded
  retry still pauses at `awaiting_chunk_approval`. #28's verdict is computed
  **after** that retry's tests run, against the amended state, so it always
  reflects the code that will actually ship.
- The acknowledgement record follows the `scope_expansion_requests` pattern:
  audit columns, an explicit state machine, idempotent creation, route maps
  conflict to 409, never auto-acts. No file contents, secrets, or token-like
  values are ever stored — only verdict, reason, hash, and audit fields.
- #28 never weakens `scope_guard` and never changes scope; it only labels the
  validation quality of an already-scoped, already-applied change.

---

## 13. Handling no-test repos

A repo with no tests yet is a **first-class state**, not an error. A blank/empty
test command → verdict `none` → acknowledgement required, **not** a block.
Forcing users to configure *some* command to proceed is how you train them to
write `echo ok`; that is actively harmful and the opposite of the goal. The
honest path is: surface "no tests ran," let the human own committing anyway.

## 14. Handling docs-only changes

Do **not** auto-detect "docs don't need tests" in v1. A docs change can still
break a doctest or a link checker, and a silent `*.md`-only exemption is exactly
the kind of hidden heuristic that erodes trust (and is explicitly rejected in
section 25). Instead, the acknowledgement copy offers the honest framing — "no
meaningful tests ran; fine for a docs-only change, risky otherwise" — and the
human owns the decision. An *explicit, logged, opt-in* docs-only softening is a
possible v2 nicety, never a silent v1 default.

## 15. Handling lint / typecheck / build / smoke commands

For v1, the only distinction that changes safety behavior is **"did real tests
execute?" vs "not."** Lint (`eslint`, `ruff`), typecheck (`mypy`, `tsc`), build,
and smoke commands are **not tests** and must not earn a `strong` verdict. Today
they fall into `UNKNOWN` at the string level (they are not in the test-runner
set), which already prevents a false `strong`. That is sufficient for v1.

Recognizing these as a distinct *category* (a per-command taxonomy of
test/lint/typecheck/build/smoke) is **display polish, not a safety change**, and
is deferred to v2 (see section 25/section 26). All of them collapse to "not confirmed tests"
for gating.

## 16. Why unknown custom commands must not be called weak

A false "weak" accusation against a real custom test suite (`./run-ci.sh`,
`make ci`, `bash scripts/test.sh`, a bespoke runner) is the **worst UX outcome**:
it cries wolf, trains users to ignore the warning, and insults legitimate setups.
The existing classifier's instinct — *never* label an unrecognized command weak —
is correct and is preserved. `UNKNOWN` → verdict `unknown` → a non-blocking note,
never an acknowledgement gate in v1. We accept a *miss* (a genuinely weak custom
script slips through as `unknown`) over a *false alarm* (a real suite branded
weak), because the false alarm destroys the feature's credibility.

---

## 17. Test count parsing design

Best-effort, additive, advisory. [`tester.py`](../../backend/pipeline/tester.py)
already has `_parse_test_counts`, which regex-matches pytest's `N passed` /
`N failed`. #28 extends this thinking but constrains it hard:

- Parse counts and **zero-test markers** ("collected 0 items", "no tests ran",
  "0 passed", framework "Tests: 0") from the captured output.
- Counts are **corroborating evidence only**. They downgrade a `LIKELY_TEST`
  string to `weak` when they prove zero tests ran. They never *fail* a run, never
  trigger rollback, and never gate by themselves.
- **Absence of a parseable count means `unknown count`, never `0 tests`.** A
  command whose output we cannot parse is not evidence that zero tests ran.
- v1 parses **pytest** (already partially present). Non-pytest count parsers
  (jest, go, cargo, mocha) are v2.

## 18. Risks of relying on output parsing (including truncation)

These are real and must be designed around, not waved away:

- **Truncation drops the summary.** `tester.py` defines
  `MAX_OUTPUT_CHARS = 10000` and `_combine_output` truncates from the **front**,
  keeping the head. But pytest's summary line ("N passed") is at the **end**. A
  verbose suite's summary can be truncated away, making a real run look like
  "0 tests." This is a live correctness bug for any count-based logic and is the
  reason #28C exists.
- **Framework-specific formats.** pytest, jest, go test, cargo, mocha all print
  different summaries; a parser tuned for one says nothing about another.
- **Parallel / randomized runners.** `pytest-xdist`, `pytest-randomly`, color
  codes, and progress output change the summary text.
- **Skips / xfail / deselected.** "0 passed, 5 skipped" technically ran nothing
  meaningful; counts must consider this.
- **Localization.** Non-English or customized reporters change the literals.

Therefore: parsing is advisory-downgrade only; an unparseable run is `unknown
count`, never a fail, never a block.

## 19. Parse counts from full output (or the tail) before truncation

Because the summary lives at the **end** of test output and the current truncation
keeps the **head**, #28C must parse counts/markers from the **full output before
truncation**, or explicitly retain the **tail**. The truncated string may still
be stored for display, but the parse must run against untruncated text.
Otherwise the verdict can wrongly conclude "0 tests" on a healthy large suite —
exactly the false negative #28 is trying to prevent.

---

## 20. Backend model / store / API needs

Follow the #27 shape (pure model in `backend/pipeline/`, thin store, audit
columns, separate route). Sketch only — nothing is built in #28A.

Pure core (`#28B`, no I/O):

```python
class TestRunVerdict(str, Enum):          # __test__ = False (pytest-collection guard)
    STRONG = "strong"; WEAK = "weak"; NONE = "none"; UNKNOWN = "unknown"

@dataclass(frozen=True)
class TestRunVerdictResult:
    verdict: TestRunVerdict
    reason: str
    string_quality: TestCommandQuality   # reuse #23A
    total: int | None; passed: int | None; failed: int | None
    counts_parsed: bool

def classify_test_run(command: str, exit_code: int, output: str) -> TestRunVerdictResult
```

Persistence (`#28D`): store the runtime verdict where the chunk's test result
already lives (the chunk record / checkpoint), display-only at first. Proposed
fields (illustrative — schema change is **not** part of #28A):

```sql
-- display-only runtime evidence on the chunk
test_run_verdict TEXT;          -- strong / weak / none / unknown
test_run_verdict_reason TEXT;
test_run_counts TEXT;           -- json {passed,failed,total} or null

-- acknowledgement, mirroring scope_expansion_requests (lands in #28F)
CREATE TABLE IF NOT EXISTS test_validation_acknowledgements (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    chunk_number INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    acknowledged_diff_hash TEXT NOT NULL,   -- ties ack to the exact code (section 9)
    acknowledged_by TEXT,
    acknowledged_at DATETIME,
    reason TEXT,
    status TEXT DEFAULT 'active',
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);
```

API (`#28E`): extend the run/chunk GET responses with a read-only
`test_validation` object (verdict, reason, counts, acknowledgement state). One new
acknowledgement route (`#28F`), modeled on #27's approve route:
`POST /runs/{run_id}/chunks/{n}/test-validation/acknowledge` — records an ack tied
to the current diff hash; **409** if the verdict is `strong` (nothing to ack) or
the diff hash is stale; mutates nothing on failure. The existing final-approval
route gains a **precondition**: verdict `weak`/`none` with no active ack for the
current diff hash → 409 "weak test validation requires acknowledgement," with no
commit/push/PR.

## 21. Frontend UI requirements

- Keep [`TestCommandQualityWarning.tsx`](../../frontend/src/components/TestCommandQualityWarning.tsx)
  for config-time (string) hints in project settings.
- Add a **runtime** validation banner on the run review (`RunDetailPage`) fed by
  the chunk's `test_run_verdict`: amber for `weak`/`none`, a muted note for
  `unknown`, nothing for `strong` (reuse the existing amber styling).
- At the **final gate**, when verdict is `weak`/`none`, render a **required
  acknowledgement checkbox** that gates the "Approve & commit" / "Create PR"
  button: "Tests did not meaningfully run (`<reason>`). I understand and want to
  commit anyway." `strong` → button enabled, no checkbox, zero friction.

## 22. Final approval gate behavior

- `strong` → unchanged from today.
- `weak` / `none` → final approval requires an **active acknowledgement bound to
  the current diff hash**. Without it, the gate returns a conflict and performs no
  commit, push, or PR. With it, the existing final approval proceeds normally.
- `unknown` → v1: no acknowledgement required; show the non-blocking note.
- The acknowledgement never **replaces** final approval; both must hold. No
  auto-approval, no auto-commit, no final-approval bypass is introduced.

---

## 23. Edge cases and race conditions

- `pytest` collecting **0 items**, exit 0 → `weak` (the headline case).
- npm default `"test"` stub (`echo no test specified` / passing echo) → `weak`.
- **Truncated output** where the summary line is cut → degrade to `unknown count`,
  **not** `0 tests` (depends on the #28C fix).
- Strong string + exit 0 + parseable "12 passed" → `strong`, no ack.
- "0 passed, 5 skipped" → treated as no meaningful tests → `weak`.
- **Retry changes the diff after an acknowledgement** → stored ack stale →
  re-require acknowledgement (the key race, section 10).
- Concurrent final-approval submits (double-click) → idempotent ack, no double
  commit (reuse run locks).
- Acknowledge attempted when verdict is `strong` → 409 (nothing to acknowledge).
- Timeout / non-zero exit / rollback path → **no verdict recorded**; #28 must
  never run on the failure branch.
- Blank command → `none`, not `unknown`.
- A run whose verdict was `weak` on chunk N but a later retry made it `strong` →
  verdict overwritten; debt cleared.

## 24. Mandatory tests (for the implementing slices, not #28A)

- **Unit:** full truth table for `classify_test_run` — every `string_quality` ×
  {tests ran / 0 collected / unparseable} × exit code.
- **Unit (regression):** long pytest output, assert the summary is still parsed
  after the #28C truncation fix.
- **Store:** ack create/read, idempotency, stale-on-diff-change.
- **Route:** ack 409 on `strong`; final-approval 409 without ack on `weak`/`none`;
  success path with a valid ack; double-submit idempotency; ack invalidated after
  a retry changes the diff.
- **Integration:** weak run → chunk proceeds, final gate blocks until ack; strong
  run → zero friction; npm-stub and 0-collected fixtures.
- **Regression of the original smoke:** `python --version` now yields a *runtime*
  `weak` verdict and a required acknowledgement, not merely a banner.

## 25. Explicit v1 non-goals (rejected approaches)

This design explicitly **rejects** the following for v1:

- **Hard-blocking all weak tests.** Breaks no-test and docs-only repos; violates
  "don't block all workflows."
- **Treating unknown custom scripts as weak.** Cries wolf, destroys credibility
  (section 16).
- **LLM-based classification for gating.** Non-deterministic, non-reproducible, a
  new failure mode in a safety gate. (An LLM *suggesting a better command* is a
  possible advisory, non-gating v2 feature.)
- **False-precision buckets like "moderate"** before runtime evidence exists.
- **Auto-exempting docs-only diffs silently.**
- **Stale acknowledgement surviving a changed diff / retry.**
- **Treating exit code 0 as strong validation without evidence.**

Also out of v1 scope (deferred, not rejected): non-pytest count parsers; a
per-command lint/typecheck/build/smoke taxonomy; coverage-percentage gating;
auto-editing the project's test command; recommending better commands.

## 26. Suggested implementation slices after #28A

Mirrors how #27 was sliced — pure core → store → wiring → route → UI — so each
slice is independently shippable and teeth land last, smallest, and most-tested.

- **#28A** — design / prep audit only (**this document**).
- **#28B** — pure runtime test-evidence classifier (`classify_test_run`), unit
  tested, no wiring.
- **#28C** — tester output parsing / truncation fix: parse counts and zero-test
  markers from full output (or the tail) **before** truncation (section 18/section 19).
- **#28D** — persist the runtime test verdict on the chunk/run, **display-only**,
  no gate.
- **#28E** — API + frontend runtime validation banner (read-only surfacing).
- **#28F** — final-approval weak/no-test **acknowledgement gate** (diff-hash
  bound). The only slice that touches an approval gate; ship last, behind the most
  tests.
- **#28G** — smoke docs / manual checklist (mirrors
  [`docs/testing/scope-expansion-recovery-smoke.md`](../testing/scope-expansion-recovery-smoke.md)).

---

## Safety Invariants (non-negotiable)

These hold at every layer and must never be relitigated by an implementation
slice:

1. **Weak is never silently treated as strong.** Exit 0 alone is not validation.
2. **Weak is never treated as failure.** No auto-rollback, no auto-fail on a
   weak-but-passing command.
3. **Chunk approval is never blocked** by test-validation quality.
4. **Final approval requires acknowledgement** only for `weak`/`none`, and the
   acknowledgement is a *precondition on* — never a replacement for — the existing
   final approval. No auto-approval, no auto-commit, no final-approval bypass.
5. **Acknowledgement is bound to the current diff hash**; a retry or amendment
   that changes the diff invalidates a stale acknowledgement.
6. **Unknown custom commands are never called weak.**
7. **Classification is deterministic** (no LLM) for any gating path.
8. **No secrets, tokens, file contents, or full diffs** are stored in the verdict
   or acknowledgement records — only verdict, reason, counts, hash, and audit
   fields.
9. **Output parsing is advisory only**; an unparseable/truncated run is `unknown
   count`, never `0 tests`.
