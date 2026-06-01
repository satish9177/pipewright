// Parser + types for structured patch failure reports (#18E).
//
// The backend (#18B/#18D) stores a patch failure as a JSON string in
// ChunkStatus.completion_summary, tagged with { "kind": "patch_failure", ... }.
// This helper parses that string defensively: it never throws and returns null
// for anything that is not a well-formed patch-failure report, so success
// summaries and malformed data fall back to the existing plain rendering.

export const PATCH_FAILURE_KIND = 'patch_failure'

export interface PatchFailureRetryInfo {
  attempts: number
  max_attempts: number
  retryable: boolean
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
