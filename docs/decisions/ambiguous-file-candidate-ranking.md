# Ambiguous File Candidate Ranking & Clarification UX (PR #17H design)

**Status:** Design / docs only. No code, no runtime behavior, and no safety guard
is changed by this PR. All behavior changes are specified here for follow-up PRs
#17I–#17K, tests-first.

**Phase:** 2H — Dogfood + Reliability Hardening.

**Scope guard for this PR:** does not touch `file_alias_grounding.py`,
`chunks.py`, `scope_guard`, `patch_applier`, memory, DB, frontend, approvals,
git, or providers. It only adds this design document. No runtime path is wired,
imported, or modified.

---

## 1. Problem statement

During dogfooding, the request:

```
add hello in the main readme docs not in the architecture or adr readme
```

is resolved by `resolve_explicit_edit_target` (file_alias_grounding.py) to the
`AMBIGUOUS` outcome with three indexed candidates:

```
README.md
docs/adr/README.md
docs/architecture/README.md
```

The route then returns the generic clarification built in
`_target_ambiguous_response` (chunks.py:187):

```
I found multiple files matching "readme": README.md, docs/adr/README.md,
docs/architecture/README.md. Which one should I edit?
```

This is **safe** (no auto-select, no run created) but **unhelpful**: the user
gave three strong hints — *main* readme, *not* architecture, *not* adr — and the
tool ignored all of them. The fix is a deterministic ranking layer that uses
those hints to surface a recommendation, **while still requiring the user to
confirm an exact path**. Nothing auto-selects while more than one candidate
exists.

### Anti-goal (explicitly rejected)

Do **not** hardcode project-specific folder rules:

```python
if "not adr" in text: exclude("docs/adr/README.md")        # NO
if "architecture" in text: prefer("docs/architecture/...") # NO
```

These do not generalize across repositories. All scoring must derive from
**generic path tokens** compared against **generic request tokens**.

---

## 2. Design goals & non-goals

**Goals**

- Keep the candidate set deterministic and index-based (unchanged from #17B/#17C).
- Single candidate → existing behavior, byte-for-byte unchanged.
- Multiple candidates → rank with generic path-token scoring; optionally surface
  one recommendation with a human-readable reason.
- Always require human confirmation of an exact path before a run is created.
- No LLM in the first implementation.
- Deterministic: identical `(request_text, candidates)` → identical result.

**Non-goals (do NOT build)**

- Hardcoded folder exclusion rules.
- LLM auto-select, route auto-selection, or any auto-select while >1 candidate.
- Fuzzy / entity aliases ("user model", "login button").
- Embeddings / vector search.
- File watcher, index rebuild, or arbitrary path creation.
- Memory of previous user picks.
- Any change to `CREATE_TARGET` behavior.

---

## 3. Architecture overview

Two new, additive layers — both downstream of the existing deterministic
resolver, neither of which can widen or invent the candidate set:

```
resolve_explicit_edit_target()            # unchanged (#17B/#17C)
        │  AMBIGUOUS(alias, candidates)   # candidates already sorted, index-based
        ▼
rank_ambiguous_file_candidates(           # NEW pure helper (#17I)
    request_text, candidates)             #   - deterministic, no DB/FS/LLM
        │  RankedCandidateResult          #   - ordered SUBSET of candidates
        ▼
_target_ambiguous_response(...)           # UPDATED payload (#17J)
        │                                 #   - numbered candidates + optional rec
        ▼
needs_clarification envelope (HTTP 200)   # no run row, user must confirm
```

The ranker is a **pure function of its two arguments**. It does not read the
index, the DB, the filesystem, or any LLM. The route is the only place that owns
the candidate list (from the resolver), so the ranker can never add or invent a
path — its output is always a permutation/subset of its input.

---

## 4. Deterministic ranking helper (PR #17I)

### 4.1 Module & signature

New module: `backend/pipeline/file_candidate_ranking.py`

```python
def rank_ambiguous_file_candidates(
    request_text: str,
    candidates: Sequence[str],
) -> RankedCandidateResult: ...
```

Hard contract:

- Takes only the request text and already-indexed candidate paths.
- Does **not** query the DB, walk the filesystem, call an LLM, or read files.
- Does **not** mutate any argument or global state.
- **Never** adds candidates and **never** invents paths: every path in the
  result is `in candidates`.
- Pure & deterministic: same inputs → same output, every call.

### 4.2 Result type

```python
class Recommendation(str, Enum):
    NONE   = "none"    # tie / weak signal → no highlighted pick
    WEAK   = "weak"    # a leader exists but margin/signal is soft
    STRONG = "strong"  # clear leader with a discriminating signal

@dataclass(frozen=True)
class RankedCandidateResult:
    ordered: tuple[str, ...]          # all input candidates, best-first (subset)
    recommended: str | None           # ordered[0] when recommendation != NONE
    recommendation: Recommendation
    reason_tokens: tuple[str, ...]    # generic tokens that justify the pick
    #   e.g. ("root-level",) or ("matched:docs", "matched:adr")
```

`reason_tokens` carry *structured* justification (so the route renders the
sentence; the helper does not own UX copy). They reference only depth/positive/
negation signals and actual path tokens — never project-specific phrasing.

### 4.3 Tokenization (generic, shared rules)

- Lowercase; split request and each path on any non-alphanumeric run
  (`/`, `.`, `-`, `_`, whitespace).
- Drop a small generic stopword set (`the, a, an, in, to, of, and, or, file,
  files, please, add, edit, into, on, at`). Keep it tiny and generic.
- The alias label token itself (e.g. `readme`) appears in **every** candidate,
  so it contributes no discriminating signal and is effectively neutral — no
  special-casing required, but it must not be treated as a positive match that
  inflates all candidates equally (it cancels out in relative scoring).

### 4.4 Scoring signals (all generic)

Score each candidate independently, then rank. Signals, in rough priority:

1. **Negation filtering (strongest, generic).**
   Scan request tokens for negation cues: `not, no, except, other than,
   rather than, instead of, excluding, without`. Tokens appearing *after* a
   cue (until a clause boundary — `,`, `;`, `.`, or end) form the **negation
   set**. Any candidate whose path segments intersect the negation set is
   **demoted hard** (large negative weight), enough to sink it below any
   non-negated candidate. Negation compares user tokens against *actual path
   segments only* — no hardcoded `adr`/`architecture`.
   - Example: "not in the architecture or adr readme" → negation set
     `{architecture, adr}` → demote `docs/architecture/README.md` and
     `docs/adr/README.md`.

2. **Root / depth preference (generic words).**
   If the request contains a generic root cue (`main, root, top, top-level,
   toplevel, project, primary, base`), prefer **shallower** paths (fewer `/`
   segments). Depth is a mild tiebreaker even without a cue, but only a cue
   makes it a *strong* signal.
   - Example: "main readme" + root cue → `README.md` (depth 0) outranks
     `docs/*/README.md` (depth 2).

3. **Positive token overlap.**
   Count request tokens (post-stopword, post-negation) that match a path
   segment or the basename (extension stripped). More overlap → higher score.
   - Example: "docs adr readme" → `docs/adr/README.md` gets `+docs +adr`.

4. **Exact path mention (defensive).**
   If the user typed an exact candidate path, the existing resolver already
   grounds it to `GROUNDED` upstream — so the ranker rarely sees this case. If
   it does (e.g. label+path both present), the exactly-mentioned candidate
   ranks first. The ranker must not rely on this for correctness; it is a
   defensive tiebreak only.

### 4.5 Decision rules (recommendation strength)

- Compute scores; sort by `(score desc, original-sorted-path asc)` for a stable,
  deterministic order. `ordered` is always the full input set in this order.
- **All candidates eliminated by negation** → do not collapse the set. Fall back
  to `ordered = sorted(candidates)`, `recommended = None`,
  `recommendation = NONE`. (Never return an empty list; never auto-pick the
  "least bad" of an all-negated set.)
- **Single clear leader** (positive margin over #2 **and** at least one positive
  or negation signal contributed) → `STRONG`, `recommended = ordered[0]`.
- **Leader exists but margin is small or only depth contributed** → `WEAK`.
- **Tie at the top** (equal top score) → `NONE`, `recommended = None`.
- The ranker **never** drops a candidate from `ordered` — even a negated one
  stays in the list (just last), because the user must still be able to pick it.
  Negation only affects *ranking and recommendation*, not the *available
  choices*.

### 4.6 Worked examples

| Request | Recommended | Strength | Why |
|---|---|---|---|
| `...main readme docs not in the architecture or adr readme` | `README.md` | STRONG | root cue + negation sinks adr/architecture |
| `add hello in docs adr readme` | `docs/adr/README.md` | STRONG | positive overlap `docs`,`adr` |
| `add hello in architecture readme` | `docs/architecture/README.md` | STRONG | positive overlap `architecture` |
| `add hello in readme` | none, or `README.md` WEAK | WEAK/NONE | only depth signal, no discriminator |
| `not in README.md or docs/adr` (negates all) | none | NONE | all negated → full list, no rec |
| equal-scoring candidates | none | NONE | tie → no strong rec |

`ordered` in every row is a permutation of the exact input candidates.

---

## 5. Clarification UX (PR #17J)

`_target_ambiguous_response` (chunks.py:187) is updated to consume the ranker
result and emit a **numbered** clarification, optionally with a recommendation.
It still returns the read-only `needs_clarification` envelope (HTTP 200) via
`_needs_clarification_response` — **no run row, no auto-select**.

**With a recommendation (STRONG/WEAK):**

```
I found multiple README files. I think you mean README.md because it is
root-level and your request excludes adr/architecture. Confirm, or choose one:
  1. README.md
  2. docs/adr/README.md
  3. docs/architecture/README.md
```

**No recommendation (NONE):**

```
I found multiple README files. Choose one exact path:
  1. README.md
  2. docs/adr/README.md
  3. docs/architecture/README.md
```

### 5.1 Payload shape (additive, backward-compatible)

Extend the existing envelope; do not remove fields. New optional fields:

```jsonc
{
  "status": "needs_clarification",
  "message": "...numbered, with optional recommendation sentence...",
  "missing_details": ["which exact path to edit: 1) README.md  2) ... 3) ..."],
  // NEW (optional, additive):
  "candidates": ["README.md", "docs/adr/README.md", "docs/architecture/README.md"],
  "recommended_path": "README.md",            // null when recommendation == NONE
  "recommendation_strength": "strong"          // "strong" | "weak" | "none"
}
```

- `candidates` is the ranker's `ordered` tuple (best-first) — a subset of the
  resolver output.
- The reason sentence is rendered from `reason_tokens` in the route, not stored
  by the helper.
- The user must reply with an exact path (or number that maps to one); only then
  does run creation proceed through the **existing** approval/scope/patch path.

### 5.2 Confirmation requirement (unchanged safety)

- The clarification creates **no run row** (still uses `_needs_clarification_response`).
- A recommendation is a hint, never a selection. The next request must name the
  exact path, which re-enters `resolve_explicit_edit_target` and grounds to a
  single `GROUNDED` target.
- No bypass of scope_guard, patch_applier, or approvals — all unchanged.

---

## 6. Optional future LLM advisory ranker (design only — do NOT build now)

An LLM ranker is **not** justified yet: the deterministic scorer handles the
observed dogfood cases. Design it only as a future fallback, behind a flag,
**default off**, invoked only when the deterministic scorer returns `NONE`
(tie/weak). Build it only if telemetry shows a real rate of unresolved ties.

### 6.1 Exact safety contract for the future LLM

The advisory ranker, if ever built, MUST:

1. Be invoked **only** when the deterministic scorer is tied/weak (`NONE`).
2. Receive **only** the request text + the **numbered candidate list** — no repo
   contents, no DB, no filesystem.
3. Return **only** a candidate **index** (1..N) or the literal `"uncertain"`.
4. **Never** return a path string. (Route maps index → path; the model never
   emits a path.)
5. **Never** invent paths and **never** select a path outside the provided list.
6. **Never** select a forbidden path (the candidate list never contains one;
   resolver already excludes forbidden targets, but the route re-validates the
   chosen index against the original candidate set regardless).
7. Treat malformed / out-of-range / non-integer output as `"uncertain"`.
8. Produce a reason that references only candidate numbers / path tokens /
   request tokens — never freeform invented content.
9. **Never auto-select.** Output is still a *recommendation*; the user must
   confirm an exact path. Identical safety as the deterministic recommendation.
10. Be gated behind a config flag, default off; absent/failed LLM → behave
    exactly as `recommendation = NONE`.

This keeps the LLM strictly **advisory and bounded**: worst case, a bad model
mislabels a recommendation the user can ignore — it can never widen scope,
invent a path, or create a run.

---

## 7. Safety boundaries (invariants preserved)

The design must NOT:

- auto-select when multiple candidates exist;
- create a run until the user confirms an exact path;
- let any LLM invent or emit paths;
- bypass the repo index (candidate set stays resolver/index-derived);
- bypass scope_guard / patch_applier / approvals / git;
- add fuzzy / entity aliases or embeddings / vector search;
- change `CREATE_TARGET` behavior;
- drop a candidate from the user-visible choice list (negation only re-ranks).

Single-candidate and non-ambiguous flows are byte-for-byte unchanged.

---

## 8. Tests for the follow-up implementations

### 8.1 Pure ranker (`test_file_candidate_ranking.py`, PR #17I)

Fixed candidate set unless noted:
`["README.md", "docs/adr/README.md", "docs/architecture/README.md"]`.

- `...main readme docs not in the architecture or adr readme`
  → `recommended == "README.md"`, strength STRONG, `reason_tokens` mention root
  + negation of `architecture`/`adr`.
- `add hello in docs adr readme` → recommends `docs/adr/README.md`.
- `add hello in architecture readme` → recommends `docs/architecture/README.md`.
- `add hello in readme` → no STRONG rec (NONE or WEAK root `README.md`); never
  auto-select.
- Negation eliminates all candidates → `recommended is None`, strength NONE,
  `ordered == sorted(candidates)` (full list preserved).
- Tie (e.g. two equally-scoring) → `recommended is None`, strength NONE.
- **Subset invariant:** `set(result.ordered) == set(candidates)` for every case.
- **Determinism:** calling the helper twice with identical args yields identical
  `RankedCandidateResult` (order, recommended, strength, reason_tokens).
- **Purity:** input list/strings are not mutated.
- Two-candidate and many-candidate (>3) sets still return a subset, no invented
  paths.

### 8.2 Route / UX (`test_chunk_routes.py`, PR #17J)

- Ambiguous resolver outcome → response includes numbered `candidates` (subset)
  and `missing_details`.
- When ranker yields STRONG/WEAK → response includes `recommended_path` and a
  reason sentence; when NONE → `recommended_path` is null and message says
  "choose one exact path".
- **No run row is created** for the ambiguous response (assert no run/patch/git
  path invoked) — same as today.
- Existing single-candidate `GROUNDED` and `NOT_FOUND`/`FORBIDDEN` flows are
  unchanged (regression assertions).
- A follow-up request naming the recommended exact path grounds to a single
  target and proceeds via the normal approval path (no shortcut).

---

## 9. Recommended PR split

- **#17H** — this design doc only (no code).
- **#17I** — pure deterministic ranker (`file_candidate_ranking.py`) +
  `test_file_candidate_ranking.py`. **No route wiring**, no behavior change.
- **#17J** — clarification UX/payload: wire ranker into
  `_target_ambiguous_response`, add numbered candidates + optional
  recommendation, `test_chunk_routes.py` coverage. No auto-select.
- **#17K (optional)** — index-age / coverage note in the clarification (e.g.
  "based on the current index; re-index if a file is missing"), if dogfooding
  shows stale-index confusion.
- **Future only** — LLM advisory ranker behind a default-off flag, *only* if
  telemetry proves a real tie/weak rate. Built to the section 6.1 contract.

Each PR has one purpose; #17I and #17J are independently revertible.

---

## 10. Should the LLM be used now?

**No.** The deterministic scorer (negation + root/depth + positive overlap)
resolves every observed dogfood case without a model call — cheaper, faster,
fully testable, and impossible to make it invent a path. The LLM is designed
here only as a bounded, default-off, advisory fallback for genuine ties, to be
built later if telemetry justifies it.

---

## 11. Risks / open questions

- **Negation scope.** Clause-boundary splitting is a heuristic; "not X or Y"
  must capture both `X` and `Y`. Conservative rule: a negation cue extends to the
  next `,`/`;`/`.`/end. Risk: an over-long negation clause could demote an
  intended candidate — mitigated because demoted candidates *remain selectable*
  in the numbered list.
- **Stopword list drift.** Keep it tiny and generic; a too-large list could strip
  a meaningful path token. Tested via the positive-overlap cases.
- **WEAK vs NONE threshold** for "add hello in readme" is a judgement call; the
  test allows either, but it must never become a STRONG auto-leaning pick.
- **Tokenizer collisions** (e.g. `docs` appearing in both request and an
  unintended path) — acceptable because the result is only a recommendation the
  user confirms.
- **Index staleness** is out of scope here (deferred to optional #17K).

---

## 12. Confirmation: no runtime behavior changed by #17H

This PR adds **only** this document. No module is imported, wired, or modified.
`file_alias_grounding.py`, `chunks.py`, scope_guard, patch_applier, memory, DB,
frontend, approvals, git, and providers are untouched. The current ambiguous
clarification (chunks.py:187) continues to return the existing safe, generic
message until #17J ships. All safety invariants in section 7 hold unchanged.

---

## #17J implemented

`/runs/chunked` now uses `rank_ambiguous_file_candidates()` only when
`resolve_explicit_edit_target()` returns `AMBIGUOUS`. The response remains a
read-only `needs_clarification` envelope: no run row is created, no candidate is
auto-selected, and the user must confirm an exact path before normal planning can
continue.

The clarification now includes numbered candidates in ranked order plus additive
payload fields:

- `candidates`
- `recommended_path`
- `recommendation_strength`

Recommendation copy is rendered from the ranker's structured reason tokens in
the route. No LLM, route broadening, file-alias grounding change, create-target
change, scope/patch behavior, DB/schema, frontend, memory, approvals, git, or
provider behavior was added for #17J.
