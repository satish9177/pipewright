# Memory Injection Discipline — Design / Audit (M3F1)

**Status:** Docs-only design/audit. No code, schema, route, helper, prompt, UI, or test change.
**Mode:** Adversarial / evidence-based. Claims cite a `file:line`, function, or test.
**Author intent:** Re-derive the *current* memory injection path as-built, name the structured-exclusion
gap, and define the safest M3F plan **before** any code slice changes injection or prompt behavior.
**Scope guard:** This document proposes nothing to build in M3F1. Later slices (M3F2+) are described as
proposals with explicit allowed/forbidden changes and acceptance criteria; none are implemented here.

Related docs:

- As-built trust audit + slice history: [`memory-m3-trust-lifecycle.md`](./memory-m3-trust-lifecycle.md)
  (§8 injection surface, §14 M3B helpers, §15 M3C1 provenance, §16 M3C2 analysis, §17–§21 M3D/M3E)
- Provenance smoke: [`../testing/memory-provenance-smoke.md`](../testing/memory-provenance-smoke.md)
- Lifecycle smoke: [`../testing/memory-lifecycle-smoke.md`](../testing/memory-lifecycle-smoke.md)
- Trust UI smoke: [`../testing/memory-trust-ui-smoke.md`](../testing/memory-trust-ui-smoke.md)

---

## 1. Purpose

M3F ("Memory retrieval / injection discipline") makes memory injection **more observable and more
disciplined — not more autonomous.** It tightens what a human can *see* about why a fact was or was not
injected, and (only later, only conservatively, only if data proves it) tightens what is injected. It
never lets memory decide truth, never auto-resolves, and never weakens the human approval gate.

**Core rule (unchanged, load-bearing):**
> Source code / explicit user instruction / tests / safety rules beat memory.
> **System detects; human decides.**

This rule is already encoded in the injected block's footer
(`prompt_builder.py:334-340`: *"follow the source code / user instruction / tests / safety rules and
suggest a memory update"*) and asserted by `test_memory_prompt_builder.py:290-298`. M3F must preserve it
verbatim — it is a behavioral instruction to the LLM, not an enforced invariant, which is exactly why
*visibility* (this phase) matters before any tightening.

M3F1 (this doc) is **docs-only**: no code, no prompt change, no role-policy change, no token-budget
change, no runtime behavior change.

---

## 2. Current as-built injection path

All behavior below is re-derived from `backend/memory/prompt_builder.py` as it stands.

### 2.1 Entry points and the single-computation guarantee

- `build_project_memory_block(project_id, role, project_name, token_budget, scopes)`
  (`prompt_builder.py:219-239`) returns **only the block string**. It now delegates to
  `build_project_memory_block_detailed(...)` and returns `.block`, documented as **byte-identical** to
  its historical output for the same inputs.
- `build_project_memory_block_detailed(...) -> MemoryBlockBuildResult`
  (`prompt_builder.py:242-350`) is the **one pure computation** that produces the block string **and**
  the structured `included_entries` / `excluded_entries` detail. It performs **no writes, no repo/LLM
  access**; it only reads already-approved active memory facts.

Because both the string and the structured detail come from one function, runtime injection and the
structured snapshot can never disagree for the same inputs. M3F must keep this property.

### 2.2 `project_id` blank behavior (fail-closed)

If `project_id` is missing/blank, the builder logs a warning and returns an **empty** result
(`prompt_builder.py:259-268`): empty block, `token_budget=0`, the role's `category_policy`, no entries.
Covered by `test_memory_prompt_builder.py:41-50, 329-331`. No global/cross-project fallback exists.

### 2.3 SQL/status filter (what is even loaded)

`_load_active_memory_rows(project_id, categories)` (`prompt_builder.py:151-165`) issues:

```sql
SELECT id, content, content_hash, category, scope, priority, status, created_at
FROM memory_facts
WHERE project_id = :project_id
  AND is_stale = 0
  AND status = 'active'
```

then keeps only rows whose `category` is in the role's allowed set. **Consequence:** stale, archived,
and historical facts, and facts whose category is not allowed for the role, are **never loaded as rows**
— so they cannot appear in `included_entries` *or* `excluded_entries`. This is the root of the §3 gap.
Status exclusion is covered behaviorally by `test_memory_prompt_builder.py:74-99, 334-350`.

### 2.4 Role category allow-list (`ROLE_CATEGORIES`, `prompt_builder.py:57-122`)

Deterministic role → allowed-category policy. Every role always includes the safety categories
`security` and `forbidden_paths`. Current sets:

| Role | Allowed categories |
|---|---|
| `triage` | security, forbidden_paths, stack, structure, test, db |
| `planner` | security, forbidden_paths, stack, db, test, structure, architecture, style, deploy, reviewer_pref, other |
| `architect` | security, forbidden_paths, stack, db, test, structure, architecture, style, deploy |
| `coder` | security, forbidden_paths, stack, db, test, structure, architecture, style, deploy, reviewer_pref, other |
| `reviewer` | security, forbidden_paths, architecture, test, deploy, style, reviewer_pref, other |
| `summary` | security, forbidden_paths, stack, db, test, deploy |
| `default` | security, forbidden_paths, stack, db, test, structure, architecture, style, deploy, other |

Unknown roles fall back to `default` via `_role_key` (`prompt_builder.py:129-131`); covered by
`test_memory_prompt_builder.py:175-184`.

### 2.5 Role token budgets (`ROLE_TOKEN_BUDGETS`, `prompt_builder.py:34-41`)

`triage=400`, `planner=1200`, `architect=1200`, `coder=1200`, `reviewer=800`, `summary=800`; unknown
roles default to `1500` (`prompt_builder.py:271-274`). An explicit `token_budget` argument overrides.
Token estimate is `(len+3)//4` (`prompt_builder.py:125-126`).

### 2.6 Ordering / ranking (`prompt_builder.py:289-294`)

Rows sort by, in order: `_category_rank` (safety/security first; reviewer re-ranks `reviewer_pref` up
and de-prioritizes non-safety, `prompt_builder.py:134-140`), then `_scope_rank` (global → preferred
scope → other, `:143-148`), then `priority` (lower first), then `created_at`. Determinism and
"safety survives truncation" are asserted by `test_memory_prompt_builder.py:301-327`.

### 2.7 Included vs. budget-dropped excluded entries (`prompt_builder.py:296-311`)

The builder walks the sorted rows, rendering each as `[{category}/{scope}] {content}`. A row that would
exceed `budget` is **appended to `excluded_entries`** and skipped (`:306-308`); a row that fits is
appended to `included_entries` and rendered. `excluded_entries` therefore contains **only in-policy,
active facts dropped because the token budget filled** — documented exactly at `prompt_builder.py:188-196`.

### 2.8 Generated advisory block format (`prompt_builder.py:323-342`)

When at least one line is selected, the block is:

```
=== PROJECT MEMORY (advisory; source code wins on conflict) ===
Project: <name or id>
Generated: <UTC isoformat>
Entries: <N> active shown
Budget used: <used> / <budget> tokens

[<category>/<scope>] <content>
... (one line per included entry)

Memory is advisory context only. If a memory entry conflicts with the current source
code, the user's explicit instruction, the project's tests, or Pipewright's safety
rules, follow the source code / user instruction / tests / safety rules and suggest a
memory update.
=== END PROJECT MEMORY ===
```

The only per-call-varying header field is `Generated` (a timestamp, not memory). If no line is selected
the block is `""`. **This exact string is the byte-identical contract M3F2+ must not break** (the only
allowed difference is the `Generated` timestamp).

### 2.9 Prompt preview vs. runtime injection

- **Runtime** (triage/planner/coder) calls `build_project_memory_block_detailed(...)` then best-effort
  `capture_memory_injection(...)`: `triage.py:237,245`, `planner.py:164,172`, `coder.py:340,350`.
- **Preview** (`GET /…/memory/prompt-preview`, `routes/memory.py:452-462`) calls the `.block`-only
  `build_project_memory_block(...)`. It renders the same block string but **does not currently expose
  the structured `excluded_entries`.**

Both paths share the one computation, so the **block string** is identical for identical inputs. The
asymmetry is only in *structured detail*: runtime persists included+excluded via provenance; preview
surfaces just the text. M3F2 should keep the block string byte-identical and may *additionally* surface
structured exclusions in both paths — without changing the rendered text.

### 2.10 Provenance capture (M3C1) — append-only, best-effort

`capture_memory_injection(result, ...)` (`injection_store.py:263-307`) reads only the
`MemoryBlockBuildResult`'s entries/policy (never a prompt or repo content), and persists an append-only
`memory_injection_events` row via `record_memory_injection_event` (`:170-260`). It **never raises** and
never changes the run outcome. Captured content is re-run through the write-path safety gate
(`validate_memory_content`) as defense-in-depth and redacted if it somehow fails (`:69-83`). Stored
`entries_json` is `{"included":[...], "excluded":[...]}`; `excluded` today carries only the budget-drop
entries from §2.7.

### 2.11 Reviewer / summary policies present but NOT wired

`ROLE_CATEGORIES` / `ROLE_TOKEN_BUDGETS` define `reviewer` and `summary`, and the preview route can
render them, but **no execution path injects memory into reviewer or summary** — confirmed by grep:
the only `build_project_memory_block*` call sites are triage/planner/coder (runtime) and the preview
route. `prompt_builder.py:6-11` and the M3A audit (§8, `memory-m3-trust-lifecycle.md:198-209`) state this
explicitly. Reviewer policy is *aspirational / preview-only* today.

---

## 3. Current exclusion gap

A human can see **what was injected** (M3C1 provenance) and **why budget-dropped facts were left out**
(`excluded_entries`). A human **cannot** currently see, as a structured reason, that a fact was withheld
because it was:

- **stale** (`status='stale'` / `is_stale=1`) — filtered in SQL (§2.3), never surfaced as an entry.
- **archived** (`status='archived'`) — same.
- **historical** (`status='historical'`) — same.
- **category-not-allowed-for-role** — filtered in Python after load (`prompt_builder.py:162-165`), never
  surfaced.
- **unsupported/unknown-role** cases — silently mapped to `default` (`_role_key`, §2.4); the operator is
  not told the role name was unrecognized.

### Why this matters

- A user can audit *inclusions* but not the *full set of reasons for exclusions*. The poisoning surface
  the M3A audit named as the top risk (`memory-m3-trust-lifecycle.md:216-222`) is about *wrong* injected
  facts; the mirror gap is that *withheld* facts are invisible, so an operator cannot easily confirm
  "the stale fact really did stop being injected" except by absence.
- **Safety facts that get budget-dropped are the worst case.** `security` / `forbidden_paths` are always
  in-policy and sort first (§2.6), but on a tight budget (e.g. `triage=400`) they *can* still land in
  `excluded_entries`. A dropped guardrail must be **loud**, not silent.
- Status/category exclusions should become **auditable without changing prompt text** — the block bytes
  stay identical; only the structured detail/provenance grows.

This gap is the natural, lowest-risk first target for M3F2: surface before suppress.

---

## 4. Exclusion reason vocabulary (proposed for M3F2+)

A controlled vocabulary so every fact considered for a role has exactly one *primary* reason it was or
was not injected. Names are proposals for later slices; **M3F1 introduces no code using them.**

**Deterministic, execution-time reasons** (knowable from the builder inputs alone, no heuristics):

| Reason | Meaning |
|---|---|
| `included` | Rendered into the block and injected. |
| `budget_dropped` | In-policy, active, but dropped because the role token budget filled (today's `excluded_entries`). |
| `status_excluded_stale` | Fact is `stale` / `is_stale=1`; not loaded for injection. |
| `status_excluded_archived` | Fact is `archived`. |
| `status_excluded_historical` | Fact is `historical` (e.g. superseded). |
| `category_not_allowed_for_role` | Active fact whose category is outside the role's allow-list. |
| `role_not_wired` | Role has a policy but is not injected at runtime (reviewer/summary). |
| `project_scope_excluded` | (If relevant) blank/missing `project_id`, or unscoped legacy row excluded. |

**Advisory, compute-on-read reasons** (heuristic; never authoritative; never auto-exclude):

| Reason | Meaning |
|---|---|
| `reality_mismatch_candidate` | Fact's single dimension value disagrees with an already-computed repo signal (M3B `check_fact_against_signal`). |
| `duplicate_candidate` | Near/exact duplicate of another fact (M3B `find_duplicate_candidates`). |
| `supersession_candidate` | Contradicts another fact on the same dimension (M3B `find_supersession_candidates`); direction undecided. |
| `unverified_high_risk` | High-risk category (see §6) with no repo verification; advisory flag only. |

The two groups must stay **separate** in any future model: deterministic reasons describe what the
builder *did* at execution time (persistable); advisory reasons are *opinions about facts* that may
change as heuristics evolve (compute-on-read only).

---

## 5. Persist vs. compute split (proposed for M3F2+)

Mirrors the existing M3C1 (persist immutable snapshot) / M3C2 (compute-on-read advisory) architecture.

**Persist at injection time** (immutable execution facts; extend the M3C1 snapshot, never rewrite it):

- `included` entries (already persisted).
- `budget_dropped` entries (already persisted as `excluded`).
- `category_not_allowed_for_role` entries — **only if** M3F2 widens detail collection to load them.
- `status_excluded_*` entries — **only if** M3F2 widens detail collection to load them.
- `token_budget` and `category_policy` used (already persisted).
- `status_at_injection` per entry (already persisted).
- `reason_at_injection` per entry (new: the §4 deterministic reason).

**Compute on read** (advisory, evolving; never stored as truth — mirrors M3C2):

- `reality_mismatch_candidate`
- `near_duplicate_candidate`
- `supersession_candidate`
- `unverified_high_risk`

**Why:** execution-time facts must be **immutable** so "what did the coder actually receive / withhold?"
stays answerable after the fact mutates (the same rationale M3C1 documents,
`memory-m3-trust-lifecycle.md:344-352`). Heuristic/advisory analysis must **not** be persisted as a
verdict, so improving the heuristics never leaves a stale stored judgment behind (M3C2 discipline,
`:378-384`).

> Caveat for M3F2: widening detail collection to *load* status/category-excluded rows means the builder
> reads more rows than today. The rendered block must remain byte-identical (those rows are partitioned
> into excluded-with-reason, never rendered). A golden test must lock this (§10).

---

## 6. Risk taxonomy

Risk is about a fact being **wrong / misleading**, not about its age or priority.

| Risk | Cases | Rationale |
|---|---|---|
| **High** | Repo-checkable reality facts (`stack`, `db`, `structure`, plus the M3B dimensions: `test_runner`, `migration_tool`, `package_manager`, `backend_framework`, `frontend_framework`) **when they conflict with a repo signal**. | Objectively verifiable; a wrong value actively misdirects triage/planner/coder. Best M3F target. |
| **High** | Safety facts (`security`, `forbidden_paths`) **if budget-dropped**. | A silently dropped guardrail is the worst failure — the role loses protection and nobody sees it. Must be surfaced loudly. |
| **Medium** | Subjective `style`, `reviewer_pref`. | Not repo-verifiable; low objective harm but a real poisoning/sycophancy vector for any *future* reviewer wiring. Flag, never auto-exclude. |
| **Medium** | `architecture`, `deploy` drift. | Drifts quietly with the system; no clean repo signal. Candidate for "unverified" surfacing, not exclusion. |
| **Lower** | `test`, `other`. | Either repo-adjacent (`test`) or already low-trust (`other` = rejected-approach / patch-failure lessons), unless clearly conflicting. |

**Hard prohibitions for the taxonomy:**

- **Do not treat age alone as proof of risk.** An old, repeatedly-correct fact is not risky.
- **Do not use latest-wins.** A newer fact is never automatically correct (M3B
  `SupersessionCandidate.recency_implies_truth=False`, `memory_trust.py:174-179`).
- **Do not use recency as truth** anywhere in surfacing copy or ranking changes.

---

## 7. M3B reality-helper wiring guidance

The M3B helpers (`memory_trust.py`) are pure and currently wired into nothing on the runtime path. When
M3F3 surfaces reality checks, it must hold these constraints:

- **Read-only warnings first.** The first wiring surfaces `reality_mismatch_candidate` as advisory copy
  only — never an exclusion.
- **No auto-exclusion** based on helper output, ever in M3F3.
- **No repo scanning inside `prompt_builder`.** The builder stays pure (no git/filesystem/network), as
  `check_fact_against_signal` is explicitly designed to take an **already-computed** `repo_value`
  (`memory_trust.py:376-440`). Mirror `repo_reality.py`'s pattern: the signal is computed elsewhere and
  passed in.
- **Ambiguous / unknown signals produce nothing.** `check_fact_against_signal` returns `unknown` for
  absent/ambiguous fact values or repo signals and **never escalates to mismatch**
  (`memory_trust.py:404-422`). Only an unambiguous `REALITY_MISMATCH` against an unambiguous repo signal
  may produce an advisory warning — exactly the conservatism `repo_reality.py:131-143` already enforces
  for the DB dimension.
- **Fact-vs-repo only, never fact-vs-fact-by-recency.** Reality checks compare a fact to the repo, not
  to a "newer" fact. This is the structural defense against latest-wins creeping in.

---

## 8. Role policy guidance

- **Do not change `ROLE_CATEGORIES` in M3F1 or M3F2.** Changing a role's allowed categories *is* a prompt
  behavior change and is out of scope for a discipline/surfacing phase.
- **Do not change `ROLE_TOKEN_BUDGETS` in M3F1 or M3F2.** Same reason.
- **Reviewer / summary memory injection remains unwired** (§2.11). M3F does not wire it.
- **Wiring reviewer/summary requires its own design slice** because a memory-fed reviewer is a direct
  sycophancy / memory-poisoning amplifier: a poisoned `reviewer_pref` or `style` fact would bias the very
  role meant to catch bad changes. Out of scope for all of M3F unless explicitly decided later with an
  adversarial review of that poisoning surface.

---

## 9. Proposed M3F sequence

Each slice lists goal / allowed / forbidden / acceptance. **Only M3F1 is delivered by this doc.**

### M3F1 — Injection discipline design/audit (this doc)

- **Goal:** Re-derive the as-built injection path, name the structured-exclusion gap, define the safest
  M3F plan and vocabulary, before any behavior change.
- **Allowed:** create this design doc only.
- **Forbidden:** any code/schema/route/prompt/UI/test change; any role-policy or token-budget change.
- **Acceptance:** doc merged; covers §1–§13; `git diff --check` clean; no code/runtime change.

### M3F2 — Complete the exclusion record (read-only, byte-identical prompt)

- **Goal:** Surface deterministic `status_excluded_*` and `category_not_allowed_for_role` exclusions
  (with `reason_at_injection`) in the detailed result, provenance snapshot, and prompt-preview — **without
  changing the rendered block bytes.**
- **Allowed:** widen detail collection in the builder to *load and classify* excluded rows; extend the
  M3C1 snapshot and the read endpoints/preview to expose structured exclusions; add tests.
- **Forbidden:** any change to the rendered block string (beyond the `Generated` timestamp); any change
  to which facts are *injected*; any role-policy/budget change; any mutation route; any repo scan in the
  builder; reviewer/summary wiring.
- **Acceptance:** golden test proves the block string is byte-identical; excluded facts carry distinct
  deterministic reasons; preview/runtime parity holds; provenance records included **and** excluded
  entries; no memory mutation; existing memory tests pass.

### M3F3 — Reality-check warnings (read-only)

- **Goal:** Surface `reality_mismatch_candidate` as advisory, compute-on-read warnings using an
  already-computed repo signal passed in safely.
- **Allowed:** compute-on-read analysis that pairs provenance/active facts with a caller-provided repo
  signal; advisory warnings in read models/preview.
- **Forbidden:** any exclusion from prompts; any repo scan/git call inside `prompt_builder`; any warning
  on ambiguous/unknown signals; any persisted verdict; latest-wins; reviewer/summary wiring.
- **Acceptance:** unknown/ambiguous signal → no warning, no exclusion; only unambiguous fact-vs-repo
  mismatch warns; nothing persisted; no mutation; block bytes unchanged.

### M3F4 — Optional conservative injection tightening (gated)

- **Goal:** *Only if* M3F2/M3F3 data proves a category is reliably wrong, conservatively tighten what is
  injected — behind an explicit opt-in/gate, fully golden-tested.
- **Allowed:** a gated, opt-in exclusion path with provenance reasons and tests; off by default.
- **Forbidden:** any default-on behavior change; any auto-resolution; any latest-wins; any LLM truth
  decision; reviewer/summary wiring.
- **Acceptance:** default behavior byte-identical; gate documented; exclusions explainable and reversible;
  tests cover gate on/off.
- **Note:** This slice may be skipped entirely if the data does not justify it. Surfacing is the goal;
  suppression is a last resort.

### M3F5 — Smoke / docs closeout

- **Goal:** Manual smoke checklist + closeout, mirroring the M3C/M3D/M3E smoke docs.
- **Allowed:** docs/checklist only.
- **Forbidden:** code/behavior change.
- **Acceptance:** checklist merged; known limitations recorded.

---

## 10. Required tests for future code slices (M3F2 / M3F3)

If and when M3F2/M3F3 touch code, they must include:

- **Golden test:** `build_project_memory_block` output is **byte-identical** before/after for a fixed
  fact set (only the `Generated` timestamp may differ). *Single most important test.*
- Detailed result includes both `included_entries` and the widened `excluded_entries`.
- Status (`stale`/`archived`/`historical`), category-not-allowed, and budget drops each produce a
  **distinct** `reason_at_injection`.
- **Prompt-preview / runtime parity:** preview's exclusion list equals what provenance records for the
  same inputs.
- Provenance records included **and** excluded entries with reasons.
- **No mutation:** running injection/analysis writes nothing to `memory_facts` / suggestions and never
  flips `is_stale`/`status` (extend the spirit of `test_memory_repo_reality.py:144-149`).
- Stale/archived/historical facts remain **excluded from the prompt block** (keep
  `test_memory_prompt_builder.py:74-99, 334-350` green).
- Advisory reality-mismatch warnings are **compute-on-read only** (nothing persisted).
- Unknown/ambiguous repo signals produce **no warning and no exclusion**.
- Reviewer/summary remain **unwired** (no provenance recorded for them).
- Existing memory tests pass: `test_memory_prompt_builder.py`, `test_memory_injection_provenance.py`,
  `test_memory_injection_analysis.py`, `test_memory_trust.py`, `test_memory_api.py`, `test_memory.py`.

---

## 11. Hidden failure modes (ranked)

1. **Preview ≠ runtime injection.** If M3F2 enriches detail but preview and runtime drift, the core trust
   artifact breaks. *Mitigation:* keep the single pure computation; never compute exclusions twice.
2. **Changing prompt bytes accidentally.** Widening row loading must not alter the rendered block.
   *Mitigation:* golden byte-identical test before/after.
3. **Silently dropping safety memory without surfacing.** A budget-dropped `security`/`forbidden_paths`
   fact is invisible today. *Mitigation:* surface it loudly with a distinct reason.
4. **Treating advisory helper output as truth** — e.g. auto-excluding on a duplicate/mismatch candidate.
   *Mitigation:* advisory reasons are compute-on-read, never persisted, never auto-acting
   (`advisory_only=True`, `recency_implies_truth=False`).
5. **Over-blocking useful memory**, degrading coder output and eroding trust in memory entirely.
   *Mitigation:* exclusion (M3F4) is optional, gated, off by default.
6. **Repo scans inside the prompt path.** Any git/filesystem/network call in `prompt_builder` breaks its
   purity guarantee. *Mitigation:* repo signal computed elsewhere and passed in.
7. **Latest-wins language creeping in** through "current"/"reality" wording. *Mitigation:* reality checks
   are fact-vs-repo only; supersession direction stays undecided.
8. **Reviewer memory poisoning if wired too early.** *Mitigation:* reviewer/summary stay unwired through
   all of M3F.

---

## 12. Explicit non-goals

- No embeddings / vector / pgvector.
- No semantic retrieval.
- No LLM truth decisions.
- No auto-resolution.
- No auto-stale / auto-archive / auto-supersede.
- No reviewer / summary memory injection.
- No role-policy or token-budget changes in M3F1 / M3F2.
- No prompt output change in M3F1 / M3F2 (block string byte-identical).
- No repo scan / git call inside `prompt_builder`.
- No frontend mutation changes.
- No backend mutation routes (M3F surfacing is read-only; M3F4 tightening is gated, not a mutation route).

---

## 13. Closeout / decision

**Recommendation: proceed.** Start M3F with this docs-only audit. Proceed to **M3F2 only after this doc
is merged.**

**M3F2 must be:** read-only, **byte-identical prompt output**, and limited to enriching structured
exclusion detail / provenance / preview parity. It surfaces *why facts were withheld*; it does **not**
change *which facts are injected*. Any actual injection tightening is deferred to the optional, gated
**M3F4**, and only if M3F2/M3F3 data justify it.

The guiding principle for the whole phase: **surface before suppress.** System detects; human decides;
source code / user instruction / tests / safety rules beat memory.

---

## 14. M3F2a — Free exclusion surfacing (implemented)

M3F2a implements the *free* half of M3F2 (per the review split): the two exclusions that are already
knowable from the rows the builder loads, with **no SQL widening, no prompt-byte change, and no change
to which facts are injected.**

**What it surfaces** (deterministic, execution-time reasons):

- `category_not_allowed_for_role` — active, non-stale facts whose category is outside the role's
  policy. The active-only query already loads these (the category filter was a post-fetch Python step),
  so they are classified, not re-queried.
- `budget_dropped` — unchanged from before, now carrying the explicit reason.

**Builder (`prompt_builder.py`):**

- `_load_active_memory_rows` now returns `(in_policy_rows, out_of_policy_rows)` from the **same,
  unchanged** `status='active' AND is_stale=0` query. No inactive facts are loaded.
- `build_project_memory_block_detailed` renders the block from `in_policy_rows` exactly as before
  (same sort, same budget math, same lines). It additionally emits `excluded_entries` = budget-dropped
  (in render order) + category-excluded (deterministically sorted). Out-of-policy facts can never reach
  the render path, so the block string stays **byte-identical**.
- `InjectedMemoryEntry` gained `exclusion_reason: str | None = None` (None for included entries). New
  constants `EXCLUSION_BUDGET_DROPPED`, `EXCLUSION_CATEGORY_NOT_ALLOWED`.

**Provenance (`injection_store.py`):** `exclusion_reason` added to `_ENTRY_KEYS` so it persists in the
append-only snapshot. `compute_entries_hash` is unchanged (it digests included-entry identity only), so
`entries_hash` is unaffected by excluded entries. Capture remains best-effort and never mutates memory.

**Endpoints / API (`routes/memory.py`):**

- `GET …/memory-injections` per-entry shape gained `exclusion_reason`; `content_hash` still stripped.
- `GET …/memory/prompt-preview` now uses the **same** `build_project_memory_block_detailed` computation
  and returns a read-only `excluded_entries` list (computed live, nothing persisted). `memory_block` is
  byte-identical to before.

**Frontend (`RunMemoryProvenancePanel.tsx`, `client.ts`):** the excluded section is collapsed, shows a
reason summary count, labels `category_not_allowed_for_role` ("this role does not use this memory
category") and `budget_dropped` ("role memory budget was full"), and highlights
**"Safety memory was budget-dropped."** when a `security`/`forbidden_paths` fact is budget-dropped. No
mutation actions.

**Explicitly deferred to M3F2b:** `status_excluded` (stale/archived/historical) — those require widening
the active-only query and returning an unbounded set, so they are **not loaded or surfaced** here. M3F2b
should surface them as a **bounded count summary**, not full per-entry rows. All advisory reasons
(reality/duplicate/supersession/unverified) remain M3F3, compute-on-read.

**Invariants held:** prompt block byte-identical; included entries unchanged; injection unchanged;
`ROLE_CATEGORIES`/`ROLE_TOKEN_BUDGETS` unchanged; SQL status filter unchanged; reviewer/summary unwired;
no mutation/auto-resolution; no LLM/embeddings/vector; no repo/git scan in `prompt_builder`.

**Tests:** `backend/tests/test_memory_free_exclusions.py` (byte-identical block with out-of-policy facts
present; included entries carry no reason; category-excluded surfaced incl. the no-in-policy case;
budget vs. category reasons distinct; status-excluded facts neither loaded nor surfaced; provenance
persists the category reason; `entries_hash` ignores excluded entries; no memory mutation;
reviewer pipeline does not import capture/builder).
