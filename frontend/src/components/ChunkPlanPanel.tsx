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
import ScopeExpansionBanner from '@/components/ScopeExpansionBanner'
import RuntimeTestValidationBanner from '@/components/RuntimeTestValidationBanner'
import AttemptHistory from '@/components/AttemptHistory'
import {
  parsePatchFailureSummary,
  parseRecoveredPatchReviewSummary,
  type RecoveredPatchReviewSummary,
} from '@/utils/patchFailure'
import { extractScopeWarnings } from '@/utils/scopeWarnings'

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
  // Patch retry wiring (#26E2). Optional so existing callers/tests stay valid.
  retryingChunkNumber?: number | null
  // #27F: called after a successful scope expansion approve/reject so the parent
  // refreshes run/chunks/gates query data via the existing invalidation pattern.
  onScopeActionComplete?: () => void
  onApprove: () => void
  onReject: (reason: string) => void
  onExecute: () => void
  onResume: () => void
  onApproveChunk: (chunkNumber: number) => void
  onRejectChunk: (chunkNumber: number, reason: string) => void
  onRetryChunk?: (chunkNumber: number, failureReportId: string) => void
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

// Display-only marker for a recovered patch awaiting review (#26E3). The patch
// was regenerated, applied, and passed tests, but is NOT committed — the
// existing awaiting_chunk_approval UI below still owns approve/commit. This adds
// no approval controls; it is context only.
function RecoveredReviewMarker({
  summary,
}: {
  summary: RecoveredPatchReviewSummary
}) {
  const weakTest = summary.weak_test_warning === true

  return (
    <div className="grid gap-3 rounded border border-green-500 bg-background p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-semibold text-green-600">
          Recovered patch ready for review
        </p>
        {weakTest && (
          <Badge
            variant="outline"
            className="border-amber-300 bg-amber-100 text-amber-800"
          >
            Weak test
          </Badge>
        )}
      </div>

      <p className="text-sm text-muted-foreground">
        Retry applied and tests passed. Review the recovered patch before
        committing.
      </p>

      {weakTest && (
        <p className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          This recovered change passed only a weak test command. Review carefully
          before approving.
        </p>
      )}

      {summary.attempts && summary.attempts.length > 0 && (
        <AttemptHistory attempts={summary.attempts} />
      )}
    </div>
  )
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
  retryingChunkNumber = null,
  onScopeActionComplete,
  onApprove,
  onReject,
  onExecute,
  onResume,
  onApproveChunk,
  onRejectChunk,
  onRetryChunk,
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
            // #27F: a pending scope expansion request makes the scope banner the
            // primary action. When present we suppress the normal #26 Retry
            // button (a SCOPE_VIOLATION otherwise offers retry_with_instruction)
            // so the user is not nudged into the wrong recovery path.
            const pendingScope = chunk.pending_scope_expansion ?? null
            // #26E3: a recovered_patch_review summary is display-only context;
            // the awaiting_chunk_approval UI below still owns approve/commit.
            const recoveredReview = patchFailure
              ? null
              : parseRecoveredPatchReviewSummary(chunk.completion_summary)
            // #22B: surface backend [SCOPE] file-scope notes (#22A) so a
            // reviewer sees scope mismatches/adjustments before approving.
            // Read-only: this only displays existing data, never changes scope.
            const scopeWarnings = extractScopeWarnings(
              definition?.rationale,
              definition?.description
            )
            const hasScopeWarning = scopeWarnings.length > 0
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

                {hasScopeWarning && (
                  <div className="mb-3 grid gap-2 rounded border border-amber-200 bg-amber-50 px-3 py-3 text-sm text-amber-900">
                    <div className="flex items-center gap-2">
                      <Badge
                        variant="outline"
                        className="border-amber-300 bg-amber-100 text-amber-800"
                      >
                        Scope warning
                      </Badge>
                    </div>
                    <p>
                      This chunk has a file-scope mismatch or constraint
                      adjustment. Review Files Expected before approving.
                    </p>
                    <ul className="list-disc space-y-1 pl-5">
                      {scopeWarnings.map((warning, index) => (
                        <li key={index} className="break-words">
                          {warning}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

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

                  <div
                    className={
                      hasScopeWarning
                        ? 'rounded border border-amber-300 bg-amber-50 px-3 py-2'
                        : undefined
                    }
                  >
                    <p className="flex items-center gap-2 font-medium">
                      Files Expected
                      {hasScopeWarning && (
                        <Badge
                          variant="outline"
                          className="border-amber-300 bg-amber-100 text-amber-800"
                        >
                          Review scope
                        </Badge>
                      )}
                    </p>
                    <p
                      className={
                        hasScopeWarning
                          ? 'break-words font-medium text-amber-900'
                          : 'text-muted-foreground break-words'
                      }
                    >
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

                  {/* #28E: display-only runtime test verdict for this chunk.
                      Renders nothing until a verdict is recorded (pending/old
                      chunks). Never gates approval, commit, or PR. */}
                  <RuntimeTestValidationBanner
                    validation={chunk.test_validation}
                  />

                  {pendingScope && (
                    // #27F: primary recovery action for a pending scope
                    // expansion. Rendered above the diagnostic patch-failure
                    // banner; owns its own approve/reject + error display.
                    <ScopeExpansionBanner
                      runId={plan.run_id}
                      chunkNumber={chunk.chunk_number}
                      request={pendingScope}
                      originalFiles={filesExpected}
                      diagnosticSummary={patchFailure?.message ?? null}
                      onActionComplete={onScopeActionComplete}
                    />
                  )}

                  {patchFailure ? (
                    // Structured patch failure (#18E): the banner shows the
                    // message, so skip the raw completion_summary dump and the
                    // generic error_message block to avoid duplication. When a
                    // scope expansion is pending we pass no onRetry so the normal
                    // #26 Retry button is not shown as the primary action (#27F).
                    <PatchFailureBanner
                      report={patchFailure}
                      projectId={plan.project_id}
                      chunkNumber={chunk.chunk_number}
                      chunkStatus={chunk.status}
                      onRetry={pendingScope ? undefined : onRetryChunk}
                      isRetrying={retryingChunkNumber === chunk.chunk_number}
                    />
                  ) : recoveredReview ? (
                    // Recovered patch awaiting review (#26E3): show a marker
                    // instead of dumping the raw recovered_patch_review JSON.
                    <RecoveredReviewMarker summary={recoveredReview} />
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
