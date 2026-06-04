# Next Phase — Recommendation and Roadmap Options

> Docs-only roadmap note written after the Operator State / Attention Panel phase
> and the Adversarial Reviewer Stage v1 **design doc**. It records the recommended
> immediate focus and the candidate next paths. It changes no runtime behavior,
> schema, routes, or packages, and it starts no implementation.

---

## Immediate recommendation

**Do demo / README / devex readiness before starting another large product
feature.**

Rationale:

- The recovery and validation chain (#26 patch failure recovery, #27 scope
  expansion recovery, #28 stronger test validation) and the Operator Attention
  Panel are complete and manually smoke-validated. This is a natural consolidation
  point.
- The project is **local self-use / demo-ready**. The highest-leverage next work is
  making it easy for a new user, reviewer, recruiter, or future AI assistant to
  **understand, run, and demo** Pipewright — not adding surface area.
- Readiness work is low-risk and docs/devex-only; it does not touch the safety
  substrate.

Concretely, the readiness focus is:

- a current, honest README and status snapshot (see
  [`../status/current-state.md`](../status/current-state.md));
- a repeatable demo / smoke checklist (see
  [`../testing/demo-smoke-checklist.md`](../testing/demo-smoke-checklist.md));
- demo polish (screenshots / a short recording of the flow and the Attention
  Panel);
- small, honest stabilization fixes only.

---

## Reviewer stage: designed, not implemented

- The **Adversarial Reviewer Stage v1 design is merged**
  ([`../design/adversarial-reviewer-stage.md`](../design/adversarial-reviewer-stage.md)).
- **No reviewer code ships and no AI review runs in the pipeline today.**
- v1 is specified as **advisory / display-only**: it gates nothing, commits
  nothing, mutates nothing, writes no memory, and does not weaken #26/#27/#28.

> **Prioritize before coding.** The reviewer stage is a **new product feature**, not
> UI polish. It must be explicitly prioritized against this readiness phase before
> any implementation slice begins. Building it strengthens no existing guarantee on
> its own and adds an LLM call, a storage surface, and a sycophancy/hallucination
> risk in exchange for advisory commentary — acceptable, but a deliberate choice,
> not a default.

---

## Possible next paths

These are options, not a committed sequence. Pick one deliberately.

### A. Demo polish / public README  — *recommended now*

- Public-facing README pass, screenshots, short demo recording.
- Tighten onboarding and the demo checklist.
- Lowest risk; no runtime change.

### B. Reviewer implementation

- Implement the merged design in small, safe slices (models/storage → internal
  advisory execution → read-model surfacing → frontend panel → optional deferred
  acknowledgement gate after smoke).
- **New product feature** — requires an explicit go-ahead first (see above).
- Must remain advisory/display-only in v1; must not change chunk outcomes.

### C. GitHub / PR robustness and checks integration

- Broaden PR preflight taxonomy (remote head verification, origin-base comparison),
  status/checks awareness.
- Still bounded by existing safety rules (no auto-merge; protected base branches).

### D. Production hardening

- Postgres / Alembic path, durable events, DB locks at scale, deployment.
- Larger effort; explicitly paused under current project rules until prioritized.

### E. Multi-LLM / provider modes

- Execution modes (fast / standard / deep), per-role model config UX, fallback,
  token/cost tracking.
- Note: per-role model selection already exists via env config; a routing **UI** is
  not implemented and is out of the reviewer stage.

### F. Memory M3

- Conflict lifecycle, dedicated memory categories, memory-usage tracking,
  constrained LLM-assisted memory, pgvector only when scale justifies.

---

## Guardrails for whatever comes next

- Do not start reviewer implementation without an explicit priority decision.
- Do not add new product features during the readiness phase.
- Do not change runtime behavior, schema, routes, or packages in docs-only PRs.
- Do not weaken `scope_guard` or auto-expand `files_expected`.
- Do not bypass chunk plan approval or final approval; final approval is never
  automatic.
- Do not claim Pipewright proves code correctness, or that the reviewer is live.

---

## Related docs

- Current status — [`../status/current-state.md`](../status/current-state.md)
- Demo / readiness smoke checklist — [`../testing/demo-smoke-checklist.md`](../testing/demo-smoke-checklist.md)
- Reviewer design (deferred) — [`../design/adversarial-reviewer-stage.md`](../design/adversarial-reviewer-stage.md)
- Operator State design — [`../design/operator-state-attention-panel.md`](../design/operator-state-attention-panel.md)
- Earlier roadmap context — [`../status/stabilization-closeout.md`](../status/stabilization-closeout.md)
