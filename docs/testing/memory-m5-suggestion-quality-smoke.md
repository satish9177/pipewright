# Memory M5 Suggestion-Quality Gate — Smoke & Closeout

> Closeout record for **§23 order-row 7 — the M5 suggestion-quality gate**
> (Pass 2 / Area B §11.3), the first Area B (memory) slice. This is the
> post-M5 self-use smoke the Appendix E.1 hard-stop requires before any deeper
> memory row opens. **Docs-only page; it changes no runtime code or schema.**
>
> **Numbering hazard:** this is the §23 **order-row 7 / M5** gate, *not* the
> Pass 1 §6 engine "item 7" (the Signal C classifier, done in Phase 1). See
> the proposal's Appendix E numbering-hazard note.

## Status

**Complete enough to move on.** Implemented and merged to `develop` via
**PR #292** (`feature/m5-suggestion-quality-gate`; commit `65911b3` "Add memory
suggestion quality gate"; design doc commit `23b6c02` "memory phase design").

### What shipped

- **Deterministic scorer (7-α).** `backend/memory/suggestion_quality.py` — a
  pure, string-only `score_suggestion(content, category)` returning a frozen
  `SuggestionQualityResult(score, quality, reasons, floored)`. No LLM, no DB, no
  filesystem, no repo/log reading. `is_objective_junk` is the only flooring
  decision (empty-after-normalize, pure run-reference, low-information denylist).
- **Floor + caps + ranking** in the one unbounded channel, `_handoff_candidates`
  (`backend/memory/run_outcome_suggestions.py`): objective junk is floored
  (counted as `floored_count`, never inserted); non-junk is ranked by
  `quality_score`; volume is capped by `policy.HANDOFF_SUGGESTION_CAP = 5` and
  `policy.RUN_SUGGESTION_TOTAL_CAP = 8` (over-cap counted as `capped_count`).
- **Structured coder channel (7-β, coder side).** `coder.py` emits
  `{content, category, scope, rationale}`; `CoderHandoff.suggested_memory_entries`
  is `List[SuggestedMemoryEntry]` (`backend/models/handoff.py`) with a tolerant
  `legacy_string_to_object` validator so legacy bare strings still parse
  (`category="other"`, `scope="global"`).
- **Additive `quality_score INTEGER` column** on `memory_suggestions`
  (`schema.sql`), threaded NULL-safe through `insert_pending_suggestion`; legacy
  and non-handoff callers are unaffected.
- **The content gate is never bypassed.** M5 sits *in front of*
  `validate_memory_content`; `blocked_count` (gate) and `floored_count`
  (quality) stay distinct counters. The three deterministic/template channels
  (`_completed_run_candidates`, `_patch_failure_candidates`,
  `_rejection_candidates`) were not touched.

### Non-blocking loose ends (tracked, do not block Row 11)

1. **Planner still asks for bare-string suggestions.** `planner.py` solicits
   `"suggested_memory_entries": ["fact …"]` and `PlannerHandoff.suggested_memory_entries`
   is still `List[str]`. **This does not feed the handoff-candidate path:**
   `_build_completion_summary` serializes **only the coder output**
   (`_serialize_suggested_memory_entries(coder_output: CoderHandoff)` in
   `chunked_orchestrator.py`), so planner entries never reach the completion
   summary or `_handoff_candidates`. Functionally harmless; a cosmetic cleanup
   (stop soliciting a discarded field) for a future slice.
2. **Inbox ranking / near-dup grouping** (M5 design open-decision #4). The
   `quality_score` is persisted and surfaced on the suggestions response model.
   **Verify** whether the inbox read path actually orders by `quality_score` and
   annotates near-duplicates via `find_duplicate_candidates` (advisory only); if
   not, track it as a small display-only follow-up. Not a blocker.

## Preconditions

- On latest `develop` with PR #292 merged.
- A project exists with a repo path and a test command.
- At least one terminal run with planner/coder handoff suggestions, or create
  one.

## Backend test commands

```powershell
venv\Scripts\python.exe -m pytest backend\tests\test_suggestion_quality.py -v
venv\Scripts\python.exe -m pytest backend\tests\test_memory_run_outcome_suggestions.py -v
venv\Scripts\python.exe -m pytest backend\tests\test_handoff_models.py -v
venv\Scripts\python.exe -m pytest backend\tests\ -v -k "memory or suggestion"
venv\Scripts\python.exe -m pytest backend\tests\ -q -m unit
```

## Frontend test command

```powershell
cd frontend
npm.cmd run build
```

## Manual smoke 1 — junk is floored, not queued

Steps:

1. Open a terminal run whose coder handoff included a pure run-reference
   ("run 1234 completed") or a low-information generic ("uses Python").
2. Generate memory suggestions.
3. Confirm `floored_count` reflects the junk and those entries are **not** in the
   pending queue.

Expected:

- Objective junk is counted, never inserted.
- Borderline / weak-but-specific facts still reach the queue ("surface before
  suppress").

## Manual smoke 2 — caps and ranking

Steps:

1. Use (or simulate) a run whose handoff produced more than 5 non-junk
   suggestions.
2. Generate suggestions.
3. Confirm at most `HANDOFF_SUGGESTION_CAP` (5) handoff suggestions are inserted,
   highest `quality_score` first, with the remainder counted as `capped_count`.
4. Confirm the per-run total respects `RUN_SUGGESTION_TOTAL_CAP` (8) after the
   deterministic/template channels are reserved.

Expected:

- The cap keeps the highest-scored candidates, not first-seen.
- Deterministic/template channels (completed-run, patch-failure, rejection) are
  preserved regardless of the handoff cap.

## Manual smoke 3 — structured coder channel + legacy tolerance

Steps:

1. Confirm a current coder handoff emits structured entries
   (`content`/`category`/`scope`/`rationale`).
2. Confirm a legacy bare-string entry still parses (category `other`, scope
   `global`).
3. Confirm `category` / `scope` outside the closed enums normalize to
   `other` / `global` rather than erroring.

## Manual smoke 4 — content gate still wins

Steps:

1. Drive a handoff suggestion containing a secret, absolute path, stack trace,
   or control-plane phrase ("skip approval", "auto-merge").
2. Generate suggestions.
3. Confirm it is **blocked** (counts as `blocked_count`), never stored — distinct
   from quality flooring.

Expected:

- M5 reduces what reaches the gate; it never substitutes for it.

## Manual smoke 5 — pending-only, human-approved

Steps:

1. Generate suggestions for a run.
2. Confirm every candidate lands **pending**; no active fact is created.
3. Approve one; confirm it becomes active only then.
4. Re-generate; confirm idempotency (generated 0 / skipped > 0).

Expected:

- No active memory without human approval.
- Quality scoring changes *which* facts are admitted, never *whether* a human
  approves them.

## Self-use closeout (Appendix E.1 hard-stop)

The post-M5 self-use pass surfaced and landed several non-memory stabilization
fixes already on `develop` (test-command classification — "Recognize wrapped and
absolute test commands"; create-collision copy — "Clarify create-existing patch
failures"; failed-chunk retry; "Annotate coder target file existence"). These
are recorded here as the closeout evidence that the M5 cycle's hard-stop smoke
was exercised, not skipped.

## Known limitations / non-goals (what M5 is NOT)

- No request-aware selection or injection change (that is §23 row 12, gated D5).
- No retriever / FTS (row 19); no embeddings / vector memory (row 23, gated D6).
- No scheduled post-run hygiene / auto-generation (row 16, gated D7).
- No detection rules-as-data yet (row 11 — the next slice; see the workplan).
- The scorer is deterministic and conservative; it may rank a genuinely useful
  but oddly-phrased fact low. It never suppresses non-junk.

## Done criteria

- `test_suggestion_quality.py`, `test_memory_run_outcome_suggestions.py`, and
  `test_handoff_models.py` pass; memory marker green; frontend build passes.
- Junk floored, caps enforced, ranking applied; legacy bare strings still parse.
- Content gate still blocks unsafe content distinctly from quality flooring.
- No active memory is created without human approval.
- The two loose ends above are recorded as non-blocking follow-ups.
