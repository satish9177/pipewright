export interface RunEvent {
  id: string
  ts: string
  run_id: string
  chunk_number: number | null
  kind: string
  stage: string | null
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
