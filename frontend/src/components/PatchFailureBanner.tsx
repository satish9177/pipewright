import { useState } from 'react'
import { projectsApi } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
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
}

const VIEW_DETAILS = 'view_details'
const REINDEX = 'reindex'

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
        <p className="text-sm font-semibold text-red-500">Patch failed</p>
        <Badge variant="outline" className="border-red-200 bg-red-100 text-red-700">
          {report.failure_type}
        </Badge>
      </div>

      <p className="text-sm text-red-500 whitespace-pre-wrap">{report.message}</p>

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
            {suggestedActions.map(action => {
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
            {reindexEnabled
              ? 'Re-index is available. Other recovery actions are not wired yet.'
              : 'Recovery actions are not wired yet. Use the details below to decide the next manual step.'}
          </p>
        </div>
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
