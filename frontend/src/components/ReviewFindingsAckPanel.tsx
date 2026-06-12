import { useState } from 'react'
import { runsApi, type ChunkStatus } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

interface ReviewFindingsAckPanelProps {
  runId: string
  chunks: ChunkStatus[]
  onAcknowledged?: () => void
}

const DEFAULT_ACK_REASON =
  'User acknowledged current high-severity reviewer findings before approval.'

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

function categoryText(categories?: string[]): string {
  if (!categories || categories.length === 0) return 'reviewer finding'
  return categories.join(', ')
}

export default function ReviewFindingsAckPanel({
  runId,
  chunks,
  onAcknowledged,
}: ReviewFindingsAckPanelProps) {
  const [pendingChunk, setPendingChunk] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const requiringChunks = chunks.filter(
    chunk => chunk.review?.requires_acknowledgement === true,
  )
  if (requiringChunks.length === 0) return null

  const outstanding = requiringChunks.filter(chunk => {
    const status = chunk.review?.acknowledgement_status
    return status === 'missing' || status === 'stale'
  })
  const acknowledged = requiringChunks.filter(
    chunk => chunk.review?.acknowledgement_status === 'current',
  )

  const handleAcknowledge = async (chunkNumber: number) => {
    setPendingChunk(chunkNumber)
    setError(null)
    setMessage(null)
    try {
      await runsApi.acknowledgeReviewFindings(
        runId,
        chunkNumber,
        DEFAULT_ACK_REASON,
      )
      setMessage(`Acknowledged reviewer findings for chunk ${chunkNumber}.`)
      onAcknowledged?.()
    } catch (actionError: unknown) {
      setError(
        getAckErrorMessage(
          actionError,
          `Failed to acknowledge reviewer findings for chunk ${chunkNumber}. Refresh and try again.`,
        ),
      )
      onAcknowledged?.()
    } finally {
      setPendingChunk(null)
    }
  }

  return (
    <div className="grid gap-3 rounded border border-red-300 bg-red-50 p-3">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-semibold text-red-900">
          Reviewer findings require acknowledgement before approval
        </p>
        <Badge
          variant="outline"
          className={
            outstanding.length > 0
              ? 'border-red-300 bg-red-100 text-red-800'
              : 'border-emerald-300 bg-emerald-100 text-emerald-800'
          }
        >
          {outstanding.length > 0 ? 'Acknowledgement required' : 'Acknowledged'}
        </Badge>
      </div>

      {outstanding.length > 0 && (
        <p className="text-sm text-red-900">
          The current advisory review has high-severity requirement-mismatch or
          security findings. You can still approve after acknowledging them.
          Acknowledging is not code approval and commits nothing.
        </p>
      )}

      {outstanding.map(chunk => {
        const review = chunk.review
        const stale = review?.acknowledgement_status === 'stale'
        return (
          <div
            key={chunk.chunk_number}
            className="grid gap-2 rounded border border-red-300 bg-white px-3 py-3 text-sm text-red-900"
          >
            <div className="flex flex-wrap items-center gap-2">
              <Badge
                variant="outline"
                className="border-red-300 bg-red-50 text-red-800"
              >
                {categoryText(review?.acknowledgement_required_categories)}
              </Badge>
              <span className="font-medium">
                Chunk {chunk.chunk_number}: {chunk.title}
              </span>
            </div>
            {stale && (
              <p className="text-red-800">
                A previous acknowledgement is stale because this chunk's diff
                changed. A fresh acknowledgement is required.
              </p>
            )}
            <div>
              <Button
                onClick={() => handleAcknowledge(chunk.chunk_number)}
                disabled={pendingChunk !== null}
                className="bg-red-700 text-white hover:bg-red-800"
              >
                {pendingChunk === chunk.chunk_number
                  ? 'Acknowledging...'
                  : 'Acknowledge reviewer findings'}
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
          Reviewer findings acknowledged for chunk {chunk.chunk_number} (current
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
