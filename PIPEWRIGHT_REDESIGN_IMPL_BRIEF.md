# Pipewright Redesign — Rolling Implementation Brief

**Date:** 2026-06-14
**Status:** Dormant after Row 11 closeout. Do not use this file to start a new
implementation slice until the maintainer explicitly opens the next row and
repurposes the brief.

---

## Row 11 Closeout

§23 order-row 11 — detection rules-as-data — is complete.

- **PR-A complete:** extracted bootstrap detection into rules-as-data /
  evaluator form while preserving ordered candidate parity.
- **Pre-PR-B fixture hardening complete:** added the missing detector fixture
  coverage before repo reality signals reused the shared rule vocabulary.
- **PR-B complete:** added pure/read-only advisory repo reality signals for
  non-DB dimensions. Signals are compute-on-read and surface only through the
  existing memory injection analysis `reality_warnings` path. `db_engine` and DB
  conflict gate behavior remain unchanged, and PR-B mutates no memory facts.
- **PR-C complete:** refactored `backend/pipeline/test_command_detection.py`
  into explicit ordered detector rules while preserving byte-identical
  `suggested_test_command` behavior.

PR-C included **no PR-C2/new detector coverage** and no classifier,
runtime-validation, frontend, schema, memory, gate, scope, Git, or PR behavior
changes.

## Current Pause

Do not start Row 12, Row 16, Row 19, Row 23, or thread UI from this brief.
The next memory row requires an explicit maintainer decision before implementation
planning or coding begins.

## Canonical Pointers

- Current status snapshot: `docs/status/current-state.md`
- Sequence and decisions: `PIPEWRIGHT_REDESIGN_WORKPLAN.md`
- Source proposal: `PIPEWRIGHT_REDESIGN_PROPOSAL.md`
