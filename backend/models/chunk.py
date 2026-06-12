"""
chunk.py
Pydantic contracts for Phase 2B chunk planning.

These models describe a proposed chunk plan only. They do not execute chunks,
apply patches, run tests, or create approvals.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChunkDefinition(BaseModel):
    chunk_number: int = Field(..., ge=1)
    title: str
    description: str
    files_expected: list[str] = Field(default_factory=list)
    depends_on: list[int] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] = "medium"
    token_estimate: int = Field(..., ge=0)
    requires_human_review: bool = False
    rationale: str = ""

    @field_validator("title", "description")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("chunk.py: field must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def _validate_chunk_rules(self):
        for dependency in self.depends_on:
            if dependency >= self.chunk_number:
                raise ValueError(
                    "chunk.py: dependencies must refer to earlier chunks only"
                )
            if dependency < 1:
                raise ValueError(
                    "chunk.py: dependencies must be positive chunk numbers"
                )

        if self.risk_level == "high" and not self.requires_human_review:
            raise ValueError(
                "chunk.py: high risk chunks require human review"
            )

        return self


class TriageResult(BaseModel):
    run_id: str
    project_id: str
    feature_description: str
    complexity: Literal["easy", "medium", "hard"]
    total_chunks: int = Field(..., ge=1)
    chunks: list[ChunkDefinition]
    reasoning: str

    @model_validator(mode="after")
    def _validate_plan_rules(self):
        if self.total_chunks != len(self.chunks):
            raise ValueError(
                "chunk.py: total_chunks must match number of chunks"
            )

        chunk_numbers = [chunk.chunk_number for chunk in self.chunks]
        expected_numbers = list(range(1, self.total_chunks + 1))
        if chunk_numbers != expected_numbers:
            raise ValueError(
                "chunk.py: chunk_numbers must be exactly 1..N with no gaps"
            )

        existing = set()
        for chunk in self.chunks:
            for dependency in chunk.depends_on:
                if dependency not in existing:
                    raise ValueError(
                        "chunk.py: dependencies must refer to existing "
                        "earlier chunks only"
                    )
            existing.add(chunk.chunk_number)

        return self


class PendingScopeExpansion(BaseModel):
    """
    Read-only view of a *pending* scope expansion request, surfaced on a failed
    chunk so the frontend can render the approve/reject UI (#27F).

    This carries only what the UI needs to act and explain: the request id and
    the chunk's failure_report_id (to call the approve/reject routes), the
    untrusted requested_files (diagnostic display only — "the previous attempt
    tried to touch these"), the lifecycle status, and the created timestamp. It
    never carries file contents, diffs, secrets, or token-like values, and it
    grants no authority on its own — approval still goes through the existing
    backend route, which re-validates everything.
    """

    request_id: str
    chunk_number: int
    failure_report_id: str
    requested_files: list[str] = Field(default_factory=list)
    status: str
    created_at: str | None = None


class TestRunValidation(BaseModel):
    """
    Read-only runtime test-validation evidence for a chunk (#28E).

    Display-only surfacing of the verdict persisted in #28D. It never gates,
    blocks, or changes any approval/commit/PR behavior — the future #28F
    acknowledgement gate is the only slice that adds enforcement. Present only
    when a verdict was recorded; otherwise the field is null (e.g. a pre-#28D
    chunk, or one skip-completed from a checkpoint without a fresh test run).
    """

    verdict: Literal["strong", "weak", "none", "unknown"]
    reason: str = ""
    command_quality: str | None = None
    counts_parsed: bool = False
    total_tests: int | None = None
    passed_tests: int | None = None
    failed_tests: int | None = None
    zero_tests_detected: bool = False
    # Read-only acknowledgement state for the #28F final-approval gate (#28G).
    # These describe the EXISTING backend gate; they add no new enforcement. The
    # gate (test_validation_ack_store) remains the source of truth — the frontend
    # uses these only to render the acknowledgement prompt/confirmation and to
    # disable the final-approval button hopefully before the backend would 409.
    #   requires_acknowledgement: this verdict category requires acknowledgement
    #     before final approval (true only for weak/none).
    #   acknowledgement_status: whether that requirement is currently satisfied,
    #     bound to the chunk's current diff hash.
    # Defaults are the safe "nothing required / nothing to do" values, so a chunk
    # plan built without the ack overlay (any non-final read path) never wrongly
    # claims an acknowledgement is outstanding.
    requires_acknowledgement: bool = False
    acknowledgement_status: Literal[
        "not_required", "missing", "current", "stale"
    ] = "not_required"


class ChunkReviewFindingReadModel(BaseModel):
    """One advisory finding, flattened for display. Display hints only — it carries
    no action id and grants no authority."""

    category: str
    severity: str
    title: str
    explanation: str
    affected_files: list[str] = Field(default_factory=list)
    suggested_human_check: str = ""
    confidence: float | None = None
    # Deterministic text the frontend may send to the existing steer route when
    # the chunk status is already failed/completed. This is not an action id, not
    # a new endpoint, and not a scope grant.
    steer_text: str | None = None


class ReviewerIndependenceReadModel(BaseModel):
    """
    Display-only disclosure of whether the advisory reviewer used the SAME
    provider/model as the coder for this exact run/chunk (#33C;
    docs/design/multi-provider-modes.md).

    Computed on read from PERSISTED provenance only — the reviewer's stored
    provider/model and the coder's persisted llm_call_provenance row for the same
    run/chunk — never from current env config (which can change after the run and
    misreport history). This is advisory evidence: it gates nothing, authorizes
    nothing, and never affects operator_state, chunk approval, final approval, or
    reviewer execution. ``status``:

      - ``self_review``  reviewer and coder used the same provider AND model.
      - ``independent``  reviewer used a different provider/model from the coder.
      - ``unknown``      coder (or reviewer) provenance is missing, so independence
                         could not be verified — never reported as independent.
      - ``unavailable``  no completed review exists, so independence does not apply.
    """

    status: Literal["independent", "self_review", "unknown", "unavailable"]
    coder_provider: str | None = None
    coder_model: str | None = None
    reviewer_provider: str | None = None
    reviewer_model: str | None = None
    message: str


class ChunkReviewReadModel(BaseModel):
    """
    Read-only ADVISORY reviewer overlay for a chunk (Adversarial Reviewer v1;
    docs/design/adversarial-reviewer-stage.md).

    This is display-only evidence produced after a chunk's tests passed. It exposes
    NO action ids and NO approve/reject controls, and it must never affect
    operator_state eligibility, chunk approval, or final approval. ``staleness`` is
    computed on read by comparing the review's bound diff/test-checkpoint identity
    to the chunk's current identity (the existing #28F hash concept) — never a new
    hash scheme, never an LLM call. A ``None`` ``review`` on a chunk means "no review
    exists" (i.e. missing); when a review row exists, ``staleness`` is current/stale
    and never falsely current for an indeterminate identity.
    """

    # ``model``/``provider`` are plain metadata names; silence pydantic's
    # protected-namespace warning for the literal field ``model``.
    model_config = ConfigDict(protected_namespaces=())

    review_status: Literal["completed", "failed", "unavailable"]
    staleness: Literal["current", "stale", "missing"]
    verdict: Literal[
        "approve_with_notes", "needs_human_attention", "risky"
    ] | None = None
    summary: str | None = None
    findings: list[ChunkReviewFindingReadModel] = Field(default_factory=list)
    test_gap_summary: str | None = None
    scope_summary: str | None = None
    security_or_safety_summary: str | None = None
    recommended_human_action: str | None = None
    reviewed_test_checkpoint_hash: str | None = None
    checkpoint_id: str | None = None
    provider: str | None = None
    model: str | None = None
    created_at: str | None = None
    # Display-only reviewer-independence disclosure (#33C). Additive: derived on
    # read from persisted coder/reviewer provenance; never gates or authorizes.
    reviewer_independence: ReviewerIndependenceReadModel | None = None
    # Display-only acknowledgement state for Phase 4 item 15. The route
    # preconditions remain the source of truth; this lets the UI render the soft
    # acknowledgement requirement before the backend would return 409.
    requires_acknowledgement: bool = False
    acknowledgement_status: Literal[
        "not_required", "missing", "current", "stale"
    ] = "not_required"
    acknowledgement_required_finding_count: int = 0
    acknowledgement_required_categories: list[str] = Field(default_factory=list)


class ChunkStatus(BaseModel):
    run_id: str
    project_id: str
    chunk_number: int
    title: str
    status: str
    risk_level: str
    requires_human_review: bool
    files_expected: list[str]
    depends_on: list[int]
    completion_summary: str | None = None
    error_message: str | None = None
    # Read-only overlay (#27F): the single pending scope expansion request for
    # this chunk, when one exists. None for the common case. Populated by
    # chunk_store.get_chunk_plan_status; never affects effective scope or
    # execution — it only lets the UI surface the existing approve/reject flow.
    pending_scope_expansion: PendingScopeExpansion | None = None
    # Read-only runtime test-validation verdict (#28E), populated from the
    # persisted chunk test_run_* columns (#28D). None when no verdict was
    # recorded. Display-only: never gates approval, commit, or PR.
    test_validation: TestRunValidation | None = None
    # Read-only ADVISORY reviewer overlay (Adversarial Reviewer v1). Populated on
    # the GET chunk read route from the chunk_reviews store; None when no review
    # exists (missing). Additive and display-only: it exposes no actions, performs
    # no LLM call on read, and never affects operator_state, chunk approval, or
    # final approval. current/stale is computed on read against the existing
    # diff/test-checkpoint identity.
    review: ChunkReviewReadModel | None = None


class ChunkPlanResponse(BaseModel):
    run_id: str
    project_id: str
    chunk_plan_status: str
    total_chunks: int
    current_chunk_number: int
    triage: TriageResult | None = None
    chunks: list[ChunkStatus]
    # Additive read-only operator attention state. It is computed on reads and
    # never persisted; existing clients can ignore it safely.
    operator_state: dict[str, Any] | None = None
    # Additive read-only PR/push status overlay (#31B). Derived on read from the
    # run row + project pr_mode; never persisted, never calls GitHub, and never
    # gates approval or any mutation. None until the run is loadable.
    pr_status: dict[str, Any] | None = None
    # Additive read-only repo-index freshness overlay (#34C). Present on run
    # creation responses when a live freshness check was performed. It never
    # gates existing approval/execution paths; stale run creation returns a
    # separate no-run JSON envelope before this model is built.
    index_freshness: dict[str, Any] | None = None
