# Clarification Selection Handoff (PR #17K design)

**Status:** Design / docs only. No code, no runtime behavior, no DB/schema, no
route, no frontend, and no safety guard is changed by this PR. All behavior is
specified here for follow-up PRs #17L–#17N, tests-first.

**Phase:** 2H — Dogfood + Reliability Hardening.

**Scope guard for this PR:** does not touch `file_alias_grounding.py`,
`file_candidate_ranking.py`, `chunks.py`, `scope_guard`, `patch_applier`,
memory, DB/schema, frontend, approvals, git, or providers. It only adds this
design document. No runtime path is wired, imported, or modified.

---

## 1. Problem statement

#17J turned the ambiguous-file clarification into a ranked, numbered list with an
optional recommendation. For the dogfood request:

```
add hello in the main readme
```

with three indexed README files, `/runs/chunked` (`chunks.py:654`) returns the
read-only `needs_clarification` envelope (HTTP 200, **no run row**) built by
`_target_ambiguous_response` (`chunks.py:198`):

```
I found multiple README files. I think you mean README.md because it is
root-level. Confirm, or choose one:
  1. README.md
  2. docs/adr/README.md
  3. docs/architecture/README.md
```

The envelope already carries the additive fields (`chunks.py:102`):

```jsonc
{
  "status": "needs_clarification",
  "candidates": ["README.md", "docs/adr/README.md", "docs/architecture/README.md"],
  "recommended_path": "README.md",
  "recommendation_strength": "strong"
}
```

**The gap.** If the user replies with the *natural* answer:

```
yes 1     |    option 1    |    use 1    |    1
```

`/runs/chunked` treats it as a brand-new feature request. `"use 1"` resolves to
`NO_TARGET` (no path token, no alias word), so it falls into the generic
specificity guard and asks for clarification *again*. The user is forced to retype
the full intent with the exact path:

```
add hello to README.md
```

This is **safe** (no wrong file is ever edited, no run is created) but
**incomplete UX**: the tool just showed a numbered list and then refused to honor
a reply that references it.

### The two things actually missing

1. **Selection context.** "1" only means something *relative to the candidate
   list we just showed*. There is currently nowhere to remember that list across
   the two requests.
2. **A deterministic selection parser** that maps `1` / `yes 1` / `use README.md`
   onto exactly one of the previous candidates — and rebuilds the *original*
   intent ("add hello") with the chosen exact path substituted for the ambiguous
   alias ("main readme").

### Anti-goal (explicitly rejected)

- **No global "1" interpretation.** `"1"` must *never* be parsed as a selection
  outside an explicit clarification context. A bare `/runs/chunked` POST of `"1"`
  stays exactly as today (non-actionable / needs clarification).
- **No memory of previous picks.** A selection resolves the *current*
  clarification only. We do not learn "this user usually means README.md".
- **No LLM.** Selection parsing is pure string handling.
- **No auto-select.** A recommendation is still a hint; a run is created only
  after the user actively selects (clicks a button or types a selection).

---

## 2. Design goals & non-goals

**Goals**

- Remember the previous clarification's candidate set long enough for the user to
  pick from it.
- Accept the natural replies: `1`, `yes 1`, `option 1`, `use 1`, `README.md`,
  `use README.md` (and, when a recommendation exists, a bare `yes` / `confirm`).
- Map a selection **only** within that clarification's candidate set.
- Preserve the original feature intent, swapping the ambiguous alias for the
  selected exact path.
- Re-enter the **existing** `/runs/chunked` pipeline so every guard
  (resolver → ground → risk scan → scope_guard → chunk-plan approval → final
  approval → patch_applier) still runs unchanged.
- Deterministic: identical `(context, selection)` → identical result.

**Non-goals (do NOT build)**

- Global / context-free "1" interpretation.
- Any memory of past selections.
- LLM selection or fuzzy matching of the reply.
- Auto-select while >1 candidate exists.
- A new approval bypass, scope relaxation, or create-target change.
- A DB/schema change (kept only as a discussed alternative, §4.3).
- Selecting a path that was not in the previous candidate set.

---

## 3. Architecture overview

The selection step is a thin, deterministic front-door that **rebuilds an
unambiguous request and feeds it back into the existing route core**. It owns no
edit logic of its own.

```
POST /runs/chunked                         # unchanged entry for first request
  └─ resolve_explicit_edit_target() = AMBIGUOUS
        └─ rank_ambiguous_file_candidates()         # #17I
              └─ _target_ambiguous_response()        # #17J
                    └─ needs_clarification envelope
                         + clarification_id          # NEW (#17M) — carries context

        ── user replies "yes 1" / clicks "Use README.md" ──>

POST /runs/chunked/clarifications/{clarification_id}/select   # NEW (#17M)
  ├─ decode + validate context (project, expiry, integrity)   # #17L helper
  ├─ parse_selection(reply, candidates) -> exact path | reject # #17L helper (pure)
  │     • reject  -> re-emit needs_clarification (NO run)
  └─ rebuild feature_description with the exact path
        └─ delegate to the SAME create-chunked-run core         # all guards intact
              └─ resolve_explicit_edit_target() = GROUNDED       # re-validated vs index
                    └─ normal chunk plan awaiting approval
```

**Key safety property:** the selection endpoint never edits or grounds anything
itself. It produces a normal feature request string and hands it to the existing
core, which **re-resolves the chosen path against the live repo index**. The
stored candidate list is used only to (a) bound the numeric/affirmation mapping
and (b) reject out-of-set replies — it is *not* trusted as an edit authority. If
the index changed between clarify and select and the path is gone, the core
simply re-clarifies; it can never edit a stale path.

---

## 4. Storage of clarification context

The context must survive one round-trip (clarify → select). Required fields:

```jsonc
{
  "version": 1,
  "project_id": "…",                  // bind selection to the same project
  "original_feature_description": "add hello in the main readme",
  "alias": "readme",                  // the ambiguous alias label (audit/UX)
  "candidates": ["README.md", "docs/adr/README.md", "docs/architecture/README.md"],
  "recommended_path": "README.md",    // null when recommendation == none
  "recommendation_strength": "strong",
  "created_at": "2026-06-01T12:00:00Z",
  "expires_at": "2026-06-01T12:30:00Z"
}
```

### 4.1 Recommended: Option D — stateless, signed `clarification_id`

The context is serialized, **HMAC-signed**, and returned as an opaque
`clarification_id` string. The select endpoint decodes it, verifies the
signature and `expires_at`, and proceeds. **No DB row, no schema change, no
server-side table to clean up.**

Why this is the best fit for Pipewright:

- **No schema change** — honors "do not change DB/schema unless only discussed as
  an option."
- **Self-expiring** — `expires_at` lives in the payload; no cleanup job.
- **Tamper-evident** — the HMAC means a client cannot widen/swap the candidate
  list (satisfies req #4, "no path outside the previous candidates"), without us
  trusting client-echoed JSON.
- **Stateless / restart-tolerant in design** — nothing to persist.

**Signing key (grounded constraint).** The only existing secret,
`PIPEWRIGHT_ENCRYPTION_KEY` (`backend/security/secrets.py:13`), is **optional**
and is *not* set in the default `local_only` mode. We therefore must **not**
require it for clarification signing — doing so would break the default local
flow. Recommendation:

- Generate a **process-ephemeral HMAC key** at startup (`os.urandom`) when no
  configured key is present. Clarifications are short-lived (§4.4), so a server
  restart invalidating outstanding `clarification_id`s is acceptable — the user
  simply re-submits the original request and gets a fresh clarification.
- If `PIPEWRIGHT_ENCRYPTION_KEY` *is* configured, derive a stable signing key
  from it so clarifications survive restarts. (Optional; not required.)

This keeps zero new required configuration while still satisfying req #4.

### 4.2 Rejected alternatives

- **Option A — frontend-only temporary state.** The frontend already holds the
  `candidates`/`recommended_path` from #17J, so it *could* rewrite the request
  client-side and never need a backend selection path. Rejected as the primary
  design because it pushes the candidate-set constraint (req #3, #4) onto the
  client: the backend would have to trust a client-supplied candidate list, which
  cannot enforce "no path outside the previous candidates" server-side. (The
  frontend *will* still render buttons — §6 — but it echoes the signed
  `clarification_id`, it does not own the constraint.)
- **Option C — reuse run/session/project state.** There is **no run row** to hang
  context on: the ambiguous clarification deliberately returns
  `_needs_clarification_response` with no `pipeline_runs` insert
  (`chunks.py:83`). Creating a run just to store context would violate "do not
  create a run until a valid candidate is selected" (req #5) and "never create
  empty runs." Project state is shared/long-lived and wrong for a per-request,
  expiring datum. Rejected.

### 4.3 Discussed-only alternative: Option B — DB table

A `clarification_contexts` table (id, project_id, original_feature_description,
alias, candidates JSON, recommended_path, created_at, expires_at) is the
straightforward durable option if we later want **audit/telemetry** of how often
selections happen, or restart-durable clarifications without depending on a
configured key. It is **not recommended for #17K/#17L** because it adds schema
for a transient value that the stateless signed token handles without migration.
Kept here only as the documented escalation path if telemetry/audit becomes a
requirement.

### 4.4 Expiry

- TTL: **30 minutes** (`expires_at = created_at + 30m`). Long enough for a human
  to read and pick; short enough that stale context cannot accumulate or be
  replayed much later against a drifted index.
- On select after expiry → return a `needs_clarification` envelope explaining the
  clarification expired and asking the user to re-submit the original request.
  **No run is created.**
- For Option D the check is purely `now > expires_at` on the decoded payload; no
  storage to sweep.

### 4.5 Project mismatch

The select endpoint receives `project_id` (path/body) **and** the
`project_id` baked into the signed context. If they differ → reject with a
`needs_clarification`/400 and create **no run**. This prevents replaying a
clarification issued for project A against project B.

### 4.6 Stale index after clarification

Handled by design, not by trusting the snapshot: the select path rebuilds a
normal feature request and re-runs `resolve_explicit_edit_target` against the
**current** index. Outcomes:

- Path still indexed → `GROUNDED` → normal flow. ✔
- Path no longer indexed → `NOT_FOUND` → safe re-clarification, no run. ✔
- Set changed so it is ambiguous again → `AMBIGUOUS` → fresh clarification. ✔

The stored candidate list never causes an edit to a vanished path.

---

## 5. Selection parser & request rebuild (PR #17L helper)

New **pure** helper, e.g. `backend/pipeline/clarification_selection.py`. No DB,
no FS, no index, no LLM. Signature sketch:

```python
def parse_clarification_selection(
    reply: str,
    candidates: Sequence[str],
    recommended_path: str | None,
) -> SelectionResult: ...   # SELECTED(path) | UNRECOGNIZED
```

### 5.1 Accepted forms (deterministic)

Normalize: lowercase a working copy, strip surrounding whitespace/punctuation,
collapse internal whitespace. Strip a **fixed, tiny** allowlist of leading
selection words: `yes, use, pick, choose, select, option, number, confirm, the,
#`. Then classify the remainder:

| Reply (examples)                | Resolves to                                  |
|---------------------------------|----------------------------------------------|
| `1`, `#1`, `1.`, `option 1`, `use 1`, `yes 1` | `candidates[0]` (1-based index → path) |
| `README.md`, `use README.md`    | that path **iff** it ∈ `candidates`          |
| `docs/adr/README.md`            | that path **iff** it ∈ `candidates`          |
| `yes`, `confirm` (alone)        | `recommended_path` **iff** it is non-null    |
| `2 or 3`, `the first one`, `idk`, `option 9` | `UNRECOGNIZED` → re-clarify     |

Rules:

1. **Index form:** the remainder is a single integer `N`. Map to
   `candidates[N-1]` iff `1 <= N <= len(candidates)`; otherwise `UNRECOGNIZED`.
   No global meaning — `candidates` is always the explicit context argument.
2. **Path form:** the remainder, normalized the same way the resolver normalizes
   a path token (`file_alias_grounding._normalize_path_token`), must be **exactly
   one** of `candidates`. Comparison matches the index's stored casing (resolver
   is case-sensitive for explicit paths). Not in set → `UNRECOGNIZED`.
3. **Affirmation-only form:** an empty remainder *after* stripping an affirmation
   word (`yes`/`confirm`) → `recommended_path` if non-null, else `UNRECOGNIZED`.
   Confirming a shown recommendation is a deliberate user action, not auto-select.
4. **Anything else / ambiguous reply** ("2 or 3", "both", free text) →
   `UNRECOGNIZED`. Never guess. Never partial-match. Never pick "closest".

`UNRECOGNIZED` is never an error that creates a run — it re-emits the same
clarification (same candidates) so the user can try again.

### 5.2 Request rebuild (req #6: preserve intent, swap alias → exact path)

We do **not** do fragile in-place alias substitution (the alias word may appear
multiple times or with varied casing). Instead we **append the chosen exact
path** to the original intent so the resolver grounds it deterministically:

```python
rebuilt = f"{original_feature_description.rstrip().rstrip('.')} (use {selected_path})"
# "add hello in the main readme" + README.md
#   -> "add hello in the main readme (use README.md)"
```

Why this is safe and deterministic:

- `_resolve_explicit_path` scans tokens left-to-right and returns the **first
  explicit-path token** *before* the alias branch runs (`file_alias_grounding.py:380`).
  The appended `README.md` is an explicit path token; the bare alias word
  `readme` is not. So the rebuilt request grounds straight to `GROUNDED(README.md)`.
- By construction the original was `AMBIGUOUS`, which means **no** explicit path
  token existed earlier in the text — so the appended path is unambiguously the
  first one. No risk of an earlier token winning.
- The rebuilt string flows through the *entire* normal core: resolver →
  `_pin_single_chunk_files_expected` → `ground_triage_result_paths` →
  `scan_triage_result` → scope_guard → approvals → patch_applier. Nothing is
  special-cased or bypassed.

The selection helper returns the chosen path; the **route** owns the rebuild
string (keeping UX/text in the route layer, like #17J kept reason copy in the
route).

---

## 6. Route wiring (PR #17M)

### 6.1 Endpoint shape — recommended

```
POST /runs/chunked/clarifications/{clarification_id}/select
Body: { "project_id": "...", "selection": "yes 1" }
```

Returns **either**:

- `ChunkPlanResponse` — a valid selection produced a chunk plan awaiting approval
  (identical to any specific request), **or**
- `needs_clarification` envelope (HTTP 200) — expired / project mismatch /
  `UNRECOGNIZED` / stale-index re-clarification. **No run created.**

Flow:

1. Decode + verify `clarification_id` (signature, `version`, `expires_at`).
   Invalid/expired → `needs_clarification` (re-submit). No run.
2. Verify `body.project_id == context.project_id`. Mismatch → reject. No run.
3. `parse_clarification_selection(selection, candidates, recommended_path)`.
   `UNRECOGNIZED` → re-emit the same clarification (same candidates +
   `clarification_id`). No run.
4. `SELECTED(path)` → rebuild feature_description (§5.2) and **delegate to the
   existing create-chunked-run core** (the body of
   `create_chunked_run_route`, factored into a shared helper). All guards run.

**Why a dedicated endpoint over reusing `POST /runs/chunked` with
`clarification_id` + `selection`:**

- Keeps `/runs/chunked` semantics simple: a feature request in, a plan-or-
  clarification out. No optional-coupled fields where `selection` only means
  something if `clarification_id` is also present.
- Makes the selection path independently testable and explicitly named.
- Guarantees "1" is interpreted **only** on this endpoint with a valid
  `clarification_id` — there is no code path where `/runs/chunked` parses "1" as
  a selection (req #3).

The reuse variant (`POST /runs/chunked` with both fields) is the documented
alternative if we later prefer a single entry point; it must enforce the same
"selection ignored unless a valid clarification_id is present" rule.

### 6.2 Ambiguous response gains `clarification_id`

`_target_ambiguous_response` (`chunks.py:198`) additively includes a signed
`clarification_id` alongside the existing `candidates` / `recommended_path` /
`recommendation_strength`. Backward compatible: existing fields unchanged; older
clients ignore the new field and keep typing exact paths.

### 6.3 Refactor note (no behavior change)

Factor the implementation branch of `create_chunked_run_route`
(`chunks.py:654`) into a reusable internal function that both the original route
and the select endpoint call with a `feature_description`. This is a pure
extract-method; the original public behavior is byte-for-byte unchanged.

---

## 7. Frontend UX (PR #17N)

Today the clarification renders message + `missing_details` + `examples` as text
(`ProjectDashboard.tsx:173`) and the user must retype. Changes:

- Extend `NeedsClarificationResponse` (`client.ts:236`) with the additive
  optional fields: `candidates?: string[]`, `recommended_path?: string | null`,
  `recommendation_strength?: 'strong' | 'weak' | 'none'`,
  `clarification_id?: string`.
- When `candidates` is present, render one **button per candidate**:
  - `Use README.md`, `Use docs/adr/README.md`, …
  - Visually mark the `recommended_path` button (e.g. "Recommended").
- Clicking a button calls the select endpoint with the exact path as
  `selection` and the echoed `clarification_id`.
- Keep a small **text reply** field that routes the same way (so `yes 1` /
  `1` / `use README.md` typed by the user hit the same backend parser). The
  frontend does **not** parse "1" itself — it forwards the raw reply so the
  deterministic mapping + candidate constraint stay server-side.
- On a `ChunkPlanResponse` result, navigate to the run (same as a normal
  successful submit). On a returned `needs_clarification` (UNRECOGNIZED / expired),
  re-render the clarification (with refreshed `clarification_id`).

No auto-click, no pre-selection, no client-side path validation as authority.

---

## 8. Tests for the follow-up implementations

### 8.1 Pure selection parser (`test_clarification_selection.py`, #17L)

Candidates: `["README.md", "docs/adr/README.md", "docs/architecture/README.md"]`,
`recommended_path = "README.md"`.

- `1`, `#1`, `1.`, `option 1`, `use 1`, `yes 1`, `select 1` → `README.md`.
- `2` → `docs/adr/README.md`; `3` → `docs/architecture/README.md`.
- `0`, `4`, `9` (out of range) → `UNRECOGNIZED`.
- `README.md`, `use README.md` → `README.md`.
- `docs/adr/README.md` → that path; `docs/other/README.md` (not in set) →
  `UNRECOGNIZED`.
- `yes` / `confirm` alone → `recommended_path` (`README.md`); with
  `recommended_path=None` → `UNRECOGNIZED`.
- `2 or 3`, `both`, `the main one`, `` (empty), garbage → `UNRECOGNIZED`.
- **Determinism / purity:** identical args → identical result; inputs not mutated.
- **No global meaning:** parser requires `candidates` as an argument; there is no
  module-level/default candidate list.

### 8.2 Context codec (`test_clarification_context.py`, #17L)

- Round-trip encode → decode yields identical fields.
- Tampered token (flipped byte / swapped candidate) → decode fails (rejected).
- Expired `expires_at` → decode reports expired.
- Version mismatch → rejected.

### 8.3 Route / handoff (`test_chunk_routes.py`, #17M)

- Ambiguous first request → envelope now includes a non-empty `clarification_id`.
- `select` with `1` / `yes 1` / `use README.md` → `ChunkPlanResponse`, run in
  `awaiting_chunk_plan_approval` (a run **is** created now, but only after a valid
  selection — assert it did not exist before select).
- `select` with `UNRECOGNIZED` reply → `needs_clarification`, **no run created**.
- `select` with expired `clarification_id` → `needs_clarification`, no run.
- `select` with mismatched `project_id` → rejected, no run.
- Rebuilt request grounds to the **selected** path (assert `files_expected ==
  [selected_path]`), and a *different* in-index path cannot be reached via an
  out-of-set selection.
- **No-global-"1" regression:** `POST /runs/chunked` with `feature_description ==
  "1"` behaves exactly as today (non-actionable / needs clarification), proving
  "1" is never globally a selection.
- Stale index: candidate removed from the index before select → select returns a
  safe re-clarification / not-found, **no run**, no edit.
- All existing single-candidate `GROUNDED`, `NOT_FOUND`, `FORBIDDEN`, and
  non-ambiguous flows unchanged (regression).

### 8.4 Frontend (#17N)

- Given `candidates`, buttons render, one per candidate, recommended marked.
- Clicking a button posts the exact path + `clarification_id` to the select
  endpoint.
- Text reply field forwards the raw string (no client-side "1" parsing).
- `ChunkPlanResponse` → navigates to run; `needs_clarification` → re-renders.

---

## 9. Recommended PR split

- **#17K** — this design doc only (no code, no behavior change).
- **#17L** — backend clarification context codec + selection parser
  (`clarification_context.py`, `clarification_selection.py`) + unit tests. Pure
  helpers; **no route wiring**, no behavior change.
- **#17M** — route wiring: add `POST /runs/chunked/clarifications/{id}/select`,
  add `clarification_id` to the ambiguous response, extract the shared
  create-chunked-run core, `test_chunk_routes.py` coverage. No new bypass.
- **#17N** — frontend candidate selection buttons + text-reply forwarding.

Each PR has one purpose; #17L is independently revertible and adds no runtime
path until #17M imports it.

---

## 10. Safety boundaries (invariants preserved)

The design must NOT:

- interpret a selection (`1`, `yes 1`, …) outside a valid `clarification_id`
  context;
- map a selection to any path not in that clarification's candidate set;
- create a run until a valid candidate is selected;
- auto-select while more than one candidate exists (a recommendation is a hint;
  `yes`/`confirm` is an explicit user confirmation, not auto-select);
- trust a client-supplied candidate list as an edit authority (the rebuilt
  request is always re-resolved against the live index);
- invent paths, or edit a path that left the index after the clarification;
- use an LLM anywhere in selection;
- bypass resolver, ground, risk scan, scope_guard, approvals, or patch_applier;
- require new mandatory configuration (signing key falls back to a
  process-ephemeral key);
- change DB/schema (Option B is documented only as an escalation path);
- add any memory of previous selections.

Single-candidate, non-ambiguous, and all first-request flows are byte-for-byte
unchanged.

---

## 11. Risks / open questions

- **Bare `yes`/`confirm` → recommended_path.** The clarification copy says
  "Confirm, or choose one", so honoring `yes` is expected UX. Risk: a user types
  `yes` meaning "yes, but the second one" — mitigated because `yes` maps to the
  recommendation *only when one exists*, and the rebuilt request still goes
  through chunk-plan approval where the wrong file is visible before any edit. If
  considered too liberal, drop affirmation-only and require an explicit index.
- **Signing key source.** Ephemeral key invalidates clarifications on restart;
  acceptable for a 30-minute transient. If durability is wanted, derive from
  `PIPEWRIGHT_ENCRYPTION_KEY` when set, or escalate to Option B. Open question:
  do we want restart-durable clarifications at all in local self-use? (Probably
  no.)
- **TTL value.** 30 min is a guess; tune from dogfood. Too long risks replay
  against a drifted index (mitigated by re-resolution); too short annoys.
- **Casing of typed exact path.** Resolver is case-sensitive for explicit paths;
  the parser matches candidates with index casing. A user typing `readme.md`
  (wrong case) → `UNRECOGNIZED` → re-clarify. Acceptable; buttons avoid this.
- **Multi-target / multi-chunk requests.** Out of scope: ambiguity here is a
  single named alias resolving to several files. Multi-file disambiguation is not
  addressed and must not be force-fit onto this single-selection flow.
- **Reply containing a number that is also a path token.** Not realistic for
  indexed paths; the index/path branch only matches full candidate paths.

---

## 12. Confirmation: no runtime behavior changed by #17K

This PR adds **only** this document. No module is imported, wired, or modified.
`file_alias_grounding.py`, `file_candidate_ranking.py`, `chunks.py`, scope_guard,
patch_applier, memory, DB/schema, frontend, approvals, git, and providers are
untouched. The current ambiguous clarification (`chunks.py:198`) continues to
return the existing safe envelope, and a reply like `yes 1` continues to be
treated as a new request until #17M ships. All safety invariants in §10 hold
unchanged.
