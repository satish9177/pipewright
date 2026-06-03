// Parser + types for structured patch failure reports (#18E).
//
// The backend (#18B/#18D) stores a patch failure as a JSON string in
// ChunkStatus.completion_summary, tagged with { "kind": "patch_failure", ... }.
// This helper parses that string defensively: it never throws and returns null
// for anything that is not a well-formed patch-failure report, so success
// summaries and malformed data fall back to the existing plain rendering.

export const PATCH_FAILURE_KIND = 'patch_failure'

// Discriminator the backend (#26D2) stores in completion_summary when a retried
// patch applied successfully and is paused awaiting human review. Distinct from
// PATCH_FAILURE_KIND so parsePatchFailureSummary ignores it.
export const RECOVERED_PATCH_REVIEW_KIND = 'recovered_patch_review'

export interface PatchFailureRetryInfo {
  attempts: number
  max_attempts: number
  retryable: boolean
}

/**
 * One patch-application attempt for a chunk (#26C diagnostics). Mirrors the
 * backend PatchRecoveryAttempt model; every field beyond the ids/number/
 * timestamp is optional so old and partial summaries still parse. Open unions
 * (`(string & {})`) keep this forward-compatible if the backend vocabulary grows.
 */
export interface PatchRecoveryAttempt {
  attempt_id: string
  attempt_number: number
  started_at: string
  recovery_mode:
    | 'initial'
    | 'human'
    | 'human_with_instruction'
    | 'auto'
    | (string & {})
  failure_type?: string | null
  failed_step?: string | null
  changed_files_attempted?: string[]
  changed_files_actual?: string[]
  scope_ok?: boolean | null
  preimage_matched?: boolean | null
  model_used?: string | null
  test_outcome?: 'passed' | 'failed' | 'not_run' | (string & {})
  outcome?: 'failed' | 'recovered' | 'manual_intervention' | (string & {})
  human_decision?: string | null
  working_tree_clean?: boolean
  rollback_performed?: boolean
}

export interface PatchFailureReport {
  kind: 'patch_failure'
  failure_type: string
  message: string
  technical_details?: string | null
  changed_files_attempted?: string[]
  changed_files_actual?: string[]
  allowed_files?: string[]
  suggested_actions?: string[]
  rollback_performed?: boolean
  working_tree_clean?: boolean
  retry?: PatchFailureRetryInfo
  stale_index_hint?: boolean
  chunk_number?: number | null
  failed_step?: string
  manual_intervention_needed?: boolean
  // Recovery diagnostics (#26C). Optional so pre-#26C summaries still parse.
  failure_report_id?: string | null
  attempts?: PatchRecoveryAttempt[]
}

/**
 * Marker stored in completion_summary when a retried patch applied successfully
 * and is paused awaiting human review (#26D2). Tagged with its own `kind` so the
 * existing patch-failure parser ignores it.
 */
export interface RecoveredPatchReviewSummary {
  kind: 'recovered_patch_review'
  failure_report_id: string
  recovery_attempt_id: string
  attempts?: PatchRecoveryAttempt[]
  weak_test_warning?: boolean | null
}

/**
 * Parse a chunk completion_summary string into a PatchFailureReport.
 *
 * Returns null (never throws) for nullish/empty input, invalid JSON,
 * non-objects, arrays, the wrong discriminator, or a missing/non-string
 * failure_type. A valid patch_failure object is returned typed.
 */
export function parsePatchFailureSummary(
  raw: string | null | undefined
): PatchFailureReport | null {
  if (!raw) return null

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }

  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return null
  }

  const candidate = parsed as Record<string, unknown>
  if (candidate.kind !== PATCH_FAILURE_KIND) return null
  if (typeof candidate.failure_type !== 'string') return null

  return candidate as unknown as PatchFailureReport
}

/**
 * Parse a chunk completion_summary string into a RecoveredPatchReviewSummary.
 *
 * Defensive like parsePatchFailureSummary: returns null (never throws) for
 * nullish/empty input, invalid JSON, non-objects, arrays, the wrong
 * discriminator, or a missing/non-string failure_report_id / recovery_attempt_id.
 * Old summaries and patch_failure summaries (wrong `kind`) return null, so the
 * two parsers never both match the same payload.
 */
export function parseRecoveredPatchReviewSummary(
  raw: string | null | undefined
): RecoveredPatchReviewSummary | null {
  if (!raw) return null

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }

  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return null
  }

  const candidate = parsed as Record<string, unknown>
  if (candidate.kind !== RECOVERED_PATCH_REVIEW_KIND) return null
  if (typeof candidate.failure_report_id !== 'string') return null
  if (typeof candidate.recovery_attempt_id !== 'string') return null

  return candidate as unknown as RecoveredPatchReviewSummary
}

const SUGGESTED_ACTION_LABELS: Record<string, string> = {
  retry: 'Retry',
  retry_with_instruction: 'Retry with instruction',
  reindex: 'Re-index and retry',
  reject_chunk: 'Reject chunk',
  mark_manual_intervention: 'Manual intervention needed',
  view_details: 'View details',
}

/** Human label for a suggested action; unknown actions are humanized. */
export function suggestedActionLabel(action: string): string {
  return (
    SUGGESTED_ACTION_LABELS[action] ?? action.replace(/_/g, ' ')
  )
}
