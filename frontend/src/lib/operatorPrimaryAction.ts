// #35F: explicit, conservative mapping from operator_state primary_action IDs to
// the legacy mutation each one already drives. This is the SINGLE source of truth
// for which primary_action IDs are clickable from the OperatorAttentionPanel.
//
// Rules:
// - IDs come verbatim from backend/pipeline/operator_state.py — never invented.
// - We map ONLY primary_action IDs whose legacy control is a single mutation that
//   needs no extra arguments derived by guessing. IDs that require a chunk number
//   or failure-report id (`retry_patch`, `approve_chunk`) are deliberately left
//   out so they fall back to the existing display-only preview.
// - primary_action only ever appears on PROGRESS states (the backend uses
//   neutral_actions for risk_decision), so this never wires a co-equal risk
//   choice. neutral_actions / secondary_actions / blocked_actions remain
//   display-only — wiring them is out of scope for #35F (#35G covers risk).

// Stable keys for the legacy mutations these primary actions map to 1:1. The key
// string intentionally equals the backend action id to document the mapping.
export type LegacyPrimaryHandlerKey =
  | 'approve_plan'
  | 'execute_chunks'
  | 'approve_final'
  | 'create_pr'

const PRIMARY_ACTION_HANDLER_KEYS: Readonly<
  Record<string, LegacyPrimaryHandlerKey>
> = {
  approve_plan: 'approve_plan',
  execute_chunks: 'execute_chunks',
  approve_final: 'approve_final',
  create_pr: 'create_pr',
}

// Returns the legacy handler key for a primary_action id, or null when the id is
// unknown/unmapped. Callers must treat null as "not clickable" (display-only).
export function primaryActionHandlerKey(
  actionId: string | null | undefined,
): LegacyPrimaryHandlerKey | null {
  if (!actionId) return null
  return PRIMARY_ACTION_HANDLER_KEYS[actionId] ?? null
}
