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
  has_github_token: boolean
  status: string
  created_at?: string | null
  updated_at?: string | null
}

export interface ProjectCreateRequest {
  name: string
  repo_path: string
  test_command: string
  branch?: string
  description?: string
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
  github_token?: string | null
  github_owner?: string | null
  github_repo?: string | null
  github_base_branch?: string | null
}

export type ProjectCreate = ProjectCreateRequest

export interface Run extends ExtraFields {
  id: string
  project_id?: string | null
  feature_description: string
  plain_english_summary?: string | null
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
  created_at: string
}

export type PipelineRun = Run

export interface LegacyRunStartResponse extends ExtraFields {
  run_id: string
  project_id: string
  status: RunStatus
  message: string
}

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
}

export interface ChunkPlanResponse extends ExtraFields {
  run_id: string
  project_id: string
  chunk_plan_status: ChunkPlanStatus
  total_chunks: number
  current_chunk_number: number
  triage?: TriageResult | null
  chunks: ChunkStatus[]
}

export interface ChunkedRunRequest {
  project_id: string
  feature_description: string
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
}

export type ChunkExecuteResponse = ChunkOperationResponse
export type ChunkResumeResponse = ChunkOperationResponse
export type ChunkApprovalResponse = ChunkOperationResponse
export type ChunkRejectionResponse = ChunkOperationResponse

export interface FinalApprovalResponse extends ExtraFields {
  status: RunStatus
  run_id: string
}

export interface PushPrResponse extends ExtraFields {
  status: RunStatus
  run_id: string
  branch_name: string
  pr_url?: string | null
  pr_number?: number | null
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
}

export interface MemorySuggestionListResponse {
  project_id: string
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
}

export const runsApi = {
  list: () => api.get<Run[]>('/runs').then(r => r.data),
  get: (id: string) =>
    api.get<Run>(`/runs/${id}`).then(r => r.data),
  start: (projectId: string, featureDescription: string) =>
    api.post<LegacyRunStartResponse>('/run', {
      project_id: projectId,
      feature_description: featureDescription,
    }).then(r => r.data),
  createChunkedRun: (projectId: string, featureDescription: string) =>
    api.post<ChunkedRunResponse>('/runs/chunked', {
      project_id: projectId,
      feature_description: featureDescription,
    }).then(r => r.data),
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
  approveFinalApproval: (runId: string) =>
    api.post<FinalApprovalResponse>(`/runs/${runId}/final-approval/approve`).then(r => r.data),
  rejectFinalApproval: (runId: string, reason?: string | null) =>
    api.post<FinalApprovalResponse>(
      `/runs/${runId}/final-approval/reject`,
      { reason },
    ).then(r => r.data),
  pushPr: (runId: string) =>
    api.post<PushPrResponse>(`/runs/${runId}/push-pr`).then(r => r.data),
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
  approveMemorySuggestion: (projectId: string, suggestionId: string) =>
    api.post<MemorySuggestionApprovalResponse>(
      `/api/v1/projects/${projectId}/memory/suggestions/${suggestionId}/approve`,
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
}
