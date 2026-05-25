export type RunEventKind =
  | 'stage_started'
  | 'stage_completed'
  | 'stage_failed'
  | 'log'
  | 'heartbeat'
  | 'run_status_changed'
  | 'chunk_status_changed'
  | 'chunk_plan_created'
  | 'chunk_plan_approved'
  | 'chunk_plan_rejected'
  | 'approval_required'
  | 'approval_decided'
  | 'git_operation'
  | 'github_operation'
  | 'rollback'
  | 'terminal'
  | (string & {})

export type RunEventStage =
  | 'triage'
  | 'planner'
  | 'coder'
  | 'patch'
  | 'test'
  | 'approval'
  | 'git'
  | 'github'
  | 'orchestrator'
  | 'system'
  | (string & {})

export interface RunEvent {
  id: string
  ts: string
  run_id: string
  chunk_number: number | null
  kind: RunEventKind
  stage: RunEventStage | null
  level: 'info' | 'warn' | 'error'
  message: string
  data: Record<string, unknown>
}

export type ConnectionStatus =
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'polling-fallback'

export type RunEventSocketMessage =
  | { type: 'event'; event: RunEvent }
  | { type: 'replay_complete'; last_event_id: string | null }
  | { type: 'heartbeat'; ts: string }
  | { type: 'close'; reason: string }
  | { type: 'error'; code?: string; message?: string }
