# Addendum brief — Chunk isolation advisory (security / DB-migration separation)

**For:** Claude Fable 5, against `PIPEWRIGHT_REDESIGN_PROPOSAL.md` (Passes 1–3) as the accepted baseline.
**Mode:** Design only. Verify against the code, then integrate this as **§18.8** (a sibling of the §18.7 sizing advisory), and update the §23 sequencing table and §24 decision roster accordingly. No code.
**Why it exists:** the proposal closed the chunk *sizing* gap (§18.7) but not the chunk *isolation* gap. This addendum closes it in the same shape.

---

## 1. The gap (verify, then state precisely)

The triage system prompt (`triage.py` `TRIAGE_SYSTEM_PROMPT`) instructs the LLM that **rule 3 — DB migrations must always be isolated in their own chunk** and **rule 4 — security/auth/permissions/encryption must always be isolated and `requires_human_review=true`**. Both are **prompt-only**: nothing deterministic verifies them after triage.

What *is* enforced deterministically today (keep, don't duplicate): dependency structure (`models/chunk.py:32-85` forward-only, positive, references-exist) and **high-risk ⇒ `requires_human_review`** (`models/chunk.py:44-47`). What is **not** enforced: that a security-sensitive change, or a migration, actually lands in its *own* chunk rather than bundled with unrelated CRUD. A triage LLM that bundles an auth change into a CRUD chunk is caught only if a human notices at the plan gate.

So the gap's real shape is: **rules 3 and 4 are aspirational prompt text with no deterministic backstop** — the symmetric problem to §18.7's sizing.

**Verification tasks before writing §18.8:**
- Confirm the migration/DB detectors already in the repo and reuse them rather than reinvent: `is_db_sensitive_run` (`backend/memory/conflict_scope.py`, already imported by `chunked_orchestrator.py`), and bootstrap's migration detection (alembic.ini / an `alembic/` dir, `prisma/schema.prisma`, etc., in `bootstrap.py`).
- Confirm there is **no** existing deterministic "security-sensitive path" classifier (the triage prompt names security but no code detects it). If one exists, reuse it; if not, §3 below defines a conservative one.
- Confirm the §18.7 validator's exact insertion point (post `ground → scan → reconcile`, re-run per plan version) and mirror it.

---

## 2. Design — a deterministic isolation advisory (mirror §18.7 exactly)

A pure, post-reconcile validator in the run-creation pipeline (same call site and per-plan-version cadence as §18.7), emitting **advisory** `[ISOLATION]` notes on the plan-gate card. Per chunk, over its `files_expected`:

- **Migration-not-isolated:** the chunk contains ≥1 migration path **and** ≥1 non-migration path → advisory "migration should be its own chunk." (Migration detection = the existing deterministic detectors from §1, not a new heuristic.)
- **Security-not-isolated:** the chunk contains ≥1 security-sensitive path **and** ≥1 non-security path → advisory "security change should be its own `requires_human_review` chunk."
- **Mixed-migration-and-security** in one chunk → both notes.

Surfaced exactly like §18.7's `[SIZE]` notes and the existing `[SCOPE]` notes: **advisory only — never auto-mutates the plan, never re-splits chunks, never blocks approval.** The natural remediation is a §19 plan-gate turn ("split the auth change in chunk 2 into its own chunk") — same workflow the sizing advisory already established. The LLM's own choice to isolate is displayed, but the deterministic detectors are the check input.

---

## 3. The one genuinely new detector — and its false-positive guard (load-bearing)

Migration detection already exists (§1). **Security-sensitive path detection** is the only new piece, and it must be designed against the E9 lesson, which the proposal itself documents: an over-eager deterministic classifier is what hardened a trivial calculator helper to `risk=high` (`file_scope_intent.py` bullet bug). Do not repeat that.

Constraints on the security detector:
- **Conservative, path-based, deterministic.** Match on path segments / filenames with a small curated set (e.g. `auth`, `oauth`, `login`, `password`, `permission`, `rbac`, `session`, `token`, `crypto`, `secret`, `security`), plus reuse of the existing forbidden-path / secret detectors in `path_safety`. No content scanning, no LLM, no fuzzy matching.
- **Advisory by default — never auto-hardens `risk_level`.** This is the explicit anti-pattern from E9. A false positive must cost the human a 5-second dismissal at the gate, not a forced high-risk + mandatory review on a trivial change.
- **The escalation question is a decision point, not an assumption** (see §4).
- **Tunable as policy** (the §4.7 policy module): the keyword/path set and the on/off switch live in policy, so a project with unusual naming can adjust without code.

State the honest residual: a security change in an unconventionally named file won't be detected (deterministic detectors only catch what they're told to). That is acceptable — rule 4's prompt instruction remains as the soft layer, the human still reviews the plan, and this advisory is *additive* signal, never the sole guarantee.

---

## 4. New decision point — D14

**D14 — Isolation advisory strength.** Three options, in increasing strength:
- **(a) Advisory-only** (recommended default): emit `[ISOLATION]` notes; the human acts via plan-gate turn or proceeds. Mirrors §18.7 exactly; zero false-positive cost.
- **(b) Advisory + escalate review:** a *confirmed migration* mixed with code, or a *confirmed security* path mixed with CRUD, additionally sets `requires_human_review=true` on that chunk (never `risk_level`, never a block) — reusing the existing high-risk⇒review precedent (`models/chunk.py:44-47`). Stronger guarantee; small false-positive cost (an unnecessary human-review flag, not a hardening).
- **(c) Hard gate:** block plan approval until isolation violations are resolved. **Not recommended** — it resurrects the E9 dead-end pattern (deterministic check blocking a human) and contradicts the proposal's "surface, never auto-block" discipline.

Recommendation: ship **(a)** first; offer **(b)** as a per-project policy escalation for teams that want the stronger backstop on migrations specifically (where false positives are near-zero because migration detection is exact). Never **(c)**.

---

## 5. Sequencing & tests

- **Slots next to §18.7** in the §23 table — a new item **6c** ("chunk-isolation advisory"), same dependencies (policy module #6, repo index), same area (Engine), no dependency on the driver or any other open decision except its own **D14**. Ships in the early deterministic batch, alongside sizing.
- **Pairs with plan-gate turns (§19, item 7b)** for remediation, exactly as the sizing advisory does — but does not block on it.
- Add **D14** to the §24 roster and gating map (item 6c needs D14; nothing else gates on it).
- **Tests (pure-function, zero LLM, zero filesystem):** migration-mixed-with-code → advisory; security-path-mixed-with-CRUD → advisory; isolated migration chunk → no note; isolated security chunk → no note; the curated keyword set's known true/false cases; the E9 regression guard (a trivially-named helper with no security/migration paths produces **no** isolation note and **no** hardening); option-(b) escalation sets `requires_human_review` but never `risk_level` and never blocks.

---

## 6. What this deliberately does **not** do

- It does not re-chunk the plan automatically (that is the LLM's job + the human's plan-turn).
- It does not block approval (D14-c rejected).
- It does not content-scan files or call an LLM.
- It does not replace rule 3/4 in the triage prompt — it is the deterministic backstop *under* them.
- It does not touch `scope_guard`, the approval gates, or any safety invariant; it only adds an advisory note and (under D14-b, if chosen) one `requires_human_review` escalation that matches existing behavior.
