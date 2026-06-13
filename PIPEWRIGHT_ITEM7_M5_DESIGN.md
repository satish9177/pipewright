# Item 7 — M5 Suggestion-Quality Gate — Design Brief (DESIGN-FIRST, no code)

**Date:** 2026-06-13
**Slice:** §23 order-row **7** — "M5 quality gate (structured handoff schema + scorer + floor + inbox ranking)" (Area B / Pass 2 §11.3).
**Status:** ✅ **COMPLETE — shipped as PR #292 (2026-06-14).** This was the design-first brief; it is retained below as the historical record. First slice of Area B (Pass 2 — Memory).

> **Numbering hazard.** This is the **§23 order-row 7 / Pass 2 §11.3 M5 gate**, *not* the workplan/Pass 1 §6 engine "item 7" (the Signal C classifier, already done). See Appendix E's reconciliation note. The file is named `ITEM7_M5` to keep the two apart.

---

## 0. Closeout (2026-06-14)

**Shipped as PR #292** (commit `65911b3` "Add memory suggestion quality gate"; design commit `23b6c02`). Recommendation §4's **7-α** (deterministic gate) shipped in full, and **7-β** (structured channel) shipped **on the coder side**:

- `backend/memory/suggestion_quality.py` — pure, string-only `score_suggestion` + `is_objective_junk` floor (the only flooring decision).
- `_handoff_candidates` (`run_outcome_suggestions.py`) — floor (`floored_count`), score-rank, and cap (`HANDOFF_SUGGESTION_CAP = 5` / `RUN_SUGGESTION_TOTAL_CAP = 8`, `capped_count`). The three deterministic/template channels were left untouched, as designed.
- `coder.py` emits `{content, category, scope, rationale}`; `CoderHandoff.suggested_memory_entries: List[SuggestedMemoryEntry]` with a `legacy_string_to_object` tolerant validator (legacy bare strings → `category="other"`, `scope="global"`).
- Additive `quality_score INTEGER` on `memory_suggestions`, threaded NULL-safe through `insert_pending_suggestion`.
- Pending-only throughout; the #21B content gate is never bypassed (`blocked_count` distinct from `floored_count`).

**Two non-blocking loose ends (do NOT block Row 11):**

1. **Planner channel still bare strings.** `planner.py` still solicits `["fact …"]` and `PlannerHandoff.suggested_memory_entries` is `List[str]`. This **does not feed the handoff-candidate path** — `_build_completion_summary` serializes only the coder output (`_serialize_suggested_memory_entries(coder_output)`), so planner entries never reach `_handoff_candidates`. Harmless; a cosmetic cleanup (stop soliciting a discarded field) for a later slice.
2. **Inbox ranking / near-dup grouping** (open-decision #4 below) — `quality_score` is persisted and surfaced on the suggestions response model; **verify** the inbox read path orders by it and annotates near-dups via `find_duplicate_candidates` (advisory only), or track as a small display-only follow-up.

**Smoke / closeout doc:** `docs/testing/memory-m5-suggestion-quality-smoke.md`. **Next slice:** §23 order-row 11 (detection rules-as-data), PR-A first — see `PIPEWRIGHT_REDESIGN_WORKPLAN.md` and Appendix E.1/E.2 (reconciled 2026-06-14).

The original design brief follows unchanged.

---

## 1. Why this is the first Area B slice (and why it precedes retrieval)

The proposal is emphatic (§8.3 deficit 2, §11.3): **entry quality must precede retrieval work.** Three reasons, verified:

1. Today's stores are small — the felt failure is *wrong/noisy facts injected*, not *relevant facts missed*. The quality gate buys more felt improvement per PR than any retriever.
2. Embeddings (rung 2, row 23) index whatever exists — embedding junk persists junk into a second derived store and re-embeds it on every edit.
3. The suggestion inbox is the human curation loop. Queue noise is what makes humans stop curating; once curation stops, every downstream memory layer degrades.

**Honesty clause (carried from §11.3):** semantic retrieval will not fix vague, wrong, or contradictory facts, or a store nobody curates. M5 changes *which* facts are admitted, not whether they were worth storing. It is independent of every other Area B item and gated on no §24 decision (§23 marks row 7 "—").

## 2. Verified current state (read 2026-06-13)

- **The prompt asks for bare strings.** `planner.py:57` and `coder.py:73` both solicit `"suggested_memory_entries": ["fact worth storing…"]`. The models carry them as `List[str]` (`models/handoff.py:46` `PlannerHandoff`, `:77` `CoderOutput`).
- **The unbounded channel is exactly one:** `_handoff_candidates` (`run_outcome_suggestions.py:229-256`). For every chunk's `completion_summary.suggested_memory_entries`, any non-empty string `≤ MAX_HANDOFF_ENTRY_CHARS` (280) becomes a pending suggestion with `category="other"`, **no scoring, no relevance, no volume cap.** The coder's entries reach the summary at `chunked_orchestrator.py:652`.
- **The other three channels are template-bounded by construction** (`_completed_run_candidates`, `_patch_failure_candidates` from a fixed enum table, `_rejection_candidates` sanitized) — §11.3 explicitly narrows M5 to the handoff passthrough. **Do not touch the three deterministic channels.**
- **The write path is safe already** and must stay the gate: `insert_pending_suggestion` (`bootstrap.py:725`) runs `validate_memory_content` (the #21B content gate — control-plane phrases, secrets, absolute paths, stack traces) + per-project content-hash dedupe + rejected-suggestion non-return. M5 sits *in front of* this, never around it.
- **No quality column exists.** `memory_suggestions` (`schema.sql:28-56`) has no `quality_score`. Ranking the inbox needs an additive column.
- **The advisory dedupe helper exists:** `find_duplicate_candidates` (`memory_trust.py:274`, M3B) — pure, advisory, never merges. Reuse it for "group near-dups in the inbox," never to auto-merge.
- **The pattern to mirror:** `test_command_quality.py` — pure, string-only, `Enum` + frozen `@dataclass` result, deterministic `classify_*`, zero LLM/DB/filesystem. The scorer is the same shape.

## 3. Scope of this slice (what §11.3 asks for)

Four pieces:

1. **Structure the channel** — `suggested_memory_entries` becomes the `memory-architecture.md §2.12` shape (`content / category / scope / rationale`), validated against the closed enums; a **tolerant parser** accepts legacy bare strings (category defaults to `other`).
2. **Score at generation time** — a pure scorer rates each candidate: penalties for run-specific references, path-only trivia, low-information genericity ("uses Python"-class); rewards for constraint form ("never X", "X lives in Y", "run tests with X") and category specificity. Score persists (additive column), ranks the inbox, groups near-dups (advisory).
3. **Floor only objective junk** — candidates below a conservative deterministic floor (empty after normalization, pure run-reference, denylist hits) are **not inserted**; they are *counted* and surfaced like today's `blocked_count`. Borderline always reaches the queue ("surface before suppress").
4. **Volume caps as policy** — per-run handoff-passthrough cap (default **5**), per-run total cap (default **8**) — `memory-architecture.md §9.1`'s numbers, relocated into the §4.7 policy module, not re-invented.

## 4. Recommended internal sequencing — split item 7 into 7-α then 7-β

The riskiest part of §11.3 is **piece 1 (structure the channel)**: it changes the planner *and* coder JSON output schema, the `PlannerHandoff`/`CoderOutput` models, the trivial-profile synth (`stage_profile.py:203` sets `suggested_memory_entries=[]`), and needs a legacy-tolerant parser — i.e. it touches the LLM stages. Pieces 2–4 are pure deterministic consumption-boundary work that needs **no prompt change at all** (a string is enough input to score, floor, and cap).

This is the same "pure deterministic modules first, LLM-touching change second" philosophy the whole redesign used in Phase 0. So I recommend item 7 ship as two PRs:

- **7-α (deterministic gate — recommended first, decision-free, no prompt/model change):**
  - New pure module `backend/memory/suggestion_quality.py` mirroring `test_command_quality.py`: `SuggestionQuality` enum (e.g. `JUNK / WEAK / OK / STRONG`) + frozen `SuggestionQualityResult(score: int, quality, reasons: list[str])`; pure `score_suggestion(content, category=...)`. String-only, no LLM/DB/filesystem.
  - Apply it **only inside `_handoff_candidates`**: compute the score; **floor** the objective-junk tier (don't insert, increment a new `floored_count` on `RunSuggestionResult`); apply the **per-run handoff cap** (keep the top-N by score). The three template channels are untouched.
  - Additive `quality_score INTEGER NULL` on `memory_suggestions` (guarded idempotent `_ensure_*_shape` migration, the established pattern); thread it through `insert_pending_suggestion` as an optional param (NULL-safe; bootstrap/other callers unaffected).
  - Caps + floor threshold live in `policy.py` (`HANDOFF_SUGGESTION_CAP = 5`, `RUN_SUGGESTION_TOTAL_CAP = 8`, `SUGGESTION_QUALITY_FLOOR`).
  - **Inbox ranking + near-dup grouping** in the suggestions read model/route (rank by `quality_score`, annotate near-dups via `find_duplicate_candidates`) — display-only, advisory; can be the tail of 7-α or its own tiny follow-up.
- **7-β (structured channel — follow-up, coordinates with the prompt):**
  - Change the planner/coder prompt to emit `{content, category, scope, rationale}`; widen the model field to a structured type; tolerant parser accepts legacy bare strings (→ `category="other"`); update the trivial-profile synth.
  - The scorer from 7-α now reads real `category`/`scope` instead of inferring, sharpening "category specificity" rewards. No new safety surface — the structured fields still pass through the same content gate and stay pending.

7-β is *optional within this cycle* — 7-α already delivers the felt win (junk floored, queue capped and ranked). If we want to honor Appendix E's "one slice, then self-use smoke," **7-α is the slice; 7-β is gated behind the smoke** or folded in only if review wants it now.

## 5. Safety invariants preserved (state explicitly in every PR)

- **Pending-only, human-approved.** M5 never creates an active fact, never approves. Every candidate still flows through `insert_pending_suggestion` → content gate → dedupe → human lifecycle. (Contract §2.8.)
- **No LLM, no embeddings, no repo/log reading in the scorer.** Pure string function. Determinism is the auditability the product sells.
- **The content gate is never bypassed.** M5 *reduces* what reaches the gate; it never substitutes for it. Blocked-by-gate (`blocked_count`) and floored-by-quality (`floored_count`) stay distinct counters.
- **Project-scoped, fail-closed** unchanged (no `project_id` → empty result, as today `:323-329`).
- **The floor's honest edge (record it):** a floored candidate was never inserted, so rejected-suggestion dedupe can't suppress its regeneration next run. Acceptable precisely because the floor is deterministic — same junk, same floor, zero queue cost (§11.3).
- **Memory stays advisory.** Nothing here grants scope, gates execution, or touches Git/PR. (Contract §2.7.)

## 6. Tests (mirror `test_command_quality`'s discipline; pure, zero LLM/DB)

- Scorer unit tests over a table of real examples: run-reference junk → floored; path-only trivia → weak; "never commit secrets to `.env`" / "tests run with `pytest -q`" → strong; generic "uses Python" → floored/weak; category-specific → reward. Boundary tests at the floor.
- Channel tests: handoff list with N>cap → top-N by score inserted, rest dropped with `floored_count`/cap accounting; legacy bare strings still parse; the three template channels produce identical output to today (parity golden).
- Migration idempotence: `quality_score` column added once, NULL for legacy rows; `insert_pending_suggestion` NULL-safe for non-handoff callers.
- Inbox: ranked by score; near-dup grouping is advisory annotation only (no mutation).

## 7. Open decisions for the maintainer (design-first review)

1. **Sequencing:** ship **7-α only** this cycle (deterministic gate; defer 7-β structured prompt behind the self-use smoke) — *recommended* — or do **7-α + 7-β together** now? (7-β touches the LLM prompts + trivial-profile synth.)
2. **Floor aggressiveness:** confirm the JUNK tier is *only* the three objective patterns (empty-after-normalize, pure run-reference, denylist genericity). Anything fuzzier risks suppressing a real fact silently — the proposal says "surface before suppress."
3. **Cap defaults:** accept §9.1's 5 (handoff) / 8 (run-total), or different? When the cap binds, keep the **highest-scored** candidates (recommended) vs. first-seen.
4. **Inbox ranking/grouping placement:** part of 7-α, or a separate tiny display PR (keeps 7-α backend-pure)?
5. **`quality_score` semantics surfaced to the human?** Show the score/reasons in the inbox card (more transparent, mirrors `TestCommandQualityResult.reason`), or keep it ranking-only?

## 8. What this deliberately does NOT do

No request-aware selection (row 12), no retriever/FTS (row 19), no embeddings (row 23), no detection rules-as-data (row 11), no post-run hygiene auto-generation (row 16) — those are later Area B slices, several gated on D5/D6/D7. No change to the three template suggestion channels. No new approval path, no `scope_guard`/gate/Git contact. The scorer never calls a model.

---

*Design only. Next step on approval: repurpose `PIPEWRIGHT_REDESIGN_IMPL_BRIEF.md` into the 7-α implementation handoff, or implement 7-α directly as one reviewed PR per the §E.4 template — maintainer's call. Per the workplan, commit these planning docs only when asked.*
