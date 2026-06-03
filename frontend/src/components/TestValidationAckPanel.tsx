import { useState } from 'react'
import { runsApi } from '@/api/client'
import type { ChunkPlanResponse, ChunkStatus } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

interface TestValidationAckPanelProps {
  runId: string
  plan: ChunkPlanResponse
  // Called after a successful (or failed) acknowledgement so the parent can
  // refresh run/chunks/gates using the existing invalidation conventions.
  onAcknowledged?: () => void
}

// Default human reason recorded when the user acknowledges without typing one.
// Kept short and honest; the backend stores it on the audit row.
const DEFAULT_ACK_REASON =
  'User acknowledged weak/no-test validation before final approval.'

// Surface the most specific backend message we can find for a 409/422/404
// without leaking a raw object. Mirrors ScopeExpansionBanner's extractor.
function getAckErrorMessage(error: unknown, fallback: string): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const data = (
      error as { response?: { data?: { detail?: unknown } } }
    ).response?.data
    if (data && typeof data.detail === 'string' && data.detail.trim()) {
      return data.detail
    }
  }
  return fallback
}

function verdictLabel(chunk: ChunkStatus): string {
  return chunk.test_validation?.verdict === 'none'
    ? 'No tests ran'
    : 'Weak test validation'
}

function countsText(chunk: ChunkStatus): string | null {
  const validation = chunk.test_validation
  if (!validation || !validation.counts_parsed) return null
  const total = validation.total_tests
  const passed = validation.passed_tests
  if (typeof total !== 'number') return null
  if (typeof passed === 'number') {
    return `${passed}/${total} tests passed`
  }
  return `${total} tests reported`
}

/**
 * Final-approval acknowledgement panel (#28G).
 *
 * Surfaces the EXISTING #28F backend gate: when a chunk's runtime test
 * validation is weak or none, final approval is blocked until a human
 * acknowledges the risk against the current diff. This panel only reads the
 * backend-computed acknowledgement_status and calls the acknowledge route — it
 * adds no enforcement of its own, and the backend remains the source of truth.
 *
 * It renders nothing when no chunk requires acknowledgement (all
 * strong/unknown/no-verdict), so strong runs are unaffected.
 */
export default function TestValidationAckPanel({
  runId,
  plan,
  onAcknowledged,
}: TestValidationAckPanelProps) {
  const [pendingChunk, setPendingChunk] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const requiringChunks = plan.chunks.filter(
    chunk => chunk.test_validation?.requires_acknowledgement === true
  )
  if (requiringChunks.length === 0) return null

  const outstanding = requiringChunks.filter(chunk => {
    const status = chunk.test_validation?.acknowledgement_status
    return status === 'missing' || status === 'stale'
  })
  const acknowledged = requiringChunks.filter(
    chunk => chunk.test_validation?.acknowledgement_status === 'current'
  )

  const handleAcknowledge = async (chunkNumber: number) => {
    setPendingChunk(chunkNumber)
    setError(null)
    setMessage(null)
    try {
      await runsApi.acknowledgeTestValidation(
        runId,
        chunkNumber,
        DEFAULT_ACK_REASON
      )
      setMessage(`Acknowledged weak/no-test validation for chunk ${chunkNumber}.`)
      onAcknowledged?.()
    } catch (actionError: unknown) {
      setError(
        getAckErrorMessage(
          actionError,
          `Failed to acknowledge chunk ${chunkNumber}. The diff may have changed; refresh and try again.`
        )
      )
      // Refresh anyway: a stale/changed-diff rejection means our view is out of
      // date, so re-fetching self-corrects the acknowledgement status.
      onAcknowledged?.()
    } finally {
      setPendingChunk(null)
    }
  }

  return (
    <div className="mb-4 grid gap-3 rounded border border-amber-400 bg-amber-50 p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-semibold text-amber-900">
          Weak test validation requires acknowledgement before final approval
        </p>
        <Badge
          variant="outline"
          className="border-amber-300 bg-amber-100 text-amber-800"
        >
          {outstanding.length > 0
            ? 'Acknowledgement required'
            : 'Acknowledged'}
        </Badge>
      </div>

      {outstanding.length > 0 && (
        <p className="text-sm text-amber-900">
          Tests did not meaningfully validate {outstanding.length === 1 ? 'a' : 'some'}{' '}
          change in this run. You can still approve, but you must explicitly
          acknowledge this risk for each affected chunk first. Acknowledging is{' '}
          <strong>not</strong> code approval and commits nothing.
        </p>
      )}

      {outstanding.map(chunk => {
        const counts = countsText(chunk)
        const stale =
          chunk.test_validation?.acknowledgement_status === 'stale'
        return (
          <div
            key={chunk.chunk_number}
            className="grid gap-2 rounded border border-amber-300 bg-amber-100 px-3 py-3 text-sm text-amber-900"
          >
            <div className="flex items-center gap-2">
              <Badge
                variant="outline"
                className="border-amber-300 bg-amber-50 text-amber-800"
              >
                {verdictLabel(chunk)}
              </Badge>
              <span className="font-medium">
                Chunk {chunk.chunk_number}: {chunk.title}
              </span>
            </div>
            {stale && (
              <p className="text-amber-800">
                A previous acknowledgement is stale because this chunk's diff
                changed (a retry or scope amendment). A fresh acknowledgement is
                required.
              </p>
            )}
            {chunk.test_validation?.reason && (
              <p className="text-amber-800">{chunk.test_validation.reason}</p>
            )}
            {counts && <p className="text-amber-800">{counts}</p>}
            <div>
              <Button
                onClick={() => handleAcknowledge(chunk.chunk_number)}
                disabled={pendingChunk !== null}
                className="bg-amber-600 text-white hover:bg-amber-700"
              >
                {pendingChunk === chunk.chunk_number
                  ? 'Acknowledging…'
                  : 'Acknowledge weak test validation'}
              </Button>
            </div>
          </div>
        )
      })}

      {acknowledged.map(chunk => (
        <p
          key={chunk.chunk_number}
          className="text-sm font-medium text-green-700"
        >
          Weak test validation acknowledged for chunk {chunk.chunk_number} (current
          diff).
        </p>
      ))}

      {message && (
        <p className="text-sm font-medium text-green-700">{message}</p>
      )}
      {error && <p className="text-sm font-medium text-red-600">{error}</p>}
    </div>
  )
}
