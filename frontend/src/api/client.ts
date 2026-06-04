import axios from 'axios'

const BASE_URL = 'http://localhost:8001'

export const api = axios.create({
  baseURL: BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
})

type ExtraFields = Record<string, unknown>

export type RunStatus =
  | 'running'
  | 'running_chunks'
  | 'paused'
  | 'failed'
  | 'rejected'
  | 'complete'
  | 'started'
  | 'awaiting_chunk_plan_approval'
  | 'chunk_plan_approved'
  | 'awaiting_chunk_approval'
  | 'chunk_approved'
  | 'awaiting_final_approval'
  | 'final_approved'
  | 'final_rejected'
  | 'pushing'
  | 'push_failed'
  | 'report_ready'
  | 'plan_ready'
  // #27F: a clean SCOPE_VIOLATION created a pending scope expansion request and
  // the run is waiting for a human approve/reject decision (chunk stays failed).
  | 'awaiting_scope_approval'
  | (string & {})

export type RunIntent =
  | 'report_only'
  | 'plan_only'
  | 'implementation'
  | (string & {})

export type ChunkStatusValue =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'rejected'
  | 'awaiting_chunk_approval'
  | (string & {})

export type ChunkPlanStatus =
  | 'awaiting_approval'
  | 'approved'
  | 'rejected'
  | 'none'
  | (string & {})

export type ApprovalStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'timeout'
  | (string & {})

export interface HealthResponse extends ExtraFields {
  status: string
  version: string
}

export type PrMode = 'local_only' | 'github_cli' | 'manual_token'

export interface Project extends ExtraFields {
  id: string
  name: string
  repo_path: string
  test_command: string
  branch: string
  description: string
  github_owner?: string | null
  github_repo?: string | null
  github_base_branch?: string | null
  pr_mode: PrMode | (string & {})
  has_github_token: boolean
  status: string
  created_at?: string | null
  updated_at?: string | null
  // Computed-on-read test command quality (#23A). Derived from test_command;
  // not stored. Used by the UI to warn about weak/unverifiable test commands.
  test_command_quality?: 'weak' | 'likely_test' | 'unknown' | (string & {})
  test_command_quality_reason?: string | null
}

export interface ProjectCreateRequest {
  name: string
  repo_path: string
  test_command: string
  branch?: string
  description?: string
  pr_mode?: PrMode
  github_token?: string
  github_owner?: string
  github_repo?: string
  github_base_branch?: string
}

export interface ProjectUpdateRequest {
  name?: string | null
  test_command?: string | null
  branch?: string | null
  description?: string | null
  pr_mode?: PrMode | null
  github_token?: string | null
  github_owner?: string | null
  github_repo?: string | null
  github_base_branch?: string | null
}

export interface ProjectDetectRequest {
  repo_path: string
}

export interface ProjectDetectResponse {
  repo_path_input: string
  is_git_repo: boolean
  git_root?: string | null
  path_is_git_root: boolean
  current_branch?: string | null
  origin_url?: string | null
  is_github_remote: boolean
  github_owner?: string | null
  github_repo?: string | null
  gh_installed: boolean
  gh_authenticated: boolean
  recommended_pr_mode: PrMode | (string & {})
  warnings: string[]
}

export type ProjectCreate = ProjectCreateRequest

export type ProjectIndexState = 'indexed' | 'not_indexed' | (string & {})

export interface ProjectIndexStatus extends ExtraFields {
  project_id: string
  files_indexed: number
  // SQLite timestamp string (e.g. "2026-06-02 12:34:56") or null when never
  // indexed. Format defensively on the frontend; do not assume ISO-8601.
  indexed_at: string | null
  status: ProjectIndexState
}

export interface ProjectReindexResponse extends ExtraFields {
  status: string
  project_id: string
  target_repo_path?: string
  files_indexed: number
  indexed_at: string | null
  message: string
}

export interface Run extends ExtraFields {
  id: string
  project_id?: string | null
  feature_description: string
  plain_english_summary?: string | null
  report_json?: string | null
  status: RunStatus
  current_step: string | null
  intent?: RunIntent | null
  chunk_plan_status?: ChunkPlanStatus | null
  chunk_plan?: string | null
  total_chunks?: number | null
  current_chunk_number?: number | null
  pr_url?: string | null
  pr_number?: number | null
  branch_name?: string | null
  pushed_at?: string | null
  pr_created_at?: string | null
  push_error?: string | null
  source_plan_run_id?: string | null
  created_at: string
}

export type PipelineRun = Run

export interface Gate extends ExtraFields {
  id: string
  run_id: string
  step?: string
  status: ApprovalStatus
  diff?: string | null
  test_results?: string | null
  ai_summary?: string | null
  plain_english_summary?: string | null
  risk_level: string
  chunk_number?: number | null
  approval_type?: string
  rejection_reason?: string | null
  decided_at?: string | null
  created_at: string
}

export type ApprovalGate = Gate

export interface GateDecisionResponse extends ExtraFields {
  status: ApprovalStatus
  gate_id: string
  reason?: string
}

export interface ChunkDefinition extends ExtraFields {
  chunk_number: number
  title: string
  description: string
  files_expected: string[]
  depends_on: number[]
  risk_level: 'low' | 'medium' | 'high' | (string & {})
  token_estimate: number
  requires_human_review: boolean
  rationale: string
}

export interface TriageResult extends ExtraFields {
  run_id: string
  project_id: string
  feature_description: string
  complexity: 'easy' | 'medium' | 'hard' | (string & {})
  total_chunks: number
  chunks: ChunkDefinition[]
  reasoning: string
}

// #27F: read-only view of the single pending scope expansion request for a
// failed chunk, surfaced by the backend on the chunk-plan payload. requested_files
// are UNTRUSTED and diagnostic only ("the previous attempt tried to touch these")
// — never a list of required files and never authorization. Approval still goes
// through the backend route, which re-validates everything.
export interface PendingScopeExpansion extends ExtraFields {
  request_id: string
  chunk_number: number
  failure_report_id: string
  requested_files: string[]
  status: string
  created_at?: string | null
}

export type TestRunVerdict = 'strong' | 'weak' | 'none' | 'unknown'

// Read-only runtime test-validation evidence for a chunk (#28E), populated from
// the persisted verdict (#28D). Display-only: it never gates approval, commit,
// or PR. Present only when a verdict was recorded; otherwise null.
export type TestValidationAckStatus =
  | 'not_required'
  | 'missing'
  | 'current'
  | 'stale'

export interface TestRunValidation extends ExtraFields {
  verdict: TestRunVerdict
  reason: string
  command_quality?: string | null
  counts_parsed: boolean
  total_tests?: number | null
  passed_tests?: number | null
  failed_tests?: number | null
  zero_tests_detected: boolean
  // Read-only #28F acknowledgement state (#28G). The backend gate is the source
  // of truth; these only drive the acknowledgement UI and pre-disable final
  // approval. requires_acknowledgement is true only for weak/none verdicts.
  requires_acknowledgement?: boolean
  acknowledgement_status?: TestValidationAckStatus
}

// Read-only ADVISORY reviewer overlay (Adversarial Reviewer v1). Display-only
// evidence surfaced on chunk reads; it grants no authority and exposes no actions.
// Mirrors backend ChunkReviewReadModel in backend/models/chunk.py.
export type ChunkReviewStatus = 'completed' | 'failed' | 'unavailable'
export type ChunkReviewStaleness = 'current' | 'stale' | 'missing'
export type ChunkReviewVerdict =
  | 'approve_with_notes'
  | 'needs_human_attention'
  | 'risky'
export type ReviewFindingCategory =
  | 'correctness'
  | 'test_gap'
  | 'scope'
  | 'security'
  | 'maintainability'
  | 'requirement_mismatch'
  | 'uncertainty'
export type ReviewFindingSeverity = 'info' | 'warning' | 'high'

export interface ChunkReviewFinding extends ExtraFields {
  category: ReviewFindingCategory | string
  severity: ReviewFindingSeverity | string
  title: string
  explanation: string
  affected_files: string[]
  suggested_human_check: string
  confidence?: number | null
}

export interface ChunkReview extends ExtraFields {
  review_status: ChunkReviewStatus
  staleness: ChunkReviewStaleness
  verdict?: ChunkReviewVerdict | null
  summary?: string | null
  findings: ChunkReviewFinding[]
  test_gap_summary?: string | null
  scope_summary?: string | null
  security_or_safety_summary?: string | null
  recommended_human_action?: string | null
  reviewed_test_checkpoint_hash?: string | null
  checkpoint_id?: string | null
  provider?: string | null
  model?: string | null
  created_at?: string | null
}

export interface ChunkStatus extends ExtraFields {
  run_id: string
  project_id: string
  chunk_number: number
  title: string
  status: ChunkStatusValue
  risk_level: string
  requires_human_review: boolean
  files_expected: string[]
  depends_on: number[]
  completion_summary?: string | null
  error_message?: string | null
  // Present only when a pending scope expansion request exists for this chunk.
  pending_scope_expansion?: PendingScopeExpansion | null
  // Present only when a runtime test verdict was recorded for this chunk (#28E).
  test_validation?: TestRunValidation | null
  // Present only when an advisory AI review exists for this chunk. Display-only.
  review?: ChunkReview | null
}

// Read-only operator attention state. It is computed on chunk reads and never
// persisted; existing clients can ignore it. Display-only: it never gates,
// triggers, or replaces the real mutating controls. Mirrors the backend
// OperatorState.model_dump() shape in backend/pipeline/operator_state.py.
export type OperatorWaitingOn = 'human' | 'system' | 'nobody'
export type OperatorDecisionType = 'progress' | 'risk_decision' | 'none'
export type OperatorActionSeverity = 'normal' | 'caution' | 'danger'
export type OperatorSafetyCheckStatus =
  | 'passed'
  | 'failed'
  | 'weak'
  | 'not_evaluated'
  | 'not_applicable'

export interface OperatorAction extends ExtraFields {
  id: string
  label: string
  intent: string
  severity: OperatorActionSeverity
  enabled: boolean
  blocked_reason?: string | null
}

export interface OperatorSafetyCheck extends ExtraFields {
  id: string
  label: string
  status: OperatorSafetyCheckStatus
  detail: string
}

export interface OperatorTrustFact extends ExtraFields {
  id: string
  label: string
  detail: string
}

export interface OperatorState extends ExtraFields {
  title: string
  explanation: string
  waiting_on: OperatorWaitingOn
  decision_type: OperatorDecisionType
  schema_version: number
  primary_action?: OperatorAction | null
  neutral_actions: OperatorAction[]
  secondary_actions: OperatorAction[]
  blocked_actions: OperatorAction[]
  safety_checks: OperatorSafetyCheck[]
  trust_facts: OperatorTrustFact[]
  out_of_app_instruction?: string | null
  is_terminal: boolean
  unknown_state_warning?: string | null
}

export interface ChunkPlanResponse extends ExtraFields {
  run_id: string
  project_id: string
  chunk_plan_status: ChunkPlanStatus
  total_chunks: number
  current_chunk_number: number
  triage?: TriageResult | null
  chunks: ChunkStatus[]
  // Additive read-only operator attention state (display-only).
  operator_state?: OperatorState | null
}

export interface ChunkedRunRequest {
  project_id: string
  feature_description: string
}

export type RecommendationStrength = 'strong' | 'weak' | 'none'

export interface NeedsClarificationResponse extends ExtraFields {
  status: 'needs_clarification'
  intent: RunIntent
  message: string
  missing_details: string[]
  examples: string[]
  // Present only on the ambiguous-file clarification (PR #17J/#17M). The
  // candidate set + signed clarification_id let the user pick a file via the
  // selection endpoint; the frontend forwards the raw reply and never parses it.
  candidates?: string[]
  recommended_path?: string | null
  recommendation_strength?: RecommendationStrength
  clarification_id?: string
}

export interface ClarificationSelectionRequest {
  project_id: string
  selection: string
}

export type ChunkedRunResult = ChunkPlanResponse | NeedsClarificationResponse

export function isNeedsClarification(
  value: ChunkedRunResult,
): value is NeedsClarificationResponse {
  return (value as NeedsClarificationResponse).status === 'needs_clarification'
}

export type ChunkedRunResponse = ChunkPlanResponse

export interface RejectRequest {
  reason?: string | null
}

export interface ChunkOperationResponse extends ExtraFields {
  status: RunStatus | ChunkStatusValue | string
  run_id?: string
  message?: string
  completed_chunks?: number
  skipped_chunks?: number
  chunk_number?: number
  failed_chunk?: number
  approval_required?: boolean
  final_approval_required?: boolean
  branch_name?: string
  next_action?: string
  resumed?: boolean
  error?: string
  // Patch retry fields (#26D3b). Present on retry responses: a new
  // failure_report_id on success/re-failure, and the retry_ineligible quartet
  // (eligible/reason/status_code/detail) returned in 409/422 error bodies.
  failure_report_id?: string
  eligible?: boolean
  reason?: string
  status_code?: number
  detail?: string
}

export type ChunkExecuteResponse = ChunkOperationResponse
export type ChunkResumeResponse = ChunkOperationResponse
export type ChunkApprovalResponse = ChunkOperationResponse
export type ChunkRejectionResponse = ChunkOperationResponse

export interface RetryChunkRequest {
  failure_report_id: string
}

export type ChunkRetryResponse = ChunkOperationResponse

// #27F scope expansion approve/reject. Approve sends the human-approved
// allowlist (for v1 the UI approves exactly the request's requested_files); the
// backend re-validates it and re-drives the chunk retry under the amended scope.
// A successful retry still pauses at awaiting_chunk_approval — approval here is
// NOT code approval and never commits.
export interface ApproveScopeExpansionRequest {
  approved_files: string[]
  reason?: string | null
}

export interface RejectScopeExpansionRequest {
  reason?: string | null
}

// Approve reuses the same operation-response shape as retry (carries a `status`
// such as awaiting_chunk_approval on success or failed on re-failure).
export type ScopeExpansionApproveResponse = ChunkOperationResponse

export interface ScopeExpansionRejectResponse extends ExtraFields {
  status: string
  request?: Record<string, unknown>
}

export interface FinalApprovalResponse extends ExtraFields {
  status: RunStatus
  run_id: string
}

// Response from the #28F acknowledge route (#28G). Acknowledgement is a
// precondition for final approval — it commits nothing and is not code approval.
export interface TestValidationAckResponse extends ExtraFields {
  status: string
  run_id: string
  chunk_number: number
  verdict: TestRunVerdict
  acknowledged_diff_hash: string
  acknowledged_at?: string | null
}

export interface PushPrResponse extends ExtraFields {
  status: RunStatus
  run_id: string
  branch_name: string
  pr_url?: string | null
  pr_number?: number | null
}

// Read-only, typed PR/push lifecycle state (#31B). Orthogonal to RunStatus.
export type PrState =
  | 'not_started'
  | 'ready_to_push'
  | 'local_ready'
  | 'local_complete'
  | 'pushing'
  | 'push_failed'
  | 'pr_open'
  | 'unknown'

// Display-only PR checks summary state (#31D). `unavailable` means the checks
// could not be retrieved (gh/network) — it is NOT a failing build.
export type ChecksState =
  | 'unknown'
  | 'pending'
  | 'passed'
  | 'failed'
  | 'unavailable'
  | 'no_checks'

export interface PrChecksSummary extends ExtraFields {
  schema_version: number
  state: ChecksState
  total: number
  passed: number
  failed: number
  pending: number
  skipped: number
  checked_at?: string | null
}

export interface PrPushFailure extends ExtraFields {
  kind: string
  summary: string
  next_action: string
  retryable: boolean
}

// Response from GET /runs/{run_id}/pr-status (#31E). Read-only: it never
// mutates run/approval/push/PR state. `checks` is present only when a PR exists
// and was explicitly refreshed.
export interface PrStatus extends ExtraFields {
  run_id: string
  schema_version: number
  pr_state: PrState
  pr_mode: PrMode
  is_terminal: boolean
  branch_name?: string | null
  pr_url?: string | null
  pr_number?: number | null
  pushed_at?: string | null
  pr_created_at?: string | null
  push_error?: string | null
  failure?: PrPushFailure | null
  checks?: PrChecksSummary | null
}

export type MemoryCategory =
  | 'stack'
  | 'structure'
  | 'test'
  | 'db'
  | 'style'
  | 'security'
  | 'architecture'
  | 'deploy'
  | 'forbidden_paths'
  | 'reviewer_pref'
  | 'other'

export type MemoryScope =
  | 'global'
  | 'backend'
  | 'frontend'
  | 'tests'
  | 'infra'

export type MemoryStatus =
  | 'active'
  | 'stale'
  | 'archived'
  | 'historical'

export type MemorySuggestionStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'archived'

export type MemoryPreviewRole =
  | 'triage'
  | 'planner'
  | 'architect'
  | 'coder'
  | 'reviewer'
  | 'summary'

export interface MemoryFact extends ExtraFields {
  id: string
  project_id: string
  content: string
  category: MemoryCategory | (string & {})
  scope: MemoryScope | (string & {})
  priority: number
  status: MemoryStatus | (string & {})
  source?: string | null
  added_by?: string | null
  approved_by?: string | null
  approved_at?: string | null
  last_verified_at?: string | null
  archived_reason?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface MemoryFactListResponse {
  project_id: string
  facts: MemoryFact[]
}

export interface MemoryListFilters {
  status?: MemoryStatus
  category?: MemoryCategory
  scope?: MemoryScope
}

export interface MemoryCreateRequest {
  content: string
  category: MemoryCategory
  scope: MemoryScope
  priority: number
  source?: string
}

export interface MemoryUpdateRequest {
  content?: string
  category?: MemoryCategory
  scope?: MemoryScope
  priority?: number
}

export interface MemoryArchiveRequest {
  reason: string
}

export interface MemoryVerifyResponse {
  id: string
  project_id: string
  last_verified_at: string
}

export interface MemoryPromptPreviewResponse {
  project_id: string
  role: MemoryPreviewRole | null
  memory_block: string
  empty: boolean
}

export interface MemorySuggestion extends ExtraFields {
  id: string
  project_id: string
  content: string
  category: MemoryCategory | (string & {})
  scope: MemoryScope | (string & {})
  priority: number
  source?: string | null
  evidence_path?: string | null
  evidence_excerpt?: string | null
  status: MemorySuggestionStatus | (string & {})
  created_at?: string | null
  updated_at?: string | null
  approved_by?: string | null
  approved_at?: string | null
  rejected_by?: string | null
  rejected_at?: string | null
  rejection_reason?: string | null
  edited_content?: string | null
  approved_fact_id?: string | null
  source_run_id?: string | null
  source_chunk_number?: number | null
  source_type?: string | null
  source_ref?: string | null
  rationale?: string | null
  suggested_by?: string | null
  risk_level?: string | null
}

export interface MemorySuggestionListResponse {
  project_id: string
  suggestions: MemorySuggestion[]
}

export interface RunMemorySuggestionGenerateResponse extends ExtraFields {
  run_id: string
  project_id: string
  generated_count: number
  skipped_count: number
  blocked_count: number
  suggestions: MemorySuggestion[]
}

export interface BootstrapSuggestionsResponse {
  project_id: string
  suggestions: MemorySuggestion[]
}

export interface MemorySuggestionApprovalResponse {
  suggestion: MemorySuggestion
  fact: MemoryFact
}

export interface MemorySuggestionFilters {
  status?: MemorySuggestionStatus
}

export const healthApi = {
  get: () => api.get<HealthResponse>('/health').then(r => r.data),
}

export const projectsApi = {
  list: () => api.get<Project[]>('/projects').then(r => r.data),
  get: (id: string) => api.get<Project>(`/projects/${id}`).then(r => r.data),
  create: (data: ProjectCreateRequest) =>
    api.post<Project>('/projects', data).then(r => r.data),
  update: (id: string, data: ProjectUpdateRequest) =>
    api.patch<Project>(`/projects/${id}`, data).then(r => r.data),
  delete: (id: string) =>
    api.delete(`/projects/${id}`).then(r => r.data),
  detect: (repo_path: string) =>
    api
      .post<ProjectDetectResponse>('/projects/detect', {
        repo_path,
      } satisfies ProjectDetectRequest)
      .then(r => r.data),
  getIndexStatus: (id: string) =>
    api
      .get<ProjectIndexStatus>(`/projects/${id}/index`)
      .then(r => r.data),
  reindex: (id: string) =>
    api
      .post<ProjectReindexResponse>(`/projects/${id}/reindex`)
      .then(r => r.data),
}

export const runsApi = {
  list: () => api.get<Run[]>('/runs').then(r => r.data),
  get: (id: string) =>
    api.get<Run>(`/runs/${id}`).then(r => r.data),
  createChunkedRun: (projectId: string, featureDescription: string) =>
    api.post<ChunkedRunResult>('/runs/chunked', {
      project_id: projectId,
      feature_description: featureDescription,
    }).then(r => r.data),
  selectClarification: (
    clarificationId: string,
    projectId: string,
    selection: string,
  ) =>
    api.post<ChunkedRunResult>(
      `/runs/chunked/clarifications/${clarificationId}/select`,
      { project_id: projectId, selection },
    ).then(r => r.data),
  getRunChunks: (runId: string) =>
    api.get<ChunkPlanResponse>(`/runs/${runId}/chunks`).then(r => r.data),
  approveChunkPlan: (runId: string) =>
    api.post<ChunkPlanResponse>(`/runs/${runId}/chunks/approve`).then(r => r.data),
  rejectChunkPlan: (runId: string, reason?: string | null) =>
    api.post<ChunkPlanResponse>(`/runs/${runId}/chunks/reject`, { reason }).then(r => r.data),
  executeChunks: (runId: string) =>
    api.post<ChunkExecuteResponse>(`/runs/${runId}/chunks/execute`).then(r => r.data),
  resumeChunks: (runId: string) =>
    api.post<ChunkResumeResponse>(`/runs/${runId}/chunks/resume`).then(r => r.data),
  approveChunk: (runId: string, chunkNumber: number) =>
    api.post<ChunkApprovalResponse>(`/runs/${runId}/chunks/${chunkNumber}/approve`).then(r => r.data),
  rejectChunk: (runId: string, chunkNumber: number, reason?: string | null) =>
    api.post<ChunkRejectionResponse>(
      `/runs/${runId}/chunks/${chunkNumber}/reject`,
      { reason },
    ).then(r => r.data),
  retryChunk: (runId: string, chunkNumber: number, failureReportId: string) =>
    api.post<ChunkRetryResponse>(
      `/runs/${runId}/chunks/${chunkNumber}/retry`,
      { failure_report_id: failureReportId } satisfies RetryChunkRequest,
    ).then(r => r.data),
  approveScopeExpansion: (
    runId: string,
    chunkNumber: number,
    requestId: string,
    approvedFiles: string[],
    reason?: string | null,
  ) =>
    api.post<ScopeExpansionApproveResponse>(
      `/runs/${runId}/chunks/${chunkNumber}/scope-expansion/${requestId}/approve`,
      { approved_files: approvedFiles, reason } satisfies ApproveScopeExpansionRequest,
    ).then(r => r.data),
  rejectScopeExpansion: (
    runId: string,
    chunkNumber: number,
    requestId: string,
    reason?: string | null,
  ) =>
    api.post<ScopeExpansionRejectResponse>(
      `/runs/${runId}/chunks/${chunkNumber}/scope-expansion/${requestId}/reject`,
      { reason } satisfies RejectScopeExpansionRequest,
    ).then(r => r.data),
  acknowledgeTestValidation: (
    runId: string,
    chunkNumber: number,
    reason?: string | null,
  ) =>
    api.post<TestValidationAckResponse>(
      `/runs/${runId}/chunks/${chunkNumber}/test-validation/acknowledge`,
      { reason },
    ).then(r => r.data),
  approveFinalApproval: (runId: string) =>
    api.post<FinalApprovalResponse>(`/runs/${runId}/final-approval/approve`).then(r => r.data),
  rejectFinalApproval: (runId: string, reason?: string | null) =>
    api.post<FinalApprovalResponse>(
      `/runs/${runId}/final-approval/reject`,
      { reason },
    ).then(r => r.data),
  approveMemoryConflict: (runId: string) =>
    api.post<GateDecisionResponse>(`/runs/${runId}/memory-conflict/approve`).then(r => r.data),
  rejectMemoryConflict: (runId: string, reason?: string | null) =>
    api.post<GateDecisionResponse>(
      `/runs/${runId}/memory-conflict/reject`,
      { reason },
    ).then(r => r.data),
  pushPr: (runId: string) =>
    api.post<PushPrResponse>(`/runs/${runId}/push-pr`).then(r => r.data),
  // Explicit, read-only PR status + checks refresh (#31E/#31F). Only called on
  // demand (e.g. a "Refresh PR checks" click) — never automatically on load.
  getPrStatus: (runId: string) =>
    api.get<PrStatus>(`/runs/${runId}/pr-status`).then(r => r.data),
  startImplementation: (runId: string) =>
    api.post<ChunkPlanResponse>(
      `/runs/${runId}/start-implementation`,
    ).then(r => r.data),
}

export const gatesApi = {
  list: () => api.get<Gate[]>('/gates').then(r => r.data),
  get: (id: string) =>
    api.get<Gate>(`/gates/${id}`).then(r => r.data),
  approve: (id: string) =>
    api.post<GateDecisionResponse>(`/gates/${id}/approve`).then(r => r.data),
  reject: (id: string, reason: string) =>
    api.post<GateDecisionResponse>(`/gates/${id}/reject`, { reason }).then(r => r.data),
}

export const memoryApi = {
  listProjectMemory: (projectId: string, filters?: MemoryListFilters) =>
    api.get<MemoryFactListResponse>(
      `/api/v1/projects/${projectId}/memory`,
      {
        params: filters
          ? Object.fromEntries(
              Object.entries(filters).filter(([, value]) => value !== undefined),
            )
          : undefined,
      },
    ).then(r => r.data),
  createProjectMemory: (projectId: string, data: MemoryCreateRequest) =>
    api.post<MemoryFact>(
      `/api/v1/projects/${projectId}/memory`,
      data,
    ).then(r => r.data),
  updateProjectMemory: (
    projectId: string,
    memoryId: string,
    data: MemoryUpdateRequest,
  ) =>
    api.patch<MemoryFact>(
      `/api/v1/projects/${projectId}/memory/${memoryId}`,
      data,
    ).then(r => r.data),
  archiveProjectMemory: (
    projectId: string,
    memoryId: string,
    reason: string,
  ) =>
    api.post<MemoryFact>(
      `/api/v1/projects/${projectId}/memory/${memoryId}/archive`,
      { reason } satisfies MemoryArchiveRequest,
    ).then(r => r.data),
  verifyProjectMemory: (projectId: string, memoryId: string) =>
    api.post<MemoryVerifyResponse>(
      `/api/v1/projects/${projectId}/memory/${memoryId}/verify`,
      {},
    ).then(r => r.data),
  previewProjectMemory: (projectId: string, role: MemoryPreviewRole) =>
    api.get<MemoryPromptPreviewResponse>(
      `/api/v1/projects/${projectId}/memory/prompt-preview`,
      { params: { role } },
    ).then(r => r.data),
  generateBootstrapMemorySuggestions: (projectId: string, force = false) =>
    api.post<BootstrapSuggestionsResponse>(
      `/api/v1/projects/${projectId}/memory/bootstrap-suggestions`,
      { force },
    ).then(r => r.data),
  listMemorySuggestions: (
    projectId: string,
    filters?: MemorySuggestionFilters,
  ) =>
    api.get<MemorySuggestionListResponse>(
      `/api/v1/projects/${projectId}/memory/suggestions`,
      {
        params: filters
          ? Object.fromEntries(
              Object.entries(filters).filter(([, value]) => value !== undefined),
            )
          : undefined,
      },
    ).then(r => r.data),
  approveMemorySuggestion: (
    projectId: string,
    suggestionId: string,
    editedContent?: string,
  ) =>
    api.post<MemorySuggestionApprovalResponse>(
      `/api/v1/projects/${projectId}/memory/suggestions/${suggestionId}/approve`,
      editedContent ? { edited_content: editedContent } : undefined,
    ).then(r => r.data),
  rejectMemorySuggestion: (
    projectId: string,
    suggestionId: string,
    reason: string,
  ) =>
    api.post<MemorySuggestion>(
      `/api/v1/projects/${projectId}/memory/suggestions/${suggestionId}/reject`,
      { reason },
    ).then(r => r.data),
  generateRunMemorySuggestions: (runId: string, requestedBy?: string) =>
    api.post<RunMemorySuggestionGenerateResponse>(
      `/api/v1/runs/${runId}/memory-suggestions/generate`,
      requestedBy ? { requested_by: requestedBy } : {},
    ).then(r => r.data),
}
