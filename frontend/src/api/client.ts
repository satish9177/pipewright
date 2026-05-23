import axios from 'axios'

const BASE_URL = 'http://localhost:8001'

export const api = axios.create({
  baseURL: BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
})

export interface Project {
  id: string
  name: string
  repo_path: string
  test_command: string
  branch: string
  description: string
  github_owner?: string
  github_repo?: string
  github_base_branch?: string
  is_active: number
  created_at: string
}

export interface ProjectCreate {
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

export interface PipelineRun {
  id: string
  project_id: string
  feature_description: string
  status: string
  current_step: string
  created_at: string
}

export interface ApprovalGate {
  id: string
  run_id: string
  status: string
  diff?: string
  test_results?: string
  ai_summary?: string
  risk_level: string
  created_at: string
}

export const projectsApi = {
  list: () => api.get<Project[]>('/projects').then(r => r.data),
  get: (id: string) => api.get<Project>(`/projects/${id}`).then(r => r.data),
  create: (data: ProjectCreate) =>
    api.post<Project>('/projects', data).then(r => r.data),
  update: (id: string, data: Partial<ProjectCreate>) =>
    api.patch<Project>(`/projects/${id}`, data).then(r => r.data),
  delete: (id: string) =>
    api.delete(`/projects/${id}`).then(r => r.data),
}

export const runsApi = {
  list: () => api.get<PipelineRun[]>('/runs').then(r => r.data),
  get: (id: string) =>
    api.get<PipelineRun>(`/runs/${id}`).then(r => r.data),
  start: (projectId: string, featureDescription: string) =>
    api.post('/run', {
      project_id: projectId,
      feature_description: featureDescription,
    }).then(r => r.data),
}

export const gatesApi = {
  list: () => api.get<ApprovalGate[]>('/gates').then(r => r.data),
  get: (id: string) =>
    api.get<ApprovalGate>(`/gates/${id}`).then(r => r.data),
  approve: (id: string) =>
    api.post(`/gates/${id}/approve`).then(r => r.data),
  reject: (id: string, reason: string) =>
    api.post(`/gates/${id}/reject`, { reason }).then(r => r.data),
}
