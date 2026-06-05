# Pipewright Durable Agent Runtime — Architecture & Phased Roadmap

**Status:** Design only. Not implementation. Not a coding task.
**Audience:** Pipewright maintainers.
**Mode:** Adversarial. Real tradeoffs, real failure modes, honest evaluation of LangGraph.

---

## 0. Critical Framing (Read Before Anything Else)

Before designing a "background engineering agent," challenge the frame:

1. **"Background agent" is a marketing word that means many different things.** Devin uses it to mean "runs unsupervised for hours, merges code." Replit Agent uses it to mean "interactive but persistent." If Pipewright adopts the term carelessly, the team will start *optimizing for autonomy* and erode the approval-gate moat. Pipewright's "durable agent" must mean: **a pipeline orchestrator that survives crashes and approval delays, not a system that operates unattended.**

2. **Most of what's described as "durable agent capability" is just "we have a worker queue plus a database."** Job queues, dead-letter queues, idempotency keys, exactly-once-vs-at-least-once — these are standard backend concerns, not novel agent infrastructure. Treating them as exotic introduces magic where there should be plumbing.

3. **The existing chunked orchestrator already has most of the state machine.** `running`, `awaiting_chunk_approval`, `awaiting_final_approval`, `failed`, plus resume via checkpoint+commit-hash validation, plus per-project repo locks. What's missing is *durability across process boundaries*, not the state machine itself. Don't rewrite — extend.

4. **The realistic durability use case is "approval delayed overnight" or "process restarted at 2am," not "agent runs unsupervised for 72 hours."** Optimize for the boring cases. The exotic cases will follow from doing the boring ones right.

5. **"Exactly-once" does not exist** in any distributed system that includes external side effects (git push, GitHub API). The correct goal is **at-least-once execution with idempotent side effects.** Anyone arguing for "exactly-once" is selling something.

Everything below assumes this framing.

---

## 1. Product Definition

### 1.1 What "background engineering agent" means for Pipewright

A Pipewright run is *durable* if it satisfies all of the following:

- The run can outlive the API process. Closing the browser, restarting the backend, or rebooting the host does not lose progress or cause duplicate work.
- The run can pause for human approval for hours or overnight and resume identically when approval arrives.
- The run can survive provider failures: rate limits, timeouts, transient 5xx — by pausing or retrying with backoff, not by silent failure.
- The run can survive worker crashes mid-chunk. On worker restart, work continues from the last durable checkpoint with verified git state.
- The run has an immutable audit trail of who/what/when, including which model produced which output and which human approved which step.

That set is the definition. Notice what's **not** in it: "runs without supervision," "decides when to merge," "operates over multiple days unattended." Those are different products.

### 1.2 How it differs from fully autonomous coding

| Property | Fully autonomous (e.g., Devin) | Pipewright durable agent |
|----------|-------------------------------|---------------------------|
| Approval before commit | No | **Mandatory.** |
| Approval before PR | No | **Mandatory.** |
| Approval before merge | No (or weak) | **Mandatory, and Pipewright never merges.** |
| Run duration target | Hours to days | Typically minutes; can pause for days while awaiting approval. |
| Failure mode | Best effort, hopes for the best | Halt and escalate to a human. |
| Audit trail | Variable | Mandatory, immutable, complete. |
| Multi-LLM with role isolation | Sometimes | Yes, with adversarial reviewer != coder. |

Pipewright's durable agent is *less autonomous and more dependable* than the autonomous category. That is the position.

### 1.3 How it differs from current chunked execution

Current chunked execution already has:
- Sequential chunks with per-chunk checkpoints.
- Resume from checkpoint with git commit-hash validation.
- Approval gates for chunk plan, high-risk chunks, and final.
- Per-project repo locks (in-process).

What it lacks:
- Locks die when the process dies (no DB-backed locks).
- Live logs die when the process dies (in-memory event bus).
- Long-running execution holds the API event loop (no worker process).
- Resume requires a manual API call (no background restart-on-boot).
- No durable record of *intent* — only of *current state*. Cannot answer "what was supposed to happen next when the crash occurred?"

### 1.4 What stays human-controlled

Forever:
- Chunk plan approval.
- High-risk chunk approval.
- Final diff approval.
- PR creation trigger.
- Memory promotion from suggestion to long-term.
- Provider/model configuration.

The durable runtime never decides on its own to bypass any of these. If a human is unreachable, the run waits. The default state for "no human input" is **paused**, not **proceed**.

---

## 2. Core Requirements

Each requirement maps to one or more roadmap phases (section 20).

| # | Requirement | Maps to |
|---|-------------|---------|
| 1 | Long-running runs (minutes today; hours feasible) | R1, R5 |
| 2 | Background execution after browser close | R2 |
| 3 | Resume after backend restart | R1, R5 |
| 4 | Resume after user session/token expiry | R6 |
| 5 | Resume after provider rate limit | R7 |
| 6 | Resume after provider timeout | R7 |
| 7 | Resume after worker crash | R2, R5 |
| 8 | Approval pause/resume across process boundaries | R6 |
| 9 | Durable logs | R3 |
| 10 | Durable checkpoints | R1, R5 |
| 11 | Immutable per-run model + memory snapshot | R1 (uses snapshot work from Memory-M1/LLM-M1) |
| 12 | Safe repo locks across processes | R4 |
| 13 | Safe git state validation on resume | R5 (extends existing) |
| 14 | No duplicate PR creation | R5, R7 |
| 15 | No duplicate chunk execution | R2, R5 |
| 16 | Full human visibility into what happened | R3 |

---

## 3. Current-State Gap Analysis

Honest catalog of what breaks today. The current stack is SQLite, in-memory event bus, in-process locks, FastAPI handling pipeline work directly.

| Scenario | What breaks today | Severity |
|----------|--------------------|----------|
| Large project, run takes 45 minutes | API event loop is busy the whole time. WebSocket clients drop. Other API calls degrade. | High |
| API process restart mid-chunk | Run status is left in `running` with no executor. Manual resume API call required. Status fields lie. | **Critical** |
| User closes browser | Run continues (good) but logs are lost (in-memory event bus). UI can't reconstruct history. | High |
| Approval delayed for hours | OK in-process if API stays up. If API restarts during the wait, in-memory state about *which approval is pending* is fine (it's in DB), but in-memory locks, in-memory events, and any in-flight executor coroutine are gone. | High |
| Provider rate limit | Current code sleeps 60s in the event loop, blocking other work. Real rate-limit windows can be minutes to hours; sleeping doesn't scale. | High |
| Chunk takes too long (single LLM call > 5 minutes) | FastAPI request that triggered execution times out; client sees error even though work continues. | Medium |
| Two runs touch same repo | In-process lock blocks the second within the same API process. **A second API process would not be blocked.** Single-instance assumption is load-bearing. | High |
| Multiple users / load balancer | None of the locks, none of the event bus, none of the in-memory state survives. Pipewright is single-instance by construction. | Critical (for "deployment" goal) |
| Resume after process restart | Works, *because the existing code does this on demand* via `resume_chunked_pipeline`. But it's not automatic — somebody has to call it. | Medium |
| Live logs after restart | **Lost.** Event bus is in-memory. UI has no way to reconstruct what happened during the previous process lifetime. | High |

The gap is not "we don't have an agent." The gap is **"we don't have a durable backend."** Naming the work "durable runtime" rather than "agent" keeps the team honest about what's actually being built.

---

## 4. Architecture Options

Three serious options. Compare adversarially.

### 4.1 Option A — Custom runtime (Postgres + Alembic + ARQ + own state machine)

**Stack:**
- PostgreSQL (replaces SQLite).
- Alembic migrations.
- Redis + ARQ for the worker queue (lighter than Celery, modern async-native).
- DB-backed run state machine (extends what exists in `pipeline_runs.status`).
- DB-backed event log (replaces in-memory bus).
- DB-backed advisory locks (Postgres `pg_advisory_lock` keyed by `(project_id, repo_path)`).
- Custom checkpoint tables (per chunk per step).
- Custom approval pause/resume (DB poll + event push).

**Pros:**
- Every concept is a thing in Pipewright's own domain model. No abstraction debt.
- Debugging is plain SQL and plain Python. Stack traces lead directly to your code.
- The existing chunked orchestrator extends naturally. Most code is reusable.
- Provider abstraction (LLM-M1) and memory architecture (Memory-M1) compose cleanly.
- No third-party agent framework to track for breaking changes.

**Cons:**
- More code to write than Option B. You own everything — including the bugs.
- State-machine plumbing, retry policies, idempotency-key tracking, replay logic — all has to be implemented and tested.
- No free "time travel" debugging. You'd have to add it if you want it.
- Easier to design a slightly wrong state machine and realize 6 months later.

**Complexity:** Medium-high.
**Debugging experience:** Excellent (own code, own DB).
**Lock-in risk:** Zero.
**Fit for Pipewright:** **Very high.** The product already is a domain-specific orchestrator with strong opinions about chunks, approvals, locks, and PR safety. Custom expresses those opinions directly.
**What you gain:** Total control. Direct debuggability. Clean composition with provider and memory work.
**What you lose:** A few years of LangChain team's checkpointer engineering and the surrounding tooling.

### 4.2 Option B — LangGraph runtime end-to-end

**Stack:**
- LangGraph as the orchestration substrate. Nodes are pipeline stages; edges are transitions; the graph is the run.
- LangGraph's PostgresSaver checkpointer.
- LangGraph's `interrupt()` for human approval pauses.
- LangChain ChatModel adapters in place of Pipewright's Provider abstraction.
- Patch application, git ops, GitHub PR — still custom.

**Pros:**
- Checkpointer is battle-tested.
- Interrupt/resume primitive is exactly the right shape for approval gates.
- Streaming events out of the box.
- "Time travel" — replay from any checkpoint — comes free.
- LangGraph community is active and growing.

**Cons:**
- LangChain ChatModel interface and Pipewright's `Provider` interface (from LLM-M1) overlap. Adopting LangGraph likely means adopting LangChain models too, undoing the LLM-M1 abstraction you just designed.
- LangChain has historical churn between versions. v0.1 → v0.2 → v0.3 broke real code. LangGraph is newer and stabilizing but still moves.
- The "node returns state updates that get merged" model is great for chat agents and awkward for stages whose primary effect is a side effect (git commit, filesystem write, GitHub API call). You end up writing nodes that are mostly side effects with thin state-merge wrappers.
- Two sources of truth for run state: LangGraph's checkpoint + Pipewright's `pipeline_runs` row. Keeping them consistent is real work.
- Debugging traces through framework layers. A bug in a custom node looks like a bug in user code wrapped in 5 layers of LangGraph machinery.
- Memory: LangChain's `Memory` classes are at odds with Pipewright's deliberate three-tier memory design. You'd need to either ignore them (mixed mental model) or wrap them (extra adapter).
- Lock-in: serialized checkpoint state is in LangGraph's format. Migrating away is non-trivial.
- LangGraph's `interrupt()` assumes the human responds in a reasonable timeframe. Multi-hour delays work but expose edges (e.g., what happens to the saved state object after the process restarts — it does work, but you have to test it carefully).

**Complexity:** Medium-low for the happy path; medium-high for the edge cases.
**Debugging experience:** Mixed. Good when the framework cooperates; bad when it doesn't.
**Lock-in risk:** Significant. Switching frameworks later is a rewrite.
**Fit for Pipewright:** Moderate. Pipewright already has its own domain model. LangGraph's substrate is largely redundant with what exists.
**What you gain:** Faster path to durable checkpoints. Free streaming. Free time travel.
**What you lose:** Provider abstraction freedom. Direct debuggability. Independence from a fast-moving framework.

### 4.3 Option C — Hybrid (custom domain model, LangGraph checkpointer only)

**Stack:**
- Custom Pipewright state machine (Option A).
- Custom worker queue (ARQ).
- Custom provider abstraction (LLM-M1).
- Custom memory (Memory-M1).
- **LangGraph's PostgresSaver used as the durable state store for run state.**
- LangGraph not used for graph orchestration. No `interrupt()`, no nodes, no graph.

**Pros:**
- You get the well-tested checkpoint persistence and the snapshot/replay APIs.
- You don't adopt LangChain models, LangChain memory, or LangChain's orchestration model.

**Cons:**
- The PostgresSaver schema is designed for LangGraph's state shape (TypedDict, reducer-merged). Using it as a generic key-value-with-history store is fighting the abstraction.
- You inherit LangChain's version churn for a single primitive you could have written yourself.
- "Custom plus a tiny bit of framework" is the worst of both worlds: the framework gives you part of the API surface, but you still need to understand the rest of LangGraph to debug the framework you depend on.

**Complexity:** Medium-high (custom plus a framework dependency to learn).
**Debugging experience:** Worse than pure custom — the durability layer is opaque.
**Lock-in risk:** Lower than Option B, higher than Option A.
**Fit for Pipewright:** Marginal. Saves perhaps two weeks of work in exchange for a permanent dependency.
**What you gain:** A small amount of plumbing.
**What you lose:** Some clarity and independence.

### 4.4 Summary table

| Property | A: Custom | B: LangGraph end-to-end | C: Hybrid |
|----------|-----------|--------------------------|-----------|
| Build cost | High | Low | Medium |
| Ongoing maintenance | Medium | Medium-high (framework churn) | Medium |
| Debuggability | Excellent | Mixed | Good |
| Fit with LLM-M1 abstraction | Perfect | Conflicts | Compatible |
| Fit with Memory-M1 design | Perfect | Conflicts | Compatible |
| Fit with approval gate model | Perfect | Workable (`interrupt()`) | N/A |
| Lock-in risk | None | High | Medium |
| Time to durable beta | 6-10 weeks | 3-5 weeks | 4-7 weeks |
| Right answer for Pipewright? | **Yes** | No | Marginal |

---

## 5. LangChain / LangGraph — Honest Evaluation

### 5.1 Should Pipewright use LangChain?

**No.** Not even partially.

LangChain's value proposition is "shared abstractions across LLM providers." Pipewright already has its own clean provider abstraction (LLM-M1), with a domain-specific shape that includes things LangChain doesn't model well: per-role retry policy, capability metadata, secret redaction, snapshot-bound model identity. Adopting LangChain ChatModels means replacing a clean local abstraction with a busier shared one and inheriting LangChain's versioning.

The only LangChain piece worth a serious look is the loader/splitter utilities, and Pipewright doesn't need them — repo indexing is already custom.

### 5.2 Should Pipewright use LangGraph?

**Not now. Possibly evaluate later (Agent-R8).** Evaluation criteria: would later get specific about which custom primitive is causing pain.

LangGraph is more interesting than LangChain. Its core primitives — state graph, checkpointer, interrupt — match real concerns. But:

- The orchestration substrate duplicates work Pipewright has already done (chunked execution with checkpoints already exists).
- The interrupt primitive solves approval pause/resume, but Pipewright already has approval gates working — durably persisting them is a small extension, not a green-field problem.
- The checkpointer is the most genuinely useful piece, but it solves "save and restore opaque state" — Pipewright's state is not opaque, it's a clearly-typed domain model in DB tables. The mismatch is real.

### 5.3 What parts of LangGraph could be useful

| Piece | Useful for Pipewright? |
|-------|------------------------|
| PostgresSaver checkpointer | Marginal — Pipewright's state is already in DB tables; serializing it as a TypedDict for the saver adds steps. |
| `interrupt()` / human-in-the-loop | Useful pattern to study; reimplementing it on top of DB rows is straightforward. |
| Streaming events | Useful pattern; needs custom durable event log anyway. |
| Time travel / replay | Genuinely nice; can be added later via checkpoint history table. |
| State graph DSL | Not useful — Pipewright's state machine is small enough that explicit code is clearer than a graph. |
| Conditional edges | Not useful for the same reason. |
| Pre-built integrations | Pipewright has none of LangChain's input data shapes (no chat history, no documents-as-context). |

### 5.4 What to avoid

- LangChain memory classes (conflict with three-tier memory).
- LangChain output parsers (Pipewright already uses Pydantic).
- LangChain agents (the autonomous-loop pattern is the opposite of what Pipewright wants).
- LCEL (LangChain Expression Language) — adds learning cost for marginal gain.

### 5.5 Would LangGraph hide product logic?

Yes, somewhat. Pipewright's product logic is:
- Approval gates are mandatory.
- Reviewer differs from coder.
- Memory writes need human approval.
- Git pushes are idempotent.
- No merge without explicit human input.

None of those are LangGraph concerns. They live above and below the orchestration layer. Adopting LangGraph means the orchestration code mixes those rules into graph nodes, where they're less visible than they would be in domain services.

### 5.6 How approval gates fit LangGraph (if you did adopt it)

LangGraph's `interrupt()` would map approval gates to graph pauses. The pause/resume works durably with the PostgresSaver. The challenge is *what is paused*: in LangGraph, the entire graph node is paused. In Pipewright, the human reviews state from the DB and the worker is idle. Same outcome; different mental model.

### 5.7 Repo locks and PR logic in LangGraph

Repo locks are a process/cluster concern, not a graph concern. They'd live outside LangGraph regardless. PR creation is a side effect at a specific graph node. Idempotency-key handling is **not** a LangGraph concern and would be custom either way.

### 5.8 Multi-LLM role routing in LangGraph

Each role would be a separate graph node. LangChain's ChatModel would be the model abstraction inside the node — which conflicts with Pipewright's `Provider` abstraction. Resolving the conflict means either:
- Use LangChain ChatModels (give up LLM-M1's abstraction).
- Use Pipewright's `Provider` inside LangGraph nodes (mixed model).
- Adapt one to the other (extra layer for no real gain).

None of these is appealing.

### 5.9 Memory in LangGraph

LangChain memory classes are session-scoped chat memory. Pipewright's three-tier memory is project-scoped, role-aware, and human-gated. The two models don't compose. You'd ignore LangChain memory entirely.

### 5.10 What would make debugging harder

- Stack traces span LangGraph layers. A bug in your node logic shows up wrapped in machinery.
- State is serialized through the checkpointer; reading it requires LangGraph's tools, not plain SQL.
- Version churn: a LangGraph update can change checkpoint format and force a migration of in-flight runs.
- Mental model: maintainers must understand both Pipewright's domain and LangGraph's framework.

---

## 6. Recommendation

**Build custom. Don't adopt LangGraph now. Set an explicit tripwire to revisit at Agent-R8.**

Rationale:

1. Pipewright's domain model is already substantial and opinionated. Adopting a generic agent framework dilutes that.
2. The provider abstraction (LLM-M1) and memory architecture (Memory-M1) are deliberately designed and would be partly undone by LangChain/LangGraph adoption.
3. The realistic durability use cases (approval delays, process restart, provider rate limits) are well-served by Postgres + ARQ + idempotency keys. There's no novel agent infrastructure required.
4. Custom keeps debugging direct. For a product whose core value is auditability, opacity is a tax.
5. The cost of switching to LangGraph later (if we want it) is bounded; the cost of building on it now and switching away later is much worse.

**Tripwire for revisiting at Agent-R8:** if any of the following becomes painful in custom implementation, revisit:
- Replay-from-arbitrary-checkpoint becomes a real feature need (not just nice-to-have).
- We add multi-step branching / time-travel debugging.
- Approval interrupt semantics get complex enough that a framework would clearly help.
- A second team takes over and would benefit from a more standard substrate.

Until any of those triggers, stay custom.

---

## 7. Durable Run State Machine

The existing pipeline already implements a state machine implicitly through `pipeline_runs.status` and per-chunk `chunk_status.status`. Formalize it. The states below cover the union of run-level and chunk-level concerns; some apply only at the run level, some only at the chunk level, both noted.

### 7.1 States

| State | Scope | Kind | Notes |
|-------|-------|------|-------|
| `created` | run | transient | Row exists; preflight not yet run. |
| `preflight_running` | run | transient | LLM/memory/git preflight in progress. |
| `preflight_failed` | run | terminal | Config invalid; never started. |
| `triaging` | run | transient | Chunk plan being generated. |
| `awaiting_chunk_plan_approval` | run | **paused / interrupt** | Indefinite wait. |
| `chunk_plan_rejected` | run | terminal | Human rejected. |
| `chunk_plan_approved` | run | transient | Brief — immediately transitions to executing. |
| `executing` | run | active | Worker actively running chunks. |
| `chunk_planning` | chunk | active | Per-chunk planner running. |
| `chunk_coding` | chunk | active | Per-chunk coder running. |
| `chunk_patching` | chunk | active | Patch being applied to working tree. |
| `chunk_testing` | chunk | active | Test command running. |
| `chunk_reviewing` | chunk | active | Reviewer running. |
| `awaiting_high_risk_approval` | chunk | **paused / interrupt** | Indefinite wait. |
| `chunk_completed` | chunk | terminal (per chunk) | Committed; next chunk can start. |
| `chunk_rejected` | chunk | terminal (per chunk) | Triggers rollback; usually terminates run. |
| `chunk_failed` | chunk | terminal (per chunk) | Tests failed or unrecoverable error. |
| `awaiting_final_approval` | run | **paused / interrupt** | All chunks done, awaiting human final review. |
| `final_approved` | run | transient | Briefly held before push. |
| `final_rejected` | run | terminal | Human rejected final diff. |
| `pushing` | run | active | Pushing branch to remote. |
| `creating_pr` | run | active | Calling GitHub API. |
| `completed` | run | terminal | PR created. |
| `failed` | run | terminal | Unrecoverable error. |
| `cancelled` | run | terminal | Human cancelled. |
| `paused_provider_quota` | run | **paused / waiting** | Provider rate-limited beyond retry budget. |
| `paused_for_human` | run | **paused / interrupt** | Generic catch-all for unanticipated approval needs. |
| `needs_human_intervention` | run | **paused / escalated** | System detected an unsafe condition; will not retry without human. |

### 7.2 Allowed transitions (subset; representative)

```
created → preflight_running → triaging → awaiting_chunk_plan_approval
                              ↓
                              preflight_failed (terminal)

awaiting_chunk_plan_approval → chunk_plan_approved → executing
                            → chunk_plan_rejected (terminal)

executing (per chunk):
    chunk_planning → chunk_coding → chunk_patching → chunk_testing → chunk_reviewing
                                                            ↓
                                                  awaiting_high_risk_approval → chunk_completed
                                                            ↓                  ↘
                                                       chunk_rejected (terminal)   chunk_failed (terminal)
                                                            
all chunks done → awaiting_final_approval → final_approved → pushing → creating_pr → completed
                                          → final_rejected (terminal)

any active state → paused_provider_quota → resumes when quota window passes → original state
any active state → paused_for_human (rare, recoverable)
any active state → needs_human_intervention (recoverable only via human)
any active state → cancelled (terminal)
any active state → failed (terminal)
```

### 7.3 Classification

- **Retryable states (worker may pick up after crash):** all `*_running`, `executing`, `chunk_*` active states, `pushing`, `creating_pr`. On worker crash, the state row is unchanged; a fresh worker enters the same state via the resume path.
- **Paused states (do not retry, wait for input):** `awaiting_*`, `paused_*`, `needs_human_intervention`.
- **Terminal states (no further action):** `completed`, `failed`, `cancelled`, `chunk_plan_rejected`, `final_rejected`, `preflight_failed`.

### 7.4 What happens on resume

1. Worker picks up a run in a retryable state (or is told to via API).
2. Worker acquires the project-repo lock (DB-backed, see section 12).
3. Worker validates git state: branch exists, last commit hash matches the expected checkpoint hash, working tree is clean.
4. Worker resolves "where were we" from the run row + checkpoints + chunk statuses (the same resolution the existing orchestrator already does).
5. Worker proceeds from the highest verifiable checkpoint, not from the recorded state — because the recorded state could be a write that happened *just before* the crash that wasn't actually completed. Trust checkpoints, not status text.

### 7.5 What state is persisted

Everything. There is no in-memory state that survives a crash by design. The orchestrator's behavior on resume is fully determined by:
- The run row.
- All chunk rows.
- All checkpoint rows.
- The LLM config snapshot (immutable).
- The memory snapshot (immutable).
- The current git HEAD on the worktree.

Anything not in that list is either ephemeral (logs are best-effort to reconstruct) or wrong to depend on.

---

## 8. Durable Checkpoint Design

The existing checkpoint table is per-chunk-per-step. Extend, don't replace.

### 8.1 What to checkpoint and what to store

| Checkpoint | Stored | Never stored |
|-----------|--------|---------------|
| Run start | Run config snapshot, memory snapshot, project state, base git HEAD on target repo | API keys, raw secret state |
| Chunk plan approved | The approved plan JSON, approver, timestamp, chunk count | — |
| Planner output (per chunk) | `PlannerHandoff` JSON, provider/model used, token usage, latency | Raw prompt text (re-derivable; redact) |
| Architect output (per chunk, if used) | `ArchitectHandoff` JSON, provider/model used, token usage, latency | Same |
| Coder patch generated (per chunk) | `CoderHandoff` JSON, provider/model used, token usage, latency | Raw prompt text |
| Patch applied (per chunk) | Files written, working-tree state hash, rollback manifest path | Raw file contents (already in git) |
| Tests passed (per chunk) | Test command exit code, stdout/stderr truncated to N KB, duration | Full stdout if huge — truncate |
| Reviewer output (per chunk) | `ReviewerHandoff` JSON, provider/model used, token usage, issues found | — |
| High-risk human approval (per chunk) | Approver, decision, timestamp, optional rejection reason | — |
| Commit created (per chunk) | Git commit hash, files in commit, parent hash | — |
| Final human approval | Approver, decision, timestamp | — |
| PR created | PR URL, PR number, branch name, head SHA at push, idempotency key used | GitHub API token |

### 8.2 Idempotency key strategy

Every external side effect carries an idempotency key. The key is deterministic from `(run_id, chunk_number, step, attempt)` or for some operations `(run_id, step)`. Examples:

| Operation | Idempotency key |
|-----------|------------------|
| Apply patch for chunk N | `patch:{run_id}:{chunk_number}` |
| Commit for chunk N | `commit:{run_id}:{chunk_number}` |
| Push branch | `push:{run_id}` |
| Create PR | `pr:{run_id}` |
| LLM call | `llm:{run_id}:{chunk_number}:{role}:{attempt_number}` |

Before invoking the side effect, check whether an effect with that key has already been recorded as completed. If yes, return the recorded result. If no, perform and record atomically (insert idempotency row + record outcome in same transaction).

### 8.3 Git commit hash / working-tree hash in checkpoints

The existing resume path validates git commit hash already. Extend to include:
- Working-tree hash (SHA-256 over a sorted list of tracked file paths and content hashes) — catches the case where the repo was tampered with outside Pipewright.
- Branch name expected to be checked out.

If validation fails on resume, the run transitions to `needs_human_intervention`. Never auto-resume from an unknown git state.

### 8.4 Provider/model snapshot in checkpoints

Each checkpoint records the `(provider, model)` actually used for that step. The run-level snapshot says what was *configured*; the checkpoint records what was *used*. They should match unless fallback was triggered (LLM-M2). Mismatches are surfaced in the UI.

### 8.5 Memory snapshot in checkpoints

The memory block injected at each step is recorded as part of the checkpoint (or referenced by a snapshot ID). This is what lets debugging answer "what did the model see when it produced this output?"

### 8.6 How to avoid resuming from a bad state

A checkpoint is considered *good* only if:
1. `tests_passed = true` (existing rule, kept).
2. Git commit hash referenced by the checkpoint resolves and matches.
3. Working-tree hash matches the post-step hash recorded at checkpoint time.
4. No subsequent checkpoint exists that contradicts it (e.g., a later "failed" checkpoint).

A checkpoint that fails any check is treated as nonexistent. The run falls back to the previous good checkpoint, or to "no checkpoint" if none exists.

### 8.7 Append-only

Checkpoint rows are never updated, only inserted. To "supersede" a checkpoint, insert a new one with a later timestamp. This makes audit, debugging, and replay possible.

---

## 9. Worker Queue Design

### 9.1 API process vs worker process

| Concern | API process | Worker process |
|---------|-------------|------------------|
| Handles HTTP | Yes | No |
| Handles WebSocket | Yes | No |
| Enqueues jobs | Yes | No |
| Reads run state for UI | Yes | No |
| Executes pipeline stages | **No (after R2)** | Yes |
| Holds repo lock | No (after R4) | Yes |
| Writes checkpoints | No | Yes |
| Publishes events | Indirect (reads DB) | Yes |

The cutover from "API does everything" to "API enqueues, worker executes" is the **single highest-risk milestone**, because it changes how every existing debugging instinct works. Schedule accordingly.

### 9.2 Choosing the queue: ARQ vs Celery vs Cloud Tasks

| Option | Pros | Cons |
|--------|------|------|
| **ARQ** (Redis-backed, async-native) | Async-first, lightweight, modern Python idioms. Small surface area. | Smaller community than Celery. Redis dependency. |
| Celery | Battle-tested, huge community, supports many brokers. | Heavier; partly sync-oriented; configuration is famously fiddly. |
| Cloud Tasks (GCP) | Fully managed; no Redis. Strong at-least-once semantics. | Cloud lock-in; HTTP-call model is awkward for long-running tasks. |
| Postgres-backed (e.g., `pg-queue`, custom `SELECT ... FOR UPDATE SKIP LOCKED`) | One fewer service. Already running Postgres. | DIY; LISTEN/NOTIFY is fine but you own the polling. |

**Recommendation: ARQ (Redis).** Async-native fits the existing FastAPI/asyncio code. Lightweight enough that the team can understand it end-to-end. Redis adds one service to deploy but is straightforward and well-understood.

**Strong second choice:** Postgres-backed queue using `SELECT ... FOR UPDATE SKIP LOCKED`. Eliminates Redis. Worth considering if the deployment story strongly favors fewer services. Slightly more code to write.

### 9.3 Job payload design

Keep job payloads small and idempotent. Job carries identifiers, not data. Data lives in DB.

```python
@dataclass(frozen=True)
class ExecuteRunJob:
    run_id: str
    enqueued_at: datetime
    enqueue_reason: Literal["initial", "resume", "approval_resume", "provider_quota_retry"]
    idempotency_key: str        # f"execute:{run_id}:{enqueue_reason}:{timestamp_ms}"
    schema_version: int = 1
```

The job is "process this run." The worker reads current run state from DB and picks up wherever it should resume. Job payloads do not encode chunk numbers or step names — those are derived from current state.

### 9.4 Job idempotency

Two layers:

1. **Queue-level deduplication:** before enqueueing, check whether a non-terminal job for the same `run_id` exists. If yes, skip enqueue, log "already queued."
2. **Worker-level idempotency:** when a worker picks up a job, it acquires the run-level lock (DB advisory lock keyed on `run_id`). Two workers trying to process the same run results in one acquiring the lock and the other returning immediately.

The combination is at-least-once delivery + serialized execution. Duplicate jobs are harmless.

### 9.5 Retry policy

| Failure | Worker action |
|---------|----------------|
| Provider transient (rate limit, timeout, 5xx) | Worker pauses run to `paused_provider_quota` with a `next_attempt_after` timestamp. Scheduler re-enqueues the run at that time. Do **not** keep the worker busy waiting. |
| Provider auth | Run transitions to `failed`. No retry. |
| DB transient | Worker retries with backoff up to 3 times within the job, then leaves the job for the queue's own retry (capped). |
| Git error | Run transitions to `needs_human_intervention`. No auto-retry. |
| Pydantic validation after correction prompt | Run transitions to `failed`. |
| Unknown exception | Run transitions to `failed`. Worker logs full stack trace. |

ARQ supports per-job retry counts; configure to small numbers (≤3) for transient errors and rely on the application-level pause-and-reschedule for provider quota.

### 9.6 Dead-letter queue

Jobs that fail more than the retry budget go to a dead-letter location. In Postgres-backed model, a `worker_jobs` row with `status='dead_letter'`. In ARQ, the equivalent is a failure queue + alerting. **A dead-lettered job does not put the run in a weird state** — the run is already in a terminal state by then. The DLQ is for operator forensics.

### 9.7 Cancellation

Two cancellation kinds:
- **Soft cancel:** mark run `cancelled` in DB. Worker checks at each safe point (between chunks, between steps). On next check, worker exits cleanly.
- **Hard cancel:** rare; only via operator action. Worker is killed. State is what's in DB; run is `cancelled`.

A cancelled run never resumes. A new run with the same feature can be created.

### 9.8 Progress events

See section 11 for the durable event log. Workers emit events to a `run_events` table (and to a Redis pub-sub channel for live streaming). The DB write is the source of truth; the Redis push is best-effort live delivery.

### 9.9 Worker crash recovery

On worker crash mid-job, the queue's heartbeat/lease mechanism (both ARQ and Postgres-with-SKIP-LOCKED handle this) marks the job available again. A new worker picks it up. The new worker enters the same resume path as a manual resume — read DB, validate git, proceed from last checkpoint.

### 9.10 Exactly-once vs at-least-once

**At-least-once with idempotent side effects.** Exactly-once is not achievable when external side effects exist (GitHub PR creation). The idempotency keys in section 8.2 are the practical answer.

### 9.11 Preventing duplicate chunk execution

Three layers of defense:
1. Queue-level dedup (section 9.4).
2. Run-level DB lock (`pg_advisory_xact_lock` on `run_id`).
3. Per-chunk idempotency: before executing chunk N, check whether a `chunk_completed` checkpoint already exists. If yes, skip.

Defense in depth because the worst case (duplicate PR creation, duplicate commit) is severe.

---

## 10. Database Design (PostgreSQL + Alembic)

Reproducing the existing tables with appropriate field changes for Postgres, plus the new tables required by the durable runtime.

### 10.1 `runs` (replaces `pipeline_runs`)

```sql
CREATE TABLE runs (
    id                       UUID PRIMARY KEY,
    project_id               UUID NOT NULL REFERENCES projects(id),
    feature_description      TEXT NOT NULL,
    status                   TEXT NOT NULL,                      -- see section 7.1
    status_reason            TEXT,
    branch_name              TEXT,
    base_branch              TEXT,
    base_commit_hash         TEXT,                                -- HEAD at run start
    pr_url                   TEXT,
    pr_number                INTEGER,
    pushed_at                TIMESTAMPTZ,
    pr_created_at            TIMESTAMPTZ,
    llm_config_snapshot      JSONB NOT NULL,                      -- from LLM-M1
    memory_snapshot_id       UUID NOT NULL REFERENCES memory_snapshots(id),
    preflight_report         JSONB,
    next_attempt_after       TIMESTAMPTZ,                         -- for paused_provider_quota
    cancelled_by             TEXT,
    cancelled_at             TIMESTAMPTZ,
    created_at               TIMESTAMPTZ DEFAULT now(),
    updated_at               TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ix_runs_project_status ON runs(project_id, status);
CREATE INDEX ix_runs_status_next_attempt ON runs(status, next_attempt_after);
```

### 10.2 `chunks`

```sql
CREATE TABLE chunks (
    id                  UUID PRIMARY KEY,
    run_id              UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    chunk_number        INTEGER NOT NULL,
    title               TEXT NOT NULL,
    requires_human_review BOOLEAN NOT NULL DEFAULT false,
    estimated_files     INTEGER,
    estimated_tokens    INTEGER,
    status              TEXT NOT NULL,                            -- see section 7.1
    status_reason       TEXT,
    commit_hash         TEXT,                                     -- set when chunk_completed
    files_touched       JSONB DEFAULT '[]'::jsonb,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE(run_id, chunk_number)
);

CREATE INDEX ix_chunks_run_status ON chunks(run_id, status);
```

### 10.3 `checkpoints`

```sql
CREATE TABLE checkpoints (
    id                  UUID PRIMARY KEY,
    run_id              UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    chunk_number        INTEGER,
    step                TEXT NOT NULL,                            -- plan|code|patch|test|review|commit|push|pr
    status              TEXT NOT NULL,                            -- success | failed
    output              JSONB,                                    -- step output (handoff, etc)
    provider            TEXT,
    model               TEXT,
    git_commit_hash     TEXT,
    working_tree_hash   TEXT,
    tests_passed        BOOLEAN,
    tokens_input        INTEGER,
    tokens_output       INTEGER,
    latency_ms          INTEGER,
    idempotency_key     TEXT NOT NULL UNIQUE,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ix_checkpoints_run_chunk_step ON checkpoints(run_id, chunk_number, step);
```

Append-only by convention. Enforce via grants (no `UPDATE` privilege on this table for the application role).

### 10.4 `approvals`

```sql
CREATE TABLE approvals (
    id                  UUID PRIMARY KEY,
    run_id              UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    chunk_number        INTEGER,
    approval_type       TEXT NOT NULL,                            -- chunk_plan | high_risk_chunk | final
    status              TEXT NOT NULL,                            -- pending | approved | rejected | timed_out | cancelled
    risk_level          TEXT,
    summary             TEXT,
    plain_english_summary TEXT,
    diff_preview        TEXT,
    decided_by          TEXT,
    decided_at          TIMESTAMPTZ,
    rejection_reason    TEXT,
    expires_at          TIMESTAMPTZ,                              -- optional approval timeout (long, e.g., 7 days)
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ix_approvals_run_status ON approvals(run_id, status);
CREATE INDEX ix_approvals_pending ON approvals(status) WHERE status = 'pending';
```

### 10.5 `repo_locks` (DB-backed locks)

```sql
CREATE TABLE repo_locks (
    project_id          UUID PRIMARY KEY REFERENCES projects(id),
    locked_by_run_id    UUID NOT NULL REFERENCES runs(id),
    locked_by_worker_id TEXT NOT NULL,
    acquired_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL                       -- acquired_at + lease_duration
);
```

See section 12 for semantics.

### 10.6 `llm_calls` (from LLM-M1; carries over)

Already designed in the LLM doc. Add `worker_id` and `job_attempt_id` columns to attribute calls to specific worker executions.

### 10.7 `memory_snapshots`

```sql
CREATE TABLE memory_snapshots (
    id                  UUID PRIMARY KEY,
    project_id          UUID NOT NULL REFERENCES projects(id),
    snapshot            JSONB NOT NULL,                           -- the active memory at this snapshot
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ix_memory_snapshots_project ON memory_snapshots(project_id);
```

One row per run. Run row references it via `memory_snapshot_id`. Immutable.

### 10.8 `run_events` (durable event log)

```sql
CREATE TABLE run_events (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    chunk_number        INTEGER,
    sequence_number     BIGINT NOT NULL,                          -- monotonic per run
    event_type          TEXT NOT NULL,
    severity            TEXT NOT NULL,                            -- debug | info | warn | error
    payload             JSONB NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE(run_id, sequence_number)
);

CREATE INDEX ix_run_events_run_seq ON run_events(run_id, sequence_number);
CREATE INDEX ix_run_events_type ON run_events(event_type);
```

### 10.9 `worker_jobs`

```sql
CREATE TABLE worker_jobs (
    id                  UUID PRIMARY KEY,
    run_id              UUID NOT NULL REFERENCES runs(id),
    job_type            TEXT NOT NULL,                            -- execute_run | retry_provider_quota | ...
    idempotency_key     TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL,                            -- queued | running | succeeded | failed | dead_letter | cancelled
    attempt_number      INTEGER NOT NULL DEFAULT 0,
    worker_id           TEXT,
    enqueued_at         TIMESTAMPTZ DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    last_error          TEXT,
    next_attempt_after  TIMESTAMPTZ
);

CREATE INDEX ix_worker_jobs_status_next ON worker_jobs(status, next_attempt_after);
CREATE INDEX ix_worker_jobs_run ON worker_jobs(run_id);
```

### 10.10 `pr_records`

```sql
CREATE TABLE pr_records (
    id                  UUID PRIMARY KEY,
    run_id              UUID NOT NULL REFERENCES runs(id) UNIQUE, -- one PR per run, enforced
    idempotency_key     TEXT NOT NULL UNIQUE,
    pr_url              TEXT NOT NULL,
    pr_number           INTEGER NOT NULL,
    branch_name         TEXT NOT NULL,
    head_sha_at_push    TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT now()
);
```

The `UNIQUE` on `run_id` is the database-level guarantee that one run can never have two PRs. The idempotency key is the operational layer; the unique constraint is the absolute defense.

---

## 11. Durable Events / Logs

Current live logs are in-memory. Replace with a durable event log fronted by a real-time push.

### 11.1 Event schema

```python
class RunEvent(BaseModel):
    id: int                              # BIGSERIAL
    run_id: str
    chunk_number: int | None
    sequence_number: int                 # monotonic per run
    event_type: EventType                # see section 11.2
    severity: Literal["debug","info","warn","error"]
    payload: dict
    created_at: datetime
```

### 11.2 Event types (initial set)

| Event type | Triggered by |
|------------|---------------|
| `run.created` | New run row inserted |
| `run.preflight.started` / `.passed` / `.failed` | Preflight |
| `run.status.changed` | Any run.status transition |
| `chunk.status.changed` | Any chunk.status transition |
| `llm.call.started` / `.succeeded` / `.failed` | Provider abstraction |
| `patch.applied` / `.rolled_back` | Patch applier |
| `tests.started` / `.passed` / `.failed` | Test runner |
| `approval.requested` / `.granted` / `.rejected` | Approval flow |
| `git.commit.created` | Per chunk commit |
| `git.push.started` / `.completed` | Push |
| `pr.create.started` / `.succeeded` / `.failed` | GitHub |
| `worker.heartbeat` | Worker lease renewal |
| `worker.acquired` / `.released` | Worker pickup |
| `memory.snapshot.captured` | Run start |

### 11.3 Ordering

`sequence_number` is a per-run monotonic counter, assigned at write time inside a transaction that locks the run row. Guarantees strict ordering per run. Across runs, ordering is not guaranteed (and not needed).

### 11.4 Replay

UI requests `GET /runs/{run_id}/events?after_sequence=N` and gets all events with `sequence_number > N`. Reconnect after browser close: fetch all events, render history, then subscribe to live.

### 11.5 WebSocket streaming + polling fallback

- **Primary:** worker writes to `run_events` (durable) and pushes to Redis pub-sub channel `run:{run_id}` (best-effort). API process subscribes and forwards to WebSocket clients.
- **Fallback:** UI polls `GET /runs/{run_id}/events?after_sequence=N` every few seconds when WS is disconnected.

The durable record is the source of truth. The pub-sub is a latency optimization.

### 11.6 Retention

Events are retained for 90 days, then archived to cheap storage (or a separate `run_events_archive` table). Operationally, you almost never look at events older than a week.

### 11.7 Redaction

Events go through the secret regex set before insertion. The payload column is JSONB; the redactor walks values and replaces matches with `[REDACTED:type]`. Never log raw provider responses or API keys.

### 11.8 UI reconstruction after refresh/restart

The UI never assumes state from memory. On run-detail mount:
1. Fetch run row.
2. Fetch chunks, approvals, checkpoints (summary only).
3. Fetch events with `after_sequence=0` (or last seen).
4. Subscribe to WebSocket for new events.

The page renders identically whether it's the first visit, a refresh after the run completed, or a reconnect mid-run.

---

## 12. Repo Locking and Concurrency

The in-process repo lock cannot survive process boundaries. Replace with a DB-backed lock.

### 12.1 Lock granularity

Lock is at `(project_id)`. One project, one repo, one mutation at a time. Branch-level locks are not needed in M1 of the durable runtime because Pipewright creates one `pipewright/{run_id[:8]}` branch per run and never touches user branches.

### 12.2 Implementation

Use a row in `repo_locks` plus a Postgres advisory lock:

```sql
-- Acquire (transaction):
INSERT INTO repo_locks (project_id, locked_by_run_id, locked_by_worker_id, expires_at)
    VALUES ($1, $2, $3, now() + interval '5 minutes')
    ON CONFLICT (project_id) DO UPDATE
       SET locked_by_run_id = EXCLUDED.locked_by_run_id,
           locked_by_worker_id = EXCLUDED.locked_by_worker_id,
           acquired_at = now(),
           last_heartbeat_at = now(),
           expires_at = now() + interval '5 minutes'
    WHERE repo_locks.expires_at < now()                          -- only steal expired locks
       OR repo_locks.locked_by_run_id = EXCLUDED.locked_by_run_id; -- or reacquire same run

-- Heartbeat (every 60 seconds while holding):
UPDATE repo_locks
   SET last_heartbeat_at = now(),
       expires_at = now() + interval '5 minutes'
 WHERE project_id = $1 AND locked_by_worker_id = $2;

-- Release:
DELETE FROM repo_locks
 WHERE project_id = $1 AND locked_by_worker_id = $2;
```

### 12.3 Lease TTL

5-minute lease, 1-minute heartbeat. If the worker crashes, the lock expires within 5 minutes and another worker can acquire it.

### 12.4 Stale lock cleanup

The `WHERE expires_at < now()` in the upsert *is* the cleanup. No background job is needed. Locks that look "stuck" in the UI but past their TTL are not actually stuck — the next acquire steals them.

### 12.5 Lock owner

`locked_by_worker_id` is a worker identifier (hostname + pid + uuid). `locked_by_run_id` is the run that "owns" the work. The combination means: this worker is acting on behalf of this run. A worker restart picks a new `worker_id`; the run id stays stable. The reacquire path (`locked_by_run_id = EXCLUDED.locked_by_run_id`) lets the new worker take over its own run's lock without waiting for expiry.

### 12.6 Distributed workers

The DB-backed lock works across processes by definition. As workers scale out (someday), they all see the same `repo_locks` table. No further design needed.

### 12.7 Preventing two workers from mutating the same repo

A worker **must** hold the lock before doing any git mutation, any patch apply, any push. The orchestrator enters the lock at the top of the execute path and releases at the bottom (or on exception). Lock acquisition failure means "another worker is on this project; try again later."

### 12.8 The `repo_locks` row is operationally visible

Always-visible "current lock owner" data lets the UI show "Run X has the lock; estimated completion in N seconds based on heartbeat." Useful for the operator.

---

## 13. Git Safety and Idempotency

The most failure-prone part of the system. Each of these is its own discipline.

### 13.1 Duplicate branches

Branch name is deterministic: `pipewright/{run_id[:8]}`. Already in the code. Resume checks that the branch exists and is on the expected commit before doing anything. Two runs cannot collide because their run IDs differ.

### 13.2 Duplicate commits

Each commit is "for chunk N." Before committing, check whether a checkpoint with `step='commit'`, `chunk_number=N`, `status='success'` exists for the run. If yes, skip — the commit was already made. The recorded commit hash is the source of truth; verify the branch is at or descended from it.

### 13.3 Duplicate PRs

Three layers:
1. `pr_records` has `UNIQUE(run_id)`. Database refuses a second PR for the same run.
2. Before calling GitHub, check whether a `pr_records` row exists for this run. If yes, return the existing PR URL.
3. The GitHub API call uses an idempotency-key header where supported, or a "look for existing PR by branch" GET before POST.

### 13.4 Pushing stale code

Before push, verify:
- Current branch is `pipewright/{run_id[:8]}`.
- `HEAD` matches the commit hash from the last successful chunk's `commit` checkpoint.
- All chunks are in `chunk_completed` status.

If any check fails, do not push. Transition to `needs_human_intervention`.

### 13.5 Resuming after local working tree changed

`_validate_target_repo` already does some of this; extend to:
- Compare current `git status --porcelain` output to the expected state from the last checkpoint's `working_tree_hash`.
- If the working tree was modified outside Pipewright (e.g., user touched files), do not resume. Transition to `needs_human_intervention`.

### 13.6 Applying patch twice

Before applying a patch, check the corresponding `checkpoint` row. If `step='patch', chunk_number=N, status='success'` exists, skip. The rollback manifest (already in code) is the source of truth for "did this patch apply."

### 13.7 Creating PR twice after retry

Covered by section 13.3 plus the unique constraint plus the pre-call existence check.

### 13.8 Accidental merge

Pipewright never merges. The codebase does not call any GitHub API that merges. This is a property of the code, not a runtime guard. Document it as a never-do-this rule and enforce by code review.

### 13.9 Idempotency-key strategy summary

Idempotency keys are stored in the table that records the operation's outcome. Same key + already-completed = return cached result. Same key + in-progress = wait or fail (configurable per operation). Different key = new operation.

Construction of keys is deterministic (no random component) so that a retried call generates the same key.

---

## 14. Human Approval Pause / Resume

Approval is the most-tested durability case in practice — it's where real runs spend most of their wall-clock time.

### 14.1 Web approval

Already exists in the current UI. Durable runtime change: the approval is recorded in the `approvals` table; the run row's status reflects the pending approval. On approval, the run transitions and a new worker job is enqueued (`enqueue_reason='approval_resume'`).

### 14.2 Slack/email approval (future, not now)

Same `approvals` table. New approval channels are new ways to record a decision; the schema is shared. The run does not care which channel approved it.

### 14.3 Approval token expiry

For Slack/email later: approval links carry a signed token with an expiry (default 7 days). Expired tokens cannot approve. The pending approval can be re-issued from the web UI.

### 14.4 User session expiry during web approval

User session is tangential to the run. A user who logs back in sees the same pending approvals. Approval is by user identity, not by session.

### 14.5 Approval while worker is stopped

This is the normal case: no worker is running while a run is paused. The worker spawns when approval arrives (re-enqueue).

### 14.6 Rejection

Run transitions to a terminal state (`chunk_plan_rejected`, `final_rejected`). Patch is rolled back via existing `rollback_patch` flow. **A rejected run does not write long-term memory.** (Memory-M1 safety rule.)

### 14.7 Edit-and-resume

For chunk plan: human can edit the plan before approving. Edit + approve writes a new chunk plan and creates the approval row. This is already partly possible in the existing flow; durable runtime makes it explicit.

For diff / coder output: M1 of the durable runtime does **not** support editing the coder's output mid-run. Either approve, or reject and start over. Editing model output sounds nice but introduces enormous resume complexity (was the human's edit checkpointed? does the reviewer see the human's edit or the model's?). Defer.

### 14.8 Timeout

Approvals have an `expires_at` (default 7 days). When reached, the approval transitions to `timed_out` and the run transitions to a paused-with-timeout state. **The run is not auto-rejected.** Timeout is a notification mechanism, not a decision.

### 14.9 Audit trail

`approvals` table records who decided what when. Combined with `run_events`, the full sequence is reconstructable: when the approval was requested, who saw it, when they decided, what they said. This is the product's audit moat — make sure it's queryable in the UI.

---

## 15. Token / Provider / Context Handling

"Token expiry" means different things; address each.

### 15.1 What "token expiry" means

| Meaning | Maps to |
|---------|---------|
| User session token expires | Approval flow handles via section 14.4 |
| Provider API key revoked/expired | Provider abstraction emits `ProviderAuthError`; run fails (not auto-retry); operator updates key |
| Context window overflow | Pre-flight rejection if estimable; runtime detection per call |
| Rate limit (quota) | `paused_provider_quota` state with `next_attempt_after`; scheduler re-enqueues |
| Long task continuation | Not a thing in Pipewright — there are no multi-hour LLM calls; if one happens, treat as timeout |

### 15.2 Chunking and compaction

Chunking is already done at the planner/triage level. Compaction (summarizing earlier context to fit) is **not** part of M1 of the durable runtime. If a chunk's prompt exceeds the model's window:
1. Pre-flight catches it where possible (estimated input tokens vs `context_window`).
2. At runtime, the provider returns an error; the run transitions to `needs_human_intervention` with the message "Chunk N's prompt is too large for the configured model. Either re-chunk the feature or use a larger-context model."

Auto-compaction is feasible (summarize older chunks' handoffs), but it's complexity that masks a real signal. The first instinct should be "re-chunk smaller," not "summarize more aggressively."

### 15.3 Memory snapshot

The memory snapshot is captured at run start and never changes for the run (see Memory-M1 doc). This means a 36-hour-paused run resumes with the memory it had at start, not whatever has accumulated since. Documented and intentional.

### 15.4 Retry semantics

| Failure | Retry strategy |
|---------|-----------------|
| Provider timeout | Provider-layer retry (1 attempt); if still fails, transition to `paused_provider_quota` with 30s `next_attempt_after`. |
| Provider rate limit | Honor `Retry-After`; if absent, exponential backoff. After 2 attempts within job, transition to `paused_provider_quota` with longer `next_attempt_after` (5-60 min). |
| Provider 5xx | 2 attempts with backoff; then `paused_provider_quota`. |
| Provider auth | No retry. Run fails. |

### 15.5 Continuation from checkpoint

The worker, on every resume, treats the situation as if the run is starting fresh except for what's in the checkpoint trail. There is no "in-flight call to continue." A call either completed (checkpoint exists) or it didn't (and we redo it).

### 15.6 Fail-safe human escalation

The `needs_human_intervention` state is the catch-all for "the runtime knows something is wrong and won't proceed without a person." Use it for:
- Git state mismatch on resume.
- Model output that violated path constraints (path-traversal attempt).
- Repeated provider failures that exceed the retry budget.
- Schema mismatch between job payload and current code (deploy happened mid-run).

Never silently auto-recover from any of these.

---

## 16. Memory Integration

The durable runtime intersects Memory-M1 at three points:

### 16.1 Memory snapshot at run start

Already designed in Memory-M1. The run row references `memory_snapshot_id`. Resume uses that snapshot. Project memory changes during the run do not affect the in-flight run.

### 16.2 Memory suggestions after a successful run

Per Memory-M1: the post-PR hook persists `suggested_memory_entries` from the planner/architect/reviewer handoffs into `memory_suggestions` with `status='pending'`. The durable runtime change: this write happens *inside the worker* (post-PR), not from the API process. The worker holds the lock until this completes, then releases.

### 16.3 Memory poisoning protection

Per Memory-M1 section 5/section 6:
- Failed runs do not write suggestions.
- Rejected chunks do not produce suggestions.
- All suggestions pass secret/PII validators.
- Promotion to long-term memory requires human approval.

The durable runtime adds: **memory writes are scoped by the worker job's run state.** A worker for a `failed` run never enters the suggestion-write path. The worker checks final run state before persisting.

### 16.4 Memory changing mid-run

Cannot happen by construction. The snapshot is immutable.

### 16.5 Context overflow from memory

Memory-M1 hard-caps the memory block at 1500 tokens. The cap is enforced at injection time. Combined with chunked context windows, memory cannot cause overflow on its own.

---

## 17. Multi-LLM Integration

The durable runtime intersects LLM-M1 at multiple points:

### 17.1 Per-role model config

Configured at the project level (LLM-M1). Frozen into `runs.llm_config_snapshot` at run creation. The worker reads from the snapshot.

### 17.2 Run snapshot

Immutable. Resume reads from the snapshot, never from the project config. Identical rule to memory.

### 17.3 Provider fallback (later, LLM-M2)

When introduced, fallback is a worker-level concern, not a graph-level one. If primary provider for a role fails after the retry budget, worker consults the fallback chain. The fallback decision is recorded as a `llm.call.fallback_triggered` event with prominent severity.

### 17.4 Token usage audit

Every provider call writes an `llm_calls` row (LLM-M1). The durable runtime adds `worker_id` and `job_attempt_id` to attribute calls. UI can answer: "Which worker, in which attempt, made this call?"

### 17.5 Provider retries

Live in the provider abstraction, not in the worker. Worker sees either `LLMResponse` (success) or `LLMError` (final failure after provider's own retries). On final failure, worker decides whether the error is transient enough to pause-and-resume or terminal.

### 17.6 Reviewer differs from coder

Soft warning in LLM-M1 pre-flight. Durable runtime does not change this rule.

### 17.7 Auto-routing (LLM-M3)

Will produce different (provider, model) per chunk. Each chunk's actual choice is recorded in checkpoint rows. Resume uses the recorded choice, not the routing decision (which might have changed).

### 17.8 Model deprecation mid-run

The snapshot has the model name. The provider's adapter returns `ProviderModelDeprecatedError` if the model is gone. Run transitions to `failed` with a clear message. No silent fallback in M1.

---

## 18. Critical Failure Modes

Adversarial matrix. Each row is a real scenario.

| # | Scenario | Detection | Mitigation |
|---|----------|-----------|------------|
| 1 | API server restarts mid-run | Worker (separate process) continues. API process doesn't matter. | Decoupled by R2. |
| 2 | Worker crashes mid-chunk | Lock lease expires (~5 min). New worker acquires; resumes from last checkpoint. | DB-backed lock + lease. |
| 3 | Redis restarts | Job queue may lose in-flight jobs. ARQ persists state in Redis itself; restart could lose unacked jobs. | Workers detect job timeout via DB; re-enqueue. Also: prefer Postgres-backed queue if Redis durability is a concern. |
| 4 | Postgres temporary outage | All workers fail to read/write. | Workers retry DB ops with backoff; if outage persists, workers exit cleanly, jobs return to queue. |
| 5 | Provider returns malformed JSON | Provider abstraction raises `ProviderResponseInvalidError`. | Correction-prompt retry on same provider; if still bad, run fails. |
| 6 | Provider rate-limited for hours | `paused_provider_quota` with long `next_attempt_after`. UI shows the wait. | Scheduler re-enqueues at the time. Operator can override. |
| 7 | User closes browser | No effect on run. WebSocket disconnects; events still durable. | Run continues in worker; UI reconstructs on reopen. |
| 8 | Approval delayed overnight | Run is `awaiting_*`; no worker active. State is durable. | Approval arrives → enqueue resume job. |
| 9 | GitHub API fails after commit, before PR | Commit succeeded; `pr_records` not written. | Re-attempt PR creation uses idempotency key; checks for existing PR by branch first. |
| 10 | PR created but DB write fails | `pr_records` row missing. Retry will see the PR exists via GitHub query and reconcile by inserting the row. | Idempotency + reconciliation. |
| 11 | DB write succeeds but PR creation fails | Inverse: `pr_records` exists but PR doesn't on GitHub. | On retry, the idempotency-key-presence check reads `pr_records`, attempts to verify the PR via GitHub GET, and either confirms or recreates. |
| 12 | Patch applied but tests never finish (hang) | Test runner timeout. | Test runner has its own timeout (already in code); on timeout, treat as `tests.failed`. |
| 13 | Tests pass but checkpoint save fails | Worker raises; lock held; next attempt redoes tests. | Idempotent test runs are not strictly possible (timing varies) but the checkpoint write is the contract. |
| 14 | Checkpoint save succeeds, process crashes before next state | New worker reads latest checkpoint, sees the step done, proceeds. | Checkpoint-driven resume. |
| 15 | Stale repo lock | Lease expires; another worker steals. | TTL + steal-on-expired. |
| 16 | Duplicate worker picks same job | Both attempt run-level lock; one wins. The other returns immediately. | DB advisory lock on `run_id`. |
| 17 | Run cancelled while worker is executing | Worker checks `cancelled` at safe points (between steps). Acts on next check. | Cooperative cancellation. |
| 18 | User changes memory/config while run is paused | Snapshot is immutable; in-flight run keeps original config. Change takes effect next run. | Banner in UI. |
| 19 | Model output exceeds context window | Pre-flight catches estimable cases; runtime catches via provider error. | Transition to `needs_human_intervention`. |
| 20 | Large repo causes prompt overflow | Same as #19. Repo indexer truncates per file; chunk plan caps file count. | Already partly mitigated. Operator sees clear error. |
| 21 | Worker deploy happens during run | New worker code may have schema changes. | Job payload has `schema_version`; new worker refuses incompatible payloads, leaves them for human inspection. |
| 22 | Version mismatch between old job and new code | See #21. | `schema_version` check. |
| 23 | Approval token leaked | Token has limited scope + expiry. | Tokens are run-specific; leaked token can approve only that run, only once. |
| 24 | Malicious model output tries to escape allowed paths | `patch_applier` rejects out-of-scope paths. | Existing safeguard; covers this. |
| 25 | Prompt injection from repo file | Memory has structural separation (Memory-M1 section 7); reviewer cross-checks code vs memory. | Layered: structural, semantic, human approval. |
| 26 | Failed run tries to write memory | Worker checks final run status before persisting suggestions. | Memory safety rule. |
| 27 | Run resumed on different worker | Designed for. Worker_id is part of lock owner; reacquire allowed for same run. | DB lock reacquire. |
| 28 | Network partition between API and worker | API reads DB; worker writes DB. As long as DB is reachable, both work. | Postgres is the only required shared dependency. |

---

## 19. Safety Rules — Non-Negotiable

1. **Human approval gates remain mandatory.** No automation in the durable runtime can elide them.
2. **No merge without approval.** Pipewright never calls a GitHub merge endpoint.
3. **No checkpoint unless tests pass** (where applicable). Existing rule, preserved.
4. **Source code and tests beat model output.** Reviewer prompt enforces; patch applier enforces; durable runtime does not change.
5. **`project_id` scoping everywhere.** Every query that reads from project-scoped data filters by project. Memory, LLM config, locks, runs.
6. **No secrets in logs, events, or memory.** All persisted text goes through the secret redactor.
7. **Idempotency keys on every external side effect.** Side effects are: git commit, git push, GitHub PR, provider API call.
8. **Repo locks around all mutations.** Worker must hold the project lock before any git or filesystem write on the target repo.
9. **Resume must validate git state.** Any mismatch transitions to `needs_human_intervention`.
10. **Durable events are redacted at write time.** Not at read time.
11. **Failed or rejected runs do not write long-term memory.** Memory-M1 safety rule, enforced at the worker level.
12. **The run snapshot (LLM config + memory) is immutable for the run's life.** Resume must use it.
13. **Workers exit cleanly on DB outage.** No partial writes. No stuck states.
14. **`needs_human_intervention` never auto-resolves.** A human must inspect and decide.

---

## 20. Phased Roadmap

| Phase | Goal | Major dependencies |
|-------|------|---------------------|
| **Agent-R0** | This document. Design only. | None. |
| **Agent-R1** | PostgreSQL migration + Alembic baseline. Move all existing tables; preserve schema; data migration script for local dev. No behavior change. | Memory-M1, LLM-M1 must land first. |
| **Agent-R2** | Worker queue (ARQ + Redis or Postgres-backed). API enqueues; worker executes. Chunked orchestrator moves to worker. Live logs still in-memory (for now). | R1. |
| **Agent-R3** | Durable event log (`run_events`). Replace in-memory bus. WebSocket reads from DB + Redis pub-sub. UI replay on reconnect. | R2. |
| **Agent-R4** | DB-backed repo locks. Replace in-process `project_repo_lock`. Lease + heartbeat. | R2. |
| **Agent-R5** | Checkpoint hardening: working-tree hash, append-only enforcement, validation on resume, idempotency keys threaded through all side effects. | R2, R4. |
| **Agent-R6** | Approval pause/resume hardening: durable `approvals` table semantics, expiry, edit-and-resume for chunk plan, audit-trail UI. | R3. |
| **Agent-R7** | Provider failure recovery: `paused_provider_quota` state, scheduler re-enqueue, honor `Retry-After`. | R2, LLM-M2 (cost/health) helps but not required. |
| **Agent-R8** | **LangGraph tripwire evaluation.** If specific pain points emerged in R1–R7, evaluate LangGraph for a narrow slice (likely just the checkpointer). Else skip. | All prior. |
| **Agent-R9** | Long-running background agent beta: enable runs that legitimately span hours with multiple approval gates. End-to-end soak testing. | All prior. |

R1 and R2 carry roughly 60% of the total work between them. R3–R7 are incremental.

### 20.1 Order rationale

- R1 first because Postgres is the substrate for everything else.
- R2 immediately after because the worker queue is what makes "durable" mean anything in practice.
- R3 follows so debugging the new worker world is tolerable (in-memory logs across processes is unworkable).
- R4 unblocks multi-worker. Until then, even with a queue, you should run exactly one worker at a time.
- R5 is the hardening pass for "we say we're durable; let's actually be durable on every adversarial case."
- R6 is the most user-visible improvement after R3.
- R7 is operationally critical for real provider workloads (especially Gemini free tier).
- R8 is intentionally late and may be a no-op.
- R9 is the beta gate.

---

## 21. What NOT to Build Now

Strict. Default to "no."

- **Don't implement any of this now.** This document is design.
- **No LangChain.** Anywhere.
- **No LangGraph.** Until R8, and only after a specific pain point motivates it.
- **No replacement of current chunked execution.** Extend it; do not rewrite.
- **No removal or weakening of approval gates.** Ever.
- **No autonomous merge.** Pipewright does not merge.
- **No browser automation.** Out of scope.
- **No cloud deployment work in this design.** Single-instance Postgres + single worker is the R1-R7 target.
- **No multi-agent debate / planner-vs-architect adversarial rounds.** Different product.
- **No streaming responses from providers.** R9 at earliest.
- **No tool use / function calling from providers.** Same.
- **No org-level / team-level policies.** Single user.
- **No replacement of provider abstraction (LLM-M1).** Preserve.
- **No replacement of memory architecture (Memory-M1).** Preserve.
- **No real-time editing of model output by humans.** Approve or reject only.
- **No Slack/email approvals.** Future phase, possibly R9+.
- **No cost dashboards.** LLM-M2 territory.
- **No metrics/observability platform (Datadog, etc.) selection in this design.** Add when concretely needed.

---

## 22. Acceptance Criteria for the Durable Agent Beta

Pipewright can be called a durable background agent when all of the following are true:

1. A run survives an API process restart with no loss of progress and no duplicate work.
2. A run survives a worker process restart (crash or graceful) with no loss of progress and no duplicate work.
3. A user can close the browser, walk away, and return to a complete event-log replay of what happened.
4. An approval can sit pending for at least 7 days; the run resumes correctly when the approval arrives.
5. Duplicate enqueue of the same run results in exactly one PR. (Tested by deliberately enqueueing the same run twice.)
6. Live logs replay after restart. The UI looks identical whether the run is live or completed yesterday.
7. The project repo lock survives across processes. Two workers trying to mutate the same project's repo serialize cleanly.
8. The run snapshot (LLM config + memory) is provably immutable: changing project config mid-run does not affect the in-flight run.
9. Resume uses the original snapshot.
10. The human can inspect the full audit trail of a run via the UI: every state transition, every LLM call, every approval, every git operation, every PR action.
11. **Five long local runs** (defined as: at least 3 chunks, at least one high-risk chunk approval, at least one overnight pause) complete end-to-end with PRs created, with no manual intervention beyond approval clicks.
12. A run paused by `paused_provider_quota` resumes automatically when the quota window passes.
13. `needs_human_intervention` is reachable from all relevant failure paths and is the only mechanism for unsafe-state recovery.

If any of these is "mostly" true, beta is not ready.

---

## 23. Adversarial Closing Notes

Things that will go wrong even with this design implemented perfectly:

- **R2 (worker queue) is the highest-risk milestone.** Once the worker runs in a separate process, the debugging instincts that worked all through Phases 2A–2D stop working. Allocate two weeks for stabilization after R2 lands; don't pile R3 onto it immediately.
- **PostgreSQL adds operational overhead from R1 onward.** Local development needs a Postgres container or service. The "zero setup, one SQLite file" pleasure of the current local dev is gone forever after R1. Make sure the local dev script is excellent or contributors will hate it.
- **Idempotency keys are easy to get subtly wrong.** The temptation is to make them include a timestamp (which breaks retries because the timestamp differs). They must be derived only from inputs that retries will reproduce identically. Code review should flag any timestamp inside an idempotency key as a bug.
- **The `paused_provider_quota` state is operationally pleasant but tempting to misuse.** "Just pause and retry later" feels safe. Beware: a run that bounces in and out of paused for a day is consuming worker resources at each wake-up. Cap the total retry budget per run, not just per attempt.
- **Memory snapshots will accumulate quickly.** One per run. Over time, the `memory_snapshots` table grows. Plan for retention (likely tie to run retention).
- **The `needs_human_intervention` state is correct, but lots of system designs would have auto-recovered.** Resist the temptation to make any path through `needs_human_intervention` automatic. It exists because we don't trust ourselves yet.
- **Approval timeouts at 7 days will look short to enterprises and long to individuals.** Make it configurable per project — but pick a sane default (7 days) and don't ship until you've felt how it behaves in practice.
- **LangGraph will become more tempting as it matures.** Stay disciplined. The decision is "not now," not "never." The R8 tripwire is the right time to revisit.
- **The single biggest risk to the whole effort is scope creep.** Each phase has a deliverable that does *one thing*. If R1 turns into "Postgres migration plus a few small new features," it will take 4 months instead of 4 weeks. Be ruthless about scope per phase.

---

## Recommended Long-Term Direction

**Build custom. Postgres + ARQ + your own state machine. Treat "durable agent" as a marketing phrase for "Pipewright with a real backend," not as a license to add autonomy.**

The path:

1. **Land Memory-M1 and LLM-M1 first.** They produce the snapshots the durable runtime requires.
2. **Agent-R1 (Postgres + Alembic).** No behavior change. Migration only.
3. **Agent-R2 (worker queue).** This is the cutover. The hardest single milestone.
4. **Agent-R3 (durable events).** Without this, debugging R2 is brutal.
5. **Agent-R4 (DB locks).** Unblocks any future multi-worker work.
6. **Agent-R5 (checkpoint hardening + idempotency).** Make every claim in this document actually true.
7. **Agent-R6 (approval pause/resume).** The most user-visible upgrade.
8. **Agent-R7 (provider failure recovery).** Operationally necessary.
9. **Agent-R8 (LangGraph evaluation).** Probably skipped. Maybe a narrow checkpointer adoption. Most likely not.
10. **Agent-R9 (beta).** Soak test. Five long local runs as the gate.

**What to refuse:**
- Adopting LangChain anywhere.
- Adopting LangGraph end-to-end.
- Removing approval gates to "feel more like an agent."
- Adding autonomy that the current product specifically rejects.

**What to keep building toward, after R9:**
- Slack/email approvals (the audit moat extends to async approval channels).
- Memory-M2 (PostgreSQL memory schema, Run/Thread Memory).
- LLM-M2 (provider fallback, cost tracking).
- Memory-M3 / LLM-M3 (semantic memory, auto-routing) — only if customer demand exists.

The product remains: an AI engineering pipeline orchestrator where humans approve every step, durably, observably, and with an audit trail. The "durable agent" framing is true but should never become an excuse to weaken the approval model. That's the moat.