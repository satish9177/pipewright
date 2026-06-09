import type { TestRunValidation } from '@/api/client'

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
  retry: 'Retry code change',
  retry_with_instruction: 'Retry with guidance',
  reindex: 'Re-index repo',
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

// Plain-language, user-facing explanation for a patch failure, keyed by the
// backend PatchFailureType (backend/pipeline/patch_failures.py). This is the
// PRIMARY copy a normal user reads; the technical `failure_type` and `message`
// stay available as secondary diagnostics. Safety: nothing here implies the
// retry will succeed, that code is correct, that anything was committed, or that
// tests ran when they did not.
//
// `tests` tracks what we can honestly say about test execution:
//   'not_run' — the change never applied, so tests could not have run.
//   'failed'  — the change applied and tests ran, but failed (rolled back).
//   'unknown' — we cannot tell; stay silent rather than guess.
interface PatchFailurePlainEntry {
  headline: string
  detail: string
  tests: 'not_run' | 'failed' | 'unknown'
}

const PATCH_FAILURE_PLAIN: Record<string, PatchFailurePlainEntry> = {
  PATCH_DOES_NOT_APPLY: {
    headline: 'Code change could not be applied',
    detail:
      'Pipewright generated a change, but the file in your repo did not match what the AI expected, so the change was not applied.',
    tests: 'not_run',
  },
  PATCH_MALFORMED: {
    headline: 'Code change could not be applied',
    detail:
      'Pipewright generated a change in a format it could not apply cleanly, so nothing was changed.',
    tests: 'not_run',
  },
  PATCH_PARTIAL_APPLY_BLOCKED: {
    headline: 'Code change could not be applied',
    detail:
      'Only part of the generated change could be applied, so Pipewright blocked it and applied nothing.',
    tests: 'not_run',
  },
  TARGET_MISSING: {
    headline: 'Code change could not be applied',
    detail:
      'A file the change expected was not found in your repo, so the change was not applied.',
    tests: 'not_run',
  },
  STALE_INDEX_OR_FILE_CHANGED: {
    headline: 'Code change could not be applied',
    detail:
      'A file changed since Pipewright last indexed the repo, so the change no longer matched and was not applied.',
    tests: 'not_run',
  },
  SCOPE_VIOLATION: {
    headline: 'Change touched files outside the approved scope',
    detail:
      'The attempt tried to modify files outside the approved chunk scope, so the change was not applied.',
    tests: 'not_run',
  },
  FORBIDDEN_FILE: {
    headline: 'Change touched a protected file',
    detail:
      'The attempt tried to modify a protected file, so the change was not applied.',
    tests: 'not_run',
  },
  NO_CHANGES: {
    headline: 'No code change was produced',
    detail:
      'Pipewright did not produce any change to apply for this chunk.',
    tests: 'not_run',
  },
  DIRTY_WORKTREE: {
    headline: 'Code change could not be applied',
    detail:
      'The repository working tree was not clean, so Pipewright did not apply the change.',
    tests: 'not_run',
  },
  TEST_FAILURE_AFTER_APPLY: {
    headline: 'Tests failed after the change was applied',
    detail:
      'Pipewright applied the change and ran tests, but the tests failed, so the change was rolled back.',
    tests: 'failed',
  },
  UNKNOWN_PATCH_FAILURE: {
    headline: 'Code change could not be applied',
    detail:
      'Pipewright could not apply the change. See the diagnostic details below.',
    tests: 'unknown',
  },
}

export interface PatchFailurePlainCopy {
  headline: string
  detail: string
  // True for every patch failure: commits only happen after you review and
  // approve a chunk, and a failed patch never reaches that point.
  committedNote: string
  // Honest, non-guessing note about tests; null when we cannot tell.
  testsNote: string | null
}

/**
 * Plain-language copy for a patch failure. Falls back to the safe "unknown"
 * entry for unrecognised failure types so new backend types never render raw.
 */
export function patchFailurePlainCopy(
  failureType: string | null | undefined,
): PatchFailurePlainCopy {
  const entry =
    (failureType && PATCH_FAILURE_PLAIN[failureType]) ||
    PATCH_FAILURE_PLAIN.UNKNOWN_PATCH_FAILURE
  const testsNote =
    entry.tests === 'not_run'
      ? 'Tests did not run because the code change was not applied.'
      : entry.tests === 'failed'
        ? 'Tests ran and failed, so the change was not kept.'
        : null
  return {
    headline: entry.headline,
    detail: entry.detail,
    committedNote: 'Nothing was committed.',
    testsNote,
  }
}

/**
 * Plain-language framing sentence for the "Patch recovery" section (#41B) that
 * wraps the PatchFailureBanner. The old copy ("The code change could not be
 * applied or was blocked...") is false for TEST_FAILURE_AFTER_APPLY, where the
 * change DID apply and tests failed afterward. This returns failure-specific,
 * truthful copy:
 *
 *   - TEST_FAILURE_AFTER_APPLY: states the change applied and tests failed.
 *     Rollback wording is guarded by `rollbackPerformed` — we only claim a
 *     rollback happened when the backend recorded one.
 *   - Every other failure type: generic copy that never overclaims what went
 *     wrong (apply vs. block vs. no-change) and never implies tests ran.
 *
 * Safety: never implies tests passed, never implies a commit, and never claims a
 * rollback that did not happen. Raw diagnostics/attempt history still render
 * below this sentence unchanged.
 */
export function patchRecoveryFrameCopy(
  failureType: string | null | undefined,
  rollbackPerformed: boolean | null | undefined,
): string {
  if (failureType === 'TEST_FAILURE_AFTER_APPLY') {
    return rollbackPerformed === true
      ? 'The change was applied, tests failed, and Pipewright rolled it back. ' +
          'Nothing was committed.'
      : 'The change was applied, but tests failed. Nothing was committed.'
  }
  return (
    'Pipewright stopped before committing. Nothing was committed. Review the ' +
    'failure details and recovery attempts below.'
  )
}

/**
 * Compact, honest "tests failed" count summary for a TEST_FAILURE_AFTER_APPLY
 * (#40D), derived purely from the already-parsed test_validation counts persisted
 * for the chunk. It never parses raw output and never invents numbers:
 *
 *   - Returns null for any failure type other than TEST_FAILURE_AFTER_APPLY.
 *   - Returns null when counts were not reliably parsed (counts_parsed !== true)
 *     or when no failures were counted, so the caller degrades gracefully to the
 *     plain "Tests ran and failed" sentence — never a fabricated "0 tests failed".
 *   - When a total is available, shows "N of M tests failed."; otherwise the
 *     bare "N test(s) failed.".
 *
 * Safety: only ever reported for a confirmed test failure, never implies tests
 * passed, and shows nothing unless the backend already parsed a failed count.
 */
export function testFailureCountSummary(
  failureType: string | null | undefined,
  validation: TestRunValidation | null | undefined,
): string | null {
  if (failureType !== 'TEST_FAILURE_AFTER_APPLY') return null
  if (!validation || validation.counts_parsed !== true) return null

  const failed = validation.failed_tests
  if (typeof failed !== 'number' || failed < 1) return null

  const total = validation.total_tests
  if (typeof total === 'number' && total >= failed) {
    return `${failed} of ${total} tests failed.`
  }
  return `${failed} test${failed === 1 ? '' : 's'} failed.`
}
