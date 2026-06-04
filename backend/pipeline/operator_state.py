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

from dataclasses import dataclass, field, is_dataclass
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1
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

    test_verdict: str | None = None
    test_ack_state: str | None = None
    final_ack_decision: Any | None = None

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

    # Chunk approval outranks the weak/no-test acknowledgement gate. While a
    # chunk is awaiting human review/approval, the immediate next action is to
    # review that change; the acknowledgement matters at the final-approval
    # gate, AFTER the chunk is approved. The weak/no-test signal is not lost —
    # it still surfaces in this state's safety checks and as a future block on
    # final approval.
    if context.chunk_awaiting_approval or context.recovered_scope_retry_awaiting_chunk_approval:
        return _chunk_approval_state(context)

    ack_state = _effective_ack_state(context)
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


def _patch_failure_state(context: OperatorStateContext) -> OperatorState:
    decision = context.patch_retry_decision
    if _decision_eligible(decision):
        return _state(
            title="Code change could not be applied",
            explanation=(
                "Pipewright generated a code change, but it could not apply that "
                "change to the current files in your repo. Nothing was committed, "
                "and tests did not run. You can try applying the change again."
            ),
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
                    "The requested code change has not been applied yet.",
                ),
            ],
            safety_checks=[
                safety_check(
                    "patch",
                    "Code change",
                    OperatorSafetyCheckStatus.FAILED,
                    "Pipewright could not apply the generated change.",
                ),
                _tests_check(context),
            ],
        )

    reason = _decision_reason(decision) or "The code change cannot be retried right now."
    return _state(
        title="Code change could not be applied — retry unavailable",
        explanation=(
            "Pipewright generated a code change but could not apply it, and it "
            "cannot retry from the current state. Nothing was committed, and tests "
            "did not run."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.NONE,
        blocked_actions=[
            _blocked_action("retry_patch", "Retry code change", reason),
            _blocked_action("approve_chunk", "Approve chunk", "The chunk is failed."),
            _blocked_action(
                "approve_final",
                "Approve final",
                "The requested code change has not been applied yet.",
            ),
        ],
        safety_checks=[
            safety_check("patch", "Code change", OperatorSafetyCheckStatus.FAILED, reason),
            _tests_check(context),
        ],
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
    checks = [
        _tests_check(context),
        _ack_check(ack_state),
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
    return _state(
        title="Tests passed with strong validation",
        explanation=(
            "Meaningful tests ran and passed according to Pipewright's process "
            "rules. This does not prove code correctness."
        ),
        waiting_on=OperatorWaitingOn.HUMAN,
        decision_type=OperatorDecisionType.PROGRESS,
        primary_action=_action("approve_chunk", "Approve chunk", "Review and approve the tested chunk when it is otherwise eligible."),
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
