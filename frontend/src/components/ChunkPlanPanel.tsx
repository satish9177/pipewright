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
import { getStatusDisplay } from '@/utils/statusDisplay'
import PatchFailureBanner from '@/components/PatchFailureBanner'
import { parsePatchFailureSummary } from '@/utils/patchFailure'

interface ChunkPlanPanelProps {
  plan: ChunkPlanResponse
  isApproving: boolean
  isRejecting: boolean
  isExecuting: boolean
  isResuming: boolean
  approvingChunkNumber: number | null
  rejectingChunkNumber: number | null
  error: string | null
  executionMessage: string | null
  executionError: string | null
  chunkActionMessage: string | null
  chunkActionError: string | null
  hiddenApprovalChunkNumbers?: number[]
  onApprove: () => void
  onReject: (reason: string) => void
  onExecute: () => void
  onResume: () => void
  onApproveChunk: (chunkNumber: number) => void
  onRejectChunk: (chunkNumber: number, reason: string) => void
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
  isExecuting,
  isResuming,
  approvingChunkNumber,
  rejectingChunkNumber,
  error,
  executionMessage,
  executionError,
  chunkActionMessage,
  chunkActionError,
  hiddenApprovalChunkNumbers = [],
  onApprove,
  onReject,
  onExecute,
  onResume,
  onApproveChunk,
  onRejectChunk,
}: ChunkPlanPanelProps) {
  const [rejectReason, setRejectReason] = useState('')
  const [chunkRejectReasons, setChunkRejectReasons] = useState<
    Record<number, string>
  >({})
  const definitionsByNumber = new Map<number, ChunkDefinition>(
    (plan.triage?.chunks ?? []).map(chunk => [chunk.chunk_number, chunk])
  )
  const isAwaitingApproval = plan.chunk_plan_status === 'awaiting_approval'
  const isApproved = plan.chunk_plan_status === 'approved'
  const actionPending =
    isApproving ||
    isRejecting ||
    isExecuting ||
    isResuming ||
    approvingChunkNumber !== null ||
    rejectingChunkNumber !== null
  const featureDescription =
    plan.triage?.feature_description || 'Feature description not available.'
  const planStatusDisplay = getStatusDisplay(plan.chunk_plan_status)

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
          <Badge variant="outline" className={planStatusDisplay.className}>
            {planStatusDisplay.label}
          </Badge>
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

        {isApproved && (
          <>
            <div className="grid gap-3 rounded border bg-background p-4">
              <div>
                <p className="text-sm font-medium">Execution Controls</p>
                <p className="text-sm text-muted-foreground">
                  Execute approved chunks or resume the run after an interruption,
                  failed chunk, or high-risk approval.
                </p>
              </div>

              {executionMessage && (
                <p className="text-sm font-medium text-green-600">
                  {executionMessage}
                </p>
              )}
              {executionError && (
                <p className="text-sm font-medium text-red-500">
                  {executionError}
                </p>
              )}

              <div className="flex flex-wrap gap-3">
                <Button
                  onClick={onExecute}
                  disabled={actionPending}
                  className="bg-blue-600 hover:bg-blue-700 text-white"
                >
                  {isExecuting ? 'Executing...' : 'Execute Chunks'}
                </Button>
                <Button
                  variant="outline"
                  onClick={onResume}
                  disabled={actionPending}
                >
                  {isResuming ? 'Resuming...' : 'Resume Run'}
                </Button>
              </div>
            </div>

            <Separator />
          </>
        )}

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
            const patchFailure = parsePatchFailureSummary(
              chunk.completion_summary
            )
            const isAwaitingChunkApproval =
              chunk.status === 'awaiting_chunk_approval'
            const showInlineChunkApproval =
              isAwaitingChunkApproval &&
              !patchFailure &&
              !hiddenApprovalChunkNumbers.includes(chunk.chunk_number)
            const chunkActionPending =
              approvingChunkNumber === chunk.chunk_number ||
              rejectingChunkNumber === chunk.chunk_number
            const chunkStatusDisplay = getStatusDisplay(chunk.status)

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
                  <Badge variant="outline" className={chunkStatusDisplay.className}>
                    {chunkStatusDisplay.label}
                  </Badge>
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
                    {filesExpected.length === 0 && (
                      <p className="mt-1 text-xs text-muted-foreground">
                        Pipewright could not confidently map this chunk to
                        indexed repository files, so no target files were
                        approved. It is marked high-risk and requires human
                        review — confirm the real target files before
                        executing.
                      </p>
                    )}
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

                  {patchFailure ? (
                    // Structured patch failure (#18E): the banner shows the
                    // message, so skip the raw completion_summary dump and the
                    // generic error_message block to avoid duplication.
                    <PatchFailureBanner report={patchFailure} />
                  ) : (
                    <>
                      {chunk.completion_summary && (
                        <div>
                          <p className="font-medium">Completion Summary</p>
                          <p className="text-muted-foreground whitespace-pre-wrap">
                            {chunk.completion_summary}
                          </p>
                        </div>
                      )}

                      {chunk.error_message && (
                        <div>
                          <p className="font-medium text-red-500">Error</p>
                          <p className="text-red-500 whitespace-pre-wrap">
                            {chunk.error_message}
                          </p>
                        </div>
                      )}
                    </>
                  )}

                  {showInlineChunkApproval && (
                    <div className="grid gap-3 rounded border border-yellow-400 p-3">
                      <div>
                        <p className="font-medium">High-Risk Chunk Approval</p>
                        <p className="text-muted-foreground">
                          Review this chunk before continuing execution.
                        </p>
                      </div>

                      <Textarea
                        value={chunkRejectReasons[chunk.chunk_number] ?? ''}
                        onChange={event =>
                          setChunkRejectReasons(previous => ({
                            ...previous,
                            [chunk.chunk_number]: event.target.value,
                          }))
                        }
                        placeholder="Optional rejection reason"
                        disabled={actionPending}
                      />

                      <div className="flex flex-wrap gap-3">
                        <Button
                          onClick={() => onApproveChunk(chunk.chunk_number)}
                          disabled={actionPending}
                          className="bg-green-600 hover:bg-green-700 text-white"
                        >
                          {chunkActionPending && approvingChunkNumber
                            ? 'Approving...'
                            : 'Approve Chunk'}
                        </Button>
                        <Button
                          variant="destructive"
                          onClick={() =>
                            onRejectChunk(
                              chunk.chunk_number,
                              chunkRejectReasons[chunk.chunk_number] ?? ''
                            )
                          }
                          disabled={actionPending}
                        >
                          {chunkActionPending && rejectingChunkNumber
                            ? 'Rejecting...'
                            : 'Reject Chunk'}
                        </Button>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        {(chunkActionMessage || chunkActionError) && (
          <div>
            {chunkActionMessage && (
              <p className="text-sm font-medium text-green-600">
                {chunkActionMessage}
              </p>
            )}
            {chunkActionError && (
              <p className="text-sm font-medium text-red-500">
                {chunkActionError}
              </p>
            )}
          </div>
        )}

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
