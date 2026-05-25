import { useState } from 'react'
import type {
  ChunkDefinition,
  ChunkPlanResponse,
  ChunkStatus,
} from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { Textarea } from '@/components/ui/textarea'

interface ChunkPlanPanelProps {
  plan: ChunkPlanResponse
  isApproving: boolean
  isRejecting: boolean
  error: string | null
  onApprove: () => void
  onReject: (reason: string) => void
}

function formatList(values: Array<string | number>) {
  if (values.length === 0) return 'None'
  return values.join(', ')
}

function getChunkDefinition(
  chunk: ChunkStatus,
  definitionsByNumber: Map<number, ChunkDefinition>
) {
  return definitionsByNumber.get(chunk.chunk_number)
}

export default function ChunkPlanPanel({
  plan,
  isApproving,
  isRejecting,
  error,
  onApprove,
  onReject,
}: ChunkPlanPanelProps) {
  const [rejectReason, setRejectReason] = useState('')
  const definitionsByNumber = new Map<number, ChunkDefinition>(
    (plan.triage?.chunks ?? []).map(chunk => [chunk.chunk_number, chunk])
  )
  const isAwaitingApproval = plan.chunk_plan_status === 'awaiting_approval'
  const actionPending = isApproving || isRejecting
  const featureDescription =
    plan.triage?.feature_description || 'Feature description not available.'

  return (
    <Card className="mb-4">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">Chunk Plan</CardTitle>
            <CardDescription>
              Review the planned execution chunks before approving.
            </CardDescription>
          </div>
          <Badge variant="secondary">{plan.chunk_plan_status}</Badge>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid gap-3 text-sm sm:grid-cols-3">
          <div>
            <p className="font-medium">Total Chunks</p>
            <p className="text-muted-foreground">{plan.total_chunks}</p>
          </div>
          <div>
            <p className="font-medium">Current Chunk</p>
            <p className="text-muted-foreground">
              {plan.current_chunk_number || 'None'}
            </p>
          </div>
          {plan.triage?.complexity && (
            <div>
              <p className="font-medium">Complexity</p>
              <p className="text-muted-foreground">
                {plan.triage.complexity}
              </p>
            </div>
          )}
        </div>

        <div>
          <p className="text-sm font-medium mb-1">Feature Description</p>
          <p className="text-sm text-muted-foreground whitespace-pre-wrap">
            {featureDescription}
          </p>
        </div>

        <Separator />

        <div className="grid gap-3">
          {plan.chunks.map(chunk => {
            const definition = getChunkDefinition(chunk, definitionsByNumber)
            const filesExpected =
              chunk.files_expected.length > 0
                ? chunk.files_expected
                : definition?.files_expected ?? []
            const dependsOn =
              chunk.depends_on.length > 0
                ? chunk.depends_on
                : definition?.depends_on ?? []
            const riskLevel =
              chunk.risk_level || definition?.risk_level || 'unknown'
            const requiresHumanReview =
              chunk.requires_human_review ||
              definition?.requires_human_review ||
              false

            return (
              <div
                key={chunk.chunk_number}
                className="rounded border bg-background p-4"
              >
                <div className="mb-3 flex items-start justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium">
                      Chunk {chunk.chunk_number}: {chunk.title}
                    </p>
                    <p className="text-sm text-muted-foreground">
                      {definition?.description || 'No description available.'}
                    </p>
                  </div>
                  <Badge variant="outline">{chunk.status}</Badge>
                </div>

                <div className="grid gap-3 text-sm">
                  <div className="grid gap-3 sm:grid-cols-3">
                    <div>
                      <p className="font-medium">Risk Level</p>
                      <p className="text-muted-foreground">{riskLevel}</p>
                    </div>
                    <div>
                      <p className="font-medium">Token Estimate</p>
                      <p className="text-muted-foreground">
                        {definition?.token_estimate ?? 'Unknown'}
                      </p>
                    </div>
                    <div>
                      <p className="font-medium">Human Review</p>
                      <p className="text-muted-foreground">
                        {requiresHumanReview ? 'Required' : 'Not required'}
                      </p>
                    </div>
                  </div>

                  <div>
                    <p className="font-medium">Files Expected</p>
                    <p className="text-muted-foreground break-words">
                      {formatList(filesExpected)}
                    </p>
                  </div>

                  <div>
                    <p className="font-medium">Depends On</p>
                    <p className="text-muted-foreground">
                      {formatList(dependsOn)}
                    </p>
                  </div>

                  {definition?.rationale && (
                    <div>
                      <p className="font-medium">Rationale</p>
                      <p className="text-muted-foreground whitespace-pre-wrap">
                        {definition.rationale}
                      </p>
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        {isAwaitingApproval && (
          <>
            <Separator />

            <div className="grid gap-3">
              <Textarea
                value={rejectReason}
                onChange={event => setRejectReason(event.target.value)}
                placeholder="Optional rejection reason"
                disabled={actionPending}
              />

              {error && (
                <p className="text-sm font-medium text-red-500">{error}</p>
              )}

              <div className="flex flex-wrap gap-3">
                <Button
                  onClick={onApprove}
                  disabled={actionPending}
                  className="bg-green-600 hover:bg-green-700 text-white"
                >
                  {isApproving ? 'Approving...' : 'Approve Plan'}
                </Button>
                <Button
                  variant="destructive"
                  onClick={() => onReject(rejectReason)}
                  disabled={actionPending}
                >
                  {isRejecting ? 'Rejecting...' : 'Reject Plan'}
                </Button>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
