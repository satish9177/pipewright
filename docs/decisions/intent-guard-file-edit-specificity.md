# Intent Guard — File-Edit Specificity (PR #17A design)

**Status:** Design / docs only. No code, no runtime behavior, no safety guard is
changed by this PR. All behavior changes are specified here for follow-up PRs
#17B–#17D, tests-first.

**Phase:** 2H — Dogfood + Reliability Hardening.

**Scope guard for this PR:** does not touch `scope_guard`, `patch_applier`,
approvals, git, memory, DB, or frontend behavior; does not loosen any safety
guard. It only adds a new design document.

---

## 1. Problem statement

During dogfooding, the request:

```
add hello in the readme
```

was rejected with the generic message:

```
This request is too vague to implement safely.
```

This is wrong. The request carries full intent for a simple file edit:

- **action:** `add` / append
- **content:** `hello`
- **target:** `readme`

A specific request was treated like a vague one ("fix it", "make it better"),
which erodes trust in the tool's judgement. The fix is to add a deterministic,
index-grounded target-resolution layer that produces **specific** clarifications
(create-this-file? / which-one?) instead of a generic rejection — and, on a
unique match, to pin the target file deterministically.

This must generalize beyond README to all simple explicit file edits.

### Examples that should be specific enough (when target grounding succeeds)

- `add hello in the readme`
- `add hello bro to README.md`
- `append test text to docs/usage.md`
- `add a comment to backend/app/models/user.py`
- `rename login button to Sign in`
- `update timeout from 30 to 60 seconds`
- `update package json script name`
- `add note to docker compose comment`

### Examples that must still require clarification (no real target)

- `fix it`
- `make it better`
- `change the file`
- `add feature`
- `improve backend`
- `refactor code`
- `update project`
- `make UI good`

---

## 2. Root-cause finding (confirm before changing anything)

The current deterministic guard **already accepts** `add hello in the readme`.

- `backend/pipeline/implementation_guard.py` →
  `assess_implementation_specificity` strips describe-only words and keeps
  concrete anchors. For this input: `add` (verb, dropped), `hello` (greeting,
  dropped), `in`/`the` (stopwords, dropped), **`readme` (kept anchor)** →
  `is_specific_enough = True`.
- `backend/pipeline/intent.py` → `_deterministic_classify` matches the `"add"`
  phrase in `_IMPLEMENTATION_PHRASES`, returning `IMPLEMENTATION` with
  `source="deterministic_verb"` and `from_llm=False`. Because `from_llm` is
  False, the LLM specificity block in `backend/routes/chunks.py`
  (`create_chunked_run_route`, the `decision.from_llm` branch) is **skipped**.

Therefore the generic rejection observed in dogfood most likely came from one of:

1. the **LLM specificity verdict** — the request fell through to the LLM
   (`classify_intent_details_async`) and Gemini judged the `hello` content
   meaningless and returned `needs_clarification`; or
2. a **stale build** that predates the current deterministic guard.

> **Implementation requirement:** #17B MUST begin by writing a failing (red)
> test that reproduces the exact path which produced the generic message, on the
> current `main`, before any behavior is changed. If the failure cannot be
> reproduced on current `main`, say so explicitly — #17C then becomes a smaller,
> purely additive change (specific clarifications + target pinning) rather than a
> bug fix.

The design below is correct regardless of which path fired, because it adds a
deterministic resolution step on top of the existing guard.

### Current call path (`POST /runs/chunked` → `create_chunked_run_route`)

```
is_non_actionable_request           # greeting / noise → needs_clarification (intent=unknown)
classify_intent_details_async       # deterministic layers, LLM fallback only if ambiguous
  └─ decision.uncertain             # → needs_clarification
IMPLEMENTATION branch:
  assess_implementation_specificity # deterministic 9A anchor guard
  LLM specificity verdict           # only when decision.from_llm
  → _needs_clarification_response    # if either says vague (generic message today)
run_triage                          # LLM chunk plan
ground_triage_result_paths          # remove invented paths, harden chunks (PR #9B)
scan_triage_result                  # risk scan
create_chunked_run / read-only run
```

The new resolution step (#17C) is inserted in the `IMPLEMENTATION` branch
**after** the existing 9A + LLM specificity checks pass and **before**
`run_triage`.

---

## 3. Deterministic "specific enough" criteria

This formalizes (does not redefine) the existing anchor principle in
`implementation_guard.py`. A request is specific enough for a **simple file
edit** when it has all three:

1. **action verb** — add / append / update / rename / remove / delete / fix /
   change (already covered by `_IMPLEMENTATION_PHRASES` in `intent.py` and
   `_GENERIC_VERBS` in `implementation_guard.py`);
2. **target reference** — an explicit relative path or a known literal-file
   alias (see §4); and
3. **change / acceptance clue** — text to add, a value to update, or a rename
   target.

For simple text edits, **action + content + target = specific.** #17A does not
change the 9A guard; it adds a grounded target-resolution layer that runs after
9A and turns generic rejections into specific clarifications.

---

## 4. File-alias grounding helper (spec for #17B)

A new pure, deterministic module — proposed
`backend/pipeline/file_alias_grounding.py` — with a function such as:

```python
def resolve_explicit_edit_target(
    project_id: str, feature_description: str
) -> EditTargetResolution: ...
```

returning exactly one of:

| Outcome | Meaning |
|---|---|
| `GROUNDED(path)` | Exactly one indexed file matches the referenced target. |
| `NOT_FOUND(alias)` | A target/alias was referenced, but no indexed file matches. |
| `AMBIGUOUS(alias, paths)` | Multiple indexed files match the referenced target. |
| `NO_TARGET` | No explicit path or known alias detected — fall through to the existing flow unchanged. |
| `FORBIDDEN(alias)` | The referenced target is a secret/`.env*`/`.git/*` path — safe refusal, never grounded. |

> #17B note: implemented in `backend/pipeline/file_alias_grounding.py` with these
> five outcomes (`EditTargetOutcome` enum + frozen `EditTargetResolution`).
> `FORBIDDEN` is realized as an explicit outcome (read-level `is_forbidden_path`
> plus a `.git`-component check), not `is_forbidden_write_path` — so legitimately
> editable infra files (`docker-compose.yml`, `requirements.txt`) still ground
> while the strict write-time guard in `patch_applier` stays unchanged.

### Resolution rules

- **Index only.** Read the current repo index (`file_index` rows). Reuse
  `plan_path_grounding.get_indexed_paths_and_dirs` and/or
  `repo_indexer.get_relevant_files`. **No filesystem walk, no LLM call.**
- **Exact / unique indexed match → `GROUNDED`.**
- **No indexed match → `NOT_FOUND`** (specific clarification; never invent).
- **Multiple matches → `AMBIGUOUS`** (specific clarification; never auto-pick).
- **Alias scope (confirmed): literal files + explicit paths only.**
  - Explicit relative paths typed by the user (`docs/usage.md`,
    `backend/app/models/user.py`, `README.md`) → direct index lookup.
  - A small **fixed** alias table mapping a phrase to a literal basename pattern:

    | Alias phrase | Resolves to |
    |---|---|
    | `readme` | `README*` (case-insensitive basename) |
    | `package json` / `package.json` | `package.json` |
    | `docker compose` | `docker-compose.yml` / `compose.yaml` |
    | `requirements` | `requirements*.txt` |
    | `pyproject` | `pyproject.toml` |

  - **Defer** fuzzy entity/symbol aliases (`user model`, `login button`) to a
    later PR. Not in #17B. No broad fuzzy matching, no confidence scoring.
- **Forbidden / secret paths are never `GROUNDED`,** even when referenced
  explicitly. Reuse `utils/path_safety.is_forbidden_path` /
  `is_forbidden_write_path`. A forbidden target produces a safe refusal, not an
  edit and not a create offer.
- **Never invent a file** unless the user **explicitly** says `create <path>`
  (see §5). A bare alias that is `NOT_FOUND` results in a *question*, not a
  silently-created file.
- **Deterministic:** identical `feature_description` + identical index → identical
  result across repeated runs. No randomness, no time/order dependence.

---

## 5. Clarification message quality (spec for #17C)

Replace the generic message **only when a target was referenced but could not be
grounded**. Keep the existing generic `NEEDS_CLARIFICATION_MESSAGE` for requests
with no target at all (`fix it`, `make it better`).

- **`NOT_FOUND`:**
  > I understood you want to edit the README, but no README file was found in
  > this project. Do you want to create README.md?

- **`AMBIGUOUS`:**
  > I found multiple README files: README.md, docs/README.md. Which one should I
  > edit?

These reuse the existing `_needs_clarification_response` envelope shape
(`status="needs_clarification"`, `intent="implementation"`, `message`,
`missing_details`, `examples`) — no run is created.

---

## 6. Integration point (spec for #17C)

In the `IMPLEMENTATION` branch of `create_chunked_run_route`
(`backend/routes/chunks.py`), after the existing 9A + LLM specificity checks pass
and **before** `run_triage`:

1. Call `resolve_explicit_edit_target(project_id, feature_description)`.
2. **`NO_TARGET`** → unchanged behavior; proceed to `run_triage` exactly as today.
3. **`GROUNDED(path)`** → deterministically **pin `files_expected`** to that one
   path for the relevant chunk(s). The pinned plan still flows through
   `ground_triage_result_paths`, `scan_triage_result`, forbidden-path checks, and
   `scope_guard` unchanged. (This is the only runtime behavior change, and it is
   additive — it narrows scope, never widens it.)
4. **`NOT_FOUND` / `AMBIGUOUS`** → return `_needs_clarification_response` with the
   specific message from §5; no run is created.
5. **Explicit `create README.md`** → route through the existing create-file path
   **only** when the target is not forbidden and its directory is safe; otherwise
   clarify. Default remains: do not invent files.

> Pinning `files_expected` is deferred to #17C. #17A introduces no code.

---

## 7. Safety boundaries (must hold)

The design must NOT allow any of the following. Each is an explicit acceptance
criterion for #17B/#17C review:

- vague large features to pass — no target → still generic clarification;
- unknown / unindexed target files to be silently invented;
- forbidden / secret files (`.env`, `.git`, secrets, private keys) to be
  targeted, grounded, or created;
- editing when multiple matches exist without user confirmation;
- bypassing `scope_guard`, `ground_triage_result_paths`, forbidden-path rules,
  chunk-plan approval, or final approval;
- LLM-only grounding or broad fuzzy matching;
- automatic multi-file selection;
- any change to patch application.

---

## 8. Tests required in #17B / #17C

Unit tests for the resolver (`#17B`, follow existing `@pytest.mark.unit` +
`@pytest.mark.parametrize` conventions; seed `file_index` via the existing test
helper):

- `add hello in the readme` → `GROUNDED("README.md")` when `README.md` indexed.
- same request → `NOT_FOUND` (create-clarify) when no README indexed.
- multiple README files (`README.md`, `docs/README.md`) → `AMBIGUOUS`.
- `append test text to docs/usage.md` → `GROUNDED("docs/usage.md")` when indexed.
- `add hello bro to README.md` → `GROUNDED("README.md")` when indexed.
- `update package json script name` → `GROUNDED("package.json")` when indexed.
- `add note to docker compose comment` → `GROUNDED("docker-compose.yml")` when indexed.
- `fix it` / `make backend better` → `NO_TARGET` (existing generic clarification
  path still fires).
- forbidden target `.env` referenced → never `GROUNDED`; safe refusal.
- explicit `create README.md` → allowed via the create-file path only when safe.
- determinism: identical input + index → identical result across repeated runs.

Integration / route tests (`#17C`, extend `test_chunk_routes.py`):

- `NOT_FOUND` / `AMBIGUOUS` → `status="needs_clarification"`, specific message,
  **no run created**, no DB rows inserted, `run_triage` not called.
- `GROUNDED` → run proceeds with `files_expected` pinned to the one path; pinned
  path still passes `ground_triage_result_paths` and `scope_guard`.

Regression: existing `test_implementation_guard.py`, `test_intent.py`, and
`test_chunk_routes.py` behavior is unchanged for all current cases.

---

## 9. Recommended PR split

- **#17A** — this design doc only. No code.
- **#17B** — `file_alias_grounding.py` deterministic helper + unit tests; starts
  with the red test reproducing the dogfood failure path. No route wiring.
- **#17C** — wire the resolver into the `/runs/chunked` IMPLEMENTATION branch:
  pin `files_expected` on a unique match; specific clarification messages on
  not-found / ambiguous; route + integration tests.
- **#17D** — dogfood polish only if needed (message wording, alias-table tweaks).

---

## 10. Risks / open questions

- **Root cause unconfirmed.** The deterministic guard already accepts the
  example, so #17B must reproduce the exact failing path first. If the failure
  was a stale build, #17C is a smaller, additive change.
- **`file_index` staleness.** `ensure_repo_indexed` only checks presence, not
  freshness, so a newly-added README may be absent from the index and (correctly)
  trigger a create-clarify. Note re-index guidance to users; clarifying is the
  safe outcome.
- **Pinning `files_expected` is a real runtime change** (in #17C, not #17A). It
  must be additive and continue through every existing guard.
- **Alias-table breadth.** Keep the table minimal in #17B; entity/symbol aliases
  are deferred.

---

## 11. What this PR explicitly does NOT do

- Does not recommend LLM-only file grounding.
- Does not recommend broad fuzzy matching without confidence.
- Does not recommend automatic multi-file selection.
- Does not bypass the scope guard.
- Does not create missing files by default.
- Does not edit forbidden files.
- Does not change patch application.
- Changes no runtime behavior and no safety guard. Only this document is added.

---

## 12. #17C implemented (wiring + confirmed root cause)

**Confirmed root cause (supersedes the §10 "unconfirmed" note):** the generic
"too vague" rejection of `add hello in the readme` comes from the deterministic
9A guard, not the LLM. In `assess_implementation_specificity`, `readme`
fuzzy-matches the verb `rename` (Levenshtein distance 2 for a length-6 token),
so it is stripped as a describe-only word; with `add`/`hello`/`in`/`the` also
stripped, no concrete anchor remains → vague. #17C does **not** change that guard
(deferred); instead the resolver supplies the missing concrete anchor.

**Integration (`backend/routes/chunks.py`, `create_chunked_run_route`,
IMPLEMENTATION branch):**

- The resolver runs **first**, but **only when the project's index is non-empty**
  (`get_indexed_paths_and_dirs(project_id).is_empty` is False). The index is
  built lazily by `run_triage`, so an empty index here means "not indexed yet,"
  not "target missing" — in that case the resolver is skipped and the existing
  guard applies unchanged.
- `GROUNDED(path)` → the grounded path is a concrete anchor, so the request is
  specific: the 9A/LLM vague guard is **bypassed**, and after `run_triage` the
  single-chunk plan's `files_expected` is pinned to `[path]`
  (`_pin_single_chunk_files_expected`; multi-chunk plans are left to normal
  grounding — conservative, no guess). The pinned path still flows through
  `ground_triage_result_paths` → `scan_triage_result` → `scope_guard` unchanged.
- `NOT_FOUND` / `AMBIGUOUS` / `FORBIDDEN` → return a **specific**
  `_needs_clarification_response` before `run_triage` (no run row created).
- `NO_TARGET` → fall through to the existing specificity guard (generic
  clarification for genuinely vague requests).

**Index-gate limitation (intentional):** on a never-indexed project the resolver
is skipped, so the `add hello in the readme` phrasing still hits the generic
guard until the project is indexed. Auto-indexing in the route would require a
filesystem walk, which is out of scope here.

**Still deferred:** fuzzy/entity aliases (`user model`, `login button`),
LLM-based grounding, and automatic file creation. `create README.md` for a
missing file returns a clarification stating auto-creation is not supported yet.
