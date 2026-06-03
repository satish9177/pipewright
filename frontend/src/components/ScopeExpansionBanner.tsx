import { useState } from 'react'
import { runsApi } from '@/api/client'
import type { PendingScopeExpansion } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'

interface ScopeExpansionBannerProps {
  runId: string
  chunkNumber: number
  request: PendingScopeExpansion
  // The chunk's original approved scope (chunks.files_expected). Shown for
  // context so the reviewer can compare what was approved vs. what the failed
  // attempt tried to touch. Optional — hidden when unavailable.
  originalFiles?: string[]
  // A short diagnostic summary from the patch failure (the failure message),
  // shown read-only so the reviewer understands why the chunk stopped.
  diagnosticSummary?: string | null
  // Called after a successful approve/reject so the parent can refresh run,
  // chunks, and gates query data using the existing invalidation conventions.
  onActionComplete?: () => void
}

// 409 (conflict / wrong branch / not pending) and 422 (validation / ineligible)
// bodies are either an HTTPException `{ detail }` or the approve route's
// side-effect-free `retry_ineligible` dict `{ status, reason, detail }`. Surface
// the most specific human-readable string we can find; never leak a raw object.
function getScopeActionErrorMessage(error: unknown, fallback: string): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const data = (
      error as {
        response?: {
          data?: { detail?: unknown; reason?: unknown }
        }
      }
    ).response?.data
    if (data) {
      if (typeof data.detail === 'string' && data.detail.trim()) {
        return data.detail
      }
      if (typeof data.reason === 'string' && data.reason.trim()) {
        return data.reason.replace(/_/g, ' ')
      }
    }
  }
  return fallback
}

// Translate the operation status returned by a successful approve-and-retry into
// honest copy. Success does NOT mean completed: the retry may have re-failed.
function approveResultMessage(status: string | undefined): string {
  if (status === 'awaiting_chunk_approval') {
    return 'Scope approved and retry succeeded. Review the recovered chunk before it is committed.'
  }
  if (status === 'failed') {
    return 'Scope approved, but the retry failed again. Review the new failure below.'
  }
  return 'Scope approval submitted. Refreshing run status…'
}

function formatFiles(values: string[]) {
  return values.join(', ')
}

export default function ScopeExpansionBanner({
  runId,
  chunkNumber,
  request,
  originalFiles,
  diagnosticSummary,
  onActionComplete,
}: ScopeExpansionBannerProps) {
  const [pendingAction, setPendingAction] = useState<'approve' | 'reject' | null>(
    null,
  )
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [rejectReason, setRejectReason] = useState('')

  const requestedFiles = request.requested_files ?? []
  // v1 approves exactly the requested files; no UI editing/adding of files and
  // no directory/glob approval. With nothing requestable there is nothing a
  // human could approve, so the approve action is hidden (reject still applies).
  const canApprove = requestedFiles.length > 0
  const actionPending = pendingAction !== null

  const handleApprove = async () => {
    if (!canApprove) return
    setPendingAction('approve')
    setMessage(null)
    setError(null)
    try {
      // Approve scope expansion and retry — NOT code approval. Send the request's
      // requested_files verbatim; the backend re-validates them.
      const result = await runsApi.approveScopeExpansion(
        runId,
        chunkNumber,
        request.request_id,
        requestedFiles,
      )
      setMessage(approveResultMessage(result.status))
      onActionComplete?.()
    } catch (actionError: unknown) {
      setError(
        getScopeActionErrorMessage(
          actionError,
          'Failed to approve scope expansion.',
        ),
      )
      // Refresh anyway: the backend may have advanced the failure/request state
      // (e.g. a re-failure), so a stale view self-corrects.
      onActionComplete?.()
    } finally {
      setPendingAction(null)
    }
  }

  const handleReject = async () => {
    setPendingAction('reject')
    setMessage(null)
    setError(null)
    try {
      await runsApi.rejectScopeExpansion(
        runId,
        chunkNumber,
        request.request_id,
        rejectReason.trim() ? rejectReason.trim() : undefined,
      )
      setMessage(
        'Scope expansion rejected. The chunk stays failed and nothing was committed.',
      )
      onActionComplete?.()
    } catch (actionError: unknown) {
      setError(
        getScopeActionErrorMessage(
          actionError,
          'Failed to reject scope expansion.',
        ),
      )
      onActionComplete?.()
    } finally {
      setPendingAction(null)
    }
  }

  return (
    <div className="grid gap-3 rounded border border-amber-400 bg-amber-50 p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-semibold text-amber-900">
          Scope expansion required
        </p>
        <Badge
          variant="outline"
          className="border-amber-300 bg-amber-100 text-amber-800"
        >
          Awaiting scope approval
        </Badge>
      </div>

      <p className="text-sm text-amber-900">
        This chunk tried to modify files outside the approved scope. Pipewright
        has not committed these changes. Pipewright stopped the chunk and is
        waiting for your decision.
      </p>

      {originalFiles && originalFiles.length > 0 && (
        <div className="text-sm">
          <p className="font-medium text-amber-900">Currently approved scope</p>
          <p className="break-words text-amber-800">
            {formatFiles(originalFiles)}
          </p>
        </div>
      )}

      <div className="text-sm">
        <p className="font-medium text-amber-900">
          Files the previous attempt tried to touch
        </p>
        {requestedFiles.length > 0 ? (
          <ul className="mt-1 list-disc space-y-1 pl-5 text-amber-800">
            {requestedFiles.map(file => (
              <li key={file} className="break-words">
                {file}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-amber-800">
            No requestable extra files were recorded, so there is nothing to
            approve. You can reject this request.
          </p>
        )}
      </div>

      {diagnosticSummary && (
        <div className="text-sm">
          <p className="font-medium text-amber-900">Why it stopped</p>
          <p className="whitespace-pre-wrap break-words text-amber-800">
            {diagnosticSummary}
          </p>
        </div>
      )}

      <p className="rounded border border-amber-300 bg-amber-100 px-3 py-2 text-sm text-amber-900">
        Approving scope expansion is <strong>not</strong> code approval. It only
        allows Pipewright to retry this chunk with the listed files added to the
        allowed scope. If retry succeeds, you will still review and approve the
        recovered chunk before any commit.
      </p>

      <Textarea
        value={rejectReason}
        onChange={event => setRejectReason(event.target.value)}
        placeholder="Optional reason (used if you reject)"
        disabled={actionPending}
      />

      <div className="flex flex-wrap gap-3">
        {canApprove && (
          <Button
            onClick={handleApprove}
            disabled={actionPending}
            className="bg-amber-600 text-white hover:bg-amber-700"
          >
            {pendingAction === 'approve'
              ? 'Approving…'
              : 'Approve scope expansion and retry'}
          </Button>
        )}
        <Button
          variant="destructive"
          onClick={handleReject}
          disabled={actionPending}
        >
          {pendingAction === 'reject' ? 'Rejecting…' : 'Reject scope expansion'}
        </Button>
      </div>

      {message && (
        <p className="text-sm font-medium text-green-700">{message}</p>
      )}
      {error && <p className="text-sm font-medium text-red-600">{error}</p>}
    </div>
  )
}
