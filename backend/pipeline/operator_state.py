"""
operator_state.py
Pure read-model foundation for the future operator attention panel.

This module computes a current-state-only OperatorState from caller-supplied
context. It deliberately performs no I/O: no DB reads/writes, no git calls, no
route calls, no retries, no approvals, no commits, no pushes, and no PR
creation. Routes remain the source of truth and must revalidate before every
mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field, is_dataclass, replace
from enum import Enum
from typing import Any

from backend.core.statuses import RunStatus
from backend.pipeline.patch_failures import human_retry_ineligible_reason

# Bumped to 2 by item 16 (phase/narrative read-model): OperatorState now also
# carries an additive, derived `phase` and `narrative`. The bump lets the
# frontend evolve rendering safely; all pre-existing fields are unchanged.
SCHEMA_VERSION = 2
UNKNOWN_STATE_MESSAGE = (
    "Pipewright cannot determine the next safe action. No action is available. "
    "Investigate before proceeding."
)


class OperatorWaitingOn(str, Enum):
    HUMAN = "human"
    SYSTEM = "system"
    NOBODY = "nobody"


class OperatorDecisionType(str, Enum):
    PROGRESS = "progress"
    RISK_DECISION = "risk_decision"
    NONE = "none"


class OperatorActionSeverity(str, Enum):
    NORMAL = "normal"
    CAUTION = "caution"
    DANGER = "danger"


class OperatorSafetyCheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    WEAK = "weak"
    NOT_EVALUATED = "not_evaluated"
    NOT_APPLICABLE = "not_applicable"


class RunPhase(str, Enum):
    """User-facing run/chunk phase (proposal §4.8, item 16).

    A coarse, derived projection of the already-selected OperatorState — six
    buckets the UI renders instead of internal status strings. Display-only and
    advisory: the phase grants no authority, is never persisted, and is never
    read by a route to make a decision. It is recomputed on every read.
    """

    PLANNING = "planning"
    WAITING_FOR_YOU = "waiting_for_you"
    WORKING = "working"
    NEEDS_ATTENTION = "needs_attention"
    DONE = "done"
    # Reserved for an explicit user cancel/stop terminal status. The codebase
    # has no such status today (terminal = complete/failed/rejected/
    # final_rejected), so no current state reaches STOPPED — failed/rejected map
    # to NEEDS_ATTENTION per the §5.4/D1 ruling. The value exists so a future
    # cancel status lands in the right phase without re-deriving this logic.
    STOPPED = "stopped"


def _dump_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            key: _dump_value(item)
            for key, item in value.__dict__.items()
            if not key.startswith("_")
        }
    if isinstance(value, list):
        return [_dump_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_dump_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _dump_value(item) for key, item in value.items()}
    return value


class _Dumpable:
    def model_dump(self) -> dict[str, Any]:
        return _dump_value(self)


@dataclass(frozen=True)
class OperatorAction(_Dumpable):
    id: str
    label: str
    intent: str
    severity: OperatorActionSeverity = OperatorActionSeverity.NORMAL
    enabled: bool = True
    blocked_reason: str | None = None


@dataclass(frozen=True)
class OperatorSafetyCheck(_Dumpable):
    id: str
    label: str
    status: OperatorSafetyCheckStatus
    detail: str


@dataclass(frozen=True)
class OperatorTrustFact(_Dumpable):
    """A short current-state-derived fact. Not history and not audit."""

    id: str
    label: str
    detail: str


@dataclass(frozen=True)
class OperatorNarrative(_Dumpable):
    """Structured what/why/next narrative (proposal §4.8, item 16).

    A display-only structuring of the state's already-curated copy and computed
    actions — it introduces NO new prose, so it cannot add a correctness claim
    or leak raw evidence, and ``whats_next`` is derived ONLY from the available
    actions, so it can never advertise a blocked or nonexistent action.
    """

    what_happened: str
    why: str
    whats_next: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperatorState(_Dumpable):
    title: str
    explanation: str
    waiting_on: OperatorWaitingOn
    decision_type: OperatorDecisionType
    schema_version: int = SCHEMA_VERSION
    primary_action: OperatorAction | None = None
    neutral_actions: list[OperatorAction] = field(default_factory=list)
    secondary_actions: list[OperatorAction] = field(default_factory=list)
    blocked_actions: list[OperatorAction] = field(default_factory=list)
    safety_checks: list[OperatorSafetyCheck] = field(default_factory=list)
    trust_facts: list[OperatorTrustFact] = field(default_factory=list)
    out_of_app_instruction: str | None = None
    is_terminal: bool = False
    unknown_state_warning: str | None = None
    # Additive, derived, display-only (item 16). Populated by
    # compute_operator_state after the state is selected; None only if an
    # OperatorState is built directly without going through that entry point.
    phase: RunPhase | None = None
    narrative: OperatorNarrative | None = None


@dataclass(frozen=True)
class OperatorStateContext:
    """
    Pure inputs for compute_operator_state.

    API wiring should adapt already-loaded run/chunk/read-model data into this
    shape. Eligibility decisions may be any object with the common attributes
    used here (eligible, reason, status_code, pr_mode, blocked_requirements).
    """

    run_status: str | None = None
    chunk_plan_status: str | None = None
    chunk_plan_awaiting_approval: bool = False
    plan_approved_not_executed: bool = False
    is_running: bool = False
    is_stalled: bool = False
    is_terminal: bool = False
    terminal_status: str | None = None

    wrong_branch: bool = False
    wrong_branch_context: str | None = None
    wrong_branch_detail: str | None = None

    pending_scope_expansion: bool = False
    scope_expansion_rejected: bool = False
    scope_expansion_approve_decision: Any | None = None
    stale_patch_failure_present: bool = False

    memory_conflict_pending: bool = False

    patch_failure_present: bool = False
    patch_retry_decision: Any | None = None
    # The PatchFailureType (string value) of the patch failure being surfaced,
    # threaded from the persisted PatchFailureReport so the run-level copy is
    # failure-type-aware (#40B). None when the type is unknown/unavailable; the
    # family mapping then falls back to the safe "unexpected failure" copy. This
    # only selects user-facing copy — it grants no authority and never changes
    # retry eligibility (which stays governed by evaluate_patch_retry_eligibility).
    failure_type: str | None = None

    test_verdict: str | None = None
    test_ack_state: str | None = None
    final_ack_decision: Any | None = None
    review_ack_state: str | None = None
    final_review_ack_decision: Any | None = None

    chunk_awaiting_approval: bool = False
    recovered_scope_retry_awaiting_chunk_approval: bool = False

    final_approval_available: bool = False
    final_approval_blocked: bool = False
    final_approval_blocked_reason: str | None = None

    pr_created: bool = False
    pr_ready: bool = False
    pr_mode: str | None = None
    pr_decision: Any | None = None

    # A prior push/PR attempt failed for this run (#31B). When set, the panel
    # must say so honestly instead of reusing the "Create pull request" ready
    # state. The retry path is the existing /push-pr route, so retry stays
    # available. failure_* carry the classified reason/next action for display.
    push_failed: bool = False
    push_failure_summary: str | None = None
    push_failure_next_action: str | None = None
    push_failure_retryable: bool = True

    local_only_manual_push: bool = False
    unknown: bool = False


def compute_operator_state(context: OperatorStateContext) -> OperatorState:
    """
    Compute the current operator state from caller-supplied observations.

    Precedence follows docs/design/operator-state-attention-panel.md. This is a
    display/read helper only; every mutating route must still revalidate fresh
    state under its existing guard/lock flow.

    Item 16 wraps the selected state with an additive, derived `phase` and
    `narrative` (both display-only). The pre-existing fields are unchanged.
    """
    state = _select_state(context)
    return replace(
        state,
        phase=project_phase(state, context),
        narrative=build_narrative(state),
    )


def _select_state(context: OperatorStateContext) -> OperatorState:
    """Select the displayed OperatorState by precedence (the pre-item-16 body).

    Unchanged from the original compute_operator_state: same branches, same
    order, same returned states. Kept as its own function so phase/narrative
    derivation can wrap it without editing any state builder.
    """
    if context.wrong_branch:
        return _wrong_branch_state(context)

    if context.pending_scope_expansion:
        return _pending_scope_expansion_state(context)

    if context.scope_expansion_rejected:
        return _scope_expansion_rejected_state(context)

    if context.memory_conflict_pending:
        return _memory_conflict_state(context)

    if context.patch_failure_present:
        return _patch_failure_state(context)

    review_ack_state = _effective_review_ack_state(context)
    if (
        context.chunk_awaiting_approval
        or context.recovered_scope_retry_awaiting_chunk_approval
    ) and review_ack_state in {"missing", "stale"}:
        return _review_ack_required_state(
            context,
            review_ack_state,
            at_chunk_gate=True,
        )

    # Chunk approval outranks the weak/no-test acknowledgement gate. While a
    # chunk is awaiting human review/approval, the immediate next action is to
    # review that change; the acknowledgement matters at the final-approval
    # gate, AFTER the chunk is approved. The weak/no-test signal is not lost —
    # it still surfaces in this state's safety checks and as a future block on
    # final approval.
    if context.chunk_awaiting_approval or context.recovered_scope_retry_awaiting_chunk_approval:
        return _chunk_approval_state(context)

    ack_state = _effective_ack_state(context)
    if review_ack_state in {"missing", "stale"}:
        return _review_ack_required_state(
            context,
            review_ack_state,
            at_chunk_gate=False,
        )
    if ack_state in {"missing", "stale"}:
        return _test_ack_required_state(context, ack_state)

    if context.final_approval_available:
        return _final_approval_available_state(context)

    if context.final_approval_blocked:
        return _final_approval_blocked_state(context)

    if context.pr_created:
        return _pr_created_state(context)

    # A recorded push/PR failure must read as a failure, never as "ready to
    # create a PR" (#31B). This outranks the ready/local-only branches below
    # because those would otherwise mask push_failed as an offer to push.
    if context.push_failed:
        return _push_failed_state(context)

    if context.local_only_manual_push or _pr_mode(context) == "local_only":
        if context.pr_ready or _decision_eligible(context.pr_decision):
            return _local_only_state(context)

    if context.pr_ready or _decision_eligible(context.pr_decision):
        return _pr_ready_state(context)

    if context.chunk_plan_awaiting_approval:
        return _chunk_plan_awaiting_approval_state(context)

    if context.plan_approved_not_executed:
        return _plan_approved_not_executed_state(context)

    if context.is_running:
        return _running_state(context)

    if context.is_stalled:
        return _stalled_state(context)

    if context.is_terminal:
        return _terminal_state(context)

    if context.unknown:
        return _unknown_state()

    if context.test_verdict == "strong":
        return _strong_tests_state(context)

    return _unknown_state()


def project_phase(
    state: OperatorState, context: OperatorStateContext
) -> RunPhase:
    """Project the displayed OperatorState onto one user-facing RunPhase (item 16).

    Pure and display-only. It classifies the ALREADY-SELECTED state by its
    visible properties (so it can never disagree with the state the operator
    sees) plus two run-status splits. The unknown/fallback state and every
    failure/anomaly map to NEEDS_ATTENTION — the fail-safe default: a phase is
    never the optimistic guess, so a degraded or stuck read never reads as
    healthy.
    """
    # Fail-safe: the unknown/unmapped fallback is always NEEDS_ATTENTION.
    if state.unknown_state_warning is not None:
        return RunPhase.NEEDS_ATTENTION
    # Terminal: success -> Done; failed/rejected -> Needs attention (D1).
    if state.is_terminal:
        return _terminal_phase(context)
    # The system is busy: plan formation -> Planning, execution/push -> Working.
    if state.waiting_on is OperatorWaitingOn.SYSTEM:
        return _system_phase(context)
    # Nothing for a human and not terminal (local-only done, PR created) -> Done.
    if state.waiting_on is OperatorWaitingOn.NOBODY:
        return RunPhase.DONE
    # waiting_on is HUMAN below.
    # A failed push still needs a human even though it offers a retry action.
    if context.push_failed:
        return RunPhase.NEEDS_ATTENTION
    # No available action (blocked/anomaly: wrong branch, scope rejected,
    # final-approval blocked, stalled, non-retryable failure) -> Needs attention.
    if state.decision_type is OperatorDecisionType.NONE:
        return RunPhase.NEEDS_ATTENTION
    # A retryable patch failure is a failure first, despite offering retry (D1).
    if state.primary_action is not None and state.primary_action.id == "retry_patch":
        return RunPhase.NEEDS_ATTENTION
    # Everything else is an expected human gate / acknowledgement / decision.
    return RunPhase.WAITING_FOR_YOU


def _terminal_phase(context: OperatorStateContext) -> RunPhase:
    status = context.terminal_status or context.run_status
    if status == RunStatus.COMPLETE:
        return RunPhase.DONE
    # failed / rejected / final_rejected -> Needs attention (D1). There is no
    # explicit cancel/stop terminal status today; if one is added, route it to
    # RunPhase.STOPPED here.
    return RunPhase.NEEDS_ATTENTION


def _system_phase(context: OperatorStateContext) -> RunPhase:
    # The initial pre-execution pipeline (triage/planner forming the plan) runs
    # under RunStatus.RUNNING; chunk execution and push are RUNNING_CHUNKS /
    # PUSHING. The strong-tests fallback is post-execution, so it lands in
    # Working too.
    if context.run_status == RunStatus.RUNNING:
        return RunPhase.PLANNING
    return RunPhase.WORKING


def build_narrative(state: OperatorState) -> OperatorNarrative:
    """Structure the state's curated copy + computed actions into what/why/next.

    Display-only and additive (item 16): it reuses the backend-owned
    title/explanation verbatim (no new prose, so it cannot introduce a
    correctness claim or leak raw evidence) and derives whats_next ONLY from the
    available actions (primary, then neutral, then secondary), so it can never
    advertise a blocked or nonexistent action.
    """
    available: list[OperatorAction] = []
    if state.primary_action is not None:
        available.append(state.primary_action)
    available.extend(state.neutral_actions)
    available.extend(state.secondary_actions)
    return OperatorNarrative(
        what_happened=state.title,
        why=state.explanation,
        whats_next=tuple(action.label for action in available),
    )


def safety_check(
    id: str,
    label: str,
    status: OperatorSafetyCheckStatus,
    detail: str,
) -> OperatorSafetyCheck:
    return OperatorSafetyCheck(id=id, label=label, status=status, detail=detail)


def trust_fact(id: str, label: str, detail: str) -> OperatorTrustFact:
    return OperatorTrustFact(id=id, label=label, detail=detail)


def _state(
    *,
    title: str,
    explanation: str,
    waiting_on: OperatorWaitingOn,
    decision_type: OperatorDecisionType,
    primary_action: OperatorAction | None = None,
    neutral_actions: list[OperatorAction] | None = None,
    secondary_actions: list[OperatorAction] | None = None,
    blocked_actions: list[OperatorAction] | None = None,
    safety_checks: list[OperatorSafetyCheck] | None = None,
    trust_facts: list[OperatorTrustFact] | None = None,
    out_of_app_instruction: str | None = None,
    is_terminal: bool = False,
    unknown_state_warning: str | None = None,
) -> OperatorState:
    return OperatorState(
        title=title,
        explanation=explanation,
        waiting_on=waiting_on,
        decision_type=decision_type,
        primary_action=primary_action,
        neutral_actions=neutral_actions or [],
        secondary_actions=secondary_actions or [],
        blocked_actions=blocked_actions or [],
        safety_checks=safety_checks or _base_safety_checks(),
        trust_facts=trust_facts or [],
        out_of_app_instruction=out_of_app_instruction,
        is_terminal=is_terminal,
        unknown_state_warning=unknown_state_warning,
    )


def _action(
    id: str,
    label: str,
    intent: str,
    *,
    severity: OperatorActionSeverity = OperatorActionSeverity.NORMAL,
    enabled: bool = True,
    blocked_reason: str | None = None,
) -> OperatorAction:
    return OperatorAction(
        id=id,
        label=label,
        intent=intent,
        severity=severity,
        enabled=enabled,
        blocked_reason=blocked_reason,
    )


def _blocked_action(id: str, label: str, reason: str) -> OperatorAction:
    return _action(
        id,
        label,
        "Blocked until Pipewright's current safety conditions are satisfied.",
        enabled=False,
        blocked_reason=reason,
    )


def _base_safety_checks() -> list[OperatorSafetyCheck]:
    return [
        safety_check(
            "branch",
            "Branch",
            OperatorSafetyCheckStatus.NOT_EVALUATED,
            "Branch safety has not been evaluated for this display state.",
        ),
        safety_check(
            "tests",
            "Tests",
            OperatorSafetyCheckStatus.NOT_EVALUATED,
            "Runtime test validation has not run for this display state.",
        ),
        safety_check(
            "final_approval",
            "Final approval",
            OperatorSafetyCheckStatus.NOT_EVALUATED,
            "Final approval has not been evaluated for this display state.",
        ),
    ]


def _tests_check(context: OperatorStateContext) -> OperatorSafetyCheck:
    verdict = context.test_verdict
    if verdict == "strong":
        return safety_check(
            "tests",
            "Tests",
            OperatorSafetyCheckStatus.PASSED,
            "Meaningful tests ran and passed according to Pipewright's process rules; this does not prove code correctness.",
        )
    if verdict in {"weak", "none"}:
        detail = (
            "Runtime validation is weak or absent and requires human acknowledgement."
        )
        return safety_check("tests", "Tests", OperatorSafetyCheckStatus.WEAK, detail)
    if verdict == "unknown":
        return safety_check(
            "tests",
            "Tests",
            OperatorSafetyCheckStatus.NOT_EVALUATED,
            "Pipewright could not confirm whether the command ran a test suite.",
        )
    return safety_check(
        "tests",
        "Tests",
        OperatorSafetyCheckStatus.NOT_EVALUATED,
        "Tests have not run yet.",
    )


def _ack_check(state: str | None) -> OperatorSafetyCheck:
    if state == "current":
        return safety_check(
            "test_acknowledgement",
            "Test acknowledgement",
            OperatorSafetyCheckStatus.PASSED,
            "The weak/no-test acknowledgement matches the current diff.",
        )
    if state == "stale":
        return safety_check(
            "test_acknowledgement",
            "Test acknowledgement",
            OperatorSafetyCheckStatus.FAILED,
            "The previous acknowledgement is stale for the current diff.",
        )
    if state == "missing":
        return safety_check(
            "test_acknowledgement",
            "Test acknowledgement",
            OperatorSafetyCheckStatus.FAILED,
            "Weak or no-test validation has not been acknowledged.",
        )
    return safety_check(
        "test_acknowledgement",
        "Test acknowledgement",
        OperatorSafetyCheckStatus.NOT_APPLICABLE,
        "No weak/no-test acknowledgement is required.",
    )


def _review_ack_check(state: str | None) -> OperatorSafetyCheck:
    if state == "current":
        return safety_check(
            "review_acknowledgement",
            "Review acknowledgement",
            OperatorSafetyCheckStatus.PASSED,
            "The reviewer-finding acknowledgement matches the current diff.",
        )
    if state == "stale":
        return safety_check(
            "review_acknowledgement",
            "Review acknowledgement",
            OperatorSafetyCheckStatus.FAILED,
            "The previous reviewer-finding acknowledgement is stale for the current diff.",
        )
    if state == "missing":
        return safety_check(
            "review_acknowledgement",
            "Review acknowledgement",
            OperatorSafetyCheckStatus.FAILED,
            "Current high-severity reviewer findings have not been acknowledged.",
        )
    return safety_check(
        "review_acknowledgement",
        "Review acknowledgement",
        OperatorSafetyCheckStatus.NOT_APPLICABLE,
        "No reviewer-finding acknowledgement is required.",
    )


def _effective_ack_state(context: OperatorStateContext) -> str | None:
    if context.test_ack_state:
        return context.test_ack_state
    decision = context.final_ack_decision
    if decision is not None and not _decision_eligible(decision):
        blocked = getattr(decision, "blocked_requirements", ()) or ()
        states = {getattr(item, "state", None) for item in blocked}
        if "stale" in states:
            return "stale"
        return "missing"
    if context.test_verdict in {"weak", "none"} and _decision_eligible(decision):
        return "current"
    return None


def _effective_review_ack_state(context: OperatorStateContext) -> str | None:
    if context.review_ack_state:
        return context.review_ack_state
    decision = context.final_review_ack_decision
    if decision is not None and not _decision_eligible(decision):
        blocked = getattr(decision, "blocked_requirements", ()) or ()
        states = {getattr(item, "state", None) for item in blocked}
        if "stale" in states:
            return "stale"
        return "missing"
    return None


def _decision_eligible(decision: Any | None) -> bool:
    return bool(getattr(decision, "eligible", False)) if decision is not None else False


def _decision_reason(decision: Any | None) -> str | None:
    if decision is None:
        return None
    reason = getattr(decision, "reason", None)
    return str(reason) if reason else None


def _pr_mode(context: OperatorStateContext) -> str | None:
    if context.pr_mode:
        return context.pr_mode
    decision = context.pr_decision
    mode = getattr(decision, "pr_mode", None)
    return str(mode) if mode else None


def _wrong_branch_state(context: OperatorStateContext) -> OperatorState:
    scope_context = (
        context.wrong_branch_context == "scope_approval"
        or context.pending_scope_expansion
    )
    title = (
        "Scope approval is blocked by branch state"
        if scope_context
        else "Retry is blocked by branch state"
    )
    detail = context.wrong_branch_detail or (
        "Pipewright cannot verify that HEAD is on the expected run branch."
    )
    return _state(
        title=title,
        explanation=(
            "Pipewright cannot safely offer this mutating action while the "
            "repository branch is wrong, detached, or unverifiable."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.NONE,
        blocked_actions=[
            _blocked_action("retry_patch", "Retry code change", detail),
            _blocked_action("approve_scope_expansion", "Approve scope expansion and retry", detail),
            _blocked_action("approve_final", "Approve final", detail),
        ],
        safety_checks=[
            safety_check("branch", "Branch", OperatorSafetyCheckStatus.FAILED, detail),
            _tests_check(context),
        ],
        out_of_app_instruction=(
            context.wrong_branch_detail
            or "Checkout the expected Pipewright branch, then refresh before proceeding."
        ),
    )


def _pending_scope_expansion_state(context: OperatorStateContext) -> OperatorState:
    return _state(
        title="Scope expansion needs review",
        explanation=(
            "The previous attempt tried to touch files outside the approved "
            "chunk scope. Approving scope only allows a retry under the expanded "
            "allowlist; it does not approve code."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.RISK_DECISION,
        neutral_actions=[
            _action(
                "approve_scope_expansion",
                "Approve scope expansion and retry",
                "Authorize retrying the failed chunk with the approved extra files.",
                severity=OperatorActionSeverity.CAUTION,
            ),
            _action(
                "reject_scope_expansion",
                "Reject scope expansion",
                "Reject the requested expanded scope; no retry or commit occurs.",
                severity=OperatorActionSeverity.CAUTION,
            ),
        ],
        blocked_actions=[
            _blocked_action(
                "retry_patch",
                "Retry code change",
                "A pending scope decision must be resolved before the code change can be retried.",
            ),
            _blocked_action("approve_chunk", "Approve chunk", "The chunk is still failed."),
            _blocked_action("approve_final", "Approve final", "Scope expansion is unresolved."),
        ],
        safety_checks=[
            safety_check(
                "scope",
                "Scope",
                OperatorSafetyCheckStatus.FAILED,
                "The previous attempt exceeded the approved chunk scope.",
            ),
            _tests_check(context),
        ],
        trust_facts=[
            trust_fact(
                "pending_scope_expansion",
                "Pending scope expansion",
                "The current safe path is a human scope decision, not normal retry.",
            )
        ],
    )


def _scope_expansion_rejected_state(context: OperatorStateContext) -> OperatorState:
    return _state(
        title="Scope expansion was rejected",
        explanation=(
            "The requested expanded scope was rejected. The chunk remains failed "
            "until the operator resolves it manually or starts a new safe path."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.NONE,
        blocked_actions=[
            _blocked_action("approve_scope_expansion", "Approve scope expansion and retry", "The request was rejected."),
            _blocked_action("retry_patch", "Retry code change", "The failed chunk needs manual review after scope rejection."),
            _blocked_action("approve_final", "Approve final", "A failed chunk remains unresolved."),
        ],
        safety_checks=[
            safety_check("scope", "Scope", OperatorSafetyCheckStatus.FAILED, "Scope expansion was rejected."),
            safety_check("patch", "Code change", OperatorSafetyCheckStatus.FAILED, "The chunk remains failed."),
            _tests_check(context),
        ],
        out_of_app_instruction="Investigate the failed chunk before proceeding.",
    )


def _memory_conflict_state(context: OperatorStateContext) -> OperatorState:
    return _state(
        title="Resolve memory conflict",
        explanation=(
            "Pipewright detected a memory conflict that requires a human decision "
            "before the run can safely continue."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.RISK_DECISION,
        neutral_actions=[
            _action("approve_memory_conflict", "Approve memory change", "Approve the pending memory-conflict gate.", severity=OperatorActionSeverity.CAUTION),
            _action("reject_memory_conflict", "Reject memory change", "Reject the pending memory-conflict gate.", severity=OperatorActionSeverity.CAUTION),
        ],
        blocked_actions=[
            _blocked_action("approve_final", "Approve final", "A memory conflict is pending."),
            _blocked_action("create_pr", "Create PR", "A memory conflict is pending."),
        ],
        safety_checks=[
            safety_check("memory_conflict", "Memory conflict", OperatorSafetyCheckStatus.FAILED, "A memory conflict requires human decision."),
            _tests_check(context),
        ],
    )


@dataclass(frozen=True)
class _PatchFailureFamily:
    """User-facing copy for a group of PatchFailureType values (#40B).

    Pure presentation only. The title is the headline and always describes *what
    happened* — never retry-availability (that is a separate sub-line). Every
    explanation ends with the always-true assurance that nothing was committed.
    The tests safety-check is family-specific so the panel never claims tests did
    not run when they ran and failed (TEST_FAILURE_AFTER_APPLY), and never claims
    tests passed.
    """

    title: str
    explanation: str
    patch_detail: str
    tests_status: OperatorSafetyCheckStatus
    tests_detail: str
    approve_final_reason: str


# Families mirror docs/design/failure-state-ux-cleanup.md §3, keyed by the
# PatchFailureType *string value* so this stays decoupled from the enum and any
# unrecognised/None value fails safe to the "unexpected failure" copy.
_FAMILY_COULD_NOT_APPLY = _PatchFailureFamily(
    title="Code change couldn't be applied",
    explanation=(
        "Pipewright generated a change, but it didn't match the current files in "
        "your repo, so nothing was applied. Nothing was committed."
    ),
    patch_detail="Pipewright could not apply the generated change.",
    tests_status=OperatorSafetyCheckStatus.NOT_EVALUATED,
    tests_detail="Tests didn't run because the change was never applied.",
    approve_final_reason="The requested code change has not been applied yet.",
)
_FAMILY_TEST_FAILURE = _PatchFailureFamily(
    title="Tests failed after the change was applied",
    explanation=(
        "Pipewright applied the change and ran your tests, but the tests failed, "
        "so the change was rolled back. Nothing was committed."
    ),
    patch_detail="The change applied, but tests failed afterward, so it was rolled back.",
    tests_status=OperatorSafetyCheckStatus.FAILED,
    tests_detail="Tests ran and failed, so the change wasn't kept.",
    approve_final_reason=(
        "The change was rolled back after tests failed, so there is nothing to approve."
    ),
)
_FAMILY_TEST_REGRESSION = _PatchFailureFamily(
    title="Tests found a regression",
    explanation=(
        "What happened: Pipewright applied the change and the test harness ran, "
        "but project tests failed. Why: this points to a real code regression, "
        "not a broken test command. What next: review the failing tests before "
        "trying a different fix. Nothing was committed."
    ),
    patch_detail="The change applied, but tests found a regression, so it was rolled back.",
    tests_status=OperatorSafetyCheckStatus.FAILED,
    tests_detail="Tests ran and failed, so the change wasn't kept.",
    approve_final_reason=(
        "The change was rolled back after tests found a regression, so there "
        "is nothing to approve."
    ),
)
_FAMILY_HARNESS_ERROR = _PatchFailureFamily(
    title="Test harness failed",
    explanation=(
        "What happened: Pipewright applied the change, but the test command did "
        "not produce a trustworthy result. Why: the runner may have crashed, "
        "been missing, collected no tests, or timed out; a timeout can also mean "
        "the change introduced a loop. What next: fix the test environment or "
        "retry once it is stable. Nothing was committed."
    ),
    patch_detail="The change applied, but the test harness failed, so it was rolled back.",
    tests_status=OperatorSafetyCheckStatus.FAILED,
    tests_detail="The test command failed before Pipewright could trust the result.",
    approve_final_reason=(
        "The change was rolled back after the test harness failed, so there is nothing to approve."
    ),
)
_FAMILY_BLOCKED_SCOPE = _PatchFailureFamily(
    title="Change was blocked — outside approved scope",
    explanation=(
        "The change tried to edit files outside this chunk's approved scope, so "
        "Pipewright refused it. This is a safety stop. Nothing was committed."
    ),
    patch_detail="The change was blocked for trying to edit files outside the approved scope.",
    tests_status=OperatorSafetyCheckStatus.NOT_EVALUATED,
    tests_detail="Tests didn't run because the change was blocked before applying.",
    approve_final_reason="The requested code change has not been applied yet.",
)
_FAMILY_BLOCKED_FORBIDDEN = _PatchFailureFamily(
    title="Change was blocked — protected file",
    explanation=(
        "The change tried to modify a protected file, so Pipewright refused it. "
        "Nothing was committed."
    ),
    patch_detail="The change was blocked for trying to modify a protected file.",
    tests_status=OperatorSafetyCheckStatus.NOT_EVALUATED,
    tests_detail="Tests didn't run because the change was blocked before applying.",
    approve_final_reason="The requested code change has not been applied yet.",
)
_FAMILY_NO_CHANGE = _PatchFailureFamily(
    title="No change was produced",
    explanation=(
        "Pipewright's change produced no edits — it may already be present in the "
        "repo. Nothing was committed."
    ),
    patch_detail="The change produced no edits to apply.",
    tests_status=OperatorSafetyCheckStatus.NOT_APPLICABLE,
    tests_detail="Tests didn't run because there was no change to test.",
    approve_final_reason="The requested code change has not been applied yet.",
)
_FAMILY_REPO_NOT_READY = _PatchFailureFamily(
    title="Repo wasn't ready for this change",
    explanation=(
        "Your working tree had uncommitted changes, so Pipewright didn't apply "
        "anything — it needs a clean tree to roll back safely. Commit or stash "
        "your changes, then retry. Nothing was committed."
    ),
    patch_detail="The working tree was not clean, so Pipewright did not apply the change.",
    tests_status=OperatorSafetyCheckStatus.NOT_EVALUATED,
    tests_detail="Tests didn't run because the change was never applied.",
    approve_final_reason="The requested code change has not been applied yet.",
)
_FAMILY_UNEXPECTED = _PatchFailureFamily(
    title="Something went wrong applying this change",
    explanation=(
        "An unexpected error stopped this change. It was rolled back and nothing "
        "was committed. See the diagnostic details below."
    ),
    patch_detail="An unexpected error stopped this change.",
    tests_status=OperatorSafetyCheckStatus.NOT_EVALUATED,
    tests_detail="Tests did not run, or Pipewright could not confirm whether they ran.",
    approve_final_reason="The requested code change has not been applied yet.",
)

_PATCH_FAILURE_FAMILY_BY_TYPE: dict[str, _PatchFailureFamily] = {
    "PATCH_DOES_NOT_APPLY": _FAMILY_COULD_NOT_APPLY,
    "PATCH_MALFORMED": _FAMILY_COULD_NOT_APPLY,
    "PATCH_PARTIAL_APPLY_BLOCKED": _FAMILY_COULD_NOT_APPLY,
    "TARGET_MISSING": _FAMILY_COULD_NOT_APPLY,
    "STALE_INDEX_OR_FILE_CHANGED": _FAMILY_COULD_NOT_APPLY,
    "TEST_FAILURE_AFTER_APPLY": _FAMILY_TEST_FAILURE,
    "TEST_REGRESSION": _FAMILY_TEST_REGRESSION,
    "HARNESS_ERROR": _FAMILY_HARNESS_ERROR,
    "SCOPE_VIOLATION": _FAMILY_BLOCKED_SCOPE,
    "FORBIDDEN_FILE": _FAMILY_BLOCKED_FORBIDDEN,
    "NO_CHANGES": _FAMILY_NO_CHANGE,
    "DIRTY_WORKTREE": _FAMILY_REPO_NOT_READY,
    "UNKNOWN_PATCH_FAILURE": _FAMILY_UNEXPECTED,
}


def _patch_failure_family(failure_type: str | None) -> _PatchFailureFamily:
    """Map a PatchFailureType string to its user-facing family copy.

    Fails safe: unknown or missing types resolve to the generic "unexpected
    failure" copy, which never claims tests ran or passed.
    """
    if not failure_type:
        return _FAMILY_UNEXPECTED
    return _PATCH_FAILURE_FAMILY_BY_TYPE.get(failure_type, _FAMILY_UNEXPECTED)


def _patch_failure_state(context: OperatorStateContext) -> OperatorState:
    decision = context.patch_retry_decision
    family = _patch_failure_family(context.failure_type)
    patch_check = safety_check(
        "patch",
        "Code change",
        OperatorSafetyCheckStatus.FAILED,
        family.patch_detail,
    )
    tests_check = safety_check(
        "tests", "Tests", family.tests_status, family.tests_detail
    )

    if _decision_eligible(decision):
        return _state(
            title=family.title,
            explanation=family.explanation + " You can try applying the change again.",
            waiting_on=OperatorWaitingOn.HUMAN,
            decision_type=OperatorDecisionType.PROGRESS,
            primary_action=_action(
                "retry_patch",
                "Retry code change",
                "Try applying the generated change again, using the files already "
                "approved for this chunk. This may succeed or fail again.",
                severity=OperatorActionSeverity.CAUTION,
            ),
            blocked_actions=[
                _blocked_action("approve_chunk", "Approve chunk", "The chunk is failed."),
                _blocked_action(
                    "approve_final",
                    "Approve final",
                    family.approve_final_reason,
                ),
            ],
            safety_checks=[patch_check, tests_check],
        )

    # Retry-unavailability is an action property, not what happened: it never
    # enters the title. The raw backend reason is mapped to plain language (#40C)
    # so no stable identifier (e.g. disallowed_failure_type) shows as primary
    # copy; a plain-language sub-line is also appended to the explanation. The
    # raw failure_type stays visible as a diagnostic badge/details elsewhere.
    reason = human_retry_ineligible_reason(_decision_reason(decision))
    return _state(
        title=family.title,
        explanation=(
            family.explanation
            + " Pipewright cannot retry this automatically from the current state"
            " — review the details below to decide the next step."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.NONE,
        blocked_actions=[
            _blocked_action("retry_patch", "Retry code change", reason),
            _blocked_action("approve_chunk", "Approve chunk", "The chunk is failed."),
            _blocked_action(
                "approve_final",
                "Approve final",
                family.approve_final_reason,
            ),
        ],
        safety_checks=[patch_check, tests_check],
    )


def _test_ack_required_state(context: OperatorStateContext, ack_state: str) -> OperatorState:
    stale = ack_state == "stale"
    title = "Test acknowledgement is stale" if stale else "Acknowledge weak validation"
    explanation = (
        "The previous acknowledgement was made for a different diff. The current "
        "change needs a fresh acknowledgement before final approval."
        if stale
        else "Tests did not meaningfully run, or no tests were configured. The "
        "operator must acknowledge this before final approval can proceed."
    )
    return _state(
        title=title,
        explanation=explanation,
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.RISK_DECISION,
        neutral_actions=[
            _action(
                "acknowledge_test_validation",
                "Acknowledge weak/no-test validation",
                "Acknowledge that validation is weak or absent for the current diff.",
                severity=OperatorActionSeverity.CAUTION,
            )
        ],
        blocked_actions=[
            _blocked_action("approve_final", "Approve final", "Weak/no-test validation acknowledgement is not current."),
            _blocked_action("create_pr", "Create PR", "Final approval is blocked by test validation acknowledgement."),
        ],
        safety_checks=[
            _tests_check(context),
            _ack_check(ack_state),
            safety_check("final_approval", "Final approval", OperatorSafetyCheckStatus.FAILED, "Final approval is blocked by test validation acknowledgement."),
        ],
    )


def _review_ack_required_state(
    context: OperatorStateContext,
    ack_state: str,
    *,
    at_chunk_gate: bool,
) -> OperatorState:
    stale = ack_state == "stale"
    title = (
        "Reviewer acknowledgement is stale"
        if stale
        else "Acknowledge reviewer findings"
    )
    explanation = (
        "The previous reviewer acknowledgement was made for a different diff. "
        "The current change needs a fresh acknowledgement before approval."
        if stale
        else "The current review contains high-severity requirement-mismatch or "
        "security findings. A human must acknowledge those findings before "
        "approving this change."
    )
    blocked = [
        _blocked_action(
            "approve_chunk" if at_chunk_gate else "approve_final",
            "Approve chunk" if at_chunk_gate else "Approve final",
            "Reviewer-finding acknowledgement is not current.",
        ),
        _blocked_action(
            "create_pr",
            "Create PR",
            "Approval is blocked by reviewer-finding acknowledgement.",
        ),
    ]
    if at_chunk_gate:
        blocked.insert(
            1,
            _blocked_action(
                "approve_final",
                "Approve final",
                "A chunk is awaiting reviewer-finding acknowledgement.",
            ),
        )
    return _state(
        title=title,
        explanation=explanation,
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.RISK_DECISION,
        neutral_actions=[
            _action(
                "acknowledge_review_findings",
                "Acknowledge reviewer findings",
                "Acknowledge current high-severity reviewer findings for the current diff.",
                severity=OperatorActionSeverity.CAUTION,
            )
        ],
        blocked_actions=blocked,
        safety_checks=[
            _tests_check(context),
            _ack_check(_effective_ack_state(context)),
            _review_ack_check(ack_state),
            safety_check(
                "approval",
                "Approval",
                OperatorSafetyCheckStatus.FAILED,
                "Approval is blocked by reviewer-finding acknowledgement.",
            ),
        ],
    )


def _chunk_approval_state(context: OperatorStateContext) -> OperatorState:
    recovered = context.recovered_scope_retry_awaiting_chunk_approval
    ack_state = _effective_ack_state(context)
    ack_pending = ack_state in {"missing", "stale"}
    # When a weak/no-test acknowledgement is still outstanding, keep that signal
    # visible: surface it in safety checks and as part of why final approval is
    # blocked — but chunk review/approval remains the primary action.
    final_blocked_reason = (
        "A chunk is awaiting approval, and weak/no-test validation must still be "
        "acknowledged before final approval."
        if ack_pending
        else "A chunk is awaiting approval."
    )
    safety_checks = [
        safety_check("chunk_approval", "Chunk approval", OperatorSafetyCheckStatus.NOT_EVALUATED, "The chunk is waiting for human approval."),
        _tests_check(context),
    ]
    if ack_pending:
        safety_checks.append(_ack_check(ack_state))
    return _state(
        # The recovered flag fires for any recovered patch review (normal manual
        # retry OR scope-expansion approve-and-retry), so the copy stays neutral
        # rather than implying scope expansion. It must not imply correctness.
        title="Review recovered code change" if recovered else "Review chunk change",
        explanation=(
            "Retry produced a change. Review the actual code before it is committed."
            if recovered
            else "The chunk produced a change and is waiting for human review before commit."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.PROGRESS,
        primary_action=_action("approve_chunk", "Approve chunk", "Approve and commit this chunk."),
        blocked_actions=[
            _blocked_action("approve_final", "Approve final", final_blocked_reason),
            _blocked_action("create_pr", "Create PR", "Final approval is not complete."),
        ],
        safety_checks=safety_checks,
    )


def _final_approval_available_state(context: OperatorStateContext) -> OperatorState:
    ack_state = _effective_ack_state(context)
    review_ack_state = _effective_review_ack_state(context)
    checks = [
        _tests_check(context),
        _ack_check(ack_state),
        _review_ack_check(review_ack_state),
        safety_check("final_approval", "Final approval", OperatorSafetyCheckStatus.NOT_EVALUATED, "All required gates are satisfied for final approval."),
    ]
    return _state(
        title="Review final result",
        explanation=(
            "All required gates for final approval are satisfied. The operator "
            "may approve the final result."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.PROGRESS,
        primary_action=_action("approve_final", "Approve final", "Approve the final run result."),
        blocked_actions=[
            _blocked_action("create_pr", "Create PR", "Final approval must complete before PR creation."),
        ],
        safety_checks=checks,
    )


def _final_approval_blocked_state(context: OperatorStateContext) -> OperatorState:
    reason = context.final_approval_blocked_reason or (
        "One or more required gates are not satisfied."
    )
    return _state(
        title="Final approval is blocked",
        explanation=(
            "Final approval cannot proceed until all required chunk, branch, "
            "scope, memory, and validation gates are satisfied."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.NONE,
        blocked_actions=[
            _blocked_action("approve_final", "Approve final", reason),
            _blocked_action("create_pr", "Create PR", "Final approval is blocked."),
        ],
        safety_checks=[
            _tests_check(context),
            _ack_check(_effective_ack_state(context)),
            _review_ack_check(_effective_review_ack_state(context)),
            safety_check("final_approval", "Final approval", OperatorSafetyCheckStatus.FAILED, reason),
        ],
    )


def _pr_ready_state(context: OperatorStateContext) -> OperatorState:
    mode = _pr_mode(context)
    if mode == "local_only":
        return _local_only_state(context)
    return _state(
        title="Create pull request",
        explanation=(
            "Final approval is complete and the branch is ready for Pipewright "
            "to push/create a PR through the configured GitHub path."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.PROGRESS,
        primary_action=_action("create_pr", "Create PR", "Push the approved branch and create or reuse a pull request."),
        blocked_actions=[
            _blocked_action("approve_final", "Approve final", "Final approval is already complete."),
        ],
        safety_checks=[
            safety_check("final_approval", "Final approval", OperatorSafetyCheckStatus.PASSED, "Final approval is complete."),
            safety_check("pr", "PR", OperatorSafetyCheckStatus.NOT_EVALUATED, "PR creation has not completed yet."),
        ],
    )


def _push_failed_state(context: OperatorStateContext) -> OperatorState:
    summary = context.push_failure_summary or (
        "Pipewright could not push the branch or create the pull request."
    )
    next_action = context.push_failure_next_action or (
        "Inspect the error detail, then retry the push."
    )
    # Final approval already passed to reach a push attempt; surface that as a
    # settled fact so the failure is clearly about the PR step, not approval.
    safety_checks = [
        safety_check(
            "final_approval",
            "Final approval",
            OperatorSafetyCheckStatus.PASSED,
            "Final approval is complete.",
        ),
        safety_check("pr", "PR", OperatorSafetyCheckStatus.FAILED, summary),
    ]
    if context.push_failure_retryable:
        return _state(
            title="Pull request could not be created",
            explanation=(
                f"{summary} Nothing was merged. {next_action} The branch may "
                "already be pushed; retrying reuses it and any existing PR."
            ),
            waiting_on=OperatorWaitingOn.HUMAN,
            decision_type=OperatorDecisionType.PROGRESS,
            primary_action=_action(
                "create_pr",
                "Retry push and PR",
                "Retry pushing the approved branch and creating or reusing the pull request.",
                severity=OperatorActionSeverity.CAUTION,
            ),
            safety_checks=safety_checks,
            out_of_app_instruction=next_action,
        )
    return _state(
        title="Pull request could not be created",
        explanation=(
            f"{summary} Nothing was merged. {next_action} This failure cannot be "
            "retried from the current state."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.NONE,
        blocked_actions=[
            _blocked_action("create_pr", "Retry push and PR", next_action),
        ],
        safety_checks=safety_checks,
        out_of_app_instruction=next_action,
    )


def _local_only_state(context: OperatorStateContext) -> OperatorState:
    return _state(
        title="Manual push required",
        explanation=(
            "The run completed locally. This project mode requires the operator "
            "to push or create a PR outside Pipewright."
        ),
        waiting_on=OperatorWaitingOn.NOBODY,
        decision_type=OperatorDecisionType.NONE,
        blocked_actions=[
            _blocked_action("create_pr", "Create PR", "This project is in local_only PR mode."),
        ],
        safety_checks=[
            safety_check("final_approval", "Final approval", OperatorSafetyCheckStatus.PASSED, "Final approval is complete."),
            safety_check("pr", "PR", OperatorSafetyCheckStatus.NOT_APPLICABLE, "Pipewright PR creation does not apply in local_only mode."),
        ],
        out_of_app_instruction="Push or create a pull request manually outside Pipewright.",
    )


def _pr_created_state(context: OperatorStateContext) -> OperatorState:
    return _state(
        title="Pull request is ready",
        explanation=(
            "A pull request already exists or was reused for this run. No further "
            "in-app action is required."
        ),
        waiting_on=OperatorWaitingOn.NOBODY,
        decision_type=OperatorDecisionType.NONE,
        blocked_actions=[
            _blocked_action("create_pr", "Create PR", "A pull request already exists for this run."),
        ],
        safety_checks=[
            safety_check("final_approval", "Final approval", OperatorSafetyCheckStatus.PASSED, "Final approval is complete."),
            safety_check("pr", "PR", OperatorSafetyCheckStatus.PASSED, "A pull request exists or was reused."),
        ],
        trust_facts=[
            trust_fact("pr_exists", "PR exists", "A PR is already recorded for this run.")
        ],
    )


def _chunk_plan_awaiting_approval_state(context: OperatorStateContext) -> OperatorState:
    return _state(
        title="Review the chunk plan",
        explanation=(
            "Pipewright needs human approval of the proposed chunk plan before "
            "it can edit files."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.PROGRESS,
        primary_action=_action("approve_plan", "Approve chunk plan", "Approve the proposed chunk plan."),
        blocked_actions=[
            _blocked_action("execute_chunks", "Execute approved chunks", "The chunk plan is not approved yet."),
            _blocked_action("approve_final", "Approve final", "No approved changes exist yet."),
        ],
        safety_checks=[
            safety_check("plan_approval", "Plan approval", OperatorSafetyCheckStatus.NOT_EVALUATED, "The chunk plan is awaiting human approval."),
            _tests_check(context),
            safety_check("pr", "PR", OperatorSafetyCheckStatus.NOT_APPLICABLE, "PR creation does not apply before execution."),
        ],
    )


def _plan_approved_not_executed_state(context: OperatorStateContext) -> OperatorState:
    return _state(
        title="Execute approved chunks",
        explanation=(
            "The plan is approved. Pipewright is waiting for the operator to "
            "start chunk execution."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.PROGRESS,
        primary_action=_action("execute_chunks", "Execute approved chunks", "Start executing the approved chunks."),
        blocked_actions=[
            _blocked_action("approve_chunk", "Approve chunk", "No chunk is awaiting approval yet."),
            _blocked_action("approve_final", "Approve final", "Chunks have not executed yet."),
        ],
        safety_checks=[
            safety_check("plan_approval", "Plan approval", OperatorSafetyCheckStatus.PASSED, "The chunk plan is approved."),
            safety_check("patch", "Code change", OperatorSafetyCheckStatus.NOT_EVALUATED, "No code change has been applied yet."),
            _tests_check(context),
        ],
    )


def _running_state(context: OperatorStateContext) -> OperatorState:
    return _state(
        title="Pipewright is running",
        explanation=(
            "A pipeline step is currently running. Wait for it to finish before "
            "taking another action."
        ),
        waiting_on=OperatorWaitingOn.SYSTEM,
        decision_type=OperatorDecisionType.NONE,
        blocked_actions=[
            _blocked_action("retry_patch", "Retry code change", "Pipewright is currently running."),
            _blocked_action("approve_final", "Approve final", "Pipewright is currently running."),
            _blocked_action("create_pr", "Create PR", "Pipewright is currently running."),
        ],
        safety_checks=[
            safety_check("active_stage", "Active stage", OperatorSafetyCheckStatus.NOT_EVALUATED, "A pipeline stage is currently running."),
            _tests_check(context),
        ],
    )


def _stalled_state(context: OperatorStateContext) -> OperatorState:
    return _state(
        title="Run may be stalled",
        explanation=(
            "Pipewright has been in a running state longer than expected based "
            "on durable persisted state. Investigate before proceeding."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.NONE,
        blocked_actions=[
            _blocked_action("retry_patch", "Retry code change", "The run may be stalled."),
            _blocked_action("approve_final", "Approve final", "The run may be stalled."),
            _blocked_action("create_pr", "Create PR", "The run may be stalled."),
        ],
        safety_checks=[
            safety_check("active_stage", "Active stage", OperatorSafetyCheckStatus.NOT_EVALUATED, "The active stage has not completed."),
            safety_check("stalled", "Stalled", OperatorSafetyCheckStatus.FAILED, "The run appears stalled from durable state."),
        ],
        out_of_app_instruction="Investigate the persisted run state before proceeding.",
    )


def _terminal_state(context: OperatorStateContext) -> OperatorState:
    status = context.terminal_status or context.run_status or "terminal"
    return _state(
        title="Run is complete",
        explanation=(
            "The run has reached a terminal state. No further Pipewright action "
            "is available for this run."
        ),
        waiting_on=OperatorWaitingOn.NOBODY,
        decision_type=OperatorDecisionType.NONE,
        blocked_actions=[
            _blocked_action("retry_patch", "Retry code change", f"The run is terminal ({status})."),
            _blocked_action("approve_final", "Approve final", f"The run is terminal ({status})."),
            _blocked_action("create_pr", "Create PR", f"The run is terminal ({status})."),
        ],
        safety_checks=[
            safety_check("terminal", "Terminal", OperatorSafetyCheckStatus.PASSED, f"The run status is {status}."),
            _tests_check(context),
        ],
        is_terminal=True,
    )


def _strong_tests_state(context: OperatorStateContext) -> OperatorState:
    # Fallback only: tests are strong but no earlier state matched. Crucially, the
    # chunk-awaiting-approval branch outranks this one and owns `approve_chunk`, so
    # by the time we reach here NO chunk is awaiting approval — recommending
    # `approve_chunk` would be stale/impossible (e.g. after a chunk is already
    # completed). No final-approval gate is pending either (that branch also
    # outranks this). So surface the strong-tests fact without an inapplicable
    # action; Pipewright will surface the next gate (final approval / finish) once
    # it is pending. Display-only — this grants no authority and gates nothing.
    return _state(
        title="Tests passed with strong validation",
        explanation=(
            "Meaningful tests ran and passed according to Pipewright's process "
            "rules. This does not prove code correctness. No chunk is waiting for "
            "your approval right now; Pipewright will surface the next step when "
            "it is ready."
        ),
        waiting_on=OperatorWaitingOn.SYSTEM,
        decision_type=OperatorDecisionType.NONE,
        safety_checks=[_tests_check(context), _ack_check(None)],
    )


def _unknown_state() -> OperatorState:
    return _state(
        title="Next safe action is unknown",
        explanation=UNKNOWN_STATE_MESSAGE,
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.NONE,
        blocked_actions=[
            _blocked_action("retry_patch", "Retry code change", UNKNOWN_STATE_MESSAGE),
            _blocked_action("approve_chunk", "Approve chunk", UNKNOWN_STATE_MESSAGE),
            _blocked_action("approve_final", "Approve final", UNKNOWN_STATE_MESSAGE),
            _blocked_action("create_pr", "Create PR", UNKNOWN_STATE_MESSAGE),
        ],
        safety_checks=[
            safety_check("unknown_state", "Unknown state", OperatorSafetyCheckStatus.FAILED, UNKNOWN_STATE_MESSAGE),
        ],
        unknown_state_warning=UNKNOWN_STATE_MESSAGE,
    )
