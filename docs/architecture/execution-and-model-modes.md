# Pipewright — Execution Modes + Model Selection Modes (Architecture Design)

**Status:** Design only. No implementation in scope.
**Immediate work remains:** LLM-M1 provider abstraction.
**Tone:** Adversarial. The goal is to find what's wrong with the design before code exists.

---

## 0. TL;DR

Two independent axes — *how much workflow Pipewright applies* (execution mode) and *how Pipewright picks the model per role* (model selection mode) — are the right way to model future configurability. Pipewright today is implicitly **Safe + Single model**, and that is the correct current default. The risk in this design is not the data model or the pipeline branching; it is **mode proliferation and UX collapse**. Three execution modes × three model selection modes = nine configurations, and most of them are not useful enough to justify their existence. Ship the data model + audit snapshot first, behavior changes later, and never let "Fast mode" degrade into "Cursor with extra friction." If Fast mode is faster than Cursor at the cost of Pipewright's differentiators, users will just use Cursor.

---

## 1. Product Framing

### 1.1 Why execution modes are needed

Pipewright today runs every task through the same pipeline: triage → chunk plan → human chunk-plan approval → planner → coder → patch → tests → reviewer → high-risk approval (if applicable) → final approval → PR. This is correct for a database migration. It is theatre for "add a docstring." A user who wants both will use Pipewright for one and Cursor for the other, and over time they will use Cursor for both because tool fragmentation is expensive.

Execution modes let one tool serve both classes of work without abandoning the safety stance that justifies its existence.

### 1.2 Why model selection modes are needed

Two real-world realities:

- **BYOK economics.** Most users have one provider key (the one they already pay for). Forcing them to bring three keys to use Pipewright is a non-starter.
- **Quality optimization.** Power users genuinely benefit from a different model per role. A cheap fast model for triage, a strong reasoning model for planning, a coding-tuned model for coder, a different strong model for reviewer (to break sycophancy). This is real, not marketing.

Model selection modes let the simple path stay simple while exposing the power-user path.

### 1.3 Why the two axes are independent

They answer different questions:

- Execution mode = "What workflow do I want for this task?"
- Model selection mode = "Which models should drive that workflow?"

A user can sensibly want Safe + Single (cautious workflow, one provider) or Fast + Manual (skip ceremony, but use the cheap model for triage and the strong model for coder). The matrix is meaningful. Combining them into one selector ("Pro mode," "Quick mode") collapses real choices into a marketing skin and confuses the audit trail later.

### 1.4 Where Pipewright is better than Cursor / Claude Code / Codex

- **Mandatory pre-PR approval gate.** Nothing reaches your default branch without a human reviewing the diff. Cursor doesn't enforce this; you can opt into it but most users don't.
- **Audit trail per role.** "What did the planner decide, with what context, and why" is recorded. No coding agent does this.
- **Chunked rollback.** A multi-file change can be reverted at chunk boundaries with checkpoints. Cursor's undo is editor-scoped, not pipeline-scoped.
- **Persistent project memory across sessions and tools.** `CLAUDE.md` and `.cursorrules` are static files; Pipewright's memory is structured, versioned, validated against repo reality, and approval-gated.
- **Reproducibility.** A finished run can be replayed from checkpoint. Cursor sessions are ephemeral.
- **Risk-aware approval escalation.** High-risk chunks (DB, auth, security) get human review even mid-run. Cursor treats all edits identically.

### 1.5 Where Pipewright is worse

Be honest about this — users discover it on day one whether you write it down or not.

- **Latency.** Pipewright adds seconds to minutes per stage. Cursor responds in real-time.
- **Token cost.** Pipewright runs multiple AI roles per task and re-injects context per stage. It will cost more tokens than Cursor for the same outcome on simple tasks. Always.
- **No interactive editing.** Cursor edits in your buffer; Pipewright works in your repo on a branch. Different mental model.
- **Setup friction.** First-run setup is heavier (BYOK, project config, test command, GitHub config). Cursor has none of that.
- **No agentic exploration during coding.** Cursor's Composer/Agent can read arbitrary files mid-task; Pipewright works from a planned file list. Less flexible, more predictable.

### 1.6 Honest positioning

Pipewright is **not** a faster or cheaper Cursor. It is a safer, auditable, memory-rich orchestrator for changes you want to do with care. The execution mode system lets you opt into less ceremony for trivial tasks; the model selection system lets you optimize cost/quality. Neither makes Pipewright competitive with Cursor on "fix this typo." Stop trying to win that fight.

---

## 2. Execution Modes — Detailed Specs

### 2.1 Fast mode

| Attribute | Value |
|---|---|
| Target user / task | Single-developer working on small, low-risk, single-file or near-single-file tasks. Docstrings, log strings, small refactors, type hints, small bug fixes, simple test additions. |
| Target outcome | Faster than today's Pipewright. Still produces a PR with a real diff. |
| Triage | Skipped or replaced with a lightweight one-shot "is this small enough for Fast?" classifier. |
| Chunk plan | Skipped. One implicit chunk. |
| Planner | Skipped or merged into coder (single combined prompt). |
| Coder | Run. |
| Patch applier | Run. (Always run — this is what produces the diff.) |
| Tests | Run. (Non-negotiable — see §2.4.) |
| Reviewer | Skipped by default. |
| High-risk approval | Not applicable (no risk classification). |
| Final approval | **Mandatory.** |
| PR creation | After final approval. |
| Memory injection | Hard facts only. No semantic memory retrieval. |
| Repo index usage | File index queried once for context. No deep file reads beyond planner's explicit list. |
| Logging / audit | Reduced (single combined log entry per run). |
| Rollback / checkpoints | One checkpoint after tests pass. |
| Expected tokens | Lowest in Pipewright's spectrum. Still higher than Cursor for the same task. |
| Expected latency | Lowest in Pipewright's spectrum. Still higher than Cursor. |
| Risk profile | Highest within Pipewright. Acceptable only because final approval gate remains. |
| Failure handling | Test failure auto-escalates to Balanced or Safe (see §11). |
| When NOT to use | DB migrations, auth, security, encryption, payments, deletion logic, concurrency primitives, infra/deployment, large refactors, anything that touches > N files (configurable threshold, e.g., 5). |

### 2.2 Balanced mode

| Attribute | Value |
|---|---|
| Target user / task | Routine feature work. Multiple files, moderate complexity, not touching security-critical code. |
| Triage | Light triage. Classifies complexity but does not always produce a multi-chunk plan. |
| Chunk plan | Generated only if triage decides chunking is warranted (typically: > N files OR identified high-risk subtask). |
| Planner | Run per chunk if chunked; once if not. |
| Coder | Run per chunk. |
| Patch applier | Run per chunk. |
| Tests | Run after each chunk's patch. |
| Reviewer | Run only for high-risk chunks or chunks exceeding a diff-size threshold. |
| High-risk approval | Required for chunks the triage marks high-risk. |
| Final approval | **Mandatory.** |
| PR creation | After final approval. |
| Memory injection | Full hard facts + semantic memory top-N. |
| Repo index usage | Full retrieval per chunk. |
| Logging / audit | Moderate. Per-chunk logs but not exhaustive role-level dumps. |
| Rollback / checkpoints | Per-chunk checkpoints when tests pass. |
| Expected tokens | Middle of Pipewright's spectrum. |
| Expected latency | Middle. |
| Risk profile | Moderate. Reviewer covers risky chunks; trivial chunks skip reviewer to save tokens. |
| Failure handling | Chunk test failure rolls back chunk; orchestrator can escalate to Safe semantics for retry. |
| When NOT to use | Database migrations, auth changes, security/encryption, payment processing — explicitly Safe-only domains. |

### 2.3 Safe mode

| Attribute | Value |
|---|---|
| Target user / task | Production-critical changes. Migrations, auth, security, payment, payroll, infrastructure, large refactors, deletion logic, anything where a wrong PR is expensive to recover from. |
| Triage | Full triage with complexity scoring and risk classification. |
| Chunk plan | Always generated. Human approval of chunk plan **mandatory** before execution. |
| Planner | Per chunk. |
| Coder | Per chunk. |
| Patch applier | Per chunk. |
| Tests | Run after every chunk's patch. Failure halts pipeline. |
| Reviewer | Run for every chunk. Different model than coder when Manual multi-model is configured. |
| High-risk approval | Required for any chunk marked high-risk. Cannot be bypassed by user setting. |
| Final approval | **Mandatory.** |
| PR creation | After final approval. |
| Memory injection | Full hard facts + semantic memory top-N. Conflict detection (from Memory M2 design) enforced at preflight. |
| Repo index usage | Full retrieval, plus targeted file content reads. |
| Logging / audit | Full. Every stage logged, every prompt/response token-counted, every approval recorded. |
| Rollback / checkpoints | Per-chunk and per-step checkpoints. Resume from any boundary. |
| Expected tokens | Highest. |
| Expected latency | Highest. |
| Risk profile | Lowest. The full ceremony exists to absorb risk. |
| Failure handling | Test failure rolls back chunk and either retries with correction prompt or surfaces to human. |
| When NOT to use | Trivial single-file fixes. The ceremony is real overhead; running Safe on a docstring change is a waste of both wall-clock time and tokens. |

### 2.4 Cross-mode invariants

Some things never change regardless of mode. These are the floor:

- **Final approval before PR creation.** Cannot be disabled by any mode. This is Pipewright's defining commitment.
- **Tests must run.** A mode that skipped tests would be a code generator, not an engineering pipeline.
- **No autonomous merge.** Pipewright opens PRs; humans merge.
- **Memory injection rules.** Current repo > project memory > semantic memory. Same in every mode.
- **No auto-save of long-term memory.** Same in every mode.
- **Rollback safety on test failure.** Failed tests roll back the chunk in all modes.

If a mode would violate any of these, it is by definition not a Pipewright mode.

### 2.5 Mode comparison table

| | Fast | Balanced | Safe |
|---|---|---|---|
| Triage | none / one-shot | light | full |
| Chunk plan | none | conditional | always + human approval |
| Per-chunk planner+coder | combined | separate, conditional chunking | separate, every chunk |
| Reviewer | skipped | risky chunks only | every chunk |
| High-risk approval | n/a | for marked chunks | mandatory for marked chunks |
| Final approval | yes | yes | yes |
| Tests | yes | yes (per chunk) | yes (per chunk) |
| Checkpoints | 1 | per chunk | per chunk + per step |
| Audit detail | low | medium | high |
| Tokens (relative) | 1× | ~2–3× | ~4–6× |
| Latency (relative) | 1× | ~2–3× | ~4–6× |
| Risk absorbed | low | medium | high |

Multipliers are illustrative, not measured. Real data will only exist after Modes-M1 logs mode + token usage per run.

---

## 3. Model Selection Modes — Detailed Specs

### 3.1 Single model

| Attribute | Value |
|---|---|
| Target user | Most users. Anyone with one provider key. |
| Selection mechanism | One `(provider, model)` pair applies to every role. |
| Role mapping | Every role → same (provider, model). |
| Cost behavior | Predictable. Easy to estimate per run. |
| Quality behavior | Bounded by the chosen model's worst capability. A cheap model produces cheap planning AND cheap reviewing. |
| Debugging complexity | Lowest. One thing to change when behavior degrades. |
| Audit requirements | Trivial. One provider/model per run. |
| Risk profile | Reviewer = coder. Sycophancy / blind-spot risk is highest here. |
| When NOT to use | When budget allows multi-model AND task is high-risk AND you have data showing a specific role benefits from a different model. |

### 3.2 Manual multi-model

| Attribute | Value |
|---|---|
| Target user | Power users. Teams. Anyone with multiple provider keys and time to tune. |
| Selection mechanism | User assigns `(provider, model)` per role: triage, planner, coder, reviewer, summary. Falls back to default if a role is unset. |
| Role mapping | Different roles can use different providers. Cross-provider conversation is per role, not per turn. |
| Cost behavior | Harder to predict. Strong-model coder + cheap-model triage can be cheaper OR more expensive than Single depending on token distribution. |
| Quality behavior | Best within current state-of-the-art. Reviewer-different-from-coder is a meaningful sycophancy mitigation. |
| Debugging complexity | High. Three providers behaving badly looks different from one provider behaving badly. |
| Audit requirements | Significant. Per-role provider/model recorded per run. |
| Risk profile | Lower than Single if reviewer ≠ coder; higher if user picks weak models for critical roles (e.g., a cheap model for reviewer). |
| When NOT to use | First-time users. Quick experiments. Anyone without a clear hypothesis about which role benefits from which model. |

### 3.3 Auto select (future)

| Attribute | Value |
|---|---|
| Target user | Eventually: everyone. Initially: nobody. |
| Selection mechanism | Pipewright picks `(provider, model)` per role based on task complexity, risk class, repo size, file count, context length, historical performance, cost budget. |
| Role mapping | Computed per task per role. |
| Cost behavior | Optimizable. Also surprising. |
| Quality behavior | In theory: better than Manual because the system has data the user doesn't. In practice: only useful after enough runs exist to train the heuristic. |
| Debugging complexity | Highest. "Why did it pick that model" is a real question that requires a real answer. |
| Audit requirements | Massive. Selection rationale must be recorded per run per role. |
| Risk profile | Hidden — model selection is no longer a user decision, so a wrong selection is on Pipewright. |
| When NOT to use | **Now.** Pipewright does not have the data, the cost model, or the capability metadata to do this honestly yet. |

### 3.4 Model selection invariants

Across all modes:

- The provider abstraction (LLM-M1) is the foundation. None of these modes ship before the abstraction.
- Provider/model selection per role is **immutable per run** once a run starts (see §6.4).
- A configured provider must exist in the registry and pass startup validation, or the run is rejected.
- Reviewer = coder is allowed but flagged in audit. Reviewer ≠ coder is recommended in Safe mode.

---

## 4. Mode Matrix (9 combinations)

| Combination | Useful? | Tokens | Quality | Complexity | Recommended use case | MVP / defer |
|---|---|---|---|---|---|---|
| **Fast + Single** | Yes | Lowest | Bounded by single model | Lowest | Solo dev, small fixes, one provider | Defer (post-Modes-M2) |
| **Fast + Manual** | Marginally | Low–mid | Slight gain from cheap-triage + strong-coder | Medium | Cost-sensitive power user | Defer (low ROI) |
| **Fast + Auto** | No | Variable | Unpredictable | High | None — Auto + Fast amplifies risk | **Do not build** |
| **Balanced + Single** | Yes | Medium | Bounded by single model | Low | Default for routine team work (future) | Defer (post-Modes-M3) |
| **Balanced + Manual** | Yes | Medium | Better reviewer independence | Medium | Teams with multiple keys, routine work | Defer |
| **Balanced + Auto** | No (yet) | Variable | Depends on data | High | Future, after data exists | Defer indefinitely |
| **Safe + Single** | Yes | High | Bounded by single model | Low | **Current Pipewright. Today's default.** | Already shipped |
| **Safe + Manual** | Yes | Highest | Best — independent reviewer + strong coder | High | High-stakes production changes, teams | Defer until Manual UI exists |
| **Safe + Auto** | No (yet) | Highest variable | Depends on data | Highest | Future enterprise case | Defer indefinitely |

Observations:

- **Six of nine combinations are deferrable.** Only Safe + Single is "today." The mode system is mostly a forward bet.
- **Auto Select × any execution mode is unbuildable today.** Three full rows depend on capability data Pipewright does not have.
- **Fast + Auto is actively dangerous and should not exist.** Auto deciding to use a cheap model on Fast mode amplifies two risk axes simultaneously.
- The matrix's actual MVP cell is the one already shipped. Everything else is product extension, not core.

---

## 5. Recommended Defaults

| Question | Answer | Why |
|---|---|---|
| Default for first-time users | **Safe + Single** | Safety stance is the differentiator. A first run that's slower but produces a careful PR teaches the product's value. A first run that's fast and produces a sloppy PR teaches that Pipewright = Cursor with friction. |
| Default for local MVP | **Safe + Single** | Same as above. This is today. |
| Default for serious/risky tasks | **Safe + Manual** (when Manual is available) | Reviewer-different-from-coder matters most on risky work. |
| Should Safe remain default? | **Yes**, indefinitely. | Defaults define the product. Safe is the product. |
| Should Balanced become default later? | **Only with data.** | After Modes-M1 logs enough runs to show that ~X% of tasks are over-served by Safe, Balanced becomes a sensible *recommended* mode (badge in UI). Default still Safe. |
| Should Fast be opt-in? | **Yes.** Explicit selection, with a warning the first time. | Users opting into Fast acknowledge what they're giving up. |
| Should Auto Select be hidden until data exists? | **Yes**, behind a feature flag. | Surfacing Auto with no training data produces user-blamed bad outcomes. |

The principle: **defaults should be defensible without explanation.** Safe + Single is defensible. Fast + Auto is not.

---

## 6. State / Data Model

### 6.1 Enums

```python
class ExecutionMode(StrEnum):
    FAST = "fast"
    BALANCED = "balanced"
    SAFE = "safe"

class ModelSelectionMode(StrEnum):
    SINGLE = "single"
    MANUAL = "manual_by_role"
    AUTO = "auto"

class Role(StrEnum):
    TRIAGE = "triage"
    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    SUMMARY = "summary"
```

### 6.2 Role model config

```python
class RoleModelConfig(BaseModel):
    provider: str           # 'gemini', 'openai', 'anthropic', ...
    model: str              # provider-specific model identifier
    # Reserved for future use; not consulted in M1:
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
```

### 6.3 Project-level configuration (future, not now)

```python
class ProjectLLMConfig(BaseModel):
    execution_mode: ExecutionMode = ExecutionMode.SAFE
    model_selection_mode: ModelSelectionMode = ModelSelectionMode.SINGLE
    # SINGLE mode uses this for every role:
    default: RoleModelConfig | None = None
    # MANUAL mode uses per-role overrides; falls back to default for unset roles:
    roles: dict[Role, RoleModelConfig] = Field(default_factory=dict)
```

This does not become a DB table until Modes-M1. Today it would be derived from env + hardcoded defaults.

### 6.4 Run mode snapshot (immutable per run)

```python
class RunModeSnapshot(BaseModel):
    """
    Captured at run start. Immutable for the lifetime of the run.
    Resume reads this, not the current project config.
    """
    run_id: str
    execution_mode: ExecutionMode
    model_selection_mode: ModelSelectionMode
    role_models: dict[Role, RoleModelConfig]
    # Captured at snapshot time:
    captured_at: datetime
    project_config_version: int | None = None
    # If the user starts a run while a different mode is "current" project setting:
    overridden_at_run_start: bool = False
```

### 6.5 Mid-run mode change semantics

**Rule:** Mid-run mode changes are ignored for the running run. The run uses its snapshot. Mode changes apply to subsequent runs only.

**Why this matters:** if a user starts a Safe run, pauses at final approval, then changes the project mode to Fast and resumes, the resume must not silently switch to Fast semantics. The chunked work already done was decided under Safe; resuming under Fast would invalidate the prior steps' assumptions. Worse, it would skip the reviewer that was supposed to look at chunks 3 and 4.

**Implementation rule when Modes-M1 lands:** every resume reads `RunModeSnapshot` and never reads project config for mode-affecting decisions.

### 6.6 What this looks like persisted

Today: nothing persisted; everything is the current single-mode behavior.
At Modes-M1: a `run_mode_snapshots` table holds the snapshot per `run_id`. The orchestrator reads it on every stage.
At Modes-M2+: `projects.execution_mode` and related fields exist; project APIs surface them; the snapshot is derived from them at run start.

---

## 7. Pipeline Architecture Implications

How each stage behaves under each mode:

| Stage | Fast | Balanced | Safe |
|---|---|---|---|
| Triage | Skipped or one-shot classifier ("is this Fast-eligible?"). | Runs; may decide chunking is not needed. | Always runs; always produces a chunk plan. |
| Chunk plan | None. Implicit single chunk. | Generated conditionally. No human approval when single chunk. | Always generated. Human approval mandatory. |
| Planner | Skipped or merged with coder. | Per chunk if chunked, once otherwise. | Per chunk, always. |
| Coder | Combined with planner. | Per chunk. | Per chunk. |
| Patch applier | Same as today. | Same. | Same. |
| Tests | Required. Failure escalates mode (§11). | Required per chunk. Failure rolls back chunk. | Required per chunk. Failure rolls back chunk and may escalate. |
| Reviewer | Skipped. | Conditional (risky chunks, large diff chunks). | Every chunk. |
| High-risk approval | N/A (no chunk-level risk classification). | For marked chunks. | For marked chunks; cannot be bypassed. |
| Final approval | **Mandatory.** | **Mandatory.** | **Mandatory.** |
| Push + PR | After final approval. | Same. | Same. |
| Resume / recovery | Reads run snapshot. | Reads run snapshot. | Reads run snapshot. |
| Logs | Reduced. One run-level event stream. | Per-chunk stream. | Per-stage per-chunk stream. |
| Checkpoints | One after tests pass. | Per chunk when tests pass. | Per step within chunk when tests pass. |

**The orchestrator's mode-awareness is concentrated in a few decision points**, not scattered. Concretely (design intent only):

- `should_run_triage(mode) -> bool`
- `should_generate_chunk_plan(mode, triage_result) -> bool`
- `requires_chunk_plan_approval(mode) -> bool`
- `should_run_reviewer(mode, chunk_risk, chunk_size) -> bool`
- `should_require_high_risk_approval(mode, chunk_risk) -> bool`

Final approval, tests, patch applier, and PR creation are *not* mode-decisioned. They're cross-mode invariants.

---

## 8. Fast Mode Architecture

The risk in Fast mode is not the modes that exist alongside it. The risk is that Fast mode becomes "Pipewright without Pipewright's value." Be specific about what survives.

### 8.1 The minimum safe Fast pipeline

```
[memory injection (hard facts only)]
→ [optional one-shot Fast-eligibility classifier]
→ [combined planner+coder prompt]
→ [patch applier]
→ [tests]
  → if fail: rollback + escalate to Balanced or Safe
→ [final approval gate]
→ [push + PR]
```

### 8.2 What Fast can skip

- Full triage (replaced by one-shot eligibility check, or skipped entirely if the user explicitly selects Fast).
- Chunk plan generation.
- Chunk plan human approval.
- Reviewer.
- High-risk approval gate (because Fast should refuse to run on high-risk tasks; see §11).

### 8.3 What Fast cannot skip

- Memory injection (still advisory, still subject to memory rules).
- Tests.
- Final approval before push.
- PR creation step.
- Test-failure rollback.
- The basic logging required for audit.

### 8.4 When Fast must escalate

See §11 for the full rule set. Summary: Fast escalates to Balanced or Safe when any of:

- Task description contains high-risk keywords (migration, auth, security, encryption, payment, payroll, deletion, concurrency, locking, worker, deployment, infra, schema).
- File count to be modified exceeds the Fast threshold.
- Planned diff touches files in protected paths (configured per project).
- Tests fail.
- Memory conflict detection (from Memory M2) flags an unresolved conflict.

### 8.5 What if a task touches DB / security / auth?

Fast mode refuses to run it. The classifier's job is to detect this *before* any code generation begins. The user sees: "This task involves database changes — Fast mode is not appropriate. Switching to Balanced." with a one-click override that is logged.

### 8.6 What approval remains mandatory

Final approval. Always. Without exception. The diff hits the user's review screen before any push.

### 8.7 How to avoid making Fast mode unsafe

Three structural defenses:

1. **Eligibility gate before code generation.** The classifier runs first. If the task isn't Fast-eligible, no AI coding work happens. The cost of a wrong "Fast-eligible" verdict is just one extra prompt, not a wrong PR.
2. **Final approval is invariant.** No matter how aggressively Fast skips internal stages, the diff still gets human review.
3. **Tests are invariant.** A Fast PR with failing tests does not reach the approval gate.

If you remove any of these three, Fast mode is no longer Pipewright. It's just "AI that writes code."

---

## 9. Balanced Mode Architecture

### 9.1 How it differs from Safe

Three concrete differences:

- **Chunking is conditional.** Triage may decide a task is one chunk. Safe always chunks.
- **Reviewer is conditional.** Reviewer runs on risky or large chunks. Safe runs reviewer on every chunk.
- **Chunk plan human approval is conditional.** When a single chunk is produced, no chunk plan approval is needed (the chunk plan is trivially one item). Safe always requires chunk plan approval.

### 9.2 When Balanced chunks

When triage scores complexity above a threshold OR identifies a high-risk subtask OR file count exceeds N. Otherwise: single chunk, no plan approval.

### 9.3 When Balanced runs reviewer

Per-chunk, when any of: chunk is marked high-risk, chunk diff exceeds size threshold, chunk touches protected paths, chunk's test result is flaky/warning, or it's the last chunk in a multi-chunk run (the integration chunk).

### 9.4 When Balanced requires high-risk approval

Same triggers as reviewer's high-risk detection. The two are aligned — if reviewer is needed, high-risk approval is needed.

### 9.5 Logs and checkpoints

Per-chunk. Sufficient to resume from any chunk boundary. Not per-step within chunks.

### 9.6 Is Balanced a good future default?

Eventually, yes — but only after Modes-M1 logs enough mode-tagged runs to know how often Safe was overkill. Don't promote it to default on intuition.

---

## 10. Safe Mode Architecture

### 10.1 Current = Safe

Today's Pipewright is Safe mode. No design change needed for Safe behavior; it already exists. Modes-M1 only adds the snapshot field that says "this run is Safe."

### 10.2 What Safe must never skip

- Triage with risk classification.
- Chunk plan generation.
- **Human approval of the chunk plan before code generation.**
- Per-chunk planner.
- Per-chunk coder.
- Per-chunk tests.
- Per-chunk reviewer.
- High-risk approval gate for marked chunks.
- Final approval.
- Per-step checkpoints when tests pass.
- Full audit log.

### 10.3 Why Safe costs more tokens

Each chunk re-injects memory, re-reads context files, runs reviewer with both diff and original files. The audit log itself doesn't cost tokens, but the multi-stage prompts do. There is no clever way to make Safe cheap. Tokens buy control.

### 10.4 Why Safe is best for risky production changes

Two reasons that compound:

- **Multi-stage cross-checking.** A bad planner output gets caught by coder's confusion, by test failure, by reviewer, or by the human at chunk approval. Four independent chances to catch a mistake.
- **Chunked rollback.** A bad chunk 3 doesn't taint chunks 1–2. Recovery is bounded.

---

## 11. Auto-Escalation Rules

The most important design in this whole doc. Without escalation, Fast is unsafe; with escalation, Fast is honest.

### 11.1 Pre-execution triggers (run from triage / eligibility)

| Trigger | Action |
|---|---|
| Task description contains any of: `migration`, `database schema`, `alembic`, `prisma migrate`, `auth`, `authentication`, `authorization`, `permission`, `rbac`, `security`, `encryption`, `secret`, `credential`, `payment`, `payroll`, `pii`, `delete user`, `delete all`, `truncate`, `drop table`, `concurrency`, `lock`, `mutex`, `worker`, `queue`, `deployment`, `infra`, `terraform`, `kubernetes`, `dockerfile`, `production` | Escalate Fast → Balanced. Log reason. |
| Task description mentions multi-file refactor or rename | Escalate Fast → Balanced. |
| File count in planner's `files_to_modify` ∪ `files_to_create` > Fast threshold (e.g., 5) | Escalate Fast → Balanced. |
| Files touched include any path in project's protected list (e.g., `migrations/`, `auth/`, `security/`, `infra/`) | Escalate Fast → Safe. (Skip Balanced — these paths warrant full ceremony.) |
| Memory M2 conflict detection has any unresolved conflict | Block run. Not an escalation — a refusal. |
| Planner risk classification = high | Balanced → Safe. |

### 11.2 Mid-execution triggers

| Trigger | Action |
|---|---|
| Test failure in Fast | Roll back. Escalate to Balanced. Re-run from start under Balanced. |
| Test failure in Balanced after one retry | Escalate to Safe. Re-run failing chunk under Safe. |
| Reviewer flags critical issue in Balanced | Promote chunk to high-risk approval gate. |
| Patch applier rejects path | Halt. No silent escalation — this is a real error. |

### 11.3 Override semantics

The user can override escalation, with constraints:

- **Always logged.** Override reason recorded. Permanent audit record.
- **Specific paths cannot be Fast-overridden.** If a project marks `migrations/` as Safe-only, no override permits Fast on those files.
- **High-risk approval cannot be bypassed.** Override may take a run from Safe to Balanced, but a chunk marked high-risk still requires high-risk approval. The mode determines workflow ceremony; it does not determine whether risk-classified work gets human attention.

### 11.4 Why this matters

Auto-escalation is the only thing that keeps Fast mode from being a footgun. Without it, a user types "fix the auth bug" in Fast mode and Pipewright produces a one-shot auth change with no reviewer. With it, the keyword match flips to Balanced before code generation begins.

False positives (Balanced when Fast would have sufficed) are an annoyance. False negatives (Fast when Safe was needed) are a product failure. The classifier biases toward false positives intentionally.

---

## 12. Model Selection Architecture

### 12.1 Resolver (design extension of LLM-M1)

```python
def resolve_run_role_models(
    project_config: ProjectLLMConfig,
    mode_snapshot_overrides: dict[Role, RoleModelConfig] | None = None,
) -> dict[Role, RoleModelConfig]:
    """
    Returns the per-role model assignment for a run.

    Precedence:
      1. mode_snapshot_overrides (per-run pinning, future feature)
      2. project_config.roles[role]            (MANUAL)
      3. project_config.default                (SINGLE / MANUAL fallback)
      4. env DEFAULT_LLM_PROVIDER / DEFAULT_LLM_MODEL
      5. hardcoded fallback
    """
```

`resolve_role_config` from LLM-M1's role_config.py is the seed of this. The full project-level resolver is its evolution, not a separate system.

### 12.2 Capability metadata (future)

```python
class ModelCapability(BaseModel):
    provider: str
    model: str
    supports_roles: set[Role]                    # which roles can use this model
    context_window_tokens: int
    cost_per_million_input_tokens: float | None
    cost_per_million_output_tokens: float | None
    recommended_for: list[str]                   # 'reasoning', 'coding', 'fast_triage'
    last_verified: datetime
```

Not built in M1. Required before Auto Select. Until then, the registry knows that providers exist; it does not know which models are good at what.

### 12.3 Validation

Startup-time validation rejects:

- Provider name not registered.
- Model name the provider claims not to support.
- Required credentials missing.

Run-start validation rejects:

- Configured role's provider not present.
- Configured role's model not present.
- Reviewer = coder in Safe mode (warn only, do not block — log it).

### 12.4 Supported roles

All five (triage, planner, coder, reviewer, summary) are valid targets for per-role configuration in Manual mode. Reviewer and summary may be unused by certain execution modes (Fast skips both); their configs are still validated but inert.

### 12.5 Reviewer-different-from-coder

In Manual mode, a UI nudge recommends reviewer ≠ coder for Safe and Balanced. In Single mode, this is impossible — note in audit that the run used same-model reviewer. In Auto mode, the selector should prefer reviewer ≠ coder when capability data supports it.

### 12.6 Fallback behavior

**Explicitly deferred.** No fallback chains in any current phase. Provider failure surfaces as `LLMError`; the role module decides whether to retry the same provider (per LLM-M1 retry rules) or fail the run. Cross-provider fallback ("Gemini timed out, try OpenAI") requires response normalization at a deeper level than LLM-M1 provides and would silently mask provider degradation.

---

## 13. Token / Cost Analysis

Honest comparison. Multipliers are illustrative; real numbers require Modes-M1 telemetry.

| Configuration | Relative token cost | What you're paying for |
|---|---|---|
| Cursor / Claude Code / Codex direct, single agent | 1× | Interactive single-turn editing. No multi-role, no audit, no chunking. |
| Pipewright Fast + Single | ~2–3× | Adds: memory injection, eligibility classifier, test runner, final approval gate, PR creation, audit log. Loses: chunking, reviewer, per-stage prompts. |
| Pipewright Balanced + Single | ~4–6× | Adds: triage, conditional chunking, conditional reviewer, per-chunk planner. |
| Pipewright Safe + Single | ~8–12× | Adds: full chunk plan, per-chunk planner+coder+reviewer, per-step audit, conflict detection. |
| Pipewright Safe + Manual multi-model | ~8–12× (different mix) | Same workflow as Safe + Single; the *cost mix* changes per role (cheap triage, expensive coder, expensive different-model reviewer). Total can be higher or lower depending on which models. |

Three honest statements:

1. **Pipewright will cost more tokens than Cursor for the same task on simple work.** Always. The workflow itself is the cost.
2. **Fast mode reduces but does not eliminate the overhead.** Even Fast has memory injection, an eligibility classifier, and a final approval gate.
3. **Safe mode buys control with tokens.** That is the trade. Marketing Pipewright as cheaper than Cursor is dishonest and will be detected by users in their first billing cycle.

The right pitch: *"Pipewright costs more tokens per task and saves you from the changes you shouldn't have shipped."*

---

## 14. Quality Analysis

| Configuration | Code quality | Planning quality | Reviewer independence | Handoff errors | Consistency | Latency | Debugging |
|---|---|---|---|---|---|---|---|
| One strong model, direct (Cursor agent) | High | Implicit, not validated | None | N/A | High within session | Lowest | Easiest |
| Same model inside Pipewright Safe | High | Validated, structured | **Low** (reviewer = coder = same model) | Moderate (multi-stage handoffs add JSON parse risk) | High | High | Medium |
| Different coder/reviewer models, Manual Safe | High | Validated, structured | **High** (different model judges work) | Moderate (handoffs) | Slightly lower (model differences) | Highest | Hardest |
| Manual multi-model per role | High | High (strong reasoning model planner) | High | Higher (more variability across roles) | Lower | High | Hard |
| Auto Select (future) | Depends on capability data | Depends | Depends | Unknown | Lower (selection varies) | Variable | Hardest |

Key observations:

- **Reviewer independence is the single biggest quality lever.** Same model reviewing its own code produces sycophantic results. This is well documented in agent literature. Manual multi-model exists primarily to enable reviewer ≠ coder.
- **Handoff errors scale with role count and model variety.** Pipewright already pays this cost (JSON parsing across roles). Multi-model amplifies it. Pydantic validation is the existing mitigation.
- **Consistency drops as the configuration space grows.** A single user running Single+Safe gets reproducible runs. A team running Manual gets per-developer variance.

---

## 15. Risks & Failure Cases

| Risk | Trigger | Severity | Mitigation |
|---|---|---|---|
| Fast mode skips too much and causes unsafe changes | Wrong eligibility classification | High | Auto-escalation rules (§11); final approval invariant; protected paths cannot be Fast. |
| Safe mode too expensive/slow for small tasks | Default applied to trivial work | Medium | Fast/Balanced exist; UI recommends mode based on task description. |
| Balanced mode ambiguity ("when does it chunk?") | Triage threshold unclear to user | Medium | Surface triage's reasoning in UI: "Chose to chunk because: 6 files affected, contains DB code." |
| User picks weak model for coder/reviewer | Manual mode misuse | High | Capability metadata warns at config time; reviewer warnings in audit. |
| Reviewer = coder misses mistakes | Single mode or Manual misconfiguration | High | Audit logs reviewer/coder identity; UI nudge in Safe mode; sycophancy-resistant reviewer system prompt. |
| Auto Select picks wrong model | Bad heuristic or insufficient data | High | Auto Select gated behind feature flag and capability data; never default. |
| Model disagreement (planner says X, coder does Y) | Inter-stage drift | Medium | Pydantic handoff contracts already catch structural drift; semantic drift is human-reviewed at final gate. |
| Handoff errors (JSON parse failures across roles) | Model output drift | Medium | LLM-M1 retry; correction prompt; same as today. |
| Provider failure mid-run | Network / 5xx / rate limit | Medium | LLM-M1 retryable error class; resume reads snapshot and uses same provider. No silent cross-provider fallback. |
| User changes mode/config mid-run | UI permits config change while run active | High | Run mode snapshot is immutable; resume reads snapshot, not project config (§6.4). |
| Mode snapshot missing on resume | Pre-Modes-M1 runs resumed post-migration | Low–medium | Backfill rule: missing snapshot = Safe. The most conservative interpretation. |
| Fallback hides real failure | (Deferred — no fallback exists) | N/A | Do not build cross-provider fallback in current phases. |
| Cost explosion from repeated context injection | Long-running Safe runs re-inject memory per chunk | Medium | Context budget per stage; alert user when run exceeds budget. |
| High-risk task incorrectly classified as Fast | Classifier false negative | High | Bias classifier toward false positives; protected paths bypass classifier entirely. |
| Users do not understand modes | Three modes × three model selections = nine combos | Medium | UI surfaces only mode + "use defaults," not nine cells. Defaults are sane. |
| UI complexity | Too many knobs visible | Medium | Tiered disclosure (§16). |
| Too many knobs | Power-user options leak into default UI | Medium | Manual + Auto hidden behind "Advanced" toggle. |
| First-run experience becomes harder | Mode selector before user has run anything | High | Default to Safe + Single. No mode selector in first-run flow. Mode shows up *after* first successful run. |

---

## 16. UX Design

The mode system can ruin Pipewright's UX faster than any other feature. The defense is **progressive disclosure**.

### 16.1 Default state

The home page mode controls show:

```
Mode: Safe (recommended)        [change]
Model: Gemini 2.5 Flash Lite    [change]
```

That's it. No mention of "model selection mode." No exposed nine-cell grid.

### 16.2 First-run flow

No mode selector at all. The user just runs. The product behaves as Safe + Single using whatever default they configured. Mode controls appear in project settings after the first run.

### 16.3 Mode selector (after first run)

A three-option pill:

```
[ Fast  ]  [ Balanced ]  [ Safe • recommended ]
```

Hovering each shows: a one-sentence description, expected tokens (relative), expected latency (relative), and a warning if the current task description triggers escalation.

### 16.4 Mode warnings (the critical UX)

Before run start, if the task description triggers escalation, show:

> "Your task mentions *database migration*. This will run in **Safe** mode instead of Fast. [Why?](link)"

The user can override, but the override flow takes two clicks and a confirmation, with the override logged.

### 16.5 Model selection UI (Advanced section)

Hidden under "Advanced settings" in project settings. Three tabs:

- **Single** (default): one provider + one model dropdown.
- **Per role** (Manual): five rows (triage, planner, coder, reviewer, summary), each with provider + model. Falls back to default if blank.
- **Auto** (greyed out with "Available after 50 runs"): waiting on capability data.

### 16.6 Recommended-mode badge

For each task, a small badge near the submit button: "Recommended: Safe." Based on task text. Helps users learn what triggers what.

### 16.7 Cost / safety estimate

Pre-run, show approximate tokens and approximate latency for the chosen mode. Pure heuristic — don't promise exactness.

### 16.8 What NOT to show in UI

- The full mode matrix.
- The escalation rules verbatim.
- Capability metadata (until Auto exists).
- "How Pipewright is choosing models" — until Auto exists, the answer is "you configured them."

---

## 17. Observability / Audit

Stored per run (and visible in run detail page):

| Field | Why |
|---|---|
| `execution_mode` | What ceremony level the run used. |
| `model_selection_mode` | How models were chosen. |
| `role_models` (per role: provider, model) | Reproducibility. |
| `escalation_events` (mode change, trigger, was-overridden) | Why the run ended up Safer than requested. |
| `skipped_stages` (per stage: skipped, reason) | What Fast/Balanced didn't run. |
| `token_usage_per_role` (provider, model, input, output) | Cost analysis. |
| `approval_gates_triggered` (chunk plan, high-risk per chunk, final) | Audit trail. |
| `reviewer_different_from_coder` (bool) | Quality signal. |
| `run_summary` (high-level outcome) | Human-readable wrap-up. |

Two principles:

- **Audit is mode-aware.** A Safe run has detail per chunk per stage; a Fast run has detail per run. Don't pretend the data is uniform when the modes aren't.
- **Audit captures the snapshot, not the current config.** A run is forever audited under the mode it ran in, even if the project later switches default mode.

---

## 18. Implementation Roadmap

Reorganized from your proposed phases. Phase boundaries chosen so each phase is independently shippable and reversible.

| Phase | Name | Scope | Ships behavior change? |
|---|---|---|---|
| **LLM-M1-A** | Provider abstraction scaffolding | `backend/llm/` package, BaseLLMProvider, GeminiProvider, FakeProvider, errors, registry, role_config resolver, sanitize. Tests. No call sites change. | No |
| **LLM-M1-B** | Migrate triage/planner/coder to abstraction | Replace direct `google.generativeai` imports with `complete_for_role(...)`. Existing tests pass unchanged. | No (behavior identical) |
| **LLM-M1-C** | Env-based single + manual model config | Wire `DEFAULT_LLM_PROVIDER/MODEL` and `{ROLE}_LLM_PROVIDER/MODEL`. Single provider works; multi-provider keys honored if set. | Marginal (only if user sets new env vars) |
| **LLM-M1-D** | Provider/model logging & audit | Add `provider`/`model` columns to existing token-usage logs. Per-role visibility in run details. | No |
| **Modes-M0** | Architecture docs only | This document and follow-ups. No code. | No |
| **Modes-M1** | Mode data model + run snapshot | Add `execution_mode`, `model_selection_mode`, `role_models` to a `run_mode_snapshots` table. Default to `SAFE`/`SINGLE`. No pipeline branching yet — snapshot is recorded but not consulted for decisions. | No |
| **Modes-M2** | Fast mode prototype | Implement Fast pipeline as a parallel code path. Triggered only via explicit env or feature flag. Auto-escalation rules enforced. Tests for false positive / false negative on classifier. | Behind flag |
| **Modes-M3** | Balanced mode | Same shape as M2: parallel path, flag-gated, escalation rules. Conditional chunking and conditional reviewer. | Behind flag |
| **Modes-M4** | UI mode selector | After M2 and M3 are real, expose the three-pill selector in the UI. Default remains Safe. | Yes (UI only) |
| **Modes-M5** | Manual multi-model UI | Per-role provider/model picker in project settings. Validation, warnings. Reviewer ≠ coder nudge. | Yes |
| **Modes-M6 (much later)** | Auto Select | Capability metadata, selection heuristic, gated behind a sufficient-run-data threshold. | Yes, with strong defaults |

Two reasons for this ordering:

- **LLM-M1 must complete before any modes work begins.** Modes consume the provider abstraction; without it, every mode reimplements provider plumbing.
- **Modes-M1 (data model) must complete before Modes-M2 (Fast).** A Fast run without a snapshot is a Fast run that can't be resumed correctly. Build the snapshot, then build the modes that use it.

---

## 19. What NOT to Build Now

Strict. Each of these is a temptation to be refused.

- **No Fast mode now.** The whole pipeline must keep producing today's Safe behavior. Fast mode is Modes-M2, behind a flag.
- **No Auto Select now.** No capability metadata, no selection heuristic. Auto is Modes-M6, possibly never.
- **No complex UI now.** Mode selector is Modes-M4. Manual UI is Modes-M5. First-run experience must not see modes.
- **No cost dashboard now.** Per-role token logging (LLM-M1-D) is enough until real usage data exists.
- **No fallback chains now.** Cross-provider fallback is a separate, harder design problem. Provider failure surfaces clearly; the user retries.
- **No autonomous merge now or ever.** PR creation is the terminal step. Merge is GitHub's job and the user's decision.
- **No removing approval gates from Safe mode.** The current chunked flow is the product's spine.
- **No changing current chunked flow during provider abstraction.** LLM-M1 must be behavior-neutral. Modes work happens after.
- **No introducing a "mode" concept into Memory M2 / repo-reality conflict detection.** Conflict detection is mode-invariant: it runs identically in Fast, Balanced, and Safe. If anything, Fast mode runs conflict detection *more strictly* because there is no reviewer to catch misses.

---

## 20. Final Recommendation

### 20.1 Is this worth building?

**Conditionally yes.** The two-axis model is correct. The risk is that you build all three execution modes and all three model selection modes when only two cells (Safe+Single, Safe+Manual) carry the product's value. Build the data model and snapshot infrastructure (Modes-M1). Build Fast as a careful experiment (Modes-M2) only after LLM-M1 is fully shipped and you have a real user who explicitly asks for it. Treat Balanced and Auto as deferred until data justifies them.

### 20.2 Which parts are core?

- The data model (ExecutionMode, ModelSelectionMode, RunModeSnapshot).
- The immutable per-run snapshot rule (§6.4).
- Per-role provider/model audit (LLM-M1-D).
- Cross-mode invariants (final approval, tests, no autonomous merge).
- Auto-escalation rules (§11). Without them, Fast is a footgun.

### 20.3 Which parts are marketing fluff?

- **Auto Select.** It is a great deck slide and a bad product feature without capability data. Until Pipewright has 1000+ runs across diverse repos with labeled outcomes, Auto Select is fiction.
- **The full 3×3 matrix as a user-visible concept.** Six of nine cells are deferrable; surfacing all nine to users is a UX disaster.
- **"Multi-model intelligence" pitched as a differentiator.** It's a power-user feature. Most users will run Single. Don't lead the pitch with Manual.
- **Cost optimization framing.** Pipewright costs more tokens than direct tools. Pitching cost savings is dishonest.

### 20.4 Which parts should be delayed?

- Auto Select (indefinitely; possibly never).
- Manual UI (Modes-M5, after Manual via env is real).
- Cost dashboard (after enough runs exist to dashboard).
- Fallback chains (until a real user need + a clean design exists).
- A "for-PMs" non-developer mode (out of scope for current phases).

### 20.5 Honest positioning

Pipewright is not a faster Cursor. Pipewright is not a cheaper Cursor. Pipewright is **the AI engineering pipeline you use when the cost of a wrong PR is higher than the cost of a slower one.** The execution modes let you scale the ceremony to the risk; the model selection modes let you allocate budget across the roles that matter. Both systems exist to defend the product's actual differentiator — *human approval and audit before code reaches the default branch* — without forcing it on changes that don't need it.

The single line for the marketing site:

> *"Pipewright runs your AI coding workflow with the safety, audit, and approval gates that matter for production work. It's slower and pricier than direct tools. That's the point."*

That's the honest pitch. It will not win the entire market. It will win the slice of the market that knows why it should care.

---

## Recommended Implementation Order

This document is design only. The actual code work continues in the order already established:

1. **LLM-M1-A** — Provider abstraction scaffolding. (Currently in progress per the LLM-M1 review doc.)
2. **LLM-M1-B** — Migrate `triage.py` / `planner.py` / `coder.py` to use the abstraction. Behavior unchanged.
3. **LLM-M1-C** — Wire env-based single + manual model config.
4. **LLM-M1-D** — Add per-role provider/model logging to the existing audit surface.
5. **Modes-M0** — Treat this document as the architecture record; circulate, iterate if real users ask for things not covered here.
6. **STOP.** Do not begin Modes-M1 until LLM-M1-D is shipped and at least one external user has asked for either Fast mode or per-role model configuration. Without that signal, modes work is speculative and tempts scope creep.
7. **Modes-M1** — Mode data model + immutable run snapshot. No behavior change. Records `execution_mode=SAFE` for every run automatically.
8. **Modes-M2** — Fast mode behind a feature flag, with auto-escalation rules enforced from day one. No UI surface yet.
9. **Modes-M3** — Balanced mode behind the same flag.
10. **Modes-M4** — UI mode selector. Default remains Safe.
11. **Modes-M5** — Manual multi-model UI in project settings.
12. **Modes-M6** — Auto Select. Only if capability data justifies it. Maybe never.

**The single non-negotiable rule:** do not let modes work start before LLM-M1 is fully complete. Every mode consumes the provider abstraction. Building modes on top of half-finished abstraction will produce architectural rework neither phase can afford.

---