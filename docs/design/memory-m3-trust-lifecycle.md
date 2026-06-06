# Memory M3 — Trust & Lifecycle As-Built Audit (M3A)

**Status:** Docs-only audit. No code, schema, route, helper, prompt, or UI change.
**Mode:** Adversarial / evidence-based. Every claim cites a `file:line`, function, or test and is
labeled **Exists / Partial / Missing / Stale-doc**.
**Author intent:** Establish ground truth about the *current* memory trust lifecycle before any M3B+
implementation, so later slices extend what exists instead of rebuilding it.
**Scope guard:** This document proposes nothing to build. Gaps are recorded, not designed.

> **Why this audit exists.** The M3 plan was written as if the trust lifecycle barely exists. It
> does — a large fraction is already shipped (status model, archive/verify/stale, repo-reality
> conflict detection, a hard write-path content gate, three-layer suggestion dedupe, role-scoped
> injection). The single most valuable output of M3A is correcting that misconception so M3B–M3G do
> not duplicate or fork existing behavior.

---

## 0. Verdict up front

| Trust property | As-built status |
|---|---|
| AI memory can be saved without human approval | **No** — provably gated (§5) |
| Suggestions can contain secrets / code / stack traces | **Blocked on write path** (regex gate, §4) |
| Memory is project-scoped | **Yes** — app-enforced on every read/write (§2) |
| Stale / archive / conflict lifecycle exists | **Partial** — exists for DB facts + manual actions; not general (§6, §7) |
| Bad (wrong) memory can silently poison future runs | **Yes, for non-DB facts** — top risk (§8) |
| Duplicate/conflicting suggestions handled beyond exact hash | **No** — exact normalized hash only (§6.4) |
| Docs/PDF stale vs. repo | **Yes, in several places** (§10) |

---

## 1. Where memory lives (storage)

| Item | Evidence | Status |
|---|---|---|
| `memory_facts` table (durable project facts) | `backend/db/schema.sql:7-25` | Exists |
| `memory_suggestions` table (pending proposals) | `backend/db/schema.sql:27-52` | Exists |
| `project_id` nullable on facts, app-enforced non-blank | `schema.sql:1-6`; `memory_store._validate_project_id` (`memory_store.py:291-294`) | Exists |
| Active-dedupe unique index `(project_id, content_hash) WHERE status='active'` | `schema.sql:293`-area; `db/database.py:471-475` | Exists |
| Pending-dedupe unique index `(project_id, content_hash) WHERE status='pending'` | `schema.sql:293-295`; `db/database.py:486-491` | Exists |
| Project/status secondary indexes | `db/database.py:477-483, 493-499` | Exists |
| Columns added via additive `ALTER TABLE` migrations (no Alembic) | `db/database.py:126-382` | Exists |

**Note:** SQLite remains the local/open-source default; no pgvector, no Postgres in the memory path.
Consistent with stated invariants.

---

## 2. Project scoping (cross-project leak defense)

| Claim | Evidence | Status |
|---|---|---|
| Every read/write validates a non-blank `project_id` | `memory_store._validate_project_id` (`memory_store.py:291-294`), called by `add_fact`, `list_facts`, `load_hard_facts`, `archive_fact`, `verify_fact`, `mark_fact_stale`, `update_fact`, `flag_stale_memories` | Exists |
| `load_hard_facts(None/"")` returns `""` (no global fallback) | `memory_store.py:521-530` | Exists |
| `build_project_memory_block(None/"")` returns `""` | `prompt_builder.py:168-172` | Exists |
| Unscoped pre-M1 rows archived on access, never injected | `_archive_unscoped_pre_m1_memory` (`memory_store.py:322-333`), called in `add_fact`, `load_hard_facts`, `list_all_facts` | Exists |
| Regression coverage | `backend/tests/test_memory.py`, `backend/tests/test_memory_prompt_builder.py`, `backend/tests/test_stabilization_smoke.py:609-622` | Exists |

**Assessment:** Project scoping is enforced at the application layer on every path inspected. The
nullable column is a documented, test-covered legacy compromise, not a leak.

---

## 3. Suggestion creation (where AI-derived memory originates)

All generators produce **pending suggestions only** — never an active fact, never an approval.

| Source | Evidence | Trigger | Status |
|---|---|---|---|
| Bootstrap (deterministic repo manifest scan) | `bootstrap.generate_bootstrap_suggestions` (`bootstrap.py:800-834`), candidates in `_collect_candidates` (`bootstrap.py:182-592`) | API only: `POST /…/memory/bootstrap-suggestions` (`routes/memory.py:309-327`) | Exists |
| Run-outcome (structured run artifacts: status, completion summaries, patch failures, rejected gates) | `run_outcome_suggestions.generate_run_memory_suggestions` (`run_outcome_suggestions.py:294-356`) | API only: `POST /api/v1/runs/{run_id}/memory-suggestions/generate` (`routes/memory.py:510-530`) | Exists |
| Coder/planner handoff `suggested_memory_entries` → pending suggestion | `_handoff_candidates` (`run_outcome_suggestions.py:217-244`) | Via run-outcome generator (API) | Exists |

**Critical finding (Stale-doc):** `memory-architecture.md:825-828` and `:864` describe a "post-PR hook"
as "the only write site for AI suggestions," wired into `chunked_orchestrator.py`. **No such hook
exists.** `grep` for `generate_run_memory_suggestions` / `generate_bootstrap_suggestions` across
`backend/pipeline/**` returns **no matches**; both are reachable only from `routes/memory.py`.
Suggestion generation today is **human/API-triggered, not automatic**. This is *safer* than the doc
describes, but the doc is wrong and must be reconciled.

- Determinism: run-outcome generation reads only structured DB fields; "No LLM, no embeddings, no
  repo/log/stack-trace reading" (`run_outcome_suggestions.py:9-18`). **Exists.**

---

## 4. Write-path content safety gate

A single validator guards every promotion of text into memory.

| Guarded path | Calls the gate via | Status |
|---|---|---|
| Manual fact create (`add_fact`) | `validate_fact_fields` → `validate_memory_content` (`memory_store.py:336-353, 446`) | Exists |
| Suggestion approval → fact | `approve_suggestion` → `validate_fact_fields` (`bootstrap.py:887-892`) | Exists |
| Bootstrap suggestion insert | `_insert_suggestion` → `validate_memory_content` (`bootstrap.py:685`) | Exists |
| Run-outcome / generic pending insert | `insert_pending_suggestion` → `validate_memory_content` (`bootstrap.py:745`) | Exists |

**What `validate_memory_content` (`memory_store.py:244-288`) blocks:**
- Length floor/ceiling 4–400 chars.
- Prompt-injection markers (`SYSTEM:`, `<|`, ``` ```system ```, `=== PROJECT MEMORY`, "ignore previous instructions") — `memory_store.py:84-92`.
- **Control-plane bypass phrases** (skip/bypass approval, auto-merge, force push, commit to main, edit `.env`, store token…) — `memory_store.py:98-130`. *Memory must never become a control channel; even a human-approved fact cannot instruct the pipeline to bypass safety.*
- Absolute local paths (`memory_store.py:137-140`).
- Raw stack traces (`memory_store.py:144-151`).
- Large raw code blocks (>8 code-like lines) (`memory_store.py:153-186`).
- Secrets: OpenAI/Gemini/GitHub/Slack/AWS keys, PEM, JWT (`SECRET_PATTERNS`, `memory_store.py:66-76`).
- Unexplained long hex (git-hash context allowed) (`memory_store.py:77-82, 234-241`).
- Phone numbers, payment cards (Luhn) (`memory_store.py:202-231`).

**Known blind spots (must be documented, not assumed away):** the gate is **regex/heuristic**. It will
miss novel secret formats and will pass plausible-but-wrong prose. It is a necessary backstop, not a
guarantee of correctness. **Status: Exists (with documented limits).**

---

## 5. Approval / rejection lifecycle (the human gate)

| Claim | Evidence | Status |
|---|---|---|
| Approval is one atomic transaction (validate → insert fact → mark suggestion approved) | `approve_suggestion` (`bootstrap.py:857-939`, single `engine.begin()`) | Exists |
| Rejection requires a reason ≥4 chars | `reject_suggestion` (`bootstrap.py:942-955`) | Exists |
| Only `pending` suggestions can be approved/rejected | `bootstrap.py:883-884, 954-955` | Exists |
| Edit-on-approve validates edited text through the same gate; original content preserved | `bootstrap.py:875, 886-892, 912-913` | Exists |
| **No auto-approval / auto-save anywhere** | `grep add_fact|approve_suggestion` in `backend/pipeline/**` → **no matches**; both reachable only from `routes/memory.py` | Exists |
| Manual fact create still requires the safety gate | `routes/memory.py:288-306` → `add_fact` | Exists |

**Assessment:** The "AI suggests, human approves" contract holds at the code level. There is no path by
which an LLM or the pipeline writes or promotes a durable memory fact. This is the strongest part of the
system and **M3 must not weaken it.**

---

## 6. Conflict & duplicate handling

### 6.1 Dedupe layers (all exact, normalized-hash)

`compute_content_hash` = `sha256(lower(strip(collapse_whitespace(content))))` (`memory_store.py:193-199`).

| Layer | Evidence | Status |
|---|---|---|
| Active-fact dedupe (per project) | `_active_memory_exists` (`bootstrap.py:595-607`); `insert_fact_in_conn` re-check (`memory_store.py:380-391`); unique index | Exists |
| Pending-suggestion dedupe (per project) | `_pending_suggestion_exists` (`bootstrap.py:610-622`); unique index | Exists |
| **Rejected-suggestion dedupe (per run)** — prevents a rejected run suggestion from silently regenerating | `_run_scoped_suggestion_exists` (`bootstrap.py:625-652`): `content_hash + source_run_id` across `pending|approved|rejected` | Exists |
| In-batch dedupe during bootstrap | `seen_hashes` (`bootstrap.py:813-823`) | Exists |

**Finding:** Dedupe is **exact normalized hash only**. Near-duplicate (`"use pytest -m unit"` vs
`"run tests with pytest -m unit"`) and **contradictory** facts (`"tabs"` vs `"spaces"`;
`"tests in /tests"` vs `"tests in backend/tests"`) are **not detected**. **Status: Missing** for
near-dup / contradiction. This is the genuine Mem0-shaped gap.

### 6.2 Reality-based conflict detection (DB only)

| Item | Evidence | Status |
|---|---|---|
| Pure, read-only DB conflict evaluator | `evaluate_db_memory_conflicts` (`repo_reality.py:108-194`) — never mutates | Exists |
| Manual reconciliation action (only mutator) | `verify_project_db_memory_against_repo` (`repo_reality.py:197-271`) via `POST /…/memory/verify-repo` (`routes/memory.py:330-347`) | Exists |
| Run-scope classifier (is this run DB-sensitive?) | `conflict_scope.py` | **Exists but unwired** — "Nothing in runtime code imports it yet" (`conflict_scope.py:8-9`) → **Partial** |
| General (non-DB) conflict detection | — | **Missing** |

**Mechanism is deliberately narrow:** only `category='db'` facts, and only by matching DB-engine tokens
(`postgres/mysql/mongo/sqlite`, `repo_reality.py:43-48`). Ambiguous/multi-engine/unknown signal never
marks anything stale (`repo_reality.py:131-143, 166-170`). Core rule encoded: **current repo state >
project memory**, and memory is never auto-edited/deleted (`repo_reality.py:15-23`).

### 6.3 No supersession / version lineage

`archive_fact` / `mark_fact_stale` set a status but record **no "fact B replaces fact A" link**, and
there is **no version-history table**. The `historical` status is allowed (`memory_store.py:50`) but has
**no producer**. **Status: Missing** (the Supermemory-shaped gap).

---

## 7. Status model & lifecycle actions

| Fact status (`ALLOWED_STATUSES`, `memory_store.py:50`) | Producer | Injected? |
|---|---|---|
| `active` | `add_fact`, `approve_suggestion` | Yes |
| `stale` | `mark_fact_stale` (`memory_store.py:642-692`), `flag_stale_memories` (`:774-802`), repo-reality | No (filtered out) |
| `archived` | `archive_fact` (`memory_store.py:570-605`), pre-M1 auto-archive | No |
| `historical` | **none** | No |

| Lifecycle action | Route | Status |
|---|---|---|
| Archive (reason required) | `POST /…/{id}/archive` (`routes/memory.py:454-472`) | Exists |
| Verify (bumps `last_verified_at`) | `POST /…/{id}/verify` (`routes/memory.py:475-487`) | Exists |
| Edit fact (re-validates, re-dedupes) | `PATCH /…/{id}` (`routes/memory.py:437-451`) | Exists |
| Mark single fact stale | `mark_fact_stale` (`memory_store.py:642-692`) | **No dedicated route** — Partial |
| Time-based staleness sweep | `flag_stale_memories` (`memory_store.py:774-802`) | **No runtime/scheduled caller** — Partial |

**Finding (Partial → effectively dead):** `grep flag_stale_memories` shows **only test callers**
(`test_memory.py`, `test_foundation.py`). Nothing in runtime invokes it. Facts therefore **never age
out** in practice. Additionally, it ages by `created_at` only — which `memory-architecture.md:17`
(Finding #3) itself flags as wrong (a stable fact gets staled at day 91 for no real reason). If we keep
it, document it as unwired and decide deliberately; do not silently rely on it.

---

## 8. Injection / retrieval discipline (the live poisoning surface)

| Role | Receives injected memory? | Evidence |
|---|---|---|
| Triage | **Yes** | `triage.py:241-245` |
| Planner | **Yes** | `planner.py:163-167` |
| Coder | **Yes** | `coder.py:339-343` |
| Reviewer | **No** | `reviewer.py:15-23` ("writes no memory"); no import of `build_project_memory_block` (grep: no match) |
| Summary | **No** | no call site found |

**Finding (Stale-doc / Partial):** `prompt_builder.py:28-35, 51-116` defines per-role **token budgets
and category policies for `reviewer` and `summary`**, but those roles are **not wired into execution** —
they are only reachable through the `/prompt-preview` route (`routes/memory.py:367-388`). So the reviewer
policy is *aspirational*. `memory-architecture.md:820` claims the reviewer reads memory — **stale.**

**Injection mechanics (Exists):**
- Filter: `is_stale = 0 AND status = 'active'` only (`prompt_builder._load_active_memory_rows`, `prompt_builder.py:145-158`; mirrored in `memory_store.load_hard_facts`, `memory_store.py:536-543`).
- Per-role category allow-list + token budget, deterministic ordering (`prompt_builder.py:51-116, 176-208`).
- Self-describing block with footer **"source code wins on conflict"** (`prompt_builder.py:215-232`).

**TOP RISK — silent poisoning by non-DB facts.** Any `active && is_stale=0` fact in a role's category
set is injected verbatim. Reality-checking exists **only for `category='db'`** (§6.2). A wrong
`structure` / `stack` / `architecture` / `style` fact (e.g. "auth lives in `backend/auth/`" after it
moved) is injected **indefinitely** with no detection. The only mitigation is the prompt footer asking
the model to prefer source — a *behavioral suggestion to the LLM*, **not an enforced invariant**. This is
the central trust gap and the reason M3 reorders read-model/injection-provenance visibility ahead of
human mutation routes.

---

## 9. Provenance & audit fields (what we can show a human)

| On `memory_facts` | `source`, `added_by`, `approved_by`, `approved_at`, `last_verified_at`, `archived_reason`, `content_hash`, `created_at`, `updated_at` (`schema.sql:7-25`) — **Exists** |
|---|---|
| On `memory_suggestions` | `source`, `source_type`, `source_run_id`, `source_chunk_number`, `source_ref`, `rationale`, `suggested_by`, `risk_level`, `evidence_path`, `evidence_excerpt`, `edited_content`, `approved_fact_id` (`schema.sql:27-52`; `_SUGGESTION_COLUMNS` `bootstrap.py:655-663`) — **Exists** |
| Injection provenance ("which facts entered which role's prompt for run X") | **Missing** — no per-run injected-memory snapshot persisted (the `memory-architecture.md:867` `injected_memory_snapshot` column was never built) |
| `content_hash` stripped from all API responses | `_sanitize_fact` (`routes/memory.py:222-227`), `_sanitize_suggestion` (`bootstrap.py:82-87`) — **Exists** |

The missing injection-provenance snapshot is what makes §8 poisoning hard to debug today, and is the
natural first M3C deliverable.

---

## 10. Stale docs / naming collisions (reconciliation backlog)

| Stale artifact | Reality | Severity |
|---|---|---|
| `prompt_builder.py:5-7` docstring: "does not wire memory into planner/coder/triage" | It **is** wired into all three (§8) | Medium — misleads maintainers |
| `memory-architecture.md` defines **"M3" = Semantic Memory / pgvector** (`:46-51, :439-509`) | The **new M3 = trust/lifecycle**. Direct **naming collision** | **High** — two meanings of "M3" in-repo |
| `memory-architecture.md:825-828, 864` "post-PR hook is the only write site" | No orchestrator hook exists; generation is API-only (§3) | Medium |
| `memory-architecture.md:820` reviewer reads memory | Reviewer does not (§8) | Medium |
| `memory-architecture.md:851` "stack-fingerprint conflict detection (M2) — do not build" | A `db`-first version **is built** (M1.5: `repo_reality.py`, referenced at `:413-418`) | Low — internally cross-referenced but confusing |

**Recommendation:** M3A records these; the docstring/`memory-architecture.md` edits are deferred to M3B
to keep M3A a pure read. The "M3" naming collision should be resolved explicitly (e.g. rename the
semantic phase to "M4 — Semantic Memory" in a later docs slice).

---

## 11. Gap register (severity-ranked, owner slice)

| # | Gap | Severity | Suggested owner slice |
|---|---|---|---|
| G1 | Non-DB active facts injected with no reality check → silent poisoning | **High** | M3F (discipline) + M3C (visibility first) |
| G2 | No injection-provenance snapshot per run (can't audit what was injected) | **High** | M3C |
| G3 | No near-duplicate / contradiction detection (exact hash only) | Medium-High | M3B (deterministic flagger) |
| G4 | No supersession / version lineage; `historical` status has no producer | Medium | M3B (model) + M3D (routes) |
| G5 | `flag_stale_memories` unwired + ages by `created_at` only | Medium | M3D (human-controlled) — decide keep/replace |
| G6 | Reviewer/summary role policies defined but not injected | Low-Medium | M3F — decide intentional or wire it |
| G7 | `conflict_scope.py` classifier built but unwired | Low | M3D/M3F |
| G8 | Stale internal docs (§10), incl. "M3" naming collision | Medium | M3B docs reconciliation |

---

## 12. Explicitly out of scope for M3A

- Any schema change, migration, or new table (incl. a `memory_conflicts` or history table).
- Any new/changed route, helper, pipeline behavior, or prompt text.
- Embeddings, vectors, semantic retrieval, pgvector.
- Defining the *general* conflict-resolution algorithm (this audit only states the need).
- Tuning safety-gate regexes.
- Frontend work.
- Wiring or removing `flag_stale_memories`.
- Editing the stale `prompt_builder` docstring or `memory-architecture.md` (deferred to M3B).

M3A produces exactly one artifact: this document.

---

## 13. Recommended M3 slice order after M3A (reaffirmed)

1. **M3B** — pure helpers (deterministic near-duplicate *flagger*; generalized reality-comparison pattern), imported by nothing yet. Plus docs reconciliation from §10.
2. **M3C** — read-model surfacing, including **injection-provenance** (G2) so poisoning is *visible* before any mutation route exists.
3. **M3D** — human-controlled archive/stale/conflict-resolution/supersession routes (no auto-resolution).
4. **M3E** — frontend memory trust UI over D.
5. **M3F** — injection discipline: tighten filters, guarantee stale/conflicted facts are excluded *and shown as excluded*; resolve reviewer/summary policy (G6).
6. **M3G** — smoke docs / manual checklist (mirror `docs/testing/memory-m2-smoke-checklist.md`).

**Borrow safely:** Mem0 → near-duplicate *candidate* flag (human-confirmed, never auto-merge);
Letta → explicit memory-tier vocabulary; Supermemory → human-decided supersession lineage;
Pipewright → repo-grounded truth + human-gated promotion (already the backbone).

**Reject:** auto ADD/UPDATE/DELETE, agent self-editing memory, latest-wins, LLM-decides-truth, automatic
conflict resolution, and semantic/vector memory before the trust lifecycle is complete.

---

## 14. M3B — Trust helper foundations (implemented)

M3B adds **pure, deterministic helper foundations** and reconciles the stale docs from §10. It adds
**no runtime wiring, no schema, no routes, no UI, and no mutation behavior** — the helpers are not
imported by any planner/coder/triage/reviewer/orchestrator path.

**New module:** `backend/memory/memory_trust.py` (stdlib-only; no DB/SQLAlchemy/FastAPI/LLM/git/filesystem import).

| Helper | Purpose | Decision authority |
|---|---|---|
| `find_duplicate_candidates(items, threshold=0.6)` / `duplicate_similarity(a, b)` | Flag near-duplicate facts/suggestions by deterministic token overlap (stopword-filtered, lightly stemmed). Returns `DuplicateCandidate` with `similarity`, `relation` (`exact`/`near`), `shared_tokens`, `reason`. | Advisory only. Does **not** replace exact-hash write dedupe; never merges. |
| `check_fact_against_signal(dimension, content, repo_value)` | Pure comparison of one fact against an **already-computed** repo signal value (no scanning). Returns `RealityCheckResult` with status `match`/`mismatch`/`unknown`/`unsupported_category`. Dimensions: `db_engine`, `backend_framework`, `frontend_framework`, `test_runner`, `migration_tool`, `package_manager`. | Advisory only. Never marks stale/archives/writes. Conservative: ambiguous/absent → `unknown`. |
| `find_supersession_candidates(items)` | Flag pairs that assert different single values for the same dimension (contradiction). Returns `SupersessionCandidate` with `recency_implies_truth=False`. | Advisory only. Direction undecided; human decides in M3D. No latest-wins. |
| `find_trust_candidates(items)` | Convenience aggregate of the two finders. | Advisory only. |

**Addresses gaps:** G3 (near-duplicate/contradiction flagging), and the *comparison* foundation for G1
(non-DB reality check) — both as pure logic, deferred wiring. G4 supersession is represented as a
candidate model only (no columns, no lineage persistence).

**Docs reconciled in M3B:** the `prompt_builder.py` docstring (now states it is wired into
triage/planner/coder and that reviewer/summary are not), and `memory-architecture.md` (the "M3 =
semantic/pgvector" naming collision → relabeled M4/future; the stale "post-PR hook is the only write
site" and "reviewer reads memory" claims now carry inline as-built corrections).

**Tests:** `backend/tests/test_memory_trust.py` (exact/near duplicates, non-duplicates,
contradiction-as-candidate, reality match/mismatch/unknown/unsupported, supersession candidates,
input-immutability, and a guard asserting the module imports no DB/runtime modules).

**Still not done (future slices):** persistence, routes, UI, injection-provenance snapshot (G2), and any
wiring of these helpers into runtime — all deferred to M3C+.

---

## 15. M3C1 — Persisted injection provenance (implemented)

M3C1 closes **G2** (no injection-provenance snapshot). It makes memory influence auditable: for a given
run/chunk/role, a human can later see the exact approved memory facts that were injected into that
role's prompt at execution time — even after those facts are edited, archived, marked stale, or
superseded. **Visibility-only:** no mutation/resolution routes, no frontend, no change to which memory
is injected.

**Core decision:** persist immutable snapshots (not a computed-from-current view), because memory facts
change after a run and a computed view cannot answer "what did the planner/coder actually receive?".

**New table:** `memory_injection_events` (append-only; created via `schema.sql`, applied to existing DBs
on `init_db` since the schema script runs idempotently). Keyed by `(run_id, chunk_number, role,
attempt_number)`; stores `project_id`, nullable `attempt_id`/`repo_head_sha`, `token_budget`,
`category_policy`, `entries_json` (`{"included":[...],"excluded":[...]}`), counts, and a deterministic
`entries_hash` over the **ordered included entries only** (never the timestamped block header). Pre-M3C
runs simply have no rows.

**New pure return path:** `build_project_memory_block_detailed(...) -> MemoryBlockBuildResult` produces
the block string AND structured detail from one computation; `build_project_memory_block(...)` now
delegates and returns `.block`, **byte-identical** to before. The builder still performs no writes.

**New store:** `backend/memory/injection_store.py` — `record_memory_injection_event` (strict,
append-only), `capture_memory_injection` (best-effort; never raises), `list_memory_injection_events`
(read-only, project-scoped). Captured content is **memory entries only** (never prompts/repo files) and
is re-validated through the write-path safety gate as defense-in-depth (redacted if it somehow fails).

**Wiring:** best-effort capture at the real injection sites — triage (run-level), planner, coder. A
coder re-run (patch/scope-expansion retry) records a new event at a higher `attempt_number`; the planner
is not re-run on patch retry, so no phantom planner event is created. Reviewer/summary are **not** wired
(they do not receive injected memory). Capture failure can never change the prompt or run outcome.

**Endpoint:** `GET /api/v1/runs/{run_id}/memory-injections` (optional `chunk_number`, `role`),
read-only, project-scoped, dedicated (not on the default run/chunk read model). Per-entry `content_hash`
is stripped for parity with the rest of the memory API; the event-level `entries_hash` is retained.

**Deferred to later M3 slices:** candidate analysis on read (duplicate/supersession/reality surfacing
via the M3B helpers), any mutation/resolution routes (M3D), frontend display, retention/pruning, and
wiring `attempt_id`/`repo_head_sha` to the patch-failure attempt machinery.

---

## 16. M3C2 — Read-only injection analysis / surfacing (implemented)

M3C2 turns the immutable M3C1 snapshots into *advisory* analysis without changing memory, prompts,
runtime behavior, or pipeline decisions. It is **read-only and compute-on-read**: nothing is persisted,
so the evolving M3B heuristics never leave stale stored state behind.

**Core decision:** compute analysis on read from the immutable provenance, using only the pure M3B
helpers. No analysis table, no stored verdicts, no automatic decisions — every output is labelled a
*candidate/advisory*, never a fact.

**New pure helper:** `backend/memory/injection_analysis.py` —
`analyze_injection_events(events, *, threshold=0.6) -> InjectionAnalysis`. It is stdlib + `memory_trust`
only (no DB/LLM/embeddings/repo/git/network — guard-tested), operating on the event dicts already
fetched by `list_memory_injection_events`. It flattens each event's `included_entries` into **distinct
facts** (keyed by `fact_id`, then `content_hash`, then normalized content) so the *same* approved fact
injected into multiple roles collapses to one entry and is never flagged as a duplicate of itself; two
*different* facts that read alike remain separate and can be flagged. It then runs
`find_duplicate_candidates` and `find_supersession_candidates` and maps each candidate back to a primary
occurrence (`event_id`, `role`, `chunk_number`, `fact_id`, content) for human traceability.

**Return model (advisory only):** `InjectionAnalysis` with summary counts (`total_events`,
`total_included_entries`, `distinct_fact_count`, `duplicate_candidate_count`,
`supersession_candidate_count`), `DuplicateFinding`s (relation `exact`/`near`, `similarity`, reason,
left/right refs, `advisory_only=True`), and `SupersessionFinding`s (`dimension`, left/right values,
`relation="possible_supersession"`, `recency_implies_truth=False`, `advisory_only=True`). Supersession
direction stays undecided — a newer fact is never automatically correct.

**Endpoint:** `GET /api/v1/runs/{run_id}/memory-injections/analysis` (optional `chunk_number`, `role`) —
a **dedicated sibling** of the M3C1 list endpoint, chosen over a query flag so the default provenance
payload stays byte-identical and the Run Detail read model is never bloated. Read-only, project-scoped,
returns empty analysis for runs with no provenance, 404 for unknown runs. Per-entry `content_hash` is
not exposed (parity with the rest of the memory API).

**Reality-check analysis deferred:** M3C2 must not scan the repo, so it computes no reality checks. The
M3B `check_fact_against_signal` comparison can be surfaced only by a later slice that passes an
already-computed repo/project signal in safely.

**Tests:** `backend/tests/test_memory_injection_analysis.py` (empty input, duplicate/supersession
candidates, distinct-fact collapsing across roles, traceability refs, advisory-only labels, input
immutability, import-purity guard; endpoint returns/scopes/404/empty, content_hash parity, and the
default list response unchanged).

**Out of scope (unchanged invariants):** no mutation/resolution routes, no frontend, no stale/archive/
supersede/resolve, no auto-resolve, no LLM/embeddings/vector/pgvector, no repo scan or git calls, no
change to memory injection filtering or prompt text/budgets/categories/order, and no default run/chunk
payload changes.

---

## 17. M3D1 — Human-controlled mark-stale route (implemented)

M3D1 is the smallest M3D mutation slice: one explicit, human-controlled route that flags a single
**active** memory fact stale. "System detects, human decides" — the route is invoked only by a human and
gates on nothing but structural validation; it never auto-resolves and is not coupled to the M3B/M3C2
advisory detectors.

**New route:** `POST /api/v1/projects/{project_id}/memory/facts/{fact_id}/stale` with `{ reason }`. It
revalidates before mutating: project ownership (404 if the fact is missing or in another project),
**active-only precondition** (409 for `stale`/`archived`/`historical`/non-active — no mutation), and a
required reason. It then calls the **existing** `mark_fact_stale` helper (wrapped, not refactored), which
sets `status='stale', is_stale=1, archived_reason=COALESCE(reason,…)`. The response is the sanitized fact
(per-entry `content_hash` stripped, as elsewhere in the memory API). Because the prompt builder selects
only `status='active' AND is_stale=0`, a stale fact is immediately excluded from future injection — no
prompt-format or filter change was needed.

**New shared helper:** `memory_store.validate_lifecycle_reason(reason)` — narrow and additive. It enforces
the same `>=4` char floor `archive_fact` already requires and blocks control-plane bypass phrases (a
lifecycle reason must never become a control channel). It does **not** run the full memory content gate
(a reason is metadata, not injected memory). `archive_fact`/`verify_fact`/`update_fact`/`approve_suggestion`/
`add_fact` are unchanged.

**Provenance immutable:** marking a fact stale never touches `memory_injection_events`; a snapshot keeps
its `status_at_injection` and `entries_hash` (regression-tested).

**Deferred to later M3D slices:** supersession route, approve-and-supersede, `superseded_by_fact_id`
column / lineage table, a `historical` producer, frontend, scheduled stale-sweep wiring, candidate
acknowledgement, and any detector-gated mutation. No schema, no auto-resolution, no prompt/pipeline
behavior change in this slice.
