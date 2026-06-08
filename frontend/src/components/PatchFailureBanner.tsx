import { useState } from 'react'
import { projectsApi } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import AttemptHistory from '@/components/AttemptHistory'
import {
  patchFailurePlainCopy,
  suggestedActionLabel,
  type PatchFailureReport,
} from '@/utils/patchFailure'

interface PatchFailureBannerProps {
  report: PatchFailureReport
  // When provided, the `reindex` suggested action becomes an enabled button
  // that re-indexes this project's repo (#19D). It only re-indexes; it never
  // retries the failed chunk. Without it, `reindex` stays a disabled
  // placeholder like the other unwired recovery actions.
  projectId?: string
  // Retry wiring (#26E2). When the chunk is failed, carries a failure_report_id,
  // a plain retry is suggested, retry is authoritatively eligible, and a callback
  // is wired, an enabled Retry button calls back with the current
  // failure_report_id. The backend remains the source of truth for eligibility —
  // a rejected retry is safe and surfaces a clear message.
  chunkNumber?: number
  chunkStatus?: string
  onRetry?: (chunkNumber: number, failureReportId: string) => void
  // Authoritative human-retry eligibility (#26E2 affordance parity). Derived from
  // operator_state (evaluate_patch_retry_eligibility) by the page and threaded in
  // so the enabled "Retry code change" button matches the OperatorAttentionPanel
  // and the route gate. False/omitted keeps retry as a disabled placeholder even
  // when a recovery action is suggested. Display-only; wires no new behavior.
  retryEligible?: boolean
  isRetrying?: boolean
}

const VIEW_DETAILS = 'view_details'
const REINDEX = 'reindex'
const RETRY = 'retry'

function formatFiles(values: string[]) {
  return values.join(', ')
}

function getReindexErrorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (
      error as { response?: { status?: number; data?: { detail?: unknown } } }
    ).response
    if (response?.status === 409) {
      return 'A run is active for this project — re-index when it finishes.'
    }
    if (typeof response?.data?.detail === 'string') {
      return response.data.detail
    }
  }
  return 'Re-index failed.'
}

export default function PatchFailureBanner({
  report,
  projectId,
  chunkNumber,
  chunkStatus,
  onRetry,
  retryEligible = false,
  isRetrying = false,
}: PatchFailureBannerProps) {
  const [showDetails, setShowDetails] = useState(false)
  const [reindexing, setReindexing] = useState(false)
  const [reindexMessage, setReindexMessage] = useState<string | null>(null)
  const [reindexError, setReindexError] = useState<string | null>(null)

  const attempted = report.changed_files_attempted ?? []
  const actual = report.changed_files_actual ?? []
  const suggestedActions = report.suggested_actions ?? []
  const technicalDetails = report.technical_details ?? ''

  // The reindex action is only actionable when we know which project to scan.
  const reindexEnabled =
    suggestedActions.includes(REINDEX) && Boolean(projectId)

  // Retry eligibility (#26E2). The enabled "Retry code change" button mirrors the
  // authoritative human-retry eligibility (`retryEligible`, derived by the page
  // from operator_state / evaluate_patch_retry_eligibility) so it agrees with the
  // OperatorAttentionPanel and the route gate — not merely the presence of a
  // recovery suggestion. We also require the plain `retry` action:
  // `retry_with_instruction` is a different recovery mode (replan with a human
  // instruction) and must NOT trigger the plain retry route, so it falls through
  // to a disabled placeholder. The backend route remains the enforcing gate — a
  // rejected retry is still safe and surfaces a clear message.
  const canRetry =
    chunkStatus === 'failed' &&
    Boolean(report.failure_report_id) &&
    suggestedActions.includes(RETRY) &&
    retryEligible === true &&
    typeof onRetry === 'function' &&
    typeof chunkNumber === 'number'

  const handleReindex = async () => {
    if (!projectId) return
    setReindexing(true)
    setReindexMessage(null)
    setReindexError(null)
    try {
      // Re-index only. This deliberately does NOT retry the failed chunk.
      const result = await projectsApi.reindex(projectId)
      setReindexMessage(
        result.message || `Re-indexed ${result.files_indexed} files.`,
      )
    } catch (error: unknown) {
      setReindexError(getReindexErrorMessage(error))
    } finally {
      setReindexing(false)
    }
  }

  // Primary, plain-language explanation of what happened. The backend
  // failure_type/message stay available below as secondary diagnostics.
  const plain = patchFailurePlainCopy(report.failure_type)

  const rollbackLabel = report.rollback_performed
    ? 'Rolled back'
    : 'Rollback not performed'
  const treeNotClean = report.working_tree_clean === false

  // Only show the "actual" list when it differs from what was attempted.
  const showActual =
    actual.length > 0 && formatFiles(actual) !== formatFiles(attempted)

  return (
    <div className="grid gap-3 rounded border border-red-500 bg-background p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-semibold text-red-500">{plain.headline}</p>
        <Badge
          variant="outline"
          className="border-red-200 bg-red-100 text-red-700"
          title="Technical failure code (diagnostic)"
        >
          {report.failure_type}
        </Badge>
      </div>

      <div className="grid gap-1 text-sm">
        <p className="text-foreground">{plain.detail}</p>
        <p className="text-muted-foreground">{plain.committedNote}</p>
        {plain.testsNote && (
          <p className="text-muted-foreground">{plain.testsNote}</p>
        )}
      </div>

      {report.message && (
        <p className="whitespace-pre-wrap text-xs text-muted-foreground">
          Diagnostic: {report.message}
        </p>
      )}

      <div className="grid gap-1 text-sm">
        <p className="text-muted-foreground">{rollbackLabel}</p>
        {treeNotClean ? (
          <p className="font-medium text-red-500">
            Manual intervention needed — working tree is not clean
          </p>
        ) : (
          report.working_tree_clean === true && (
            <p className="text-muted-foreground">Working tree clean</p>
          )
        )}
        {report.manual_intervention_needed === true && !treeNotClean && (
          <p className="font-medium text-red-500">
            Manual intervention needed before this run can continue.
          </p>
        )}
      </div>

      {report.stale_index_hint === true && (
        <p className="text-xs text-amber-700">
          This is based on the current repo index. Re-index if recently
          added/removed files are missing.
        </p>
      )}

      {attempted.length > 0 && (
        <div className="text-sm">
          <p className="font-medium">Files attempted</p>
          <p className="text-muted-foreground break-words">
            {formatFiles(attempted)}
          </p>
        </div>
      )}

      {showActual && (
        <div className="text-sm">
          <p className="font-medium">Files changed on disk</p>
          <p className="text-muted-foreground break-words">
            {formatFiles(actual)}
          </p>
        </div>
      )}

      {suggestedActions.length > 0 && (
        <div className="grid gap-2">
          <div className="flex flex-wrap gap-2">
            {canRetry && (
              <Button
                size="sm"
                onClick={() => onRetry!(chunkNumber!, report.failure_report_id!)}
                disabled={isRetrying}
              >
                {isRetrying ? 'Retrying…' : 'Retry code change'}
              </Button>
            )}
            {suggestedActions.map(action => {
              // The dedicated Retry button above replaces the placeholder when
              // retry is actually wired; otherwise fall through to the disabled
              // placeholder (unchanged behavior).
              if (action === RETRY && canRetry) {
                return null
              }
              if (action === VIEW_DETAILS) {
                return (
                  <Button
                    key={action}
                    size="sm"
                    variant="outline"
                    onClick={() => setShowDetails(previous => !previous)}
                    aria-expanded={showDetails}
                  >
                    {showDetails ? 'Hide details' : suggestedActionLabel(action)}
                  </Button>
                )
              }
              if (action === REINDEX && reindexEnabled) {
                return (
                  <Button
                    key={action}
                    size="sm"
                    variant="outline"
                    onClick={handleReindex}
                    disabled={reindexing}
                  >
                    {reindexing
                      ? 'Re-indexing…'
                      : 'Re-index and refresh index'}
                  </Button>
                )
              }
              return (
                <Button key={action} size="sm" variant="outline" disabled>
                  {suggestedActionLabel(action)}
                </Button>
              )
            })}
          </div>
          {reindexMessage && (
            <p className="text-xs font-medium text-green-600">
              {reindexMessage}
            </p>
          )}
          {reindexError && (
            <p className="text-xs font-medium text-red-500">{reindexError}</p>
          )}
          <p className="text-xs text-muted-foreground">
            {canRetry
              ? 'Retrying asks the AI to generate the change again. It may succeed or fail again, and nothing is committed until you review and approve the result.'
              : reindexEnabled
                ? 'Re-indexing refreshes Pipewright’s view of your repo so a retry can match the current files. Use the details below to decide the next step.'
                : 'Use the details below to decide the next manual step.'}
          </p>
        </div>
      )}

      {report.attempts && report.attempts.length > 0 && (
        <AttemptHistory attempts={report.attempts} />
      )}

      {showDetails && technicalDetails && (
        <pre className="max-h-64 overflow-auto rounded border bg-muted p-3 font-mono text-xs whitespace-pre-wrap break-words">
          {technicalDetails}
        </pre>
      )}
      {showDetails && !technicalDetails && (
        <p className="text-xs text-muted-foreground">
          No technical details were captured.
        </p>
      )}
    </div>
  )
}
